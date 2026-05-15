# PRD Amendment 001 — WSL Support and Claude CLI Integration

**Amends:** [PRD.md](./PRD.md)  
**Date:** 2026-05-15  
**Status:** Approved  
**Scope:** Phase 1 (CLI)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Problem Statement](#problem-statement)
3. [Goals](#goals)
4. [Non-Goals](#non-goals)
5. [User Stories](#user-stories)
6. [Requirements](#requirements)
7. [Acceptance Criteria](#acceptance-criteria)
8. [Success Metrics](#success-metrics)
9. [Open Questions](#open-questions)
10. [Timeline Considerations](#timeline-considerations)

---

## Executive Summary

Two targeted additions to the Phase 1 CLI scope. First, PromptPal must run correctly inside Windows Subsystem for Linux (WSL) — the primary development environment for this project. Second, when the user already has the Claude CLI (`claude`) installed, PromptPal should use it as the backend instead of requiring a raw Anthropic API key. These changes reduce the setup burden for the most common user profile and remove a hard dependency on direct API key management.

---

## Problem Statement

**WSL:** The target developer environment is WSL2 (Ubuntu on Windows). Without explicit WSL compatibility — correct PATH handling, clipboard support, home directory resolution, and line endings — the tool silently fails or behaves unpredictably on the machine it will be used on most.

**Claude CLI as backend:** The Anthropic API key is a friction point: it requires an Anthropic account, API access (separate from Claude.ai subscription), and manual env var configuration. Users who already have `claude` (Claude Code CLI) installed are authenticated and can make Claude API calls via that binary. Requiring them to also manage a raw API key is unnecessary duplication.

---

## Goals

1. PromptPal installs and runs correctly in a WSL2 Ubuntu environment on the first attempt.
2. Users with `claude` CLI installed can run `promptpal` with zero additional auth configuration.
3. The tool auto-detects which backend to use (Claude CLI vs. API key) without user intervention.
4. The fallback chain is transparent: users always know which backend is active.
5. WSL-specific clipboard and terminal behavior is handled gracefully without extra setup steps.

---

## Non-Goals

- No Windows-native (PowerShell/CMD) support — WSL is the only Windows delivery mechanism in Phase 1.
- No support for other CLI tools (OpenAI CLI, Gemini CLI) as backends — Claude CLI only.
- No GUI changes in this amendment — Phase 2 GUI WSL support is a separate scope item.
- No automatic installation of `claude` CLI — PromptPal will not install or update it.
- No per-backend output normalization differences — the improved prompt format is identical regardless of backend.

---

## User Stories

### WSL Support

**As a developer running WSL2 on Windows**, I want `promptpal` to install and run correctly inside my WSL Ubuntu shell so that I do not have to maintain a separate native Linux machine to use the tool.

**As a WSL user**, I want clipboard copy (`--copy`) to work from inside my WSL shell so that I can paste the improved prompt into a Windows application without manual selection.

**As a WSL user**, I want the installer to detect I am in WSL and use the correct home directory (`/home/<user>`, not `/mnt/c/Users/<user>`) so that config and history are stored in the WSL filesystem where I/O is fast.

**As a WSL user**, I want the tool's output to use LF line endings so that text I paste into Windows applications does not include stray carriage returns.

### Claude CLI Backend

**As a user who has Claude Code installed**, I want `promptpal` to automatically use my existing `claude` auth so that I do not need to separately manage an `ANTHROPIC_API_KEY` environment variable.

**As a new user**, I want `promptpal --status` to show me which backend is active (Claude CLI or API key) and confirm auth is working so that I can diagnose setup problems immediately.

**As a power user**, I want to explicitly force a specific backend with `--backend claude-cli` or `--backend api-key` so that I can test both or override the auto-detection.

**As a user whose `claude` CLI session has expired**, I want a clear error message telling me to run `claude auth login` rather than a cryptic API failure so that I can fix the problem in one step.

---

## Requirements

### WSL Support — Must Have (P0)

| ID | Requirement |
|----|-------------|
| WSL-01 | The installer detects WSL via `/proc/sys/kernel/osrelease` containing `microsoft` and adjusts install paths accordingly. |
| WSL-02 | `~/.promptpal/` is always created inside the WSL filesystem (`/home/<user>/`), never on the NTFS mount (`/mnt/c/...`). |
| WSL-03 | All files written by PromptPal use LF line endings; the tool never writes CRLF. |
| WSL-04 | The bash entrypoint resolves `HOME` from the WSL environment (`/home/<user>`), not from any Windows-inherited env var. |
| WSL-05 | The tool's test suite includes a WSL-detection smoke test that runs in CI. |

### WSL Support — Should Have (P1)

| ID | Requirement |
|----|-------------|
| WSL-06 | `--copy` flag works in WSL via `clip.exe` (Windows clipboard) when `xclip`/`xsel`/`pbcopy` are unavailable. |
| WSL-07 | `promptpal --status` reports `Platform: WSL2` alongside backend and auth status. |
| WSL-08 | The installer warns if `HOME` is not under `/home/` and explains the correct WSL launch method. |

### WSL Support — Could Have (P2)

| ID | Requirement |
|----|-------------|
| WSL-09 | Shell completion scripts are auto-installed for the default shell detected in the WSL environment. |
| WSL-10 | The GUI (Phase 2) launches from WSL via `wslg` or X410 without manual display configuration. |

---

### Claude CLI Backend — Must Have (P0)

| ID | Requirement |
|----|-------------|
| CLI-01 | On startup, auto-detect `claude` on `PATH` via `command -v claude`. |
| CLI-02 | If `claude` is found and `ANTHROPIC_API_KEY` is not set, use Claude CLI as the backend. |
| CLI-03 | If both are available, prefer Claude CLI. User can override with `--backend api-key`. |
| CLI-04 | If neither is available, fail immediately with a clear message listing both setup options. |
| CLI-05 | Claude CLI backend invokes: `claude -p "<system_prompt>\n\n<user_prompt>"` and captures stdout as the improved prompt. |
| CLI-06 | Multi-turn refinement via Claude CLI uses a temp conversation file or stateful `claude` session flags if supported; falls back to sending full context as a single prompt per turn if not. |
| CLI-07 | Auth errors from `claude` (non-zero exit, auth-related stderr) produce a human-readable message: `"Claude CLI auth failed. Run: claude auth login"`. |

### Claude CLI Backend — Should Have (P1)

| ID | Requirement |
|----|-------------|
| CLI-08 | `promptpal --status` shows: active backend, `claude` binary path (if CLI mode), model, and a live auth check result. |
| CLI-09 | `--backend` flag accepts `claude-cli` or `api-key` to override auto-detection. |
| CLI-10 | Token usage logging (`usage.log`) continues to work in Claude CLI mode; parse token counts from `claude` verbose output if available, otherwise log `null`. |
| CLI-11 | `--model` flag is passed through to `claude` CLI when using the CLI backend (e.g., `claude --model claude-opus-4-5`). |

### Claude CLI Backend — Could Have (P2)

| ID | Requirement |
|----|-------------|
| CLI-12 | Support `--backend mcp` in a future phase to route through an MCP server instead of direct API or CLI. |
| CLI-13 | Detect `claude` version and warn if it is below a minimum known-working version. |

---

## Acceptance Criteria

### WSL

**Given** the installer is run in a WSL2 Ubuntu shell  
**When** installation completes  
**Then** `~/.promptpal/` exists at `/home/<user>/.promptpal/`, not under `/mnt/c/`

**Given** the user runs `promptpal --copy "a prompt"`  
**When** `xclip` and `xsel` are not installed  
**Then** the improved prompt is copied to the Windows clipboard via `clip.exe` and a message confirms this

**Given** the user runs `promptpal` with `HOME` set to a Windows path (regression scenario)  
**When** the tool starts  
**Then** it prints a warning: `"Warning: HOME appears to be a Windows path. For best results, launch from WSL: wsl -d Ubuntu -- promptpal"` and exits with code 1

**Given** any file written by PromptPal (history JSON, config, usage log)  
**When** inspected with `file <path>`  
**Then** the result says `ASCII text` (not `ASCII text, with CRLF line terminators`)

### Claude CLI Backend

**Given** `claude` is on PATH and `ANTHROPIC_API_KEY` is not set  
**When** `promptpal "write a sorting function"` is run  
**Then** the tool uses the Claude CLI backend and returns an improved prompt without prompting for an API key

**Given** `claude` is on PATH and `ANTHROPIC_API_KEY` is set  
**When** `promptpal "prompt"` is run without `--backend`  
**Then** the Claude CLI backend is used (CLI takes precedence) and `promptpal --status` confirms `Backend: claude-cli`

**Given** the user passes `--backend api-key`  
**When** `promptpal "prompt"` is run  
**Then** the raw API key backend is used regardless of whether `claude` is on PATH

**Given** `claude` auth has expired  
**When** `promptpal "prompt"` is run in Claude CLI mode  
**Then** the error output reads: `"Claude CLI auth failed. Run: claude auth login"` and exits with code 1

**Given** neither `claude` is on PATH nor `ANTHROPIC_API_KEY` is set  
**When** `promptpal "prompt"` is run  
**Then** the error output reads:
```
Error: No backend available. Set up one of the following:
  Option 1 (Claude CLI): Install Claude Code and run `claude auth login`
  Option 2 (API key):    export ANTHROPIC_API_KEY="sk-ant-..."
```

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Install success rate on WSL2 Ubuntu 22.04 | 100% (zero known failures) |
| Time to first improved prompt in WSL (Claude CLI mode) | < 15 seconds from cold start |
| Users requiring API key setup when `claude` is already installed | 0% |
| Auth error messages actionable without external docs | 100% (self-contained fix instructions) |

---

## Open Questions

| # | Question | Owner | Blocking? |
|---|----------|-------|-----------|
| 1 | Does `claude` CLI support a `--model` flag or equivalent for model selection? Check `claude --help`. | Engineering | No |
| 2 | Does `claude` CLI support multi-turn conversation state via flags, or must full context be re-sent each turn? | Engineering | Yes — affects CLI-06 implementation |
| 3 | Is there a minimum `claude` CLI version that supports the invocation pattern in CLI-05? | Engineering | No |
| 4 | Should `--backend` be persisted to `config.json` when explicitly set, or only apply to the current invocation? | Product | No |

---

## Timeline Considerations

- WSL support (P0 items) must be validated before Phase 1 is considered shippable — this is the primary dev environment.
- Claude CLI backend detection (CLI-01 through CLI-07) should be implemented in the same pass as the API client, not as an afterthought, to avoid dual code paths that diverge.
- Open question #2 (multi-turn via Claude CLI) must be answered before implementing the refinement loop to avoid rework.
