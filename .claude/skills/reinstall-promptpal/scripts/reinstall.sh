#!/usr/bin/env bash
#
# Reinstall PromptPal: sync the repo, re-run install.sh, and verify that the
# ~/.promptpal/lib snapshot the launcher actually runs matches the repo.
#
# Why: the `promptpal` command does NOT run from this repo. install.sh copies
# core/ + defaults/ + promptpal_main.py into $PROMPTPAL_HOME/lib, and the
# launcher execs from there. A bare `git pull` updates the repo but not that
# snapshot, so a code change only takes effect after install.sh re-runs.
#
# Honors PROMPTPAL_HOME (default ~/.promptpal), the same variable install.sh
# uses, so verification points at the real install root.
set -euo pipefail

script_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel)"
prompt_home="${PROMPTPAL_HOME:-$HOME/.promptpal}"
lib="$prompt_home/lib"

printf 'Repo:        %s\n' "$repo_root"
printf 'Install lib: %s\n' "$lib"

# 1. Sync — best-effort. Installing from the current working tree is still
#    valid (and desirable when testing local edits or a feature branch), so a
#    failed pull must warn rather than abort.
printf '\n==> git pull --ff-only\n'
if ! git -C "$repo_root" pull --ff-only; then
    printf 'Warning: pull skipped (offline, diverged, no upstream, or local changes).\n' >&2
    printf '         Installing from the current working tree instead.\n' >&2
fi

# 2. Reinstall — copies core/ + defaults/ + promptpal_main.py into $lib.
printf '\n==> install.sh\n'
bash "$repo_root/install.sh"

# 3. Verify — the step a bare `git pull` skips. Compare the repo source against
#    the installed snapshot; __pycache__/*.pyc are build artifacts, not source.
printf '\n==> verify snapshot matches repo\n'
status=0
diff -r --exclude=__pycache__ --exclude='*.pyc' \
    "$repo_root/core" "$lib/core" >/dev/null || status=1
diff "$repo_root/promptpal_main.py" "$lib/promptpal_main.py" >/dev/null || status=1

if [ "$status" -eq 0 ]; then
    head="$(git -C "$repo_root" rev-parse --short HEAD)"
    printf 'PASS: installed snapshot matches repo (%s).\n' "$head"
else
    printf 'FAIL: installed snapshot differs from the repo:\n' >&2
    diff -r --exclude=__pycache__ --exclude='*.pyc' \
        "$repo_root/core" "$lib/core" >&2 || true
    diff "$repo_root/promptpal_main.py" "$lib/promptpal_main.py" >&2 || true
    exit 1
fi
