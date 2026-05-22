"""Tests for core/cli_backend.py (US-005, SPEC §6, P1-BKND-03/09/10/12, D-7, D-10).

This file is named for US-016's mapping:
``P1-BKND-03 → tests/unit/test_cli_backend_streamjson.py`` — it covers
the stream-json command vector, NDJSON stdin, auth-keyword detection,
the ``"Human:"`` literal regression bait, and the 3-iteration history
accumulation that motivated dropping the old ``_build_prompt`` flatten.

Coverage map (one test → one acceptance criterion or sub-rule):

  AC #1  Command vector with all six flags                  → test_argv_*
  AC #2  Messages array on stdin as NDJSON                  → test_stdin_*
  AC #3  Auth failure detection from non-zero + keyword     → test_auth_failure_*
  AC #4  check_auth() runs `claude --version`               → test_check_auth_*
  AC #5  Each turn records null tokens                      → test_tokens_null_*
  AC #6  Literal "Human:" prompt survives round-trip        → test_human_literal_*
  AC #7  3 iterations send all 5 prior entries on turn 3    → test_three_iterations_*
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.backend import BackendResponse
from core.cli_backend import (
    AUTH_ERROR_MESSAGE,
    AUTH_FAILURE_KEYWORDS,
    CLI_NOT_FOUND_MESSAGE,
    DEFAULT_EXECUTABLE,
    STREAM_JSON_FLAGS,
    CliAuthError,
    CliBackend,
    CliError,
    CliInvocationError,
    CliNotFoundError,
    _build_argv,
    _CliRunResult,
    _default_runner,
    _extract_text_from_event,
    _is_auth_failure,
    _parse_stream_json,
    _safe_unlink,
    _serialize_messages_ndjson,
    _summarize_stdout_failure,
    _write_system_prompt_tempfile,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _assistant_event(text: str) -> dict[str, Any]:
    """Canonical ``claude --output-format=stream-json`` assistant envelope."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _api_retry_event(
    attempt: int, *, status: int = 529, error: str = "rate_limit", max_retries: int = 10
) -> dict[str, Any]:
    """A ``system/api_retry`` envelope as emitted on stdout while the CLI
    retries an overloaded/rate-limited API (observed against claude 2.1.143)."""
    return {
        "type": "system",
        "subtype": "api_retry",
        "attempt": attempt,
        "max_retries": max_retries,
        "error_status": status,
        "error": error,
    }


def _result_error_event(error: str) -> dict[str, Any]:
    """A terminal ``result`` envelope flagged as an error."""
    return {"type": "result", "subtype": "error_during_execution", "is_error": True, "error": error}


def _ndjson(*events: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode("utf-8")


class _FakeRunner:
    """Programmable runner: returns canned :class:`_CliRunResult`s or raises.

    Records every (argv, stdin_bytes) pair so tests can assert on the
    exact command vector and stdin payload separately.
    """

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv, stdin_bytes):
        self.calls.append({"argv": list(argv), "stdin": bytes(stdin_bytes)})
        if not self._items:
            raise AssertionError("FakeRunner exhausted")
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ok(stdout: bytes = b"", stderr: bytes = b"") -> _CliRunResult:
    return _CliRunResult(exit_code=0, stdout=stdout, stderr=stderr)


def _fail(exit_code: int, stderr: bytes) -> _CliRunResult:
    return _CliRunResult(exit_code=exit_code, stdout=b"", stderr=stderr)


@pytest.fixture
def make_backend():
    """Factory that wires a CliBackend to a recordable :class:`_FakeRunner`."""

    def _make(
        items: list[Any], *, model: str = "claude-sonnet-4-6"
    ) -> tuple[CliBackend, _FakeRunner]:
        runner = _FakeRunner(items)
        backend = CliBackend(model=model, runner=runner)
        return backend, runner

    return _make


# ---------------------------------------------------------------------------
# Constants — pinned verbatim so accidental drift breaks the test
# ---------------------------------------------------------------------------


def test_auth_error_message_is_canonical() -> None:
    assert AUTH_ERROR_MESSAGE == "Claude CLI auth failed. Run: claude auth login"
    assert CliAuthError.MESSAGE == AUTH_ERROR_MESSAGE


