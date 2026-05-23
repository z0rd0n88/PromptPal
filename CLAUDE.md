# CLAUDE.md

## Architecture constraints
- Core is **stdlib-only by design** (PRD D-1/D-2): no third-party imports, no
  `requirements.txt` / `pyproject.toml`. Don't add dependencies — port or inline instead.
- Spec is `PRD-PHASE1.md`. Commits use `feat: US-NNN`; tests and docstrings pin requirement
  IDs like `P1-<AREA>-NN` (e.g. `P1-LOOP-01`) — preserve them when editing.

## claude-cli backend (the fragile part)
- Before changing how `claude` is invoked, read the `core/cli_backend.py` **module docstring**
  — it records hard-won `--input-format=stream-json` quirks: `--bare` breaks OAuth, `--verbose`
  is required, failures land on stdout with empty stderr, and message `content` must be a block
  array (`[{"type":"text","text":…}]`), never a bare string, or multi-turn input crashes the parser.

## Tests
- Run with `uv run --with pytest python -m pytest` — the repo is stdlib-only (no venv, no `pip`;
  pytest isn't installed system-wide). Use `python3`, not `python` (there's no `python` on PATH).
  The README's bare `python -m pytest` does not work as-is.
- `tests/unit` is fast (fake runners, no subprocess); `tests/integration` also uses fakes.
  `tests/integration/test_winget_launcher.py` skips off-Windows.
- Type-check with `uv run --with pyright --with pytest pyright core tests` — pyright needs
  `--with pytest` too, or it false-flags `Import "pytest" could not be resolved`.
- `/checks` runs both (pyright + full pytest); CI (`.github/workflows/ci.yml`) runs them on every PR.

## Reinstall after code changes
- The `promptpal` command runs from a **copied** snapshot at `${PROMPTPAL_HOME:-~/.promptpal}/lib`,
  not from this repo. A `git pull` does **not** take effect until `install.sh` re-runs.
- Use `/reinstall-promptpal` (or `bash .claude/skills/reinstall-promptpal/scripts/reinstall.sh`)
  to pull → re-run `install.sh` → verify the installed snapshot matches the repo.

## Project Architecture

See [`ARCH.md`](./ARCH.md) for the full, auto-maintained file-tree map and component overview — the primary entry point for orienting in this repo. The tree refreshes automatically on every commit via `.githooks/pre-commit`; refresh manually with `python3 .githooks/gen_arch.py`. After cloning, activate the hook once with `git config core.hooksPath .githooks`. Use the `code-explorer` agent for deeper tracing.
