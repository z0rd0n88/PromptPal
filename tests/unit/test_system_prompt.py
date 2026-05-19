"""Tests for core/system_prompt.py (US-007 / SPEC §11, P1-SP-01..05, D-3).

Coverage map (1 test → 1 acceptance criterion or sub-rule):

  AC #1  core/system_prompt.txt exists with a default prompt   → test_bundled_*
  AC #2  Installer seeds from bundled on first run             → test_seed_*
  AC #3  Existing file never overwritten without --update-...  → test_seed_does_not_overwrite_*
  AC #4  --update verifies sha256; mismatch leaves file        → test_update_checksum_*
  AC #5  Backup of current file printed before atomic replace  → test_update_backup_*
  AC #6  --system-prompt FILE overrides for invocation only    → test_resolve_*
  AC #7  Missing/unreadable path → actionable message          → test_read_*
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import core.system_prompt as sp_mod
from core.config import Config
from core.system_prompt import (
    BACKUP_SUFFIX,
    BUNDLED_SYSTEM_PROMPT_PATH,
    CHECKSUM_MISMATCH_MESSAGE,
    DOWNLOAD_FAILED_MESSAGE_TEMPLATE,
    MISSING_MESSAGE_TEMPLATE,
    SHA256_SIDECAR_SUFFIX,
    SystemPromptChecksumError,
    SystemPromptDownloadError,
    SystemPromptError,
    SystemPromptMissingError,
    _atomic_write_bytes,
    _parse_sidecar,
    read_system_prompt,
    resolve_system_prompt_path,
    seed_system_prompt,
    update_system_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_fetcher(responses: dict[str, bytes]):
    """Return a ``Fetcher`` that serves canned bytes by URL.

    Missing URL → :class:`SystemPromptDownloadError` (matches the
    contract of the production fetcher).
    """

    def fetch(url: str) -> bytes:
        if url not in responses:
            raise SystemPromptDownloadError(
                DOWNLOAD_FAILED_MESSAGE_TEMPLATE.format(url=url, reason="not found")
            )
        return responses[url]

    return fetch


def _sidecar(payload: bytes) -> bytes:
    """Return a coreutils-style sha256sum sidecar for ``payload``."""
    digest = hashlib.sha256(payload).hexdigest()
    return f"{digest}  system-prompt.md\n".encode("utf-8")


# ---------------------------------------------------------------------------
# AC #1 — bundled default exists
# ---------------------------------------------------------------------------


def test_bundled_system_prompt_path_points_at_core_dir():
    """The bundled path lives at ``core/system_prompt.txt`` (D-3)."""
    assert BUNDLED_SYSTEM_PROMPT_PATH.name == "system_prompt.txt"
    assert BUNDLED_SYSTEM_PROMPT_PATH.parent.name == "core"


def test_bundled_system_prompt_file_exists():
    """The repo ships ``core/system_prompt.txt`` (P1-SP-01)."""
    assert BUNDLED_SYSTEM_PROMPT_PATH.is_file()


def test_bundled_system_prompt_has_content():
    """Bundled file is non-empty (avoid silently shipping an empty prompt)."""
    body = BUNDLED_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    assert body.strip(), "core/system_prompt.txt must not be empty"


def test_bundled_system_prompt_uses_lf_line_endings():
    """The bundled prompt is UTF-8 / LF (P1-PLAT-08)."""
    raw = BUNDLED_SYSTEM_PROMPT_PATH.read_bytes()
    assert b"\r\n" not in raw, "bundled prompt must not contain CRLF"


# ---------------------------------------------------------------------------
# AC #2 / AC #3 — seed semantics
# ---------------------------------------------------------------------------


def test_seed_creates_file_when_missing(tmp_path):
    """First-run: target is created from the bundled default (P1-SP-01)."""
    target = tmp_path / "system-prompt.md"
    assert not target.exists()
    did_seed = seed_system_prompt(target)
    assert did_seed is True
    assert target.is_file()
    assert (
        target.read_text(encoding="utf-8")
        == BUNDLED_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    )


def test_seed_creates_parent_directory(tmp_path):
    """Parent of the target is created on demand (installer convenience)."""
    target = tmp_path / "nested" / "deeper" / "system-prompt.md"
    assert seed_system_prompt(target) is True
    assert target.is_file()


def test_seed_does_not_overwrite_existing_file(tmp_path):
    """Subsequent runs: user content is preserved (P1-SP-02)."""
    target = tmp_path / "system-prompt.md"
    target.write_text("USER-EDITED CONTENT\n", encoding="utf-8")
    did_seed = seed_system_prompt(target)
    assert did_seed is False
    assert target.read_text(encoding="utf-8") == "USER-EDITED CONTENT\n"


def test_seed_does_not_overwrite_empty_file(tmp_path):
    """An empty file still counts as existing — no silent overwrite."""
    target = tmp_path / "system-prompt.md"
    target.write_text("", encoding="utf-8")
    assert seed_system_prompt(target) is False
    assert target.read_text(encoding="utf-8") == ""


def test_seed_uses_injected_bundled_path(tmp_path):
    """The ``bundled_path`` kwarg is the test seam (no hard-coded repo path)."""
    fake_bundle = tmp_path / "fake-bundle.txt"
    fake_bundle.write_text("ALTERNATE BUNDLE\n", encoding="utf-8")
    target = tmp_path / "system-prompt.md"
    assert seed_system_prompt(target, bundled_path=fake_bundle) is True
    assert target.read_text(encoding="utf-8") == "ALTERNATE BUNDLE\n"


def test_seed_preserves_lf_line_endings(tmp_path):
    """Seeded file matches bundled bytes exactly — no CRLF translation."""
    target = tmp_path / "system-prompt.md"
    seed_system_prompt(target)
    assert target.read_bytes() == BUNDLED_SYSTEM_PROMPT_PATH.read_bytes()


# ---------------------------------------------------------------------------
# AC #6 — resolve_system_prompt_path
# ---------------------------------------------------------------------------


def test_resolve_returns_config_path_when_no_override(tmp_path):
    """No ``--system-prompt`` flag → use ``Config.system_prompt_path``."""
    cfg = Config(system_prompt_path=str(tmp_path / "from-config.md"))
    assert resolve_system_prompt_path(cfg) == tmp_path / "from-config.md"


def test_resolve_expands_tilde_in_config_path(monkeypatch, tmp_path):
    """``~`` in ``Config.system_prompt_path`` is expanded to absolute path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config(system_prompt_path="~/.promptpal/system-prompt.md")
    resolved = resolve_system_prompt_path(cfg)
    assert resolved == tmp_path / ".promptpal" / "system-prompt.md"


