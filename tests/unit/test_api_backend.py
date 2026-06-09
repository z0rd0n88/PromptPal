"""Tests for core/api_backend.py (US-004, SPEC §6, P1-BKND-02/08/10/11/12).

Coverage map (1 test → 1 acceptance criterion or sub-rule):

  AC #1  POST /v1/messages with anthropic-version: 2023-06-01     → test_request_*
  AC #2  Key read only from env; never on disk/log/stdout         → test_key_*
  AC #3  Retry: 401 immediate, 429 Retry-After, 5xx 1/2/4, net    → test_retry_*
  AC #4  complete() full text; streams on TTY when stream=True    → test_complete_*, test_stream_*
  AC #5  check_auth() uses max_tokens=1                           → test_check_auth_*
  AC #6  Each API turn records numeric input_tokens/output_tokens → test_tokens_*
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

import core.api_backend as api_mod
from core.api_backend import (
    ANTHROPIC_VERSION,
    AUTH_ERROR_MESSAGE,
    CHECK_AUTH_MAX_TOKENS,
    DEFAULT_API_BASE_URL,
    DEFAULT_MAX_TOKENS,
    MESSAGES_PATH,
    NETWORK_ERROR_MESSAGE,
    NO_KEY_MESSAGE,
    RATE_LIMITED_MESSAGE_TEMPLATE,
    SERVER_ERROR_MESSAGE,
    ApiAuthError,
    ApiBackend,
    ApiError,
    ApiKeyMissingError,
    ApiNetworkError,
    ApiRateLimitError,
    ApiServerError,
    _default_transport,
    _HttpResponse,
    _iter_sse,
    _parse_retry_after,
    _TransportNetworkError,
)
from core.backend import BackendResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_payload(text: str = "improved", in_t: int = 11, out_t: int = 22) -> bytes:
    return json.dumps(
        {
            "id": "msg_test",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": in_t, "output_tokens": out_t},
        }
    ).encode("utf-8")


def _http(status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> _HttpResponse:
    return _HttpResponse(status=status, headers=headers or {}, body=body)


class _FakeTransport:
    """Programmable transport that yields canned responses in order.

    Each item is either an :class:`_HttpResponse` (returned) or an
    :class:`Exception` instance (raised).
    """

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, headers, body, stream):
        parsed_body = json.loads(body.decode("utf-8")) if body else None
        self.calls.append(
            {"url": url, "headers": dict(headers), "body": parsed_body, "stream": stream}
        )
        if not self._items:
            raise AssertionError("FakeTransport exhausted")
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-XXXXXXXX")
    return "sk-ant-test-XXXXXXXX"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_backend(api_key: str, sleeps: list[float]):
    """Return a factory that builds ApiBackends bound to ``transport`` + recorded sleeps."""

    def _make(items: list[Any], *, model: str = "claude-sonnet-4-6") -> tuple[ApiBackend, _FakeTransport]:
        transport = _FakeTransport(items)
        backend = ApiBackend(
            model=model,
            sleeper=lambda s: sleeps.append(s),
            transport=transport,
        )
        return backend, transport

    return _make


# ---------------------------------------------------------------------------
# AC #2 — key from env only
# ---------------------------------------------------------------------------


def test_key_missing_raises_with_canonical_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ApiKeyMissingError) as exc:
        ApiBackend(model="claude-sonnet-4-6")
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert "export ANTHROPIC_API_KEY=" in str(exc.value)
    assert ApiKeyMissingError.MESSAGE == NO_KEY_MESSAGE


def test_key_empty_string_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ApiKeyMissingError):
        ApiBackend(model="claude-sonnet-4-6")


def test_key_sent_as_x_api_key_header(make_backend) -> None:
    backend, transport = make_backend([_http(200, _ok_payload())])
    backend.complete("sys", [{"role": "user", "content": "hi"}])
    headers = transport.calls[0]["headers"]
    assert headers["x-api-key"] == "sk-ant-test-XXXXXXXX"
    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert headers["Content-Type"] == "application/json"


def test_key_never_appears_in_repr_or_str(api_key: str) -> None:
    backend = ApiBackend(model="claude-sonnet-4-6")
    assert api_key not in repr(backend)
    assert api_key not in str(backend)


def test_key_never_leaks_to_stderr_on_auth_error(
    make_backend, capsys: pytest.CaptureFixture[str], api_key: str
) -> None:
    backend, _ = make_backend([_http(401, b'{"type":"error","error":{"message":"invalid"}}')])
    with pytest.raises(ApiAuthError):
        backend.complete("", [{"role": "user", "content": "hi"}])
    captured = capsys.readouterr()
    assert api_key not in captured.out
    assert api_key not in captured.err


# ---------------------------------------------------------------------------
# AC #1 — request shape
# ---------------------------------------------------------------------------


def test_request_url_is_v1_messages(make_backend) -> None:
    backend, transport = make_backend([_http(200, _ok_payload())])
    backend.complete("", [{"role": "user", "content": "hi"}])
    assert transport.calls[0]["url"] == f"{DEFAULT_API_BASE_URL}{MESSAGES_PATH}"


def test_request_body_includes_model_and_messages(make_backend) -> None:
    backend, transport = make_backend([_http(200, _ok_payload())])
    msgs = [{"role": "user", "content": "hi"}]
    backend.complete("sys-prompt", msgs)
    body = transport.calls[0]["body"]
    assert body["model"] == "claude-sonnet-4-6"
    assert body["messages"] == msgs
    assert body["system"] == "sys-prompt"
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS
    assert "stream" not in body  # non-stream path omits it


def test_request_omits_system_when_empty(make_backend) -> None:
    backend, transport = make_backend([_http(200, _ok_payload())])
    backend.complete("", [{"role": "user", "content": "hi"}])
    assert "system" not in transport.calls[0]["body"]


def test_request_messages_list_is_defensive_copy(make_backend) -> None:
    backend, transport = make_backend([_http(200, _ok_payload())])
    msgs = [{"role": "user", "content": "hi"}]
    backend.complete("", msgs)
    msgs.append({"role": "user", "content": "mutated"})
    assert transport.calls[0]["body"]["messages"] == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# AC #4 — complete() returns full text
# ---------------------------------------------------------------------------


def test_complete_returns_assembled_text(make_backend) -> None:
    backend, _ = make_backend([_http(200, _ok_payload(text="hello world"))])
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert isinstance(result, BackendResponse)
    assert result.text == "hello world"


def test_complete_concatenates_multiple_text_blocks(make_backend) -> None:
    body = json.dumps(
        {
            "content": [
                {"type": "text", "text": "part-1 "},
                {"type": "text", "text": "part-2"},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    ).encode("utf-8")
    backend, _ = make_backend([_http(200, body)])
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert result.text == "part-1 part-2"


def test_complete_ignores_non_text_blocks(make_backend) -> None:
    body = json.dumps(
        {
            "content": [
                {"type": "tool_use", "name": "x", "input": {}},
                {"type": "text", "text": "kept"},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    ).encode("utf-8")
    backend, _ = make_backend([_http(200, body)])
    assert backend.complete("", []).text == "kept"


# ---------------------------------------------------------------------------
# AC #6 — numeric token counts
# ---------------------------------------------------------------------------


def test_tokens_recorded_numeric_for_api_turn(make_backend) -> None:
    backend, _ = make_backend([_http(200, _ok_payload(in_t=42, out_t=99))])
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert result.input_tokens == 42
    assert result.output_tokens == 99
    assert isinstance(result.input_tokens, int)
    assert isinstance(result.output_tokens, int)


# ---------------------------------------------------------------------------
# AC #3 — retry behavior
# ---------------------------------------------------------------------------


def test_retry_401_fails_immediately_no_retry(make_backend, sleeps: list[float]) -> None:
    backend, transport = make_backend([_http(401, b'{"error":"x"}')])
    with pytest.raises(ApiAuthError) as exc:
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert str(exc.value) == AUTH_ERROR_MESSAGE
    assert len(transport.calls) == 1
    assert sleeps == []


def test_retry_429_honors_retry_after(
    make_backend, sleeps: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    backend, transport = make_backend(
        [
            _http(429, b"", headers={"Retry-After": "7"}),
            _http(200, _ok_payload()),
        ]
    )
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert result.text == "improved"
    assert sleeps == [7.0]
    assert len(transport.calls) == 2
    err = capsys.readouterr().err
    assert RATE_LIMITED_MESSAGE_TEMPLATE.format(seconds=7) in err


def test_retry_429_missing_header_defaults_to_one_second(
    make_backend, sleeps: list[float]
) -> None:
    backend, _ = make_backend(
        [_http(429, b"", headers={}), _http(200, _ok_payload())]
    )
    backend.complete("", [{"role": "user", "content": "hi"}])
    assert sleeps == [1.0]


def test_retry_429_caps_at_3_retries(make_backend, sleeps: list[float]) -> None:
    # 4 consecutive 429s — first call + 3 retries = 4 attempts, then raise
    backend, transport = make_backend(
        [_http(429, b"", headers={"Retry-After": "1"}) for _ in range(4)]
    )
    with pytest.raises(ApiRateLimitError):
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert len(transport.calls) == 4
    assert sleeps == [1.0, 1.0, 1.0]


def test_retry_5xx_uses_exponential_backoff_1_2_4(
    make_backend, sleeps: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    backend, transport = make_backend(
        [
            _http(500, b"oops"),
            _http(502, b"oops"),
            _http(503, b"oops"),
            _http(200, _ok_payload()),
        ]
    )
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert result.text == "improved"
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(transport.calls) == 4
    assert SERVER_ERROR_MESSAGE in capsys.readouterr().err


def test_retry_5xx_caps_at_3_retries(make_backend, sleeps: list[float]) -> None:
    backend, transport = make_backend([_http(500, b"oops") for _ in range(4)])
    with pytest.raises(ApiServerError):
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert len(transport.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_network_retries_once_after_2s(
    make_backend, sleeps: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    backend, transport = make_backend(
        [
            _TransportNetworkError("connection refused"),
            _http(200, _ok_payload()),
        ]
    )
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert result.text == "improved"
    assert sleeps == [2.0]
    assert len(transport.calls) == 2
    assert NETWORK_ERROR_MESSAGE in capsys.readouterr().err


def test_retry_network_caps_at_one_retry(make_backend, sleeps: list[float]) -> None:
    backend, transport = make_backend(
        [
            _TransportNetworkError("connection refused"),
            _TransportNetworkError("connection refused"),
        ]
    )
    with pytest.raises(ApiNetworkError):
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert sleeps == [2.0]
    assert len(transport.calls) == 2


def test_retry_categories_have_independent_counters(
    make_backend, sleeps: list[float]
) -> None:
    """A 429 burst should not exhaust the 5xx budget (and vice versa)."""
    backend, _ = make_backend(
        [
            _http(429, b"", headers={"Retry-After": "1"}),
            _http(429, b"", headers={"Retry-After": "1"}),
            _http(429, b"", headers={"Retry-After": "1"}),
            _http(500, b"oops"),
            _http(500, b"oops"),
            _http(500, b"oops"),
            _http(200, _ok_payload()),
        ]
    )
    result = backend.complete("", [{"role": "user", "content": "hi"}])
    assert result.text == "improved"
    assert sleeps == [1.0, 1.0, 1.0, 1.0, 2.0, 4.0]


def test_retry_unhandled_4xx_raises_apierror_without_retry(
    make_backend, sleeps: list[float]
) -> None:
    backend, transport = make_backend([_http(400, b'{"error":"bad request"}')])
    with pytest.raises(ApiError) as exc:
        backend.complete("", [{"role": "user", "content": "hi"}])
    assert "400" in str(exc.value)
    assert sleeps == []
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# AC #5 — check_auth uses max_tokens=1
# ---------------------------------------------------------------------------


def test_check_auth_uses_max_tokens_1(make_backend) -> None:
    backend, transport = make_backend([_http(200, _ok_payload())])
    assert backend.check_auth() is True
    body = transport.calls[0]["body"]
    assert body["max_tokens"] == CHECK_AUTH_MAX_TOKENS == 1
    assert body["messages"]  # must have at least one message per API contract


def test_check_auth_returns_false_on_401(make_backend) -> None:
    backend, _ = make_backend([_http(401, b'{"error":"x"}')])
    assert backend.check_auth() is False


def test_check_auth_returns_false_on_network_failure(make_backend) -> None:
    backend, _ = make_backend(
        [_TransportNetworkError("x"), _TransportNetworkError("x")]
    )
    assert backend.check_auth() is False


# ---------------------------------------------------------------------------
# AC #4 — streaming
# ---------------------------------------------------------------------------


def _stream_events() -> list[dict]:
    return [
        {
            "type": "message_start",
            "message": {"id": "msg_x", "usage": {"input_tokens": 13, "output_tokens": 1}},
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 47}},
        {"type": "message_stop"},
    ]


def test_stream_accumulates_text_and_tokens(
    make_backend, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """P1-FIX-28-01 / P1-PIPE-09: stream=True assembles text+tokens, but
    no longer writes deltas to stdout (pipe-safety reserves stdout for
    the final improved prompt only).
    """
    backend, transport = make_backend(
        [_HttpResponse(status=200, headers={}, stream_iter=iter(_stream_events()))]
    )
    result = backend.complete("", [{"role": "user", "content": "hi"}], stream=True)
    assert result.text == "Hello world"
    assert result.input_tokens == 13
    assert result.output_tokens == 47
    assert transport.calls[0]["stream"] is True
    assert transport.calls[0]["body"]["stream"] is True
    # P1-FIX-28-01: streamed deltas MUST NOT reach stdout
    captured = capsys.readouterr()
    assert captured.out == ""


def test_stream_honored_when_stdout_not_a_tty(
    make_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-FIX-28-02: stream=True is honored regardless of TTY state.

    Replaces the previous TTY-downgrade behavior — since the stream path
    no longer emits user-visible output, the isatty() gate is moot and
    ``stream`` now controls only the transport (SSE vs JSON).
    """
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    backend, transport = make_backend(
        [_HttpResponse(status=200, headers={}, stream_iter=iter(_stream_events()))]
    )
    result = backend.complete("", [{"role": "user", "content": "hi"}], stream=True)
    assert result.text == "Hello world"
    assert transport.calls[0]["stream"] is True
    assert transport.calls[0]["body"]["stream"] is True


