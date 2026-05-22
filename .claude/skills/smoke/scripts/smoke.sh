#!/usr/bin/env bash
#
# Smoke-test the INSTALLED promptpal binary's launcher wiring
# (launcher -> promptpal_main.py -> core). The unit/integration suites use fake
# backends and never exercise the real installed command, so this catches
# install/launcher breakage they can't. `--help` needs no backend or API call.
set -euo pipefail

if ! command -v promptpal >/dev/null 2>&1; then
    printf 'FAIL: promptpal not on PATH — run /reinstall-promptpal first.\n' >&2
    exit 1
fi
printf 'binary: %s\n' "$(command -v promptpal)"

out="$(promptpal --help 2>&1)" || {
    printf 'FAIL: "promptpal --help" exited non-zero:\n' >&2
    printf '%s\n' "$out" >&2
    exit 1
}

printf 'PASS: launcher wiring OK ("promptpal --help" ran). First lines:\n'
printf '%s\n' "$out" | head -5
