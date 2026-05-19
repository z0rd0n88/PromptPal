"""Tests for install.sh (US-013, SPEC §10).

Coverage map (1 test → 1 acceptance criterion or sub-rule):

  AC #1  Works on macOS / Linux / WSL2 with bash + python3        → all subprocess tests
  AC #2  WSL HOME guard refuses to install                         → test_wsl_guard_*
  AC #3a Binary placed at $INSTALL_DIR/promptpal                   → test_fresh_install_places_binary
  AC #3b PATH warning when INSTALL_DIR not on PATH                 → test_warns_when_install_dir_not_on_path
  AC #3c No warning when INSTALL_DIR on PATH                       → test_no_warning_when_install_dir_on_path
  AC #4a ~/.promptpal/history exists after install                 → test_fresh_install_creates_history_dir
  AC #4b config.json seeded from defaults/config.json              → test_fresh_install_seeds_config_from_defaults
  AC #4c system-prompt.md seeded from core/system_prompt.txt       → test_fresh_install_seeds_system_prompt
  AC #4d Existing config.json never overwritten                    → test_reinstall_preserves_user_config
  AC #4e Existing system-prompt.md never overwritten               → test_reinstall_preserves_user_system_prompt
  AC #5a Backend check: claude detected                            → test_backend_check_lists_claude_cli
  AC #5b Backend check: API key detected                           → test_backend_check_lists_api_key
  AC #5c No backend → two-option hint, exit 0                      → test_backend_check_two_option_hint_when_none
  AC #6  shellcheck install.sh clean                               → test_shellcheck_install_sh_clean
  AC #6b shellcheck generated launcher clean                       → test_shellcheck_generated_launcher_clean
  AC #7  Strict bash mode + LF endings (project-wide hygiene)      → test_install_sh_uses_strict_mode,
                                                                     test_install_sh_uses_lf_line_endings,
                                                                     test_generated_launcher_uses_lf_line_endings

Plus structural fences:
  - Generated launcher carries the WSL HOME guard and exits 1 on /mnt/c/*
  - Generated launcher's WSL message mirrors WSL_LAUNCH_FIX_MESSAGE
  - Installed lib/ contains core/ and defaults/ (overwrites on re-install)
  - End-to-end: installed binary handles --help via the real CLI

Strategy: every test runs the real install.sh in a fresh ``tmp_path``
acting as ``$HOME`` so nothing escapes into the real user environment.
A controlled ``PATH`` is passed so the backend-check tests can flip
``claude`` on/off by adding/removing a shim from PATH.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.backend import NoBackendError
from core.platform import WSL_LAUNCH_FIX_MESSAGE

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "install.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_path() -> str:
    """Return a minimal PATH that has /usr/bin/python3 + coreutils.

    Deliberately omits ``/usr/local/bin`` because some dev/CI machines
    have a real ``claude`` binary installed there — that would silently
    flip the no-backend tests to the has-claude branch. Backend-shim
    tests prepend their own dir to make ``claude`` available on purpose.
    """
    return "/usr/bin:/bin"


def _run_install(
    tmp_path: Path,
    *,
    install_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
    on_path: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke install.sh with HOME=<tmp_path>/home and a controlled env."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    install_dir = install_dir if install_dir is not None else home / ".local" / "bin"
    path = _base_path()
    if on_path:
        path = f"{install_dir}:{path}"
    env: dict[str, str] = {
        "HOME": str(home),
        "PATH": path,
        "INSTALL_DIR": str(install_dir),
        "PROMPTPAL_HOME": str(home / ".promptpal"),
        "LANG": "C.UTF-8",
    }
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [str(INSTALLER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _run_install_with_home(
    home: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke install.sh with an arbitrary HOME (used by WSL-guard tests).

    PROMPTPAL_HOME and INSTALL_DIR are NOT pre-set: the guard must fire
    before the script reads them, so a Windows-path HOME never leads to
    real filesystem writes.
    """
    env: dict[str, str] = {
        "HOME": home,
        "PATH": _base_path(),
        "LANG": "C.UTF-8",
    }
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [str(INSTALLER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.fixture(autouse=True)
def _require_installer() -> None:
    if not INSTALLER.exists():
        pytest.skip("install.sh not present in this checkout")
    if not os.access(INSTALLER, os.X_OK):
        pytest.fail("install.sh exists but is not executable")


# ---------------------------------------------------------------------------
# Static checks on install.sh
# ---------------------------------------------------------------------------


def test_install_sh_uses_strict_mode() -> None:
    """``set -euo pipefail`` makes failures fail loudly (no silent drift)."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text


def test_install_sh_uses_lf_line_endings() -> None:
    """Project-wide LF discipline — a CRLF script would break on Linux."""
    raw = INSTALLER.read_bytes()
    assert b"\r\n" not in raw, "install.sh contains CRLF line endings"


def test_shellcheck_install_sh_clean() -> None:
    """``shellcheck install.sh`` returns 0 with no output (AC #6)."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed in this env")
    result = subprocess.run(
        [shellcheck, str(INSTALLER)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        f"shellcheck failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert result.stdout == ""
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# AC #2 — WSL HOME guard fires *before* any filesystem write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "home",
    [
        "/mnt/c/Users/alice",
        "/mnt/c/Users/alice/nested/path",
        "/mnt/c/",
        "/c/Users/alice",
        "/c/Users/alice/nested/path",
        "/c/",
    ],
)
def test_wsl_guard_exits_1_on_ntfs_home(tmp_path: Path, home: str) -> None:
    """install.sh must refuse and write nothing when HOME is NTFS-mounted."""
    # Point PROMPTPAL_HOME at the sandbox so a leaked write would be visible.
    sentinel = tmp_path / "promptpal-should-not-exist"
    result = _run_install_with_home(
        home,
        extra_env={"PROMPTPAL_HOME": str(sentinel), "INSTALL_DIR": str(tmp_path / "bin")},
    )
    assert result.returncode == 1
    assert "Warning: HOME appears to be a Windows path." in result.stderr
    assert not sentinel.exists(), "install.sh wrote to disk despite WSL HOME guard"


def test_wsl_guard_text_mirrors_constant(tmp_path: Path) -> None:
    """install.sh's WSL message must mirror WSL_LAUNCH_FIX_MESSAGE line-by-line.

    This is the *drift fence*: editing one side without the other fails
    this test instead of leaking inconsistent help to users.
    """
    result = _run_install_with_home(
        "/mnt/c/Users/alice",
        extra_env={"PROMPTPAL_HOME": str(tmp_path / "nope"), "INSTALL_DIR": str(tmp_path / "bin")},
    )
    assert result.returncode == 1
    for line in WSL_LAUNCH_FIX_MESSAGE.splitlines():
        assert line in result.stderr, (
            f"missing line in install.sh output: {line!r}\n"
            f"--- stderr ---\n{result.stderr}"
        )


def test_wsl_guard_substring_not_matched(tmp_path: Path) -> None:
    """Only leading ``/mnt/c/`` or ``/c/`` matters — a path that merely
    *contains* one of those substrings mid-string must not trigger."""
    safe = tmp_path / "deep_mnt_c_in_middle"
    safe.mkdir()
    result = _run_install(tmp_path, extra_env={"HOME": str(safe)})
    assert "Warning: HOME appears to be a Windows path." not in result.stderr


# ---------------------------------------------------------------------------
# AC #3 / #4 — Filesystem layout after a fresh install
# ---------------------------------------------------------------------------


def test_fresh_install_creates_history_dir(tmp_path: Path) -> None:
    result = _run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home" / ".promptpal" / "history").is_dir()


def test_fresh_install_seeds_config_from_defaults(tmp_path: Path) -> None:
    result = _run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    seeded = (tmp_path / "home" / ".promptpal" / "config.json").read_bytes()
    source = (REPO_ROOT / "defaults" / "config.json").read_bytes()
    assert seeded == source


def test_fresh_install_seeds_system_prompt_from_source(tmp_path: Path) -> None:
    result = _run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    seeded = (tmp_path / "home" / ".promptpal" / "system-prompt.md").read_bytes()
    source = (REPO_ROOT / "core" / "system_prompt.txt").read_bytes()
    assert seeded == source


def test_fresh_install_lays_down_lib_root(tmp_path: Path) -> None:
    """``~/.promptpal/lib/`` must contain core/ and defaults/ so the
    generated launcher can find ``core.main`` via PYTHONPATH."""
    result = _run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    lib = tmp_path / "home" / ".promptpal" / "lib"
    assert (lib / "core" / "__init__.py").is_file()
    assert (lib / "core" / "main.py").is_file()
    assert (lib / "defaults" / "config.json").is_file()


def test_fresh_install_places_binary(tmp_path: Path) -> None:
    result = _run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    binary = tmp_path / "home" / ".local" / "bin" / "promptpal"
    assert binary.is_file()
    assert binary.stat().st_mode & stat.S_IXUSR, "binary is not executable"


def test_reinstall_preserves_user_config(tmp_path: Path) -> None:
    """AC #4: existing config.json must NEVER be overwritten."""
    _run_install(tmp_path)
    config = tmp_path / "home" / ".promptpal" / "config.json"
    custom = '{"version": 1, "default_iterations": 99, "user_edited": true}\n'
    config.write_text(custom, encoding="utf-8")
    second = _run_install(tmp_path)
    assert second.returncode == 0, second.stderr
    assert config.read_text(encoding="utf-8") == custom


def test_reinstall_preserves_user_system_prompt(tmp_path: Path) -> None:
    """AC #4: existing system-prompt.md must NEVER be overwritten."""
    _run_install(tmp_path)
    prompt = tmp_path / "home" / ".promptpal" / "system-prompt.md"
    custom = "MY CUSTOM PROMPT — please do not clobber\n"
    prompt.write_text(custom, encoding="utf-8")
    second = _run_install(tmp_path)
    assert second.returncode == 0, second.stderr
    assert prompt.read_text(encoding="utf-8") == custom


def test_reinstall_refreshes_lib_root(tmp_path: Path) -> None:
    """``~/.promptpal/lib/`` is managed — re-install replaces its contents.

    Drops a sentinel into lib/core/, re-runs install, and verifies the
    sentinel is gone (proving the old tree was scrubbed).
    """
    _run_install(tmp_path)
    sentinel = tmp_path / "home" / ".promptpal" / "lib" / "core" / "_stale.py"
    sentinel.write_text("# stale from previous install\n", encoding="utf-8")
    assert sentinel.exists()
    second = _run_install(tmp_path)
    assert second.returncode == 0, second.stderr
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# AC #3b/c — PATH warning behavior
# ---------------------------------------------------------------------------


def test_warns_when_install_dir_not_on_path(tmp_path: Path) -> None:
    result = _run_install(tmp_path, on_path=False)
    assert result.returncode == 0, result.stderr
    assert "is not on your PATH" in result.stderr
    # The copy-paste line is offered to the user.
    assert "export PATH=" in result.stderr


def test_no_warning_when_install_dir_on_path(tmp_path: Path) -> None:
    result = _run_install(tmp_path, on_path=True)
    assert result.returncode == 0, result.stderr
    assert "is not on your PATH" not in result.stderr


# ---------------------------------------------------------------------------
# AC #5 — Backend check is informational and never fatal
# ---------------------------------------------------------------------------


def test_backend_check_two_option_hint_when_none(tmp_path: Path) -> None:
    """No claude on PATH and no API key → print two-option hint, exit 0."""
    result = _run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    # Both option labels from NoBackendError.MESSAGE must appear.
    assert "Option 1 (Claude CLI):" in result.stdout
    assert "Option 2 (API key):" in result.stdout


def test_two_option_hint_mirrors_constant(tmp_path: Path) -> None:
    """install.sh's option labels must match NoBackendError.MESSAGE.

    Drift fence: if a developer renames the options in core/backend.py
    without updating install.sh, this test fails.
    """
    result = _run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    for label in ("Option 1 (Claude CLI):", "Option 2 (API key):"):
        assert label in NoBackendError.MESSAGE
        assert label in result.stdout


def test_backend_check_lists_claude_cli(tmp_path: Path) -> None:
    """``claude`` on PATH → installer reports it and does NOT print the hint."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    claude_shim = shim_dir / "claude"
    claude_shim.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    claude_shim.chmod(0o755)
    result = _run_install(
        tmp_path,
        extra_env={"PATH": f"{shim_dir}:{_base_path()}"},
    )
    assert result.returncode == 0, result.stderr
    assert "Claude CLI detected on PATH" in result.stdout
    assert "Option 1 (Claude CLI):" not in result.stdout


def test_backend_check_lists_api_key(tmp_path: Path) -> None:
    """``ANTHROPIC_API_KEY`` set → installer reports it and skips the hint."""
    result = _run_install(
        tmp_path,
        extra_env={"ANTHROPIC_API_KEY": "sk-ant-test-not-real"},
    )
    assert result.returncode == 0, result.stderr
    assert "ANTHROPIC_API_KEY is set" in result.stdout
    assert "Option 1 (Claude CLI):" not in result.stdout
    # Sanity: the *value* never leaks to stdout/stderr.
    assert "sk-ant-test-not-real" not in result.stdout
    assert "sk-ant-test-not-real" not in result.stderr


def test_backend_check_empty_api_key_treated_as_unset(tmp_path: Path) -> None:
    """An empty ``ANTHROPIC_API_KEY`` should NOT count as configured."""
    result = _run_install(tmp_path, extra_env={"ANTHROPIC_API_KEY": ""})
    assert result.returncode == 0, result.stderr
    assert "ANTHROPIC_API_KEY is set" not in result.stdout
    assert "Option 1 (Claude CLI):" in result.stdout


# ---------------------------------------------------------------------------
# Generated launcher — structural checks
# ---------------------------------------------------------------------------


def _install_and_get_wrapper(tmp_path: Path) -> Path:
    result = _run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    return tmp_path / "home" / ".local" / "bin" / "promptpal"


def test_generated_launcher_uses_lf_line_endings(tmp_path: Path) -> None:
    wrapper = _install_and_get_wrapper(tmp_path)
    assert b"\r\n" not in wrapper.read_bytes()


def test_generated_launcher_uses_strict_mode(tmp_path: Path) -> None:
    wrapper = _install_and_get_wrapper(tmp_path)
    assert "set -euo pipefail" in wrapper.read_text(encoding="utf-8")


def test_generated_launcher_bakes_absolute_lib_path(tmp_path: Path) -> None:
    """``$promptpal_lib`` must expand at install time (not at run time).

    A run-time expansion would leak the developer's ``$PROMPTPAL_HOME``
    env into the user's launcher.
    """
    wrapper = _install_and_get_wrapper(tmp_path)
    text = wrapper.read_text(encoding="utf-8")
    expected = str(tmp_path / "home" / ".promptpal" / "lib")
    assert f'PROMPTPAL_LIB="{expected}"' in text


def test_generated_launcher_carries_wsl_guard(tmp_path: Path) -> None:
    wrapper = _install_and_get_wrapper(tmp_path)
    text = wrapper.read_text(encoding="utf-8")
    assert "/mnt/c/*|/c/*" in text
    assert "wsl -d Ubuntu -- promptpal" in text


def test_shellcheck_generated_launcher_clean(tmp_path: Path) -> None:
    """The generated launcher is shipped to users; shellcheck it too."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed in this env")
    wrapper = _install_and_get_wrapper(tmp_path)
    result = subprocess.run(
        [shellcheck, str(wrapper)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        f"shellcheck failed on generated launcher:\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_generated_launcher_wsl_guard_runs(tmp_path: Path) -> None:
    """Run the installed launcher with a Windows-path HOME and confirm
    it exits 1 with the canonical message."""
    wrapper = _install_and_get_wrapper(tmp_path)
    result = subprocess.run(
        [str(wrapper), "--help"],
        env={
            "HOME": "/mnt/c/Users/alice",
            "PATH": _base_path(),
            "LANG": "C.UTF-8",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    for line in WSL_LAUNCH_FIX_MESSAGE.splitlines():
        assert line in result.stderr


def test_generated_launcher_help_works(tmp_path: Path) -> None:
    """End-to-end: installed launcher resolves core.main via PYTHONPATH."""
    wrapper = _install_and_get_wrapper(tmp_path)
    safe_home = tmp_path / "runtime_home"
    safe_home.mkdir()
    result = subprocess.run(
        [str(wrapper), "--help"],
        env={
            "HOME": str(safe_home),
            "PATH": _base_path(),
            "LANG": "C.UTF-8",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "promptpal" in result.stdout.lower()