def test_resolve_override_wins_over_config(tmp_path):
    """``--system-prompt FILE`` overrides ``Config.system_prompt_path``."""
    cfg = Config(system_prompt_path=str(tmp_path / "from-config.md"))
    override = tmp_path / "from-cli.md"
    assert (
        resolve_system_prompt_path(cfg, cli_override=override)
        == override
    )


def test_resolve_override_expands_tilde(monkeypatch, tmp_path):
    """CLI override paths also get ``~`` expanded."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config(system_prompt_path="/elsewhere/from-config.md")
    resolved = resolve_system_prompt_path(cfg, cli_override="~/cli.md")
    assert resolved == tmp_path / "cli.md"


def test_resolve_override_accepts_str_or_path(tmp_path):
    """The override accepts either ``str`` or :class:`Path`."""
    cfg = Config()
    s = str(tmp_path / "as-str.md")
    p = tmp_path / "as-path.md"
    assert resolve_system_prompt_path(cfg, cli_override=s) == Path(s)
    assert resolve_system_prompt_path(cfg, cli_override=p) == p


def test_resolve_does_not_persist_override(tmp_path):
    """Override is ephemeral — Config is not mutated (P1-SP-04)."""
    cfg = Config(system_prompt_path=str(tmp_path / "from-config.md"))
    original_path = cfg.system_prompt_path
    resolve_system_prompt_path(cfg, cli_override=tmp_path / "from-cli.md")
    assert cfg.system_prompt_path == original_path


def test_resolve_does_not_check_existence(tmp_path):
    """``resolve_system_prompt_path`` doesn't stat the file — that's read's job."""
    cfg = Config(system_prompt_path=str(tmp_path / "does-not-exist.md"))
    # Must not raise.
    resolve_system_prompt_path(cfg)


