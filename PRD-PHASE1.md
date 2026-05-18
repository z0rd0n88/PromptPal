# PromptPal — Phase 1 PRD (CLI)

**Status:** Approved for build
**Scope:** Phase 1 only — CLI on macOS, Linux, and WSL2 Ubuntu (Windows reached via WSL)
**Derived from:** [PRD.md](./PRD.md), [PRD-UPDATE-001-wsl-and-claude-cli.md](./PRD-UPDATE-001-wsl-and-claude-cli.md), [SPEC.md](./SPEC.md)
**Date:** 2026-05-17

This document is the authoritative Phase 1 requirements catalog. It composes the original `PRD.md` (Phase 1 sections), `PRD-UPDATE-001` (WSL + Claude CLI backend), and the resolutions to the SPEC §16 open questions into a single buildable surface. Phase 2 (GUI) material is intentionally excluded.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Scope and Phase Boundaries](#2-scope-and-phase-boundaries)
3. [Personas and Top User Stories](#3-personas-and-top-user-stories)
4. [Decision Log](#4-decision-log)
5. [Functional Requirements](#5-functional-requirements)
   1. [Pipeline (P1-PIPE-\*)](#51-pipeline-p1-pipe-)
   2. [Backends and Auto-Detection (P1-BKND-\*)](#52-backends-and-auto-detection-p1-bknd-)
   3. [Refinement Loop (P1-LOOP-\*)](#53-refinement-loop-p1-loop-)
   4. [CLI Surface — Flags (P1-FLAG-\*)](#54-cli-surface--flags-p1-flag-)
   5. [System Prompt Management (P1-SP-\*)](#55-system-prompt-management-p1-sp-)
   6. [History and Persistence (P1-HIST-\*)](#56-history-and-persistence-p1-hist-)
   7. [Configuration (P1-CFG-\*)](#57-configuration-p1-cfg-)
   8. [WSL and Platform (P1-PLAT-\*)](#58-wsl-and-platform-p1-plat-)
   9. [Installation and Distribution (P1-INST-\*)](#59-installation-and-distribution-p1-inst-)
   10. [Error Handling (P1-ERR-\*)](#510-error-handling-p1-err-)
6. [Non-Functional Requirements](#6-non-functional-requirements)
7. [Out of Scope (Phase 1)](#7-out-of-scope-phase-1)
8. [Acceptance Criteria](#8-acceptance-criteria)
9. [Success Metrics](#9-success-metrics)
10. [Test Coverage Requirements](#10-test-coverage-requirements)
11. [Risks and Mitigations](#11-risks-and-mitigations)
12. [Dependencies and Constraints](#12-dependencies-and-constraints)
13. [Release Checklist](#13-release-checklist)
14. [SPEC Amendments Triggered by This PRD](#14-spec-amendments-triggered-by-this-prd)

---

## 1. Executive Summary

PromptPal Phase 1 ships a single command, `promptpal`, that takes a raw user prompt and returns a Prompt Builder–style improved version. It is a thin bash entrypoint that delegates to a Python 3 core (stdlib only). The core auto-detects which backend to use — the locally installed Claude CLI (preferred) or the Anthropic Messages API via raw key — and supports a stateful multi-turn refinement loop. All sessions are persisted as atomic JSON writes under `~/.promptpal/`. The tool is a first-class citizen of WSL2 Ubuntu, distributed on Windows via `winget` with a launcher that shells into WSL.

The Phase 1 success bar is: a user with either `claude` on `PATH` *or* `ANTHROPIC_API_KEY` in their environment can install PromptPal and improve their first prompt in under 15 seconds, with the improved prompt and full session JSON written to disk, on macOS, Linux, or WSL2 Ubuntu, without any extra runtime installs beyond Python 3.

---

## 2. Scope and Phase Boundaries

### In Scope (Phase 1)

- `promptpal` CLI — single binary on `PATH` (`/usr/local/bin/promptpal` or `~/.local/bin/promptpal`).
- Two backend implementations behind a single ABC: `CliBackend` (Claude CLI subprocess) and `ApiBackend` (Anthropic HTTP).
- Auto-detection of the active backend at startup, overridable per-invocation via `--backend`.
- Multi-turn refinement loop (`[a]ccept / [i]terate / [d]iscard / [r]aw / [c]opy`).
- Local history at `~/.promptpal/history/<uuid>.json` with index and search.
- Configurable system prompt (`~/.promptpal/system-prompt.md`), bundled default at `core/system_prompt.txt`, optional remote update via signed sidecar (D-3).
- WSL2 first-class support: HOME guard, `clip.exe` clipboard, LF line endings.
- Installer: WSL Ubuntu native install path; Windows `winget` package whose launcher shells into `wsl -d Ubuntu -- promptpal "$@"`.
- Test suite with ≥ 80% coverage, including a WSL-detection smoke test in CI.

### Explicitly Out of Scope (Phase 1)

All Phase 2/3 items, listed in [§7](#7-out-of-scope-phase-1).

---

## 3. Personas and Top User Stories

### Power User / Developer (primary)

Runs WSL2 Ubuntu on a Windows machine. Has `claude` already authenticated. Wants `promptpal "..."` to Just Work with no extra env var, and wants `echo prompt | promptpal --quiet` to be pipe-clean for shell composition.

- *"I want to pipe a prompt through `promptpal --quiet` in a Makefile and get only the improved text on stdout."*
- *"I want `--copy` to land the improved prompt on the Windows clipboard so I can paste it into Claude.ai or VS Code."*
- *"I never want to manage an `ANTHROPIC_API_KEY` if `claude auth login` already worked."*

### Prompt Engineer

Iteratively crafts prompts. Wants the diff view, labeled sessions, and `--replay` so they can resume refinement on a prompt from yesterday.

- *"I want every session named and searchable so my prompt library survives terminal resets."*
- *"I want `--status` to confirm which backend is active before I run a 5-turn refinement so I don't lose work to an expired token."*

### CI/Automation User

Embeds `promptpal` in a non-interactive script. Cannot tolerate any prompt-for-input or surprise streaming chatter on stdout.

- *"I need exit code 1 (not a hang) when no backend is configured, with the actionable fix text on stderr."*
- *"I need `--output json` to return a single parseable object — never a partial stream."*

---

## 4. Decision Log

The following ten decisions are locked. Requirements in [§5](#5-functional-requirements) trace back to them. The "SPEC delta" column flags where the resolution changes the existing SPEC text — those changes are summarized in [§14](#14-spec-amendments-triggered-by-this-prd).

| ID | Open Question | Decision | Rationale | SPEC delta |
|----|---------------|----------|-----------|------------|
| **D-1** | Q1 — CLI runtime | Keep **bash + python3 (stdlib only)** | Zero new runtime dependencies on macOS/Linux/WSL; matches non-goal of "no extra runtime installs". Go/Rust rewrite deferred to a hypothetical Phase 3. | None |
| **D-2** | Q2 — `jq` dependency | **None.** All JSON handled in Python. | `jq` is not present on stock Windows/WSL Ubuntu images; Python stdlib's `json` module suffices. | None |
| **D-3** | Q3 — System prompt source | **Bundle** `core/system_prompt.txt` in the repo. `--update-system-prompt` fetches from a GitHub raw URL and verifies a co-located `.sha256` sidecar before atomic-replacing `~/.promptpal/system-prompt.md`. | Lets users run offline immediately after install; signed sidecar prevents a compromised CDN from injecting prompt-injection payloads. | SPEC §10 and §11 must reference `core/system_prompt.txt`; SPEC §11 First-Run currently `cp defaults/system-prompt.md` — change to `cp core/system_prompt.txt`. |
| **D-4** | Q4 — GUI timeline | **Phase 2 starts only after the CLI is stable on both backends** (`CliBackend` and `ApiBackend`) for ≥ 2 weeks in real use. | Avoids building GUI plumbing on top of an unstable core. Out of scope for this PRD. | None (Phase 2 untouched) |
| **D-5** | Q5 — Distribution | **Windows-first via `winget`.** The `winget` package is a thin launcher that requires WSL Ubuntu and shells into `wsl -d Ubuntu -- promptpal "$@"`. WSL install is the curl one-liner; Homebrew tap for macOS is a follow-up, not a Phase 1 blocker. | The primary dev environment is WSL2 (per `~/CLAUDE.md`). Native Windows builds would duplicate the entire backend layer; the launcher pattern keeps a single implementation. | New §11 subsection needed for `winget` launcher + Windows-side install path. |
| **D-7** | Q7 / S-1 — Claude CLI multi-turn | Use `claude --input-format=stream-json --output-format=stream-json` with `--print` for non-interactive completion. **Drop `_build_prompt` flattening entirely.** Feed the full `messages` array as NDJSON on stdin; consume `assistant`/`message` events from stdout. | Native multi-turn preserves role boundaries and avoids the `"Human:"/"Assistant:"` prefix hack that risks confusing the model and breaking on prompts containing those literal strings. Stream output is also a free win for interactive UX. | SPEC §6 `_build_prompt` is removed; `CliBackend.complete` rewritten around stream-json pipe. |
| **D-8** | Q8 / S-2 — `--model` for CLI backend | **Verified:** `claude --model claude-sonnet-4-6` accepts the same model strings as the Anthropic API. Pass `--model` through to `claude` unchanged. | Removes a translation table and avoids a class of "works on API, breaks on CLI" bugs. | SPEC §6 already correct; verification status promoted from "blocking" to "resolved". |
| **D-9** | Q9 / S-3 — `clip.exe` UTF-8 | **Verified:** `clip.exe` accepts UTF-8 without a BOM. Round-trip tested with Greek, CJK, and emoji. | No transcoding shim needed in `core/platform.py`. | SPEC §7 unchanged; test matrix in §14 of SPEC adds UTF-8 round-trip case. |
| **D-Q10** | Q10 / S-4 — `preferred_backend` persistence | `--backend <claude-cli\|api-key>` **persists** to `config.json`. `--backend auto` clears the field (writes `"auto"`). Persistence happens after a successful turn, never on a failed call. | Matches the principle of least surprise: a user who explicitly picks a backend intends to keep using it; auto-detection only matters until first explicit choice. Failed-call gate prevents a transient outage from rewriting config. | SPEC §10 must add "writes `preferred_backend` after successful first turn when `--backend` is explicit". |
| **D-10** | NEW — CLI default flags | `CliBackend` always invokes `claude` with `--bare --system-prompt-file <path>` in addition to the stream-json flags. `--bare` strips Claude Code chrome from output; `--system-prompt-file` avoids re-passing the entire system prompt on every call (faster + lets the prompt live as a real file). | Without `--bare`, output is contaminated by tool-use blocks and status lines. Without `--system-prompt-file`, multi-turn restarts would lose the system role. | SPEC §6 `CliBackend.complete` command vector adds `--bare --system-prompt-file`. |

---

## 5. Functional Requirements

Each requirement carries a stable ID, priority (MUST / SHOULD / NICE), a one-line description, and a SPEC reference where applicable. Acceptance criteria are listed in [§8](#8-acceptance-criteria); test mapping in [§10](#10-test-coverage-requirements).

### 5.1 Pipeline (`P1-PIPE-*`)

| ID | Priority | Requirement | SPEC ref |
|----|----------|-------------|----------|
| P1-PIPE-01 | MUST | Accept raw prompt via positional CLI arg: `promptpal "your prompt"`. | §5 Input Resolution |
| P1-PIPE-02 | MUST | Accept raw prompt via stdin when stdin is not a TTY: `echo "prompt" \| promptpal`. | §5 Input Resolution |
| P1-PIPE-03 | MUST | Accept raw prompt via interactive TTY input when no arg and stdin is a TTY. | §5 Input Resolution |
| P1-PIPE-04 | MUST | Load system prompt from path defined in `Config.system_prompt_path` (default `~/.promptpal/system-prompt.md`). On first run, seed from bundled `core/system_prompt.txt`. | §10, §11, D-3 |
| P1-PIPE-05 | MUST | Send the system prompt and `messages` array through the resolved `Backend.complete()`. | §6 |
| P1-PIPE-06 | MUST | Display unified diff between original and improved prompts when improved length > 3 lines. | §5 Output Modes |
| P1-PIPE-07 | SHOULD | Stream API/CLI tokens to terminal in interactive mode (suppressed under `--quiet` and non-TTY stdout). | §6 Streaming |
| P1-PIPE-08 | SHOULD | Show a progress spinner during backend calls in interactive mode. | §5 Output Modes |
| P1-PIPE-09 | MUST | Final improved prompt is written to **stdout**; all logs, warnings, diff chrome, and spinner output go to **stderr**. Required for pipe-safety. | §12 |

### 5.2 Backends and Auto-Detection (`P1-BKND-*`)

| ID | Priority | Requirement | SPEC ref |
|----|----------|-------------|----------|
| P1-BKND-01 | MUST | Implement a `Backend` ABC with `name`, `complete(system, messages, stream)`, and `check_auth()` methods. | §6 |
| P1-BKND-02 | MUST | Implement `ApiBackend` against the Anthropic Messages API (`POST /v1/messages`, `anthropic-version: 2023-06-01`). | §6 |
| P1-BKND-03 | MUST | Implement `CliBackend` invoking `claude --print --model <m> --bare --system-prompt-file <path> --input-format=stream-json --output-format=stream-json` and piping the `messages` array as NDJSON on stdin (per D-7, D-10). | §6, D-7, D-10 |
| P1-BKND-04 | MUST | `resolve_backend(preferred, model)` selection order: explicit flag → `Config.preferred_backend` → auto-detect (`claude` on PATH → `CliBackend`; else `ANTHROPIC_API_KEY` set → `ApiBackend`; else `NoBackendError`). | §6, §10 |
| P1-BKND-05 | MUST | When `preferred == "claude-cli"` and `claude` is missing, fail immediately with `FileNotFoundError` and actionable text. Do **not** silently fall back to the API. | §6, §12 |
| P1-BKND-06 | MUST | When `preferred == "api-key"` and `ANTHROPIC_API_KEY` is unset, fail immediately with the env-var instructions message. Do **not** silently fall back to the CLI. | §6, §12 |
| P1-BKND-07 | MUST | When neither backend is available, exit 1 with the two-option setup message (see §12 error table). | §12 |
| P1-BKND-08 | MUST | `ApiBackend.complete()` retry behavior: 429 honors `Retry-After` (max 3 retries); 5xx exponential backoff 1s/2s/4s (max 3 retries); network errors retry once after 2s. 401 fails immediately. | §6 Retry logic |
| P1-BKND-09 | MUST | `CliBackend` detects auth failure via non-zero exit + any of `{authentication, unauthorized, auth, login, token}` in stderr (case-insensitive), maps to `"Claude CLI auth failed. Run: claude auth login"`. | §6 |
| P1-BKND-10 | MUST | Each persisted turn records `backend: "claude-cli" \| "api-key"`. `input_tokens` and `output_tokens` are `null` for CLI turns; numeric for API turns. | §4 Session Record |
| P1-BKND-11 | MUST | API key is read **only** from the `ANTHROPIC_API_KEY` environment variable. It is never read from `config.json`, never echoed to stdout, never written to history, never logged to `usage.log`. | §10, §12 |
| P1-BKND-12 | SHOULD | `Backend.check_auth()` performs a lightweight liveness check (CLI: `claude --version`; API: minimal `messages` call with `max_tokens=1`). Used by `--status`. | §6 |
| P1-BKND-13 | MUST | When `--backend <claude-cli\|api-key>` is passed explicitly, persist the choice to `config.preferred_backend` after the first successful turn. `--backend auto` resets the field to `"auto"`. (D-Q10) | §10, D-Q10 |

### 5.3 Refinement Loop (`P1-LOOP-*`)

| ID | Priority | Requirement | SPEC ref |
|----|----------|-------------|----------|
| P1-LOOP-01 | MUST | After displaying the improved prompt, present the interactive choice line `[a]ccept [i]terate [d]iscard [r]aw [c]opy`. | §8 |
| P1-LOOP-02 | MUST | On `[i]terate`, read user feedback, append it as a `user` turn to `messages`, call `Backend.complete()` again, append the response, and re-display. The full `messages` array is sent every call — no truncation in Phase 1. | §8 |
| P1-LOOP-03 | MUST | Support at minimum 3 successive iterations in a single session. | §8 |
| P1-LOOP-04 | MUST | On `[a]ccept`: write/finalize the session JSON with `status: "accepted"`, optionally copy to clipboard (if `auto_copy` or `--copy`), then exit 0. | §8, §9 |
| P1-LOOP-05 | MUST | On `[d]iscard`: write/finalize the session JSON with `status: "discarded"` (so history reflects the attempt), then exit 0. | §8, §9 |
| P1-LOOP-06 | SHOULD | On `[r]aw`: print the raw improved prompt with no diff/chrome to stderr (so it doesn't pollute a potentially piped stdout) and re-present the choice line. | §5 |
| P1-LOOP-07 | SHOULD | On `[c]opy`: copy current improved prompt to clipboard and re-present the choice line. Warn (non-fatal) if no provider is available. | §7 |
| P1-LOOP-08 | MUST | If `--iterations N` is passed, run N auto-iterations with a synthesized feedback turn (`"Improve this further."`) before presenting the choice line. | §5 |

### 5.4 CLI Surface — Flags (`P1-FLAG-*`)

All flags are parsed by `argparse` in `core/cli.py` and mapped to the `CLIOptions` dataclass (SPEC §5). Behaviors below are normative; the SPEC describes the dataclass shape.

| Flag | Priority | Behavior |
|------|----------|----------|
| `--model MODEL` | MUST | Override default model (Config: `default_model`, default `claude-sonnet-4-6`). Passed through to both backends identically (D-8). |
| `--iterations N` | SHOULD | Run N auto-iterations (P1-LOOP-08) before the interactive prompt. |
| `--no-history` | MUST | Skip writing the session and index entry. Usage log still written unless `--quiet` is also set. |
| `--copy` | SHOULD | Copy final improved prompt to clipboard on accept. |
| `--show-history` | MUST | Print a paginated list of sessions from `index.json` (newest first), then exit 0. |
| `--replay SESSION_ID` | SHOULD | Load the specified session, replay `messages` into a new session, and enter the refinement loop. |
| `--system-prompt FILE` | SHOULD | Override `Config.system_prompt_path` for this invocation. |
| `--output FORMAT` | MUST | `plain` (default) / `json` / `markdown`. `json` emits a single object `{original, improved, turns, session_id, backend, model}`; `markdown` emits a fenced block. |
| `--quiet` | MUST | Suppress diff, spinner, streaming, and choice line; emit only the improved prompt on stdout. Auto-accept after first turn. |
| `--search KEYWORD` | SHOULD | Search history (index first, then full-session fallback per SPEC §9), print results to stdout, exit 0. |
| `--export SESSION_ID` | SHOULD | Dump the full session JSON to stdout, exit 0. |
| `--name LABEL` | NICE | Assign a human-readable label to the session (recorded in both session file and index). |
| `--update-system-prompt` | NICE | Fetch the system prompt from `Config.system_prompt_update_url`, verify its `.sha256` sidecar, atomically replace `~/.promptpal/system-prompt.md`, exit 0. (D-3) |
| `--uninstall` | SHOULD | Remove the installed binary and (with explicit confirmation) `~/.promptpal/`. |
| `--backend NAME` | MUST | `auto` (default — clears persistence) / `claude-cli` / `api-key`. Persists on explicit non-auto value after first successful turn (D-Q10). |
| `--status` | MUST | Print backend, model, auth check result, platform, config path, history count; exit 0. Format per SPEC §5. |

### 5.5 System Prompt Management (`P1-SP-*`)

| ID | Priority | Requirement | SPEC ref |
|----|----------|-------------|----------|
| P1-SP-01 | MUST | A default system prompt is bundled in the repo at `core/system_prompt.txt`. The installer copies this to `~/.promptpal/system-prompt.md` on first run if and only if no file already exists there. (D-3) | §11 |
| P1-SP-02 | MUST | `~/.promptpal/system-prompt.md` is user-editable and is never overwritten without an explicit `--update-system-prompt` invocation. | §11 |
| P1-SP-03 | SHOULD | `--update-system-prompt` downloads `Config.system_prompt_update_url` and the co-located `<url>.sha256` sidecar, verifies the SHA-256 matches, and atomically (`tempfile.mkstemp` → `os.rename`) replaces `~/.promptpal/system-prompt.md`. On hash mismatch: fail with a clear "checksum mismatch — refusing to overwrite" message and exit 1. | §11, D-3 |
| P1-SP-04 | MUST | `--system-prompt FILE` overrides the resolved path for the current invocation only (no persistence). | §5 |
| P1-SP-05 | MUST | If `Config.system_prompt_path` is missing or unreadable at runtime, fail with "System prompt file not found at <path>. Run with `--update-system-prompt` to restore the default." and exit 1. | §10, §12 |

### 5.6 History and Persistence (`P1-HIST-*`)

| ID | Priority | Requirement | SPEC ref |
|----|----------|-------------|----------|
| P1-HIST-01 | MUST | One JSON file per session at `~/.promptpal/history/<uuid>.json`. Session schema per SPEC §4. | §4, §9 |
| P1-HIST-02 | MUST | Maintain `~/.promptpal/history/index.json` (entries: `session_id, created_at, label, status, original_prompt_preview`). | §4 |
| P1-HIST-03 | MUST | All writes use the atomic `tempfile.mkstemp` → `os.rename` pattern. On any write failure, clean up the temp file. | §9 |
| P1-HIST-04 | MUST | Update the session file on every turn (incremental), not only on accept — so a crash mid-refinement does not lose work. `status: "in-progress"` until accept/discard. | §4, §9 |
| P1-HIST-05 | SHOULD | Append-only NDJSON usage log at `~/.promptpal/usage.log`. One line per turn. `input_tokens`/`output_tokens` may be `null` (CLI backend). | §4 |
| P1-HIST-06 | SHOULD | Enforce `Config.max_history_entries` by evicting oldest entries (by `created_at`) from both the index and disk. Eviction runs after every accept/discard. | §9 |
| P1-HIST-07 | SHOULD | `--search KEYWORD` matches the index's `original_prompt_preview` and `label` first; falls back to scanning `original_prompt` and `final_prompt` in session files. Results sorted by `created_at` descending. | §9 |
| P1-HIST-08 | MUST | History write failures are non-fatal (warn to stderr, exit code unchanged). The improved prompt is still printed. | §12 |

### 5.7 Configuration (`P1-CFG-*`)

| ID | Priority | Requirement | SPEC ref |
|----|----------|-------------|----------|
| P1-CFG-01 | MUST | `~/.promptpal/config.json` schema per SPEC §4 with all listed fields. `Config` dataclass per SPEC §10. | §4, §10 |
| P1-CFG-02 | MUST | Merge order: dataclass defaults → `config.json` overrides → CLI flag overrides. | §10 |
| P1-CFG-03 | MUST | Unknown fields in `config.json` are ignored (forward-compatible). Type-mismatched fields fall back to default with a stderr warning. | §10 |
| P1-CFG-04 | MUST | Corrupt `config.json` (JSON parse error) → exit 1 with "Config file corrupt at ~/.promptpal/config.json. Delete it to reset." | §12 |
| P1-CFG-05 | MUST | Writes to `config.json` (e.g., backend persistence) use the same atomic pattern as history writes. | §9, §10 |
| P1-CFG-06 | MUST | `Config.preferred_backend` accepts exactly `"auto" \| "claude-cli" \| "api-key"`. Any other value falls back to `"auto"` with a stderr warning. | §10 |

### 5.8 WSL and Platform (`P1-PLAT-*`)

| ID | Priority | Requirement | SPEC ref |
|----|----------|-------------|----------|
| P1-PLAT-01 | MUST | `core/platform.py` runs `detect_platform()` at startup before any backend or history call. | §7 |
| P1-PLAT-02 | MUST | WSL detection: `/proc/sys/kernel/osrelease` contains `microsoft` → `is_wsl=True`. Substring `wsl2` → `wsl_version=2`. | §7 |
| P1-PLAT-03 | MUST | HOME guard: if `is_wsl=True` and `HOME` starts with `/mnt/c/` or `/c/`, print the WSL launch fix message to stderr and exit 1. (Bash entrypoint duplicates this guard so it triggers even if Python startup fails.) | §5, §7 |
| P1-PLAT-04 | MUST | `_resolve_home()` cross-checks `HOME` against the passwd entry for the current UID; passwd wins if they disagree and `HOME` is NTFS-mounted. | §7 |
| P1-PLAT-05 | MUST | Clipboard provider selection priority: `xclip -selection clipboard` → `xsel --clipboard --input` → `pbcopy` → (WSL only) `clip.exe` → none. | §7 |
| P1-PLAT-06 | MUST | On WSL with `clip.exe` selected, send UTF-8 input without a BOM. Verified working for Greek, CJK, and emoji (D-9). | §7, D-9 |
| P1-PLAT-07 | MUST | When no clipboard provider is available, `--copy` and `auto_copy` print a one-line warning to stderr; the run continues and exits 0. | §7, §12 |
| P1-PLAT-08 | MUST | All files written by PromptPal use LF line endings. Verified via `file <path>` returning `ASCII text` (not `ASCII text, with CRLF line terminators`). | §7 |
| P1-PLAT-09 | SHOULD | `--status` includes `Platform: WSL2` (or `WSL1` / `Linux` / `macOS`) on its first line. | §5 |

### 5.9 Installation and Distribution (`P1-INST-*`)

| ID | Priority | Requirement | SPEC ref |
|----|----------|-------------|----------|
| P1-INST-01 | MUST | `install.sh` works on macOS, Linux, and WSL2 Ubuntu without additional runtime installs beyond what is already on those base images (bash, python3, curl/wget). | §11 |
| P1-INST-02 | MUST | The installer refuses to run when `HOME` is a Windows path (`/mnt/c/...` or `/c/...`); prints the `wsl -d Ubuntu` fix and exits 1. | §11, §7 |
| P1-INST-03 | MUST | Binary lands at `$INSTALL_DIR/promptpal` (default `~/.local/bin/promptpal`); installer warns if `$INSTALL_DIR` is not on `PATH`. | §11 |
| P1-INST-04 | MUST | `~/.promptpal/` is created with `history/`, `config.json` (from `defaults/config.json`), and `system-prompt.md` (from bundled `core/system_prompt.txt`). Existing files are never overwritten. | §11, D-3 |
| P1-INST-05 | MUST | Installer runs the post-install backend check and prints which backend(s) are configured. If none, prints the two-option setup hint (non-fatal — installer still exits 0). | §11 |
| P1-INST-06 | MUST | **Windows distribution via `winget`:** A `winget` manifest publishes `promptpal` whose installed binary is `promptpal.exe`, a thin Windows launcher that (a) checks for WSL Ubuntu, prompting the user to `wsl --install -d Ubuntu` if absent, and (b) forwards all args via `wsl -d Ubuntu -- promptpal "$@"`. The launcher contains no Anthropic logic. (D-5) | New §11 subsection (see §14) |
| P1-INST-07 | SHOULD | `uninstall.sh` removes the binary and prompts (`Yn`) before removing `~/.promptpal/`. With `--purge`, removes without prompting. | §11 |
| P1-INST-08 | SHOULD | First-run setup (`core/setup.py`) runs when `~/.promptpal/` is missing on first `promptpal` invocation; emits the SPEC §11 first-run summary. | §11 |

### 5.10 Error Handling (`P1-ERR-*`)

All errors go to **stderr**. Stdout is reserved for the improved prompt and `--output json` payloads. Mapping per SPEC §12, codified here for traceability.

| ID | Trigger | User Message | Exit |
|----|---------|--------------|------|
| P1-ERR-01 | `ANTHROPIC_API_KEY` unset with `--backend api-key` or after auto-resolution to API | Env-var instructions block (SPEC §10) | 1 |
| P1-ERR-02 | HTTP 401 from API | `"API key rejected. Check ANTHROPIC_API_KEY."` | 1 |
| P1-ERR-03 | HTTP 429 | `"Rate limited. Retrying in <n>s..."` then retry per P1-BKND-08 | — |
| P1-ERR-04 | Network error | `"Network error. Retrying..."` then retry once per P1-BKND-08 | 1 if final |
| P1-ERR-05 | HTTP 5xx | `"Anthropic API error. Retrying..."` with backoff | 1 if final |
| P1-ERR-06 | Corrupt `config.json` | `"Config file corrupt at ~/.promptpal/config.json. Delete it to reset."` | 1 |
| P1-ERR-07 | History write `OSError` | `"Warning: could not save session to history."` | — |
| P1-ERR-08 | Ctrl-C | `"\nCancelled."` | 130 |
| P1-ERR-09 | No backend available | Two-option setup block (SPEC §6) | 1 |
| P1-ERR-10 | `--backend claude-cli` forced, `claude` absent | `"Error: claude CLI not found on PATH. Install Claude Code first."` | 1 |
| P1-ERR-11 | CLI auth failure (non-zero + auth keyword) | `"Claude CLI auth failed. Run: claude auth login"` | 1 |
| P1-ERR-12 | WSL HOME regression | WSL launch fix message (SPEC §7) | 1 |
| P1-ERR-13 | No clipboard provider, `--copy` requested | `"Warning: no clipboard provider found. Install xclip or xsel."` | — |
| P1-ERR-14 | `--update-system-prompt` SHA-256 mismatch | `"System prompt checksum mismatch — refusing to overwrite. Verify Config.system_prompt_update_url."` | 1 |
| P1-ERR-15 | `--update-system-prompt` download failure | `"Could not fetch system prompt from <url>: <reason>"` | 1 |

---

## 6. Non-Functional Requirements

| ID | Category | Requirement |
|----|----------|-------------|
| NFR-01 | Performance | Time to first improved prompt: < 10 s on broadband for API backend, < 15 s for CLI backend (cold subprocess start). Measured from `promptpal` invocation to first stdout byte of improved prompt. |
| NFR-02 | Performance | Spinner/streaming has no measurable effect on improved-prompt latency (< 50ms overhead). |
| NFR-03 | Reliability | No data loss on Ctrl-C mid-turn: incremental session writes (P1-HIST-04) guarantee in-progress state is on disk. |
| NFR-04 | Reliability | Atomic writes mean a `kill -9` mid-write leaves either the old file intact or the new file complete — never a partial file. |
| NFR-05 | Security | API key never appears in: `config.json`, `usage.log`, history files, stdout, stderr, process arg list. |
| NFR-06 | Security | `--update-system-prompt` requires sha256 sidecar verification (D-3) — no overwrite on mismatch. |
| NFR-07 | Compatibility | Runs on macOS 13+, Ubuntu 22.04+, WSL2 Ubuntu 22.04+. Python 3.10+ required (stdlib only). |
| NFR-08 | Compatibility | All file output is UTF-8 with LF line endings. |
| NFR-09 | Observability | `--status` produces a single-screen summary suitable for `cat`/`tee` (< 20 lines). |
| NFR-10 | Maintainability | Test coverage ≥ 80% (`pytest --cov=core --cov-report=term-missing`). |
| NFR-11 | Maintainability | No third-party Python packages — stdlib only (D-1, D-2). |
| NFR-12 | Portability | Windows reachability is solely via the `winget` launcher → WSL2 → Linux binary. No native Windows code path. (D-5) |

---

## 7. Out of Scope (Phase 1)

The following are explicitly excluded and deferred to later phases. Implementations that touch these areas must be reviewed against scope before merge.

- **Tauri GUI** (Phase 2). `gui/` directory remains absent from Phase 1 releases.
- **Profiles** (`~/.promptpal/profiles/`). Directory not created; no flag wired.
- **MCP server mode** (Phase 2+).
- **Batch mode** (file of prompts unattended) — roadmap.
- **Comparison mode** (same prompt through two models/profiles).
- **Built-in prompt templates library**.
- **Team sync / cloud storage** (Phase 3).
- **Git integration** for accepted prompts.
- **Shell completion scripts** beyond what's in the repo (`completions/` may exist but is not installed by `install.sh` in Phase 1).
- **OpenAI / Gemini CLI backends** — Claude CLI only.
- **Homebrew tap, apt PPA, Snap, Chocolatey** — `winget` (D-5) + curl one-liner only for Phase 1.
- **Native Windows binary** — Windows is reached via WSL only (D-5).
- **Per-backend output normalization differences** — improved prompt format is identical regardless of backend.
- **Automatic `claude` CLI installation** — installer detects and instructs, never installs.

---

## 8. Acceptance Criteria

These are the executable, given/when/then criteria that gate Phase 1 release. They map to manual + automated test cases (§10).

### Backend Selection

- **AC-BKND-01.** Given `claude` is on `PATH` and `ANTHROPIC_API_KEY` is unset, when `promptpal "test"` runs, then `CliBackend` is used and the persisted turn has `backend: "claude-cli"`.
- **AC-BKND-02.** Given both `claude` is on `PATH` and `ANTHROPIC_API_KEY` is set, when `promptpal "test"` runs without `--backend`, then `CliBackend` is used (CLI precedence).
- **AC-BKND-03.** Given the same conditions as AC-BKND-02, when `promptpal --backend api-key "test"` runs, then `ApiBackend` is used, the turn records `backend: "api-key"`, and `config.preferred_backend` is updated to `"api-key"` after success (D-Q10).
- **AC-BKND-04.** Given `config.preferred_backend == "api-key"`, when `promptpal --backend auto "test"` runs, then `config.preferred_backend` is reset to `"auto"` and the auto-detection chain runs.
- **AC-BKND-05.** Given neither backend is available, when `promptpal "test"` runs, then exit code is 1, stdout is empty, and stderr contains both `Option 1 (Claude CLI):` and `Option 2 (API key):` lines.
- **AC-BKND-06.** Given `claude` auth has expired, when `promptpal "test"` runs in CLI mode, then stderr contains `"Claude CLI auth failed. Run: claude auth login"` and exit is 1.

### Multi-Turn via stream-json (D-7)

- **AC-MT-01.** Given a prompt containing the literal substring `"Human:"`, when refined through `CliBackend`, then the improved prompt is correct (no role-prefix confusion). This is the regression bait for the dropped `_build_prompt` flattening.
- **AC-MT-02.** Given 3 successive iterations through `CliBackend`, when each turn completes, then the `messages` array sent to `claude` on turn 3 contains all 5 prior entries (user, assistant, user, assistant, user) as NDJSON.

### WSL

- **AC-WSL-01.** Given the installer runs in WSL2 Ubuntu, when install completes, then `~/.promptpal/` is at `/home/<user>/.promptpal/`, not under `/mnt/c/`.
- **AC-WSL-02.** Given `xclip` and `xsel` are not installed in WSL, when `promptpal --copy "<UTF-8 with emoji 🚀>"` runs, then the prompt is on the Windows clipboard, byte-identical when pasted back (D-9 round-trip).
- **AC-WSL-03.** Given `HOME=/mnt/c/Users/sneak`, when `promptpal "test"` runs, then exit is 1 and stderr contains the `wsl -d Ubuntu` fix message.
- **AC-WSL-04.** Given any file written by PromptPal, when inspected with `file <path>`, then the output is `ASCII text` (no CRLF).

### Pipe / Quiet

- **AC-PIPE-01.** Given a non-TTY stdin, when `echo "prompt" | promptpal --quiet` runs, then stdout contains only the improved prompt (no spinner, no diff, no choice line), terminated by a single newline.
- **AC-PIPE-02.** Given `promptpal --output json "test"`, when the command completes, then stdout is a single JSON object that parses with `json.loads()` and contains the keys `original`, `improved`, `turns`, `session_id`, `backend`, `model`.

### System Prompt Update (D-3)

- **AC-SP-01.** Given `Config.system_prompt_update_url` is set, when `--update-system-prompt` runs and the SHA-256 sidecar matches, then `~/.promptpal/system-prompt.md` is atomically replaced and exit is 0.
- **AC-SP-02.** Given the same URL but a sidecar mismatch, when `--update-system-prompt` runs, then the existing file is unchanged and exit is 1 with the "checksum mismatch" message.

### Winget Launcher (D-5)

- **AC-WINGET-01.** Given a Windows host without WSL Ubuntu installed, when the user runs `winget install promptpal` and then `promptpal "test"`, then the launcher prints the `wsl --install -d Ubuntu` instruction and exits 1.
- **AC-WINGET-02.** Given a Windows host with WSL Ubuntu installed and PromptPal installed inside it, when the user runs `promptpal "test"` from PowerShell, then the call lands at the WSL `promptpal` binary and returns the improved prompt to the PowerShell stdout.

### Refinement Loop

- **AC-LOOP-01.** Given an accepted session, when the session JSON is inspected, then it contains `status: "accepted"` and `final_prompt` equals the assistant text of the last turn.
- **AC-LOOP-02.** Given Ctrl-C pressed during a `[i]terate` prompt, when the next `--show-history` runs, then the in-progress session is present with `status: "in-progress"`.

---

## 9. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Time to first improved prompt (API, broadband, warm) | < 10 s | Manual stopwatch across 10 runs (median) |
| Time to first improved prompt (CLI, cold subprocess) | < 15 s | Same as above |
| Refinement passes per session (P50) | ≥ 2 | Aggregate from `index.json` after 1 week of dogfooding |
| Install success on WSL2 Ubuntu 22.04 | 100% | `install.sh` from clean image, no errors |
| `claude`-already-installed users requiring API key setup | 0% | UX: `--status` shows `claude-cli` backend with no env var set |
| Auth error messages actionable without external docs | 100% | Every error in §5.10 contains a fix step in the same message |
| Test coverage | ≥ 80% | `pytest --cov=core` in CI |

---

## 10. Test Coverage Requirements

Inherits the test plan from SPEC §14. The following table maps Phase 1 requirements to test files. New tests required by the decisions in §4 are flagged **NEW**.

| Requirement | Test file | Notes |
|-------------|-----------|-------|
| P1-PIPE-01..03 | `tests/integration/test_stdin.py` | Cover arg, stdin, interactive paths |
| P1-PIPE-06 | `tests/unit/test_diff.py` | Short prompt → no diff; long → unified diff |
| P1-BKND-01..09 | `tests/unit/test_backend.py` | All `resolve_backend` paths |
| **P1-BKND-03** | `tests/unit/test_cli_backend_streamjson.py` **NEW** | Verify stdin NDJSON, `--bare --system-prompt-file --input-format=stream-json --output-format=stream-json` cmd vector (D-7, D-10). Replaces the obsolete `test_cli_backend_prompt_flattening` test. |
| **P1-BKND-13** | `tests/unit/test_backend_persistence.py` **NEW** | `--backend api-key` writes `preferred_backend`; `--backend auto` clears; failed call does not persist (D-Q10) |
| P1-LOOP-01..05 | `tests/integration/test_pipeline.py` | Mock backend, exercise loop branches |
| P1-FLAG-* | `tests/integration/test_flags.py` | One assert per flag |
| P1-SP-01..05 | `tests/unit/test_system_prompt.py` **NEW** | Bundled-default seed, no-overwrite on existing, sidecar verify, mismatch refusal (D-3) |
| P1-HIST-* | `tests/unit/test_history.py` | Atomic write, index upsert, eviction, search, incremental update |
| P1-CFG-* | `tests/unit/test_config.py` | Defaults, file override, CLI override, corrupt file, unknown-field tolerance |
| P1-PLAT-01..09 | `tests/unit/test_platform.py` | Per SPEC §14 + **NEW** UTF-8 clipboard round-trip test (D-9) |
| P1-ERR-* | Mixed in test_pipeline + test_backend | Each error path mapped |
| **AC-WINGET-01..02** | `tests/integration/test_winget_launcher.py` **NEW** (skipped unless `WSL_INTEGRATION=1`) | Smoke test from PowerShell host (D-5) |

CI matrix: Ubuntu 22.04, macOS 13, WSL2 Ubuntu 22.04 (self-hosted runner). Test the bash entrypoint, not just the Python core, so PATH and HOME guard regressions are caught.

---

## 11. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| `claude` CLI changes `--input-format=stream-json` event schema in a future release | Medium | High — multi-turn breaks | Pin a minimum `claude` version in `--status`; emit deprecation warning if older. Add an integration test that fails loudly on schema drift. |
| `clip.exe` UTF-8 behavior regresses on a future Windows update | Low | Medium — emoji prompts garbled | Manual smoke test in the release checklist (§13); D-9 verification re-run on each Windows major version. |
| Atomic write fails on a network-mounted `$HOME` (FUSE/NFS without `rename` atomicity) | Low | Medium — possible partial files | Document supported filesystems in install README; warn in `--status` if `$HOME` is on a non-POSIX FS. |
| `winget` launcher diverges from the WSL binary (D-5) | Medium | High — version skew | The launcher is generated from a template and version-locked in CI; the WSL binary's version is queried on launcher start and a stderr warning is emitted on mismatch. |
| SHA-256 sidecar URL is unreachable but the prompt URL works | Low | Medium — failed updates | `--update-system-prompt` requires *both* URLs to succeed; partial success is treated as full failure (no partial overwrite). |
| Users edit `~/.promptpal/system-prompt.md` then run `--update-system-prompt` and lose their edits | Medium | Low | Before atomic replace, copy current file to `~/.promptpal/system-prompt.md.bak` and print the backup path. |
| The "single file per session" pattern accumulates many small files | Low | Low | `Config.max_history_entries` default 500 caps the directory size; eviction is automatic (P1-HIST-06). |

---

## 12. Dependencies and Constraints

### Runtime Dependencies (target machines)

- `bash` 4+
- `python3` ≥ 3.10 (stdlib only — D-1, D-2)
- `curl` or `wget` (installer + `--update-system-prompt`)
- One of: `claude` CLI on `PATH` **or** `ANTHROPIC_API_KEY` set
- Optional: `xclip`, `xsel`, `pbcopy`, or `clip.exe` (clipboard)

### Build/Test Dependencies (developer machines)

- `pytest`, `pytest-cov` (testing)
- `shellcheck` (bash entrypoint + install.sh linting)
- `pyright` or `mypy` (type checking — informational, not gating)

### Constraints

- No third-party Python packages may be added to the runtime path (D-1).
- No `jq` (D-2).
- No native Windows code (D-5).
- The system prompt file is treated as user data — never overwritten silently (P1-SP-02).
- The API key is treated as a secret with strict redaction rules (NFR-05).

---

## 13. Release Checklist

Pre-release gates that must pass before a Phase 1 GA tag.

- [ ] All `MUST` requirements in §5 have at least one passing test.
- [ ] `pytest --cov=core` reports ≥ 80% coverage.
- [ ] `shellcheck bin/promptpal install.sh` returns clean.
- [ ] Manual matrix (one row per row of the test matrix in §10) executed end-to-end:
  - [ ] macOS 13 + API backend
  - [ ] Ubuntu 22.04 + CLI backend
  - [ ] WSL2 Ubuntu 22.04 + CLI backend + `clip.exe` UTF-8 round-trip (D-9)
  - [ ] WSL2 Ubuntu 22.04 + API backend (force via `--backend api-key`)
  - [ ] Windows 11 host + `winget install promptpal` + WSL launcher (D-5)
- [ ] `promptpal --status` rendered output reviewed for clarity.
- [ ] Every error message in §5.10 manually triggered and verified to contain a fix instruction.
- [ ] `--update-system-prompt` exercised against the canonical URL with both a matching and a mismatching sidecar.
- [ ] `--backend auto` clears `preferred_backend`; `--backend <name>` persists it (D-Q10).
- [ ] Atomic-write fault injection: `kill -9` mid-session-write leaves no partial files in `~/.promptpal/history/`.
- [ ] No occurrences of `ANTHROPIC_API_KEY` value in any file under `~/.promptpal/` after a full session.
- [ ] README installation snippet verified working on a clean WSL Ubuntu image.
- [ ] `winget` manifest validated locally via `winget validate`.

---

## 14. SPEC Amendments Triggered by This PRD

The decisions in §4 require the following targeted edits to `SPEC.md`. `SPEC.md` is a living document (per project convention) and is edited in place, not via amendment files.

| SPEC section | Change | Driver |
|--------------|--------|--------|
| §6 — Claude CLI Backend | Remove `_build_prompt` and the `Human:`/`Assistant:` flattening helper. Rewrite `CliBackend.complete()` to invoke `claude --print --model <m> --bare --system-prompt-file <path> --input-format=stream-json --output-format=stream-json`, piping the `messages` array as NDJSON on stdin and consuming assistant deltas from stdout. | D-7, D-10 |
| §6 — Test list `test_cli_backend_prompt_flattening` | Remove. Replace with `test_cli_backend_streamjson` covering the NDJSON pipe and command vector. | D-7 |
| §10 — Backend Selection | Add: "When `--backend <claude-cli\|api-key>` is passed, write the value to `Config.preferred_backend` after the first successful turn. When `--backend auto` is passed, write `\"auto\"` immediately and skip persistence on subsequent turns." | D-Q10 |
| §11 — First-run setup + install.sh | Replace `cp defaults/system-prompt.md` with `cp core/system_prompt.txt`. Add a "Windows via WSL" subsection describing the `winget` launcher pattern (D-5). | D-3, D-5 |
| §11 — `--update-system-prompt` | Document the sha256 sidecar verification flow and the "no overwrite on mismatch" rule. | D-3 |
| §16 — Open Questions | Mark Q1, Q2, Q3, Q4, Q5, S-1, S-2, S-3, S-4 as **resolved** with a one-line pointer to the matching D-row in this PRD's §4. | All |

These amendments should land in the same PR as the implementation of the corresponding requirements, not as a separate doc-only PR — that keeps the SPEC and the code in lockstep.

---

*End of Phase 1 PRD.*
