# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

PromptPal is currently in the **specification phase** — only PRD and SPEC documents exist. No code has been written yet. Before implementing anything, read `SPEC.md` in full; it is the authoritative source for architecture, data schemas, file layout, and design decisions.

## Architecture Overview

Phase 1 is a bash entrypoint (`bin/promptpal`) that delegates immediately to a Python core (`core/`). The bash layer only handles PATH integration, HOME guard, and bootstrap; all logic lives in Python.

### Backend Abstraction

The central design decision is a `Backend` ABC (`core/backend.py`) with two concrete implementations:

- `core/api_backend.py` — Anthropic Messages API over HTTP
- `core/cli_backend.py` — `claude -p` subprocess with multi-turn prompt flattening

Auto-detection at startup: Claude CLI on PATH → preferred; `ANTHROPIC_API_KEY` in env → fallback; neither → exit 1 with instructions for both options. Override via `--backend` flag or `preferred_backend` in config.

### Data Flow

```
bin/promptpal (bash) → core/cli.py (argparse) → core/improve.py (pipeline)
  → resolve_backend() → Backend.complete(system, messages)
  → core/history.py (atomic write) → core/diff.py (display)
```

The `messages` list is stateful across refinement turns — the full array is sent on every API call. No truncation in Phase 1.

### WSL Support

`core/platform.py` runs at startup before anything else. It detects WSL via `/proc/sys/kernel/osrelease`, guards against `HOME=/mnt/c/...` regressions (exits 1 with a clear fix), and selects the clipboard provider (`xclip` → `xsel` → `pbcopy` → `clip.exe` → none).

### Persistence

`~/.promptpal/history/` holds one JSON file per session plus an `index.json`. All writes use the atomic `tempfile.mkstemp` → `os.rename` pattern. The index is the only shared mutable state between CLI and GUI (Phase 2).

## Key Files

| File | Purpose |
|------|---------|
| `SPEC.md` | Authoritative technical spec — architecture, schemas, all module designs |
| `PRD.md` | Product requirements; amends tracked in `PRD-UPDATE-001-wsl-and-claude-cli.md` |

## PRD Amendment Convention

The PRD uses an immutable amendment pattern: `PRD.md` is never rewritten; changes go in numbered amendment files (`PRD-UPDATE-NNN-<topic>.md`) referenced from the Amendments table in `PRD.md`. The SPEC does **not** follow this pattern — it is a living document edited in place.

## Open Questions (from SPEC.md §16)

Before implementing, resolve at minimum:
- **S-2 (blocking):** Verify `claude --model <id>` accepts the same model ID strings as the API before wiring up `--model` passthrough in `CliBackend`.
- **S-1:** Check whether `claude` CLI supports native multi-turn flags; if yes, replace `_build_prompt` flattening in `cli_backend.py`.