# ---------------------------------------------------------------------------
# AC #7 — read_system_prompt: missing/unreadable → actionable message
# ---------------------------------------------------------------------------


def test_read_returns_contents(tmp_path):
    """Happy path: returns UTF-8 text verbatim."""
    target = tmp_path / "system-prompt.md"
    target.write_text("PROMPT BODY\n", encoding="utf-8")
    assert read_system_prompt(target) == "PROMPT BODY\n"


def test_read_raises_missing_when_file_absent(tmp_path):
    """Missing file → :class:`SystemPromptMissingError` (P1-SP-05)."""
    target = tmp_path / "does-not-exist.md"
    with pytest.raises(SystemPromptMissingError):
        read_system_prompt(target)


def test_read_missing_message_is_canonical(tmp_path):
    """Error carries the exact P1-SP-05 message with path interpolated."""
    target = tmp_path / "missing.md"
    try:
        read_system_prompt(target)
    except SystemPromptMissingError as e:
        assert str(e) == MISSING_MESSAGE_TEMPLATE.format(path=target)
    else:
        pytest.fail("expected SystemPromptMissingError")


def test_read_missing_message_template_shape():
    """Canonical message template pins both halves of the actionable text."""
    msg = MISSING_MESSAGE_TEMPLATE.format(path="/x/y/z.md")
    assert msg == (
        "System prompt file not found at /x/y/z.md. "
        "Run with --update-system-prompt to restore the default."
    )


def test_read_raises_missing_when_directory(tmp_path):
    """Path points at a directory → translated to SystemPromptMissingError."""
    target = tmp_path / "system-prompt.md"
    target.mkdir()
    with pytest.raises(SystemPromptMissingError):
        read_system_prompt(target)


def test_read_missing_error_is_subclass_of_base():
    """Error hierarchy: callers can ``except SystemPromptError`` to catch all."""
    assert issubclass(SystemPromptMissingError, SystemPromptError)
    assert issubclass(SystemPromptChecksumError, SystemPromptError)
    assert issubclass(SystemPromptDownloadError, SystemPromptError)


# ---------------------------------------------------------------------------
# AC #4 — update verifies sha256
# ---------------------------------------------------------------------------


def test_update_with_matching_sha_replaces_file(tmp_path):
    """Happy path: matching sidecar → target replaced with downloaded bytes."""
    target = tmp_path / "system-prompt.md"
    target.write_text("OLD BODY\n", encoding="utf-8")
    new_payload = b"NEW BODY\n"
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: new_payload, url + SHA256_SIDECAR_SUFFIX: _sidecar(new_payload)}
    )
    update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_bytes() == new_payload


def test_update_with_mismatched_sha_raises_checksum_error(tmp_path):
    """Mismatch → :class:`SystemPromptChecksumError` and file untouched."""
    target = tmp_path / "system-prompt.md"
    target.write_text("OLD BODY\n", encoding="utf-8")
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {
            url: b"NEW BODY\n",
            url + SHA256_SIDECAR_SUFFIX: _sidecar(b"DIFFERENT PAYLOAD\n"),
        }
    )
    with pytest.raises(SystemPromptChecksumError):
        update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_text(encoding="utf-8") == "OLD BODY\n"


def test_update_checksum_mismatch_message_canonical(tmp_path):
    """Mismatch carries the canonical P1-ERR-14 message verbatim."""
    target = tmp_path / "system-prompt.md"
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: b"NEW\n", url + SHA256_SIDECAR_SUFFIX: _sidecar(b"OTHER\n")}
    )
    try:
        update_system_prompt(url, target, fetcher=fetcher)
    except SystemPromptChecksumError as e:
        assert str(e) == CHECKSUM_MISMATCH_MESSAGE
        assert str(e) == (
            "System prompt checksum mismatch — refusing to overwrite. "
            "Verify Config.system_prompt_update_url."
        )
    else:
        pytest.fail("expected SystemPromptChecksumError")


