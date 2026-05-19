"""Anthropic Messages API backend (SPEC §6, P1-BKND-02, P1-BKND-08, P1-BKND-11).

This module implements :class:`ApiBackend`, the HTTP backend that talks to
``POST /v1/messages`` on ``api.anthropic.com`` using the
``anthropic-version: 2023-06-01`` header.

Public surface
--------------

- :class:`ApiBackend` — concrete :class:`Backend` for Anthropic's HTTP API.
- :class:`ApiError` / :class:`ApiAuthError` / :class:`ApiRateLimitError` /
  :class:`ApiServerError` / :class:`ApiNetworkError` /
  :class:`ApiKeyMissingError` — error hierarchy; ``ApiKeyMissingError``
  is raised by the constructor when ``ANTHROPIC_API_KEY`` is unset and
  carries :data:`NO_KEY_MESSAGE` (P1-ERR-01).
- Constants :data:`DEFAULT_API_BASE_URL`, :data:`ANTHROPIC_VERSION`,
  :data:`MESSAGES_PATH`, :data:`NO_KEY_MESSAGE`,
  :data:`AUTH_ERROR_MESSAGE`, :data:`RATE_LIMITED_MESSAGE_TEMPLATE`,
  :data:`SERVER_ERROR_MESSAGE`, :data:`NETWORK_ERROR_MESSAGE`.

Retry budget (P1-BKND-08)
-------------------------

Independent counters per category:

- ``429``: up to 3 retries; sleep honors ``Retry-After`` (seconds);
  default 1s when the header is missing or malformed.
- ``5xx``: up to 3 retries; sleeps 1s / 2s / 4s.
- network failure: 1 retry after 2s.
- ``401``: no retry — raises :class:`ApiAuthError` immediately
  (P1-ERR-02).

API key handling (P1-BKND-11 / NFR-05)
--------------------------------------

The key is read **only** from the ``ANTHROPIC_API_KEY`` environment
variable in :meth:`ApiBackend.__init__`. It is held in a private
attribute, never echoed to stdout/stderr, never persisted, and never
included in retry/error messages.

Testability
-----------

The HTTP layer is injected via the ``transport`` constructor kwarg —
tests pass a fake callable that returns canned :class:`_HttpResponse`
objects (or raises :class:`_TransportNetworkError`) without touching
the network. The retry sleep is injected via ``sleeper``. Both have
sensible production defaults.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Callable

from core.backend import Backend, BackendResponse


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_API_BASE_URL: str = "https://api.anthropic.com"
ANTHROPIC_VERSION: str = "2023-06-01"
MESSAGES_PATH: str = "/v1/messages"

DEFAULT_MAX_TOKENS: int = 4096
CHECK_AUTH_MAX_TOKENS: int = 1

MAX_RATE_LIMIT_RETRIES: int = 3
MAX_SERVER_ERROR_RETRIES: int = 3
MAX_NETWORK_RETRIES: int = 1

NETWORK_RETRY_DELAY_SECONDS: float = 2.0
DEFAULT_RATE_LIMIT_DELAY_SECONDS: float = 1.0
SERVER_ERROR_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

NO_KEY_MESSAGE: str = (
    "Error: ANTHROPIC_API_KEY is not set.\n"
    'Set it with: export ANTHROPIC_API_KEY="sk-ant-..."'
)
AUTH_ERROR_MESSAGE: str = "API key rejected. Check ANTHROPIC_API_KEY."
RATE_LIMITED_MESSAGE_TEMPLATE: str = "Rate limited. Retrying in {seconds}s..."
SERVER_ERROR_MESSAGE: str = "Anthropic API error. Retrying..."
NETWORK_ERROR_MESSAGE: str = "Network error. Retrying..."


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """Base class for :class:`ApiBackend` failures."""


class ApiKeyMissingError(ApiError):
    """Raised when ``ANTHROPIC_API_KEY`` is unset (P1-ERR-01)."""

    MESSAGE = NO_KEY_MESSAGE


class ApiAuthError(ApiError):
    """401 from the Messages API; no retry (P1-ERR-02)."""


class ApiRateLimitError(ApiError):
    """429 retry budget exhausted (P1-ERR-03)."""


class ApiServerError(ApiError):
    """5xx retry budget exhausted (P1-ERR-05)."""


class ApiNetworkError(ApiError):
    """Connection-level failure after retry exhausted (P1-ERR-04)."""


# ---------------------------------------------------------------------------
# Internal transport types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes = b""
    stream_iter: Iterable[dict] | None = None


class _TransportNetworkError(Exception):
    """Raised by a transport to signal a connection-level (non-HTTP) failure.

    Treated as the trigger for the network-retry branch in
    :meth:`ApiBackend._send_with_retries`.
    """


Transport = Callable[[str, dict[str, str], bytes, bool], _HttpResponse]
"""Signature of the injectable HTTP transport: (url, headers, body, stream) -> response.

