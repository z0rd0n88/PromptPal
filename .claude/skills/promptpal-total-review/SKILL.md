---
name: promptpal-total-review
description: Multi-agent codebase review for PromptPal (stdlib-only Python, dual backends). Triggers on /promptpal-total-review.
---

# promptpal-total-review

Project wrapper for the shared [`total-review`](https://github.com/z0rd0n88/ClaudeConfig/blob/main/skills/total-review/SKILL.md) pattern (lives at `~/.claude/skills/total-review/`).

Follow `~/.claude/skills/total-review/REFERENCE.md` step-by-step using this directory's [`config.yml`](config.yml). **Read `<repo>/ARCH.md` first** — it is the authoritative file-tree map.

## PromptPal-specific notes

These supplement the global REFERENCE.md guidance; reviewers should treat them as additional invariants on top of what `config.yml` carries.

- **Stdlib-only core (PRD D-1/D-2)** — `core/` may not import any third-party package. No `requirements.txt`, no `pyproject.toml` runtime deps. New imports of non-stdlib modules in `core/` are a bug; reviewers should port or inline instead.
- **claude-cli stream-json contract** — `core/cli_backend.py`'s module docstring records hard-won quirks: `--bare` breaks OAuth, `--verbose` is required, failures land on stdout with empty stderr, and message `content` MUST be a block array (`[{"type":"text","text":...}]`) — never a bare string, or multi-turn input crashes the parser. Any edit that touches `claude` argv, stream-json I/O, or content shape gets a CRITICAL on regression.
- **Requirement-ID pinning** — tests and docstrings carry IDs like `P1-LOOP-01`, `P1-CFG-03` from `PRD-PHASE1.md`. Preserve them on edits; orphaned or duplicated IDs are a doc-mode finding.
- **Atomic I/O discipline** — `core/_io.py` provides atomic write helpers used by `config.py`, `history.py`, `system_prompt.py`. Direct `open(..., 'w')` writes to user-state files bypass the atomic guarantee; flag as silent-failure risk.
- **Installed-snapshot drift** — `promptpal` runs from `~/.promptpal/lib/`, not the repo. Changes to `core/` are invisible until `install.sh` re-runs. Architecture-mode reviewers should flag mechanisms that assume "running from repo" (path resolution, hot-reload, etc.).
- **Commit message convention** — features carry `feat: US-NNN` referencing the PRD user story. Architecture/cleanup commits that touch invariants without an ADR get a docs-mode finding.
- **Tests via `uv run`** — pytest is not installed system-wide. Anything that hardcodes `pytest` or `python -m pytest` (instead of `uv run --with pytest python -m pytest`) is broken on a fresh clone.

## Adversaries to model in `security` mode

In addition to OWASP A01–A10:
- **Prompt-injection via raw input** — the user-supplied prompt is concatenated with the system prompt and forwarded to the backend; reviewers should check for injection escape paths (especially in the iterate loop, where the model's output re-enters the next turn).
- **Filesystem traversal in user-state paths** — `~/.promptpal/` contents (history, config, system-prompt overrides) are user-writable. Any path joined under there with attacker-influenced filenames needs traversal checks.
- **Untrusted JSON from external CLIs** — `claude` stream-json output is parsed line-by-line by `_parse_stream_json`, which **drops unparseable lines by design** (fail-open on individual NDJSON lines so a single bad line doesn't abort response assembly). The adversary therefore is: *a hostile line must not raise out of the parser AND must not inject into the assembled assistant output*. Additionally, the request side: a malformed envelope (e.g. bare `{"role": ...}` instead of `{"type": role, "message": {...}}`) produces a **silent success** — `claude` exits 0 with empty assistant output and no error code. Reviewers should flag any code path that treats exit 0 + empty output as "model declined" rather than "envelope shape regressed".
- **Env-var injection into subprocess argv** — config-driven flags piped into `claude` argv must not enable shell metacharacter abuse.
- **HOME-hijack via WSL/Windows path bleed** — the `bin/promptpal` launcher guards against a misrouted `HOME`; bypasses there leak credentials into `/mnt/c/...`.

## Mode quick-start

| Mode | What it does |
|---|---|
| `code` | Correctness + atomicity + idiom + typing — files issue |
| `cleanup` | Dead code, duplication, unused helpers — files issue |
| `security` | OWASP + prompt-injection + path-traversal + adversaries above — files issue |
| `architecture` | Backend abstraction integrity, cli↔core boundary, silent-failure ladder — files issue |
| `test` | Coverage + fake parity (api/cli backend fakes), requirement-ID pinning — files issue |
| `perf` | Subprocess startup overhead, NDJSON parse hot path, history-index O(n) growth — files issue |
| `docs` | `ARCH.md` / PRD / `CLAUDE.md` drift — inline patch or small issue |
| `pre-pr` | Diff-only sanity check before opening a PR — inline summary, no issue |

`all` = every mode except `pre-pr`, one issue per mode.

## Notes on agent availability

- `ecc-code-explorer` and `ecc-code-architect` are parked at user scope as of 2026-06-07 and activated in this project under `.claude/agents/`. They light up automatically wherever `code-explorer` / `code-architect` appear in mode defaults or slice `lenses`.
- Two PromptPal-specific lenses are activated: `cli-backend-reviewer` (the `claude` argv + stream-json layer) and `history-persistence-reviewer` (the `~/.promptpal/history` writer). They join `code` and `architecture` modes additively via `agents_add` in `config.yml`; per-slice `lenses` allowlists decide where they run. `cli-backend-reviewer` also appears in `tests.lenses` to catch fake-parity drift between the production backends and their `tests/` fakes.
- **Tool-capability split:** fix-capable agents (`python-pro`, `ecc-tdd-guide`, `ecc-refactor-cleaner`, `unused-code-cleaner`) deliberately retain `Write` / `Edit` so they can author tests and execute refactors in fix-pass dispatches, while pure-review agents (`ecc-security-reviewer`, `ecc-performance-optimizer`, `ecc-python-reviewer`, `ecc-silent-failure-hunter`, `critical-thinking`) are stripped to read-only.

## Slice quick-reference

The `config.yml` declares seven layer slices:

- `cli` — `core/cli.py`, `core/main.py`, `core/__init__.py`, **`core/resolve.py`** (moved here from `state` 2026-06-07; it's a path resolver, not a state writer). Lenses include `silent-failure-hunter` (owns the exit-code / error funnel) and `code-architect`.
- `backends` — both backend adapters + the abstract base. The fragile part per CLAUDE.md.
- `state` — config + history + system-prompt + atomic I/O (everything writing to `~/.promptpal/`).
- `loop` — interactive accept/iterate/discard, diff renderer, input parser. `silent-failure-hunter` joins here for parity with `backends`/`state`.
- `platform` — platform detection + winget launcher wiring.
- **`launchers_shell`** — bash launcher + installer/uninstaller + Windows shell launchers (`bin/promptpal`, `install.sh`, `uninstall.sh`, `launcher/promptpal.cmd`, `launcher/promptpal.ps1`). Split off from the old monolithic `launchers` slice 2026-06-07.
- **`packaging`** — winget manifests + the Python entry-point shim (`promptpal_main.py`). Other half of the old `launchers` split — distinct lens set (`python-reviewer` joins for the shim).
- `tests` — narrow lens set + `cli-backend-reviewer` for fake-parity drift coverage.

## No-SQL note

PromptPal has no SQL. `sql-pro` is intentionally omitted from the agent set and from every slice's `lenses` allowlist. Do not re-add unless a future feature introduces a relational store.
