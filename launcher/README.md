# PromptPal Windows launcher (D-5 / P1-INST-06)

This directory contains the Windows distribution surface for PromptPal.
Windows is reached **only** via WSL Ubuntu (NFR-12); the files here are
a thin shim, not a port.

## Files

| File | Purpose |
|---|---|
| `promptpal.cmd` | Entry point winget aliases to `promptpal` on PATH. Delegates to `promptpal.ps1`. |
| `promptpal.ps1` | WSL Ubuntu detection + arg forwarding via `wsl -d Ubuntu -- promptpal "$@"`. |
| `winget/manifests/p/PromptPal/PromptPal/0.1.0/` | winget multi-file manifest (version + installer + en-US locale). |

## Contract

- **AC-WINGET-01**: when WSL Ubuntu is not installed, the launcher
  prints `PromptPal requires WSL Ubuntu. Run: wsl --install -d Ubuntu`
  to stderr and exits 1.
- **AC-WINGET-02**: when WSL Ubuntu is installed *and* PromptPal is
  installed inside it (via `install.sh` from the upstream repo),
  `promptpal "test"` from PowerShell forwards into WSL and returns the
  improved prompt on PowerShell stdout.
- **No Anthropic logic**: the launcher never reads `ANTHROPIC_API_KEY`,
  never calls the Anthropic API, never imports any Anthropic module.
  All prompt-improvement logic lives in the WSL-side `core/` package.

## Release process

1. Bump `PackageVersion` in all three manifest files (must stay in
   lockstep — `winget validate` rejects mismatches).
2. Zip `promptpal.cmd` and `promptpal.ps1` into
   `promptpal-launcher-<version>.zip` and upload to the GitHub release.
3. Replace `InstallerUrl` and `InstallerSha256` in the installer
   manifest with the actual release artifact values. The repo ships a
   zero-hash placeholder so an accidental publish without rewrite
   fails fast.
4. Run `winget validate launcher/winget/manifests/p/PromptPal/PromptPal/<version>/`
   from a Windows host and confirm the validator reports the manifest
   passes (Phase 1 §15 release checklist item).
5. Submit to the winget-pkgs community repo per
   <https://learn.microsoft.com/en-us/windows/package-manager/package/>.
