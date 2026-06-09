"""Refinement loop (US-010 / SPEC §8, P1-LOOP-01..08).

After the first ``Backend.complete()`` call lands the initial improved
prompt, the user is presented with the canonical choice line:

  ``[a]ccept [i]terate [d]iscard [r]aw [c]opy``

This module owns that interactive state machine. It is structured as a
pure function over injectable I/O seams (``stdin`` / ``stderr`` /
``choice_reader`` / ``feedback_reader`` / ``display`` / ``copy_fn``) so
tests exercise every branch with ``io.StringIO`` queues rather than
monkeypatching ``sys`` globals.

Pipe-safety contract (P1-PIPE-09) is upheld structurally: the loop
writes nothing to ``stdout``. The choice prompt, raw-print, and copy
confirmation all land on ``stderr``. Only the CLI layer (US-011) prints
the final improved prompt to stdout, and only when the loop returns
``status == "accepted"``.

Persistence is the caller's job:

- The loop does **not** call ``write_session`` / ``finalize_session``.
  It returns a :class:`LoopOutcome` carrying the terminal status, the
  final improved text, the full messages projection, and the per-call
  metadata for every backend turn the loop produced. The CLI layer
  (US-011) folds those into the in-flight :class:`core.history.Session`
  and writes it to disk.
- The loop does **not** call ``sys.exit``. The caller decides the exit
  code (0 for accept/discard; the loop only ever returns one of those
  two statuses).
- The loop does **not** call ``platform.copy_to_clipboard`` directly.
  Clipboard support is injected via ``copy_fn`` so tests can record
  invocations without a real provider on PATH. The CLI layer wires
  ``copy_fn = lambda text: copy_to_clipboard(text, platform)``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TextIO

from core.backend import Backend


# ---------------------------------------------------------------------------
# Canonical strings — pinned verbatim by tests
# ---------------------------------------------------------------------------

CHOICE_PROMPT: str = "[a]ccept [i]terate [d]iscard [r]aw [c]opy: "
"""The interactive choice line (P1-LOOP-01)."""

ITERATE_FEEDBACK_PROMPT: str = (
    "Enter your feedback (single line, then Enter):\n"
)

SYNTHESIZED_FEEDBACK: str = "Improve this further."
"""Auto-iteration feedback turn (P1-LOOP-08)."""

RAW_HEADER: str = "--- raw improved prompt ---"
"""Header printed to stderr before the raw improved prompt (P1-LOOP-06)."""

COPY_SUCCESS_MESSAGE: str = "Copied to clipboard."
"""Stderr confirmation when ``copy_fn`` returns True."""


# ---------------------------------------------------------------------------
# Action constants
# ---------------------------------------------------------------------------

ACTION_ACCEPT: str = "a"
ACTION_ITERATE: str = "i"
ACTION_DISCARD: str = "d"
ACTION_RAW: str = "r"
ACTION_COPY: str = "c"

VALID_ACTIONS: tuple[str, ...] = (
    ACTION_ACCEPT,
    ACTION_ITERATE,
    ACTION_DISCARD,
    ACTION_RAW,
    ACTION_COPY,
)


# ---------------------------------------------------------------------------
# Status constants — match history.STATUS_* values, kept local so this
# module doesn't import the history layer.
# ---------------------------------------------------------------------------

STATUS_ACCEPTED: str = "accepted"
STATUS_DISCARDED: str = "discarded"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopTurn:
    """One backend call made during the loop.

    Captures the metadata the caller needs to fold into the
    :class:`core.history.Session` (role/content/token-counts) without
    coupling this module to ``core.history``. ``user_content`` is the
    feedback string that triggered this assistant response; the caller
    derives the user turn from it.
    """

    backend: str
    input_tokens: int | None
    output_tokens: int | None
    response_text: str
    user_content: str


@dataclass(frozen=True)
class LoopOutcome:
    """Terminal state returned to the CLI layer.

    ``status`` is one of :data:`STATUS_ACCEPTED` / :data:`STATUS_DISCARDED`.
    ``final_prompt`` is the assistant text the user accepted (or the
    last assistant text when discarded, for the historical record).
    ``messages`` is the full conversation projection — ready to feed
    back into ``--replay``. ``new_turns`` lists every backend call the
    loop made (one per ``[i]terate`` choice plus auto-iterations).
    """

    status: str
    final_prompt: str
    messages: tuple[dict[str, str], ...]
    new_turns: tuple[LoopTurn, ...]


# ---------------------------------------------------------------------------
# I/O seam helpers
# ---------------------------------------------------------------------------


ChoiceReader = Callable[[], str]
"""Signature: ``() -> str``. Returns one line of raw input from the user."""

FeedbackReader = Callable[[], str]
"""Signature: ``() -> str``. Returns one line of iterate feedback (no banner)."""

Display = Callable[[str, str], None]
"""Signature: ``(original, improved) -> None``. Renders the diff/full prompt."""

CopyFn = Callable[[str], bool]
"""Signature: ``(text) -> bool``. ``True`` = wrote to clipboard, ``False`` = warned."""


def _default_choice_reader(stdin: TextIO, stderr: TextIO) -> ChoiceReader:
    def reader() -> str:
        print(CHOICE_PROMPT, end="", file=stderr, flush=True)
        return stdin.readline()

    return reader


def _default_feedback_reader(stdin: TextIO, stderr: TextIO) -> FeedbackReader:
    def reader() -> str:
        print(ITERATE_FEEDBACK_PROMPT, end="", file=stderr, flush=True)
        line = stdin.readline()
        # Strip a single trailing newline so the feedback turn doesn't
        # carry the user's Enter keystroke into the conversation history.
        if line.endswith("\n"):
            line = line[:-1]
        return line

    return reader


def _default_display(stderr: TextIO) -> Display:
    def display(_original: str, improved: str) -> None:
        print(improved, file=stderr)

    return display


def _normalize_action(raw: str) -> str | None:
    """Map a raw input line to a canonical action character or ``None``.

    Returns one of ``a/i/d/r/c`` (the first matching character of the
    stripped, lowercased line) or ``None`` for empty / unrecognized
    input. ``None`` means "re-present the choice line" — the loop
    re-prompts rather than guessing intent (P1-LOOP-01 keeps the menu
    explicit).
    """
    s = raw.strip().lower()
    if not s:
        return None
    c = s[0]
    return c if c in VALID_ACTIONS else None


# ---------------------------------------------------------------------------
# Loop entry point
# ---------------------------------------------------------------------------


def run_refinement_loop(
    *,
    backend: Backend,
    system: str,
    initial_messages: Sequence[Mapping[str, str]],
    initial_improved: str,
    original_prompt: str,
    auto_iterations: int = 0,
    copy_on_accept: bool = False,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    display: Display | None = None,
    choice_reader: ChoiceReader | None = None,
    feedback_reader: FeedbackReader | None = None,
    copy_fn: CopyFn | None = None,
) -> LoopOutcome:
    """Run the refinement loop until the user accepts or discards.

    Parameters
    ----------
    backend
        The resolved :class:`Backend` instance to call on each iterate.
        The same backend handles auto-iterations.
    system
        The resolved system prompt text (already loaded by the caller).
    initial_messages
        The conversation *before* the loop starts: the original user
        prompt and the first assistant response. The loop appends new
        user/assistant turns on each iterate.
    initial_improved
        The assistant text from ``initial_messages`` — passed in
        directly so the loop doesn't have to dig into the message
        envelope.
    original_prompt
        The raw user prompt — passed to :func:`display` so the diff
        layer can compute a unified diff against ``improved``.
    auto_iterations
        Number of automatic iterations to run *before* presenting the
        choice line (P1-LOOP-08 / ``--iterations N``). Each auto-
        iteration appends ``SYNTHESIZED_FEEDBACK`` as a user turn and
        calls the backend. ``0`` skips auto-iteration.
    copy_on_accept
        When ``True``, the accept branch calls ``copy_fn`` with the
        final improved prompt. Bundled by the CLI layer from
        ``auto_copy`` (Config) or ``--copy`` (flag) — the loop doesn't
        need to know which.
    stdin / stderr / display / choice_reader / feedback_reader / copy_fn
        Injectable seams. Production callers pass nothing and get
        ``sys.stdin`` / ``sys.stderr`` / a plain "print improved to
        stderr" display / line-based readers / ``copy_fn=None`` (which
        treats every copy invocation as "no provider").

    Returns
    -------
    LoopOutcome
        Carries the terminal status and the data the caller needs to
        finalize and write the session.
    """
    stdin_in: TextIO = stdin if stdin is not None else sys.stdin
    stderr_in: TextIO = stderr if stderr is not None else sys.stderr
    choice_fn: ChoiceReader = (
        choice_reader
        if choice_reader is not None
        else _default_choice_reader(stdin_in, stderr_in)
    )
    feedback_fn: FeedbackReader = (
        feedback_reader
        if feedback_reader is not None
        else _default_feedback_reader(stdin_in, stderr_in)
    )
    display_fn: Display = display if display is not None else _default_display(stderr_in)

    messages: list[dict[str, str]] = [dict(m) for m in initial_messages]
    improved = initial_improved
    new_turns: list[LoopTurn] = []

    # P1-LOOP-08: auto-iterations BEFORE the first choice line.
    n_auto = max(0, auto_iterations)
    for _ in range(n_auto):
        improved, turn = _iterate(
            backend=backend,
            system=system,
            messages=messages,
            feedback=SYNTHESIZED_FEEDBACK,
        )
        new_turns.append(turn)
    if n_auto > 0:
        display_fn(original_prompt, improved)

    while True:
        # H8 (issue #30): default _default_choice_reader uses readline()
        # which returns "" on EOF, but an input()-backed injected reader
        # raises EOFError instead. Treat both as "EOF — exit cleanly"
        # so the loop never infinite-loops on a closed input stream.
        try:
            raw = choice_fn()
        except EOFError:
            raw = ""
        if raw == "":
            # EOF on stdin — bail out cleanly as a discard so the loop
            # cannot infinite-loop on a closed input stream.
            return LoopOutcome(
                status=STATUS_DISCARDED,
                final_prompt=improved,
                messages=tuple(messages),
                new_turns=tuple(new_turns),
            )

        action = _normalize_action(raw)
        if action is None:
            continue  # re-present the choice line

        if action == ACTION_ACCEPT:
            if copy_on_accept and copy_fn is not None:
                ok = copy_fn(improved)
                if ok:
                    print(COPY_SUCCESS_MESSAGE, file=stderr_in)
            return LoopOutcome(
                status=STATUS_ACCEPTED,
                final_prompt=improved,
                messages=tuple(messages),
                new_turns=tuple(new_turns),
            )

        if action == ACTION_DISCARD:
            return LoopOutcome(
                status=STATUS_DISCARDED,
                final_prompt=improved,
                messages=tuple(messages),
                new_turns=tuple(new_turns),
            )

        if action == ACTION_ITERATE:
            # H8 (issue #30): same EOFError tolerance as choice_fn — an
            # input()-backed feedback reader raises EOFError, which we
            # treat as empty feedback (skip + re-present, not crash).
            try:
                feedback = feedback_fn()
            except EOFError:
                feedback = ""
            if not feedback.strip():
                # Empty feedback — skip the backend call so we don't
                # burn tokens on whitespace. Re-present the choice line.
                # M17 (issue #30): tell the user why nothing happened so
                # the silent re-presentation doesn't look like a hang.
                print(
                    "Feedback was empty — iteration skipped.",
                    file=stderr_in,
                )
                continue
            improved, turn = _iterate(
                backend=backend,
                system=system,
                messages=messages,
                feedback=feedback,
            )
            new_turns.append(turn)
            display_fn(original_prompt, improved)
            continue

        if action == ACTION_RAW:
            print(RAW_HEADER, file=stderr_in)
            print(improved, file=stderr_in)
            continue

        if action == ACTION_COPY:
            if copy_fn is None:
                # No copy_fn wired (no provider available). The CLI
                # layer wires a copy_fn that prints its own no-provider
                # warning; when copy_fn is None here we still need to
                # warn so the user knows the action was ignored.
                # Mirrors P1-PLAT-07's non-fatal contract.
                print(
                    "Warning: no clipboard provider available.",
                    file=stderr_in,
                )
                continue
            ok = copy_fn(improved)
            if ok:
                print(COPY_SUCCESS_MESSAGE, file=stderr_in)
            # On False: copy_fn already warned via its own channel
            # (e.g. platform.copy_to_clipboard prints NO_CLIPBOARD_WARNING
            # or a subprocess-failed warning). Don't double-print.
            continue


def _iterate(
    *,
    backend: Backend,
    system: str,
    messages: list[dict[str, str]],
    feedback: str,
) -> tuple[str, LoopTurn]:
    """Append a user feedback turn, call the backend, append the response.

    Mutates ``messages`` in place (the loop holds the only reference)
    and returns the new improved text plus a :class:`LoopTurn` carrying
    the metadata the caller will fold into the session.

    H7 (issue #30): commits the user turn to ``messages`` **only after**
    ``backend.complete`` returns successfully. Pre-fix code appended the
    user turn first, so a raise from the backend (network blip, auth
    failure, timeout) left a dangling user turn — the next ``_iterate``
    call appended another user turn on top, producing two consecutive
    user turns that the production claude-cli parser misbehaves on.

    The candidate is the existing ``messages`` plus the new user turn;
    the defensive ``[dict(m) for m in candidate]`` copy still protects
    against a backend that mutates its argument.
    """
    candidate = list(messages)
    candidate.append({"role": "user", "content": feedback})
    response = backend.complete(system, [dict(m) for m in candidate])
    # Backend call succeeded — only now commit the user turn and the
    # matching assistant turn to the real messages list.
    messages.append({"role": "user", "content": feedback})
    messages.append({"role": "assistant", "content": response.text})
    turn = LoopTurn(
        backend=backend.name,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        response_text=response.text,
        user_content=feedback,
    )
    return response.text, turn
