"""Claude CLI backend (SPEC §6, P1-BKND-03/09/10/12, D-7, D-10).

This module implements :class:`CliBackend`, the subprocess-based backend
that invokes the locally installed ``claude`` CLI using its native
``--input-format=stream-json`` / ``--output-format=stream-json`` pipe.

The full command vector is::

    claude --print --model <m> \\
           --system-prompt-file <path> \\
           --input-format=stream-json --output-format=stream-json \\
           --verbose

``--system-prompt-file`` lets the system prompt live as a real file (we
write the ``system`` argument to a 0600 tempfile per call and clean up
afterwards). ``--verbose`` is mandated by the Claude CLI when ``--print``
is combined with ``--output-format=stream-json``; the CLI exits 1 with
``"--output-format=stream-json requires --verbose"`` otherwise. It does
not add stderr chatter — the extra detail rides on the stream-json event
channel, where :func:`_extract_text_from_event` already ignores
non-``assistant`` event types by design.

``--bare`` was originally specified by PRD D-10 to strip Claude Code
chrome from output, but the Claude CLI implementation couples ``--bare``
to authentication: with ``--bare`` set, ``Anthropic auth is strictly
ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and keychain
are never read)``. That breaks the primary auto-detect path (a user
who ran ``claude auth login`` and has no ``ANTHROPIC_API_KEY`` exported
hits a synthetic ``"Not logged in"`` stream-json reply with exit 0). We
therefore omit ``--bare``: the extra ``system``/``rate_limit_event``
envelopes that come back are ignored by :func:`_extract_text_from_event`
on the read side, so output parsing is unaffected. The trade-off is
that the user's Claude Code hooks, plugin sync, and CLAUDE.md
auto-discovery run on every PromptPal call. Users who want the
``--bare`` perf path can route through :class:`ApiBackend` instead by
exporting ``ANTHROPIC_API_KEY`` and running with
``promptpal --backend api-key`` (which persists the preference).

The ``messages`` array is fed to ``claude`` as NDJSON on **stdin**; the
assistant response is reassembled from ``assistant`` events on
**stdout**. This replaces the obsolete ``_build_prompt`` flattener that
collapsed turns into a single ``"Human: ... Assistant:"`` string —
prompts that contained the literal substring ``"Human:"`` confused that
flattener and produced garbled output. With the stream-json pipe the
role boundary is structural, so the literal-``"Human:"`` regression
cannot recur (AC-MT-01).

Public surface
--------------

- :class:`CliBackend` — concrete :class:`Backend` for the CLI path.
- :class:`CliError` / :class:`CliAuthError` / :class:`CliNotFoundError` /
  :class:`CliInvocationError` — error hierarchy. ``CliAuthError`` carries
  :data:`AUTH_ERROR_MESSAGE` (P1-ERR-11); ``CliNotFoundError`` carries
  :data:`CLI_NOT_FOUND_MESSAGE` (P1-ERR-10).
- Constants :data:`DEFAULT_EXECUTABLE`, :data:`AUTH_FAILURE_KEYWORDS`,
  :data:`AUTH_ERROR_MESSAGE`, :data:`CLI_NOT_FOUND_MESSAGE`,
  :data:`STREAM_JSON_FLAGS`.

Auth failure detection (P1-BKND-09)
-----------------------------------

Exit code non-zero **and** stderr contains any of the
case-insensitive substrings in :data:`AUTH_FAILURE_KEYWORDS` ::

    ("authentication", "unauthorized", "auth", "login", "token")

→ raises :class:`CliAuthError` with the canonical
``"Claude CLI auth failed. Run: claude auth login"`` message. Other
non-zero exits raise :class:`CliInvocationError`.

Stdout diagnostic fallback
--------------------------

Under ``--output-format=stream-json`` the CLI reports *non-auth*
failures on **stdout** (as ``result``/``api_retry`` events), leaving
stderr empty — most commonly when an overloaded API (HTTP 529) makes
``claude`` exhaust its 10 internal retries and exit 1. When stderr is
empty, :func:`_summarize_stdout_failure` mines that stdout stream so
:class:`CliInvocationError` carries the real reason (e.g.
``"rate_limit (HTTP 529) after 10 retries (claude gave up)"``) instead
of a bare ``"<no stderr>"``.

Token accounting (P1-BKND-10)
-----------------------------

CLI turns record ``input_tokens=None`` and ``output_tokens=None`` per
the SPEC §4 schema — the CLI does not surface usage on the
stream-json transcript, and we never invent values.

Testability
-----------

The subprocess layer is injected via the ``runner`` constructor kwarg.
The default runner is :func:`_default_runner` (a :mod:`subprocess`
wrapper). Tests substitute a callable that returns canned
:class:`_CliRunResult` instances without spawning a real process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable

from core.backend import Backend, BackendResponse, Message


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_EXECUTABLE: str = "claude"

#: Sub-strings checked case-insensitively against ``claude`` stderr on
#: a non-zero exit. Any match maps to :class:`CliAuthError`
#: (P1-BKND-09 / AC-BKND-06). Order doesn't matter; the longer keywords
#: come first only because they're more specific.
AUTH_FAILURE_KEYWORDS: tuple[str, ...] = (
    "authentication",
    "unauthorized",
    "auth",
    "login",
    "token",
)

AUTH_ERROR_MESSAGE: str = "Claude CLI auth failed. Run: claude auth login"
CLI_NOT_FOUND_MESSAGE: str = (
    "Error: claude CLI not found on PATH. Install Claude Code first."
)

#: Flag tail shared by every ``complete()`` invocation (D-7, D-10). Held
#: as a constant so a test can assert the exact ordering without
#: rebuilding the full argv.
STREAM_JSON_FLAGS: tuple[str, ...] = (
    "--input-format=stream-json",
    "--output-format=stream-json",
    "--verbose",
)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class CliError(Exception):
    """Base class for :class:`CliBackend` failures."""


class CliNotFoundError(CliError):
    """The ``claude`` binary was not found on ``PATH`` (P1-ERR-10)."""

    MESSAGE = CLI_NOT_FOUND_MESSAGE


class CliAuthError(CliError):
    """Non-zero exit with an auth keyword in stderr (P1-ERR-11)."""

    MESSAGE = AUTH_ERROR_MESSAGE


class CliInvocationError(CliError):
    """Non-zero exit that was not classified as an auth failure."""


# ---------------------------------------------------------------------------
# Runner abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CliRunResult:
    """Outcome of a single ``claude`` invocation.

    ``stdout`` is the *raw* bytes emitted by the subprocess (one JSON
    object per line in stream-json mode). Parsing into events happens in
    :func:`_parse_stream_json` so the runner stays trivial to fake.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[Sequence[str], bytes], _CliRunResult]
