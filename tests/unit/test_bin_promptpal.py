"""Tests for the bin/promptpal bash launcher and core/main.py (US-012).

Coverage map (1 test → 1 acceptance criterion or sub-rule):

  AC #1  Invokes promptpal_main.py by path (not ``-m core.main``)     → test_help_*
  AC #1b core.main.main() delegates to core.cli.main()                → test_core_main_delegates_to_cli
  AC #1c Argv is forwarded verbatim (no munging in the wrapper)       → test_launcher_forwards_argv_verbatim
  AC #1d Python exit code propagates through ``exec``                 → test_launcher_propagates_python_exit_code
  AC #2a WSL guard: HOME=/mnt/c/* exits 1 with the fix message       → test_wsl_guard_mnt_c_*
  AC #2b WSL guard: HOME=/c/* exits 1 with the fix message           → test_wsl_guard_slash_c_*
  AC #2c WSL guard text mirrors WSL_LAUNCH_FIX_MESSAGE                → test_wsl_guard_text_pins_constant
  AC #2d WSL guard runs *before* Python (Python errors don't appear) → test_wsl_guard_runs_before_python
  AC #2e Guard matches by *prefix*, not substring                     → test_wsl_guard_substring_not_matched
  AC #3  shellcheck bin/promptpal returns clean                       → test_shellcheck_clean
  AC #4  Strict bash mode + LF line endings (P1-PLAT-08)              → test_launcher_uses_strict_mode,
                                                                       test_launcher_uses_lf_line_endings

Strategy: every subprocess test launches the real bash script with a
controlled env. ``--help`` is the safest happy-path probe because argparse
exits 0 before any backend, filesystem, or network code runs. A few tests
stub ``python3`` with a bash shim so the wrapper's argv handling and exit-
code propagation can be verified independently of the real CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.platform import WSL_LAUNCH_FIX_MESSAGE

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "bin" / "promptpal"


def _run(
    home: str,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the launcher with a minimal controlled env."""
    env: dict[str, str] = {
        "HOME": home,
        # Keep PATH because we need /usr/bin/python3.
        "PATH": os.environ.get("PATH", ""),
        # Avoid leaking the developer's API key into a subprocess that
        # *might* probe a backend if argparse changes shape later.
        "LANG": "C.UTF-8",
    }
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [str(LAUNCHER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.fixture(autouse=True)
def _require_launcher() -> None:
    if not LAUNCHER.exists():
        pytest.skip("bin/promptpal not present in this checkout")
    if not os.access(LAUNCHER, os.X_OK):
        pytest.fail("bin/promptpal exists but is not executable")


# ---------------------------------------------------------------------------
# AC #4 — Static checks against the launcher file
# ---------------------------------------------------------------------------


def test_launcher_uses_strict_mode() -> None:
    """``set -euo pipefail`` makes failures fail loudly (no silent drift)."""
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text


def test_launcher_uses_lf_line_endings() -> None:
    """Project-wide LF discipline — a CRLF script would break on Linux."""
    raw = LAUNCHER.read_bytes()
    assert b"\r\n" not in raw, "launcher contains CRLF line endings"


def test_launcher_invokes_bootstrap_by_path() -> None:
    """The launcher must invoke the bootstrap *by path*, not ``-m``.

    ``python3 -m core.main`` puts the CWD on ``sys.path[0]`` and lets a
    stray ``core/`` in the user's directory shadow the real package.
    Invoking ``promptpal_main.py`` by path puts the script's own dir on
    ``sys.path[0]`` instead. The negative assertion is the regression
    guard against reverting to ``-m``.
    """
    text = LAUNCHER.read_text(encoding="utf-8")
    # Inspect the actual exec line (ignore explanatory comments that may
    # mention the old ``-m core.main`` invocation).
    exec_lines = [ln for ln in text.splitlines() if ln.strip().startswith("exec ")]
    assert len(exec_lines) == 1, exec_lines
    assert "promptpal_main.py" in exec_lines[0]
    assert "-m core.main" not in exec_lines[0]


# ---------------------------------------------------------------------------
# AC #1 — Invokes promptpal_main.py by path
# ---------------------------------------------------------------------------


def test_help_runs_python_module_and_exits_0(tmp_path: Path) -> None:
    """A safe HOME + --help reaches argparse, which exits 0."""
    result = _run(str(tmp_path), ["--help"])
    assert result.returncode == 0, result.stderr
    # argparse prints 'usage: promptpal ...' on stdout for --help.
    assert "usage:" in result.stdout.lower()
    assert "promptpal" in result.stdout.lower()


def test_help_lists_status_flag(tmp_path: Path) -> None:
    """Sanity: the forwarded module is the real CLI, not a stub."""
    result = _run(str(tmp_path), ["--help"])
    assert result.returncode == 0
    assert "--status" in result.stdout


# ---------------------------------------------------------------------------
# AC #2 — WSL HOME guard fires before Python starts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "home",
    [
        "/mnt/c/Users/alice",
        "/mnt/c/Users/alice/nested/path",
        "/mnt/c/",
    ],
)
def test_wsl_guard_mnt_c_exits_1(home: str) -> None:
    result = _run(home, ["--help"])
    assert result.returncode == 1
    assert "Warning: HOME appears to be a Windows path." in result.stderr
    # Python's --help would print 'usage:' on stdout — it must not have run.
    assert result.stdout == ""


@pytest.mark.parametrize(
    "home",
    [
        "/c/Users/alice",
        "/c/Users/alice/nested/path",
        "/c/",
    ],
)
def test_wsl_guard_slash_c_exits_1(home: str) -> None:
    result = _run(home, ["--help"])
    assert result.returncode == 1
    assert "Warning: HOME appears to be a Windows path." in result.stderr
    assert result.stdout == ""


def test_wsl_guard_text_pins_constant() -> None:
    """Bash message must mirror WSL_LAUNCH_FIX_MESSAGE line by line.

    This is the *drift fence*: editing one side without the other
    fails this test instead of leaking inconsistent help to users.
    """
    result = _run("/mnt/c/Users/alice", ["--help"])
    assert result.returncode == 1
    for line in WSL_LAUNCH_FIX_MESSAGE.splitlines():
        assert line in result.stderr, (
            f"missing line in bash output: {line!r}\n"
            f"--- stderr ---\n{result.stderr}"
        )


def test_wsl_guard_runs_before_python() -> None:
    """Even with an arg argparse would reject, the guard still fires first.

    If Python ran first we would see argparse's 'error: unrecognized
    arguments' on stderr instead of the WSL message; the test pins the
    ordering.
    """
    result = _run("/mnt/c/Users/alice", ["--this-flag-does-not-exist"])
    assert result.returncode == 1
    assert "Warning: HOME appears to be a Windows path." in result.stderr
    # Python's argparse error message would contain 'unrecognized'.
    assert "unrecognized" not in result.stderr.lower()


def test_non_wsl_home_passes_through(tmp_path: Path) -> None:
    """A normal HOME does NOT trigger the guard."""
    result = _run(str(tmp_path), ["--help"])
    assert "Warning: HOME appears to be a Windows path." not in result.stderr


def test_wsl_guard_substring_not_matched(tmp_path: Path) -> None:
    """Only the leading prefix ``/mnt/c/`` or ``/c/`` matters — a path that
    merely *contains* one of those substrings mid-string must not trigger
    the guard."""
    safe = tmp_path / "deep_/mnt/c_in_middle"
    safe.mkdir(parents=True)
    result = _run(str(safe), ["--help"])
    assert "Warning: HOME appears to be a Windows path." not in result.stderr


# ---------------------------------------------------------------------------
# AC #1c — Argv is forwarded verbatim (verified with a python3 shim)
# ---------------------------------------------------------------------------


def test_launcher_forwards_argv_verbatim(tmp_path: Path) -> None:
    """Args passed to the launcher must reach ``python3`` unchanged.

    Stubs ``python3`` with a bash shim that records every argv element to
    a file. The wrapper does ``exec python3 <repo>/promptpal_main.py
    "$@"``, so the shim sees the bootstrap path followed by the user's
    argv.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    args_file = tmp_path / "args.txt"
    shim = bindir / "python3"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{args_file}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    result = _run(
        str(home),
        ["--quiet", "improve this prompt", "--model", "x"],
        extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    forwarded = args_file.read_text(encoding="utf-8").splitlines()
    assert forwarded[0].endswith("promptpal_main.py")
    assert forwarded[1:] == ["--quiet", "improve this prompt", "--model", "x"]


def test_launcher_propagates_python_exit_code(tmp_path: Path) -> None:
    """``exec`` replaces the bash process so python3's exit code is the
    caller's exit code — no munging in the wrapper."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "python3"
    shim.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
    shim.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    result = _run(
        str(home),
        [],
        extra_env={"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")},
    )
    assert result.returncode == 42