def test_cli_not_found_message_is_canonical() -> None:
    assert CLI_NOT_FOUND_MESSAGE == (
        "Error: claude CLI not found on PATH. Install Claude Code first."
    )
    assert CliNotFoundError.MESSAGE == CLI_NOT_FOUND_MESSAGE


def test_auth_failure_keywords_match_spec() -> None:
    """PRD P1-BKND-09 lists exactly these five tokens (order doesn't matter)."""
    assert set(AUTH_FAILURE_KEYWORDS) == {
        "authentication",
        "unauthorized",
        "auth",
        "login",
        "token",
    }


def test_stream_json_flags_are_pinned() -> None:
    """Catch silent reordering of the format flags.

    ``--verbose`` is mandatory in this combo: the Claude CLI exits 1 with
    ``"--output-format=stream-json requires --verbose"`` when ``--print``
    is used without it. Pinning the tuple here makes accidental removal
    surface in CI instead of at first real invocation.
    """
    assert STREAM_JSON_FLAGS == (
        "--input-format=stream-json",
        "--output-format=stream-json",
        "--verbose",
    )


def test_default_executable_is_plain_claude() -> None:
    assert DEFAULT_EXECUTABLE == "claude"


# ---------------------------------------------------------------------------
# Backend ABC parity
# ---------------------------------------------------------------------------


def test_name_property_includes_model(make_backend) -> None:
    backend, _ = make_backend([])
    assert backend.name == "claude-cli (claude-sonnet-4-6)"


def test_constructor_does_not_invoke_runner() -> None:
    """CliBackend must not touch the subprocess at construction time.

    Mirror of ApiBackend's same-named guarantee: nothing expensive,
    nothing failable, no PATH probe until ``complete()`` or
    ``check_auth()`` runs.
    """
    runner = _FakeRunner([])
    CliBackend(model="m", runner=runner)
    assert runner.calls == []


# ---------------------------------------------------------------------------
# AC #1 — command vector
# ---------------------------------------------------------------------------


def test_argv_has_all_required_flags_in_order(make_backend) -> None:
    """Pinned argv shape.

    PRD D-10 originally included ``--bare`` at argv[4], but ``--bare``
    couples to ``ANTHROPIC_API_KEY``-only auth and broke OAuth users —
    so the flag was removed (see :mod:`core.cli_backend` docstring).
    The negative assertion at the end is a regression guard.
    """
    backend, runner = make_backend([_ok(_ndjson(_assistant_event("ok")))])
    backend.complete("sys-prompt", [{"role": "user", "content": "hi"}])
    argv = runner.calls[0]["argv"]
    assert argv[0] == "claude"
    assert argv[1] == "--print"
    assert argv[2:4] == ["--model", "claude-sonnet-4-6"]
    assert argv[4] == "--system-prompt-file"
    # argv[5] is the temp file path — content checked below
    assert argv[6] == "--input-format=stream-json"
    assert argv[7] == "--output-format=stream-json"
    assert argv[8] == "--verbose"
    assert len(argv) == 9
    assert "--bare" not in argv


def test_argv_model_flag_threads_through(make_backend) -> None:
    backend, runner = make_backend(
        [_ok(_ndjson(_assistant_event("ok")))], model="claude-opus-4-7"
    )
    backend.complete("", [{"role": "user", "content": "hi"}])
    argv = runner.calls[0]["argv"]
    assert argv[2:4] == ["--model", "claude-opus-4-7"]


def test_argv_executable_kwarg_overrides_default(monkeypatch) -> None:
    runner = _FakeRunner([_ok(_ndjson(_assistant_event("ok")))])
    backend = CliBackend(
        model="m", executable="/opt/bin/claude", runner=runner
    )
    backend.complete("", [{"role": "user", "content": "hi"}])
    assert runner.calls[0]["argv"][0] == "/opt/bin/claude"


def test_build_argv_helper_matches_spec() -> None:
    argv = _build_argv("claude", "claude-sonnet-4-6", "/tmp/sp.md")
    assert argv == [
        "claude",
        "--print",
        "--model",
        "claude-sonnet-4-6",
        "--system-prompt-file",
        "/tmp/sp.md",
        "--input-format=stream-json",
        "--output-format=stream-json",
        "--verbose",
    ]