"""Signature of the injectable subprocess runner.

Receives the full argv and the NDJSON stdin bytes; returns a
:class:`_CliRunResult`. Must raise :class:`CliNotFoundError` when the
executable is missing from ``PATH`` — all other failure modes are
surfaced via ``exit_code`` so the caller can apply the auth-keyword
heuristic uniformly.
"""


def _default_runner(argv: Sequence[str], stdin_bytes: bytes) -> _CliRunResult:
    """Spawn ``claude`` via :mod:`subprocess` and collect its output.

    ``FileNotFoundError`` from ``subprocess.run`` (binary missing) is
    translated to :class:`CliNotFoundError` so the CLI layer can surface
    the SPEC §12 message verbatim.
    """
    try:
        proc = subprocess.run(
            list(argv),
            input=stdin_bytes,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise CliNotFoundError(CLI_NOT_FOUND_MESSAGE) from e
    return _CliRunResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


# ---------------------------------------------------------------------------
# Pure helpers (also unit-tested directly)
# ---------------------------------------------------------------------------


def _is_auth_failure(stderr_text: str) -> bool:
    """Return ``True`` when stderr carries an auth-failure keyword.

    Comparison is case-insensitive (per P1-BKND-09).
    """
    lower = stderr_text.lower()
    return any(kw in lower for kw in AUTH_FAILURE_KEYWORDS)


def _normalize_content(content: Any) -> Any:
    """Coerce a turn's ``content`` into the CLI's block-array shape.

    The Claude CLI's ``--input-format=stream-json`` parser scans each
    message's content blocks for tool markers (``"tool_use_id" in block``)
    when rebuilding multi-turn history, so ``content`` must be a list of
    block *objects* — not a bare string. A string makes JavaScriptCore
    iterate it character-by-character and throw ``W is not an Object.
    (evaluating '"tool_use_id"in W')``, which surfaces as ``claude exited
    1`` on the first ``[i]terate`` turn (the single-turn first call slips
    through because a lone trailing user prompt isn't block-scanned).

    A string is wrapped as a single ``text`` block; an already-list value
    is forwarded unchanged (idempotent — callers may pass block-shaped
    content directly); anything else is left as-is so we never mask an
    unexpected shape with a lossy guess.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content


def _serialize_messages_ndjson(messages: Sequence[dict[str, Any]]) -> bytes:
    """Encode ``messages`` as NDJSON in Claude Code's stream-json input shape.

    The Claude CLI's ``--input-format=stream-json`` expects each line to
    be an envelope::

        {"type": "user"|"assistant", "message": {"role": ..., "content": [...]}}

    not a bare ``{"role": ..., "content": ...}``. The unwrapped shape is
    silently ignored — claude exits 0 after running its hooks but never
    calls the model, producing a stream of ``system`` events and no
    ``assistant`` reply. Empirically reproduced against
    ``claude-code 2.1.143``.

    Each input ``m`` (PromptPal's internal Messages-API-style shape) is
    wrapped as ``{"type": m["role"], "message": {**m, "content": ...}}``,
    with ``content`` normalized to a block array via
    :func:`_normalize_content` (a new dict is built, never mutating ``m``).
    Empty list → empty bytes. A trailing newline is appended so the final
    object terminates cleanly when ``claude`` reads stdin line-by-line.
    """
    if not messages:
        return b""
    wrapped = [
        {
            "type": m["role"],
            "message": {**m, "content": _normalize_content(m.get("content"))},
        }
        for m in messages
    ]
    lines = [json.dumps(w, ensure_ascii=False) for w in wrapped]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_argv(
    executable: str, model: str, system_prompt_path: str
) -> list[str]:
    """Build the full ``claude`` argv.

    Order: ``--print --model <m> --system-prompt-file <p>
    --input-format=stream-json --output-format=stream-json --verbose``.

    Note: PRD D-10 originally specified ``--bare`` in this slot; it was
    removed because the Claude CLI couples ``--bare`` to API-key-only
    auth (see module docstring).
    """
    return [
        executable,
        "--print",
        "--model",
        model,
        "--system-prompt-file",
        system_prompt_path,
        *STREAM_JSON_FLAGS,
    ]


def _extract_text_from_event(event: dict[str, Any]) -> list[str]:
    """Pull assistant text chunks out of one stream-json event.

    Recognized shapes:

    - ``{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}``
      — the canonical envelope ``claude --output-format=stream-json``
      emits for a model reply.
    - ``{"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}``
      — the streaming-delta shape used by some claude versions.

    Anything else (``system``, ``user``, ``result``, tool blocks,
    unknown types) contributes no text. We never raise on an unknown
    shape — forward-compat by design.
    """
    parts: list[str] = []
    etype = event.get("type")
    if etype == "assistant":
        msg = event.get("message") or {}
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    elif etype == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            text = delta.get("text")
            if isinstance(text, str):
                parts.append(text)
    return parts


def _parse_stream_json(stdout: bytes) -> str:
    """Concatenate all assistant text out of an NDJSON stdout stream.

    Lines that don't parse as JSON are silently dropped (the CLI
    occasionally prints non-JSON warnings on stdout under
    ``--output-format=stream-json``; per spec we never let one of those
    abort the response assembly).
    """
    chunks: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            chunks.extend(_extract_text_from_event(event))
    return "".join(chunks)


def _format_retry_summary(retry: dict[str, Any], count: int) -> str:
    """Render one ``api_retry`` event as a human-readable failure reason.

    The Claude CLI retries an overloaded/rate-limited API (HTTP 529/429)
    with exponential backoff up to ``max_retries`` before exiting
    non-zero; the last such event tells us *why* it gave up.
    """
    error = retry.get("error")
    status = retry.get("error_status")
    if isinstance(error, str) and error and isinstance(status, int):
        reason = f"{error} (HTTP {status})"
    elif isinstance(error, str) and error:
        reason = error
    elif isinstance(status, int):
        reason = f"HTTP {status}"
    else:
        reason = "API error"
    maximum = retry.get("max_retries")
    attempts = maximum if isinstance(maximum, int) else count
    return f"{reason} after {attempts} retries (claude gave up)"


def _summarize_stdout_failure(stdout: bytes) -> str:
    """Best-effort one-line failure reason from a stream-json stdout stream.

    On a non-zero exit the Claude CLI reports *why* on **stdout** (as
    stream-json ``system``/``result`` events), not on stderr — so without
    this the user only ever sees ``claude exited 1: <no stderr>`` and has
    no idea a transient, retryable overload (HTTP 529) is to blame. We
    scan, in priority order, for:

    1. a terminal ``result`` event flagged ``is_error`` (the CLI's own
       authoritative final status), then
    2. ``api_retry`` events (the CLI exhausting its retries against an
       overloaded/rate-limited API), summarising the last one.

    Returns ``""`` when stdout carries no recognisable failure signal, so
    the caller falls back to its own placeholder. Mirrors
    :func:`_parse_stream_json`'s contract: unparseable lines are skipped
    and a novel event shape never raises — a parsing slip must not mask
    the original exit code.
    """
    result_reason = ""
    result_is_subtype_enum = False
    last_retry: dict[str, Any] | None = None
    retry_count = 0
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result" and event.get("is_error"):
            for key in ("error", "result", "subtype"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    result_reason = value.strip()
                    # M13 (issue #30): the ``subtype`` field is an enum
                    # token (e.g. ``error_during_execution``), not a
                    # human-readable failure reason. Track that fact so
                    # the suffix can be appended AFTER the 500-char
                    # truncation below — otherwise a near-cap reason
                    # could swallow the marker mid-word.
                    result_is_subtype_enum = key == "subtype"
                    break
        elif (
            event.get("type") == "system"
            and event.get("subtype") == "api_retry"
        ):
            # P1-FIX-28-05: match the documented system/api_retry wire
            # shape; don't count other event types that happen to reuse
            # the ``api_retry`` subtype.
            retry_count += 1
            last_retry = event
    if result_reason:
        truncated = result_reason[:500]
        if result_is_subtype_enum:
            truncated = f"{truncated} (subtype)"
        return truncated
    if last_retry is not None:
        return _format_retry_summary(last_retry, retry_count)[:500]
    return ""


# ---------------------------------------------------------------------------
# CliBackend
# ---------------------------------------------------------------------------


class CliBackend(Backend):
    """Claude CLI backend (P1-BKND-03).

    The CLI is invoked once per ``complete()`` call; multi-turn context
    is preserved by sending the *full* ``messages`` array on every turn
    (D-7 / AC-MT-02).
    """

    def __init__(
        self,
        model: str,
        *,
        executable: str = DEFAULT_EXECUTABLE,
        runner: Runner | None = None,
    ) -> None:
        self._model = model
        self._executable = executable
        self._runner: Runner = runner or _default_runner

    @property
    def name(self) -> str:
        return f"claude-cli ({self._model})"

    # -- public API ----------------------------------------------------------

    def complete(
        self,
        system: str,
        messages: list[Message],
        stream: bool = False,
    ) -> BackendResponse:
        """Send a single completion turn through ``claude``.

        ``system`` is written to a 0600 tempfile (cleaned up after the
        call), then its path is passed via ``--system-prompt-file``.
        ``messages`` is fed as NDJSON on stdin. ``stream`` is currently
        a no-op for this backend — the CLI's stream-json output already
        arrives in chunks, but real-time tee-to-stdout is a P1-PIPE-07
        SHOULD that lands with the pipeline (US-009/US-010).
        """
        # ``stream`` is accepted for ABC parity but not surfaced to the
        # user terminal in this phase (P1-PIPE-07 is SHOULD, not MUST,
        # and the CLI path's UX flows through US-010).
        del stream
        path = _write_system_prompt_tempfile(system)
        try:
            argv = _build_argv(self._executable, self._model, path)
            stdin_bytes = _serialize_messages_ndjson(messages)
            result = self._runner(argv, stdin_bytes)
        finally:
            _safe_unlink(path)
        if result.exit_code != 0:
            stderr_text = result.stderr.decode("utf-8", errors="replace")
            if _is_auth_failure(stderr_text):
                raise CliAuthError(AUTH_ERROR_MESSAGE)
            # In stream-json mode claude reports failures on stdout (e.g. an
            # api_retry storm after an HTTP 529 overload), leaving stderr
            # empty. Fall back to the stdout diagnostic so the user sees the
            # real reason instead of a bare ``<no stderr>``.
            detail = stderr_text.strip()[:500]
            if not detail:
                detail = _summarize_stdout_failure(result.stdout)
            raise CliInvocationError(
                f"claude exited {result.exit_code}: {detail or '<no stderr>'}"
            )
        text = _parse_stream_json(result.stdout)
        return BackendResponse(text=text, input_tokens=None, output_tokens=None)

    def check_auth(self) -> bool:
        """Run ``claude --version`` and return ``True`` on exit 0 (P1-BKND-12).

        Binary-presence probe only — a real OAuth liveness check would
        need interactive user input, unsuitable for ``--status``. See
        :meth:`core.backend.Backend.check_auth` for the two-tier
        rationale (M14, issue #30) and how this differs from
        :meth:`core.api_backend.ApiBackend.check_auth`'s real round-trip.
        """
        try:
            result = self._runner([self._executable, "--version"], b"")
        except CliNotFoundError:
            return False
        except CliError:
            return False
        return result.exit_code == 0


# ---------------------------------------------------------------------------
# Internal helpers (filesystem)
# ---------------------------------------------------------------------------


def _write_system_prompt_tempfile(system: str) -> str:
    """Create a 0600 tempfile with ``system`` and return its path.

    LF line endings (NFR-08 / P1-PLAT-08). ``mkstemp`` already opens
    with mode 0600 on POSIX, which keeps a multi-user box from reading
    the user's custom prompt.

    The raw fd returned by ``tempfile.mkstemp`` is closed explicitly if
    ``os.fdopen`` raises before adopting it (e.g. under fd-table
    exhaustion / EMFILE). Without this guard, the failure would leak a
    file descriptor on every retry — compounding the exhaustion exactly
    when more open fds matter most.
    """
    fd, path = tempfile.mkstemp(prefix="promptpal-syspr-", suffix=".md")
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        except BaseException:  # signal-safe: close raw fd even on KeyboardInterrupt
            os.close(fd)
            raise
        with f:
            f.write(system)
    except Exception:
        _safe_unlink(path)
        raise
    return path


def _safe_unlink(path: str) -> None:
    """``os.unlink`` that swallows ``FileNotFoundError`` only."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:  # other ENOENT-adjacent errors get a stderr note
        print(
            f"Warning: could not remove temp file {path}: {e}",
            file=sys.stderr,
        )
