"""Prompt input resolution (US-009, SPEC §5 Input Resolution, P1-PIPE-01..03).

Resolves the raw user prompt from one of three sources, in priority order:

1. Positional CLI argument: ``promptpal "your prompt"`` (P1-PIPE-01).
2. ``stdin`` pipe when stdin is **not** a TTY: ``echo "prompt" | promptpal``
   (P1-PIPE-02).
3. Interactive TTY input when no arg is given and stdin **is** a TTY
   (P1-PIPE-03).

The resolver writes nothing to ``stdout`` — the interactive banner goes to
``stderr`` so the pipe-safety contract from P1-PIPE-09 ("improved prompt
on stdout, everything else on stderr") is upheld even in interactive mode.

Test seam pattern matches the rest of ``core/``: ``stdin``, ``stderr``,
and ``isatty`` are injectable kwargs; production callers pass nothing
and get the real ``sys.stdin`` / ``sys.stderr`` / ``stdin.isatty``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO


EMPTY_PROMPT_MESSAGE: str = (
    "Error: empty prompt. Pass a prompt as an argument, pipe it via stdin, "
    "or enter text interactively."
)

INTERACTIVE_PROMPT_BANNER: str = (
    "Enter your prompt (end with Ctrl-D on Linux/macOS, Ctrl-Z then Enter "
    "on Windows):\n"
)


class EmptyPromptError(ValueError):
    """Raised when the resolved prompt is empty after stripping whitespace.

    The CLI layer (US-011) catches this and exits 1 with
    :data:`EMPTY_PROMPT_MESSAGE` on stderr.
    """

    MESSAGE: str = EMPTY_PROMPT_MESSAGE


def read_prompt(
    positional_arg: str | None,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    isatty: Callable[[], bool] | None = None,
) -> str:
    """Return the raw user prompt resolved from arg / stdin / interactive.

    Selection order (P1-PIPE-01..03):

    - ``positional_arg`` is non-``None`` → return it verbatim. Whitespace-
      only args raise :class:`EmptyPromptError` so a stray ``promptpal ""``
      surfaces as an actionable error instead of silently falling through
      to stdin.
    - ``positional_arg`` is ``None`` and ``stdin`` is **not** a TTY →
      ``stdin.read()`` (the full piped payload, trailing newline preserved).
    - ``positional_arg`` is ``None`` and ``stdin`` **is** a TTY → write the
      interactive banner to ``stderr`` and read until EOF.

    Bytes are returned verbatim — no trim, no normalization. The
    backend layer is responsible for treating ``"Human:"``-laced prompts
    byte-identically (matches the AC-MT-01 contract from US-005).
    """
    if positional_arg is not None:
        if not positional_arg.strip():
            raise EmptyPromptError(EMPTY_PROMPT_MESSAGE)
        return positional_arg

    stdin_in: TextIO = stdin if stdin is not None else sys.stdin
    stderr_in: TextIO = stderr if stderr is not None else sys.stderr
    is_tty_fn: Callable[[], bool] = (
        isatty if isatty is not None else stdin_in.isatty
    )

    if is_tty_fn():
        print(INTERACTIVE_PROMPT_BANNER, end="", file=stderr_in, flush=True)

    text = stdin_in.read()
    if not text.strip():
        raise EmptyPromptError(EMPTY_PROMPT_MESSAGE)
    return text
