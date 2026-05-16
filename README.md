# PromptPal

A local CLI tool that improves prompts using Claude. Paste a rough prompt, get a refined one back, iterate with feedback, and accept when it's right.

```
$ promptpal "write a sorting function"

Improved:
  Write a Python function that accepts a list of integers and returns a new
  list sorted in ascending order using the merge sort algorithm. Include
  docstring, type hints, and a brief complexity note.

[a]ccept  [i]terate  [d]iscard  [r]aw  [c]opy
```

## Status

**Pre-implementation, fully specified.** PRD and SPEC are complete; all ten §16 open questions are resolved. Code has not been written yet — the next step is committing the resolutions branch and starting Phase 1.0 (repo bootstrap).

- [`PRD.md`](PRD.md) — product requirements (+ [`PRD-UPDATE-001`](PRD-UPDATE-001-wsl-and-claude-cli.md) for WSL & Claude CLI)
- [`SPEC.md`](SPEC.md) — full technical specification
- [`DEV-TRACKER.md`](DEV-TRACKER.md) — phased implementation tracker, decisions log, next actions
- [`CLAUDE.md`](CLAUDE.md) — architecture guidance for AI-assisted contributors

## Planned Features

- Two backends: Anthropic API key or Claude CLI (auto-detected, no config required)
- Multi-turn refinement — iterate with feedback, full context preserved (CLI backend uses native `stream-json` multi-turn — no prompt flattening)
- Session history with search and replay
- WSL2 first-class support (Windows clipboard via `clip.exe`, HOME guard against `/mnt/c/...` regressions)
- `--quiet` / pipe-friendly mode for scripting
- Phase 2: Tauri desktop GUI sharing the same `~/.promptpal/` store

## Planned Distribution

Phase 1 ships **Windows-first via winget**, with WSL Ubuntu as the supported runtime:

- Signed `.msi` / `.exe` installer published through winget
- Windows-side launcher shells into `wsl -d Ubuntu -- promptpal`
- WSL payload installs `bin/promptpal` + `core/` under the user's WSL home
- Homebrew, apt, Snap deferred until post-Windows launch

Requires `python3` (already present on WSL Ubuntu). No other runtime dependencies — JSON handling is pure Python stdlib, no `jq`.

## Backend Setup

PromptPal needs one of:

```bash
# Option 1 — Claude CLI (preferred if already installed)
claude auth login

# Option 2 — Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."
```

Run `promptpal --status` to confirm which backend is active. Pass `--backend api|cli` to force one; the choice persists to `~/.promptpal/config.json` until you run `--backend auto` to restore detection.
