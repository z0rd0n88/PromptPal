"""Config dataclass and load/save plumbing.

Fields here are the authoritative runtime configuration surface. The
merge order is: dataclass defaults -> ~/.promptpal/config.json overrides
-> CLI flag overrides (handled by core/cli.py).

See PRD §5.7 (P1-CFG-*) and SPEC §4/§10.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

VALID_PREFERRED_BACKENDS = ("auto", "claude-cli", "api-key")
VALID_OUTPUT_FORMATS = ("plain", "json", "markdown")


def _default_promptpal_dir() -> str:
    return str(Path.home() / ".promptpal")


def _default_system_prompt_path() -> str:
    return str(Path.home() / ".promptpal" / "system-prompt.md")


def _default_history_dir() -> str:
    return str(Path.home() / ".promptpal" / "history")


def _default_usage_log_path() -> str:
    return str(Path.home() / ".promptpal" / "usage.log")


def _default_config_path() -> str:
    return str(Path.home() / ".promptpal" / "config.json")


@dataclass
class Config:
    """Runtime configuration.

    All fields have defaults so a Config() with no arguments is valid for
    smoke tests and first-run flows.
    """

    default_model: str = "claude-sonnet-4-6"
    preferred_backend: str = "auto"
    system_prompt_path: str = field(default_factory=_default_system_prompt_path)
    system_prompt_update_url: str = ""
    history_dir: str = field(default_factory=_default_history_dir)
    usage_log_path: str = field(default_factory=_default_usage_log_path)
    config_path: str = field(default_factory=_default_config_path)
    promptpal_dir: str = field(default_factory=_default_promptpal_dir)
    max_history_entries: int = 500
    auto_copy: bool = False
    default_iterations: int = 0
    default_output_format: str = "plain"
    api_base_url: str = "https://api.anthropic.com"
    api_version: str = "2023-06-01"
    request_timeout_seconds: int = 60
    spinner_enabled: bool = True
    stream_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELD_TYPES: dict[str, type] = {}


def _field_types() -> dict[str, type]:
    if not _FIELD_TYPES:
        for f in fields(Config):
            _FIELD_TYPES[f.name] = f.type if isinstance(f.type, type) else type(getattr(Config(), f.name))
    return _FIELD_TYPES


def normalize_preferred_backend(value: str, warn: bool = True) -> str:
    """Coerce an arbitrary preferred_backend value into the allowed set.

    Unknown values fall back to ``"auto"`` with a stderr warning so that a
    typoed config.json or stale persisted value cannot crash startup.
    """

    if value in VALID_PREFERRED_BACKENDS:
        return value
    if warn:
        import sys

        print(
            f"Warning: invalid preferred_backend {value!r}; "
            f"falling back to 'auto'.",
            file=sys.stderr,
        )
    return "auto"


def apply_overrides(base: Config, overrides: dict[str, Any], *, warn: bool = True) -> Config:
    """Return a new Config with `overrides` merged on top of `base`.

    - Unknown keys are silently ignored (forward-compatible per P1-CFG-03).
    - Type-mismatched values fall back to the field default with a stderr
      warning.
    - ``preferred_backend`` is additionally clamped to the valid set.
    """

    import sys

    data = base.to_dict()
    types = _field_types()
    defaults = Config().to_dict()

    for key, value in overrides.items():
        if key not in data:
            continue
        expected = types.get(key)
        if expected is bool:
            if not isinstance(value, bool):
                if warn:
                    print(
                        f"Warning: config field {key!r} expected bool, "
                        f"got {type(value).__name__}; using default.",
                        file=sys.stderr,
                    )
                value = defaults[key]
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int):
                if warn:
                    print(
                        f"Warning: config field {key!r} expected int, "
                        f"got {type(value).__name__}; using default.",
                        file=sys.stderr,
                    )
                value = defaults[key]
        elif expected is str:
            if not isinstance(value, str):
                if warn:
                    print(
                        f"Warning: config field {key!r} expected str, "
                        f"got {type(value).__name__}; using default.",
                        file=sys.stderr,
                    )
                value = defaults[key]
        data[key] = value

    data["preferred_backend"] = normalize_preferred_backend(
        data["preferred_backend"], warn=warn
    )
    return Config(**data)


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    warn: bool = True,
) -> Config:
    """Load Config from disk, applying defaults for missing fields.

    A missing file is not an error — defaults are returned. A corrupt file
    (JSON parse error) raises ``ConfigCorruptError`` so the caller can map
    it to the user-facing exit-1 message (P1-CFG-04).
    """

    import json

    base = Config()
    path = Path(config_path) if config_path is not None else Path(base.config_path)
    if not path.exists():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigCorruptError(str(path)) from exc
    if not isinstance(raw, dict):
        raise ConfigCorruptError(str(path))
    return apply_overrides(base, raw, warn=warn)


def save_config(config: Config, config_path: str | os.PathLike[str] | None = None) -> None:
    """Atomically write Config to disk via tempfile.mkstemp -> os.rename."""

    import json
    import tempfile

    path = Path(config_path) if config_path is not None else Path(config.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(config.to_dict(), f, indent=2, sort_keys=True)
            f.write("\n")
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ConfigCorruptError(Exception):
    """Raised when config.json fails to parse."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"Config file corrupt at {path}. Delete it to reset."
        )
        self.path = path
