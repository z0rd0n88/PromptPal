"""Smoke tests for the Backend ABC (US-001).

Story-specific resolve_backend tests land in US-006.
"""

from __future__ import annotations

import pytest

from core.backend import (
    AuthError,
    AuthStatus,
    Backend,
    BackendError,
    CompletionResult,
    NoBackendError,
)


def test_backend_is_abstract():
    with pytest.raises(TypeError):
        Backend()  # type: ignore[abstract]


def test_completion_result_defaults():
    r = CompletionResult(text="hi")
    assert r.text == "hi"
    assert r.input_tokens is None
    assert r.output_tokens is None


def test_auth_status_default_detail():
    s = AuthStatus(ok=True)
    assert s.ok is True
    assert s.detail == ""


def test_error_hierarchy():
    assert issubclass(AuthError, BackendError)
    assert issubclass(NoBackendError, BackendError)


def test_subclass_must_implement_all_abstract_methods():
    class Half(Backend):
        name = "half"

        def complete(self, system, messages, stream=False):
            return CompletionResult(text="x")

    with pytest.raises(TypeError):
        Half()  # check_auth not implemented

    class Full(Backend):
        name = "full"

        def complete(self, system, messages, stream=False):
            return CompletionResult(text="x")

        def check_auth(self):
            return AuthStatus(ok=True)

    instance = Full()
    assert instance.name == "full"
    assert instance.complete("sys", []).text == "x"
    assert instance.check_auth().ok is True
