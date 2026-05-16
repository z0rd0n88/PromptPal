# Skill-Builder Prompt: `xan-multi-agent-review`

> **Path convention:** `<repo-root>` refers to the absolute path of the git repository the skill is being built in (resolve via `git rev-parse --show-toplevel`). `~/.claude/` refers to the user-scoped Claude Code config directory. Do not hardcode any other absolute path that ties the skill to a specific machine, user, or project.

You are creating a project-level Claude Code skill at `<repo-root>/.claude/skills/xan-multi-agent-review/SKILL.md`. Do not execute the skill — only build it. Your output is the skill files themselves (SKILL.md plus any helper assets), committed-ready, on a feature branch in a worktree per the project's git rules.

## Table of Contents

1. [Target the Skill](#1-target-the-skill)
2. [Reference Material](#2-reference-material)
3. [Required Behavior](#3-required-behavior)
4. [SKILL.md Frontmatter](#4-skillmd-frontmatter)
5. [SKILL.md Body Structure](#5-skillmd-body-structure)
6. [Reviewer Brief Template](#6-reviewer-brief-template)
7. [Synthesizer Brief Template](#7-synthesizer-brief-template)
8. [Acceptance Criteria](#8-acceptance-criteria)
9. [Open Decisions](#9-open-decisions)

## Executive Summary

You will produce a single skill file (and optional helper assets) that generalizes the one-off prompt at `<repo-root>/docs/prompts/multi-agent-spec-review-v2.md` along two axes: (a) target type — PR, directory, file, or spec-doc set — and (b) reviewer roster — overrideable per invocation, defaulting to the four reviewers + synthesizer used in v2.md. The skill orchestrates parallel Agent calls in a single assistant message, then invokes a synthesizer agent to merge findings, then prints the synthesizer's report verbatim. Frontmatter must be terse (one-sentence description) to avoid prompt-length errors. The skill must NOT execute reviews itself — it must construct briefs, dispatch agents, and relay output.

## 1. Target the Skill

**Skill name:** `xan-multi-agent-review`

**Skill scope:** Project-level (lives under `<repo-root>/.claude/skills/`, not `~/.claude/skills/`).

**Skill purpose:** Run a parallel multi-perspective review over a target (PR, directory, file, or spec-doc set), then merge the reviewer outputs into a single prioritized report.

**Invocation surface (the user types one of these):**

```
xan-multi-agent-review pr 42
xan-multi-agent-review pr https://github.com/owner/repo/pull/42
xan-multi-agent-review dir core/
xan-multi-agent-review file SPEC.md
xan-multi-agent-review spec SPEC.md SPEC-UPDATE-001-foo.md PRD.md

# With reviewer override (comma-separated agent names, no spaces):
xan-multi-agent-review pr 42 --reviewers architect-reviewer,security-reviewer,python-reviewer

# With synthesizer override:
xan-multi-agent-review dir core/ --synthesizer code-reviewer

# With opt-in file output:
xan-multi-agent-review pr 42 --write-to docs/reviews/pr-42-review.md
```

**Argument grammar (encode this in the SKILL.md body, not in invocation magic):**

| Position | Required | Values |
|---|---|---|
| 1 (target type) | yes | `pr` \| `dir` \| `file` \| `spec` |
| 2..N (target args) | yes | per type: PR number/URL; directory path; one file path; one or more markdown paths |
| `--reviewers <csv>` | no | comma-separated agent names; default = the v2.md roster |
| `--synthesizer <name>` | no | single agent name; default = `knowledge-synthesizer` |
| `--write-to <path>` | no | path to also save the final report; default = stdout only |
| `--force` | no | with `--write-to`, overwrite if the destination exists |
| `--max-files <n>` | no | override the directory file-count cap (default 50) |

## 2. Reference Material

You MUST read these in full before writing SKILL.md:

| Path | Why |
|---|---|
| `<repo-root>/docs/prompts/multi-agent-spec-review-v2.md` | Canonical example. Extract the reviewer brief structure, the severity rubric, the synthesizer merge format, and the "critical-thinking is integrated separately" rule. |
| `<repo-root>/CLAUDE.md` | Project rules — repo root, PRD/SPEC conventions, where skill files live. |
| `~/.claude/CLAUDE.md` (Skill Authoring section) | The hard rule: SKILL.md frontmatter `description:` MUST be ONE sentence. Long descriptions break invocation with "prompt is too long". |
| `~/.claude/CLAUDE.md` (Hard Rule: All Code Changes Go Through a Worktree) | Create the worktree before writing any file: `git worktree add .worktrees/xan-multi-agent-review -b feat/xan-multi-agent-review main`. |

When templating from v2.md, preserve verbatim:
- The severity scale (CRITICAL / HIGH / MEDIUM / LOW) and its definitions.
- The four-reviewer default roster: `architect-reviewer`, `critical-thinking`, `silent-failure-hunter`, `security-reviewer`.
- The synthesizer merge skeleton: executive summary → critical → high → medium/low table → unstated assumptions → reviewer disagreements → recommended changes → open questions.
- The carve-out that `critical-thinking` does NOT use the severity scale and must be merged as its own section, not folded into the severity buckets.

## 3. Required Behavior

The skill, when invoked, MUST do the following in order:

### 3.1 Parse and validate

1. Parse positional args + flags per the grammar in §1.
2. Reject with a clear error and exit if:
   - Target type is missing or not one of `pr|dir|file|spec`.
   - Target args don't match the type (e.g., `pr` with no number/URL).
   - `--reviewers` is empty or contains whitespace.
3. If `--reviewers` is omitted, use the default roster.
4. If `--synthesizer` is omitted, use `knowledge-synthesizer`.
5. Validate every reviewer name against `.claude/agents/*.md` (project) and `~/.claude/agents/*.md` (user) BEFORE spawning anything. Unknown name → hard-fail with the list of known agents.

### 3.2 Resolve materials per target type

Each branch produces a **materials block** — a self-contained string that each reviewer brief will embed verbatim. Reviewers receive no shared conversation state, so the materials block is the only way they see the target.

| Target type | Resolution steps |
|---|---|
| `pr <n\|url>` | Run `gh pr view <ref> --json number,title,body,headRefName,baseRefName,files,author,url`. Run `gh pr diff <ref>`. From the JSON, list each changed file's absolute path (resolve relative to repo root). Materials block contains: PR metadata table, full diff, and the file list with absolute paths and an instruction to read each in full. If the diff exceeds 100KB, emit a warning to stderr and proceed (do not auto-truncate). If `gh` is not on PATH, hard-fail with install instructions. |
| `dir <path>` | Resolve to absolute path. Run `git ls-files <path>` (respects .gitignore). If file count exceeds the cap (default 50, override via `--max-files`), abort with a message listing the count and the cap; do not proceed. Materials block contains: the directory's absolute path, the file list with absolute paths, and an instruction to read each in full. |
| `file <path>` | Resolve to absolute path. Verify the file exists and is readable. Reject if more than one path is given. Materials block contains: the absolute path and an instruction to read it in full. |
| `spec <p1> [<p2> ...]` | Resolve each path to absolute. Verify each exists. Materials block contains: the list of absolute paths and an instruction to read each in full. Mirrors the v2.md structure. |

### 3.3 Build reviewer briefs

For each reviewer in the roster, fill in the [Reviewer Brief Template](#6-reviewer-brief-template). The brief must be self-contained — the agent will not see the orchestrator's conversation. Include the materials block, the severity rubric, the reviewer's role, the return format, and the explicit instruction to read all listed files/diffs in full before writing the report.

For `{{REVIEWER_ROLE_DESCRIPTION}}`: read the agent's definition file (`.claude/agents/<name>.md` or `~/.claude/agents/<name>.md`) and use the first paragraph of its body. If the agent has no definition file (e.g., it's a built-in), use the literal string `"Apply your standard review lens to the materials above."`

### 3.4 Spawn reviewers IN PARALLEL

CRITICAL: All reviewer Agent tool calls MUST be issued in a SINGLE assistant message with multiple Agent tool invocations. Do NOT serialize them across multiple assistant turns. The SKILL.md must say this explicitly so future invocations don't degrade to sequential execution.

If the roster has N reviewers, the assistant message contains N parallel Agent calls.

### 3.5 Collect outputs

After all reviewers return, capture each reviewer's verbatim output keyed by reviewer name. Do not edit, summarize, or filter at this stage.

### 3.6 Invoke synthesizer

Build the synthesizer brief from the [Synthesizer Brief Template](#7-synthesizer-brief-template), embedding all reviewer outputs verbatim and the same materials block. Spawn the synthesizer as a single Agent call.

### 3.7 Print verbatim

Print the synthesizer's output as the final assistant message, verbatim, with no preamble or postscript — OTHER THAN, when `--write-to <path>` was provided, a single line above the verbatim output that reads `Saved to: <absolute path>`.

When `--write-to <path>` is provided:
- Resolve `<path>` to an absolute path (relative to repo root if not already absolute).
- Create parent directories as needed.
- If the destination file already exists and `--force` is NOT set, exit with an error before invoking the synthesizer; do not overwrite silently.
- Write the synthesizer's output to the path, then print the `Saved to:` line followed by the verbatim output.

### 3.8 Non-goals

- The skill does NOT execute the reviewers' suggestions.
- The skill does NOT write to any file unless `--write-to` is set.
- The skill does NOT compress or truncate reviewer outputs before passing them to the synthesizer.

## 4. SKILL.md Frontmatter

### 4.0 Prerequisite check (do this BEFORE writing the frontmatter)

Verify the subagent-spawn tool name used in this Claude Code build. Wrong tool name silently degrades parallelism with no error — this is a load-bearing decision.

1. Inspect any existing project skill that spawns subagents: `ls <repo-root>/.claude/skills/*/SKILL.md` and grep their `allowed-tools` blocks.
2. If the project has no existing skills, fall back to user-level skills: `ls ~/.claude/skills/*/SKILL.md` and grep theirs.
3. If neither yields a clear answer, default to `Agent` (matches the tool list documented in this prompt-builder run) and document the decision in the SKILL.md body.

Use the verified name in the `allowed-tools` block below.

### 4.1 Frontmatter

Use exactly this frontmatter. The `description` is a single sentence. Do not lengthen it; long descriptions cause "prompt is too long" errors per the global skill-authoring rule.

```yaml
---
name: xan-multi-agent-review
description: Run a parallel multi-perspective review (architect, critical-thinking, silent-failure, security by default) over a PR, directory, file, or spec-doc set, then merge findings via a synthesizer agent.
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - Agent   # or Task — verify per §4.0 before committing
---
```

Notes:
- `Bash` is needed for `gh pr view`, `gh pr diff`, `git ls-files`, and the optional `--write-to` file write.
- `Read`/`Glob`/`Grep` are needed for path validation, agent-definition lookups, and the directory enumeration fallback if `git ls-files` is unavailable.

## 5. SKILL.md Body Structure

The SKILL.md body must contain the following sections, in this order:

1. **Purpose** — one paragraph restating the skill's job.
2. **When to use** — bullet list of trigger phrases ("review this PR", "audit this directory", "review the spec amendment").
3. **Invocation grammar** — the table from §1, copied verbatim.
4. **Defaults** — the default reviewer roster and synthesizer name.
5. **Workflow** — the seven steps from §3, written as imperative instructions to the future invocation.
6. **Parallelism rule** — a callout box stating that all reviewer Agent calls MUST be in one assistant message.
7. **Reviewer brief template** — the full template from §6, with placeholders.
8. **Synthesizer brief template** — the full template from §7, with placeholders.
9. **Critical-thinking integration note** — explicit reminder that `critical-thinking` outputs do NOT use the severity scale and must be merged as a standalone section in the synthesizer report.
10. **Error handling** — what to do when `gh` is missing, when a path doesn't exist, when an agent fails, when `--write-to` would overwrite.
11. **Resolved decisions** — the choices made for each item in §9 of this builder prompt, with a one-line rationale per item.

## 6. Reviewer Brief Template

The skill must generate one brief per reviewer at invocation time, filling in `{{ ... }}` placeholders. Embed this template verbatim in SKILL.md so the future invocation can mechanically expand it.

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
# Pull from .claude/agents/<name>.md or ~/.claude/agents/<name>.md (first paragraph of body).
# If no definition exists, use: "Apply your standard review lens to the materials above."

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

## 7. Synthesizer Brief Template

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
The critical-thinking reviewer's output, integrated as its own section. Do NOT fold its items into the severity buckets above. If no critical-thinking reviewer was in the roster, omit this section.

## Reviewer Disagreements
List places where two or more reviewers contradicted each other. For each disagreement: the issue, what each reviewer said, and your recommended resolution.

## Recommended Changes (Prioritized)
An ordered list of concrete actions, sequenced by severity then by dependency. Each item is one sentence.

## Open Questions for the Author
Decisions or clarifications the author must make before acting on this review.

Do not include preamble. Start with `# Multi-Agent Review: ...`.
```

## 8. Acceptance Criteria

The skill is correct if and only if all of the following hold. Verify each before finalizing.

- [ ] A `SKILL.md` exists at a valid skill location — either project-scoped (`<repo-root>/.claude/skills/xan-multi-agent-review/SKILL.md`) or user-scoped (`~/.claude/skills/xan-multi-agent-review/SKILL.md`). The skill is built into the project location by default, but later promotion to user scope (move the directory, no other changes) must still satisfy this check. Do not hardcode the project path here.
- [ ] Frontmatter `description` is exactly one sentence (no period-separated run-ons, no embedded lists).
- [ ] Frontmatter `name` is `xan-multi-agent-review`.
- [ ] `allowed-tools` includes `Bash`, `Read`, `Glob`, `Grep`, and the project's verified subagent-spawn tool name (per §4.0).
- [ ] Invoking with no `--reviewers` flag dispatches to exactly four reviewers: `architect-reviewer`, `critical-thinking`, `silent-failure-hunter`, `security-reviewer`.
- [ ] Invoking with `--reviewers a,b,c` dispatches to exactly three Agent calls, with names `a`, `b`, `c`.
- [ ] All reviewer Agent calls are issued in a single assistant message (parallel), not across multiple turns.
- [ ] No `--synthesizer` flag → synthesizer is `knowledge-synthesizer`. With flag → uses the provided name.
- [ ] Unknown reviewer or synthesizer name → hard-fail before spawning anything, with the list of known agents.
- [ ] PR target with a number works (`pr 42`); PR target with a URL works (`pr https://github.com/...`).
- [ ] Directory target enumerates via `git ls-files <path>` and aborts above the file-count cap (default 50; overrideable via `--max-files`).
- [ ] File target accepts one path and rejects multiple paths.
- [ ] Spec target accepts one or more markdown paths.
- [ ] The synthesizer brief embeds every reviewer's output verbatim.
- [ ] The final assistant message is the synthesizer's output verbatim — with no preamble or postscript, except for the single `Saved to: <path>` line when `--write-to` is set.
- [ ] `--write-to <path>` saves the synthesizer output and refuses to overwrite an existing file unless `--force` is also set.
- [ ] Without `--write-to`, no files are created.
- [ ] The `critical-thinking` reviewer's output appears as its own section in the synthesized report and is NOT folded into the severity buckets.
- [ ] `gh` not on PATH and target type is `pr` → hard-fail with install instructions.
- [ ] PR diff > 100KB → warn on stderr, proceed (no auto-truncation).
- [ ] Skill files are committed on a feature branch in `.worktrees/xan-multi-agent-review/` per the project's worktree rule.

## 9. Open Decisions

Each item below has a recommended default. Apply the recommendation, document the choice in the SKILL.md "Resolved decisions" section, and move on. Do not block the build on these.

| # | Decision | Default | Override |
|---|---|---|---|
| 1 | Directory file-count cap | 50 | `--max-files <n>` |
| 2 | Unknown reviewer name | hard-fail with list of known agents | none |
| 3 | `gh` CLI absence (PR target) | hard-fail with install instructions | none |
| 4 | PR URL parsing | pass through to `gh pr view` unchanged | none |
| 5 | Subagent-spawn tool name | verify per §4.0; default `Agent` | none |
| 6 | Reviewer agent discovery | validate at parse time against `.claude/agents/` + `~/.claude/agents/` | none |
| 7 | `--write-to` overwrite behavior | refuse if exists | `--force` |
| 8 | PR diff size limit | warn at 100KB; do not auto-truncate | none |

---

End of skill-builder prompt. Hand this file to a fresh session that will create the actual skill files.