def test_system_prompt_file_contains_the_passed_text(make_backend, tmp_path) -> None:
    captured_path: list[str] = []

    def runner(argv, stdin_bytes):
        # capture path while file still exists, read content here
        captured_path.append(argv[5])
        with open(argv[5], "rb") as f:
            captured_path.append(f.read().decode("utf-8"))  # type: ignore[arg-type]
        return _ok(_ndjson(_assistant_event("ok")))

    backend = CliBackend(model="m", runner=runner)
    backend.complete("you are a Pal", [{"role": "user", "content": "hi"}])
    path_used, contents = captured_path
    assert contents == "you are a Pal"
    # cleanup happens after the call returns
    assert not os.path.exists(path_used)


def test_system_prompt_tempfile_is_cleaned_up_on_error() -> None:
    """Even when the runner raises, the temp file must be removed."""
    paths: list[str] = []

    def runner(argv, stdin_bytes):
        paths.append(argv[5])
        raise CliInvocationError("boom")

    backend = CliBackend(model="m", runner=runner)
    with pytest.raises(CliInvocationError):
        backend.complete("sys", [{"role": "user", "content": "hi"}])
    assert paths
    assert not os.path.exists(paths[0])


def test_system_prompt_tempfile_helper_uses_lf_endings(tmp_path) -> None:
    """P1-PLAT-08: PromptPal writes only LF."""
    path = _write_system_prompt_tempfile("line one\nline two\n")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        assert b"\r\n" not in raw
        assert raw == b"line one\nline two\n"
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# AC #2 — NDJSON stdin
# ---------------------------------------------------------------------------


def test_stdin_is_ndjson_one_object_per_line(make_backend) -> None:
    """Each message is wrapped in the {"type":..., "message":{...}} envelope.

    Claude Code's --input-format=stream-json silently ignores the bare
    {"role":...,"content":...} shape (hooks fire, model is never called,
    exit 0 with no assistant events). See :func:`_serialize_messages_ndjson`.
    """
    backend, runner = make_backend([_ok(_ndjson(_assistant_event("ok")))])
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    backend.complete("", msgs)
    stdin_text = runner.calls[0]["stdin"].decode("utf-8")
    lines = stdin_text.rstrip("\n").split("\n")
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert parsed == [{"type": m["role"], "message": m} for m in msgs]


def test_stdin_terminates_with_a_single_newline(make_backend) -> None:
    backend, runner = make_backend([_ok(_ndjson(_assistant_event("ok")))])
    backend.complete("", [{"role": "user", "content": "hi"}])
    stdin = runner.calls[0]["stdin"]
    assert stdin.endswith(b"\n")
    assert not stdin.endswith(b"\n\n")


def test_stdin_empty_messages_sends_empty_bytes(make_backend) -> None:
    backend, runner = make_backend([_ok(_ndjson(_assistant_event("ok")))])
    backend.complete("", [])
    assert runner.calls[0]["stdin"] == b""


def test_stdin_preserves_unicode_without_ascii_escape(make_backend) -> None:
    """ensure_ascii=False so emoji / CJK stay readable on the wire."""
    backend, runner = make_backend([_ok(_ndjson(_assistant_event("ok")))])
    backend.complete("", [{"role": "user", "content": "héllo 🚀 你好"}])
    stdin_text = runner.calls[0]["stdin"].decode("utf-8")
    assert "héllo 🚀 你好" in stdin_text
    assert "\\u" not in stdin_text


def test_serialize_messages_ndjson_helper() -> None:
    """Each emitted line is the wrapped envelope, not the bare message."""
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    out = _serialize_messages_ndjson(msgs)
    decoded = out.decode("utf-8").rstrip("\n").split("\n")
    assert [json.loads(line) for line in decoded] == [
        {"type": "user", "message": {"role": "user", "content": "a"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "b"}},
    ]


def test_serialize_messages_ndjson_empty_is_empty_bytes() -> None:
    assert _serialize_messages_ndjson([]) == b""


# ---------------------------------------------------------------------------
# AC #3 — auth failure detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr_text",
    [
        "Error: AUTHENTICATION required",  # canonical
        "you must run claude auth login",
        "401 Unauthorized",
        "token expired",
        "auth required",
        "Please LOGIN first",
    ],
)
def test_auth_failure_detection_is_case_insensitive(make_backend, stderr_text: str) -> None:
    backend, _ = make_backend([_fail(1, stderr_text.encode("utf-8"))])
    with pytest.raises(CliAuthError) as exc:
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert str(exc.value) == AUTH_ERROR_MESSAGE


