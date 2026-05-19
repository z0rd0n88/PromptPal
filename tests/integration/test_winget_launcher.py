"""Windows launcher smoke tests (US-014, AC-WINGET-01..02).

These tests exercise the real ``launcher/promptpal.ps1`` against a real
``wsl.exe`` and PowerShell environment. They are skipped unless the
``WSL_INTEGRATION=1`` env var is set, because the WSL Ubuntu side must
have ``promptpal`` already installed via ``install.sh`` and PowerShell
must be on PATH. The PRD §10 test plan calls this file out explicitly:

    | AC-WINGET-01..02 | tests/integration/test_winget_launcher.py NEW
      (skipped unless WSL_INTEGRATION=1) | Smoke test from PowerShell host (D-5) |

The unit-test side (``tests/unit/test_winget_launcher.py``) pins the
launcher *contents* — what it says, what it forwards, what it never
touches — so US-014 has full coverage even on machines where this
integration suite skips.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.winget_launcher import (
    PROMPTPAL_PS1_PATH,
    WSL_UBUNTU_MISSING_MESSAGE,
)


def _wsl_integration_enabled() -> bool:
    return os.environ.get("WSL_INTEGRATION") == "1"


def _powershell() -> str | None:
    """Return the path to a PowerShell binary, or None when unavailable."""
    for name in ("pwsh", "powershell.exe", "powershell"):
        path = shutil.which(name)
        if path is not None:
            return path
    return None


pytestmark = pytest.mark.skipif(
    not _wsl_integration_enabled(),
    reason="set WSL_INTEGRATION=1 to run the Windows launcher smoke tests",
)


@pytest.fixture
def powershell_bin() -> str:
    bin_path = _powershell()
    if bin_path is None:
        pytest.skip("powershell / pwsh not available on PATH")
    return bin_path


def _run_ps1(
    powershell_bin: str,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env: dict[str, str] = dict(os.environ)
    if extra_env is not None:
        env.update(extra_env)
    cmd = [
        powershell_bin,
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PROMPTPAL_PS1_PATH),
        *args,
    ]
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC-WINGET-01 — missing WSL Ubuntu prints the install hint, exits 1.
# ---------------------------------------------------------------------------


def test_missing_wsl_ubuntu_prints_install_hint(powershell_bin: str) -> None:
    """Stubbed wsl.exe on PATH that reports no distros installed."""
    # Use a controlled PATH so a fake wsl.exe shim is found first.
    fake_path = Path("/tmp/promptpal-fake-wsl")
    fake_path.mkdir(parents=True, exist_ok=True)
    shim = fake_path / "wsl.exe"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)

    result = _run_ps1(
        powershell_bin,
        ["test"],
        extra_env={"PATH": f"{fake_path}:{os.environ.get('PATH', '')}"},
    )
    assert result.returncode == 1
    assert WSL_UBUNTU_MISSING_MESSAGE in result.stderr


# ---------------------------------------------------------------------------
# AC-WINGET-02 — present WSL Ubuntu lands at the WSL promptpal binary.
# ---------------------------------------------------------------------------


def test_present_wsl_forwards_to_inner_promptpal(powershell_bin: str) -> None:
    """Requires WSL Ubuntu installed *and* PromptPal installed inside it."""
    result = _run_ps1(powershell_bin, ["--version"])
    # --version is the cheapest probe that proves the inner Python CLI ran.
    assert result.returncode == 0, result.stderr
    assert "promptpal" in result.stdout.lower(), result.stdout
