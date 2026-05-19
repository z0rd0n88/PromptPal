"""Tests for uninstall.sh (US-015, SPEC §11 / P1-INST-07).

Coverage map (1 test → 1 acceptance criterion or sub-rule):

  AC #1a  Binary at $INSTALL_DIR/promptpal removed                   → test_removes_binary
  AC #1b  Missing binary is informational, not an error              → test_missing_binary_is_informational
  AC #2a  (Yn) prompt — ENTER removes data dir                       → test_yes_default_removes_data_dir
  AC #2b  (Yn) prompt — explicit y removes                           → test_explicit_y_removes_data_dir
  AC #2c  (Yn) prompt — n keeps data                                 → test_n_keeps_data_dir
  AC #2d  (Yn) prompt — typo keeps data (safe default)               → test_typo_keeps_data_dir
  AC #2e  --purge skips prompt                                       → test_purge_skips_prompt
  AC #2f  Missing data dir is informational, not an error            → test_missing_data_dir_is_informational
  AC #3   WSL HOME guard refuses to run                              → test_wsl_guard_*
  AC #5   shellcheck uninstall.sh clean                              → test_shellcheck_uninstall_sh_clean
  Hygiene Strict bash mode + LF endings                              → test_uninstall_sh_uses_strict_mode,
                                                                       test_uninstall_sh_uses_lf_line_endings

Plus drift fences:
  - WSL message in uninstall.sh mirrors WSL_LAUNCH_FIX_MESSAGE
  - WSL guard fires before touching the filesystem

Strategy: every test runs the real uninstall.sh in a fresh ``tmp_path``
acting as ``$HOME`` so nothing escapes into the real user environment.
``read`` is fed via subprocess ``input=`` so the (Yn) prompt is driven
deterministically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.platform import WSL_LAUNCH_FIX_MESSAGE

REPO_ROOT = Path(__file__).resolve().parents[2]
UNINSTALLER = REPO_ROOT / "uninstall.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_path() -> str:
    """Return a minimal PATH; matches test_install._base_path."""
    return "/usr/bin:/bin"


def _seed_install(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake post-install layout inside ``tmp_path``.

    Mirrors what install.sh produces: $HOME/.local/bin/promptpal (the
    launcher) and $HOME/.promptpal/{config.json, system-prompt.md,
    history/}. Tests can pass these to ``_run_uninstall`` and assert on
    what survives.
    """
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    promptpal = home / ".promptpal"
    history = promptpal / "history"
    bin_dir.mkdir(parents=True)
    history.mkdir(parents=True)
    (bin_dir / "promptpal").write_text(
        "#!/usr/bin/env bash\necho fake\n", encoding="utf-8"
    )
    (promptpal / "config.json").write_text("{}\n", encoding="utf-8")
    (promptpal / "system-prompt.md").write_text("placeholder\n", encoding="utf-8")
    return bin_dir, promptpal


