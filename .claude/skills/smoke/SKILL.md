---
name: smoke
description: "Smoke-test the installed promptpal binary's launcher wiring (launcher → promptpal_main.py → core) via `promptpal --help`, which needs no backend. Use after install/reinstall, or when the user asks to smoke-test or sanity-check the installed CLI."
user-invocable: true
---

# smoke

Verify the **installed** `promptpal` command actually runs — coverage the test
suite skips, since unit/integration tests use fake backends and never exercise
the real binary.

## Quick start

```bash
bash .claude/skills/smoke/scripts/smoke.sh
```

It confirms `promptpal` is on PATH and that `promptpal --help` runs (exit 0),
which exercises the full launcher → `promptpal_main.py` → `core` chain without
calling any backend or the Anthropic API.

## Notes

- A *full* improve cycle isn't smoke-tested here because it requires a live
  backend (claude CLI or API key). `--help` is the no-cost wiring check.
- If it fails with "not on PATH" or a launcher error, run `/reinstall-promptpal`
  and re-run.
- Complements `/checks` (source-level pyright + pytest) and CI — this one checks
  the *installed artifact*, not the repo.
