---
name: xan-multi-agent-review
description: Run a parallel multi-perspective review (architect, critical-thinking, silent-failure, security by default) over a PR, directory, file, or spec-doc set, then merge findings via a synthesizer agent.
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - Agent
---

# xan-multi-agent-review

## 1. Purpose

Run a fan-out / fan-in review over a single target. The skill spawns N reviewer agents in parallel, each with a self-contained brief containing the same materials and severity rubric. After all reviewers return, a synthesizer agent merges their reports into one prioritized review document. The skill prints the synthesized report verbatim; reviewers' raw outputs are not surfaced separately.

The skill itself does not produce findings — it orchestrates agents and relays their output. It does not execute the recommendations.

## 2. When to use

Trigger phrases:
- "review this PR" / "audit PR <n>"
- "review/audit this directory" / "review `core/`"
- "review this file" / "review `<path>`"
- "review the spec" / "review the spec amendment" / "review SPEC.md and the update"
- "do a multi-agent review of …"
- explicit invocation: `xan-multi-agent-review …`

Do NOT use for:
- Single-pass review by one specific agent (call the agent directly).
- Anything that would write to source files — this skill is read-only by default.

## 3. Invocation grammar

```
xan-multi-agent-review <target-type> <target-args...> [flags]
```

Examples:

```
xan-multi-agent-review pr 42
xan-multi-agent-review pr https://github.com/owner/repo/pull/42
xan-multi-agent-review dir core/
xan-multi-agent-review file SPEC.md
xan-multi-agent-review spec SPEC.md SPEC-UPDATE-001-foo.md PRD.md

# Reviewer override (comma-separated, no spaces):
xan-multi-agent-review pr 42 --reviewers architect-reviewer,security-reviewer,python-reviewer

# Synthesizer override:
xan-multi-agent-review dir core/ --synthesizer code-reviewer

# Opt-in file output:
xan-multi-agent-review pr 42 --write-to docs/reviews/pr-42-review.md
```

| Position / flag | Required | Values |
|---|---|---|
| `<target-type>` (positional 1) | yes | `pr` \| `dir` \| `file` \| `spec` |
| `<target-args>` (positional 2..N) | yes | per type: PR number/URL; directory path; one file path; one or more markdown paths |
| `--reviewers <csv>` | no | comma-separated agent names; default = the v2 roster (see §4) |
| `--synthesizer <name>` | no | single agent name; default = `knowledge-synthesizer` |
| `--write-to <path>` | no | path to also save the final report; default = stdout only |
| `--force` | no | with `--write-to`, allow overwriting an existing file |
| `--max-files <n>` | no | override the directory file-count cap (default 50) |

## 4. Defaults

- **Reviewer roster** (when `--reviewers` is omitted, in this order):
  1. `architect-reviewer`
  2. `critical-thinking`
  3. `silent-failure-hunter`
  4. `security-reviewer`
- **Synthesizer**: `knowledge-synthesizer`
- **Directory file-count cap**: 50 (override with `--max-files <n>`)
- **PR diff size warning threshold**: 100 KB (warn on stderr, do not auto-truncate)
- **`--write-to` overwrite policy**: refuse if destination exists, unless `--force` is also set

## 5. Workflow

Execute these steps in order on every invocation:

### 5.1 Parse and validate

1. Parse positional args + flags per §3.
2. Hard-fail with a clear error and exit if:
   - `<target-type>` is missing or not one of `pr|dir|file|spec`.
   - `<target-args>` don't match the type (e.g., `pr` with no number/URL; `file` with more than one path).
   - `--reviewers` is present but empty, or contains whitespace inside the CSV.
3. If `--reviewers` is omitted → use the default roster from §4.
4. If `--synthesizer` is omitted → use `knowledge-synthesizer`.
5. Validate every reviewer name AND the synthesizer name against the union of `.claude/agents/*.md` (project) and `~/.claude/agents/*.md` (user). Unknown name → hard-fail before spawning anything, with the list of known agent names.

### 5.2 Resolve materials per target type

Each branch produces a **materials block** — a self-contained string that every reviewer brief will embed verbatim. Reviewers receive NO shared conversation state, so the materials block is the only way they see the target.

