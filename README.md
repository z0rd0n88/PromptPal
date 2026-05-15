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

**Pre-implementation.** The PRD and technical specification are complete. Code has not been written yet.

- [`PRD.md`](PRD.md) — product requirements
- [`SPEC.md`](SPEC.md) — full technical specification (architecture, schemas, module designs)

## Planned Features

- Two backends: Anthropic API key or Claude CLI (auto-detected, no config required)
- Multi-turn refinement — iterate with feedback, full context preserved
- Session history with search and replay
- WSL2 first-class support (Windows clipboard via `clip.exe`, HOME guard)
- `--quiet` / pipe-friendly mode for scripting
- Phase 2: Tauri desktop GUI sharing the same `~/.promptpal/` store

## Planned Installation

```bash
curl -fsSL https://raw.githubusercontent.com/z0rd0n88/PromptPal/main/install.sh | bash
```

Requires `python3`. No other dependencies. Works on Linux, macOS, and WSL2.

## Backend Setup

PromptPal needs one of:

```bash
# Option 1 — Claude CLI (preferred if already installed)
claude auth login

# Option 2 — Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."
```

Run `promptpal --status` to confirm which backend is active.