def test_auth_failure_only_when_exit_nonzero(make_backend) -> None:
    """Exit 0 with an auth-shaped stderr is NOT an auth failure."""
    backend, _ = make_backend(
        [_CliRunResult(exit_code=0, stdout=_ndjson(_assistant_event("ok")), stderr=b"warning: token rotation")]
    )
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert result.text == "ok"


def test_non_auth_nonzero_exit_raises_invocation_error(make_backend) -> None:
    backend, _ = make_backend([_fail(2, b"model overloaded; try again")])
    with pytest.raises(CliInvocationError) as exc:
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert "claude exited 2" in str(exc.value)
    assert "overloaded" in str(exc.value)


def test_invocation_error_excludes_no_stderr_placeholder(make_backend) -> None:
    """Empty stderr → message includes '<no stderr>' rather than dangling colon."""
    backend, _ = make_backend([_fail(7, b"")])
    with pytest.raises(CliInvocationError) as exc:
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert "<no stderr>" in str(exc.value)


def test_invocation_error_surfaces_stdout_retry_diagnostic(make_backend) -> None:
    """The real-world bug: claude exhausts API retries (HTTP 529) and exits
    non-zero with **empty stderr** — the reason rides on stdout as
    ``api_retry`` events. The error must surface that, not ``<no stderr>``.
    """
    stdout = _ndjson(
        {"type": "system", "subtype": "init"},
        _api_retry_event(1),
        _api_retry_event(10),
    )
    backend, _ = make_backend([_CliRunResult(exit_code=1, stdout=stdout, stderr=b"")])
    with pytest.raises(CliInvocationError) as exc:
        backend.complete("", [{"role": "user", "content": "hi"}])
    msg = str(exc.value)
    assert "claude exited 1" in msg
    assert "529" in msg
    assert "rate_limit" in msg
    assert "<no stderr>" not in msg


def test_invocation_error_surfaces_stdout_result_error(make_backend) -> None:
    """A terminal ``result`` event flagged ``is_error`` is the authoritative
    reason and is preferred over retry chatter."""
    stdout = _ndjson(
        _api_retry_event(1),
        _result_error_event("context length exceeded"),
    )
    backend, _ = make_backend([_CliRunResult(exit_code=1, stdout=stdout, stderr=b"")])
    with pytest.raises(CliInvocationError) as exc:
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert "context length exceeded" in str(exc.value)


def test_invocation_error_prefers_stderr_over_stdout_when_present(make_backend) -> None:
    """Existing behaviour is preserved: a non-empty stderr still wins, so the
    stdout scan is a pure fallback that can't regress today's messages."""
    stdout = _ndjson(_api_retry_event(10))
    backend, _ = make_backend(
        [_CliRunResult(exit_code=2, stdout=stdout, stderr=b"explicit stderr reason")]
    )
    with pytest.raises(CliInvocationError) as exc:
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert "explicit stderr reason" in str(exc.value)


def test_summarize_stdout_failure_summarizes_retry_storm() -> None:
    stdout = _ndjson(_api_retry_event(1), _api_retry_event(2), _api_retry_event(10))
    summary = _summarize_stdout_failure(stdout)
    assert "rate_limit" in summary
    assert "529" in summary
    assert "10" in summary  # max_retries / attempts surfaced


def test_summarize_stdout_failure_prefers_result_error_event() -> None:
    stdout = _ndjson(_api_retry_event(1), _result_error_event("the real reason"))
    assert _summarize_stdout_failure(stdout) == "the real reason"


def test_summarize_stdout_failure_ignores_successful_result() -> None:
    """A clean run carries a non-error ``result`` — it must not be mistaken
    for a failure reason."""
    stdout = _ndjson(
        _assistant_event("hi"),
        {"type": "result", "subtype": "success", "is_error": False},
    )
    assert _summarize_stdout_failure(stdout) == ""


def test_summarize_stdout_failure_empty_and_garbage_return_empty() -> None:
    assert _summarize_stdout_failure(b"") == ""
    assert _summarize_stdout_failure(b"not json\n{broken\n") == ""