def _run_uninstall(
    tmp_path: Path,
    *,
    bin_dir: Path | None = None,
    promptpal_home: Path | None = None,
    args: tuple[str, ...] = (),
    stdin: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke uninstall.sh with HOME=<tmp_path>/home and a controlled env."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bin_dir = bin_dir if bin_dir is not None else home / ".local" / "bin"
    promptpal_home = (
        promptpal_home if promptpal_home is not None else home / ".promptpal"
    )
    env: dict[str, str] = {
        "HOME": str(home),
        "PATH": _base_path(),
        "INSTALL_DIR": str(bin_dir),
        "PROMPTPAL_HOME": str(promptpal_home),
        "LANG": "C.UTF-8",
    }
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [str(UNINSTALLER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        input=stdin,
    )


def _run_uninstall_with_home(
    home: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke uninstall.sh with an arbitrary HOME (used by WSL-guard tests).

    INSTALL_DIR and PROMPTPAL_HOME are NOT pre-set: the guard must fire
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
        [str(UNINSTALLER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        input="",
    )


@pytest.fixture(autouse=True)
def _require_uninstaller() -> None:
    if not UNINSTALLER.exists():
        pytest.skip("uninstall.sh not present in this checkout")
    if not os.access(UNINSTALLER, os.X_OK):
        pytest.fail("uninstall.sh exists but is not executable")


# ---------------------------------------------------------------------------
# Static checks on uninstall.sh
# ---------------------------------------------------------------------------


def test_uninstall_sh_uses_strict_mode() -> None:
    """``set -euo pipefail`` makes failures fail loudly."""
    text = UNINSTALLER.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text


def test_uninstall_sh_uses_lf_line_endings() -> None:
    """Project-wide LF discipline — a CRLF script would break on Linux."""
    raw = UNINSTALLER.read_bytes()
    assert b"\r\n" not in raw, "uninstall.sh contains CRLF line endings"


def test_shellcheck_uninstall_sh_clean() -> None:
    """``shellcheck uninstall.sh`` returns 0 with no output (AC #5)."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed in this env")
    result = subprocess.run(
        [shellcheck, str(UNINSTALLER)],
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
# AC #3 — WSL HOME guard fires *before* any filesystem mutation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "home",
    [
        "/mnt/c/Users/alice",
        "/mnt/c/Users/alice/nested",
        "/mnt/c/",
        "/c/Users/alice",
        "/c/Users/alice/nested",
        "/c/",
    ],
)
def test_wsl_guard_exits_1_on_ntfs_home(tmp_path: Path, home: str) -> None:
    """uninstall.sh must refuse and touch nothing when HOME is NTFS-mounted."""
    sentinel_bin = tmp_path / "bin"
    sentinel_data = tmp_path / "promptpal-should-survive"
    sentinel_data.mkdir()
    canary = sentinel_data / "config.json"
    canary.write_text("survive me\n", encoding="utf-8")
    result = _run_uninstall_with_home(
        home,
        extra_env={
            "INSTALL_DIR": str(sentinel_bin),
            "PROMPTPAL_HOME": str(sentinel_data),
        },
    )
    assert result.returncode == 1
    assert "Warning: HOME appears to be a Windows path." in result.stderr
    assert canary.exists(), "uninstall.sh wrote to disk despite WSL HOME guard"
    assert canary.read_text(encoding="utf-8") == "survive me\n"


def test_wsl_guard_text_mirrors_constant(tmp_path: Path) -> None:
    """uninstall.sh's WSL message must mirror WSL_LAUNCH_FIX_MESSAGE line-by-line.

    Drift fence: editing one side without the other fails this test
    instead of leaking inconsistent help to users.
    """
    sentinel_data = tmp_path / "nope"
    result = _run_uninstall_with_home(
        "/mnt/c/Users/alice",
        extra_env={
            "PROMPTPAL_HOME": str(sentinel_data),
            "INSTALL_DIR": str(tmp_path / "bin"),
        },
    )
    assert result.returncode == 1
    for line in WSL_LAUNCH_FIX_MESSAGE.splitlines():
        assert line in result.stderr, (
            f"missing line in uninstall.sh output: {line!r}\n"
            f"--- stderr ---\n{result.stderr}"
        )


def test_wsl_guard_not_substring_match(tmp_path: Path) -> None:
    """A HOME like ``/tmp/foo/mnt/c`` (substring, not prefix) must pass.

    Pins the case-prefix semantics so a future edit that loosens the
    pattern to ``*mnt/c*`` would fail this test.
    """
    odd_home = tmp_path / "weird" / "mnt" / "c"
    odd_home.mkdir(parents=True)
    sentinel_bin = tmp_path / "bin"
    result = _run_uninstall_with_home(
        str(odd_home),
        extra_env={
            "INSTALL_DIR": str(sentinel_bin),
            "PROMPTPAL_HOME": str(odd_home / ".promptpal"),
        },
    )
    # The script proceeds (returncode 0) and reports "not found" for
    # both targets — i.e., the guard did NOT fire.
    assert result.returncode == 0
    assert "Warning: HOME appears to be a Windows path." not in result.stderr


# ---------------------------------------------------------------------------
# AC #1 — binary removal
# ---------------------------------------------------------------------------


def test_removes_binary(tmp_path: Path) -> None:
    bin_dir, promptpal = _seed_install(tmp_path)
    binary = bin_dir / "promptpal"
    assert binary.exists()
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        args=("--purge",),
    )
    assert result.returncode == 0, result.stderr
    assert not binary.exists()
    assert f"Removed binary: {binary}" in result.stdout


def test_missing_binary_is_informational(tmp_path: Path) -> None:
    """A second uninstall (or a fresh box) must not error on the missing binary."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = home / ".local" / "bin"
    promptpal = home / ".promptpal"
    bin_dir.mkdir(parents=True)
    promptpal.mkdir()
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        args=("--purge",),
    )
    assert result.returncode == 0
    assert "Binary not found at" in result.stdout
    assert f"{bin_dir / 'promptpal'}" in result.stdout


# ---------------------------------------------------------------------------
# AC #2 — (Yn) prompt and --purge
# ---------------------------------------------------------------------------


def test_yes_default_removes_data_dir(tmp_path: Path) -> None:
    """An empty answer (just <ENTER>) must remove the data directory.

    (Yn) semantics — uppercase Y means default-Yes.
    """
    bin_dir, promptpal = _seed_install(tmp_path)
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        stdin="\n",
    )
    assert result.returncode == 0, result.stderr
    assert not promptpal.exists()
    assert "Removed data directory" in result.stdout


def test_explicit_y_removes_data_dir(tmp_path: Path) -> None:
    bin_dir, promptpal = _seed_install(tmp_path)
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        stdin="y\n",
    )
    assert result.returncode == 0
    assert not promptpal.exists()


def test_uppercase_Y_removes_data_dir(tmp_path: Path) -> None:
    bin_dir, promptpal = _seed_install(tmp_path)
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        stdin="Y\n",
    )
    assert result.returncode == 0
    assert not promptpal.exists()


