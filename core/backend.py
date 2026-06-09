"""Backend abstract base class.

Authoritative shape: SPEC.md §6 (Backend Integration). Every concrete
backend (`ApiBackend`, `CliBackend`, future additions) implements the same
contract so the pipeline depends only on this interface.

Token-count semantics (P1-BKND-10): API turns carry numeric
`input_tokens`/`output_tokens`; CLI turns carry `None` for both.

US-001 only defines the ABC. Concrete backends, `resolve_backend()`, and
the auto-detection ladder land in US-006 (SPEC §6 "Auto-Detection").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TypeAlias


#: One message on the wire between PromptPal and a backend.
#:
#: At minimum carries ``{"role": "user"|"assistant", "content": <string-or-block-array>}``;
#: ``content`` can be a bare string (callers' shape) or a block-array
#: ``[{"type":"text","text":"..."}]`` after normalization in
#: :func:`core.cli_backend._normalize_content`. Typed as
#: ``dict[str, Any]`` rather than ``TypedDict`` because the value shape
#: is heterogeneous; the alias is a documentation hook so a future
#: tightening to ``TypedDict`` lands in one place.
Message: TypeAlias = dict[str, Any]


@dataclass(frozen=True)
class BackendResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None


class Backend(ABC):
    """Abstract base class for prompt-improvement backends (SPEC §6).

    Convention: concrete subclasses (and related exception classes in the
    same backend module) MAY define a ``MESSAGE: ClassVar[str]`` class
    attribute carrying a canonical user-facing error string — see e.g.
    ``ApiKeyMissingError.MESSAGE`` and ``NoBackendError.MESSAGE``. This
    is a documentation convention only; it is not enforced by the ABC.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable identifier, e.g. ``'claude-cli (claude-sonnet-4-6)'``."""

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[Message],
        stream: bool = False,
    ) -> BackendResponse:
        """Execute one completion turn and return the assembled response.

        ``messages`` follows the :data:`Message` wire shape — see the
        alias docstring at the top of this module for the per-element
        contract.
        """

    @abstractmethod
    def check_auth(self) -> bool:
        """Lightweight liveness/auth probe (used by ``--status`` and first-run setup).

        Implementations may interpret "auth" with backend-appropriate
        strictness:

        - :class:`core.api_backend.ApiBackend` fires a real
          ``max_tokens=1`` round-trip — a true auth check.
        - :class:`core.cli_backend.CliBackend` runs ``claude --version``,
          which probes binary presence rather than OAuth liveness. A
          real auth check there would require an interactive OAuth
          round-trip unsuitable for ``--status``.

        The bool return reflects the strongest confidence the backend
        can establish without user interaction (M14 — issue #30).
        """


class NoBackendError(Exception):
    """Raised when no backend is available and no explicit choice was made."""

    MESSAGE = (
        "Error: No backend available. Set up one of the following:\n"
        "  Option 1 (Claude CLI): Install Claude Code and run `claude auth login`\n"
        '  Option 2 (API key):    export ANTHROPIC_API_KEY="sk-ant-..."'
    )
