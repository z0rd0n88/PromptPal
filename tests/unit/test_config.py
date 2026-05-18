"""Tests for the Config dataclass and loader (SPEC §10).

US-001: dataclass shape + parity with bundled ``defaults/config.json``.
US-002: load/merge/atomic-save plumbing (this file extends US-001 tests).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import (
    DEFAULT_MAX_HISTORY_ENTRIES,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT_PATH,
    DEFAULT_SYSTEM_PROMPT_UPDATE_URL,
    Config,
    ConfigCorruptError,
    apply_overrides,
    load_config,
    normalize_preferred_backend,
    save_config,
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


# ---------------------------------------------------------------------------
# US-001 — dataclass shape and defaults-file parity
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# US-002 — normalize_preferred_backend (AC #5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["auto", "claude-cli", "api-key"])
def test_normalize_preferred_backend_passes_valid(value):
    assert normalize_preferred_backend(value, warn=False) == value


def test_normalize_preferred_backend_invalid_falls_back_to_auto(capsys):
    """AC #5: invalid values fall back to 'auto' with a stderr warning."""
    assert normalize_preferred_backend("nope") == "auto"
    err = capsys.readouterr().err
    assert "preferred_backend" in err
    assert "nope" in err
    assert "auto" in err


def test_normalize_preferred_backend_warn_false_silences_stderr(capsys):
    assert normalize_preferred_backend("nope", warn=False) == "auto"
    assert capsys.readouterr().err == ""


def test_normalize_preferred_backend_non_string_falls_back(capsys):
    """Numeric / bool / None all clamp to 'auto'."""
    for value in (42, None, True, ["api-key"]):
        assert normalize_preferred_backend(value, warn=False) == "auto"


# ---------------------------------------------------------------------------
# US-002 — apply_overrides (AC #1, #2, #3)
# ---------------------------------------------------------------------------


def test_apply_overrides_empty_dict_returns_equivalent_config():
    base = Config()
    merged = apply_overrides(base, {}, warn=False)
    assert merged == base


def test_apply_overrides_returns_new_instance():
    """Immutability: callers should not see the base mutated."""
    base = Config()
    merged = apply_overrides(base, {"default_model": "x"}, warn=False)
    assert merged is not base
    assert base.default_model == DEFAULT_MODEL  # base untouched
    assert merged.default_model == "x"


def test_apply_overrides_unknown_field_silently_ignored():
    """AC #2: unknown fields in config.json are silently ignored."""
    base = Config()
    merged = apply_overrides(base, {"unknown_field": "value"}, warn=False)
    assert not hasattr(merged, "unknown_field")
    assert merged == base


def test_apply_overrides_applies_valid_string():
    merged = apply_overrides(Config(), {"default_model": "claude-opus-4-7"}, warn=False)
    assert merged.default_model == "claude-opus-4-7"


def test_apply_overrides_applies_valid_int():
    merged = apply_overrides(Config(), {"max_history_entries": 42}, warn=False)
    assert merged.max_history_entries == 42


def test_apply_overrides_applies_valid_bool():
    merged = apply_overrides(Config(), {"auto_copy": True, "show_diff": False}, warn=False)
    assert merged.auto_copy is True
    assert merged.show_diff is False


def test_apply_overrides_type_mismatch_falls_back_with_warning(capsys):
    """AC #3: type-mismatched fields fall back to default + stderr warning."""
    base = Config()
    merged = apply_overrides(base, {"max_history_entries": "not-an-int"})
    assert merged.max_history_entries == DEFAULT_MAX_HISTORY_ENTRIES
    err = capsys.readouterr().err
    assert "max_history_entries" in err
    assert "int" in err.lower()


def test_apply_overrides_bool_is_strict_not_int(capsys):
    """A bool override on an int field is a type mismatch (and vice versa)."""
    merged = apply_overrides(Config(), {"max_history_entries": True})
    assert merged.max_history_entries == DEFAULT_MAX_HISTORY_ENTRIES
    assert "max_history_entries" in capsys.readouterr().err

    merged = apply_overrides(Config(), {"auto_copy": 1})
    assert merged.auto_copy is False
    assert "auto_copy" in capsys.readouterr().err


def test_apply_overrides_warn_false_silences_stderr(capsys):
    apply_overrides(Config(), {"max_history_entries": "bad"}, warn=False)
    assert capsys.readouterr().err == ""


def test_apply_overrides_preferred_backend_invalid_clamps_to_auto(capsys):
    """AC #5 within apply_overrides: invalid preferred_backend clamps to 'auto' with warn."""
    base = Config(preferred_backend="api-key")
    merged = apply_overrides(base, {"preferred_backend": "bogus"})
    assert merged.preferred_backend == "auto"
    assert "preferred_backend" in capsys.readouterr().err


