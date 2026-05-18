"""Tests for the Config dataclass (US-001 scope, SPEC §10).

US-001 only locks the dataclass shape and parity with the bundled
``defaults/config.json``. The full load-from-file / CLI-override /
corrupt-file / unknown-field tolerance pipeline lands in US-002.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.config import (
    DEFAULT_MAX_HISTORY_ENTRIES,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT_PATH,
    DEFAULT_SYSTEM_PROMPT_UPDATE_URL,
    Config,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_FILE = REPO_ROOT / "defaults" / "config.json"


SPEC_FIELDS = frozenset(
    {
        "version",
        "default_model",
        "default_iterations",
        "auto_copy",
        "show_diff",
        "system_prompt_path",
        "history_enabled",
        "max_history_entries",
        "system_prompt_update_url",
        "preferred_backend",
    }
)


def test_config_has_exactly_the_spec_fields():
    """SPEC §10 mandates these 10 fields and no others."""
    assert Config.field_names() == SPEC_FIELDS


def test_config_defaults():
    cfg = Config()
    assert cfg.version == 1
    assert cfg.default_model == DEFAULT_MODEL
    assert cfg.default_iterations == 1
    assert cfg.auto_copy is False
    assert cfg.show_diff is True
    assert cfg.system_prompt_path == DEFAULT_SYSTEM_PROMPT_PATH
    assert cfg.history_enabled is True
    assert cfg.max_history_entries == DEFAULT_MAX_HISTORY_ENTRIES
    assert cfg.system_prompt_update_url == DEFAULT_SYSTEM_PROMPT_UPDATE_URL
    assert cfg.preferred_backend == "auto"


def test_max_history_entries_default_matches_prd():
    """PRD §11 risks table: default 500."""
    assert DEFAULT_MAX_HISTORY_ENTRIES == 500


def test_defaults_config_json_exists():
    """P1-INST-04 / US-001 AC: bundled defaults file is shipped in the repo."""
    assert DEFAULTS_FILE.is_file(), f"missing: {DEFAULTS_FILE}"


def test_defaults_file_values_match_dataclass():
    """Every value in ``defaults/config.json`` equals the in-code default."""
    data = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    cfg = Config()
    for key, value in data.items():
        assert hasattr(cfg, key), f"defaults/config.json has unknown field: {key}"
        assert getattr(cfg, key) == value, (
            f"defaults/config.json[{key}] = {value!r} differs from dataclass default {getattr(cfg, key)!r}"
        )


def test_defaults_file_covers_all_dataclass_fields():
    """Every Config field is represented in ``defaults/config.json``."""
    data = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    assert Config.field_names() == frozenset(data.keys())


def test_resolved_system_prompt_path_expands_tilde():
    cfg = Config(system_prompt_path="~/.promptpal/system-prompt.md")
    resolved = cfg.resolved_system_prompt_path()
    assert "~" not in str(resolved)
    assert resolved.is_absolute()