| Target type | Resolution steps |
|---|---|
| `pr <n\|url>` | If `gh` is not on PATH → hard-fail with install instructions. Run `gh pr view <ref> --json number,title,body,headRefName,baseRefName,files,author,url`. Run `gh pr diff <ref>`. From the JSON, extract each changed file's path and resolve to absolute (relative to `git rev-parse --show-toplevel`). If the diff exceeds 100 KB, emit a single-line warning to stderr and proceed without truncation. Materials block contains: PR metadata table, full diff, and the file list with absolute paths and an explicit instruction to read each in full. |
| `dir <path>` | Resolve to absolute. Run `git ls-files <path>` (respects `.gitignore`). If the file count exceeds the cap (default 50, override via `--max-files`), abort with a message listing the count and the cap; do not proceed. Materials block contains: the directory's absolute path, the file list with absolute paths, and an instruction to read each in full. |
| `file <path>` | Resolve to absolute. Verify the file exists and is readable. Reject if more than one path is given. Materials block contains: the absolute path and an instruction to read it in full. |
| `spec <p1> [<p2> ...]` | Resolve each path to absolute. Verify each exists and is readable. Materials block contains: the list of absolute paths and an instruction to read each in full. |

### 5.3 Build reviewer briefs

For each reviewer in the roster, expand the [Reviewer brief template](#7-reviewer-brief-template) with the values resolved above. The brief must be self-contained — the agent will not see this skill's conversation.

For `{{REVIEWER_ROLE_DESCRIPTION}}`:
1. Attempt to read `.claude/agents/<reviewer-name>.md` (project scope).
2. If absent, try `~/.claude/agents/<reviewer-name>.md` (user scope).
3. If both absent (e.g., built-in agent) → use the literal fallback string: `"Apply your standard review lens to the materials above."`
4. If found → use the first paragraph of the agent definition's body (everything after the closing `---` of the frontmatter, up to the first blank line).

### 5.4 Spawn reviewers IN PARALLEL

> ⚠️ **PARALLELISM RULE — DO NOT SERIALIZE**
>
> All reviewer `Agent` tool calls MUST be issued in a SINGLE assistant message containing N parallel tool invocations. Do NOT spread them across multiple turns. If the roster has N reviewers, the orchestrator message contains exactly N `Agent` tool uses, all dispatched together.
>
> Each call MUST set:
> - `subagent_type` to the reviewer's agent name.
> - `description` to `"Multi-agent review: <reviewer-name>"`.
> - `prompt` to the fully-expanded reviewer brief (see §7).

### 5.5 Collect outputs

After all reviewers return, capture each reviewer's output verbatim, keyed by reviewer name in the order they appeared in the roster. Do not edit, summarize, reformat, or filter at this stage.

### 5.6 Invoke synthesizer

Expand the [Synthesizer brief template](#8-synthesizer-brief-template) with:
- `{{SYNTHESIZER_NAME}}` = the resolved synthesizer (default `knowledge-synthesizer`).
- `{{N_REVIEWERS}}` = the roster size.
- `{{TARGET_TYPE}}` = the resolved target type.
- `{{MATERIALS_BLOCK}}` = the same materials block from §5.2.
- `{{REVIEWER_OUTPUTS}}` = each reviewer's verbatim output, with a delimiter and the reviewer name as a header (see template).
- `{{TARGET_LABEL}}` = a human-readable label for the target (e.g., `PR #42: <title>`, `directory core/`, `file SPEC.md`, `spec set: SPEC.md + SPEC-UPDATE-001-foo.md`).

Spawn the synthesizer as a single `Agent` tool call with `subagent_type` = the synthesizer name and `description` = `"Multi-agent review: synthesize"`.

### 5.7 Print verbatim

Print the synthesizer's output as the final assistant message, verbatim, with NO preamble and NO postscript — **except** when `--write-to <path>` is set, in which case prepend exactly one line: `Saved to: <absolute path>`.

When `--write-to <path>` is set:
1. Resolve `<path>` to absolute (relative to `git rev-parse --show-toplevel` if not already absolute).
2. If the destination file exists AND `--force` is NOT set → hard-fail BEFORE invoking the synthesizer.
3. Create parent directories as needed (`mkdir -p`).
4. Write the synthesizer's output to `<path>`.
5. Print the `Saved to:` line, then the synthesizer's output verbatim.

## 6. Parallelism rule (callout)

> Reviewer dispatch is the load-bearing optimization of this skill. If you spawn agents one at a time across multiple assistant turns, latency multiplies by N and you've reduced this skill to a sequential checklist with extra steps. Always emit all reviewer `Agent` calls in ONE message.

## 7. Reviewer brief template

Expand placeholders (`{{ ... }}`) at invocation time. Pass the result verbatim as the `prompt` field of each `Agent` call.

```
You are the {{REVIEWER_NAME}} agent reviewing the following target. You have no prior conversation context. Read every file and diff listed in the MATERIALS section in full before producing your report. Do not skim, do not sample.

## TARGET TYPE
{{TARGET_TYPE}}   # one of: pr | dir | file | spec

## MATERIALS
{{MATERIALS_BLOCK}}
# For pr: PR metadata, full diff, list of changed files with absolute paths.
# For dir: directory absolute path + file list with absolute paths.
# For file: single absolute path.
# For spec: list of absolute markdown paths.

## YOUR ROLE
{{REVIEWER_ROLE_DESCRIPTION}}
# Pulled from .claude/agents/<name>.md or ~/.claude/agents/<name>.md (first paragraph of body).
# If no definition exists, this is the literal: "Apply your standard review lens to the materials above."

## SEVERITY SCALE
Use these levels for every finding (EXCEPT if you are critical-thinking — see below):
- CRITICAL: Security vulnerability, data loss risk, or correctness bug that breaks the contract.
- HIGH: Significant bug, design flaw, or maintainability issue that should block merge.
- MEDIUM: Quality concern that should be addressed but does not block.
- LOW: Style, minor suggestion, or nit.

If you are the `critical-thinking` reviewer: do NOT use this scale. Instead, return a list of unstated assumptions, missing context, decisions made without justification, and questions the author should answer before proceeding.

## RETURN FORMAT
Return a single markdown document with this structure:

### Summary
One paragraph: what you reviewed and your overall take.

### Findings
For severity-using reviewers: a section per severity level (CRITICAL → LOW), each finding with:
- **Title**
- **Location** (file:line or file region)
- **Description**
- **Suggested fix**

For critical-thinking: a flat list of assumptions/questions/missing-context items, each with:
- **Item**
- **Why it matters**
- **What to verify or decide**

### Confidence
One line: how confident you are in this review and what would raise that confidence.

Do not include preamble or sign-off. Start with `### Summary`.
```

## 8. Synthesizer brief template

```
You are the {{SYNTHESIZER_NAME}} agent. You have no prior conversation context. Your job is to merge {{N_REVIEWERS}} reviewer reports into a single prioritized review document.

## TARGET TYPE
{{TARGET_TYPE}}

## MATERIALS THE REVIEWERS READ
{{MATERIALS_BLOCK}}

## REVIEWER REPORTS (verbatim)
{{#each reviewer in REVIEWER_OUTPUTS}}
---
### Reviewer: {{reviewer.name}}
{{reviewer.output}}
---
{{/each}}

## MERGE INSTRUCTIONS

Produce a single markdown report with EXACTLY this structure and these section headings:

# Multi-Agent Review: {{TARGET_LABEL}}

## Executive Summary
2-4 sentences: what was reviewed, how many reviewers, headline findings, recommended action.

## Critical Findings
Every CRITICAL-severity finding from any severity-using reviewer. Deduplicate when two reviewers raised the same issue (note both reviewer names). Each finding: title, location, description, suggested fix, attribution (which reviewer(s) raised it).

## High Findings
Same format as Critical, for HIGH-severity findings.

## Medium and Low Findings
A single table with columns: Severity | Title | Location | Reviewer(s) | One-line description.

## Unstated Assumptions and Open Questions (from critical-thinking)
The critical-thinking reviewer's output, integrated as its own section. Do NOT fold its items into the severity buckets above. If no critical-thinking reviewer was in the roster, omit this section entirely (do not emit an empty section).

## Reviewer Disagreements
List places where two or more reviewers contradicted each other. For each disagreement: the issue, what each reviewer said, and your recommended resolution.

## Recommended Changes (Prioritized)
An ordered list of concrete actions, sequenced by severity then by dependency. Each item is one sentence.

## Open Questions for the Author
Decisions or clarifications the author must make before acting on this review.

Do not include preamble. Start with `# Multi-Agent Review: ...`.
```

## 9. Critical-thinking integration note

The `critical-thinking` reviewer (when in the roster) returns a flat list of unstated assumptions, missing context, and questions — NOT severity-tagged findings.

In the synthesized report:
- Its output goes in the **"Unstated Assumptions and Open Questions (from critical-thinking)"** section.
- It is **NOT** folded into the Critical / High / Medium-Low buckets.
- If `critical-thinking` is not in the roster, omit that section from the synthesized report entirely.

This carve-out is intentional: severity buckets reward false-positive findings (any reviewer can manufacture a "MEDIUM"); the critical-thinking output rewards surfacing what the author isn't seeing, which is a different cognitive task and a different output shape.

## 10. Error handling

| Condition | Action |
|---|---|
| Missing or invalid `<target-type>` | Hard-fail with a one-line usage summary. Do not spawn agents. |
| `gh` not on PATH and target is `pr` | Hard-fail with: `gh CLI is required for PR review. Install: https://cli.github.com/`. |
| Directory has > `--max-files` files | Hard-fail with the count, the cap, and the override flag (`--max-files <n>`). |
| `file` target has more than one path | Hard-fail telling the user to use `spec` for multiple markdown files, or split into separate invocations otherwise. |
| Path doesn't exist (`file` or `spec`) | Hard-fail with the missing path; do not silently skip. |
| Unknown reviewer or synthesizer name | Hard-fail before any agent is spawned, with the union of known names from `.claude/agents/` and `~/.claude/agents/`. |
| `--write-to` destination exists, no `--force` | Hard-fail before invoking the synthesizer (don't waste the agent call). |
| Reviewer agent fails or returns empty output | Continue with the remaining reviewers' outputs. Note the failed reviewer in the synthesizer brief as `<reviewer-name>: [FAILED — no output]` so the synthesizer can flag it in "Reviewer Disagreements". |
| Synthesizer fails | Print: `Synthesizer <name> failed. Reviewer outputs follow:` then dump each reviewer's verbatim output with delimiters. Better to surface the raw material than to lose it. |
| PR diff > 100 KB | Emit a warning to stderr (`warning: PR diff is <n>KB; reviewers may truncate`) and proceed. Do NOT auto-truncate. |

## 11. Resolved decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Directory file-count cap | 50, override via `--max-files <n>` | 50 keeps reviewer briefs within typical context budgets; explicit override beats silent truncation. |
| 2 | Unknown reviewer name | Hard-fail with list of known agents | Failing fast at parse time beats spawning an `Agent` call that errors mid-flight with an opaque message. |
| 3 | `gh` CLI absence (PR target) | Hard-fail with install instructions | The PR branch cannot proceed without `gh`; no manual-paste fallback because diffs are too large to copy by hand. |
| 4 | PR URL parsing | Pass through to `gh pr view` unchanged | `gh` already handles `github.com` URLs, enterprise hosts, and `#<n>` shorthand — re-parsing duplicates work and introduces drift. |
| 5 | Subagent-spawn tool name | `Agent` | Verified against `~/.claude/skills/edit-agents/SKILL.md` and the tool schema available in this Claude Code build; `subagent_type` is the parameter the Agent tool exposes. |
| 6 | Reviewer agent discovery | Validate at parse time against `.claude/agents/` + `~/.claude/agents/` | Fail-fast at parse time; see #2. |
| 7 | `--write-to` overwrite | Refuse if exists, allow with `--force` | Aligns with `cp`/`mv` defaults; review reports often accumulate and silent overwrite would destroy history. |
| 8 | PR diff size limit | Warn at 100 KB, do not auto-truncate | Truncation would silently degrade review quality; the user can split a megapatch into chunks themselves. |
