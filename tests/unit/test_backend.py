"""Tests for the Backend ABC contract (US-001 scope, SPEC §6).

US-001 only locks the shape of the ABC. Concrete backend behavior
(``resolve_backend`` ladder P1-BKND-01..09, stream-json pipe, retry/auth
handling) lands in US-006+.
"""

from __future__ import annotations

import dataclasses

import pytest

from core.backend import (
    Backend,
    BackendResponse,
    NoBackendError,
)


def test_backend_is_abstract():
    """Instantiating Backend directly must fail — it's an ABC."""
    with pytest.raises(TypeError):
        Backend()  # type: ignore[abstract]


def test_subclass_missing_methods_cannot_instantiate():
    """A subclass that omits ``complete`` or ``check_auth`` is still abstract."""

    class Half(Backend):
        @property
        def name(self) -> str:
            return "half"

        def check_auth(self) -> bool:
            return True

    with pytest.raises(TypeError):
        Half()  # type: ignore[abstract]


def test_minimal_concrete_backend_satisfies_contract():
    """A subclass that implements all members is instantiable and returns BackendResponse."""

    class Stub(Backend):
        @property
        def name(self) -> str:
            return "api-key (claude-sonnet-4-6)"

        def complete(
            self,
            system: str,
            messages: list[dict],
            stream: bool = False,
        ) -> BackendResponse:
            return BackendResponse(text="ok", input_tokens=1, output_tokens=2)

        def check_auth(self) -> bool:
            return True

    backend = Stub()
    result = backend.complete("sys", [{"role": "user", "content": "hi"}])
    assert result.text == "ok"
    assert result.input_tokens == 1
    assert result.output_tokens == 2
    assert backend.check_auth() is True
    assert backend.name == "api-key (claude-sonnet-4-6)"


def test_stream_kw_defaults_to_false_per_spec():
    """SPEC §6: ``stream: bool = False`` — callers can omit the keyword."""

    class Stub(Backend):
        @property
        def name(self) -> str:
            return "stub"

        def complete(self, system, messages, stream=False):
            return BackendResponse(text=f"stream={stream}", input_tokens=None, output_tokens=None)

        def check_auth(self) -> bool:
            return True

    assert Stub().complete("s", []).text == "stream=False"


def test_backend_response_is_frozen():
    """BackendResponse is frozen so backends cannot mutate a recorded turn."""
    result = BackendResponse(text="x", input_tokens=None, output_tokens=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.text = "y"  # type: ignore[misc]


def test_cli_backend_records_null_tokens():
    """P1-BKND-10: CLI turns record ``None`` for both token counts."""
    result = BackendResponse(text="x", input_tokens=None, output_tokens=None)
    assert result.input_tokens is None
    assert result.output_tokens is None


def test_no_backend_error_is_a_plain_exception():
    """SPEC §6: ``NoBackendError(Exception)`` — not RuntimeError, not a custom base."""
    assert issubclass(NoBackendError, Exception)
    assert NoBackendError.__bases__ == (Exception,)


def test_no_backend_error_has_spec_message():
    """SPEC §6 mandates the exact remediation text on ``NoBackendError.MESSAGE``."""
    msg = NoBackendError.MESSAGE
    assert "No backend available" in msg
    assert "Option 1 (Claude CLI)" in msg
    assert "claude auth login" in msg
    assert "Option 2 (API key)" in msg
    assert 'ANTHROPIC_API_KEY="sk-ant-..."' in msg
