"""Tests for the Windows winget launcher (US-014, D-5 / P1-INST-06).

Coverage map (1 test → 1 acceptance criterion or sub-rule):

  AC #1a  launcher/promptpal.cmd exists and delegates to .ps1          → test_cmd_delegates_to_ps1
  AC #1b  launcher/promptpal.ps1 exists and detects WSL                → test_ps1_uses_wsl_list_quiet_for_detection
  AC #1c  Missing WSL Ubuntu → wsl --install message + exit 1          → test_ps1_emits_install_message_when_ubuntu_missing,
                                                                         test_ps1_exits_1_when_ubuntu_missing
  AC #2a  Present WSL Ubuntu → forwards via wsl -d Ubuntu -- promptpal → test_ps1_forwards_via_wsl_d_ubuntu
  AC #2b  Args pass through with @args splat (verbatim)                → test_ps1_forwards_args_with_splat
  AC #3   No Anthropic logic anywhere in launcher/                      → test_no_anthropic_logic_in_launcher_files
  AC #4a  winget version manifest valid + correct package id           → test_version_manifest_fields
  AC #4b  winget installer manifest valid + portable + alias           → test_installer_manifest_fields
  AC #4c  winget locale manifest valid + license + tags                → test_locale_manifest_fields
  AC #4d  All three manifests share PackageIdentifier + PackageVersion → test_manifests_share_identity
  AC #4e  Manifest directory follows publisher/package/version layout  → test_manifest_directory_layout
  AC #5   Integration smoke test exists (skipped unless WSL_INTEGRATION) → handled in
                                                                          tests/integration/test_winget_launcher.py

Plus drift fences:
  - WSL_UBUNTU_MISSING_MESSAGE matches verbatim in promptpal.ps1 + README → test_wsl_missing_message_pins_constant
  - WSL_FORWARD_COMMAND substring present in promptpal.ps1               → test_forward_command_pins_constant
  - Launcher files use LF endings (NFR-08)                               → test_launcher_files_use_lf_line_endings

YAML parsing strategy: the manifests use a tiny subset of YAML (scalar
key/value pairs, indented sub-mappings, simple lists). A lightweight
``_yaml_scan`` helper walks lines and extracts the scalar values needed
for assertions — avoids adding a PyYAML dependency (NFR-11).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.winget_launcher import (
    LAUNCHER_DIR,
    PROMPTPAL_CMD_PATH,
    PROMPTPAL_PS1_PATH,
    WINGET_MANIFEST_DIR,
    WINGET_MANIFEST_VERSION,
    WINGET_PACKAGE_IDENTIFIER,
    WINGET_PACKAGE_VERSION,
    WSL_FORWARD_COMMAND,
    WSL_UBUNTU_MISSING_MESSAGE,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_README = LAUNCHER_DIR / "README.md"

VERSION_MANIFEST = WINGET_MANIFEST_DIR / "PromptPal.PromptPal.yaml"
INSTALLER_MANIFEST = WINGET_MANIFEST_DIR / "PromptPal.PromptPal.installer.yaml"
LOCALE_MANIFEST = WINGET_MANIFEST_DIR / "PromptPal.PromptPal.locale.en-US.yaml"


# ---------------------------------------------------------------------------
# Minimal YAML scanner
# ---------------------------------------------------------------------------


def _yaml_scalar(text: str, key: str) -> str | None:
    """Return the scalar value for a ``key: value`` mapping at any depth.

    Walks each line so leading indentation doesn't matter — sufficient
    for the manifest assertions we make. Returns ``None`` when the key
    is absent or introduces a block (no inline value).
    """
    needle = f"{key}:"
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(needle):
            continue
        after = stripped[len(needle):]
        if not after or after.lstrip() == "":
            return None  # block form, no inline scalar
        return after.strip()
    return None


def _yaml_block_lines(text: str, key: str) -> list[str]:
    """Return de-indented lines under a ``key:`` block mapping.

    Stops at the next top-level (column-0) key.
    """
    lines = text.splitlines()
    result: list[str] = []
    in_block = False
    for line in lines:
        if not in_block:
            if line.rstrip() == f"{key}:":
                in_block = True
            continue
        if not line:
            result.append(line)
            continue
        if not line.startswith((" ", "\t")):
            break
        result.append(line)
    return result


# ---------------------------------------------------------------------------
# AC #1 — Launcher files
# ---------------------------------------------------------------------------


def test_cmd_exists() -> None:
    assert PROMPTPAL_CMD_PATH.is_file()


def test_ps1_exists() -> None:
    assert PROMPTPAL_PS1_PATH.is_file()


def test_cmd_delegates_to_ps1() -> None:
    """The .cmd shim's sole job is to invoke the .ps1 with args."""
    body = PROMPTPAL_CMD_PATH.read_text(encoding="utf-8")
    assert "powershell.exe" in body
    assert "promptpal.ps1" in body
    assert "-ExecutionPolicy Bypass" in body
    assert "%*" in body, "must forward all args"


