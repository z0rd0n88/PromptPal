"""System prompt seed / read / update (US-007 / SPEC §11, P1-SP-01..05, D-3).

The system prompt is the single piece of state that lives at
``~/.promptpal/system-prompt.md`` and is treated as **user data**: it is
seeded from the bundled :data:`BUNDLED_SYSTEM_PROMPT_PATH` on first run
and is **never overwritten** afterwards except by the explicit
``--update-system-prompt`` flow.

Public surface
--------------

- :func:`seed_system_prompt` — copy the bundled default into the user's
  config dir if and only if the target file doesn't already exist
  (P1-SP-01, P1-SP-02). Returns ``True`` when a seed happened, ``False``
  otherwise.
- :func:`resolve_system_prompt_path` — collapses the (``--system-prompt``
  flag, ``Config.system_prompt_path``) pair into the path to use for
  this invocation. The flag wins but is **not persisted** (P1-SP-04).
- :func:`read_system_prompt` — read the resolved path and raise
  :class:`SystemPromptMissingError` with the canonical "System prompt
  file not found at <path>. Run with ``--update-system-prompt`` to
  restore the default." message when the file is missing or unreadable
  (P1-SP-05 / P1-ERR-15 partner — exits via the CLI layer, not here).
- :func:`update_system_prompt` — implements the ``--update-system-prompt``
  flow (P1-SP-03 / D-3): GET ``url`` and ``url + ".sha256"``, verify the
  SHA-256 matches, back the current file up to ``<target>.bak``, then
  atomically replace via ``tempfile.mkstemp`` + ``os.replace``. Any
  failure (download, mismatch) leaves the existing file untouched.

Error hierarchy
---------------

- :class:`SystemPromptError`            — base.
- :class:`SystemPromptMissingError`     — read-time miss (P1-SP-05, P1-ERR-15-adjacent).
- :class:`SystemPromptChecksumError`    — update-time SHA mismatch (P1-ERR-14).
- :class:`SystemPromptDownloadError`    — update-time network/HTTP failure (P1-ERR-15).

All errors carry the canonical message strings the CLI surfaces
verbatim; tests pin them so accidental drift breaks the suite.

Testability
-----------

:func:`update_system_prompt` accepts an injectable ``fetcher`` callable
``(url) -> bytes`` that raises :class:`SystemPromptDownloadError` on
failure. The default is :func:`_default_fetcher` (urllib-backed). Tests
pass a programmable fake without touching the network.

Atomic write contract
---------------------

The replace path uses the same pattern as
:func:`core.config.save_config`: ``tempfile.mkstemp`` in the same
directory as the target, write payload, ``os.replace``. The tempfile
is unlinked on failure. This guarantees that a crash mid-write leaves
either the old file intact (with ``.bak`` co-located) or the new file
complete — never a half-written file (NFR-04).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from core._io import _fsync_dir_best_effort, atomic_write_bytes
from core.config import Config


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUNDLED_SYSTEM_PROMPT_PATH: Path = (
    Path(__file__).resolve().parent / "system_prompt.txt"
)
"""Absolute path to the bundled default prompt (D-3)."""

BACKUP_SUFFIX: str = ".bak"
"""Appended to the target file's name (not just its suffix) — i.e.
``system-prompt.md`` → ``system-prompt.md.bak`` (P1-SP-03 / risk table)."""

SHA256_SIDECAR_SUFFIX: str = ".sha256"
"""Co-located sidecar fetched alongside the prompt URL (D-3)."""

MISSING_MESSAGE_TEMPLATE: str = (
    "System prompt file not found at {path}. "
    "Run with --update-system-prompt to restore the default."
)
"""Canonical P1-SP-05 message; ``{path}`` is interpolated with the resolved path."""

CHECKSUM_MISMATCH_MESSAGE: str = (
    "System prompt checksum mismatch — refusing to overwrite. "
    "Verify Config.system_prompt_update_url."
)
"""Canonical P1-ERR-14 message — pinned verbatim by tests."""

DOWNLOAD_FAILED_MESSAGE_TEMPLATE: str = (
    "Could not fetch system prompt from {url}: {reason}"
)
"""Canonical P1-ERR-15 template — used for both main and sidecar fetch failures."""

XML_TAGS_DIRECTIVE: str = (
    "Output-structure option: when the prompt has multiple parts, you may "
    "delimit the sections of the rewritten prompt with XML-style tags "
    "(<task>, <input>, <constraints>, <output_format>) instead of plain "
    "section headings."
)
"""Opt-in instruction appended to the system prompt only when ``--xml-tags``
is passed. The bundled default prompt deliberately omits the XML-style-tags
option (it prefers plain headings); this directive restores it per-invocation
via :func:`apply_xml_tags_directive`."""


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class SystemPromptError(Exception):
    """Base class for system-prompt operations."""


class SystemPromptMissingError(SystemPromptError):
    """Resolved ``system_prompt_path`` is missing or unreadable (P1-SP-05).

    Carries :data:`MISSING_MESSAGE_TEMPLATE` interpolated with the path.
    """


class SystemPromptChecksumError(SystemPromptError):
    """The downloaded prompt's SHA-256 doesn't match the sidecar (P1-ERR-14).

    Carries :data:`CHECKSUM_MISMATCH_MESSAGE` verbatim. The on-disk file
    is untouched when this is raised.
    """


class SystemPromptDownloadError(SystemPromptError):
    """Network or HTTP failure during ``--update-system-prompt`` (P1-ERR-15).

    Carries :data:`DOWNLOAD_FAILED_MESSAGE_TEMPLATE` interpolated with
    the URL that failed and a short reason. The on-disk file is
    untouched when this is raised.
    """


# ---------------------------------------------------------------------------
# Injectable transport
# ---------------------------------------------------------------------------


Fetcher = Callable[[str], bytes]
"""Signature: ``(url) -> bytes``. Must raise :class:`SystemPromptDownloadError`
on any failure so the caller never sees a raw :mod:`urllib` exception."""


def _default_fetcher(url: str) -> bytes:
    """GET ``url`` and return the response body, translating errors.

    Catches every :mod:`urllib` exception (and :class:`OSError`) and
    re-raises as :class:`SystemPromptDownloadError` with the canonical
    template, so the update-flow caller has a single exception type to
    surface.
    """
    try:
        with urllib.request.urlopen(url) as resp:  # noqa: S310 — user-configured URL
            return resp.read()
    except urllib.error.HTTPError as e:
        reason = f"HTTP {e.code}"
        raise SystemPromptDownloadError(
            DOWNLOAD_FAILED_MESSAGE_TEMPLATE.format(url=url, reason=reason)
        ) from e
    except urllib.error.URLError as e:
        raise SystemPromptDownloadError(
            DOWNLOAD_FAILED_MESSAGE_TEMPLATE.format(url=url, reason=str(e.reason))
        ) from e
    except OSError as e:
        raise SystemPromptDownloadError(
            DOWNLOAD_FAILED_MESSAGE_TEMPLATE.format(url=url, reason=str(e))
        ) from e


# ---------------------------------------------------------------------------
# Seed (P1-SP-01, P1-SP-02)
# ---------------------------------------------------------------------------


def seed_system_prompt(
    target_path: str | Path,
    *,
    bundled_path: str | Path = BUNDLED_SYSTEM_PROMPT_PATH,
) -> bool:
    """Copy the bundled default to ``target_path`` if it doesn't exist yet.

    Returns ``True`` if a seed write happened, ``False`` if the target
    already existed (the P1-SP-02 no-overwrite guarantee). The target
    directory is created with ``parents=True, exist_ok=True``.

    The seed write uses ``os.open`` with ``O_CREAT|O_EXCL|O_WRONLY`` so
    the "does it exist?" check is atomic with the create at the syscall
    level — the pre-fix ``Path.exists()`` + ``shutil.copyfile`` had a
    TOCTOU window where a racing process could create the file between
    the check and the copy, silently clobbering user content.

    Mode is pinned to ``0o600`` so the seeded user-state file matches
    the tempfile-side contract enforced in
    :func:`core.cli_backend._write_system_prompt_tempfile` (NFR-08 /
    P1-PLAT-08). LF line endings are preserved verbatim because we
    write the bundled bytes through a binary-mode fd, never invoking
    text-mode translation.

    Durability: ``f.flush()`` + ``os.fsync(file_fd)`` + best-effort
    parent-dir fsync, matching the recipe in
    :func:`core._io.atomic_write_bytes` (NFR-04). The fd-leak guard
    mirrors the same module's ``os.fdopen`` protection.
    """
    target = Path(target_path)
    payload = Path(bundled_path).read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            str(target),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return False
    try:
        f = os.fdopen(fd, "wb")
    except BaseException:  # signal-safe: close raw fd even on KeyboardInterrupt
        os.close(fd)
        raise
    try:
        with f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir_best_effort(target.parent)
    except BaseException:  # signal-safe: don't leave a partial seed behind
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return True


# ---------------------------------------------------------------------------
# Resolve (P1-SP-04)
# ---------------------------------------------------------------------------


def resolve_system_prompt_path(
    config: Config,
    *,
    cli_override: str | Path | None = None,
) -> Path:
    """Return the path to use for this invocation.

    - ``cli_override`` (``--system-prompt FILE``) wins when present. The
      path is expanded for ``~`` but is **not** persisted — the caller
      never writes this back to Config (P1-SP-04).
    - Otherwise, ``config.resolved_system_prompt_path()`` is returned
      (i.e. ``~`` expanded in ``Config.system_prompt_path``).

    Note: the file's existence is *not* checked here. That gate lives in
    :func:`read_system_prompt` so the loop can stage other startup work
    (e.g. backend resolution) before tripping the missing-file error.
    """
    if cli_override is not None:
        return Path(cli_override).expanduser()
    return config.resolved_system_prompt_path()


# ---------------------------------------------------------------------------
# Read (P1-SP-05 / P1-ERR-15 partner)
# ---------------------------------------------------------------------------


def read_system_prompt(path: str | Path) -> str:
    """Read the resolved system prompt as UTF-8 text.

    Raises :class:`SystemPromptMissingError` (with the canonical
    P1-SP-05 message) when the file is missing or unreadable.
    :class:`OSError` (e.g. permission denied) is translated; we never
    leak the raw exception out of this helper.
    """
    path_obj = Path(path)
    try:
        return path_obj.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as e:
        raise SystemPromptMissingError(
            MISSING_MESSAGE_TEMPLATE.format(path=path_obj)
        ) from e


# ---------------------------------------------------------------------------
# XML-style-tags directive (--xml-tags opt-in)
# ---------------------------------------------------------------------------


def apply_xml_tags_directive(system: str, *, enabled: bool) -> str:
    """Return ``system`` with the XML-style-tags directive appended when enabled.

    The bundled prompt tells the model to structure multi-part rewrites with
    plain section headings only. ``--xml-tags`` opts back into the XML-style
    tag option for a single invocation by appending
    :data:`XML_TAGS_DIRECTIVE`. When ``enabled`` is ``False`` the prompt is
    returned verbatim, so the default path is byte-for-byte the on-disk file.
    """
    if not enabled:
        return system
    return f"{system.rstrip()}\n\n{XML_TAGS_DIRECTIVE}\n"


# ---------------------------------------------------------------------------
# Update (P1-SP-03 / D-3 / NFR-06 / P1-ERR-14 / P1-ERR-15)
# ---------------------------------------------------------------------------


def _parse_sidecar(raw: bytes) -> str:
    """Return the hex digest from a SHA-256 sidecar body.

    Accepts both formats:
      - ``<hex>\\n`` — bare digest (what ``shasum -a 256 < file`` emits
        without a filename).
      - ``<hex>  <filename>\\n`` — sha256sum coreutils format.

    The first whitespace-separated token is taken, lowercased, and
    validated as a 64-char hex string. Anything else raises
    :class:`SystemPromptChecksumError` — a malformed sidecar can't
    authorize an overwrite.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    first = text.split(None, 1)[0] if text else ""
    digest = first.lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise SystemPromptChecksumError(CHECKSUM_MISMATCH_MESSAGE)
    return digest


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Delegate to :func:`core._io.atomic_write_bytes` with module-specific
    tempfile prefix/suffix. Kept as a thin wrapper because tests import
    this symbol directly to smoke-test the helper from inside this
    module's namespace.
    """
    atomic_write_bytes(
        target, payload, prefix=".system-prompt-", suffix=".md"
    )


def update_system_prompt(
    url: str,
    target_path: str | Path,
    *,
    fetcher: Fetcher | None = None,
) -> Path | None:
    """Implement ``--update-system-prompt`` (P1-SP-03 / D-3).

    Steps:

    1. GET ``url`` → ``payload`` (bytes).
    2. GET ``url + ".sha256"`` → sidecar; parse expected hex digest.
    3. Compute SHA-256 of ``payload``; abort with
       :class:`SystemPromptChecksumError` on mismatch (P1-ERR-14).
       The on-disk file is **not** touched.
    4. If the target already exists, copy it to ``<target>.bak`` and
       remember that path so the caller can print it (P1-SP-03 / risk
       table — "users edit … and lose their edits").
    5. Atomically replace the target with ``payload``
       (``tempfile.mkstemp`` + ``os.replace``).

    Returns the backup path when a backup was made, ``None`` when the
    target didn't exist beforehand. Raises
    :class:`SystemPromptDownloadError` if either GET fails;
    :class:`SystemPromptChecksumError` if the digest doesn't match.

    The ``fetcher`` kwarg is the seam tests use to avoid the network.
    """
    fetch: Fetcher = fetcher if fetcher is not None else _default_fetcher
    target = Path(target_path)

    payload = fetch(url)
    sidecar_url = url + SHA256_SIDECAR_SUFFIX
    sidecar_raw = fetch(sidecar_url)

    expected = _parse_sidecar(sidecar_raw)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise SystemPromptChecksumError(CHECKSUM_MISMATCH_MESSAGE)

    backup_path: Path | None = None
    if target.exists():
        backup_path = target.parent / (target.name + BACKUP_SUFFIX)
        shutil.copyfile(str(target), str(backup_path))

    _atomic_write_bytes(target, payload)
    return backup_path
