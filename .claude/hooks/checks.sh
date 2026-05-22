#!/usr/bin/env bash
#
# PostToolUse advisory check for PromptPal.
#
# After an Edit/Write/MultiEdit to a Python source under core/ or tests/, run
# pyright + the fast unit suite and print a concise PASS/FAIL summary.
#
# ADVISORY BY DESIGN: this hook always exits 0, so it never blocks a tool call.
# That's deliberate — the project follows TDD, and a blocking test hook would
# fight the RED phase (a test you just wrote and expect to fail) and mid-refactor
# states. The summary lands on stderr so regressions are visible without
# sabotaging the red-green loop. Comprehensive gating lives in CI and /checks.
#
# pyright needs the test deps importable to analyze test files, hence
# `--with pyright --with pytest`. First run is slow (uv fetches pyright); cached after.
set -euo pipefail

root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

# Extract the edited file path from the hook's stdin JSON (stdlib only — no jq).
file_path="$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
ti = data.get("tool_input") or {}
print(ti.get("file_path") or "")
' 2>/dev/null || true)"

# Only act on .py files under core/ or tests/.
case "$file_path" in
    *.py) ;;
    *) exit 0 ;;
esac
case "$file_path" in
    "$root"/core/*|"$root"/tests/*) ;;
    *) exit 0 ;;
esac

cd "$root"

pyright_out="$(mktemp)"
pytest_out="$(mktemp)"
trap 'rm -f "$pyright_out" "$pytest_out"' EXIT

fails=""
if ! uv run --with pyright --with pytest pyright core tests >"$pyright_out" 2>&1; then
    fails="${fails} pyright"
fi
if ! uv run --with pytest python -m pytest tests/unit -q >"$pytest_out" 2>&1; then
    fails="${fails} pytest"
fi

if [ -n "$fails" ]; then
    printf '\n⚠ promptpal checks FAILED:%s (advisory — not blocking)\n' "$fails" >&2
    tail -15 "$pyright_out" >&2 || true
    tail -15 "$pytest_out" >&2 || true
else
    printf '✓ promptpal checks passed (pyright + unit tests)\n' >&2
fi
exit 0
