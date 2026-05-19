# PromptPal Windows launcher (D-5 / P1-INST-06).
#
# Forwards arguments to the WSL Ubuntu `promptpal` binary. Contains no
# Anthropic logic and no API keys — Windows is reached via WSL only
# (NFR-12).
#
# Exits with the wrapped command's exit code on success, 1 when WSL
# Ubuntu is not installed (printing the install instruction to stderr).

$ErrorActionPreference = 'Stop'

function Write-StdErr {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Test-WslUbuntu {
    # `wsl.exe --list --quiet` emits UTF-16-LE; PowerShell handles the
    # decoding when we capture via the call operator. Returns $true when
    # any installed distro name matches /^Ubuntu/ (e.g. Ubuntu,
    # Ubuntu-22.04, Ubuntu-24.04).
    try {
        $output = & wsl.exe --list --quiet 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    foreach ($line in $output) {
        if ($line -match '^\s*Ubuntu') {
            return $true
        }
    }
    return $false
}

if (-not (Test-WslUbuntu)) {
    Write-StdErr 'PromptPal requires WSL Ubuntu. Run: wsl --install -d Ubuntu'
    exit 1
}

& wsl.exe -d Ubuntu -- promptpal @args
exit $LASTEXITCODE
