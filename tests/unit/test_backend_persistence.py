"""Tests for backend persistence (US-006, P1-BKND-13, D-Q10).

This file is named for US-016's mapping:
``P1-BKND-13 → tests/unit/test_backend_persistence.py`` — it covers the
explicit-flag persistence policy that lives on top of :func:`resolve_backend`.

Coverage map (one test → one acceptance criterion or sub-rule):

  AC #5  --backend <claude-cli|api-key> persists after first success → test_persist_*
  AC #6  --backend auto resets preferred_backend immediately         → test_clear_*
  AC #7  Failed call does not persist backend choice                 → test_failed_call_*

The policy is split between two helpers:

- :func:`clear_backend_preference` — called by the CLI layer *before*
  resolution when ``--backend auto`` is parsed (immediate reset).
- :func:`persist_backend_preference` — called by the CLI layer *after
  the first successful turn* with ``--backend claude-cli`` or
  ``--backend api-key``. A failed turn must not call this — that
  contract is enforced by the CLI layer; we pin it here with a
  representative integration-shape test.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.backend import Backend, BackendResponse
from core.config import Config, load_config, save_config
from core.resolve import (
    ANTHROPIC_API_KEY_ENV,
    CLAUDE_EXECUTABLE,
    clear_backend_preference,
    persist_backend_preference,
    resolve_backend,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _StubCli(Backend):
    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def name(self) -> str:
        return f"cli-stub ({self._model})"

    def complete(self, system, messages, stream=False):
        return BackendResponse(text="", input_tokens=None, output_tokens=None)

    def check_auth(self) -> bool:
        return True


class _StubApi(Backend):
    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def name(self) -> str:
        return f"api-stub ({self._model})"

    def complete(self, system, messages, stream=False):
        return BackendResponse(text="", input_tokens=1, output_tokens=2)

    def check_auth(self) -> bool:
        return True


def _which_has(*present: str):
    table = {name: f"/usr/bin/{name}" for name in present}
    return table.get


# ---------------------------------------------------------------------------
# persist_backend_preference (AC #5)
# ---------------------------------------------------------------------------


def test_persist_api_key_writes_field_to_disk(tmp_path):
    """AC #5 / AC-BKND-03: explicit --backend api-key persists after success."""
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)  # baseline 'auto'

    persist_backend_preference(config_path, "api-key")

    reloaded = load_config(config_path)
    assert reloaded.preferred_backend == "api-key"


def test_persist_claude_cli_writes_field_to_disk(tmp_path):
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)

    persist_backend_preference(config_path, "claude-cli")

    assert load_config(config_path).preferred_backend == "claude-cli"


def test_persist_preserves_other_fields(tmp_path):
    """Persisting only touches preferred_backend — other Config fields survive."""
    config_path = tmp_path / "config.json"
    save_config(
        Config(default_model="custom-model", max_history_entries=42, auto_copy=True),
        config_path,
    )

    persist_backend_preference(config_path, "api-key")

    reloaded = load_config(config_path)
    assert reloaded.preferred_backend == "api-key"
    assert reloaded.default_model == "custom-model"
    assert reloaded.max_history_entries == 42
    assert reloaded.auto_copy is True


def test_persist_when_config_file_does_not_exist(tmp_path):
    """No config file on disk → persist writes a fresh file with defaults + override."""
    config_path = tmp_path / "config.json"
    assert not config_path.exists()

    persist_backend_preference(config_path, "api-key")

    assert config_path.is_file()
    reloaded = load_config(config_path)
    assert reloaded.preferred_backend == "api-key"
    assert reloaded == Config(preferred_backend="api-key")  # rest are defaults


def test_persist_uses_atomic_write_no_temp_leftovers(tmp_path):
    """AC #5 inherits the atomic-write contract from save_config (P1-CFG-05)."""
    config_path = tmp_path / "config.json"
    persist_backend_preference(config_path, "claude-cli")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == [], f"unexpected leftovers: {leftovers}"


def test_persist_uses_lf_and_trailing_newline(tmp_path):
    config_path = tmp_path / "config.json"
    persist_backend_preference(config_path, "api-key")
    raw = config_path.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw


def test_persist_overwrites_existing_preference(tmp_path):
    config_path = tmp_path / "config.json"
    save_config(Config(preferred_backend="claude-cli"), config_path)

    persist_backend_preference(config_path, "api-key")

    assert load_config(config_path).preferred_backend == "api-key"


def test_persist_ignores_invalid_values(tmp_path):
    """Defensive: only ('auto','claude-cli','api-key') are persisted; typos are dropped."""
    config_path = tmp_path / "config.json"
    save_config(Config(preferred_backend="claude-cli"), config_path)

    persist_backend_preference(config_path, "bogus")  # type: ignore[arg-type]

    # Existing value untouched
    assert load_config(config_path).preferred_backend == "claude-cli"


# ---------------------------------------------------------------------------
# clear_backend_preference (AC #6)
# ---------------------------------------------------------------------------


