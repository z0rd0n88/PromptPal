"""Tests for core/platform.py (US-003, SPEC §7, P1-PLAT-01..09).

Coverage map (1 test → 1 acceptance criterion or sub-rule):

  AC #1  WSL detection from /proc/sys/kernel/osrelease         → test_detect_wsl_*
  AC #2  HOME guard: /mnt/c or /c prefix → stderr + exit 1     → test_assert_wsl_home_safe_*
  AC #3  passwd cross-check wins when HOME is NTFS-mounted     → test_resolve_home_*
  AC #4  Clipboard priority xclip → xsel → pbcopy → clip.exe   → test_clipboard_*
  AC #5  clip.exe sends UTF-8 without a BOM                    → test_copy_to_clipboard_utf8_no_bom
  AC #6  No provider → one-line warning, no error              → test_copy_to_clipboard_no_provider*
  AC #7  Files PromptPal writes use LF line endings            → test_lf_line_endings_*
"""

from __future__ import annotations

import dataclasses
from typing import Iterable

import pytest

import core.platform as platform_mod
from core.platform import (
    NO_CLIPBOARD_WARNING,
    OSRELEASE_PATH,
    WSL_LAUNCH_FIX_MESSAGE,
    Platform,
    _detect_clipboard,
    _detect_wsl,
    _resolve_home,
    assert_wsl_home_safe,
    copy_to_clipboard,
    detect_platform,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _which_factory(available: Iterable[str]):
    """Return a ``shutil.which`` replacement that resolves only ``available``."""
    seen = set(available)

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in seen else None

    return which


def _platform(
    *,
    is_wsl: bool = False,
    wsl_version: int | None = None,
    home: str = "/home/alex",
    clipboard_cmd: tuple[str, ...] = (),
) -> Platform:
    return Platform(
        is_wsl=is_wsl,
        wsl_version=wsl_version,
        home=home,
        clipboard_cmd=clipboard_cmd,
    )


# ---------------------------------------------------------------------------
# Platform dataclass
# ---------------------------------------------------------------------------


def test_platform_dataclass_is_frozen():
    """Platform snapshot must be immutable — read once at startup, then frozen."""
    p = _platform()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.is_wsl = True  # type: ignore[misc]


def test_platform_fields_shape():
    """SPEC §7 fields: is_wsl, wsl_version, home, clipboard_cmd."""
    names = {f.name for f in dataclasses.fields(Platform)}
    assert names == {"is_wsl", "wsl_version", "home", "clipboard_cmd"}


# ---------------------------------------------------------------------------
# _detect_wsl — AC #1
# ---------------------------------------------------------------------------


def test_detect_wsl2_substring(tmp_path):
    """``wsl2`` substring → wsl_version=2."""
    f = tmp_path / "osrelease"
    f.write_text("5.15.146.1-microsoft-standard-WSL2\n", encoding="utf-8")
    assert _detect_wsl(str(f)) == (True, 2)


def test_detect_wsl_v1_microsoft_only(tmp_path):
    """``microsoft`` without ``wsl2`` → wsl_version=1."""
    f = tmp_path / "osrelease"
    f.write_text("4.4.0-19041-Microsoft\n", encoding="utf-8")
    assert _detect_wsl(str(f)) == (True, 1)


def test_detect_wsl_case_insensitive(tmp_path):
    """Detection is case-insensitive — uppercase ``MICROSOFT`` still matches."""
    f = tmp_path / "osrelease"
    f.write_text("FOO-MICROSOFT-BAR\n", encoding="utf-8")
    assert _detect_wsl(str(f)) == (True, 1)


def test_detect_not_wsl_when_no_microsoft(tmp_path):
    """Native Linux osrelease → is_wsl=False."""
    f = tmp_path / "osrelease"
    f.write_text("6.6.0-generic\n", encoding="utf-8")
    assert _detect_wsl(str(f)) == (False, None)


def test_detect_not_wsl_when_osrelease_missing(tmp_path):
    """Missing /proc/sys/kernel/osrelease (e.g. macOS) → is_wsl=False."""
    f = tmp_path / "does-not-exist"
    assert _detect_wsl(str(f)) == (False, None)


def test_detect_wsl_default_path_constant():
    """Default ``osrelease_path`` argument is the SPEC-mandated /proc path."""
    assert OSRELEASE_PATH == "/proc/sys/kernel/osrelease"


# ---------------------------------------------------------------------------
# _resolve_home — AC #3
# ---------------------------------------------------------------------------


def test_resolve_home_not_wsl_passes_env_through():
    """Off-WSL: HOME is trusted as-is (no passwd cross-check)."""
    assert (
        _resolve_home(is_wsl=False, env={"HOME": "/mnt/c/Users/sneak"})
        == "/mnt/c/Users/sneak"
    )


def test_resolve_home_wsl_valid_home_passes_through():
    """On WSL: a valid HOME under /home/ is trusted as-is."""
    assert _resolve_home(is_wsl=True, env={"HOME": "/home/alex"}) == "/home/alex"


def test_resolve_home_wsl_ntfs_mnt_c_falls_back_to_passwd(monkeypatch):
    """``HOME=/mnt/c/...`` on WSL → passwd entry wins."""

    class FakeEntry:
        pw_dir = "/home/alex"

    monkeypatch.setattr(platform_mod.pwd, "getpwuid", lambda _uid: FakeEntry())
    assert (
        _resolve_home(is_wsl=True, env={"HOME": "/mnt/c/Users/sneak"})
        == "/home/alex"
    )


def test_resolve_home_wsl_ntfs_c_prefix_falls_back_to_passwd(monkeypatch):
    """``HOME=/c/...`` (Cygwin-style) on WSL → passwd entry wins."""

    class FakeEntry:
        pw_dir = "/home/alex"

    monkeypatch.setattr(platform_mod.pwd, "getpwuid", lambda _uid: FakeEntry())
    assert (
        _resolve_home(is_wsl=True, env={"HOME": "/c/Users/sneak"}) == "/home/alex"
    )


def test_resolve_home_passwd_lookup_failure_returns_env_home(monkeypatch):
    """If passwd has no entry for our UID, return the env HOME (best effort)."""

    def boom(_uid):
        raise KeyError("no such uid")

    monkeypatch.setattr(platform_mod.pwd, "getpwuid", boom)
    assert (
        _resolve_home(is_wsl=True, env={"HOME": "/mnt/c/Users/sneak"})
        == "/mnt/c/Users/sneak"
    )


def test_resolve_home_reads_os_environ_when_env_arg_omitted(monkeypatch):
    """Default env source is os.environ — production callers don't pass env."""
    monkeypatch.setenv("HOME", "/home/alex")
    assert _resolve_home(is_wsl=True) == "/home/alex"


# ---------------------------------------------------------------------------
# assert_wsl_home_safe — AC #2
# ---------------------------------------------------------------------------


def test_assert_wsl_home_safe_no_op_when_not_wsl(monkeypatch):
    """Off-WSL: a Windows-style HOME is irrelevant (no exit)."""
    monkeypatch.setenv("HOME", "/mnt/c/Users/sneak")
    assert_wsl_home_safe(_platform(is_wsl=False))  # must not raise/exit


def test_assert_wsl_home_safe_no_op_when_home_is_under_home(monkeypatch):
    """On WSL with a valid HOME: no exit."""
    monkeypatch.setenv("HOME", "/home/alex")
    assert_wsl_home_safe(_platform(is_wsl=True, wsl_version=2))


def test_assert_wsl_home_safe_exits_on_mnt_c_home(monkeypatch, capsys):
    """WSL + HOME under /mnt/c → exit 1 with the SPEC §7 fix message."""
    monkeypatch.setenv("HOME", "/mnt/c/Users/sneak")
    with pytest.raises(SystemExit) as exc:
        assert_wsl_home_safe(_platform(is_wsl=True, wsl_version=2))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "HOME appears to be a Windows path" in err
    assert "wsl -d Ubuntu -- promptpal" in err


def test_assert_wsl_home_safe_exits_on_c_prefix_home(monkeypatch):
    """WSL + HOME=/c/... → exit 1 (Cygwin-style prefix is also rejected)."""
    monkeypatch.setenv("HOME", "/c/Users/sneak")
    with pytest.raises(SystemExit) as exc:
        assert_wsl_home_safe(_platform(is_wsl=True, wsl_version=2))
    assert exc.value.code == 1


def test_wsl_launch_fix_message_contents():
    """The constant carries the canonical SPEC §7 / P1-ERR-12 text."""
    assert "Warning: HOME appears to be a Windows path." in WSL_LAUNCH_FIX_MESSAGE
    assert "For best results, launch from WSL:" in WSL_LAUNCH_FIX_MESSAGE
    assert "wsl -d Ubuntu -- promptpal" in WSL_LAUNCH_FIX_MESSAGE


# ---------------------------------------------------------------------------
# _detect_clipboard — AC #4
# ---------------------------------------------------------------------------


def test_clipboard_xclip_is_first(monkeypatch):
    """All three Unix providers present → xclip wins."""
    monkeypatch.setattr(
        platform_mod.shutil,
        "which",
        _which_factory({"xclip", "xsel", "pbcopy"}),
    )
    assert _detect_clipboard(is_wsl=False) == ("xclip", "-selection", "clipboard")


def test_clipboard_xsel_when_no_xclip(monkeypatch):
    """xsel + pbcopy present, xclip absent → xsel wins."""
    monkeypatch.setattr(
        platform_mod.shutil, "which", _which_factory({"xsel", "pbcopy"})
    )
    assert _detect_clipboard(is_wsl=False) == ("xsel", "--clipboard", "--input")


def test_clipboard_pbcopy_when_no_xclip_xsel(monkeypatch):
    """Only pbcopy present → pbcopy selected (macOS path)."""
    monkeypatch.setattr(platform_mod.shutil, "which", _which_factory({"pbcopy"}))
    assert _detect_clipboard(is_wsl=False) == ("pbcopy",)


def test_clipboard_clip_exe_only_on_wsl(monkeypatch):
    """clip.exe present + is_wsl=True → clip.exe selected."""
    monkeypatch.setattr(platform_mod.shutil, "which", _which_factory({"clip.exe"}))
    assert _detect_clipboard(is_wsl=True) == ("clip.exe",)


def test_clipboard_clip_exe_skipped_off_wsl(monkeypatch):
    """clip.exe is_wsl=False → not selected (only available on WSL bridge)."""
    monkeypatch.setattr(platform_mod.shutil, "which", _which_factory({"clip.exe"}))
    assert _detect_clipboard(is_wsl=False) == ()


def test_clipboard_xclip_wins_over_clip_exe_on_wsl(monkeypatch):
    """On WSL with both xclip and clip.exe: xclip wins (priority order)."""
    monkeypatch.setattr(
        platform_mod.shutil, "which", _which_factory({"xclip", "clip.exe"})
    )
    assert _detect_clipboard(is_wsl=True) == ("xclip", "-selection", "clipboard")


def test_clipboard_none_available(monkeypatch):
    """Nothing on PATH → empty tuple (caller becomes a no-op + warns)."""
    monkeypatch.setattr(platform_mod.shutil, "which", _which_factory(set()))
    assert _detect_clipboard(is_wsl=True) == ()
    assert _detect_clipboard(is_wsl=False) == ()


# ---------------------------------------------------------------------------
# copy_to_clipboard — AC #5, #6
# ---------------------------------------------------------------------------


def test_copy_to_clipboard_no_provider_warns_and_returns_false(capsys):
    """No provider → one-line stderr warning, return False, no exception."""
    result = copy_to_clipboard("hello", _platform())
    assert result is False
    err = capsys.readouterr().err
    assert NO_CLIPBOARD_WARNING in err
    # AC #6 demands a *one-line* warning; print() adds exactly one trailing \n.
    assert err.count("\n") == 1


def test_copy_to_clipboard_utf8_no_bom(monkeypatch):
    """clip.exe receives UTF-8 *without* a BOM, round-trips Greek/CJK/emoji (D-9)."""
    captured: dict[str, object] = {}

    class FakeProc:
        returncode = 0
        stderr = b""

    def fake_run(cmd, *, input, capture_output, check):
        captured["cmd"] = list(cmd)
        captured["input"] = input
        captured["check"] = check
        return FakeProc()

    monkeypatch.setattr(platform_mod.subprocess, "run", fake_run)

    text = "γειά κόσμε 你好 🚀"  # Greek + CJK + emoji (P1-PLAT-06 / D-9)
    p = _platform(is_wsl=True, wsl_version=2, clipboard_cmd=("clip.exe",))
    assert copy_to_clipboard(text, p) is True

    assert captured["cmd"] == ["clip.exe"]
    sent = captured["input"]
    assert isinstance(sent, bytes)
    # No UTF-8 BOM prefix (0xEF 0xBB 0xBF) — `utf-8-sig` would add one.
    assert not sent.startswith(b"\xef\xbb\xbf")
    # Byte-for-byte round-trip clean.
    assert sent.decode("utf-8") == text
    # Don't propagate non-zero exits as exceptions.
    assert captured["check"] is False


def test_copy_to_clipboard_invokes_full_provider_argv(monkeypatch):
    """xclip's full argv (with selection flag) is forwarded to subprocess.run."""
    captured: dict[str, object] = {}

    class FakeProc:
        returncode = 0
        stderr = b""

    def fake_run(cmd, *, input, capture_output, check):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(platform_mod.subprocess, "run", fake_run)
    p = _platform(clipboard_cmd=("xclip", "-selection", "clipboard"))
    assert copy_to_clipboard("ok", p) is True
    assert captured["cmd"] == ["xclip", "-selection", "clipboard"]


def test_copy_to_clipboard_nonzero_exit_warns_returns_false(monkeypatch, capsys):
    """Provider returns non-zero → warn on stderr, return False, no raise."""

    class FakeProc:
        returncode = 1
        stderr = b"xclip: cannot open display"

    monkeypatch.setattr(
        platform_mod.subprocess, "run", lambda *_a, **_kw: FakeProc()
    )
    p = _platform(clipboard_cmd=("xclip", "-selection", "clipboard"))
    assert copy_to_clipboard("x", p) is False
    err = capsys.readouterr().err
    assert "Warning" in err
    assert "xclip" in err


def test_copy_to_clipboard_oserror_warns_returns_false(monkeypatch, capsys):
    """subprocess raises OSError → warn, return False, do not propagate."""

    def boom(*_a, **_kw):
        raise OSError("no such file")

    monkeypatch.setattr(platform_mod.subprocess, "run", boom)
    p = _platform(clipboard_cmd=("xclip",))
    assert copy_to_clipboard("x", p) is False
    assert "Warning" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# detect_platform — end-to-end
# ---------------------------------------------------------------------------


def test_detect_platform_returns_platform_instance():
    """Smoke: detect_platform() runs against the real env and returns a Platform."""
    p = detect_platform()
    assert isinstance(p, Platform)
    assert isinstance(p.is_wsl, bool)
    assert p.wsl_version in (None, 1, 2)
    assert isinstance(p.home, str)
    assert isinstance(p.clipboard_cmd, tuple)


# ---------------------------------------------------------------------------
# LF line endings — AC #7
# ---------------------------------------------------------------------------


def test_lf_line_endings_in_save_config(tmp_path):
    """Regression: files PromptPal writes use LF (no CRLF, ends with \\n)."""
    from core.config import Config, save_config

    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)
    raw = cfg_path.read_bytes()
    assert b"\r\n" not in raw, "CRLF detected in config.json"
    assert raw.endswith(b"\n"), "config.json missing trailing newline"