# ---------------------------------------------------------------------------
# Regression — a stray ``core/`` in the CWD must NOT shadow the real package
# ---------------------------------------------------------------------------


def test_stray_core_in_cwd_does_not_shadow_real_package(tmp_path: Path) -> None:
    """Running from a dir that has its own ``core/`` must still work.

    This is the regression guard for the ``python3 -m core.main`` footgun:
    ``-m`` put the CWD on ``sys.path[0]``, so a sabotage ``core/`` in the
    working directory would be imported instead of the real one. The
    path-invoked bootstrap puts the *script's* dir first, so the booby
    trap is never imported and ``--help`` still exits 0.
    """
    booby = tmp_path / "booby"
    (booby / "core").mkdir(parents=True)
    # If this package is ever imported, it explodes loudly.
    (booby / "core" / "__init__.py").write_text(
        'raise RuntimeError("WRONG core/ imported from CWD")\n',
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [str(LAUNCHER), "--help"],
        cwd=str(booby),
        env={"HOME": str(home), "PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        f"launcher imported the stray core/ from CWD.\n--- stderr ---\n{result.stderr}"
    )
    assert "usage:" in result.stdout.lower()
    assert "WRONG core/" not in result.stderr


# ---------------------------------------------------------------------------
# AC #1b — core.main.main() delegates to core.cli.main()
# ---------------------------------------------------------------------------


def test_core_main_delegates_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """``core.main.main()`` must forward to ``core.cli.main()`` verbatim.

    Patches the import-time binding in ``core.main`` (``_cli_main``) so the
    delegation is observable without running the real pipeline.
    """
    import core.main as main_mod

    captured: dict[str, int] = {}

    def fake_cli_main() -> int:
        captured["called"] = 1
        return 42

    monkeypatch.setattr(main_mod, "_cli_main", fake_cli_main)
    rc = main_mod.main()
    assert rc == 42
    assert captured == {"called": 1}


# ---------------------------------------------------------------------------
# AC #3 — shellcheck clean
# ---------------------------------------------------------------------------


def test_shellcheck_clean() -> None:
    """``shellcheck bin/promptpal`` returns 0 with no output."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed in this env")
    result = subprocess.run(
        [shellcheck, str(LAUNCHER)],
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
