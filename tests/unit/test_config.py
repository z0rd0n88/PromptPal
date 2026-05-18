"""Tests for core/config.py (US-001 smoke + US-002 will extend)."""

from __future__ import annotations

import json

import pytest

from core.config import (
    Config,
    ConfigCorruptError,
    apply_overrides,
    load_config,
    normalize_preferred_backend,
    save_config,
)


def test_defaults():
    c = Config()
    assert c.default_model == "claude-sonnet-4-6"
    assert c.preferred_backend == "auto"
    assert c.max_history_entries == 500
    assert c.auto_copy is False
    assert c.default_output_format == "plain"
    assert c.api_version == "2023-06-01"


def test_normalize_preferred_backend_passthrough():
    for v in ("auto", "claude-cli", "api-key"):
        assert normalize_preferred_backend(v, warn=False) == v


def test_normalize_preferred_backend_invalid(capsys):
    assert normalize_preferred_backend("nope") == "auto"
    err = capsys.readouterr().err
    assert "invalid preferred_backend" in err
    assert "auto" in err


def test_apply_overrides_unknown_field_ignored():
    base = Config()
    merged = apply_overrides(base, {"unknown_field": 42}, warn=False)
    assert not hasattr(merged, "unknown_field")
    assert merged.default_model == base.default_model


def test_apply_overrides_type_mismatch_falls_back(capsys):
    base = Config()
    merged = apply_overrides(base, {"max_history_entries": "not-an-int"})
    assert merged.max_history_entries == 500
    assert "expected int" in capsys.readouterr().err


def test_apply_overrides_bool_strict(capsys):
    base = Config()
    merged = apply_overrides(base, {"auto_copy": "true"})
    assert merged.auto_copy is False
    assert "expected bool" in capsys.readouterr().err


def test_apply_overrides_real_values():
    base = Config()
    merged = apply_overrides(
        base,
        {"default_model": "claude-opus-4-7", "max_history_entries": 10, "auto_copy": True},
        warn=False,
    )
    assert merged.default_model == "claude-opus-4-7"
    assert merged.max_history_entries == 10
    assert merged.auto_copy is True


def test_apply_overrides_clamps_preferred_backend():
    base = Config()
    merged = apply_overrides(base, {"preferred_backend": "bogus"}, warn=False)
    assert merged.preferred_backend == "auto"


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "absent.json")
    assert cfg.default_model == Config().default_model


def test_load_config_corrupt_raises(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(ConfigCorruptError):
        load_config(p)


def test_load_config_non_object_raises(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigCorruptError):
        load_config(p)


def test_save_then_load_roundtrip(tmp_path):
    cfg = Config(default_model="m", max_history_entries=7, auto_copy=True)
    path = tmp_path / "config.json"
    save_config(cfg, path)
    assert path.read_bytes().endswith(b"\n")
    assert b"\r\n" not in path.read_bytes()
    loaded = load_config(path)
    assert loaded.default_model == "m"
    assert loaded.max_history_entries == 7
    assert loaded.auto_copy is True


def test_save_uses_atomic_write(tmp_path, monkeypatch):
    cfg = Config()
    path = tmp_path / "config.json"
    save_config(cfg, path)
    # No temp files left behind.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".config-")]
    assert leftovers == []


def test_defaults_config_json_keys_match_dataclass():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    raw = json.loads((repo_root / "defaults" / "config.json").read_text(encoding="utf-8"))
    field_names = set(Config().to_dict().keys())
    assert set(raw.keys()) == field_names