def test_n_keeps_data_dir(tmp_path: Path) -> None:
    bin_dir, promptpal = _seed_install(tmp_path)
    canary = promptpal / "config.json"
    canary_text = canary.read_text(encoding="utf-8")
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        stdin="n\n",
    )
    assert result.returncode == 0
    assert promptpal.exists()
    assert canary.read_text(encoding="utf-8") == canary_text
    assert "Keeping data directory" in result.stdout


def test_typo_keeps_data_dir(tmp_path: Path) -> None:
    """Anything other than '', y, or Y must keep the directory (safe default)."""
    bin_dir, promptpal = _seed_install(tmp_path)
    canary = promptpal / "config.json"
    canary_text = canary.read_text(encoding="utf-8")
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        stdin="qq\n",
    )
    assert result.returncode == 0
    assert promptpal.exists()
    assert canary.read_text(encoding="utf-8") == canary_text


def test_eof_keeps_data_dir(tmp_path: Path) -> None:
    """EOF on stdin (no TTY, no input at all) must keep the directory.

    Distinct from the ``"\\n"`` case (default-yes): when ``read`` itself
    returns non-zero (no input available), the script falls through to
    the ``*`` branch and keeps data. This guards against an automated
    re-run with a closed stdin silently wiping history.
    """
    bin_dir, promptpal = _seed_install(tmp_path)
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        stdin="",  # immediate EOF — read returns non-zero
    )
    assert result.returncode == 0
    assert promptpal.exists(), (
        "EOF (closed stdin) must keep data dir; got stdout=\n"
        + result.stdout
    )


def test_purge_skips_prompt(tmp_path: Path) -> None:
    """--purge removes the data dir without prompting (stdin closed)."""
    bin_dir, promptpal = _seed_install(tmp_path)
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
        args=("--purge",),
        stdin="",
    )
    assert result.returncode == 0, result.stderr
    assert not promptpal.exists()
    # No "(Yn)" prompt should appear when --purge is set.
    assert "(Yn)" not in result.stdout
    assert "(Yn)" not in result.stderr


def test_missing_data_dir_is_informational(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "promptpal").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    promptpal = home / ".promptpal"  # never created
    result = _run_uninstall(
        tmp_path,
        bin_dir=bin_dir,
        promptpal_home=promptpal,
    )
    assert result.returncode == 0
    assert "Data directory not found" in result.stdout
    # Binary should still have been removed in step 1.
    assert not (bin_dir / "promptpal").exists()


# ---------------------------------------------------------------------------
# Unknown-flag handling
# ---------------------------------------------------------------------------


def test_unknown_flag_exits_1(tmp_path: Path) -> None:
    result = _run_uninstall(
        tmp_path,
        args=("--nope",),
    )
    assert result.returncode == 1
    assert "unknown argument" in result.stderr.lower()


def test_help_flag_exits_0(tmp_path: Path) -> None:
    result = _run_uninstall(
        tmp_path,
        args=("--help",),
    )
    assert result.returncode == 0
    assert "Usage: uninstall.sh" in result.stdout