def test_is_auth_failure_helper() -> None:
    assert _is_auth_failure("PLEASE auth login") is True
    assert _is_auth_failure("Authentication required") is True
    assert _is_auth_failure("unauthorized") is True
    assert _is_auth_failure("token expired") is True
    assert _is_auth_failure("login first") is True
    assert _is_auth_failure("model overloaded") is False
    assert _is_auth_failure("") is False


# ---------------------------------------------------------------------------
# AC #4 — check_auth() uses --version
# ---------------------------------------------------------------------------


def test_check_auth_runs_version_subcommand() -> None:
    runner = _FakeRunner([_ok(stdout=b"1.2.3\n")])
    backend = CliBackend(model="m", runner=runner)
    assert backend.check_auth() is True
    assert runner.calls[0]["argv"] == ["claude", "--version"]
    assert runner.calls[0]["stdin"] == b""


def test_check_auth_returns_false_on_nonzero_exit() -> None:
    runner = _FakeRunner([_fail(1, b"not authenticated")])
    backend = CliBackend(model="m", runner=runner)
    assert backend.check_auth() is False


def test_check_auth_returns_false_when_binary_missing() -> None:
    def runner(argv, stdin_bytes):
        raise CliNotFoundError(CLI_NOT_FOUND_MESSAGE)

    backend = CliBackend(model="m", runner=runner)
    assert backend.check_auth() is False


def test_check_auth_swallows_other_cli_errors() -> None:
    """Any :class:`CliError` from the runner → False, never propagated."""

    def runner(argv, stdin_bytes):
        raise CliError("transport blip")

    backend = CliBackend(model="m", runner=runner)
    assert backend.check_auth() is False


# ---------------------------------------------------------------------------
# AC #5 — null token accounting
# ---------------------------------------------------------------------------


def test_tokens_null_for_cli_turn(make_backend) -> None:
    backend, _ = make_backend([_ok(_ndjson(_assistant_event("improved")))])
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert isinstance(result, BackendResponse)
    assert result.text == "improved"
    assert result.input_tokens is None
    assert result.output_tokens is None


# ---------------------------------------------------------------------------
# AC #6 — literal "Human:" round-trip (regression bait for the dropped flattener)
# ---------------------------------------------------------------------------


def test_human_literal_in_user_content_survives_round_trip(make_backend) -> None:
    """AC-MT-01: a prompt containing ``"Human:"`` must not be mangled.

    The dropped ``_build_prompt`` flattener collapsed messages into a
    single string with ``"Human:"`` / ``"Assistant:"`` markers, which
    confused the model when those literals appeared in user content.
    With stream-json, role boundaries are structural — we verify the
    user content reaches the CLI byte-identically.
    """
    backend, runner = make_backend([_ok(_ndjson(_assistant_event("ok")))])
    weird = 'Original prompt mentioning "Human:" and Assistant: as text.'
    backend.complete("", [{"role": "user", "content": weird}])
    stdin_text = runner.calls[0]["stdin"].decode("utf-8")
    payload = json.loads(stdin_text.rstrip("\n"))
    assert payload == {
        "type": "user",
        "message": {"role": "user", "content": weird},
    }


# ---------------------------------------------------------------------------
# AC #7 — 3 successive iterations send all 5 prior entries on turn 3
# ---------------------------------------------------------------------------


