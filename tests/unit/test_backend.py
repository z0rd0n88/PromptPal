"""Tests for the Backend ABC contract + ``resolve_backend`` ladder.

US-001 locked the ABC shape; US-006 adds the resolution ladder
(P1-BKND-04..07) and the explicit-flag persistence helpers
(P1-BKND-13). This file covers the selection paths; backend
persistence has its own file (``test_backend_persistence.py``) per the
US-016 mapping.

Coverage map for the new section:

  P1-BKND-04  Explicit flag → Config.preferred_backend → auto-detect → test_resolve_*
  P1-BKND-05  preferred="claude-cli" + missing claude → CliNotFoundError → test_resolve_explicit_cli_*
  P1-BKND-06  preferred="api-key" + unset env → ApiKeyMissingError    → test_resolve_explicit_api_*
  P1-BKND-07  Both unavailable + auto → NoBackendError                → test_resolve_auto_no_backend_*
"""

from __future__ import annotations

import dataclasses

import pytest

from core.api_backend import ApiKeyMissingError
from core.backend import (
    Backend,
    BackendResponse,
    Message,
    NoBackendError,
)
from core.cli_backend import CliNotFoundError
from core.config import Config
from core.resolve import (
    ANTHROPIC_API_KEY_ENV,
    CLAUDE_EXECUTABLE,
    resolve_backend,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _StubCli(Backend):
    """Minimal ``CliBackend`` substitute used by resolve_backend tests."""

    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def name(self) -> str:
        return f"claude-cli-stub ({self._model})"

    def complete(self, system, messages, stream=False):
        return BackendResponse(text="", input_tokens=None, output_tokens=None)

    def check_auth(self) -> bool:
        return True


class _StubApi(Backend):
    """Minimal ``ApiBackend`` substitute that doesn't touch the env."""

    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def name(self) -> str:
        return f"api-key-stub ({self._model})"

    def complete(self, system, messages, stream=False):
        return BackendResponse(text="", input_tokens=1, output_tokens=2)

    def check_auth(self) -> bool:
        return True


def _which_has(*present: str):
    """Return a ``which``-like callable that finds only the named binaries."""
    table = {name: f"/usr/bin/{name}" for name in present}
    return table.get


def _which_none(_name: str) -> str | None:
    """``which`` that returns ``None`` for everything (nothing on PATH)."""
    return None


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


def test_message_type_alias_exists():
    """H6 (issue #30): ``Backend.complete``'s message-wire type uses a
    documented ``Message`` alias instead of bare ``list[dict]``.

    The alias is ``dict[str, Any]`` — type-safety boost is purely
    documentation, but a future change to ``TypedDict`` can land without
    touching every callsite.
    """
    # The alias is importable and resolves to a dict-shaped type.
    # We don't pin it to ``dict[str, Any]`` exactly because the alias may
    # later tighten to a TypedDict — but it must remain dict-compatible.
    msg: Message = {"role": "user", "content": "hi"}
    assert isinstance(msg, dict)
    assert msg["role"] == "user"


def test_no_backend_error_has_spec_message():
    """SPEC §6 mandates the exact remediation text on ``NoBackendError.MESSAGE``."""
    msg = NoBackendError.MESSAGE
    assert "No backend available" in msg
    assert "Option 1 (Claude CLI)" in msg
    assert "claude auth login" in msg
    assert "Option 2 (API key)" in msg
    assert 'ANTHROPIC_API_KEY="sk-ant-..."' in msg


# ---------------------------------------------------------------------------
# resolve_backend — selection order (P1-BKND-04)
# ---------------------------------------------------------------------------


def test_resolve_explicit_cli_when_available():
    """preferred='claude-cli' uses the CLI even if API key is also set (AC #1)."""
    backend = resolve_backend(
        "claude-cli",
        "claude-sonnet-4-6",
        config=Config(),
        env={ANTHROPIC_API_KEY_ENV: "sk-ant-xyz"},
        which=_which_has(CLAUDE_EXECUTABLE),
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubCli)


def test_resolve_explicit_api_when_available():
    """preferred='api-key' uses the API even if claude is on PATH (AC #1)."""
    backend = resolve_backend(
        "api-key",
        "claude-sonnet-4-6",
        config=Config(),
        env={ANTHROPIC_API_KEY_ENV: "sk-ant-xyz"},
        which=_which_has(CLAUDE_EXECUTABLE),
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubApi)


def test_resolve_no_flag_falls_through_to_config_preference():
    """preferred=None and Config.preferred_backend='api-key' → ApiBackend."""
    backend = resolve_backend(
        None,
        "m",
        config=Config(preferred_backend="api-key"),
        env={ANTHROPIC_API_KEY_ENV: "sk-ant-xyz"},
        which=_which_has(CLAUDE_EXECUTABLE),  # claude is on PATH but ignored
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubApi)


def test_resolve_no_flag_with_config_cli_uses_cli_even_if_missing_path_would_error():
    """When config says claude-cli and claude IS on PATH → CliBackend."""
    backend = resolve_backend(
        None,
        "m",
        config=Config(preferred_backend="claude-cli"),
        env={},
        which=_which_has(CLAUDE_EXECUTABLE),
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubCli)


def test_resolve_explicit_auto_bypasses_config_preference():
    """preferred='auto' is the user's explicit choice — bypass Config (AC #6 spirit).

    Config.preferred_backend='api-key' would otherwise win, but '--backend auto'
    means the user wants auto-detect right now. (The caller has already cleared
    persistence via ``clear_backend_preference``.)
    """
    backend = resolve_backend(
        "auto",
        "m",
        config=Config(preferred_backend="api-key"),
        env={},  # no API key
        which=_which_has(CLAUDE_EXECUTABLE),
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubCli)


# ---------------------------------------------------------------------------
# resolve_backend — auto-detect ladder (P1-BKND-04)
# ---------------------------------------------------------------------------


def test_resolve_auto_prefers_cli_when_claude_on_path():
    """CLI precedes API in the auto-detect ladder (AC #1, AC-BKND-02)."""
    backend = resolve_backend(
        None,
        "m",
        config=Config(),  # preferred_backend='auto'
        env={ANTHROPIC_API_KEY_ENV: "sk-ant-xyz"},
        which=_which_has(CLAUDE_EXECUTABLE),
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubCli)


def test_resolve_auto_uses_api_when_only_env_key_set():
    backend = resolve_backend(
        None,
        "m",
        config=Config(),
        env={ANTHROPIC_API_KEY_ENV: "sk-ant-xyz"},
        which=_which_none,
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubApi)


# ---------------------------------------------------------------------------
# resolve_backend — error paths (P1-BKND-05..07)
# ---------------------------------------------------------------------------


def test_resolve_explicit_cli_missing_raises_cli_not_found():
    """P1-BKND-05: preferred='claude-cli' + claude missing → CliNotFoundError. No silent fallback to API."""
    with pytest.raises(CliNotFoundError) as excinfo:
        resolve_backend(
            "claude-cli",
            "m",
            config=Config(),
            env={ANTHROPIC_API_KEY_ENV: "sk-ant-xyz"},  # API key present, must NOT fall back
            which=_which_none,
            cli_factory=_StubCli,
            api_factory=_StubApi,
        )
    assert "claude CLI not found on PATH" in str(excinfo.value)


def test_resolve_explicit_api_missing_key_raises_api_key_missing():
    """P1-BKND-06: preferred='api-key' + unset key → ApiKeyMissingError. No silent fallback to CLI."""
    with pytest.raises(ApiKeyMissingError) as excinfo:
        resolve_backend(
            "api-key",
            "m",
            config=Config(),
            env={},  # no API key
            which=_which_has(CLAUDE_EXECUTABLE),  # claude present, must NOT fall back
            cli_factory=_StubCli,
            api_factory=_StubApi,
        )
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_resolve_explicit_api_empty_key_raises():
    """An empty ``ANTHROPIC_API_KEY=""`` counts as unset (matches ApiBackend behavior)."""
    with pytest.raises(ApiKeyMissingError):
        resolve_backend(
            "api-key",
            "m",
            config=Config(),
            env={ANTHROPIC_API_KEY_ENV: ""},
            which=_which_none,
            cli_factory=_StubCli,
            api_factory=_StubApi,
        )


def test_resolve_auto_no_backend_raises_no_backend_error():
    """P1-BKND-07 / AC-BKND-05: both unavailable → NoBackendError."""
    with pytest.raises(NoBackendError) as excinfo:
        resolve_backend(
            None,
            "m",
            config=Config(),
            env={},
            which=_which_none,
            cli_factory=_StubCli,
            api_factory=_StubApi,
        )
    msg = str(excinfo.value)
    assert "Option 1 (Claude CLI):" in msg
    assert "Option 2 (API key):" in msg


def test_resolve_explicit_auto_no_backend_raises_no_backend_error():
    """Explicit '--backend auto' with nothing detected also raises NoBackendError."""
    with pytest.raises(NoBackendError):
        resolve_backend(
            "auto",
            "m",
            config=Config(preferred_backend="claude-cli"),  # ignored: explicit auto
            env={},
            which=_which_none,
            cli_factory=_StubCli,
            api_factory=_StubApi,
        )


def test_resolve_unknown_preferred_value_collapses_to_auto():
    """Defensive: a typo from the caller collapses to auto-detect, not a crash."""
    backend = resolve_backend(
        "claude_cli_typo",  # unknown value
        "m",
        config=Config(),
        env={ANTHROPIC_API_KEY_ENV: "sk-ant-xyz"},
        which=_which_none,
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubApi)


def test_resolve_model_is_forwarded_to_factory():
    """The model argument flows through to whichever factory ends up called."""
    seen: list[str] = []

    def _factory(m: str) -> Backend:
        seen.append(m)
        return _StubCli(m)

    resolve_backend(
        "claude-cli",
        "claude-opus-4-7",
        config=Config(),
        env={},
        which=_which_has(CLAUDE_EXECUTABLE),
        cli_factory=_factory,
        api_factory=_StubApi,
    )
    assert seen == ["claude-opus-4-7"]


def test_resolve_defaults_use_os_environ_and_shutil_which(monkeypatch):
    """Smoke test: when env/which are not injected, real os.environ + shutil.which are used."""
    # Set up: pretend nothing is available so we get NoBackendError, but exercise the defaults code path.
    monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-promptpal-test-path")
    with pytest.raises(NoBackendError):
        resolve_backend(
            None,
            "m",
            config=Config(),
            cli_factory=_StubCli,
            api_factory=_StubApi,
        )