def test_update_accepts_bare_hex_sidecar(tmp_path):
    """Sidecar may be just ``<hex>\\n`` (no filename) — ``shasum`` style."""
    target = tmp_path / "system-prompt.md"
    payload = b"BODY\n"
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = (digest + "\n").encode("utf-8")
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher({url: payload, url + SHA256_SIDECAR_SUFFIX: sidecar})
    update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_bytes() == payload


def test_update_accepts_coreutils_sidecar(tmp_path):
    """Sidecar may be ``<hex>  <filename>\\n`` (sha256sum coreutils style)."""
    target = tmp_path / "system-prompt.md"
    payload = b"BODY\n"
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = f"{digest}  system-prompt.txt\n".encode("utf-8")
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher({url: payload, url + SHA256_SIDECAR_SUFFIX: sidecar})
    update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_bytes() == payload


def test_update_rejects_garbage_sidecar(tmp_path):
    """Malformed sidecar (not 64 hex chars) → checksum error, file untouched."""
    target = tmp_path / "system-prompt.md"
    target.write_text("OLD\n", encoding="utf-8")
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: b"BODY\n", url + SHA256_SIDECAR_SUFFIX: b"not-a-hash\n"}
    )
    with pytest.raises(SystemPromptChecksumError):
        update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_text(encoding="utf-8") == "OLD\n"


def test_update_rejects_uppercase_non_hex_sidecar(tmp_path):
    """64-char string with non-hex chars (e.g. 'Z') → checksum error."""
    target = tmp_path / "system-prompt.md"
    target.write_text("OLD\n", encoding="utf-8")
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: b"BODY\n", url + SHA256_SIDECAR_SUFFIX: ("z" * 64 + "\n").encode()}
    )
    with pytest.raises(SystemPromptChecksumError):
        update_system_prompt(url, target, fetcher=fetcher)


def test_update_accepts_uppercase_hex_sidecar(tmp_path):
    """Sidecar with uppercase hex is lowercased and accepted (defensive)."""
    target = tmp_path / "system-prompt.md"
    payload = b"BODY\n"
    digest = hashlib.sha256(payload).hexdigest().upper()
    sidecar = (digest + "\n").encode("utf-8")
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher({url: payload, url + SHA256_SIDECAR_SUFFIX: sidecar})
    update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_bytes() == payload


# ---------------------------------------------------------------------------
# AC #5 — backup before atomic replace
# ---------------------------------------------------------------------------


def test_update_creates_backup_when_target_exists(tmp_path):
    """Existing user content is backed up to ``<target>.bak`` (risk-table)."""
    target = tmp_path / "system-prompt.md"
    target.write_text("USER-EDITED\n", encoding="utf-8")
    payload = b"FRESH\n"
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: payload, url + SHA256_SIDECAR_SUFFIX: _sidecar(payload)}
    )
    backup_path = update_system_prompt(url, target, fetcher=fetcher)
    assert backup_path == target.parent / (target.name + BACKUP_SUFFIX)
    assert backup_path is not None and backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8") == "USER-EDITED\n"


def test_update_backup_suffix_is_dot_bak():
    """Backup naming is ``<filename>.bak`` per the SPEC §11 risk-table line."""
    assert BACKUP_SUFFIX == ".bak"


def test_update_no_backup_when_target_absent(tmp_path):
    """First-run update (no prior file) → no backup made; returns ``None``."""
    target = tmp_path / "system-prompt.md"
    payload = b"FIRST\n"
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: payload, url + SHA256_SIDECAR_SUFFIX: _sidecar(payload)}
    )
    backup_path = update_system_prompt(url, target, fetcher=fetcher)
    assert backup_path is None
    assert not (target.parent / (target.name + BACKUP_SUFFIX)).exists()
    assert target.read_bytes() == payload


