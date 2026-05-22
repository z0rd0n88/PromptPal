#!/usr/bin/env bash
#
# PreToolUse guard: block file-tool writes into the LIVE PromptPal data dir
# (${PROMPTPAL_HOME:-~/.promptpal}) — real session history, config.json, and the
# install snapshot the launcher runs. Editing those by hand during dev can
# corrupt actual user state.
#
# Exit 2 blocks the tool call and feeds the message back to Claude. install.sh
# and /reinstall-promptpal write via Bash (not the Edit/Write tools), so they
# are unaffected by this guard.
set -euo pipefail

home_data="${PROMPTPAL_HOME:-$HOME/.promptpal}"

file_path="$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
ti = data.get("tool_input") or {}
print(ti.get("file_path") or "")
' 2>/dev/null || true)"

if [ -z "$file_path" ]; then
    exit 0
fi

# Resolve to absolute, normalized paths and test containment robustly.
blocked="$(python3 - "$file_path" "$home_data" <<'PY'
import os, sys
target = os.path.abspath(os.path.expanduser(sys.argv[1]))
base = os.path.abspath(os.path.expanduser(sys.argv[2]))
print("yes" if target == base or target.startswith(base + os.sep) else "no")
PY
)"

if [ "$blocked" = "yes" ]; then
    printf 'Blocked: refusing to edit live PromptPal data under %s.\n' "$home_data" >&2
    printf 'That dir holds real session history, config.json, and the install snapshot.\n' >&2
    printf 'To change installed code, edit the repo and run /reinstall-promptpal instead.\n' >&2
    exit 2
fi
exit 0
