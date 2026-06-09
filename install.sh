#!/usr/bin/env bash
# PromptPal installer (US-013, SPEC §10 / P1-INST-01..06).
#
# Responsibilities
# ----------------
#   1. WSL HOME guard (P1-PLAT-03 / P1-ERR-12): refuse to install when the
#      installer's own HOME is an NTFS-mounted Windows path. The fix
#      message text mirrors core.platform.WSL_LAUNCH_FIX_MESSAGE so a
#      test pins the two together (see tests/unit/test_install.py).
#
#   2. Lay down the install root at ${PROMPTPAL_HOME:-~/.promptpal}/lib/
#      with copies of core/ and defaults/. This directory is *managed* —
#      re-running the installer always overwrites it.
#
#   3. Seed user-owned files exactly once. Per AC #4, ~/.promptpal/
#      config.json and system-prompt.md are NEVER overwritten on a
#      subsequent install; history/ is created empty.
#
#   4. Generate a fresh launcher at ${INSTALL_DIR:-~/.local/bin}/promptpal
#      that hardcodes the absolute PROMPTPAL_LIB and execs
#      ``python3 -m core.main``. The generated launcher carries its own
#      copy of the WSL HOME guard so the fix message reaches the user
#      before Python imports anything.
#
#   5. Post-install backend check (AC #5, P1-INST-05). Prints which
#      backend(s) are configured (Claude CLI on PATH and/or
#      ANTHROPIC_API_KEY set); if neither, prints the two-option setup
#      hint from core.backend.NoBackendError.MESSAGE. Always exits 0 —
#      a fresh machine with no backend yet is a valid state.
#
# Required tooling
# ----------------
# bash, coreutils (mkdir/cp/rm/chmod), python3, find. No curl/wget at
# install time — those are listed in the SPEC for the one-liner that
# fetches install.sh from GitHub before this script runs.
#
# Environment overrides
# ---------------------
#   INSTALL_DIR      directory the ``promptpal`` binary is placed in
#                    (default $HOME/.local/bin)
#   PROMPTPAL_HOME   user data + install root parent
#                    (default $HOME/.promptpal)

set -euo pipefail

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

script_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$script_dir"

# Sanity-check source layout — surface a clear message rather than letting
# cp explode mid-flight if install.sh is invoked from outside the repo.
for required in \
    "$repo_root/core" \
    "$repo_root/defaults/config.json" \
    "$repo_root/core/system_prompt.txt" \
    "$repo_root/promptpal_main.py"; do
    if [ ! -e "$required" ]; then
        printf 'Error: required source path missing: %s\n' "$required" >&2
        printf 'Run install.sh from the PromptPal repository root.\n' >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Lay down user data dir + managed install root (AC #4)
# ---------------------------------------------------------------------------
mkdir -p "$PROMPTPAL_HOME/history"
mkdir -p "$PROMPTPAL_HOME/lib"

# Managed install root — always replaced so upgrades pick up new code.
rm -rf "$PROMPTPAL_HOME/lib/core" "$PROMPTPAL_HOME/lib/defaults"
cp -R "$repo_root/core" "$PROMPTPAL_HOME/lib/core"
cp -R "$repo_root/defaults" "$PROMPTPAL_HOME/lib/defaults"
# Path-invoked bootstrap (sibling of core/ so ``import core`` resolves to
# the managed lib, never a stray core/ in the user's cwd).
cp "$repo_root/promptpal_main.py" "$PROMPTPAL_HOME/lib/promptpal_main.py"

# Strip __pycache__/ that may have crept in from the dev checkout — keep
# the install layout clean so the first run rebuilds bytecode for the
# user's Python version.
find "$PROMPTPAL_HOME/lib" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Seed user-owned files (AC #4: existing files never overwritten).
if [ ! -e "$PROMPTPAL_HOME/config.json" ]; then
    cp "$repo_root/defaults/config.json" "$PROMPTPAL_HOME/config.json"
fi
if [ ! -e "$PROMPTPAL_HOME/system-prompt.md" ]; then
    cp "$repo_root/core/system_prompt.txt" "$PROMPTPAL_HOME/system-prompt.md"
fi

# ---------------------------------------------------------------------------
# Generate launcher at $INSTALL_DIR/promptpal (AC #3)
# The launcher hardcodes the absolute PROMPTPAL_LIB so it works from any
# cwd and survives the repo being deleted after install.
# ---------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
wrapper="$INSTALL_DIR/promptpal"
promptpal_lib="$PROMPTPAL_HOME/lib"

# Heredoc rules: unquoted delimiter expands `$promptpal_lib` (what we
# want — bake the absolute path into the launcher). All other vars and
# the literal `$@` are escaped with `\` so they survive into the output.
cat > "$wrapper" <<WRAPPER_EOF
#!/usr/bin/env bash
# PromptPal launcher (generated by install.sh — do not edit by hand).
set -euo pipefail

case "\${HOME:-}" in
    /mnt/c/*|/c/*)
        printf 'Warning: HOME appears to be a Windows path.\n' >&2
        printf 'For best results, launch from WSL:\n' >&2
        printf '  wsl -d Ubuntu -- promptpal\n' >&2
        exit 1
        ;;
esac

PROMPTPAL_LIB="$promptpal_lib"
exec python3 "\${PROMPTPAL_LIB}/promptpal_main.py" "\$@"
WRAPPER_EOF
chmod +x "$wrapper"

# L12 (issue #30): single multi-arg printf for consistency with the
# bin/promptpal launcher and uninstall.sh idioms — same shape across
# the three scripts means less drift risk on a future edit.
printf '%s\n' \
    'PromptPal installed:' \
    "  binary:  $wrapper" \
    "  data:    $PROMPTPAL_HOME" \
    "  lib:     $promptpal_lib"

# ---------------------------------------------------------------------------
# PATH check (warn, never fail — AC #3 "warns if not on PATH")
# ---------------------------------------------------------------------------
case ":${PATH:-}:" in
    *:"$INSTALL_DIR":*)
        ;;
    *)
        printf '\n' >&2
        printf 'Warning: %s is not on your PATH.\n' "$INSTALL_DIR" >&2
        printf 'Add this line to ~/.bashrc, ~/.zshrc, or your shell rc file:\n' >&2
        # shellcheck disable=SC2016  # literal $PATH is the message we want the user to copy
        printf '  export PATH="%s:$PATH"\n' "$INSTALL_DIR" >&2
        ;;
esac

# ---------------------------------------------------------------------------
# Backend check (AC #5)
# Informational only. Always exits 0 so a fresh-from-disk machine can run
# the installer first and configure a backend afterwards.
# ---------------------------------------------------------------------------
have_cli=false
have_key=false
if command -v claude >/dev/null 2>&1; then
    have_cli=true
fi
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    have_key=true
fi

printf '\n'
printf 'Backend check:\n'
if [ "$have_cli" = true ]; then
    printf '  - Claude CLI detected on PATH\n'
fi
if [ "$have_key" = true ]; then
    printf '  - ANTHROPIC_API_KEY is set\n'
fi
if [ "$have_cli" = false ] && [ "$have_key" = false ]; then
    # Mirrors core.backend.NoBackendError.MESSAGE — tests pin both halves
    # so accidental drift surfaces in CI.
    printf '  No backend configured. Set up one of the following:\n'
    # shellcheck disable=SC2016  # backticks here are literal markdown-style in user-facing copy
    printf '    Option 1 (Claude CLI): Install Claude Code and run `claude auth login`\n'
    printf '    Option 2 (API key):    export ANTHROPIC_API_KEY="sk-ant-..."\n'
fi

exit 0