def test_stream_disabled_when_stream_false(make_backend) -> None:
    backend, transport = make_backend([_http(200, _ok_payload())])
    backend.complete("", [{"role": "user", "content": "hi"}], stream=False)
    assert transport.calls[0]["stream"] is False


# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------


def test_iter_sse_skips_comments_and_blanks() -> None:
    raw = b": heartbeat\n\ndata: {\"type\":\"ping\"}\n\n: another\n\n"
    events = list(_iter_sse(io.BytesIO(raw)))
    assert events == [{"type": "ping"}]


def test_iter_sse_skips_non_data_event_lines() -> None:
    """Lines like ``event: message_start`` are framing hints we don't consume."""
    raw = b"event: message_start\ndata: {\"type\":\"x\"}\n\n"
    events = list(_iter_sse(io.BytesIO(raw)))
    assert events == [{"type": "x"}]


def test_iter_sse_handles_done_sentinel() -> None:
    raw = b'data: {"type":"a"}\n\ndata: [DONE]\n\ndata: {"type":"b"}\n\n'
    events = list(_iter_sse(io.BytesIO(raw)))
    assert events == [{"type": "a"}]


def test_iter_sse_ignores_malformed_json_lines() -> None:
    raw = b"data: not-json\n\ndata: {\"ok\":true}\n\n"
    events = list(_iter_sse(io.BytesIO(raw)))
    assert events == [{"ok": True}]