def test_three_iterations_send_full_history_on_turn_three(make_backend) -> None:
    """AC-MT-02: turn 3's stdin contains all 5 prior messages as NDJSON.

    The refinement loop appends each turn's input + response to
    ``messages`` and re-invokes ``complete()`` — no truncation in
    Phase 1 (P1-LOOP-02). This test stands in for the loop by calling
    ``complete()`` three times with an accumulating list.
    """
    runs = [
        _ok(_ndjson(_assistant_event("turn-1 reply"))),
        _ok(_ndjson(_assistant_event("turn-2 reply"))),
        _ok(_ndjson(_assistant_event("turn-3 reply"))),
    ]
    backend, runner = make_backend(runs)
    messages: list[dict[str, Any]] = []

    # Turn 1
    messages.append({"role": "user", "content": "make this better"})
    backend.complete("sp", messages)
    messages.append({"role": "assistant", "content": "turn-1 reply"})
    # Turn 2 (user iterates)
    messages.append({"role": "user", "content": "shorter please"})
    backend.complete("sp", messages)
    messages.append({"role": "assistant", "content": "turn-2 reply"})
    # Turn 3 — this is the one PRD spec calls out
    messages.append({"role": "user", "content": "more concrete"})
    backend.complete("sp", messages)

    turn3_stdin = runner.calls[2]["stdin"].decode("utf-8")
    lines = turn3_stdin.rstrip("\n").split("\n")
    parsed = [json.loads(line) for line in lines]
    assert len(parsed) == 5
    # Each line is now the wrapped envelope; reach into ``message`` to
    # inspect the underlying Messages-API record.
    assert [m["type"] for m in parsed] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert [m["message"]["role"] for m in parsed] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert parsed[0]["message"]["content"] == "make this better"
    assert parsed[1]["message"]["content"] == "turn-1 reply"
    assert parsed[2]["message"]["content"] == "shorter please"
    assert parsed[3]["message"]["content"] == "turn-2 reply"
    assert parsed[4]["message"]["content"] == "more concrete"


def test_three_iterations_each_turn_produces_a_call(make_backend) -> None:
    """Three ``complete()`` calls → exactly three runner invocations."""
    backend, runner = make_backend(
        [
            _ok(_ndjson(_assistant_event("a"))),
            _ok(_ndjson(_assistant_event("b"))),
            _ok(_ndjson(_assistant_event("c"))),
        ]
    )
    msgs: list[dict[str, Any]] = []
    for i in range(3):
        msgs.append({"role": "user", "content": f"turn-{i}"})
        backend.complete("", msgs)
        msgs.append({"role": "assistant", "content": f"reply-{i}"})
    assert len(runner.calls) == 3


# ---------------------------------------------------------------------------
# stream-json parsing
# ---------------------------------------------------------------------------


def test_parse_stream_json_extracts_assistant_text() -> None:
    payload = _ndjson(_assistant_event("hello world"))
    assert _parse_stream_json(payload) == "hello world"


def test_parse_stream_json_concatenates_multiple_assistant_events() -> None:
    payload = _ndjson(
        _assistant_event("part-1 "),
        _assistant_event("part-2"),
    )
    assert _parse_stream_json(payload) == "part-1 part-2"


def test_parse_stream_json_concatenates_multiple_text_blocks() -> None:
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "alpha "},
                {"type": "text", "text": "beta"},
            ],
        },
    }
    assert _parse_stream_json(_ndjson(event)) == "alpha beta"


def test_parse_stream_json_ignores_non_text_blocks() -> None:
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "x"},
                {"type": "text", "text": "kept"},
            ],
        },
    }
    assert _parse_stream_json(_ndjson(event)) == "kept"


def test_parse_stream_json_ignores_non_assistant_envelopes() -> None:
    payload = _ndjson(
        {"type": "system", "session_id": "abc"},
        {"type": "user", "message": {"content": [{"type": "text", "text": "X"}]}},
        _assistant_event("only this"),
        {"type": "result", "is_error": False},
    )
    assert _parse_stream_json(payload) == "only this"


def test_parse_stream_json_supports_content_block_delta_shape() -> None:
    """Forward-compat: some claude versions emit streaming text_delta blocks."""
    payload = _ndjson(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "y"}},
    )
    assert _parse_stream_json(payload) == "xy"


def test_parse_stream_json_skips_unparseable_lines() -> None:
    payload = (
        b"not json at all\n"
        + _ndjson(_assistant_event("kept"))
        + b"warning: trailing junk\n"
    )
    assert _parse_stream_json(payload) == "kept"


def test_parse_stream_json_handles_empty_stdout() -> None:
    assert _parse_stream_json(b"") == ""


def test_parse_stream_json_skips_blank_lines() -> None:
    payload = b"\n\n" + _ndjson(_assistant_event("ok")) + b"\n\n"
    assert _parse_stream_json(payload) == "ok"


def test_extract_text_from_event_unknown_type_returns_empty() -> None:
    assert _extract_text_from_event({"type": "totally_new"}) == []


