---
name: checks
description: "Run PromptPal's local verification gate — pyright type-check plus the full pytest suite — via the project's uv-based runner. Use to verify changes before committing, or when the user asks to run checks, tests, or a type-check."
user-invocable: true
---

# checks

Run the project's verification gate and report the result.

## Commands

```bash
uv run --with pyright --with pytest pyright core tests   # type-check (0 errors = clean)
uv run --with pytest python -m pytest -q                 # full suite (unit + integration)
```

## Notes

- The repo is **stdlib-only** (no venv, no `pip`): `uv run --with …` provides the
  tooling ephemerally. Use `python3`, not `python` (no `python` on PATH). The
  README's bare `python -m pytest` does not work as-is.
- `pyright` needs `--with pytest` so it can resolve test imports — without it you
  get spurious `Import "pytest" could not be resolved` errors.
- For a fast inner loop, scope tests with `python -m pytest tests/unit -q`
  (~2s, no subprocess); the integration suite uses fakes too.

## Report

State PASS/FAIL for each step. On failure, show the relevant pyright lines or the
pytest short-test-summary so the user can act.