def test_ps1_uses_wsl_list_quiet_for_detection() -> None:
    body = PROMPTPAL_PS1_PATH.read_text(encoding="utf-8")
    assert "wsl.exe --list --quiet" in body


def test_ps1_emits_install_message_when_ubuntu_missing() -> None:
    body = PROMPTPAL_PS1_PATH.read_text(encoding="utf-8")
    assert WSL_UBUNTU_MISSING_MESSAGE in body


def test_ps1_exits_1_when_ubuntu_missing() -> None:
    """The 'missing Ubuntu' branch must end in `exit 1`."""
    body = PROMPTPAL_PS1_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(WSL_UBUNTU_MISSING_MESSAGE) + r".*?exit\s+1",
        re.DOTALL,
    )
    assert pattern.search(body), "missing-ubuntu branch must call `exit 1`"


# ---------------------------------------------------------------------------
# AC #2 — WSL forwarding
# ---------------------------------------------------------------------------


def test_ps1_forwards_via_wsl_d_ubuntu() -> None:
    body = PROMPTPAL_PS1_PATH.read_text(encoding="utf-8")
    assert WSL_FORWARD_COMMAND in body


def test_ps1_forwards_args_with_splat() -> None:
    """``@args`` is PowerShell's splat operator — forwards argv verbatim."""
    body = PROMPTPAL_PS1_PATH.read_text(encoding="utf-8")
    assert "@args" in body


def test_ps1_propagates_exit_code() -> None:
    """The PowerShell launcher must surface WSL's exit code."""
    body = PROMPTPAL_PS1_PATH.read_text(encoding="utf-8")
    assert "exit $LASTEXITCODE" in body


# ---------------------------------------------------------------------------
# AC #3 — No Anthropic logic in launcher/
# ---------------------------------------------------------------------------


FORBIDDEN_ANTHROPIC_SUBSTRINGS = (
    "ANTHROPIC_API_KEY",
    "anthropic.com",
    "api.anthropic.com",
    "x-api-key",
    "claude-sonnet",
    "claude-opus",
    "claude-haiku",
)


@pytest.mark.parametrize(
    "path",
    [PROMPTPAL_CMD_PATH, PROMPTPAL_PS1_PATH],
    ids=lambda p: p.name,
)
def test_no_anthropic_logic_in_launcher_files(path: Path) -> None:
    body = path.read_text(encoding="utf-8")
    for needle in FORBIDDEN_ANTHROPIC_SUBSTRINGS:
        assert needle not in body, (
            f"launcher {path.name} must not embed Anthropic logic; found {needle!r}"
        )


def test_no_anthropic_logic_in_executable_launcher_files() -> None:
    """Catches stray .py/.json artifacts that might leak Anthropic logic.

    Limited to executable / config files — Markdown docs are allowed to
    explain *what the launcher refuses to do* (and therefore name
    ``ANTHROPIC_API_KEY`` as the secret it never reads).
    """
    extensions = {".cmd", ".ps1", ".bat", ".exe", ".py", ".json", ".yaml", ".yml"}
    for path in LAUNCHER_DIR.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for needle in FORBIDDEN_ANTHROPIC_SUBSTRINGS:
            assert needle not in body, (
                f"launcher file {path.relative_to(REPO_ROOT)} embeds {needle!r}"
            )


# ---------------------------------------------------------------------------
# AC #4 — winget manifest
# ---------------------------------------------------------------------------


def test_manifest_directory_layout() -> None:
    """Publisher/Package/Version layout the winget validator expects."""
    rel = WINGET_MANIFEST_DIR.relative_to(LAUNCHER_DIR / "winget" / "manifests")
    assert rel.parts == ("p", "PromptPal", "PromptPal", WINGET_PACKAGE_VERSION)


def test_version_manifest_fields() -> None:
    text = VERSION_MANIFEST.read_text(encoding="utf-8")
    assert _yaml_scalar(text, "PackageIdentifier") == WINGET_PACKAGE_IDENTIFIER
    assert _yaml_scalar(text, "PackageVersion") == WINGET_PACKAGE_VERSION
    assert _yaml_scalar(text, "DefaultLocale") == "en-US"
    assert _yaml_scalar(text, "ManifestType") == "version"
    assert _yaml_scalar(text, "ManifestVersion") == WINGET_MANIFEST_VERSION