def test_extract_text_from_event_missing_message_is_safe() -> None:
    assert _extract_text_from_event({"type": "assistant"}) == []


def test_extract_text_from_event_assistant_content_not_list_is_safe() -> None:
    assert (
        _extract_text_from_event(
            {"type": "assistant", "message": {"content": None}}
        )
        == []
    )


def test_extract_text_from_event_text_not_string_is_skipped() -> None:
    """A malformed text block (text: null) must not raise."""
    assert (
        _extract_text_from_event(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": None}]},
            }
        )
        == []
    )


# ---------------------------------------------------------------------------
# Default subprocess runner — covered without spawning real claude
# ---------------------------------------------------------------------------


def test_default_runner_translates_missing_binary_to_cli_not_found(
    monkeypatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no such file: claude")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CliNotFoundError) as exc:
        _default_runner(["claude", "--version"], b"")
    assert str(exc.value) == CLI_NOT_FOUND_MESSAGE


def test_default_runner_passes_stdin_and_captures_streams(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Proc:
        def __init__(self, returncode, stdout, stderr):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["input"] = kwargs.get("input")
        captured["capture_output"] = kwargs.get("capture_output")
        captured["check"] = kwargs.get("check")
        return _Proc(0, b"OUT", b"ERR")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _default_runner(["claude", "--print"], b"stdin-bytes")
    assert result.exit_code == 0
    assert result.stdout == b"OUT"
    assert result.stderr == b"ERR"
    assert captured["argv"] == ["claude", "--print"]
    assert captured["input"] == b"stdin-bytes"
    assert captured["capture_output"] is True
    assert captured["check"] is False


def test_default_runner_returns_nonzero_without_raising(monkeypatch) -> None:
    class _Proc:
        returncode = 5
        stdout = b""
        stderr = b"boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    result = _default_runner(["claude", "--print"], b"")
    assert result.exit_code == 5
    assert result.stderr == b"boom"


# ---------------------------------------------------------------------------
# _safe_unlink — exercised on its own so coverage stays > 95%
# ---------------------------------------------------------------------------


def test_safe_unlink_swallows_missing_file(tmp_path: Path) -> None:
    _safe_unlink(str(tmp_path / "does-not-exist"))  # must not raise


def test_safe_unlink_removes_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "scratch"
    target.write_text("x", encoding="utf-8")
    _safe_unlink(str(target))
    assert not target.exists()


def test_safe_unlink_warns_on_oserror(monkeypatch, capsys) -> None:
    def boom(path):
        raise PermissionError("nope")

    monkeypatch.setattr(os, "unlink", boom)
    _safe_unlink("/tmp/whatever")
    err = capsys.readouterr().err
    assert "Warning: could not remove temp file" in err
    assert "/tmp/whatever" in err


# ---------------------------------------------------------------------------
# Backend-ABC contract: stream kwarg is accepted, return shape is stable
# ---------------------------------------------------------------------------


def test_stream_kwarg_is_accepted_and_does_not_affect_payload(make_backend) -> None:
    """``stream`` is required by the ABC but currently a no-op on the CLI path."""
    backend, runner = make_backend(
        [
            _ok(_ndjson(_assistant_event("a"))),
            _ok(_ndjson(_assistant_event("a"))),
        ]
    )
    r1 = backend.complete("", [{"role": "user", "content": "x"}], stream=False)
    r2 = backend.complete("", [{"role": "user", "content": "x"}], stream=True)
    assert r1.text == r2.text == "a"
    # stdin is byte-identical between the two calls (stream is a no-op here);
    # argv differs only in the tempfile path slot, so we don't pin it.
    assert runner.calls[0]["stdin"] == runner.calls[1]["stdin"]


def test_runner_kwarg_is_optional() -> None:
    """Default runner is used when ``runner`` is omitted."""
    backend = CliBackend(model="m")
    # We won't actually call complete() (would spawn claude); just confirm
    # the backend was constructed with the module-level default.
    assert backend._runner is _default_runner  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# CliError hierarchy
# ---------------------------------------------------------------------------


def test_error_hierarchy_is_a_single_root() -> None:
    assert issubclass(CliAuthError, CliError)
    assert issubclass(CliNotFoundError, CliError)
    assert issubclass(CliInvocationError, CliError)
    assert issubclass(CliError, Exception)
