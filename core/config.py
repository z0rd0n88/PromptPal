"""Runtime configuration for PromptPal.

Authoritative schema: SPEC.md §10 (Configuration). The ten fields below
mirror the user-editable JSON persisted at ``~/.promptpal/config.json``.

Merge order (PRD P1-CFG-02):

    dataclass defaults  →  config.json overrides  →  CLI flag overrides

Both override layers are applied via :func:`apply_overrides`, which:

- silently drops unknown fields (P1-CFG-03 / US-002 AC #2),
- warns on stderr and keeps the base value when a field's runtime type
  doesn't match the dataclass declaration (US-002 AC #3), and
- clamps ``preferred_backend`` through :func:`normalize_preferred_backend`
  so an invalid value cannot survive past this layer (US-002 AC #5).

:func:`load_config` reads JSON from a path and pipes the result through
:func:`apply_overrides`. Corrupt JSON or a non-object root raises
:class:`ConfigCorruptError` carrying the canonical "Config file corrupt
at <path>. Delete it to reset." message the CLI surfaces verbatim
(US-002 AC #4).

:func:`save_config` writes atomically via ``tempfile.mkstemp`` +
``os.replace`` so a crash mid-write can never leave a half-written file
behind (US-002 AC #6).

Field provenance:

  - ``version``                 — SPEC §4 (schema version)
  - ``default_model``           — SPEC §10, PRD §5.4 (``--model``)
  - ``default_iterations``      — SPEC §10
  - ``auto_copy``               — P1-LOOP-04
  - ``show_diff``               — SPEC §10
  - ``system_prompt_path``      — P1-PIPE-04, P1-SP-05
  - ``history_enabled``         — SPEC §10
  - ``max_history_entries``     — P1-HIST-06 (§11 risks: default 500)
  - ``system_prompt_update_url``— P1-SP-03, AC-SP-01
  - ``preferred_backend``       — P1-CFG-06, D-Q10

The API key is intentionally NOT a Config field. PRD P1-BKND-11 requires
it to be read only from the ``ANTHROPIC_API_KEY`` env var, never from
``config.json``, never logged, never written to history.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal

from core._io import atomic_write_bytes


PreferredBackend = Literal["auto", "claude-cli", "api-key"]

VALID_PREFERRED_BACKENDS: tuple[str, ...] = ("auto", "claude-cli", "api-key")

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_SYSTEM_PROMPT_PATH = "~/.promptpal/system-prompt.md"
DEFAULT_SYSTEM_PROMPT_UPDATE_URL = (
    "https://raw.githubusercontent.com/z0rd0n88/PromptPal/main/core/system_prompt.txt"
)
DEFAULT_MAX_HISTORY_ENTRIES = 500


@dataclass
class Config:
    version: int = 1
    default_model: str = DEFAULT_MODEL
    default_iterations: int = 1
    auto_copy: bool = False
    show_diff: bool = True
    system_prompt_path: str = DEFAULT_SYSTEM_PROMPT_PATH
    history_enabled: bool = True
    max_history_entries: int = DEFAULT_MAX_HISTORY_ENTRIES
    system_prompt_update_url: str = DEFAULT_SYSTEM_PROMPT_UPDATE_URL
    preferred_backend: PreferredBackend = "auto"

    @classmethod
    def field_names(cls) -> frozenset[str]:
        """Names of all declared fields — used for unknown-field tolerance (P1-CFG-03)."""
        return frozenset(f.name for f in fields(cls))

    def resolved_system_prompt_path(self) -> Path:
        """Return ``system_prompt_path`` with ``~`` expanded to an absolute path."""
        return Path(self.system_prompt_path).expanduser()


class ConfigCorruptError(Exception):
    """Raised when the on-disk config.json cannot be parsed.

    Carries the canonical message "Config file corrupt at <path>. Delete
    it to reset." that the CLI surfaces verbatim per US-002 AC #4.
    """


# ---------------------------------------------------------------------------
# preferred_backend validation (US-002 AC #5)
# ---------------------------------------------------------------------------


def normalize_preferred_backend(value: Any, *, warn: bool = True) -> PreferredBackend:
    """Return a valid ``PreferredBackend``, clamping invalid inputs to ``"auto"``.

    Per US-002 AC #5: any value not in ``("auto", "claude-cli", "api-key")``
    falls back to ``"auto"`` with a stderr warning. ``warn=False``
    suppresses the warning (used by tests and by re-merge paths that have
    already warned once).
    """
    if isinstance(value, str) and value in VALID_PREFERRED_BACKENDS:
        return value  # type: ignore[return-value]
    if warn:
        print(
            f"warning: invalid preferred_backend {value!r}; falling back to 'auto'",
            file=sys.stderr,
        )
    return "auto"


# ---------------------------------------------------------------------------
# Overrides (US-002 AC #1, #2, #3, #5)
# ---------------------------------------------------------------------------


def _expected_type(field_name: str) -> type | None:
    """Return the runtime type the dataclass expects for ``field_name``.

    Derived from the default values on ``Config()`` so this helper stays
    in lockstep with the dataclass without duplicating type annotations.
    """
    if field_name not in Config.field_names():
        return None
    return type(getattr(Config(), field_name))


def _value_matches_field_type(field_name: str, value: Any) -> bool:
    """Strict type check that distinguishes ``bool`` from ``int``.

    Python's ``isinstance(True, int)`` is True; for config fields where
    the dataclass declares ``int`` we want bools rejected, and vice versa.
    Other primitives use ordinary ``isinstance``.
    """
    expected = _expected_type(field_name)
    if expected is None:
        return False
    if expected is bool:
        return type(value) is bool
    if expected is int:
        return type(value) is int
    return isinstance(value, expected)


def apply_overrides(
    base: Config, overrides: dict[str, Any], *, warn: bool = True
) -> Config:
    """Return a new ``Config`` with ``overrides`` applied on top of ``base``.

    - Unknown fields in ``overrides`` are silently dropped (AC #2).
    - Fields whose override value doesn't match the dataclass type are
      ignored with a stderr warning; ``base``'s value flows through (AC #3).
    - ``preferred_backend`` is clamped through
      :func:`normalize_preferred_backend` so invalid strings cannot
      survive (AC #5).
    - ``base`` is not mutated; the new ``Config`` is produced via
      :func:`dataclasses.replace`.
    """
    known = Config.field_names()
    accepted: dict[str, Any] = {}
    for name, value in overrides.items():
        if name not in known:
            continue  # AC #2: unknown fields silently ignored
        if name == "preferred_backend":
            accepted[name] = normalize_preferred_backend(value, warn=warn)
            continue
        if not _value_matches_field_type(name, value):
            if warn:
                expected = _expected_type(name)
                expected_name = expected.__name__ if expected is not None else "?"
                got = type(value).__name__
                print(
                    f"warning: invalid type for {name}: expected {expected_name}, "
                    f"got {got} ({value!r}); ignoring override",
                    file=sys.stderr,
                )
            continue  # AC #3
        accepted[name] = value
    if not accepted:
        return base
    return replace(base, **accepted)


# ---------------------------------------------------------------------------
# Load (US-002 AC #1, #2, #3, #4)
# ---------------------------------------------------------------------------


def load_config(path: str | Path) -> Config:
    """Load ``Config`` from JSON at ``path``, applying overrides on defaults.

    - Missing file → ``Config()`` (no error).
    - Valid JSON object → :func:`apply_overrides` applied to ``Config()``.
    - Corrupt JSON or non-object root → :class:`ConfigCorruptError`.

    Reads the file with a single ``read_text`` call and catches
    ``FileNotFoundError`` directly — the older ``exists()``-then-``read_text``
    shape carried a TOCTOU window where a concurrent ``--clear-config``
    or external ``rm`` between the two calls would surface as an
    uncaught ``OSError`` instead of the documented "missing → defaults"
    behavior.
    """
    path_obj = Path(path)
    try:
        raw = path_obj.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigCorruptError(
            f"Config file corrupt at {path_obj}. Delete it to reset."
        ) from e
    if not isinstance(data, dict):
        raise ConfigCorruptError(
            f"Config file corrupt at {path_obj}. Delete it to reset."
        )
    return apply_overrides(Config(), data)


# ---------------------------------------------------------------------------
# Save (US-002 AC #6)
# ---------------------------------------------------------------------------


def save_config(cfg: Config, path: str | Path) -> None:
    """Atomically write ``cfg`` to ``path`` (UTF-8, LF, trailing newline).

    Delegates to :func:`core._io.atomic_write_bytes` per US-002 AC #6.
    The payload is serialized to JSON with ``indent=2`` and a trailing
    newline so the file is human-readable and matches the convention
    used by ``defaults/config.json``.

    Iterates ``dataclasses.fields(cfg)`` directly (rather than
    ``Config.field_names()``, which returns a ``frozenset``) so the
    on-disk key order is the dataclass declaration order. Frozenset
    iteration order is insertion-order in current CPython but not
    guaranteed by the language; pinning the order here avoids spurious
    diffs for users who track ``config.json`` in their dotfile repo.
    """
    path_obj = Path(path)
    payload: dict[str, Any] = {f.name: getattr(cfg, f.name) for f in fields(cfg)}
    body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path_obj, body, prefix=".config-", suffix=".json")