# ---------------------------------------------------------------------------
# H5 — _iter_sse closes underlying response (issue #30)
# ---------------------------------------------------------------------------


class _ClosableBytes(io.BytesIO):
    """``BytesIO`` that records whether ``close()`` was invoked.

    The real urllib ``HTTPResponse`` exposes ``close()`` that releases the
    underlying socket; a leaked response keeps the socket open until GC.
    """

    def __init__(self, raw: bytes) -> None:
        super().__init__(raw)
        self.close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


def test_iter_sse_closes_response_on_normal_iteration() -> None:
    """H5: when the consumer drains all events normally, the underlying
    response must be closed so the HTTP socket is released."""
    raw = b'data: {"type":"a"}\n\ndata: [DONE]\n\n'
    resp = _ClosableBytes(raw)
    events = list(_iter_sse(resp))
    assert events == [{"type": "a"}]
    assert resp.close_calls == 1, "resp.close() must run after normal iteration"


def test_iter_sse_closes_response_when_consumer_stops_early() -> None:
    """H5: when the consumer exits mid-stream (the realistic leak scenario
    — e.g. an exception during ``_consume_stream``), the generator's
    ``finally`` block must still run ``resp.close()``."""
    raw = b'data: {"type":"a"}\n\ndata: {"type":"b"}\n\ndata: {"type":"c"}\n\n'
    resp = _ClosableBytes(raw)
    gen = _iter_sse(resp)
    next(gen)  # consume one event
    gen.close()  # consumer abandons the stream
    assert resp.close_calls == 1, (
        "resp.close() must run when the consumer stops iterating early"
    )