def test_apply_overrides_preferred_backend_valid_passes_through():
    base = Config()
    merged = apply_overrides(base, {"preferred_backend": "api-key"}, warn=False)
    assert merged.preferred_backend == "api-key"


def test_apply_overrides_partial_keeps_rest_of_base():
    base = Config(default_model="x", auto_copy=True)
    merged = apply_overrides(base, {"default_model": "y"}, warn=False)
    assert merged.default_model == "y"
    assert merged.auto_copy is True  # untouched override


# ---------------------------------------------------------------------------
# US-002 — load_config (AC #1, #2, #3, #4)
# ---------------------------------------------------------------------------


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "absent.json")
    assert cfg == Config()


def test_load_config_valid_file_applies_overrides(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"default_model": "claude-opus-4-7", "auto_copy": True}),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.default_model == "claude-opus-4-7"
    assert cfg.auto_copy is True
    # Untouched fields keep defaults
    assert cfg.max_history_entries == DEFAULT_MAX_HISTORY_ENTRIES


def test_load_config_corrupt_json_raises(tmp_path):
    """AC #4: corrupt JSON raises ConfigCorruptError with canonical message."""
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigCorruptError) as excinfo:
        load_config(path)
    msg = str(excinfo.value)
    assert "Config file corrupt at" in msg
    assert str(path) in msg
    assert "Delete it to reset." in msg


def test_load_config_non_object_json_raises(tmp_path):
    """A list or scalar at root is not a valid config object."""
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigCorruptError):
        load_config(path)


def test_load_config_unknown_field_silently_ignored(tmp_path):
    """AC #2 via load_config: extra keys do not break loading."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"default_model": "x", "future_field": "ignored"}),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.default_model == "x"
    assert not hasattr(cfg, "future_field")


def test_load_config_type_mismatch_warns_and_uses_default(tmp_path, capsys):
    """AC #3 via load_config: type mismatch warns and uses dataclass default."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"max_history_entries": "five hundred"}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.max_history_entries == DEFAULT_MAX_HISTORY_ENTRIES
    assert "max_history_entries" in capsys.readouterr().err


def test_load_config_accepts_string_path(tmp_path):
    """Caller convenience: str paths work alongside Path."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"default_model": "y"}), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.default_model == "y"


# ---------------------------------------------------------------------------
# US-002 — save_config (AC #6)
# ---------------------------------------------------------------------------


def test_save_then_load_roundtrip(tmp_path):
    original = Config(
        default_model="claude-opus-4-7",
        max_history_entries=7,
        auto_copy=True,
        preferred_backend="api-key",
    )
    path = tmp_path / "config.json"
    save_config(original, path)
    reloaded = load_config(path)
    assert reloaded == original


def test_save_config_uses_atomic_write_no_temp_leftovers(tmp_path):
    """AC #6: tempfile -> os.rename pattern; no leftover .config-* files."""
    save_config(Config(), tmp_path / "config.json")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == [], f"unexpected leftovers: {leftovers}"


def test_save_config_ends_with_newline_and_uses_lf(tmp_path):
    """Persistence files use UTF-8 + LF + trailing newline."""
    path = tmp_path / "config.json"
    save_config(Config(), path)
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw


def test_save_config_writes_all_fields(tmp_path):
    """The on-disk file should be a complete snapshot of the dataclass."""
    path = tmp_path / "config.json"
    save_config(Config(), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert frozenset(data.keys()) == SPEC_FIELDS


def test_save_config_overwrites_existing_file(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(default_model="first"), path)
    save_config(Config(default_model="second"), path)
    assert json.loads(path.read_text(encoding="utf-8"))["default_model"] == "second"


def test_save_config_accepts_string_path(tmp_path):
    """save_config mirrors load_config in accepting str or Path."""
    save_config(Config(), str(tmp_path / "config.json"))
    assert (tmp_path / "config.json").is_file()


# ---------------------------------------------------------------------------
# US-002 — ConfigCorruptError
# ---------------------------------------------------------------------------


def test_config_corrupt_error_is_an_exception():
    """Keep the inheritance shallow — callers should `except ConfigCorruptError` directly."""
    assert issubclass(ConfigCorruptError, Exception)


def test_config_corrupt_error_message_format(tmp_path):
    """Message must follow AC #4 wording so the CLI can surface it verbatim."""
    path = tmp_path / "config.json"
    path.write_text("garbage", encoding="utf-8")
    with pytest.raises(ConfigCorruptError) as excinfo:
        load_config(path)
    assert str(excinfo.value) == f"Config file corrupt at {path}. Delete it to reset."
