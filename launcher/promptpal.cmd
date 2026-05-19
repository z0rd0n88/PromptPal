@echo off
REM PromptPal Windows launcher entry point (D-5 / P1-INST-06).
REM
REM This thin shim is what winget aliases to `promptpal` on PATH. It
REM delegates to promptpal.ps1 which performs the WSL Ubuntu detection
REM and forwards args via `wsl -d Ubuntu -- promptpal "$@"`.
REM
REM Contains no Anthropic logic. Windows is reached via WSL only.

setlocal
set "PROMPTPAL_LAUNCHER_DIR=%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PROMPTPAL_LAUNCHER_DIR%promptpal.ps1" %*
exit /b %ERRORLEVEL%
