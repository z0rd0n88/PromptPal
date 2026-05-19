#!/usr/bin/env bash
# PromptPal uninstaller (US-015, SPEC §11 / P1-INST-07).
#
# Responsibilities
# ----------------
#   1. WSL HOME guard (P1-PLAT-03 / P1-ERR-12): refuse to run when HOME
#      is an NTFS-mounted Windows path so a misrouted invocation can't
#      "uninstall" the wrong tree. Mirrors install.sh and
#      bin/promptpal — text pinned to core.platform.WSL_LAUNCH_FIX_MESSAGE
#      by tests so a future edit on either side fails the drift fence.
#
#   2. Remove the installed binary at $INSTALL_DIR/promptpal
#      (default $HOME/.local/bin/promptpal). Missing binary is treated
#      as "already uninstalled" — informational, not an error.
#
#   3. Prompt the user before deleting $PROMPTPAL_HOME (default
#      $HOME/.promptpal). The prompt is "(Yn)": ENTER / y / Y removes;
#      anything else (including a typo) leaves the directory in place.
#      With --purge, no prompt; the directory is removed unconditionally.
#
#   4. Always exits 0 on clean completion (whether the user kept their
#      data or not). Exit 1 is reserved for the WSL HOME guard.
#
# Environment overrides (match install.sh)
# ----------------------------------------
#   INSTALL_DIR      directory the ``promptpal`` binary was installed to
#                    (default $HOME/.local/bin)
#   PROMPTPAL_HOME   user data + install root parent
#                    (default $HOME/.promptpal)

set -euo pipefail

# ---------------------------------------------------------------------------
# Arg parse — only one flag: --purge (also short-form -p disallowed to
# avoid colliding with future flags).
# ---------------------------------------------------------------------------
purge=false
for arg in "$@"; do
    case "$arg" in
        --purge)
            purge=true
            ;;
        -h|--help)
            cat <<'USAGE'
Usage: uninstall.sh [--purge]

Removes the PromptPal binary at $INSTALL_DIR/promptpal (default
$HOME/.local/bin/promptpal) and prompts before deleting $PROMPTPAL_HOME
(default $HOME/.promptpal). Pass --purge to skip the prompt.

Environment overrides:
  INSTALL_DIR      where the binary lives (default $HOME/.local/bin)
  PROMPTPAL_HOME   user data dir       (default $HOME/.promptpal)
USAGE
            exit 0
            ;;
        *)
            printf 'Error: unknown argument: %s\n' "$arg" >&2
            printf 'Run uninstall.sh --help for usage.\n' >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# WSL HOME guard (P1-PLAT-03 / P1-ERR-12)
# Must mirror core/platform.py:WSL_LAUNCH_FIX_MESSAGE line-by-line.
# ---------------------------------------------------------------------------
case "${HOME:-}" in
    /mnt/c/*|/c/*)
        printf '%s\n' \
            'Warning: HOME appears to be a Windows path.' \
            'For best results, launch from WSL:' \
            '  wsl -d Ubuntu -- promptpal' >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
PROMPTPAL_HOME="${PROMPTPAL_HOME:-$HOME/.promptpal}"
wrapper="$INSTALL_DIR/promptpal"

# ---------------------------------------------------------------------------
# Step 1 — remove the binary (AC #1)
# ---------------------------------------------------------------------------
if [ -e "$wrapper" ] || [ -L "$wrapper" ]; then
    rm -f "$wrapper"
    printf 'Removed binary: %s\n' "$wrapper"
else
    printf 'Binary not found at %s (already uninstalled?).\n' "$wrapper"
fi

# ---------------------------------------------------------------------------
# Step 2 — prompt for ~/.promptpal/ removal (AC #2)
#
# (Yn) semantics:
#   - <ENTER> / y / Y                    → remove
#   - anything else (n, N, typo, EOF)    → keep
# With --purge: skip the prompt and remove unconditionally.
# ---------------------------------------------------------------------------
if [ ! -e "$PROMPTPAL_HOME" ]; then
    printf 'Data directory not found at %s (already removed?).\n' "$PROMPTPAL_HOME"
    exit 0
fi

remove_data=false
if [ "$purge" = true ]; then
    remove_data=true
else
    printf 'Remove %s and all PromptPal history? (Yn) ' "$PROMPTPAL_HOME"
    # `read -r` against a closed stdin returns non-zero. We treat EOF
    # the same as a "keep" answer so an automated re-run without a TTY
    # never blows away user data accidentally. `|| true` is needed under
    # `set -e` so the script doesn't exit on the read failure path.
    answer=""
    read -r answer || answer="__EOF__"
    case "$answer" in
        ""|y|Y)
            remove_data=true
            ;;
        *)
            remove_data=false
            ;;
    esac
fi

if [ "$remove_data" = true ]; then
    rm -rf "$PROMPTPAL_HOME"
    printf 'Removed data directory: %s\n' "$PROMPTPAL_HOME"
else
    printf 'Keeping data directory: %s\n' "$PROMPTPAL_HOME"
fi

exit 0
