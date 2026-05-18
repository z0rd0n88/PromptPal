"""Runtime configuration for PromptPal.

Authoritative schema: SPEC.md §10 (Configuration). The ten fields below
mirror the user-editable JSON persisted at ``~/.promptpal/config.json``.
Each field has a hard-coded default (used when no file exists) and is
overridable in three layers per PRD P1-CFG-02:

    dataclass defaults  →  config.json overrides  →  CLI flag overrides

US-001 only requires that the dataclass and the bundled defaults file
exist; the full load/merge/atomic-write pipeline lands in US-002.

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

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal


PreferredBackend = Literal["auto", "claude-cli", "api-key"]

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