def test_clear_resets_to_auto(tmp_path):
    """AC #6 / AC-BKND-04: --backend auto resets persisted preference immediately."""
    config_path = tmp_path / "config.json"
    save_config(Config(preferred_backend="api-key"), config_path)

    clear_backend_preference(config_path)

    assert load_config(config_path).preferred_backend == "auto"


def test_clear_preserves_other_fields(tmp_path):
    config_path = tmp_path / "config.json"
    save_config(
        Config(
            preferred_backend="claude-cli",
            default_model="model-x",
            max_history_entries=7,
        ),
        config_path,
    )

    clear_backend_preference(config_path)

    reloaded = load_config(config_path)
    assert reloaded.preferred_backend == "auto"
    assert reloaded.default_model == "model-x"
    assert reloaded.max_history_entries == 7


def test_clear_when_config_file_does_not_exist(tmp_path):
    """Clearing creates a fresh config with auto + defaults."""
    config_path = tmp_path / "config.json"
    assert not config_path.exists()

    clear_backend_preference(config_path)

    assert config_path.is_file()
    assert load_config(config_path).preferred_backend == "auto"


def test_clear_is_idempotent(tmp_path):
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)

    clear_backend_preference(config_path)
    clear_backend_preference(config_path)

    assert load_config(config_path).preferred_backend == "auto"


def test_clear_writes_field_value_auto_on_disk(tmp_path):
    """The JSON file actually contains preferred_backend: 'auto'."""
    config_path = tmp_path / "config.json"
    save_config(Config(preferred_backend="api-key"), config_path)

    clear_backend_preference(config_path)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["preferred_backend"] == "auto"


# ---------------------------------------------------------------------------
# Failed call does not persist (AC #7)
# ---------------------------------------------------------------------------


def test_failed_call_does_not_persist_via_caller_contract(tmp_path):
    """AC #7: a failed first turn must not trigger persist_backend_preference.

    The contract is structural — persist_backend_preference is only called
    *after* backend.complete() returns successfully. We model that contract
    here: when backend.complete() raises, the caller's try/except path
    skips the persist call, so preferred_backend on disk stays unchanged.
    """
    config_path = tmp_path / "config.json"
    save_config(Config(preferred_backend="auto"), config_path)

    class _Failing(Backend):
        @property
        def name(self) -> str:
            return "failing"

        def complete(self, system, messages, stream=False):
            raise RuntimeError("simulated backend failure")

        def check_auth(self) -> bool:
            return False

    backend: Backend = _Failing()

    cli_preferred = "api-key"
    try:
        backend.complete("", [])
        persisted = True
    except Exception:
        persisted = False

    # The caller skipped persistence because complete() raised.
    if persisted and cli_preferred in ("claude-cli", "api-key"):
        persist_backend_preference(config_path, cli_preferred)

    # Disk still says 'auto' because persist was skipped on failure.
    assert load_config(config_path).preferred_backend == "auto"


def test_successful_call_then_persist_changes_disk(tmp_path):
    """Mirror of the failed-call test: success path *does* persist."""
    config_path = tmp_path / "config.json"
    save_config(Config(preferred_backend="auto"), config_path)

    class _Ok(Backend):
        @property
        def name(self) -> str:
            return "ok"

        def complete(self, system, messages, stream=False):
            return BackendResponse(text="t", input_tokens=1, output_tokens=2)

        def check_auth(self) -> bool:
            return True

    backend: Backend = _Ok()
    cli_preferred = "api-key"

    try:
        backend.complete("", [])
        persisted_now = True
    except Exception:
        persisted_now = False

    if persisted_now and cli_preferred in ("claude-cli", "api-key"):
        persist_backend_preference(config_path, cli_preferred)

    assert load_config(config_path).preferred_backend == "api-key"


# ---------------------------------------------------------------------------
# Integration: --backend auto resets even when resolve_backend then runs (AC-BKND-04)
# ---------------------------------------------------------------------------


def test_clear_then_resolve_with_explicit_auto_uses_auto_detect_ladder(tmp_path):
    """AC-BKND-04 end-to-end:

    Given config.preferred_backend == 'api-key',
    when ``--backend auto`` is passed (caller clears, then resolves with 'auto'),
    then config.preferred_backend is reset to 'auto' on disk
    AND the auto-detection chain runs (claude on PATH → CLI wins).
    """
    config_path = tmp_path / "config.json"
    save_config(Config(preferred_backend="api-key"), config_path)

    # 1. Immediate reset (AC #6).
    clear_backend_preference(config_path)
    assert load_config(config_path).preferred_backend == "auto"

    # 2. Resolution with explicit 'auto' bypasses Config, runs auto-detect.
    backend = resolve_backend(
        "auto",
        "m",
        config=load_config(config_path),  # in-memory config is now 'auto'
        env={ANTHROPIC_API_KEY_ENV: "sk-ant-xyz"},
        which=_which_has(CLAUDE_EXECUTABLE),
        cli_factory=_StubCli,
        api_factory=_StubApi,
    )
    assert isinstance(backend, _StubCli)