Must raise :class:`_TransportNetworkError` for connection failures.
HTTP error status codes are returned via ``_HttpResponse.status``, not
raised, so the retry loop can inspect them uniformly.
"""


# ---------------------------------------------------------------------------
# Default transport (urllib-based)
# ---------------------------------------------------------------------------


def _iter_sse(resp: Any) -> Iterator[dict]:
    """Yield parsed SSE events from a urllib HTTPResponse-like file object.

    Events are JSON objects on ``data:`` lines; comments (``:``), empty
    lines, and the ``[DONE]`` sentinel are silently skipped.
    """
    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].lstrip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _default_transport(
    url: str, headers: dict[str, str], body: bytes, stream: bool
) -> _HttpResponse:
    """Send a single POST via :mod:`urllib`. Surfaces HTTP status as data, network as exception."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req)  # noqa: S310 — fixed scheme, known host
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()
        except Exception:
            err_body = b""
        return _HttpResponse(
            status=e.code, headers={k: v for k, v in e.headers.items()}, body=err_body
        )
    except urllib.error.URLError as e:
        raise _TransportNetworkError(str(e.reason)) from e
    if stream:
        return _HttpResponse(
            status=resp.status,
            headers={k: v for k, v in resp.headers.items()},
            stream_iter=_iter_sse(resp),
        )
    return _HttpResponse(
        status=resp.status,
        headers={k: v for k, v in resp.headers.items()},
        body=resp.read(),
    )


# ---------------------------------------------------------------------------
# ApiBackend
# ---------------------------------------------------------------------------


@dataclass
class _StreamAccumulator:
    """Mutable state used while folding an SSE stream into a BackendResponse."""

    text_parts: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


def _parse_retry_after(headers: dict[str, str], default: float) -> float:
    """Return the Retry-After delay in seconds, or ``default`` if missing/malformed.

    Only the integer-seconds form is honored; HTTP-date Retry-After is
    treated as missing per simplicity (this matches what Anthropic's
    docs show in practice).
    """
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return default
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, seconds)