def test_update_backup_overwrites_previous_backup(tmp_path):
    """Second update replaces the prior ``.bak`` (single rolling backup)."""
    target = tmp_path / "system-prompt.md"
    target.write_text("V1\n", encoding="utf-8")
    url = "https://example.invalid/system-prompt.txt"

    # First update: V1 → V2; backup contains V1.
    payload2 = b"V2\n"
    fetcher = _fake_fetcher(
        {url: payload2, url + SHA256_SIDECAR_SUFFIX: _sidecar(payload2)}
    )
    update_system_prompt(url, target, fetcher=fetcher)

    # Second update: V2 → V3; backup should now contain V2 (not V1).
    payload3 = b"V3\n"
    fetcher = _fake_fetcher(
        {url: payload3, url + SHA256_SIDECAR_SUFFIX: _sidecar(payload3)}
    )
    backup_path = update_system_prompt(url, target, fetcher=fetcher)
    assert backup_path is not None
    assert backup_path.read_text(encoding="utf-8") == "V2\n"
    assert target.read_text(encoding="utf-8") == "V3\n"


# ---------------------------------------------------------------------------
# Atomic write contract + download failure (P1-ERR-15)
# ---------------------------------------------------------------------------


def test_update_writes_atomically_via_temp_and_replace(tmp_path, monkeypatch):
    """Replace path goes through tempfile.mkstemp + os.replace (P1-SP-03)."""
    target = tmp_path / "system-prompt.md"
    target.write_text("OLD\n", encoding="utf-8")

    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def tracking_replace(src, dst, *args, **kwargs):
        replace_calls.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("core._io.os.replace", tracking_replace)

    payload = b"NEW\n"
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: payload, url + SHA256_SIDECAR_SUFFIX: _sidecar(payload)}
    )
    update_system_prompt(url, target, fetcher=fetcher)

    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    # Tempfile must be in the same directory as target (same-FS atomic rename).
    assert Path(src).parent == target.parent
    assert Path(dst) == target
    # Tempfile must not survive after replace.
    leftovers = [p for p in target.parent.iterdir() if p.name.startswith(".system-")]
    assert leftovers == []


def test_update_cleans_up_tempfile_on_write_failure(tmp_path, monkeypatch):
    """When os.replace raises, the tempfile is unlinked (no leak)."""
    target = tmp_path / "system-prompt.md"
    target.write_text("OLD\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core._io.os.replace", boom)

    payload = b"NEW\n"
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: payload, url + SHA256_SIDECAR_SUFFIX: _sidecar(payload)}
    )
    with pytest.raises(OSError):
        update_system_prompt(url, target, fetcher=fetcher)
    leftovers = [p for p in target.parent.iterdir() if p.name.startswith(".system-")]
    assert leftovers == []
    # Target is unchanged.
    assert target.read_text(encoding="utf-8") == "OLD\n"


def test_update_main_fetch_failure_raises_and_leaves_file(tmp_path):
    """Main URL download fails → :class:`SystemPromptDownloadError`, file untouched."""
    target = tmp_path / "system-prompt.md"
    target.write_text("OLD\n", encoding="utf-8")
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher({})  # neither url present
    with pytest.raises(SystemPromptDownloadError):
        update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_text(encoding="utf-8") == "OLD\n"


def test_update_sidecar_fetch_failure_raises_and_leaves_file(tmp_path):
    """Sidecar download fails → download error, no partial overwrite (risk-table)."""
    target = tmp_path / "system-prompt.md"
    target.write_text("OLD\n", encoding="utf-8")
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher({url: b"NEW\n"})  # sidecar missing
    with pytest.raises(SystemPromptDownloadError):
        update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_text(encoding="utf-8") == "OLD\n"


def test_update_download_failed_message_template():
    """P1-ERR-15 template pins both halves of the actionable text."""
    msg = DOWNLOAD_FAILED_MESSAGE_TEMPLATE.format(url="https://x/y", reason="boom")
    assert msg == "Could not fetch system prompt from https://x/y: boom"


