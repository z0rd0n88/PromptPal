"""Canonical constants for the Windows winget launcher (D-5 / P1-INST-06).

The launcher itself lives under ``launcher/`` as ``.cmd`` + ``.ps1`` (no
Python), but the user-visible "WSL Ubuntu missing" message is a contract
between the launcher and the upstream documentation. Keeping the string
here lets the test suite pin every copy verbatim via the same drift-fence
pattern as :data:`core.platform.WSL_LAUNCH_FIX_MESSAGE` (US-012).

Editing the message text? Edit *one* place — the test
``tests/unit/test_winget_launcher.py`` fails until both ``promptpal.ps1``
and ``launcher/README.md`` are updated to match.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

LAUNCHER_DIR: Path = REPO_ROOT / "launcher"
"""Directory containing the Windows launcher files and winget manifests."""

PROMPTPAL_CMD_PATH: Path = LAUNCHER_DIR / "promptpal.cmd"
"""Entry point winget aliases to ``promptpal`` on PATH."""

PROMPTPAL_PS1_PATH: Path = LAUNCHER_DIR / "promptpal.ps1"
"""PowerShell script that performs WSL Ubuntu detection + arg forwarding."""

WINGET_MANIFEST_DIR: Path = (
    LAUNCHER_DIR / "winget" / "manifests" / "p" / "PromptPal" / "PromptPal" / "0.1.0"
)
"""Versioned winget multi-file manifest directory (publisher/package/version)."""

WSL_UBUNTU_MISSING_MESSAGE: str = (
    "PromptPal requires WSL Ubuntu. Run: wsl --install -d Ubuntu"
)
"""Stderr message printed by the launcher when WSL Ubuntu is not installed (AC-WINGET-01)."""

WSL_FORWARD_COMMAND: str = "wsl.exe -d Ubuntu -- promptpal"
"""Static prefix of the launcher's forward command when WSL Ubuntu is present
(AC-WINGET-02). The runtime launcher in ``launcher/promptpal.ps1`` appends
``@args`` after this prefix; the L11 (issue #30) clarification is that this
constant encodes the prefix only — it is not a complete shell command and
the test fence is a substring match, not an equality check."""

WINGET_PACKAGE_IDENTIFIER: str = "PromptPal.PromptPal"
"""winget package identifier — must match all three manifest files."""

WINGET_PACKAGE_VERSION: str = "0.1.0"
"""winget package version — must match all three manifest files and the manifest directory name."""

WINGET_MANIFEST_VERSION: str = "1.6.0"
"""winget schema version used by all three manifests in the bundled set."""
