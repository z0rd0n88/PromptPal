"""Backend resolution and persistence (US-006 / SPEC §6, P1-BKND-04..07, P1-BKND-13).

:func:`resolve_backend` is the single entry point the CLI layer calls to
turn a ``--backend`` value (``"auto" | "claude-cli" | "api-key" | None``)
into a concrete :class:`~core.backend.Backend` instance. The selection
order (P1-BKND-04) is::

    1. explicit ``preferred`` (when ``"claude-cli"`` / ``"api-key"``)
    2. ``Config.preferred_backend`` (when non-``"auto"``)
    3. auto-detect: ``claude`` on PATH → CLI; else env key set → API;
       else :class:`~core.backend.NoBackendError`.

Hard-fail paths (P1-BKND-05/06):

- ``preferred == "claude-cli"`` and ``claude`` missing → :class:`CliNotFoundError`.
  Never silently falls back to the API.
- ``preferred == "api-key"`` and ``ANTHROPIC_API_KEY`` unset →
  :class:`ApiKeyMissingError`. Never silently falls back to the CLI.

Persistence (P1-BKND-13 / D-Q10):

The persist-after-success and clear-on-auto policy is driven by the
caller. :func:`clear_backend_preference` writes ``"auto"`` to disk
*immediately* (called *before* the resolve/turn sequence when the user
passes ``--backend auto``). :func:`persist_backend_preference` writes
``"claude-cli"`` or ``"api-key"`` to disk *after the first successful
turn*. A failed turn must therefore not call ``persist_*`` — that's the
caller's responsibility, and the tests pin both halves.

Testability
-----------

- ``env`` is injected (defaults to :data:`os.environ`) so tests can
  toggle ``ANTHROPIC_API_KEY`` without monkeypatching globals.
- ``which`` is injected (defaults to :func:`shutil.which`) so tests can
  fake the presence/absence of ``claude`` on PATH.
- ``cli_factory`` / ``api_factory`` are injected so tests don't need a
  real :class:`CliBackend` / :class:`ApiBackend` instance (the latter
  raises on construction when the env key is missing — useful behavior
  in production, awkward in unit tests).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Callable

from core.api_backend import (
    NO_KEY_MESSAGE,
    ApiBackend,
    ApiKeyMissingError,
)
from core.backend import Backend, NoBackendError
from core.cli_backend import (
    CLI_NOT_FOUND_MESSAGE,
    CliBackend,
    CliNotFoundError,
)
from core.config import (
    VALID_PREFERRED_BACKENDS,
    Config,
    PreferredBackend,
    load_config,
    save_config,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_EXECUTABLE: str = "claude"
ANTHROPIC_API_KEY_ENV: str = "ANTHROPIC_API_KEY"


# Type aliases for the injectable hooks. These mirror the signatures
# tests use, kept explicit so callers don't have to import internal
# Callable types.
WhichFn = Callable[[str], "str | None"]
ApiFactory = Callable[[str], Backend]
CliFactory = Callable[[str], Backend]


# ---------------------------------------------------------------------------
# resolve_backend (P1-BKND-04..07)
# ---------------------------------------------------------------------------


def _effective_preference(
    preferred: str | None, config_preference: str
) -> PreferredBackend:
    """Collapse the (CLI flag, Config) pair into the layer that drives selection.

    - Explicit CLI ``"claude-cli"``/``"api-key"`` always wins.
    - Explicit CLI ``"auto"`` bypasses Config (per AC #6: ``--backend
      auto`` *is* the user's explicit choice — they want auto-detect,
      not whatever's persisted in Config).
    - ``None`` (no flag) falls through to Config.
    - Anything else (defensive: typo from caller) collapses to ``"auto"``.
    """
    if preferred == "claude-cli" or preferred == "api-key":
        return preferred  # type: ignore[return-value]
    if preferred is None and config_preference in VALID_PREFERRED_BACKENDS:
        return config_preference  # type: ignore[return-value]
    return "auto"


def resolve_backend(
    preferred: str | None,
    model: str,
    *,
    config: Config,
    env: Mapping[str, str] | None = None,
    which: WhichFn | None = None,
    api_factory: ApiFactory | None = None,
    cli_factory: CliFactory | None = None,
) -> Backend:
    """Resolve which :class:`Backend` to use per P1-BKND-04..07.

    Parameters
    ----------
    preferred:
        ``"auto" | "claude-cli" | "api-key" | None``. ``None`` means no
        ``--backend`` flag was passed (the CLI default).
    model:
        Model id forwarded to whichever backend is constructed.
    config:
        Current :class:`Config` — only ``preferred_backend`` is read.
    env, which, api_factory, cli_factory:
        Injectable for tests. Defaults wire up the real environment,
        :func:`shutil.which`, and the real backend constructors.

    Raises
    ------
    CliNotFoundError:
        Preferred is ``"claude-cli"`` and ``claude`` is missing from
        ``PATH`` (P1-BKND-05). Carries
        :data:`~core.cli_backend.CLI_NOT_FOUND_MESSAGE`.
    ApiKeyMissingError:
        Preferred is ``"api-key"`` and ``ANTHROPIC_API_KEY`` is unset
        (P1-BKND-06). Carries :data:`~core.api_backend.NO_KEY_MESSAGE`.
    NoBackendError:
        Auto-detect ran and neither backend is available (P1-BKND-07).
        Carries the two-option setup message.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    which_fn: WhichFn = which if which is not None else shutil.which
    cli_make: CliFactory = cli_factory if cli_factory is not None else CliBackend
    api_make: ApiFactory = api_factory if api_factory is not None else ApiBackend

    effective = _effective_preference(preferred, config.preferred_backend)

    if effective == "claude-cli":
        if not which_fn(CLAUDE_EXECUTABLE):
            raise CliNotFoundError(CLI_NOT_FOUND_MESSAGE)
        return cli_make(model)

    if effective == "api-key":
        if not env_map.get(ANTHROPIC_API_KEY_ENV, ""):
            raise ApiKeyMissingError(NO_KEY_MESSAGE)
        return api_make(model)

    # effective == "auto" — auto-detect ladder.
    if which_fn(CLAUDE_EXECUTABLE):
        return cli_make(model)
    if env_map.get(ANTHROPIC_API_KEY_ENV, ""):
        return api_make(model)
    raise NoBackendError(NoBackendError.MESSAGE)


# ---------------------------------------------------------------------------
# Persistence helpers (P1-BKND-13 / AC #5, #6, #7)
# ---------------------------------------------------------------------------


def _write_preferred_backend(
    config_path: str | Path, value: PreferredBackend
) -> None:
    """Load → set ``preferred_backend=value`` → atomically save.

    Other Config fields on disk are preserved verbatim (the load/save
    round-trip retains them via :func:`load_config` /
    :func:`save_config`). Missing config file is treated as "defaults"
    — we still write the new preference so it's there for the next run.
    """
    config = load_config(config_path)
    new_config = replace(config, preferred_backend=value)
    save_config(new_config, config_path)


def clear_backend_preference(config_path: str | Path) -> None:
    """Reset ``Config.preferred_backend`` to ``"auto"`` (AC #6, P1-BKND-13).

    Called by the CLI layer the moment ``--backend auto`` is parsed —
    before resolve_backend, before any turn. The reset is immediate so
    a subsequent crash doesn't leave a stale preference on disk.
    """
    _write_preferred_backend(config_path, "auto")


def persist_backend_preference(
    config_path: str | Path, value: PreferredBackend
) -> None:
    """Persist an explicit ``--backend`` choice to disk (AC #5, P1-BKND-13).

    Called by the CLI layer *after the first successful turn* with
    ``--backend claude-cli`` or ``--backend api-key``. Failed turns
    must therefore not call this (AC #7).

    Defensive: invalid values (somehow plumbed through from a typo) are
    silently dropped — we never persist a non-canonical preference.
    """
    if value not in VALID_PREFERRED_BACKENDS:
        return
    _write_preferred_backend(config_path, value)