def test_iter_sse_tolerates_response_without_close() -> None:
    """H5: a response-like object without ``close()`` (some test fakes,
    older stdlib objects) must not cause the generator to crash."""

    class _NoCloseBytes(io.BytesIO):
        close = None  # type: ignore[assignment]

    resp = _NoCloseBytes(b'data: {"type":"a"}\n\n')
    # Must not raise even though ``close`` is None.
    events = list(_iter_sse(resp))
    assert events == [{"type": "a"}]


# ---------------------------------------------------------------------------
# M15 — _iter_sse UTF-8 strictness (issue #30)
# ---------------------------------------------------------------------------


def test_iter_sse_skips_lines_with_invalid_utf8() -> None:
    """M15: a line with invalid UTF-8 used to be silently mangled by
    ``errors="replace"`` — replacing the bad bytes with U+FFFD and yielding
    a corrupted JSON. Strict decode + skip surfaces the corruption cleanly:
    the bad line drops, the rest of the stream parses normally."""
    # 0xff is never valid in UTF-8 in any position.
    raw = b'data: {"bad":"\xff\xff"}\n\ndata: {"good":true}\n\n'
    events = list(_iter_sse(io.BytesIO(raw)))
    assert events == [{"good": True}], (
        "invalid-UTF-8 line must be skipped, not yielded with replacement chars"
    )