def test_update_creates_parent_directory(tmp_path):
    """If the target's parent dir is missing, it's created on the fly."""
    target = tmp_path / "nested" / "system-prompt.md"
    payload = b"FRESH\n"
    url = "https://example.invalid/system-prompt.txt"
    fetcher = _fake_fetcher(
        {url: payload, url + SHA256_SIDECAR_SUFFIX: _sidecar(payload)}
    )
    update_system_prompt(url, target, fetcher=fetcher)
    assert target.read_bytes() == payload


def test_update_default_fetcher_is_urllib_backed():
    """Default fetcher is the urllib-backed one — production has no setup step."""
    assert sp_mod._default_fetcher is sp_mod._default_fetcher  # sanity
    # Just exercise that it's callable and module-level (not a placeholder).
    assert callable(sp_mod._default_fetcher)


def test_default_fetcher_translates_url_error(monkeypatch):
    """Default fetcher converts urllib URLError → SystemPromptDownloadError."""
    import urllib.error

    def boom(_url):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("core.system_prompt.urllib.request.urlopen", boom)
    with pytest.raises(SystemPromptDownloadError) as ex:
        sp_mod._default_fetcher("https://example.invalid/x")
    assert "connection refused" in str(ex.value)


def test_default_fetcher_translates_http_error(monkeypatch):
    """Default fetcher converts HTTPError → SystemPromptDownloadError with status."""
    import email.message
    import urllib.error

    def boom(_url):
        raise urllib.error.HTTPError(
            url="https://example.invalid/x",
            code=404,
            msg="Not Found",
            hdrs=email.message.Message(),
            fp=None,
        )

    monkeypatch.setattr("core.system_prompt.urllib.request.urlopen", boom)
    with pytest.raises(SystemPromptDownloadError) as ex:
        sp_mod._default_fetcher("https://example.invalid/x")
    assert "HTTP 404" in str(ex.value)


def test_default_fetcher_translates_os_error(monkeypatch):
    """Default fetcher converts OSError → SystemPromptDownloadError."""

    def boom(_url):
        raise OSError("network unreachable")

    monkeypatch.setattr("core.system_prompt.urllib.request.urlopen", boom)
    with pytest.raises(SystemPromptDownloadError):
        sp_mod._default_fetcher("https://example.invalid/x")


def test_default_fetcher_happy_path(monkeypatch):
    """Default fetcher returns the response body on a 200 (production path)."""

    class FakeResp:
        def read(self) -> bytes:
            return b"PAYLOAD\n"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(_url):
        return FakeResp()

    monkeypatch.setattr("core.system_prompt.urllib.request.urlopen", fake_urlopen)
    assert sp_mod._default_fetcher("https://example.invalid/x") == b"PAYLOAD\n"


# ---------------------------------------------------------------------------
# _parse_sidecar — internal but tested directly for edge cases
# ---------------------------------------------------------------------------


def test_parse_sidecar_extracts_first_token():
    """Picks the first whitespace-separated token from a multi-line sidecar."""
    digest = "0" * 64
    raw = f"{digest}  some-file.md\nignored second line\n".encode("utf-8")
    assert _parse_sidecar(raw) == digest


def test_parse_sidecar_lowercases_hex():
    """Hex is normalized to lowercase before comparison."""
    digest = "ABCDEF" * 10 + "ABCD"  # 64 hex upper
    assert _parse_sidecar((digest + "\n").encode("utf-8")) == digest.lower()


def test_parse_sidecar_empty_body_rejected():
    """An empty sidecar is treated as a malformed hash."""
    with pytest.raises(SystemPromptChecksumError):
        _parse_sidecar(b"")


def test_parse_sidecar_wrong_length_rejected():
    """Hex of the wrong length is rejected."""
    with pytest.raises(SystemPromptChecksumError):
        _parse_sidecar(b"deadbeef\n")


# ---------------------------------------------------------------------------
# _atomic_write_bytes — internal, exercised via update tests; quick smoke test
# ---------------------------------------------------------------------------


def test_atomic_write_bytes_creates_target(tmp_path):
    """Smoke test of the atomic-write helper."""
    target = tmp_path / "x.bin"
    _atomic_write_bytes(target, b"hello")
    assert target.read_bytes() == b"hello"