class ApiBackend(Backend):
    """Anthropic Messages API backend (P1-BKND-02).

    Reads ``ANTHROPIC_API_KEY`` at construction; raises
    :class:`ApiKeyMissingError` when unset. The key is held privately
    and never echoed (P1-BKND-11 / NFR-05).
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str = DEFAULT_API_BASE_URL,
        sleeper: Callable[[float], None] = time.sleep,
        transport: Transport | None = None,
    ) -> None:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ApiKeyMissingError(NO_KEY_MESSAGE)
        self._model = model
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._sleeper = sleeper
        self._transport: Transport = transport or _default_transport

    @property
    def name(self) -> str:
        return f"api-key ({self._model})"

    # -- public API ----------------------------------------------------------

    def complete(
        self,
        system: str,
        messages: list[dict],
        stream: bool = False,
    ) -> BackendResponse:
        """Send a single completion turn.

        Streaming is enabled only when both ``stream=True`` and
        ``sys.stdout.isatty()``; otherwise a single non-stream call is
        issued. Either path returns the full assistant text plus numeric
        input/output token counts (P1-BKND-10).
        """
        do_stream = bool(stream) and sys.stdout.isatty()
        payload = self._build_payload(
            system=system,
            messages=messages,
            max_tokens=DEFAULT_MAX_TOKENS,
            stream=do_stream,
        )
        response = self._send_with_retries(payload, stream=do_stream)
        if do_stream:
            return self._consume_stream(response)
        return self._parse_response_body(response.body)

    def check_auth(self) -> bool:
        """Lightweight ``max_tokens=1`` round-trip used by ``--status`` (P1-BKND-12)."""
        payload = self._build_payload(
            system="",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=CHECK_AUTH_MAX_TOKENS,
            stream=False,
        )
        try:
            response = self._send_with_retries(payload, stream=False)
        except ApiError:
            return False
        return 200 <= response.status < 300

    # -- internals -----------------------------------------------------------

    def _build_payload(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": list(messages),
        }
        if system:
            payload["system"] = system
        if stream:
            payload["stream"] = True
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "x-api-key": self._api_key,
        }

    def _send_with_retries(
        self, payload: dict[str, Any], *, stream: bool
    ) -> _HttpResponse:
        url = f"{self._base_url}{MESSAGES_PATH}"
        body = json.dumps(payload).encode("utf-8")
        headers = self._headers()
        rate_retries = 0
        server_retries = 0
        network_retries = 0
        while True:
            try:
                response = self._transport(url, headers, body, stream)
            except _TransportNetworkError as e:
                if network_retries >= MAX_NETWORK_RETRIES:
                    raise ApiNetworkError(NETWORK_ERROR_MESSAGE) from e
                network_retries += 1
                print(NETWORK_ERROR_MESSAGE, file=sys.stderr)
                self._sleeper(NETWORK_RETRY_DELAY_SECONDS)
                continue
            status = response.status
            if 200 <= status < 300:
                return response
            if status == 401:
                raise ApiAuthError(AUTH_ERROR_MESSAGE)
            if status == 429:
                if rate_retries >= MAX_RATE_LIMIT_RETRIES:
                    raise ApiRateLimitError(
                        f"Rate limited after {MAX_RATE_LIMIT_RETRIES} retries"
                    )
                delay = _parse_retry_after(
                    response.headers, default=DEFAULT_RATE_LIMIT_DELAY_SECONDS
                )
                print(
                    RATE_LIMITED_MESSAGE_TEMPLATE.format(seconds=int(delay)),
                    file=sys.stderr,
                )
                rate_retries += 1
                self._sleeper(delay)
                continue
            if 500 <= status < 600:
                if server_retries >= MAX_SERVER_ERROR_RETRIES:
                    raise ApiServerError(
                        f"Anthropic API error {status} after "
                        f"{MAX_SERVER_ERROR_RETRIES} retries"
                    )
                delay = SERVER_ERROR_BACKOFF_SECONDS[server_retries]
                print(SERVER_ERROR_MESSAGE, file=sys.stderr)
                server_retries += 1
                self._sleeper(delay)
                continue
            raise ApiError(
                f"HTTP {status}: "
                f"{response.body.decode('utf-8', errors='replace')[:500]}"
            )

    def _parse_response_body(self, body: bytes) -> BackendResponse:
        data = json.loads(body.decode("utf-8"))
        text_parts: list[str] = []
        for block in data.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        usage = data.get("usage", {}) or {}
        return BackendResponse(
            text="".join(text_parts),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

    def _consume_stream(self, response: _HttpResponse) -> BackendResponse:
        acc = _StreamAccumulator()
        events = response.stream_iter or ()
        for event in events:
            etype = event.get("type")
            if etype == "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
                acc.input_tokens = usage.get("input_tokens", acc.input_tokens)
                acc.output_tokens = usage.get("output_tokens", acc.output_tokens)
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        acc.text_parts.append(text)
                        sys.stdout.write(text)
                        sys.stdout.flush()
            elif etype == "message_delta":
                usage = event.get("usage") or {}
                if "input_tokens" in usage:
                    acc.input_tokens = usage["input_tokens"]
                if "output_tokens" in usage:
                    acc.output_tokens = usage["output_tokens"]
        return BackendResponse(
            text="".join(acc.text_parts),
            input_tokens=acc.input_tokens,
            output_tokens=acc.output_tokens,
        )