def test_iter_sse_preserves_valid_multibyte_utf8() -> None:
    """M15: valid multi-byte UTF-8 inside a line must round-trip cleanly.
    No regression in handling legitimate non-ASCII content."""
    # ``data: {"emoji":"🎯"}`` with the rocket inside as actual UTF-8 bytes.
    raw = 'data: {"emoji":"🎯"}\n\n'.encode("utf-8")
    events = list(_iter_sse(io.BytesIO(raw)))
    assert events == [{"emoji": "🎯"}]


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------


def test_parse_retry_after_reads_integer_seconds() -> None:
    assert _parse_retry_after({"Retry-After": "13"}, default=1.0) == 13.0


def test_parse_retry_after_case_insensitive() -> None:
    assert _parse_retry_after({"retry-after": "5"}, default=1.0) == 5.0


def test_parse_retry_after_default_on_missing() -> None:
    assert _parse_retry_after({}, default=1.0) == 1.0


def test_parse_retry_after_default_on_garbage() -> None:
    assert _parse_retry_after({"Retry-After": "soon"}, default=1.0) == 1.0


def test_parse_retry_after_clamps_negative_to_zero() -> None:
    assert _parse_retry_after({"Retry-After": "-3"}, default=1.0) == 0.0


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_name_includes_model(api_key: str) -> None:
    backend = ApiBackend(model="claude-haiku-4-5")
    assert backend.name == "api-key (claude-haiku-4-5)"


# ---------------------------------------------------------------------------
# Default transport (urllib wrapper) — round-trip via monkeypatched urlopen
# ---------------------------------------------------------------------------


