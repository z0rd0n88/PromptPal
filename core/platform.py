"""Platform detection and clipboard utilities (SPEC §7, P1-PLAT-01..09).

This module is the single source of truth for "what kind of machine am I on?"
in PromptPal. It is read at startup and the resulting :class:`Platform`
snapshot is threaded through the pipeline so backend, history, and CLI
layers never re-probe the environment.

Public surface
--------------

- :func:`detect_platform` — returns a :class:`Platform` snapshot.
- :func:`assert_wsl_home_safe` — exits 1 with the SPEC §7 fix message
  when ``$HOME`` points at NTFS on WSL (P1-PLAT-03 / P1-ERR-12). The bash
  entrypoint (US-012) duplicates this guard so it fires before Python.
- :func:`copy_to_clipboard` — best-effort clipboard write. Returns
  ``True`` on success, ``False`` otherwise; never raises. A one-line
  stderr warning is printed when no provider is available
  (P1-PLAT-07 / P1-ERR-13).
- Constants :data:`WSL_LAUNCH_FIX_MESSAGE` and
  :data:`NO_CLIPBOARD_WARNING` carry the canonical strings the CLI
  surfaces verbatim (matched against in tests, so accidental drift is
  caught).

WSL detection (P1-PLAT-02)
--------------------------

We read ``/proc/sys/kernel/osrelease`` (case-insensitively):

- substring ``microsoft``  → ``is_wsl=True``
- substring ``wsl2``       → ``wsl_version=2``
- ``microsoft`` without ``wsl2`` → ``wsl_version=1``
- file missing / unreadable → ``is_wsl=False, wsl_version=None``

HOME resolution (P1-PLAT-04)
----------------------------

WSL occasionally hands a child process ``HOME=/mnt/c/Users/<name>`` when
launched from a Windows-side shortcut. NTFS-mounted HOME breaks atomic
``os.replace`` and clobbers our LF line endings, so we cross-check
against the passwd entry for the current UID and let passwd win.

Clipboard priority (P1-PLAT-05)
-------------------------------

``xclip → xsel → pbcopy → clip.exe (WSL only) → none``. On WSL with
``clip.exe`` selected, text is sent as raw UTF-8 *without* a BOM
(P1-PLAT-06 / D-9). ``str.encode("utf-8")`` already omits the BOM —
``"utf-8-sig"`` would prepend one and must never be used here.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass


WSL_LAUNCH_FIX_MESSAGE: str = (
    "Warning: HOME appears to be a Windows path.\n"
    "For best results, launch from WSL:\n"
    "  wsl -d Ubuntu -- promptpal"
)

NO_CLIPBOARD_WARNING: str = (
    "Warning: no clipboard provider found. Install xclip or xsel."
)

OSRELEASE_PATH: str = "/proc/sys/kernel/osrelease"

_NTFS_HOME_PREFIXES: tuple[str, ...] = ("/mnt/c/", "/c/")

_CLIPBOARD_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("xclip", "-selection", "clipboard"),
    ("xsel", "--clipboard", "--input"),
    ("pbcopy",),
)

_WSL_CLIPBOARD: tuple[str, ...] = ("clip.exe",)


@dataclass(frozen=True)
class Platform:
    """Immutable snapshot of the runtime platform (SPEC §7)."""

    is_wsl: bool
    wsl_version: int | None
    home: str
    clipboard_cmd: tuple[str, ...]


def detect_platform() -> Platform:
    """Return a :class:`Platform` describing the current runtime."""
    is_wsl, wsl_version = _detect_wsl()
    home = _resolve_home(is_wsl)
    clipboard_cmd = _detect_clipboard(is_wsl)
    return Platform(
        is_wsl=is_wsl,
        wsl_version=wsl_version,
        home=home,
        clipboard_cmd=clipboard_cmd,
    )


def _detect_wsl(osrelease_path: str = OSRELEASE_PATH) -> tuple[bool, int | None]:
    """Probe ``/proc/sys/kernel/osrelease`` for WSL markers (P1-PLAT-02).

    ``osrelease_path`` is parameterized for tests; production callers pass
    nothing and read the real file.
    """
    try:
        with open(osrelease_path, encoding="utf-8") as f:
            osrelease = f.read().lower()
    except (FileNotFoundError, OSError):
        return False, None
    if "microsoft" not in osrelease:
        return False, None
    return True, 2 if "wsl2" in osrelease else 1


def _has_ntfs_home_prefix(home: str) -> bool:
    return any(home.startswith(p) for p in _NTFS_HOME_PREFIXES)


def _resolve_home(
    is_wsl: bool, env: Mapping[str, str] | None = None
) -> str:
    """Return a WSL-safe HOME (P1-PLAT-04).

    When ``is_wsl`` and the env HOME starts with ``/mnt/c/`` or ``/c/``,
    fall back to ``pwd.getpwuid(os.getuid()).pw_dir`` — the passwd entry
    is set correctly by the WSL init shell even when the inherited HOME
    is wrong. Non-WSL platforms and valid WSL HOMEs pass through.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    home = env_map.get("HOME", "")
    if is_wsl and _has_ntfs_home_prefix(home):
        try:
            return pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            return home
    return home


def _detect_clipboard(is_wsl: bool) -> tuple[str, ...]:
    """Return the first available clipboard command, or ``()`` (P1-PLAT-05).

    Probes ``shutil.which`` in priority order:
    ``xclip → xsel → pbcopy → clip.exe (WSL only) → none``.
    """
    for cmd in _CLIPBOARD_CANDIDATES:
        if shutil.which(cmd[0]) is not None:
            return cmd
    if is_wsl and shutil.which(_WSL_CLIPBOARD[0]) is not None:
        return _WSL_CLIPBOARD
    return ()


def assert_wsl_home_safe(platform: Platform) -> None:
    """Exit 1 with the SPEC §7 fix message when WSL + HOME is NTFS-mounted.

    Per P1-PLAT-03 / P1-ERR-12. The bash entrypoint duplicates this guard
    so it also triggers before Python starts. The raw ``$HOME`` (not the
    resolved one) is what we check — the resolved value may already have
    been corrected by ``_resolve_home``.
    """
    raw_home = os.environ.get("HOME", "")
    if platform.is_wsl and _has_ntfs_home_prefix(raw_home):
        print(WSL_LAUNCH_FIX_MESSAGE, file=sys.stderr)
        sys.exit(1)


def copy_to_clipboard(text: str, platform: Platform) -> bool:
    """Pipe ``text`` to the platform clipboard (P1-PLAT-06 / P1-PLAT-07).

    Returns ``True`` on success, ``False`` when no provider is available
    or when the provider call fails. Never raises — per P1-PLAT-07 the
    run must continue and exit 0 even if the clipboard write didn't
    happen.

    Encoding: ``str.encode("utf-8")`` produces UTF-8 *without* a BOM,
    which ``clip.exe`` requires for round-trip-clean non-ASCII (D-9).
    """
    if not platform.clipboard_cmd:
        print(NO_CLIPBOARD_WARNING, file=sys.stderr)
        return False
    try:
        proc = subprocess.run(
            list(platform.clipboard_cmd),
            input=text.encode("utf-8"),
            capture_output=True,
            check=False,
        )
    except OSError as e:
        print(f"Warning: clipboard write failed: {e}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        stderr_msg = proc.stderr.decode("utf-8", errors="replace").strip()
        print(
            f"Warning: clipboard provider {platform.clipboard_cmd[0]!r} "
            f"exited {proc.returncode}: {stderr_msg or 'unknown error'}",
            file=sys.stderr,
        )
        return False
    return True
