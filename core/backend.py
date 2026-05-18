"""Backend abstract base class.

All concrete backends (CliBackend, ApiBackend) implement this ABC. The
pipeline depends only on this interface so backends can be swapped without
touching call sites. See PRD §5.2 (P1-BKND-01) and SPEC §6.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CompletionResult:
    """Result of a single Backend.complete() call.

    `input_tokens` and `output_tokens` are None for backends that do not
    expose usage (e.g. the Claude CLI).
    """

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class AuthStatus:
    """Result of Backend.check_auth(). `ok=True` means the backend is usable."""

    ok: bool
    detail: str = ""


class BackendError(Exception):
    """Base class for backend-raised errors."""


class AuthError(BackendError):
    """Authentication failed (401, expired CLI token, etc)."""


class NoBackendError(BackendError):
    """No backend could be resolved."""


class Backend(ABC):
    """Abstract backend interface.

    Concrete backends must set `name` to one of `"claude-cli"` or `"api-key"`
    (the same string is persisted to session records and config).
    """

    name: str = ""

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: Iterable[dict[str, str]],
        stream: bool = False,
    ) -> CompletionResult:
        """Run a single completion turn.

        Args:
            system: System prompt text.
            messages: Sequence of {"role": "user"|"assistant", "content": str}.
            stream: If True and stdout is a TTY, stream tokens to stderr.

        Returns:
            CompletionResult with the full assistant text.
        """
        raise NotImplementedError

    @abstractmethod
    def check_auth(self) -> AuthStatus:
        """Lightweight liveness check used by --status."""
        raise NotImplementedError
