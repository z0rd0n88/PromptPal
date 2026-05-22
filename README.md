# PromptPal

A small CLI that rewrites a raw prompt into a clearer, more capable version using Claude. One command, two backends (the local Claude CLI or the Anthropic API), zero runtime dependencies beyond Python 3.

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Requirements](#requirements)
3. [Install](#install)
4. [Quick Start](#quick-start)
5. [Backends](#backends)
6. [Configuration & Data Layout](#configuration--data-layout)
7. [CLI Flags](#cli-flags)
8. [Architecture](#architecture)
9. [Testing](#testing)
10. [Uninstall](#uninstall)
11. [Status & Roadmap](#status--roadmap)

## Executive Summary

PromptPal is a single-binary CLI (`promptpal`) that pipes your raw prompt through a Prompt Builder-style system prompt and returns a refined version, then optionally enters an interactive refinement loop (`accept / iterate / discard / raw / copy`). It auto-detects whether to call the locally installed Claude CLI or the Anthropic Messages API, persists every session as atomic JSON under `~/.promptpal/`, and treats WSL2 Ubuntu as the canonical Windows path (a winget package ships a launcher that shells into WSL). The Python core is stdlib-only — there is no `pip install` step, no `requirements.txt`, no `pyproject.toml`. Pipe-clean (`--quiet`, `--output json`) for scripting; stateful (`--replay`, `--search`) for prompt-engineering work.

## Requirements

- **macOS, Linux, or WSL2 Ubuntu** (Windows is reached *only* through WSL — see [the Windows launcher](launcher/README.md))
- **Python 3.10+** on `PATH`
- **One of:**
  - [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) authenticated via `claude auth login`, *or*
  - `ANTHROPIC_API_KEY` exported in the environment

No further packages are installed. The core is stdlib-only by design (PRD §4, D-1/D-2).

## Install

From a WSL Ubuntu / Linux / macOS shell:

```bash
git clone https://github.com/z0rd0n88/PromptPal.git
cd PromptPal
./install.sh
```

This:

1. Refuses to run if `$HOME` is an NTFS-mounted Windows path (`/mnt/c/*` / `/c/*`) — protects against running from a misrouted launch.
2. Copies `core/` and `defaults/` into `~/.promptpal/lib/` (the *managed* install root — re-running `install.sh` always replaces it).
3. Seeds `~/.promptpal/config.json` and `~/.promptpal/system-prompt.md` **once** — subsequent installs never overwrite user-owned files.
4. Generates a launcher at `~/.local/bin/promptpal` with a hardcoded absolute `PROMPTPAL_LIB`.
5. Warns if `~/.local/bin` is not on `PATH` and reports which backends are configured.

Override paths via `INSTALL_DIR` and `PROMPTPAL_HOME` env vars.

### Windows (via winget)

Windows users install a thin launcher that requires WSL Ubuntu underneath:

```powershell
winget install PromptPal.PromptPal
wsl --install -d Ubuntu      # if not already installed
wsl -d Ubuntu -- bash -c "git clone https://github.com/z0rd0n88/PromptPal.git && cd PromptPal && ./install.sh"
```

After that, `promptpal "..."` from PowerShell forwards into WSL. See [`launcher/README.md`](launcher/README.md) for the contract.

## Quick Start

```bash
# One-shot improvement printed to stdout
promptpal "write a function that returns the nth fibonacci number"

# Pipe-clean for scripts (stdout = improved prompt only)
echo "summarize the attached PDF" | promptpal --quiet

# Auto-iterate before the interactive choice
promptpal --iterations 3 "draft a release note for v0.2"

# Drop the result on the (Windows) clipboard from WSL
promptpal --copy "rewrite as a customer email"

# Browse history
promptpal --show-history
promptpal --search "release note"
promptpal --replay 9c4f2a1e
promptpal --export 9c4f2a1e > session.json

# Status check before a long refinement
promptpal --status
```

## Backends

PromptPal resolves a backend at every invocation:

| Priority | Backend | Trigger |
|---|---|---|
| 1 | Persisted preference in `~/.promptpal/config.json` | `preferred_backend = "claude-cli"` or `"api-key"` |
| 2 | Claude CLI auto-detect | `claude` is on `PATH` |
| 3 | API auto-detect | `ANTHROPIC_API_KEY` is set |
| 4 | Failure | Neither configured → exit 1 with two-option setup hint |

Override per-call with `--backend {auto,claude-cli,api-key}`. An *explicit* `--backend claude-cli`/`api-key` **persists** after a successful turn; `--backend auto` clears the preference. A failed call never rewrites config.

The CLI backend uses `claude --input-format=stream-json --output-format=stream-json --print --bare --system-prompt-file <path>` and feeds the full message history as NDJSON on stdin — there's no `"Human:"/"Assistant:"` flattening, so prompts containing those literals don't confuse the model (PRD D-7).

## Configuration & Data Layout

```
~/.promptpal/
├── config.json            # user settings (seeded from defaults/config.json)
├── system-prompt.md       # editable rewrite prompt (seeded from core/system_prompt.txt)
├── lib/
│   ├── core/              # managed — replaced on every install.sh
│   └── defaults/
├── history/
│   ├── index.json         # newest-first index of all sessions
│   └── <uuid>.json        # one file per session, atomically written
└── usage.ndjson           # append-only token-usage log
```

Editable keys in `config.json` (defaults shown):

| Key | Default | Purpose |
|---|---|---|
| `default_model` | `claude-sonnet-4-6` | passed through unchanged to both backends |
| `default_iterations` | `1` | auto-iterations before the interactive choice |
| `auto_copy` | `false` | implicit `--copy` on accept |
| `show_diff` | `true` | show colored diff between original and improved prompt |
| `system_prompt_path` | `~/.promptpal/system-prompt.md` | path to the rewrite system prompt |
| `history_enabled` | `true` | global kill-switch for session writes |
| `max_history_entries` | `500` | LRU-prune the index past this count |
| `system_prompt_update_url` | GitHub raw URL | source for `--update-system-prompt` (sha256-verified) |
| `preferred_backend` | `auto` | persisted by an explicit `--backend` choice |

## CLI Flags

```
positional:
  prompt                  Prompt text. If omitted, read from stdin or interactively.

options:
  --model MODEL           Override default_model.
  --iterations N          Auto-iterate N times before the interactive choice.
  --no-history            Don't persist this session.
  --copy                  Copy accepted prompt to clipboard (clip.exe on WSL).
  --show-history          Paginated session list (newest first).
  --replay SESSION_ID     Resume an existing session in the refinement loop.
  --system-prompt FILE    Override the rewrite prompt for this run only.
  --output {plain,json}   Stdout shape. Default: plain.
  --quiet                 Pipe-clean: suppress diff/spinner/choice; auto-accept.
  --search KEYWORD        Search history; print matching index rows.
  --export SESSION_ID     Dump full session JSON and exit.
  --name LABEL            Tag the session with a human-readable label.
  --update-system-prompt  Fetch + sha256-verify a new rewrite prompt.
  --uninstall             Run uninstall.sh.
  --backend {auto,claude-cli,api-key}
  --status                Print backend, model, auth, platform, history count.
  --xml-tags              Let the model structure the rewrite with XML-style
                          tags (<task>, <input>, ...). Default: plain headings.
```

## Architecture

```
bin/promptpal          (bash)    WSL HOME guard → exec python3 -m core.main
└─ core/
   ├── main.py                   thin __main__ shim
   ├── cli.py                    single orchestration layer — owns every stdout/stderr write
   ├── backend.py                Backend ABC + NoBackendError canonical message
   ├── cli_backend.py            CliBackend — subprocess + stream-json NDJSON pipe
   ├── api_backend.py            ApiBackend — urllib transport + SSE streaming
   ├── resolve.py                backend selection + preference persistence
   ├── config.py                 atomic JSON read/write of ~/.promptpal/config.json
   ├── system_prompt.py          seeding + remote update with sha256 sidecar verification
   ├── history.py                session writes, index upsert, search, replay
   ├── platform.py               WSL/macOS/Linux detection + clip.exe / pbcopy / xclip
   ├── input.py                  read_prompt — stdin / arg / interactive
   ├── diff.py                   side-by-side or unified diff renderer
   ├── loop.py                   accept/iterate/discard/raw/copy state machine
   └── _io.py                    atomic_write_bytes + append_ndjson_line (shared)
```

Every CLI helper is pure-functional and side-effect-free on import; the CLI is the only place flags, filesystem, network, and exit codes meet. Every public seam (`backend_factory`, `fetcher`, `clock`, `id_factory`, `copy_fn`, `detect_platform_fn`, …) is injectable for tests — see `tests/integration/test_pipeline.py` for end-to-end usage.

## Testing

```bash
python -m pytest                # 80%+ coverage target (PRD §10)
python -m pytest tests/unit     # fast, no subprocess
python -m pytest tests/integration
```

- `tests/unit/` — module-level tests with fakes for backends, filesystem, clock, id_factory.
- `tests/integration/test_flags.py` — one assert per PRD §5.4 flag, end-to-end through `core.cli.main` (catches regressions where the parser still accepts a flag but `main()` silently ignores it).
- `tests/integration/test_pipeline.py` — full happy-path + history-write + replay round-trip.
- `tests/integration/test_stdin.py` — pipe-safety contract: stdout carries *only* the improved prompt or the JSON envelope.
- `tests/integration/test_winget_launcher.py` — Windows launcher contract (skipped off-Windows).

Pyright is configured in `pyrightconfig.json` (basic mode, Python 3.10).

## Uninstall

```bash
./uninstall.sh           # interactive: keeps ~/.promptpal by default
./uninstall.sh --purge   # remove ~/.promptpal without prompting
```

Removes `~/.local/bin/promptpal`. The `~/.promptpal/` data dir is preserved unless `--purge` is passed or the prompt is answered `y`.

## Status & Roadmap

**Phase 1 (CLI) — current.** The full requirements catalog is [`PRD-PHASE1.md`](PRD-PHASE1.md). Implemented user stories: US-003 (platform), US-011/012 (CLI surface), US-013 (install), US-014 (winget), US-015 (uninstall + `--replay`), US-016 (test suite). Outstanding work and decisions are in [`ralph/progress.txt`](ralph/progress.txt).

**Phase 2 (GUI)** is explicitly deferred until both backends have proven stable in real use for ≥ 2 weeks (PRD D-4).
