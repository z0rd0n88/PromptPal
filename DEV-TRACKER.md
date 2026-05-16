# PromptPal — Developer Progress Tracker

**Last updated:** 2026-05-15
**Active branch:** `chore/spec-open-questions` (worktree: `.worktrees/spec-open-questions`) — **PR [#1](https://github.com/z0rd0n88/PromptPal/pull/1) open**, addressing reviewer feedback in-flight
**Phase:** Specification → about to enter Phase 1 implementation

This tracker is the single source of truth for what is decided, what is in flight, and what is blocked. Update it on every PR that lands in `main`. The authoritative technical detail still lives in `SPEC.md`; this file tracks execution against it.

---

## Status Legend

| Icon | Meaning |
|------|---------|
| ✅ DONE | Merged to `main`; verified |
| 🟢 IN PROGRESS | Active branch; PR open or imminent |
| ⏸️ BLOCKED | Cannot proceed until a listed blocker clears |
| 📋 PLANNED | Scoped, not yet started |
| ❌ DROPPED | Considered and rejected; see notes |

---

## Decisions Log

Decisions made during the spec-open-questions session. Each row corresponds to a question in `SPEC.md` §16 (Open Questions) or a finding from research.

| ID | Topic | Outcome | Date | Source |
|----|-------|---------|------|--------|
| D-01 | GUI timeline (Q4) | Phase 2 starts after CLI is stable on both backends. No calendar gate. | 2026-05-15 | `SPEC.md` §16 Q4 |
| D-02 | Distribution channel (Q5) | Windows / winget first. Signed `.msi` or `.exe`. WSL Ubuntu is the supported runtime; Windows-side launcher shells into `wsl -d Ubuntu -- promptpal`. Homebrew / apt / Snap deferred. | 2026-05-15 | `SPEC.md` §16 Q5 |
| D-03 | `preferred_backend` persistence (Q10 / S-4) | Persist when `--backend <name>` is passed; atomic write; `--backend auto` clears the field. | 2026-05-15 | `SPEC.md` §16 Q10, S-4 |
| D-04 | CLI runtime (Q1) | Stay with bash + python3. Python stdlib only. Bash entrypoint stays a thin shim (PATH, HOME guard, bootstrap). | 2026-05-15 | `SPEC.md` §16 Q1 |
| D-05 | `jq` dependency (Q2) | No `jq`. All JSON handling in Python. | 2026-05-15 | `SPEC.md` §16 Q2 |
| D-06 | System prompt source (Q3) | Bundle as `core/system_prompt.txt` in the Python package. `--update-system-prompt` fetches from `https://raw.githubusercontent.com/z0rd0n88/PromptPal/main/core/system_prompt.txt` and verifies against a sha256 published alongside (`core/system_prompt.sha256`). | 2026-05-15 | `SPEC.md` §16 Q3 |
| D-07 | Multi-turn flags (Q7 / S-1) | RESOLVED. Claude CLI supports native multi-turn via `--input-format=stream-json --output-format=stream-json` with `-p`. `CliBackend` uses stream-json roundtrip. NO `_build_prompt` flattening. | 2026-05-15 | `SPEC.md` §16 Q7, S-1 |
| D-08 | Model flag (Q8 / S-2) | RESOLVED. `claude --model claude-sonnet-4-6` works. Same alias and full-name strings as the API. Probe verified end-to-end. | 2026-05-15 | `SPEC.md` §16 Q8, S-2 |
| D-09 | `clip.exe` UTF-8 (Q9 / S-3) | RESOLVED. `clip.exe` accepts UTF-8 directly without a BOM. Verified via `printf '... αβγ 中文 emoji 🎉 ...' \| clip.exe` → `Get-Clipboard` round-trip. | 2026-05-15 | `SPEC.md` §16 Q9, S-3 |
| D-10 | `claude -p` default system prompt | NEW finding. `claude -p` ships a ~28k-token default Claude Code system prompt (~$0.10 per trivial call). `CliBackend` MUST pass `--bare` and `--system-prompt-file <our-prompt>` to override. Track as a hard requirement on the CLI backend. | 2026-05-15 | Session research; folds into `SPEC.md` §6 (Backend Integration) |

---

## Phase 1 — CLI MVP

Phase ordering is dependency-driven. Earlier phases unblock later ones; do not reorder without updating the table.

### Phase 1.0 — Repo bootstrap

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| Confirm `.worktrees/` is gitignored on `main` | unassigned | 📋 PLANNED | — | Verify via subpath probe per CLAUDE.md guidance |
| Create `core/`, `bin/`, `tests/` skeleton | unassigned | 📋 PLANNED | — | Match `SPEC.md` §3 (Directory Layout) |
| Add `pyproject.toml` (stdlib-only, no runtime deps) | unassigned | 📋 PLANNED | — | Per D-04; pin `python_requires = ">=3.10"` |
| Wire `ruff` + `pytest` config | unassigned | 📋 PLANNED | — | Dev deps only |
| `.gitignore` for `__pycache__/`, `.pytest_cache/`, build artifacts | unassigned | 📋 PLANNED | — | — |

### Phase 1.1 — WSL guard + platform detection

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| `core/platform.py` — WSL detection via `/proc/sys/kernel/osrelease` | unassigned | 📋 PLANNED | — | `SPEC.md` §7.1, §7.2 |
| HOME guard — exit 1 on `HOME=/mnt/c/...` with fix instructions | unassigned | 📋 PLANNED | — | Mirrors global CLAUDE.md HOME rule |
| Clipboard provider selection (xclip → xsel → pbcopy → clip.exe → none) | unassigned | 📋 PLANNED | — | `SPEC.md` §7.3; `clip.exe` is UTF-8 safe per D-09 |
| Unit tests for each platform branch (mock `os.uname` + filesystem) | unassigned | 📋 PLANNED | — | — |

### Phase 1.2 — Backend abstraction

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| `core/backend.py` ABC: `Backend.complete(system, messages) -> str` | unassigned | 📋 PLANNED | — | `SPEC.md` §6.1 |
| `resolve_backend()` auto-detection (CLI on PATH → API key in env → exit 1) | unassigned | 📋 PLANNED | — | `SPEC.md` §6.2 |
| Honor `--backend` flag and `preferred_backend` config (D-03) | unassigned | 📋 PLANNED | — | Atomic config write on persist |
| Tests: missing-both, both-present, override paths | unassigned | 📋 PLANNED | — | — |

### Phase 1.3 — API backend

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| `core/api_backend.py` — Anthropic Messages POST | unassigned | 📋 PLANNED | — | `SPEC.md` §6.3 |
| SSE response parser | unassigned | 📋 PLANNED | — | Stdlib only; no `httpx`/`requests` |
| Retry with exponential backoff (429, 5xx) | unassigned | 📋 PLANNED | — | Cap at N retries; surface final error |
| Usage logging to `~/.promptpal/usage.log` | unassigned | 📋 PLANNED | — | `SPEC.md` §4.4 |
| Tests: success, 429 retry, 5xx retry, fatal 4xx, network error | unassigned | 📋 PLANNED | — | Mock `urllib` |

### Phase 1.4 — CLI backend

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| **PRECONDITION SPIKE: `--bare` refinement quality** | unassigned | ⏸️ BLOCKED | Phase 1.8 task "Author `core/system_prompt.txt` v0.1" must produce a draft prompt first | Run `claude -p --bare --system-prompt-file <draft>` on 5–10 representative refinement inputs; capture cost (target: cents, not dimes) AND output quality vs. an `ApiBackend` baseline. Document results in this row. Implementation cannot start until quality is judged acceptable. |
| `core/cli_backend.py` — `claude -p` subprocess | unassigned | 📋 PLANNED | Spike above | `SPEC.md` §6.4 |
| Use `--input-format=stream-json --output-format=stream-json` (D-07) | unassigned | 📋 PLANNED | Spike above | NO prompt flattening; envelope shape per `SPEC.md` §6.4 |
| Pass `--bare --system-prompt-file <path>` (D-10) | unassigned | 📋 PLANNED | Spike above | Hard requirement; cost-critical |
| Pass `--model` through unchanged (D-08) | unassigned | 📋 PLANNED | — | Same strings as API |
| Pass `--no-session-persistence` to keep state in-memory only | unassigned | 📋 PLANNED | — | Avoids polluting user's own `claude --resume` history |
| Tests: stream-json roundtrip, `--bare` flag assertion, system-prompt-file path assertion, model passthrough, subprocess error, error-envelope parsing | unassigned | 📋 PLANNED | — | Per `SPEC.md` §14 test rows |

### Phase 1.5 — Refinement loop + diff

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| `core/improve.py` — refinement state machine | unassigned | 📋 PLANNED | — | `SPEC.md` §8.1 |
| `messages` array management (full array sent every turn) | unassigned | 📋 PLANNED | — | `SPEC.md` §8.2; no truncation in Phase 1 |
| `core/diff.py` — colorized terminal diff between turns | unassigned | 📋 PLANNED | — | `difflib` from stdlib |
| Tests: single-turn, multi-turn, abort mid-turn | unassigned | 📋 PLANNED | — | — |

### Phase 1.6 — History persistence

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| `core/history.py` — atomic write (`tempfile.mkstemp` → `os.rename`) | unassigned | 📋 PLANNED | — | `SPEC.md` §9.1 |
| `index.json` maintenance (append, evict, dedupe) | unassigned | 📋 PLANNED | — | `SPEC.md` §9.2, §9.3 |
| Search implementation | unassigned | 📋 PLANNED | — | `SPEC.md` §9.4 |
| Tests: concurrent writes, crash mid-write, index rebuild from session files | unassigned | 📋 PLANNED | — | — |

### Phase 1.7 — Bash entrypoint

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| `bin/promptpal` — PATH integration, HOME guard, bootstrap | unassigned | 📋 PLANNED | — | `SPEC.md` §5.1; thin shim only |
| Verify shebang + chmod in install path | unassigned | 📋 PLANNED | — | — |
| Smoke test on Ubuntu (WSL) and on a clean container | unassigned | 📋 PLANNED | — | — |

### Phase 1.8 — Bundled system prompt + `--update-system-prompt`

> ⚠️ **Phase 1.4 (CLI backend) and Phase 1.4's `--bare` quality spike both depend on the v0.1 prompt content existing.** This phase has effective Phase 0 priority — schedule it alongside Phase 1.0/1.1, not after Phase 1.7.

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| **Author `core/system_prompt.txt` v0.1 — actual prompt content** | unassigned | 📋 PLANNED | — | Per D-06. This is the prompt-engineering task itself, not the file plumbing. Owner needs prompt-engineering judgment, not just code. Iterate with the Phase 1.4 spike. |
| Generate `core/system_prompt.sha256` and check both into the package | unassigned | 📋 PLANNED | Prompt content above | Trivial once content is locked |
| `--update-system-prompt` fetcher with sha256 verification | unassigned | 📋 PLANNED | — | Atomic replace on success only |
| Tests: hash mismatch refuses to replace, network error keeps existing prompt, fetched file matches sha256 sidecar | unassigned | 📋 PLANNED | — | — |

### Phase 1.9 — Tests (unit, integration, E2E)

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| Unit tests across all modules | unassigned | 📋 PLANNED | — | `SPEC.md` §13.1 |
| Integration tests for full refinement pipeline (mocked backend) | unassigned | 📋 PLANNED | — | `SPEC.md` §13.2 |
| E2E test: real `claude` CLI on a fixture prompt | unassigned | 📋 PLANNED | Requires CI runner with `claude` installed | `SPEC.md` §13.4 |
| Coverage gate: 80%+ per `~/.claude/rules/common/testing.md` | unassigned | 📋 PLANNED | — | Block merges below threshold |

### Phase 1.10 — Packaging (winget + Windows launcher)

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| Build signed `.msi` or `.exe` installer | unassigned | 📋 PLANNED | Code-signing cert | Per D-02 |
| Windows launcher: `wsl -d Ubuntu -- promptpal "$@"` passthrough | unassigned | 📋 PLANNED | — | Argument quoting matters |
| **Revise `SPEC.md` §15 (Build and Release) for winget + signing pipeline** | unassigned | 📋 PLANNED | — | The current `package.sh` tarball workflow in §15 predates D-02. Replace with: signed-installer build steps, winget manifest format, submission flow. |
| winget manifest authoring | unassigned | 📋 PLANNED | §15 revision | — |
| Submit to `microsoft/winget-pkgs` | unassigned | 📋 PLANNED | Awaiting installer | Review timeline unknown — see Open Risks |

---

## Phase 2 — Tauri GUI

| Task | Owner | Status | Blocker | Notes |
|------|-------|--------|---------|-------|
| Phase 2 entire scope | unassigned | 📋 PLANNED | Phase 1 must be stable on both backends (D-01) | `SPEC.md` §11; placeholder until Phase 1 ships |

---

## Open Risks

Distinct from resolved questions. Active threats to delivery.

- **claude.exe MSIX migration regressions** — Per global CLAUDE.md, `HOME=/c/Users/sneak` regressions break the launch contract. Any test runner or CI agent that picks up the deprecated MSIX entry point will fail the HOME guard. Mitigation: canonical entry point is the WSL-native `claude` binary; document in install.sh.
- **`--bare` + custom system prompt — refinement quality unverified** — D-10 fixes the cost issue, but we have not yet measured refinement quality against the stripped-down prompt. Spike a refinement on a representative input before committing to `--bare`.
- **SSE error-recovery edge cases (API backend)** — Partial event streams, mid-stream disconnects, and idle timeouts are not covered by the current test sketch. Add fault-injection tests in Phase 1.3.
- **winget review timeline unknown** — `microsoft/winget-pkgs` review can take days to weeks. Cannot commit to a public-launch date until first manifest is accepted.

---

## Follow-ups (track, don't block this PR)

Surfaced by review feedback on PR #1. Track but do not gate this PR on them.

| ID | Item | Why now-but-not-blocking | Owner | Status |
|----|------|--------------------------|-------|--------|
| F-01 | Declare `claude` CLI version floor (≥ 2.1.143) | Native stream-json multi-turn was verified on 2.1.143; older versions may lack `--input-format=stream-json` or `--bare`. Decide: hard-block on too-old, or warn-and-proceed. Ship in Phase 1.4. | unassigned | 📋 PLANNED |
| F-02 | Decide governance for decisions log: canonicalize this DEV-TRACKER OR backfill `docs/adr/0001-…md` files | Currently silently forked: the spec body, §16 resolutions, and DEV-TRACKER decisions log can drift. Pick one source of truth before D-11+ accumulate. | unassigned | 📋 PLANNED |
| F-04 | Re-litigate D-03 (`preferred_backend` silent persistence) UX once users exercise `--backend` | Reviewer flagged that silent persistence is a UX trap; we mitigate with a one-line confirmation print, but watch for confusion in early users. Revisit before public-launch. | unassigned | 📋 PLANNED |

(Item 10 from the review — adding a Phase 1.10 row for the §15 revision — is now an in-line task, see Phase 1.10 above.)

---

## Next Concrete Actions

Pick up immediately. Each item is independently mergeable.

1. ~~Commit the §16 resolutions onto `chore/spec-open-questions`.~~ ✅ **DONE** (commit `03f288b`)
2. ~~Open PR for `chore/spec-open-questions` → `main`.~~ ✅ **DONE** ([PR #1](https://github.com/z0rd0n88/PromptPal/pull/1))
3. Address PR #1 review feedback in a follow-up commit on the same branch (this commit). Specifically: fix §10 to match D-03; rewrite §6 CliBackend pseudocode for stream-json + `--bare`; canonicalize `core/system_prompt.txt` path across §3, install.sh, defaults; replace stale §14 test row.
4. Author `core/system_prompt.txt` v0.1 content and run the Phase 1.4 `--bare` quality spike against it before any CLI-backend implementation work begins.
5. Verify `.worktrees/` is gitignored on `main` via subpath probe; add the entry if absent before the next worktree is created.
