# PromptPal — Product Requirements Document

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Problem Statement](#problem-statement)
3. [Goals and Non-Goals](#goals-and-non-goals)
4. [User Personas](#user-personas)
5. [Feature Requirements](#feature-requirements)
6. [Phase Roadmap](#phase-roadmap)
7. [Success Metrics](#success-metrics)
8. [Constraints and Assumptions](#constraints-and-assumptions)
9. [Open Questions](#open-questions)

---

## Amendments

| # | File | Summary | Date |
|---|------|---------|------|
| 001 | [PRD-UPDATE-001-wsl-and-claude-cli.md](./PRD-UPDATE-001-wsl-and-claude-cli.md) | WSL2 support (P0 for Phase 1) and Claude CLI as an alternative backend to raw API key | 2026-05-15 |

---

## Executive Summary

PromptPal is a standalone prompt-improvement tool that accepts a raw user prompt and returns a refined, production-quality version by routing it through the Claude API using the Prompt Builder methodology. The tool ships first as a CLI bash script installable on the system PATH, then evolves into a local desktop GUI application. Both interfaces share the same core improvement pipeline, configuration store, and history database at `~/.promptpal/`.

The core value proposition: **compress the gap between "what a user types" and "what Claude needs to perform well"** — without requiring the user to understand prompt engineering or open a Claude session.

---

## Problem Statement

Users writing prompts for Claude (and LLMs generally) frequently produce underspecified, ambiguous, or structurally weak prompts. The result is mediocre outputs that require multiple retries, manual refinement, or domain knowledge the user doesn't have. Existing solutions require staying inside a Claude session, have no history, and provide no iterative refinement workflow.

PromptPal solves this by:
- Running prompt improvement as an offline, standalone tool that integrates into any workflow
- Providing a structured refinement loop (accept / iterate / discard) so users can steer improvement
- Persisting prompt history for reuse, search, and export
- Eventually offering a native GUI that makes the tool accessible to non-terminal users

---

## Goals and Non-Goals

### Goals

- Ship a working CLI tool (`promptpal`) by end of Phase 1
- Enable single-command prompt improvement with no required setup beyond an API key
- Support iterative multi-turn refinement without losing conversation context
- Persist history across sessions with search and export
- Produce a clean upgrade path to a GUI in Phase 2

### Non-Goals (Phase 1)

- No team sync or shared prompt library (Phase 3)
- No GUI (Phase 2)
- No MCP server mode (Phase 2+)
- No batch processing at launch (roadmap item)
- No cloud hosting or SaaS — local-only

---

## User Personas

### Power User / Developer
Uses CLI tools daily. Wants to pipe prompts through `promptpal` in shell scripts and CI pipelines. Values `--quiet` mode, JSON output, and piped input. Cares about speed and composability.

### Prompt Engineer
Iteratively crafts prompts for production systems. Needs history, comparison mode, and named sessions to manage a library of tested prompts. Wants diff view to see exactly what changed.

### Non-Technical User (Phase 2)
Occasional LLM user who writes prompts in natural language. Doesn't use a terminal. Needs the GUI to reduce friction. Benefits most from the first-run guided setup and templates.

---

## Feature Requirements

### Phase 1 — CLI

#### Core Pipeline

| ID | Requirement | Priority |
|----|-------------|----------|
| P1-01 | Accept raw prompt via CLI arg: `promptpal "your prompt"` | MUST |
| P1-02 | Accept raw prompt via stdin: `echo "prompt" \| promptpal` | MUST |
| P1-03 | Accept raw prompt via interactive mode: `promptpal` (no args) | MUST |
| P1-04 | Load Prompt Builder system prompt from `~/.promptpal/system-prompt.md` | MUST |
| P1-05 | Send prompt to Claude API and return improved version | MUST |
| P1-06 | Stream API response tokens to terminal (interactive mode) | SHOULD |
| P1-07 | Display unified diff between original and improved (prompts > 3 lines) | MUST |
| P1-08 | Show progress spinner during API calls | SHOULD |

#### Refinement Loop

| ID | Requirement | Priority |
|----|-------------|----------|
| P1-09 | After displaying improved prompt, enter interactive loop: [a]ccept / [i]terate / [d]iscard / [r]aw / [c]opy | MUST |
| P1-10 | On iterate, accept user feedback and run another improvement pass preserving full conversation context | MUST |
| P1-11 | Support minimum 3 successive improvement passes per session | MUST |
| P1-12 | On accept, write session to history and optionally copy to clipboard | MUST |
| P1-13 | On discard, exit without saving | MUST |

#### CLI Flags

| Flag | Description | Priority |
|------|-------------|----------|
| `--model MODEL` | Override Claude model (default: `claude-sonnet-4-6`) | MUST |
| `--iterations N` | Run N auto improvement passes before presenting output | SHOULD |
| `--no-history` | Skip writing session to history | MUST |
| `--copy` | Auto-copy final output to clipboard | SHOULD |
| `--show-history` | Display paginated history list | MUST |
| `--replay SESSION_ID` | Load past session for further refinement | SHOULD |
| `--system-prompt FILE` | Use custom system prompt file | SHOULD |
| `--output FORMAT` | Output format: plain / json / markdown | MUST |
| `--quiet` | Output only improved prompt text (pipe-friendly) | MUST |
| `--search KEYWORD` | Search history by keyword | SHOULD |
| `--export SESSION_ID` | Export session to stdout | SHOULD |
| `--name LABEL` | Assign a human-readable label to the session | NICE |
| `--update-system-prompt` | Fetch latest system prompt from configured URL | NICE |
| `--uninstall` | Remove tool and config | SHOULD |

#### History and Persistence

| ID | Requirement | Priority |
|----|-------------|----------|
| P1-14 | Store sessions at `~/.promptpal/history/SESSION_ID.json` | MUST |
| P1-15 | Maintain session index at `~/.promptpal/history/index.json` | MUST |
| P1-16 | Each session record: session_id, created_at, model, original_prompt, turns[], final_prompt, status | MUST |
| P1-17 | Use atomic file writes (write temp, then rename) to prevent corruption | MUST |
| P1-18 | Log token usage to `~/.promptpal/usage.log` per turn | SHOULD |
| P1-19 | Support `max_history_entries` config with oldest-first eviction | SHOULD |

#### Configuration

| ID | Requirement | Priority |
|----|-------------|----------|
| P1-20 | Read config from `~/.promptpal/config.json` | MUST |
| P1-21 | Config keys: default_model, default_iterations, auto_copy, show_diff, system_prompt_path, history_enabled, max_history_entries | MUST |
| P1-22 | Read `ANTHROPIC_API_KEY` from environment; fail with actionable error if absent | MUST |
| P1-23 | Never log API key to history, usage log, or any file | MUST |

#### Installation

| ID | Requirement | Priority |
|----|-------------|----------|
| P1-24 | Installable via curl one-liner | MUST |
| P1-25 | Installer places binary at `/usr/local/bin/promptpal` or `~/.local/bin/promptpal` | MUST |
| P1-26 | Installer creates `~/.promptpal/` with default config and system prompt | MUST |
| P1-27 | Installer checks dependencies: `curl`/`wget`, `jq` or `python3`/`node` for JSON | MUST |
| P1-28 | Works on macOS and Linux without additional runtime installs | MUST |
| P1-29 | First-run guided setup if `~/.promptpal/` does not exist | SHOULD |

#### API Integration

| ID | Requirement | Priority |
|----|-------------|----------|
| P1-30 | Use Anthropic Messages API (`/v1/messages`) | MUST |
| P1-31 | System prompt as `system` parameter (not injected into user turn) | MUST |
| P1-32 | Multi-turn: stateful messages array, append per iteration | MUST |
| P1-33 | Handle rate limits with `retry-after` header backoff | MUST |
| P1-34 | Handle network errors: retry once, then fail with clear message | MUST |
| P1-35 | Handle auth errors: fail immediately with actionable message | MUST |

---

### Phase 2 — GUI Application

| ID | Requirement | Priority |
|----|-------------|----------|
| P2-01 | Local desktop app (Tauri preferred; Electron acceptable) | MUST |
| P2-02 | Split-pane layout: raw input left, improved output right | MUST |
| P2-03 | Diff view toggle | MUST |
| P2-04 | Full conversation thread view for multi-turn sessions | MUST |
| P2-05 | History sidebar with search | MUST |
| P2-06 | Drag-and-drop text file input | SHOULD |
| P2-07 | Preferences panel mirroring CLI config | MUST |
| P2-08 | Native installers: .deb/.AppImage (Linux), .dmg (macOS), .exe (Windows) | SHOULD |
| P2-09 | Shared `~/.promptpal/` store with CLI (no file-lock conflicts) | MUST |

---

### Phase 3 — Roadmap Items

- Team sync: share history and profiles to S3 / GitHub Gist / PromptPal server
- MCP server mode: expose as MCP tool for Claude Code and compatible clients
- Batch mode: process a file of prompts unattended
- Comparison mode: run same prompt through two models/profiles side by side
- Prompt templates library: 10-15 built-in templates for common use cases
- Git integration: commit accepted prompts to a versioned prompt library
- Shell completion: bash, zsh, fish tab-completion scripts

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Time to first improved prompt | < 10 seconds on standard broadband |
| Refinement passes per session (P50) | ≥ 2 |
| History search correctness | Returns relevant session in top 3 results |
| CLI install success rate on macOS/Ubuntu 22+ | 95%+ |
| CLI works without extra runtime installs | bash + curl + (python3 or node) only |

---

## Constraints and Assumptions

- Requires `ANTHROPIC_API_KEY` in environment — no bundled key
- Phase 1 targets macOS and Linux; Windows via WSL is acceptable but not primary
- History store is local-only in Phase 1 — no sync, no backup
- The Prompt Builder system prompt is the same one used in the Claude Code `prompt-builder` agent
- Tauri for GUI requires Rust toolchain at build time (not at runtime — binary is self-contained)
- The tool is single-user; no multi-user or permission model is needed in Phase 1–2

---

## Open Questions

1. Should the CLI be implemented in bash + python3, or go straight to a Go/Rust binary for better streaming support?
2. Should `jq` be a hard dependency or should the tool embed a minimal JSON parser?
3. What is the canonical source URL for the Prompt Builder system prompt used in `--update-system-prompt`?
4. Should the GUI (Phase 2) be built before or after the CLI reaches feature-complete status?
5. Is there a target distribution channel for the GUI (Homebrew, apt PPA, winget, Snap)?