def test_installer_manifest_fields() -> None:
    text = INSTALLER_MANIFEST.read_text(encoding="utf-8")
    assert _yaml_scalar(text, "PackageIdentifier") == WINGET_PACKAGE_IDENTIFIER
    assert _yaml_scalar(text, "PackageVersion") == WINGET_PACKAGE_VERSION
    assert _yaml_scalar(text, "ManifestType") == "installer"
    assert _yaml_scalar(text, "ManifestVersion") == WINGET_MANIFEST_VERSION
    # Portable installer wrapping a zip — winget drops promptpal.cmd on PATH.
    body = text
    assert "InstallerType: zip" in body
    assert "NestedInstallerType: portable" in body
    assert "RelativeFilePath: promptpal.cmd" in body
    assert "PortableCommandAlias: promptpal" in body


def test_locale_manifest_fields() -> None:
    text = LOCALE_MANIFEST.read_text(encoding="utf-8")
    assert _yaml_scalar(text, "PackageIdentifier") == WINGET_PACKAGE_IDENTIFIER
    assert _yaml_scalar(text, "PackageVersion") == WINGET_PACKAGE_VERSION
    assert _yaml_scalar(text, "PackageLocale") == "en-US"
    assert _yaml_scalar(text, "ManifestType") == "defaultLocale"
    assert _yaml_scalar(text, "ManifestVersion") == WINGET_MANIFEST_VERSION
    assert _yaml_scalar(text, "License") == "MIT"
    # Tags block must mention WSL so search surfaces this for the right users.
    tags_block = _yaml_block_lines(text, "Tags")
    assert any("- wsl" in line for line in tags_block), tags_block


def test_manifests_share_identity() -> None:
    """All three manifest files must agree on PackageIdentifier/Version."""
    for manifest in (VERSION_MANIFEST, INSTALLER_MANIFEST, LOCALE_MANIFEST):
        text = manifest.read_text(encoding="utf-8")
        assert _yaml_scalar(text, "PackageIdentifier") == WINGET_PACKAGE_IDENTIFIER
        assert _yaml_scalar(text, "PackageVersion") == WINGET_PACKAGE_VERSION


def test_installer_url_matches_version() -> None:
    """Release URL path segment encodes the package version (catches stale bumps)."""
    text = INSTALLER_MANIFEST.read_text(encoding="utf-8")
    installer_url = _yaml_scalar(text, "InstallerUrl")
    assert installer_url is not None
    assert f"v{WINGET_PACKAGE_VERSION}" in installer_url
    assert f"-{WINGET_PACKAGE_VERSION}.zip" in installer_url


def test_installer_sha256_is_placeholder_or_valid_hex() -> None:
    """SHA must be 64 hex chars; zero-hash placeholder fails publish fast."""
    text = INSTALLER_MANIFEST.read_text(encoding="utf-8")
    sha = _yaml_scalar(text, "InstallerSha256")
    assert sha is not None
    assert re.fullmatch(r"[0-9A-Fa-f]{64}", sha), f"InstallerSha256 not 64-hex: {sha!r}"


# ---------------------------------------------------------------------------
# Drift fences
# ---------------------------------------------------------------------------


def test_wsl_missing_message_pins_constant_ps1() -> None:
    body = PROMPTPAL_PS1_PATH.read_text(encoding="utf-8")
    assert WSL_UBUNTU_MISSING_MESSAGE in body


def test_wsl_missing_message_pins_constant_readme() -> None:
    body = LAUNCHER_README.read_text(encoding="utf-8")
    assert WSL_UBUNTU_MISSING_MESSAGE in body


def test_forward_command_pins_constant() -> None:
    body = PROMPTPAL_PS1_PATH.read_text(encoding="utf-8")
    assert WSL_FORWARD_COMMAND in body


@pytest.mark.parametrize(
    "path",
    [
        PROMPTPAL_CMD_PATH,
        PROMPTPAL_PS1_PATH,
        VERSION_MANIFEST,
        INSTALLER_MANIFEST,
        LOCALE_MANIFEST,
        LAUNCHER_README,
    ],
    ids=lambda p: p.name,
)
def test_launcher_files_use_lf_line_endings(path: Path) -> None:
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"{path.name} contains CRLF; project policy is LF only"


# ---------------------------------------------------------------------------
# Sanity: the canonical module is wired up
# ---------------------------------------------------------------------------


def test_constants_module_paths_resolve() -> None:
    assert PROMPTPAL_CMD_PATH.is_file()
    assert PROMPTPAL_PS1_PATH.is_file()
    assert WINGET_MANIFEST_DIR.is_dir()