class _FakeUrlopenResponse:
    """Minimal stand-in for ``http.client.HTTPResponse`` used by urllib."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))


def test_default_transport_returns_status_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeUrlopenResponse(200, {"Content-Type": "application/json"}, b'{"ok":true}')
    monkeypatch.setattr(api_mod.urllib.request, "urlopen", lambda req: fake)
    resp = _default_transport("https://api.example.com/v1/messages", {}, b"{}", stream=False)
    assert resp.status == 200
    assert resp.body == b'{"ok":true}'


def test_default_transport_returns_status_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import email.message
    import urllib.error

    headers_obj = email.message.Message()
    headers_obj["Retry-After"] = "5"
    err = urllib.error.HTTPError(
        url="https://api.example.com/v1/messages",
        code=429,
        msg="Too Many Requests",
        hdrs=headers_obj,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error":"slow down"}'),
    )

    def raise_err(_req):
        raise err

    monkeypatch.setattr(api_mod.urllib.request, "urlopen", raise_err)
    resp = _default_transport("https://api.example.com/v1/messages", {}, b"{}", stream=False)
    assert resp.status == 429
    assert resp.body == b'{"error":"slow down"}'
    assert resp.headers["Retry-After"] == "5"


def test_default_transport_http_error_with_unreadable_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import email.message
    import urllib.error

    class _BrokenFp:
        def read(self):
            raise OSError("stream closed")

        def close(self) -> None:
            return None

    err = urllib.error.HTTPError(
        url="https://api.example.com/v1/messages",
        code=500,
        msg="Server Error",
        hdrs=email.message.Message(),  # type: ignore[arg-type]
        fp=_BrokenFp(),  # type: ignore[arg-type]
    )

    def raise_err(_req):
        raise err

    monkeypatch.setattr(api_mod.urllib.request, "urlopen", raise_err)
    resp = _default_transport("https://api.example.com/v1/messages", {}, b"{}", stream=False)
    assert resp.status == 500
    assert resp.body == b""


def test_default_transport_url_error_raises_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    def raise_err(_req):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(api_mod.urllib.request, "urlopen", raise_err)
    with pytest.raises(_TransportNetworkError):
        _default_transport("https://api.example.com/v1/messages", {}, b"{}", stream=False)


def test_default_transport_stream_branch_returns_event_iter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeUrlopenResponse(
        200,
        {"Content-Type": "text/event-stream"},
        b'data: {"type":"ping"}\n\n',
    )
    monkeypatch.setattr(api_mod.urllib.request, "urlopen", lambda req: fake)
    resp = _default_transport(
        "https://api.example.com/v1/messages", {}, b"{}", stream=True
    )
    assert resp.status == 200
    assert resp.stream_iter is not None
    assert list(resp.stream_iter) == [{"type": "ping"}]


def test_base_url_trailing_slash_stripped(make_backend, sleeps: list[float]) -> None:
    transport = _FakeTransport([_http(200, _ok_payload())])
    backend = ApiBackend(
        model="m",
        base_url="https://api.example.com/",
        sleeper=lambda s: sleeps.append(s),
        transport=transport,
    )
    backend.complete("", [{"role": "user", "content": "hi"}])
    assert transport.calls[0]["url"] == f"https://api.example.com{MESSAGES_PATH}"


# ---------------------------------------------------------------------------
# Issue #28 follow-up fixes (P1-FIX-28-NN)
# ---------------------------------------------------------------------------


def test_stream_does_not_write_to_stdout_when_piped(
    make_backend, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """P1-FIX-28-01 / P1-PIPE-09: with piped stdout, streaming must not
    leak deltas to stdout (reserved for the final improved prompt).
    """
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    backend, _transport = make_backend(
        [_HttpResponse(status=200, headers={}, stream_iter=iter(_stream_events()))]
    )
    result = backend.complete("", [{"role": "user", "content": "hi"}], stream=True)
    assert result.text == "Hello world"
    captured = capsys.readouterr()
    assert captured.out == ""


def test_parse_response_body_null_content_returns_empty(make_backend) -> None:
    """P1-FIX-28-03: ``{"content": null}`` must not crash (mirrors the
    defensive ``or []`` pattern in cli_backend._extract_text_from_event).
    """
    backend, _transport = make_backend([])
    body = json.dumps({"content": None, "usage": {"input_tokens": 1, "output_tokens": 2}}).encode("utf-8")
    result = backend._parse_response_body(body)
    assert isinstance(result, BackendResponse)
    assert result.text == ""
    assert result.input_tokens == 1
    assert result.output_tokens == 2


def test_parse_response_body_null_usage_returns_none_tokens(make_backend) -> None:
    """P1-FIX-28-03: ``{"usage": null}`` already handled — pin it."""
    backend, _transport = make_backend([])
    body = json.dumps({"content": [{"type": "text", "text": "x"}], "usage": None}).encode("utf-8")
    result = backend._parse_response_body(body)
    assert result.text == "x"
    assert result.input_tokens is None
    assert result.output_tokens is None


def test_server_error_backoff_length_invariant() -> None:
    """P1-FIX-28-04: backoff tuple must cover all server-error retries.

    The retry loop indexes ``SERVER_ERROR_BACKOFF_SECONDS[server_retries]``
    up to ``server_retries == MAX_SERVER_ERROR_RETRIES - 1``. The
    module-level assert catches a mismatch at import time.
    """
    assert len(api_mod.SERVER_ERROR_BACKOFF_SECONDS) >= api_mod.MAX_SERVER_ERROR_RETRIES
