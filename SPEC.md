# PromptPal — Technical Specification

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [System Architecture](#system-architecture)
3. [Directory Layout](#directory-layout)
4. [Data Schemas](#data-schemas)
5. [CLI Implementation](#cli-implementation)
6. [Backend Integration](#backend-integration)
7. [WSL and Platform Support](#wsl-and-platform-support)
8. [Refinement Loop](#refinement-loop)
9. [History and Persistence](#history-and-persistence)
10. [Configuration](#configuration)
11. [Installation](#installation)
12. [Error Handling](#error-handling)
13. [GUI — Phase 2 Architecture](#gui--phase-2-architecture)
14. [Testing Strategy](#testing-strategy)
15. [Build and Release](#build-and-release)
16. [Open Questions](#open-questions)

---

## Executive Summary

PromptPal is a two-phase local tool. Phase 1 delivers a bash CLI (`promptpal`) backed by a `python3` subprocess for JSON handling, diff generation, and API calls. Phase 2 delivers a Tauri desktop GUI that shares the same `~/.promptpal/` store. The tool supports two backends — the Anthropic Messages API (direct HTTP) and the Claude CLI subprocess — selected automatically at startup. WSL2 is a first-class supported platform. Multi-turn refinement uses a stateful messages array; history is stored as individual JSON files with atomic writes.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    User Interface Layer                   │
│                                                           │
│   CLI (bash entrypoint)          GUI (Tauri, Phase 2)    │
│   promptpal <args>               promptpal-gui            │
└────────────────────┬────────────────────┬────────────────┘
                     │                    │
                     ▼                    ▼
┌─────────────────────────────────────────────────────────┐
│                    Core Engine (Python)                   │
│                                                           │
│   core/improve.py     — pipeline orchestrator            │
│   core/backend.py     — backend auto-detection + ABC     │
│   core/api_backend.py — Anthropic HTTP client            │
│   core/cli_backend.py — Claude CLI subprocess wrapper    │
│   core/platform.py    — WSL detection, clipboard, paths  │
│   core/history.py     — session persistence              │
│   core/config.py      — config loader                    │
│   core/diff.py        — unified diff renderer            │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│                ~/.promptpal/ (shared store)               │
│                                                           │
│   config.json                                            │
│   system_prompt.txt                                       │
│   history/                                               │
│     index.json                                           │
│     <uuid>.json  (one per session)                       │
│   profiles/      (Phase 2)                               │
│   usage.log                                              │
└─────────────────────────────────────────────────────────┘
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
┌─────────────────┐   ┌──────────────────────────┐
│ Anthropic API   │   │ Claude CLI subprocess     │
│ POST /v1/msgs   │   │ claude -p "..."           │
└─────────────────┘   └──────────────────────────┘
```

---

## Directory Layout

### Repository

```
promptpal/
├── bin/
│   └── promptpal              # bash entrypoint (added to PATH)
├── core/
│   ├── __init__.py
│   ├── improve.py             # pipeline orchestrator
│   ├── backend.py             # backend ABC + auto-detection factory
│   ├── api_backend.py         # Anthropic HTTP client
│   ├── cli_backend.py         # Claude CLI subprocess wrapper (stream-json + --bare)
│   ├── platform.py            # WSL detection, clipboard, HOME guard
│   ├── history.py             # session persistence
│   ├── config.py              # config loader/writer
│   ├── diff.py                # diff rendering
│   ├── system_prompt.txt      # bundled canonical PromptPal system prompt (D-06)
│   └── system_prompt.sha256   # sha256 of system_prompt.txt; verified on update
├── tests/
│   ├── unit/
│   │   ├── test_api.py
│   │   ├── test_backend.py
│   │   ├── test_platform.py
│   │   ├── test_history.py
│   │   ├── test_config.py
│   │   └── test_diff.py
│   └── integration/
│       ├── test_pipeline.py
│       ├── test_backend_detection.py
│       ├── test_stdin.py
│       └── test_flags.py
├── install.sh                 # one-liner installer
├── uninstall.sh
├── completions/
│   ├── promptpal.bash
│   ├── promptpal.zsh
│   └── promptpal.fish
└── gui/                       # Phase 2 — Tauri app
    ├── src-tauri/
    └── src/
```

### Runtime Store (`~/.promptpal/`)

```
~/.promptpal/
├── config.json
├── system_prompt.txt          # writable copy; seeded from core/system_prompt.txt
├── system_prompt.sha256       # hash of the active local prompt
├── usage.log
├── history/
│   ├── index.json
│   └── <uuid>.json
└── profiles/              # Phase 2
    └── <name>.md
```

---

## Data Schemas

### Session Record (`~/.promptpal/history/<uuid>.json`)

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": "2026-05-15T14:30:00Z",
  "updated_at": "2026-05-15T14:32:15Z",
  "label": "optional human-readable name",
  "model": "claude-sonnet-4-6",
  "original_prompt": "write me a sorting function",
  "turns": [
    {
      "turn": 1,
      "user_input": "write me a sorting function",
      "assistant_output": "Improved prompt text...",
      "feedback": null,
      "backend": "claude-cli",
      "input_tokens": null,
      "output_tokens": null,
      "duration_ms": 2340
    },
    {
      "turn": 2,
      "user_input": "make it more specific to Python",
      "assistant_output": "Further improved prompt...",
      "feedback": "make it more specific to Python",
      "backend": "api-key",
      "input_tokens": 501,
      "output_tokens": 203,
      "duration_ms": 1890
    }
  ],
  "final_prompt": "Final accepted prompt text...",
  "status": "accepted"
}
```

**Status values:** `in-progress` | `accepted` | `discarded`

The `backend` field on each turn enables per-turn provenance tracking if the user switches backends across sessions. `input_tokens` and `output_tokens` are `null` when the Claude CLI backend is used.

### Session Index (`~/.promptpal/history/index.json`)

```json
{
  "version": 1,
  "entries": [
    {
      "session_id": "550e8400-e29b-41d4-a716-446655440000",
      "created_at": "2026-05-15T14:30:00Z",
      "label": "optional label",
      "status": "accepted",
      "original_prompt_preview": "write me a sorting function"
    }
  ]
}
```

### Config (`~/.promptpal/config.json`)

```json
{
  "version": 1,
  "default_model": "claude-sonnet-4-6",
  "default_iterations": 1,
  "auto_copy": false,
  "show_diff": true,
  "system_prompt_path": "~/.promptpal/system_prompt.txt",
  "history_enabled": true,
  "max_history_entries": 500,
  "system_prompt_update_url": "https://raw.githubusercontent.com/z0rd0n88/PromptPal/main/core/system_prompt.txt",
  "preferred_backend": "auto"
}
```

`preferred_backend` values: `"auto"` (default) | `"claude-cli"` | `"api-key"`

### Usage Log (`~/.promptpal/usage.log`)

Append-only, one JSON object per line (NDJSON):

```
{"ts":"2026-05-15T14:30:01Z","session_id":"550e...","model":"claude-sonnet-4-6","turn":1,"input_tokens":312,"output_tokens":187}
```

---

## CLI Implementation

### Entrypoint (`bin/promptpal`)

The bash script is a thin dispatcher. It validates the WSL HOME guard first, then locates the Python core and delegates:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Guard: reject Windows NTFS home in WSL — prevents writing history to /mnt/c/
if [[ "${HOME:-}" == /mnt/c/* ]] || [[ "${HOME:-}" == /c/* ]]; then
  echo "Warning: HOME appears to be a Windows path." >&2
  echo "Launch from WSL: wsl -d Ubuntu -- promptpal" >&2
  exit 1
fi

PROMPTPAL_HOME="${PROMPTPAL_HOME:-$HOME/.promptpal}"
CORE_DIR="$(dirname "$(realpath "$0")")/../core"

# Bootstrap: run first-time setup if store missing
if [[ ! -d "$PROMPTPAL_HOME" ]]; then
  python3 "$CORE_DIR/setup.py" --home "$PROMPTPAL_HOME"
fi

exec python3 "$CORE_DIR/cli.py" --home "$PROMPTPAL_HOME" "$@"
```

### Argument Parsing (`core/cli.py`)

Use Python's `argparse`. All flags map to a `CLIOptions` dataclass passed to `improve.py`.

```python
@dataclass
class CLIOptions:
    prompt: str | None          # positional or None for interactive/stdin
    model: str
    iterations: int
    no_history: bool
    copy: bool
    show_history: bool
    replay: str | None
    system_prompt_file: str | None
    output_format: str          # plain | json | markdown
    quiet: bool
    search: str | None
    export_id: str | None
    label: str | None
    update_system_prompt: bool
    uninstall: bool
    home: str                   # ~/.promptpal path
    backend: str | None         # None = auto, "claude-cli", "api-key"
    status: bool                # --status: print backend/auth info and exit
```

`--status` output format:

```
PromptPal status
────────────────
Platform:  WSL2
Backend:   claude-cli (claude-sonnet-4-6)
Auth:      ✓ OK
claude:    /usr/local/bin/claude
Config:    ~/.promptpal/config.json
History:   12 sessions
```

### Input Resolution

Priority order for prompt source:

1. Positional CLI argument (`promptpal "prompt"`)
2. stdin if not a TTY (`echo "prompt" | promptpal`)
3. Interactive TTY input (user types at prompt)

```python
def resolve_prompt(args: CLIOptions) -> str:
    if args.prompt:
        return args.prompt
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return input("Enter your prompt: ").strip()
```

### Output Modes

| Mode | Behavior |
|------|----------|
| `plain` (default) | Formatted terminal output with diff, spinner, loop |
| `json` | Single JSON object: `{original, improved, turns, session_id}` |
| `markdown` | Fenced markdown block of improved prompt |
| `--quiet` | Raw improved prompt text only, no chrome |

In `--quiet` or non-TTY output, disable spinner and diff; write only the final improved prompt to stdout.

---

## Backend Integration

### Abstract Interface (`core/backend.py`)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class BackendResponse:
    text: str
    input_tokens: int | None   # None when backend doesn't expose counts
    output_tokens: int | None

class Backend(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier, e.g. 'claude-cli (claude-sonnet-4-6)'"""

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[dict],
        stream: bool = False,
    ) -> BackendResponse:
        """
        Execute one completion turn.
        - system: the Prompt Builder system prompt text
        - messages: full conversation history as [{role, content}]
        - stream: if True, print tokens to stdout as they arrive
        Returns BackendResponse(text, input_tokens, output_tokens).
        input_tokens / output_tokens may be None if the backend doesn't
        expose them (CLI backend).
        """

    @abstractmethod
    def check_auth(self) -> bool:
        """
        Perform a lightweight liveness check.
        Returns True if auth is valid, False otherwise.
        Used by --status and first-run setup.
        """
```

### Auto-Detection (`core/backend.py` — `resolve_backend`)

```python
def resolve_backend(preferred: str | None, model: str) -> Backend:
    """
    Auto-detection order:
      1. If preferred == "api-key"   → ApiBackend  (fail fast if key absent)
      2. If preferred == "claude-cli"→ CliBackend  (fail fast if not on PATH)
      3. If preferred is None (auto):
         a. claude on PATH           → CliBackend
         b. ANTHROPIC_API_KEY set    → ApiBackend
         c. Neither                  → raise NoBackendError
    """
    from core.cli_backend import CliBackend
    from core.api_backend import ApiBackend
    import os, shutil

    if preferred == "api-key":
        return ApiBackend(api_key=_require_api_key(), model=model)
    if preferred == "claude-cli":
        return CliBackend(model=model)

    # auto
    if shutil.which("claude"):
        return CliBackend(model=model)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return ApiBackend(api_key=api_key, model=model)

    raise NoBackendError()

class NoBackendError(Exception):
    MESSAGE = (
        "Error: No backend available. Set up one of the following:\n"
        "  Option 1 (Claude CLI): Install Claude Code and run `claude auth login`\n"
        "  Option 2 (API key):    export ANTHROPIC_API_KEY=\"sk-ant-...\""
    )
```

### Anthropic HTTP Backend (`core/api_backend.py`)

Implements `Backend` using the Anthropic Messages API.

```python
from core.backend import Backend, BackendResponse
import urllib.request, json, os

class ApiBackend(Backend):
    BASE_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    @property
    def name(self) -> str:
        return f"api-key ({self.model})"

    def check_auth(self) -> bool:
        # Attempt a minimal API call; return True on 200, False on 401
        ...

    def complete(self, system, messages, stream=False) -> BackendResponse:
        # HTTP implementation — see request shape below
        ...
```

**Request shape:**

```json
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 4096,
  "system": "<contents of system_prompt.txt>",
  "messages": [
    {"role": "user", "content": "raw prompt text"},
    {"role": "assistant", "content": "improved prompt text"},
    {"role": "user", "content": "user feedback for next pass"}
  ]
}
```

**Headers:**

```
x-api-key: $ANTHROPIC_API_KEY
anthropic-version: 2023-06-01
content-type: application/json
```

**Retry logic:**

```
401 Unauthorized  → fail immediately, print actionable message
429 Rate Limit    → read Retry-After header, sleep, retry (max 3 attempts)
5xx Server Error  → exponential backoff 1s/2s/4s, retry (max 3 attempts)
Network Error     → retry once after 2s, then fail with message
```

**Streaming:** In interactive mode use `"stream": true`; print tokens as `text_delta` events arrive. In `--quiet`/non-TTY mode use non-streaming.

### Claude CLI Backend (`core/cli_backend.py`)

Spawns `claude -p` with stream-json input/output for native multi-turn (D-07), overriding the default Claude Code system prompt (~28k cache-creation tokens, ~\$0.10/turn) with PromptPal's bundled prompt-improver prompt via `--bare --system-prompt-file` (D-10). Verified against `claude 2.1.143`.

```python
from core.backend import Backend, BackendResponse
import subprocess, shutil, sys, json

# Native stream-json multi-turn was verified on this version. Older versions
# may lack `--input-format=stream-json` or the `--bare` flag; warn at startup
# (do not block) until the floor is formally declared (see DEV-TRACKER F-01).
CLAUDE_VERSION_FLOOR = "2.1.143"


class CliBackend(Backend):
    def __init__(self, model: str, system_prompt_path: str):
        self.model = model
        self._system_prompt_path = system_prompt_path  # absolute path to system_prompt.txt
        self._claude_path = shutil.which("claude")
        if not self._claude_path:
            raise FileNotFoundError("claude CLI not found on PATH")

    @property
    def name(self) -> str:
        return f"claude-cli ({self.model})"

    def check_auth(self) -> bool:
        return subprocess.run(
            [self._claude_path, "--version"],
            capture_output=True, text=True,
        ).returncode == 0

    def complete(self, system: str, messages: list[dict]) -> BackendResponse:
        # The `system` argument is intentionally ignored — the bundled
        # system_prompt.txt at self._system_prompt_path is the canonical
        # source of system instructions for this backend (D-10).
        # ApiBackend uses the same file content as its `system` field,
        # so the two backends agree on the prompt.
        cmd = [
            self._claude_path,
            "-p",
            "--bare",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--no-session-persistence",
            "--model", self.model,
            "--system-prompt-file", self._system_prompt_path,
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Stream-json envelope per turn (one JSON object per line on stdin).
        # We feed the entire `messages` history each call — same contract
        # as the API backend — instead of relying on CLI session state.
        for m in messages:
            envelope = {
                "type": m["role"],  # "user" or "assistant"
                "message": {"role": m["role"], "content": m["content"]},
            }
            proc.stdin.write(json.dumps(envelope) + "\n")
        proc.stdin.close()

        # Parse stream-json envelopes off stdout. The terminal envelope is
        # `{"type":"result","is_error":<bool>,"result":"<text>","usage":{...}}`.
        # Intermediate events are filtered (we don't pass --include-partial-messages,
        # so we don't expect any). Non-JSON noise is tolerated defensively.
        text = ""
        input_tokens = output_tokens = None
        for line in proc.stdout:
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") != "result":
                continue
            if evt.get("is_error"):
                raise RuntimeError(evt.get("result") or "claude CLI error")
            text = evt.get("result", "")
            usage = evt.get("usage") or {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")

        rc = proc.wait()
        if rc != 0:
            stderr = proc.stderr.read().strip()
            if _is_auth_error(stderr):
                print("Claude CLI auth failed. Run: claude auth login", file=sys.stderr)
                sys.exit(1)
            raise RuntimeError(f"claude CLI exited {rc}: {stderr}")

        return BackendResponse(
            text=text.strip(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _is_auth_error(stderr: str) -> bool:
    auth_patterns = ["authentication", "unauthorized", "auth", "login", "token"]
    return any(p in stderr.lower() for p in auth_patterns)
```

**Stream-json envelope shape (D-07):**

- **stdin (one JSON object per line):** `{"type":"<role>","message":{"role":"<role>","content":"<text>"}}` where `<role>` is `"user"` or `"assistant"`. The full conversation is sent each turn — the subprocess holds no state between calls.
- **stdout:** the single envelope we consume is `{"type":"result","is_error":<bool>,"result":"<assistant text>","usage":{"input_tokens":<n>,"output_tokens":<n>,...}}`. Without `--include-partial-messages`, no intermediate events are emitted. Defensive parsing (`json.JSONDecodeError` → skip) covers any future preamble noise the CLI might add.

**Why `--bare`:** Suppresses Claude Code's full default system prompt, hooks, LSP, plugin sync, attribution, auto-memory, background prefetches, keychain reads, and CLAUDE.md auto-discovery. Forces auth via `ANTHROPIC_API_KEY` or `apiKeyHelper` only — exactly the isolated subprocess we want for refinement. Without `--bare`, a trivial probe burned 27,982 cache-creation tokens (~\$0.10) loading Code's system prompt (D-10).

**Why `--system-prompt-file` (not `--system-prompt`):** Reads from disk so the bundled `core/system_prompt.txt` is the single source of truth and a hash check (`core/system_prompt.sha256`) can verify integrity on `--update-system-prompt`. Avoids embedding the prompt in argv (which would show up in `ps` output and hit shell argv length limits).

**Why `--no-session-persistence`:** Prevents the CLI from writing this conversation to its own on-disk session store (`~/.config/claude/`), which would conflict with PromptPal's `~/.promptpal/history/` and confuse `claude --resume` for the user's own future sessions.

**Why neither `--continue` nor `--resume`/`--session-id`:** Both force the CLI to own conversation state on disk. Our in-memory `messages` list is the source of truth; stream-json round-tripping the full history each turn keeps the `Backend` contract clean and makes the subprocess stateless.

**Model passthrough (D-08):** `--model <id>` forwarded unchanged. Verified end-to-end that `claude --model claude-sonnet-4-6` accepts the same alias and full-name strings as the API.

**Token counts:** Available from the `usage` object in the result envelope; recorded in history.

### Pipeline Integration (`core/improve.py`)

The pipeline orchestrator receives a `Backend` instance via dependency injection:

```python
def run_pipeline(opts: CLIOptions, platform: Platform) -> None:
    config = load_config(opts.home)
    backend = resolve_backend(
        preferred=opts.backend or config.preferred_backend or None,
        model=opts.model or config.default_model,
    )
    system_prompt = load_system_prompt(config)
    # ... rest of pipeline; replace client.complete() with backend.complete()
```

---

## WSL and Platform Support

### `core/platform.py`

Detects the runtime platform and provides WSL-safe utilities.

```python
import os, subprocess, sys
from dataclasses import dataclass

@dataclass
class Platform:
    is_wsl: bool
    wsl_version: int | None    # 1 or 2, None if not WSL
    home: str                  # resolved WSL-safe home
    clipboard_cmd: list[str]   # command to pipe text into clipboard

def detect_platform() -> Platform:
    is_wsl, wsl_version = _detect_wsl()
    home = _resolve_home(is_wsl)
    clipboard_cmd = _detect_clipboard(is_wsl)
    return Platform(is_wsl=is_wsl, wsl_version=wsl_version,
                    home=home, clipboard_cmd=clipboard_cmd)

def _detect_wsl() -> tuple[bool, int | None]:
    try:
        osrelease = open("/proc/sys/kernel/osrelease").read().lower()
        if "microsoft" not in osrelease:
            return False, None
        return True, 2 if "wsl2" in osrelease else 1
    except FileNotFoundError:
        return False, None

def _resolve_home(is_wsl: bool) -> str:
    home = os.environ.get("HOME", "")
    if is_wsl and (home.startswith("/mnt/c") or home.startswith("/c/")):
        # Regression: HOME points at Windows NTFS mount — use passwd entry instead
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    return home

def _detect_clipboard(is_wsl: bool) -> list[str]:
    for cmd in (["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
                ["pbcopy"]):
        if _cmd_exists(cmd[0]):
            return cmd
    if is_wsl and _cmd_exists("clip.exe"):
        return ["clip.exe"]
    return []    # no clipboard; --copy will warn

def _cmd_exists(name: str) -> bool:
    return subprocess.run(["command", "-v", name], shell=False,
                          capture_output=True).returncode == 0

def assert_wsl_home_safe(platform: Platform) -> None:
    raw = os.environ.get("HOME", "")
    if raw.startswith("/mnt/c") or raw.startswith("/c/"):
        print(
            "Warning: HOME appears to be a Windows path.\n"
            "For best results, launch from WSL:\n"
            "  wsl -d Ubuntu -- promptpal",
            file=sys.stderr,
        )
        sys.exit(1)
```

### WSL Detection Logic

| Check | Value | Result |
|-------|-------|--------|
| `/proc/sys/kernel/osrelease` contains `microsoft` + `wsl2` | True | `Platform(is_wsl=True, wsl_version=2)` |
| `/proc/sys/kernel/osrelease` contains `microsoft` only | True | `Platform(is_wsl=True, wsl_version=1)` |
| `/proc/sys/kernel/osrelease` missing or no `microsoft` | — | `Platform(is_wsl=False, wsl_version=None)` |
| `HOME` starts with `/mnt/c/` or `/c/` | WSL + bad HOME | Bail with actionable error, exit 1 |

`_resolve_home()` cross-checks `$HOME` against the passwd entry for the current UID. If they disagree and `$HOME` is an NTFS mount path, the passwd value wins.

### Clipboard Provider Selection

Provider selection priority (evaluated at startup by `_detect_clipboard()`):

| Priority | Command | Available when |
|----------|---------|----------------|
| 1 | `xclip -selection clipboard` | X11 session or XWayland |
| 2 | `xsel --clipboard --input` | X11 session or XWayland |
| 3 | `pbcopy` | macOS |
| 4 | `clip.exe` | WSL2 (Windows clipboard bridge) |
| — | None | Warn user; `--copy` is a no-op |

When no clipboard provider is found, `--copy` prints a non-fatal warning and the improved prompt is still displayed.

---

## Refinement Loop

### State Machine

```
          ┌─────────────────────────────────┐
          │         INITIAL STATE           │
          │   resolve_prompt() → raw_prompt │
          └───────────────┬─────────────────┘
                          │
                          ▼
          ┌─────────────────────────────────┐
          │         API CALL                │
          │   messages.append(user_turn)    │
          │   response = client.complete()  │
          │   messages.append(asst_turn)    │
          └───────────────┬─────────────────┘
                          │
                          ▼
          ┌─────────────────────────────────┐
          │         DISPLAY                 │
          │   show improved prompt          │
          │   show diff (if > 3 lines)      │
          └───────────────┬─────────────────┘
                          │
                          ▼
          ┌─────────────────────────────────┐
          │    INTERACTIVE PROMPT           │
          │  [a]ccept [i]terate [d]iscard   │
          │  [r]aw    [c]opy                │
          └──┬──────────┬──────────┬────────┘
             │          │          │
             ▼          ▼          ▼
          ACCEPT     ITERATE    DISCARD
          save()   feedback→   exit()
          exit()   API CALL
```

### messages Array Management

```python
messages: list[dict] = []

# Turn 1
messages.append({"role": "user", "content": raw_prompt})
response = client.complete(system=system_prompt, messages=messages)
messages.append({"role": "assistant", "content": response.text})

# Turn N (iterate)
messages.append({"role": "user", "content": user_feedback})
response = client.complete(system=system_prompt, messages=messages)
messages.append({"role": "assistant", "content": response.text})
```

The full messages array is sent on each API call. No summarization or truncation in Phase 1 — the context window is large enough for typical prompt improvement sessions.

---

## History and Persistence

### Atomic Write Pattern

```python
import os, json, uuid, tempfile

def write_session(session: dict, history_dir: str) -> None:
    path = os.path.join(history_dir, f"{session['session_id']}.json")
    tmp_fd, tmp_path = tempfile.mkstemp(dir=history_dir, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(session, f, indent=2)
        os.rename(tmp_path, path)  # atomic on POSIX
    except Exception:
        os.unlink(tmp_path)
        raise
```

### Index Maintenance

On every write (create or update), reload `index.json`, upsert the entry for this `session_id`, and atomically overwrite the index using the same tmp → rename pattern.

### Eviction

When `max_history_entries` is exceeded, remove the oldest entries by `created_at` from both the index and their individual JSON files.

### Search Implementation

`--search KEYWORD`: scan `index.json` first (searches `original_prompt_preview` and `label`). If no match, fall back to scanning all session JSON files for `original_prompt` and `final_prompt` content. Return matching session IDs sorted by `created_at` descending.

---

## Configuration

### Loader (`core/config.py`)

```python
@dataclass
class Config:
    version: int = 1
    default_model: str = "claude-sonnet-4-6"
    default_iterations: int = 1
    auto_copy: bool = False
    show_diff: bool = True
    system_prompt_path: str = "~/.promptpal/system_prompt.txt"
    history_enabled: bool = True
    max_history_entries: int = 500
    system_prompt_update_url: str = "https://raw.githubusercontent.com/z0rd0n88/PromptPal/main/core/system_prompt.txt"
    preferred_backend: str = "auto"    # "auto" | "claude-cli" | "api-key"

def load_config(home: str) -> Config:
    path = os.path.join(home, "config.json")
    if not os.path.exists(path):
        return Config()
    with open(path) as f:
        data = json.load(f)
    return Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
```

CLI flags override config values. Merge order: defaults → config.json → CLI flags.

### Backend Selection

Merge order on each invocation: `Config.preferred_backend` → `--backend` CLI flag → auto-detection.

Per D-03, `--backend <name>` **persists** to `config.json` so subsequent invocations default to the same backend without re-passing the flag. Persistence rules:

- Persist via the same atomic `tempfile.mkstemp` → `os.rename` pattern used for history writes.
- Write only when the resolved backend differs from the current on-disk value (avoids spurious mtime churn on every call).
- `--backend auto` is the explicit reset path: it clears `preferred_backend` (writing `"auto"` back) and re-runs auto-detection on the current invocation.
- The CLI prints a one-line confirmation on persistence (e.g. `Saved preferred_backend = api-key to ~/.promptpal/config.json`) so the side effect is visible.

`ANTHROPIC_API_KEY` is read exclusively from the environment variable, never from config. When the API backend is selected and the key is absent:

```
Error: ANTHROPIC_API_KEY environment variable is not set.

To fix:
  export ANTHROPIC_API_KEY="sk-ant-..."

Add this to your ~/.bashrc or ~/.zshrc to persist it.
```

When no backend is available at all:

```
Error: No backend available. Set up one of the following:
  Option 1 (Claude CLI): Install Claude Code and run `claude auth login`
  Option 2 (API key):    export ANTHROPIC_API_KEY="sk-ant-..."
```

---

## Installation

### `install.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
PROMPTPAL_HOME="${HOME}/.promptpal"
REPO_RAW="https://raw.githubusercontent.com/user/promptpal/main"

# 1. Detect WSL and validate HOME
IS_WSL=false
if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  IS_WSL=true
  echo "WSL detected."
fi
if [[ "${HOME:-}" == /mnt/c/* ]] || [[ "${HOME:-}" == /c/* ]]; then
  echo "Error: HOME is a Windows path ($HOME)." >&2
  echo "Launch the installer from a WSL shell: wsl -d Ubuntu -- bash install.sh" >&2
  exit 1
fi

# 2. Check dependencies
command -v python3 >/dev/null || { echo "Error: python3 required"; exit 1; }
command -v curl >/dev/null || command -v wget >/dev/null || { echo "Error: curl or wget required"; exit 1; }

# 3. Download core files
mkdir -p "$INSTALL_DIR" "$PROMPTPAL_HOME/history"
# ... download bin/ and core/ files

# 4. config.json is written on first run by core/setup.py from dataclass defaults
#    (Python is the canonical writer per D-05 — bash never touches JSON).

# 5. Seed default system prompt (only if not exists) from the bundled core/ copy.
[[ -f "$PROMPTPAL_HOME/system_prompt.txt" ]] || cp "$INSTALL_DIR/../core/system_prompt.txt" "$PROMPTPAL_HOME/system_prompt.txt"
[[ -f "$PROMPTPAL_HOME/system_prompt.sha256" ]] || cp "$INSTALL_DIR/../core/system_prompt.sha256" "$PROMPTPAL_HOME/system_prompt.sha256"

# 6. Make binary executable and ensure install dir is on PATH
chmod +x "$INSTALL_DIR/promptpal"
echo "Installation complete. Run: promptpal --help"

# 7. Backend availability check
HAS_CLAUDE_CLI=false
HAS_API_KEY=false
command -v claude >/dev/null 2>&1 && HAS_CLAUDE_CLI=true || true
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && HAS_API_KEY=true || true

if $HAS_CLAUDE_CLI; then
  echo "Backend: Claude CLI (claude found on PATH)  ✓"
elif $HAS_API_KEY; then
  echo "Backend: API key (ANTHROPIC_API_KEY set)  ✓"
else
  echo ""
  echo "Warning: No backend configured."
  echo "  Option 1: Install Claude Code and run 'claude auth login'"
  echo "  Option 2: export ANTHROPIC_API_KEY=\"sk-ant-...\""
fi
```

### First-Run Setup

If `~/.promptpal/` does not exist when `promptpal` is invoked, `core/setup.py` runs a guided init:

```
PromptPal — first run setup
────────────────────────────
Config directory: ~/.promptpal/  ✓ created
Default config:   ~/.promptpal/config.json  ✓ written
System prompt:    ~/.promptpal/system_prompt.txt  ✓ written

ANTHROPIC_API_KEY is set.  ✓

Running a demo improvement pass...

Original: "write a function"
Improved: "Write a Python function that [...]"

Setup complete. Run `promptpal --help` for usage.
```

---

## Error Handling

### Error Categories and Responses

| Category | Detection | User Message | Exit Code |
|----------|-----------|--------------|-----------|
| Missing API key | `ANTHROPIC_API_KEY` not in env (API backend selected) | Actionable env var message | 1 |
| Auth failure | HTTP 401 | "API key rejected. Check ANTHROPIC_API_KEY." | 1 |
| Rate limit | HTTP 429 | "Rate limited. Retrying in Xs..." (auto-retry) | — |
| Network error | `ConnectionError` | "Network error. Retrying..." (retry once) | 1 |
| API server error | HTTP 5xx | "Anthropic API error. Retrying..." (backoff) | 1 |
| Config parse error | `json.JSONDecodeError` | "Config file corrupt at ~/.promptpal/config.json. Delete it to reset." | 1 |
| History write error | `OSError` on write | "Warning: could not save session to history." (non-fatal) | — |
| Interrupted (Ctrl-C) | `KeyboardInterrupt` | "\nCancelled." | 130 |
| No backend available | Neither `claude` on PATH nor `ANTHROPIC_API_KEY` set | Multi-line setup instructions listing both options | 1 |
| Claude CLI not found | `--backend claude-cli` forced but `claude` absent | "Error: claude CLI not found on PATH. Install Claude Code first." | 1 |
| Claude CLI auth failure | Non-zero exit + auth keyword in stderr | "Claude CLI auth failed. Run: claude auth login" | 1 |
| WSL HOME regression | `HOME` starts with `/mnt/c/` or `/c/` | "Warning: HOME appears to be a Windows path. Launch from WSL: wsl -d Ubuntu -- promptpal" | 1 |
| Clipboard unavailable | No provider found; `--copy` requested | "Warning: no clipboard provider found. Install xclip or xsel." (non-fatal) | — |

All errors go to stderr. stdout is reserved for the improved prompt output (important for piped usage).

---

## GUI — Phase 2 Architecture

### Technology Choice: Tauri

- Rust backend + WebView frontend (HTML/CSS/JS or Svelte)
- Binary is self-contained; no Chromium bundled
- Communicates with the Python core via Tauri commands (spawns subprocess or embeds via PyO3)
- Shares `~/.promptpal/` store; no IPC daemon needed

### Window Layout

```
┌─────────────────────────────────────────────────────────────┐
│  PromptPal                                        ─  □  ✕   │
├───────────────┬─────────────────────────────────────────────┤
│  HISTORY      │  RAW INPUT              │  IMPROVED OUTPUT  │
│  ─────────    │  ─────────────────────  │  ───────────────  │
│  [session 1]  │  [text area]            │  [text area]      │
│  [session 2]  │                         │                   │
│  [session 3]  │  Model: [dropdown]      │  [diff toggle]    │
│               │  Iterations: [1]        │                   │
│  [search box] │                         │  [Accept] [Iter.] │
│               │  [Improve ▶]            │  [Discard][Copy]  │
└───────────────┴─────────────────────────┴───────────────────┘
```

### Shared Store Concurrency

Both CLI and GUI write history files independently. Atomic rename (POSIX) prevents corruption. The index file is the only shared mutable state; both processes use the tmp → rename pattern. No file locking is needed for single-user local use.

---

## Testing Strategy

### Unit Tests (`tests/unit/`)

| Module | Tests |
|--------|-------|
| `test_api.py` | Mock HTTP responses for 200, 401, 429, 503; verify retry logic and error messages |
| `test_backend.py` | See detailed test list below |
| `test_platform.py` | See detailed test list below |
| `test_history.py` | Atomic write, index upsert, eviction, search |
| `test_config.py` | Load defaults, override with file, override with CLI flags; missing file fallback; `preferred_backend` field |
| `test_diff.py` | Short prompt (< 3 lines) shows no diff; long prompt shows unified diff |

**`tests/unit/test_backend.py`**

| Test | Description |
|------|-------------|
| `test_auto_detect_prefers_cli` | When `claude` is on PATH and API key is set, `resolve_backend(None)` returns `CliBackend` |
| `test_auto_detect_api_fallback` | When `claude` is NOT on PATH but API key is set, returns `ApiBackend` |
| `test_auto_detect_no_backend` | When neither is available, raises `NoBackendError` with correct message |
| `test_force_api_backend` | `resolve_backend("api-key")` returns `ApiBackend` even when `claude` is on PATH |
| `test_force_cli_backend` | `resolve_backend("claude-cli")` returns `CliBackend` |
| `test_force_cli_missing` | `resolve_backend("claude-cli")` raises `FileNotFoundError` when `claude` not on PATH |
| `test_cli_backend_auth_error` | Auth error in claude stderr maps to correct exit message |
| `test_cli_backend_stream_json_roundtrip` | Mock `claude` subprocess; verify each `messages` turn becomes one stream-json envelope on stdin and the `result` envelope on stdout populates `BackendResponse.text` + `input_tokens`/`output_tokens` |
| `test_cli_backend_passes_bare_and_system_prompt_file` | Assert spawned argv contains `--bare`, `--no-session-persistence`, and `--system-prompt-file <path>` (D-10 hard requirement) |
| `test_cli_backend_ignores_system_arg` | `complete(system="ignored", ...)` does not propagate the arg to the subprocess; only the bundled file is used |
| `test_cli_backend_result_error_envelope` | `{"type":"result","is_error":true,"result":"..."}` raises `RuntimeError` with the message text |

**`tests/unit/test_platform.py`**

| Test | Description |
|------|-------------|
| `test_detect_wsl2` | Mock `/proc/sys/kernel/osrelease` with `microsoft-wsl2` → `is_wsl=True, wsl_version=2` |
| `test_detect_not_wsl` | Mock osrelease without `microsoft` → `is_wsl=False` |
| `test_home_regression_ntfs` | `HOME=/mnt/c/Users/sneak` → `_resolve_home` returns passwd home |
| `test_home_valid_wsl` | `HOME=/home/alex` → `_resolve_home` returns `/home/alex` unchanged |
| `test_clipboard_clip_exe_wsl` | WSL + no xclip/xsel → `clip.exe` selected |
| `test_clipboard_xclip` | xclip present → `xclip -selection clipboard` selected |
| `test_clipboard_none` | No provider → empty list returned |

### Integration Tests (`tests/integration/`)

| Test | Description |
|------|-------------|
| `test_pipeline.py` | End-to-end with a mocked API: prompt in → improved out → history written |
| `test_backend_detection.py` | See detailed test list below |
| `test_stdin.py` | Verify piped input works: `echo "prompt" \| python3 core/cli.py --quiet` |
| `test_flags.py` | Verify `--no-history`, `--quiet`, `--output json`, `--backend`, `--status` behave correctly |

**`tests/integration/test_backend_detection.py`**

| Test | Description |
|------|-------------|
| `test_pipeline_uses_cli_backend` | Mock `claude` subprocess; verify improved prompt returned and `backend: "claude-cli"` written to session JSON |
| `test_pipeline_uses_api_backend` | Mock HTTP; verify `backend: "api-key"` in session JSON |
| `test_pipeline_no_backend_exits_1` | No claude, no API key → verify exit code 1 and correct error text on stderr |

### Coverage Target

80% minimum. Run with:

```bash
pytest --cov=core --cov-report=term-missing tests/
```

### Manual Test Checklist (pre-release)

- [ ] `promptpal "write a sorting function"` → improved prompt displayed
- [ ] Diff shown for prompts > 3 lines
- [ ] `[i]terate` with feedback → second pass preserves context
- [ ] Session written to `~/.promptpal/history/`
- [ ] `promptpal --show-history` lists session
- [ ] `promptpal --search "sorting"` returns session
- [ ] `echo "prompt" | promptpal --quiet` outputs only improved text
- [ ] `promptpal --output json "prompt"` returns valid JSON
- [ ] Missing `ANTHROPIC_API_KEY` shows actionable error
- [ ] Rate limit response triggers retry with correct backoff
- [ ] Install and run inside WSL2 Ubuntu 22.04 — `~/.promptpal/` created at `/home/<user>/`
- [ ] `promptpal --status` shows `Platform: WSL2` and correct backend
- [ ] `promptpal --copy "prompt"` copies to Windows clipboard via `clip.exe` in WSL
- [ ] Files written by promptpal pass `file <path>` → "ASCII text" (no CRLF)
- [ ] Launch with `HOME=/mnt/c/Users/sneak` → exits 1 with WSL warning
- [ ] With `claude` on PATH, no `ANTHROPIC_API_KEY` → uses claude-cli backend
- [ ] With both available, no `--backend` → CLI backend preferred
- [ ] `--backend api-key` forces API even when `claude` is on PATH
- [ ] Neither backend available → exits 1 with both setup options listed
- [ ] Claude auth expired → exits 1 with "claude auth login" message

---

## Build and Release

### Phase 1 — CLI

```bash
# Run tests
pytest tests/

# Package for distribution (creates promptpal-<version>.tar.gz)
./scripts/package.sh

# Install locally
./install.sh
```

### Phase 2 — GUI

```bash
# Install Tauri prerequisites (Rust toolchain)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Development
cd gui && npm install && npm run tauri dev

# Build native installer
npm run tauri build
# Output: gui/src-tauri/target/release/bundle/
```

---

## Open Questions

1. ~~**CLI runtime:** Stay with bash + python3, or port to Go/Rust?~~ — resolved: stay with bash + python3. Python stdlib covers HTTP, JSON, subprocess, atomic file writes; the hot path is I/O-bound so Go/Rust offers no user-visible benefit; ubiquitous on the target platforms (Linux, macOS, WSL Ubuntu); zero install friction. The bash entrypoint stays a thin shim — PATH integration, HOME guard, exec into `python3 -m core.cli`. Revisit only if real users hit a scenario where bash+python startup time becomes a workflow blocker.
2. ~~**`jq` dependency:** Hard-require it, or pure bash/python3?~~ — resolved: no `jq`. All JSON handling lives in Python via the stdlib `json` module (no parsing in bash at all). Keeps the dependency story to "python3". Bash never touches JSON.
3. ~~**System prompt source:** Canonical URL for `--update-system-prompt`?~~ — resolved: bundle the prompt inside the package at `core/system_prompt.txt` so first-run works offline. `--update-system-prompt` fetches `https://raw.githubusercontent.com/z0rd0n88/PromptPal/main/core/system_prompt.txt` plus a sibling `core/system_prompt.sha256` for verification, then atomic-replaces the local copy and prints a diff. Same repo as the source — no separate prompt-hosting infra.
4. ~~**GUI timeline:** Build Phase 2 before or after CLI reaches feature-complete?~~ — resolved: Phase 2 starts after the CLI is stable. No fixed calendar gate; "stable" means the refinement loop, backend auto-detect, history persistence, and WSL guard are all proven in real use against both backends.
5. ~~**Distribution channel:** Homebrew formula, apt PPA, Snap, winget?~~ — resolved: target Windows distribution. Primary channel is winget (with a signed `.msi` or `.exe` installer); WSL Ubuntu is the supported runtime, so the installer must lay down both the WSL-side `bin/promptpal` + `core/` tree and a Windows-side launcher that shells into `wsl -d Ubuntu -- promptpal`. Homebrew/apt/Snap deferred until post-Windows-launch. **Follow-up:** revise the `## Build and Release` section to describe the winget submission + signing pipeline; the current `package.sh` tarball workflow is insufficient.
6. ~~**Streaming API in bash:** python3 subprocess handles SSE streaming~~ — resolved: CLI backend uses blocking subprocess; API backend uses SSE via python3. No bash-level streaming needed.
7. ~~**Claude CLI multi-turn flags (S-1):**~~ — resolved by probe of `claude 2.1.143 --help` (2026-05-15). The CLI exposes three multi-turn mechanisms; PromptPal will use **`--input-format=stream-json --output-format=stream-json` with `-p`**, which streams a JSON conversation through stdin/stdout in the same shape as the Messages API. `--resume`/`--session-id` and `--continue` are rejected because they force the CLI to own session state (on-disk, cwd-coupled), conflicting with our in-memory `messages` list. **`_build_prompt` flattening is dropped.** `CliBackend.complete()` will: spawn `claude -p --bare --input-format=stream-json --output-format=stream-json --model <id> --no-session-persistence --system-prompt-file <bundled-prompt>`, write each turn as one `{"type":"user","message":{"role":"user","content":...}}` JSON line, read the assistant turn(s) back as JSON envelopes.
8. ~~**Claude CLI model flag (S-2):**~~ — resolved end-to-end (2026-05-15). `claude -p --model claude-sonnet-4-6 --no-session-persistence --output-format json` returned a valid response with `"modelUsage":{"claude-sonnet-4-6":{...}}` confirming the requested model. `--help` text confirms the flag accepts both aliases (`sonnet`, `opus`) and full IDs (`claude-sonnet-4-6`) — same set the API takes. Pass `--model <id>` through unchanged in `CliBackend`; no translation layer.
9. ~~**`clip.exe` UTF-8 (S-3):**~~ — resolved by probe (2026-05-15). `printf 'PromptPal UTF-8 test: αβγ 中文 emoji 🎉 — em-dash\n' | clip.exe` round-tripped perfectly via PowerShell `Get-Clipboard` — Greek, CJK, emoji, and em-dash all preserved. No BOM needed. Implementation: pipe the UTF-8-encoded Python string straight into `clip.exe` stdin.

## Findings discovered while resolving open questions

- **CLI default system prompt is huge.** A trivial probe (`echo "Reply with exactly one word: ok" | claude -p --model claude-sonnet-4-6 --no-session-persistence --output-format json`) reported `cache_creation_input_tokens: 27982` and `total_cost_usd: 0.1089`. `claude -p` ships the full Claude Code system prompt by default. PromptPal would burn ~$0.10 per refinement turn unless we suppress it. **`CliBackend` MUST always pass `--bare` and `--system-prompt-file <bundled-promptpal-prompt>`** so the refinement model gets only PromptPal's prompt-improver instructions, not Code's tool-use scaffolding. Cost on a `--bare` call should be in the cents, not dimes.
- **Clipboard provider order on bare WSL Ubuntu:** `xclip`, `xsel`, `pbcopy` are NOT installed by default on the dev host; only `clip.exe` is reachable (via `/mnt/c/Windows/system32/clip.exe`). `core/platform.py` should NOT assume any X-server tool is present and must keep `clip.exe` as the WSL primary, falling through to "none" only when both fail.
10. ~~**`preferred_backend` persistence (S-4):** Should passing `--backend` persist the choice to `config.json`?~~ — resolved: yes, persist the choice. When `--backend <name>` is supplied, write `preferred_backend: <name>` to `config.json` (atomic `tempfile.mkstemp` → `os.rename`, same pattern as history writes) so subsequent invocations default to the same backend without re-passing the flag. Implementation note: only update the file when the resolved backend differs from the on-disk value, to avoid spurious mtime churn. To restore auto-detect after persisting, the user runs `--backend auto` (treated as "unset `preferred_backend` and re-detect").
