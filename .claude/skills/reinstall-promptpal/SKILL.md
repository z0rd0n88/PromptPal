---
name: reinstall-promptpal
description: "Reinstall PromptPal so repo changes reach the ~/.promptpal/lib snapshot the launcher actually runs. Use when PromptPal code changed (after a merge, git pull, or local edit) but the promptpal command still shows old behavior, or when the user asks to reinstall, rebuild, or update PromptPal."
user-invocable: true
---

# Reinstall PromptPal

## Why this exists

The installed `promptpal` command does **not** run from this repo. The
launcher at `~/.local/bin/promptpal` execs
`python3 ${PROMPTPAL_HOME:-~/.promptpal}/lib/promptpal_main.py`, and
`install.sh` **copies** `core/`, `defaults/`, and `promptpal_main.py` into
that `lib/` directory at install time.

So a `git pull` updates the repo but **not** the snapshot the launcher uses.
After any code change, `install.sh` must re-run for it to take effect.

## Quick start

```bash
bash .claude/skills/reinstall-promptpal/scripts/reinstall.sh
```

The script does `git pull --ff-only` (best-effort) → re-runs `install.sh` →
verifies the installed `lib/` snapshot matches the repo, printing `PASS`/`FAIL`.

## What it does (checklist)

- [ ] Resolve repo root and `${PROMPTPAL_HOME:-~/.promptpal}/lib`
- [ ] Fast-forward the repo — warns and continues if it can't, so local edits
      or a feature branch are still installed from the working tree
- [ ] Run `install.sh` (re-copies `core/` + `defaults/`; preserves your
      `config.json`, `system-prompt.md`, and `history/`)
- [ ] Diff repo `core/` + `promptpal_main.py` against the installed `lib/`
      copy and exit non-zero on any mismatch

## Manual fallback

```bash
git pull --ff-only          # in the repo
bash install.sh             # re-copy into ~/.promptpal/lib
```

## Notes

- Respects `PROMPTPAL_HOME` (defaults to `~/.promptpal`) — the same variable
  `install.sh` honors, so verification targets the real install root.
- Safe to re-run: `install.sh` only overwrites `lib/core` and `lib/defaults`;
  user config and saved sessions are left in place.
- Installing from a worktree/feature branch installs *that* checkout — handy
  for testing an unmerged change before opening a PR.
