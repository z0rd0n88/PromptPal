"""Integration tests for core/loop.py (US-010 / P1-LOOP-01..08).

Coverage map (1 test cluster → 1 acceptance criterion):

  AC #1   choice line shown after improved prompt        → test_choice_*
  AC #2   [i]terate appends user/assistant, recalls       → test_iterate_*
  AC #3   3+ successive iterations supported              → test_three_iterations
  AC #4   [a]ccept yields accepted status + optional copy → test_accept_*
  AC #5   [d]iscard yields discarded status               → test_discard_*
  AC #6   [r]aw prints to stderr, re-prompts              → test_raw_*
  AC #7   [c]opy invokes copy_fn, re-prompts              → test_copy_*
  AC #8   --iterations N auto-iterates before choice line → test_auto_iterations_*

The fake backend below pops responses from a queue so each iteration
sees a deterministic improved text. Choice / feedback are injected as
explicit callables that drain pre-seeded lists — no monkeypatching of
``sys`` globals.
"""

from __future__ import annotations

import io

import pytest

from core.backend import Backend, BackendResponse, Message
from core.loop import (
    CHOICE_PROMPT,
    COPY_SUCCESS_MESSAGE,
    ITERATE_FEEDBACK_PROMPT,
    RAW_HEADER,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    SYNTHESIZED_FEEDBACK,
    VALID_ACTIONS,
    LoopTurn,
    run_refinement_loop,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeBackend(Backend):
    """Backend that pops pre-seeded responses off a queue.

    Each ``complete()`` call records the (system, messages) pair it was
    invoked with and returns the next response in the queue. When the
    queue empties, raises ``AssertionError`` so a misconfigured test
    fails loudly instead of looping forever.
    """

    def __init__(
        self,
        responses: list[BackendResponse],
        *,
        name: str = "fake-backend (test)",
    ) -> None:
        self._responses = list(responses)
        self._name = name
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    @property
    def name(self) -> str:
        return self._name

    def complete(
        self, system: str, messages: list[Message], stream: bool = False
    ) -> BackendResponse:
        if not self._responses:
            raise AssertionError(
                "FakeBackend: response queue exhausted — test wired the "
                "wrong number of responses."
            )
        self.calls.append((system, list(messages)))
        return self._responses.pop(0)

    def check_auth(self) -> bool:
        return True


def _make_choice_reader(choices: list[str]):
    """Return a choice_reader that pops one entry per call.

    When the queue empties, returns ``""`` (EOF) so the loop bails out
    cleanly as a discard instead of hanging the test.
    """
    pending = list(choices)

    def reader() -> str:
        if not pending:
            return ""
        return pending.pop(0)

    return reader


def _make_feedback_reader(feedbacks: list[str]):
    """Return a feedback_reader that pops one entry per call."""
    pending = list(feedbacks)

    def reader() -> str:
        if not pending:
            return ""
        return pending.pop(0)

    return reader


def _make_copy_fn(*, success: bool = True, calls: list[str] | None = None):
    """Return a copy_fn that records every text passed to it."""
    record = calls if calls is not None else []

    def copy(text: str) -> bool:
        record.append(text)
        return success

    return copy, record


def _initial_messages(
    original_prompt: str = "user prompt", improved: str = "improved v1"
) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": original_prompt},
        {"role": "assistant", "content": improved},
    ]


# ---------------------------------------------------------------------------
# Constants — pin canonical strings (regression bait)
# ---------------------------------------------------------------------------


def test_choice_prompt_lists_all_five_actions():
    """The canonical choice line names every menu option (P1-LOOP-01)."""
    assert "[a]ccept" in CHOICE_PROMPT
    assert "[i]terate" in CHOICE_PROMPT
    assert "[d]iscard" in CHOICE_PROMPT
    assert "[r]aw" in CHOICE_PROMPT
    assert "[c]opy" in CHOICE_PROMPT


def test_valid_actions_are_exactly_the_five_menu_chars():
    """VALID_ACTIONS is the source of truth for character matching."""
    assert set(VALID_ACTIONS) == {"a", "i", "d", "r", "c"}


def test_synthesized_feedback_is_pinned_verbatim():
    """P1-LOOP-08 hardcodes 'Improve this further.' as the auto-iteration body."""
    assert SYNTHESIZED_FEEDBACK == "Improve this further."


def test_status_constants_match_history_layer():
    """The loop's status strings must match history.STATUS_* so the
    caller can pass them straight into finalize_session.
    """
    from core.history import STATUS_ACCEPTED as H_ACC
    from core.history import STATUS_DISCARDED as H_DISC

    assert STATUS_ACCEPTED == H_ACC
    assert STATUS_DISCARDED == H_DISC


# ---------------------------------------------------------------------------
# AC #1 — choice line shown after improved prompt
# ---------------------------------------------------------------------------


def test_choice_line_written_to_stderr_on_first_prompt():
    """The default choice reader writes the menu to stderr."""
    backend = FakeBackend(responses=[])
    stdin = io.StringIO("a\n")
    stderr = io.StringIO()

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        stdin=stdin,
        stderr=stderr,
    )
    assert outcome.status == STATUS_ACCEPTED
    assert CHOICE_PROMPT in stderr.getvalue()


def test_loop_writes_nothing_to_stdout(capsys):
    """Pipe-safety contract (P1-PIPE-09): the loop never writes to stdout."""
    backend = FakeBackend(responses=[])
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["a"]),
    )
    assert outcome.status == STATUS_ACCEPTED
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# AC #2 — [i]terate appends user/assistant, recalls backend, redisplays
# ---------------------------------------------------------------------------


def test_iterate_appends_user_feedback_then_calls_backend():
    """[i]terate: user feedback → user turn → backend call → assistant turn."""
    backend = FakeBackend(
        responses=[BackendResponse(text="improved v2", input_tokens=10, output_tokens=20)]
    )
    feedback_reader = _make_feedback_reader(["make it shorter"])
    choice_reader = _make_choice_reader(["i", "a"])

    outcome = run_refinement_loop(
        backend=backend,
        system="SYSTEM",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=choice_reader,
        feedback_reader=feedback_reader,
    )

    # Backend called once during the loop.
    assert len(backend.calls) == 1
    # Full messages array sent (4 entries: original user, v1 assistant,
    # feedback user, [v2 assistant lands AFTER the call]).
    system, messages = backend.calls[0]
    assert system == "SYSTEM"
    assert messages == [
        {"role": "user", "content": "user prompt"},
        {"role": "assistant", "content": "improved v1"},
        {"role": "user", "content": "make it shorter"},
    ]
    # Final improved came from the backend.
    assert outcome.final_prompt == "improved v2"
    assert outcome.status == STATUS_ACCEPTED
    # new_turns reflects exactly one backend call.
    assert outcome.new_turns == (
        LoopTurn(
            backend="fake-backend (test)",
            input_tokens=10,
            output_tokens=20,
            response_text="improved v2",
            user_content="make it shorter",
        ),
    )


def test_iterate_redisplays_improved_prompt():
    """[i]terate: re-displays the new improved prompt via the display callback."""
    backend = FakeBackend(
        responses=[BackendResponse(text="improved v2", input_tokens=1, output_tokens=2)]
    )
    displays: list[tuple[str, str]] = []

    def display(orig: str, imp: str) -> None:
        displays.append((orig, imp))

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["i", "a"]),
        feedback_reader=_make_feedback_reader(["shorter"]),
        display=display,
    )

    # The display was invoked exactly once after the iterate completed,
    # with the new improved text.
    assert displays == [("user prompt", "improved v2")]


def test_iterate_with_empty_feedback_skips_backend_call():
    """Empty feedback: do not burn a backend call; re-present choice line."""
    backend = FakeBackend(responses=[])  # would AssertionError if called
    choice_reader = _make_choice_reader(["i", "a"])
    feedback_reader = _make_feedback_reader(["   \n"])  # whitespace only

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=choice_reader,
        feedback_reader=feedback_reader,
    )
    # Backend never called; original improved survives.
    assert backend.calls == []
    assert outcome.final_prompt == "improved v1"
    assert outcome.new_turns == ()


def test_iterate_empty_feedback_prints_notice_to_stderr():
    """M17 (issue #30): an empty feedback (Enter at the iterate prompt)
    used to silently re-present the choice line. A one-line stderr
    notice now tells the user why nothing happened."""
    backend = FakeBackend(responses=[])
    stderr = io.StringIO()
    choice_reader = _make_choice_reader(["i", "a"])
    feedback_reader = _make_feedback_reader(["   \n"])  # whitespace only

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=choice_reader,
        feedback_reader=feedback_reader,
        stderr=stderr,
    )
    err = stderr.getvalue()
    assert "no feedback" in err.lower() or "empty" in err.lower(), (
        f"expected empty-feedback notice on stderr; got: {err!r}"
    )
    # Must be ASCII-safe — em-dash would crash on cp1252 / LANG=C terminals.
    assert "—" not in err, (
        "M17 notice must use ASCII hyphen, not em-dash, for terminal safety"
    )


def test_iterate_backend_failure_does_not_corrupt_messages():
    """H7 (issue #30): when ``backend.complete`` raises mid-iterate, the
    messages list must NOT carry a dangling user turn.

    Pre-fix code appended the user turn FIRST, then called the backend.
    On a raise, the user turn stayed in the list with no matching
    assistant turn — the next iterate appended ANOTHER user turn, giving
    two consecutive user turns that the production claude-cli parser
    misbehaves on. The fix builds a candidate, calls the backend with
    the candidate, only commits on success.
    """
    from core.loop import _iterate

    class _BoomBackend(Backend):
        @property
        def name(self) -> str:
            return "boom"

        def complete(
            self, system: str, messages: list[Message], stream: bool = False
        ) -> BackendResponse:
            raise RuntimeError("backend boom")

        def check_auth(self) -> bool:
            return False

    messages: list[Message] = [
        {"role": "user", "content": "orig"},
        {"role": "assistant", "content": "improved v1"},
    ]
    snapshot = [dict(m) for m in messages]
    with pytest.raises(RuntimeError, match="backend boom"):
        _iterate(
            backend=_BoomBackend(),
            system="sys",
            messages=messages,
            feedback="make it shorter",
        )
    # Messages must be byte-identical to the snapshot — no dangling
    # user turn left behind.
    assert messages == snapshot, (
        "_iterate must not commit the user turn when backend.complete fails"
    )


def test_loop_choice_reader_eoferror_treated_as_eof():
    """H8 (issue #30): a caller-injected choice_reader backed by
    ``input()`` raises ``EOFError`` on closed stdin. The loop's
    ``readline()``-based default returns ``""`` and exits via the
    EOF branch; ``input()``-based readers must reach the same exit
    instead of crashing the loop.
    """

    def eof_choice_reader() -> str:
        raise EOFError()

    outcome = run_refinement_loop(
        backend=FakeBackend(responses=[]),
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=eof_choice_reader,
    )
    # Same exit as raw == "": discard.
    assert outcome.status == STATUS_DISCARDED
    assert outcome.final_prompt == "improved v1"


def test_loop_feedback_reader_eoferror_treated_as_eof():
    """H8: same protection for feedback_reader — an EOFError must be
    treated as empty feedback (skip iterate + re-present), not crash."""
    backend = FakeBackend(responses=[])
    stderr = io.StringIO()

    def eof_feedback_reader() -> str:
        raise EOFError()

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["i", "a"]),
        feedback_reader=eof_feedback_reader,
        stderr=stderr,
    )
    # Same as a whitespace-only feedback: backend not called, accept exit
    # via the next choice.
    assert backend.calls == []
    assert outcome.final_prompt == "improved v1"


def test_iterate_messages_grow_monotonically():
    """Each iterate appends exactly two turns: user feedback + assistant response."""
    backend = FakeBackend(
        responses=[
            BackendResponse(text="v2", input_tokens=1, output_tokens=2),
            BackendResponse(text="v3", input_tokens=3, output_tokens=4),
        ]
    )

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["i", "i", "a"]),
        feedback_reader=_make_feedback_reader(["fb1", "fb2"]),
    )
    # Initial 2 turns + 2 iterations × 2 turns = 6 messages.
    assert len(outcome.messages) == 6
    # Order pins the structural invariant.
    assert outcome.messages == (
        {"role": "user", "content": "user prompt"},
        {"role": "assistant", "content": "improved v1"},
        {"role": "user", "content": "fb1"},
        {"role": "assistant", "content": "v2"},
        {"role": "user", "content": "fb2"},
        {"role": "assistant", "content": "v3"},
    )


# ---------------------------------------------------------------------------
# AC #3 — 3+ successive iterations supported (P1-LOOP-03)
# ---------------------------------------------------------------------------


def test_three_iterations_in_one_session():
    """The loop supports at least 3 successive iterations before accept."""
    backend = FakeBackend(
        responses=[
            BackendResponse(text=f"v{n}", input_tokens=n, output_tokens=n * 2)
            for n in (2, 3, 4)
        ]
    )
    initial = [
        {"role": "user", "content": "user prompt"},
        {"role": "assistant", "content": "v1"},
    ]

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=initial,
        initial_improved="v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["i", "i", "i", "a"]),
        feedback_reader=_make_feedback_reader(["fb1", "fb2", "fb3"]),
    )
    assert len(backend.calls) == 3
    assert outcome.final_prompt == "v4"
    # On the 3rd iterate's backend call, the prior 5 messages must be
    # present in the request body (P1-LOOP-02: no truncation).
    third_call_messages = backend.calls[2][1]
    # By the 3rd iterate's backend call: 2 initial + 2 pairs (u/a) from
    # iterates 1+2 + the new user feedback for iterate 3 = 7 messages.
    # P1-LOOP-02 mandates the full array is sent — no truncation.
    assert len(third_call_messages) == 7
    assert third_call_messages[0] == {"role": "user", "content": "user prompt"}
    assert third_call_messages[1] == {"role": "assistant", "content": "v1"}
    assert third_call_messages[-1] == {"role": "user", "content": "fb3"}


# ---------------------------------------------------------------------------
# AC #4 — [a]ccept yields accepted status + optional copy
# ---------------------------------------------------------------------------


def test_accept_returns_accepted_outcome():
    """[a]ccept on the first choice returns immediately with status=accepted."""
    backend = FakeBackend(responses=[])
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["a"]),
    )
    assert outcome.status == STATUS_ACCEPTED
    assert outcome.final_prompt == "improved v1"
    assert outcome.new_turns == ()


def test_accept_with_copy_on_accept_calls_copy_fn():
    """When copy_on_accept=True, the accept branch calls copy_fn(improved)."""
    backend = FakeBackend(responses=[])
    copy, copied = _make_copy_fn(success=True)
    stderr = io.StringIO()

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["a"]),
        copy_on_accept=True,
        copy_fn=copy,
        stderr=stderr,
    )
    assert outcome.status == STATUS_ACCEPTED
    assert copied == ["improved v1"]
    assert COPY_SUCCESS_MESSAGE in stderr.getvalue()


def test_accept_without_copy_on_accept_does_not_call_copy_fn():
    """When copy_on_accept=False, copy_fn is not invoked even when wired."""
    backend = FakeBackend(responses=[])
    copy, copied = _make_copy_fn(success=True)

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["a"]),
        copy_on_accept=False,
        copy_fn=copy,
    )
    assert copied == []


def test_accept_with_copy_on_accept_but_no_copy_fn_is_quiet():
    """copy_on_accept=True + copy_fn=None: silent (no warning, no crash)."""
    backend = FakeBackend(responses=[])
    stderr = io.StringIO()

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["a"]),
        copy_on_accept=True,
        copy_fn=None,
        stderr=stderr,
    )
    assert outcome.status == STATUS_ACCEPTED
    # No success or warning emitted — the accept path completes silently
    # when no clipboard is wired.
    assert COPY_SUCCESS_MESSAGE not in stderr.getvalue()


def test_accept_failed_copy_does_not_print_success():
    """copy_fn returning False suppresses the success message."""
    backend = FakeBackend(responses=[])
    copy, _calls = _make_copy_fn(success=False)
    stderr = io.StringIO()

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["a"]),
        copy_on_accept=True,
        copy_fn=copy,
        stderr=stderr,
    )
    assert COPY_SUCCESS_MESSAGE not in stderr.getvalue()


# ---------------------------------------------------------------------------
# AC #5 — [d]iscard yields discarded status
# ---------------------------------------------------------------------------


def test_discard_returns_discarded_outcome():
    backend = FakeBackend(responses=[])
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["d"]),
    )
    assert outcome.status == STATUS_DISCARDED
    assert outcome.final_prompt == "improved v1"


def test_discard_after_iterate_preserves_latest_improved():
    """[d]iscard records the latest improved text for the session record."""
    backend = FakeBackend(
        responses=[BackendResponse(text="improved v2", input_tokens=1, output_tokens=2)]
    )
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["i", "d"]),
        feedback_reader=_make_feedback_reader(["tweak"]),
    )
    assert outcome.status == STATUS_DISCARDED
    assert outcome.final_prompt == "improved v2"


def test_discard_does_not_call_copy_fn_even_when_copy_on_accept_true():
    backend = FakeBackend(responses=[])
    copy, copied = _make_copy_fn(success=True)
    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["d"]),
        copy_on_accept=True,
        copy_fn=copy,
    )
    assert copied == []


# ---------------------------------------------------------------------------
# AC #6 — [r]aw prints to stderr, re-presents choice
# ---------------------------------------------------------------------------


def test_raw_writes_to_stderr_and_re_prompts(capsys):
    """[r]aw: header + improved go to stderr, then choice line re-shown."""
    backend = FakeBackend(responses=[])
    stderr = io.StringIO()
    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["r", "a"]),
        stderr=stderr,
    )
    text = stderr.getvalue()
    assert RAW_HEADER in text
    assert "improved v1" in text
    # stdout untouched.
    assert capsys.readouterr().out == ""


def test_raw_re_presents_choice_for_each_invocation():
    """Repeated [r]aw is allowed and the choice line resurfaces every time."""
    backend = FakeBackend(responses=[])
    choices = _make_choice_reader(["r", "r", "r", "a"])
    stderr = io.StringIO()

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=choices,
        stderr=stderr,
    )
    assert outcome.status == STATUS_ACCEPTED


# ---------------------------------------------------------------------------
# AC #7 — [c]opy invokes copy_fn, re-presents choice
# ---------------------------------------------------------------------------


def test_copy_invokes_copy_fn_with_current_improved():
    backend = FakeBackend(responses=[])
    copy, copied = _make_copy_fn(success=True)
    stderr = io.StringIO()

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["c", "a"]),
        copy_fn=copy,
        stderr=stderr,
    )
    assert outcome.status == STATUS_ACCEPTED
    assert copied == ["improved v1"]
    assert COPY_SUCCESS_MESSAGE in stderr.getvalue()


def test_copy_after_iterate_uses_latest_improved():
    backend = FakeBackend(
        responses=[BackendResponse(text="improved v2", input_tokens=1, output_tokens=2)]
    )
    copy, copied = _make_copy_fn(success=True)

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["i", "c", "a"]),
        feedback_reader=_make_feedback_reader(["tweak"]),
        copy_fn=copy,
    )
    assert copied == ["improved v2"]


def test_copy_with_no_copy_fn_warns_non_fatally():
    """[c]opy + copy_fn=None: warn to stderr, run continues (P1-PLAT-07)."""
    backend = FakeBackend(responses=[])
    stderr = io.StringIO()

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["c", "a"]),
        copy_fn=None,
        stderr=stderr,
    )
    assert outcome.status == STATUS_ACCEPTED
    assert "no clipboard provider" in stderr.getvalue().lower()


def test_copy_failed_does_not_print_success():
    """copy_fn returning False: no success message; copy_fn already warned."""
    backend = FakeBackend(responses=[])
    copy, _ = _make_copy_fn(success=False)
    stderr = io.StringIO()

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["c", "a"]),
        copy_fn=copy,
        stderr=stderr,
    )
    assert COPY_SUCCESS_MESSAGE not in stderr.getvalue()


# ---------------------------------------------------------------------------
# AC #8 — --iterations N auto-iterates before choice line (P1-LOOP-08)
# ---------------------------------------------------------------------------


def test_auto_iterations_runs_n_backend_calls_before_choice():
    """N=2: two synthesized 'Improve this further.' rounds before the first choice."""
    backend = FakeBackend(
        responses=[
            BackendResponse(text="v2", input_tokens=1, output_tokens=2),
            BackendResponse(text="v3", input_tokens=3, output_tokens=4),
        ]
    )
    choice_reader = _make_choice_reader(["a"])

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        auto_iterations=2,
        choice_reader=choice_reader,
    )
    assert outcome.final_prompt == "v3"
    assert len(backend.calls) == 2
    # Synthesized feedback turns were used.
    assert outcome.messages[2] == {"role": "user", "content": SYNTHESIZED_FEEDBACK}
    assert outcome.messages[4] == {"role": "user", "content": SYNTHESIZED_FEEDBACK}
    assert len(outcome.new_turns) == 2


def test_auto_iterations_zero_skips_all_backend_calls():
    backend = FakeBackend(responses=[])
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        auto_iterations=0,
        choice_reader=_make_choice_reader(["a"]),
    )
    assert backend.calls == []
    assert outcome.final_prompt == "v1"


def test_auto_iterations_negative_treated_as_zero():
    """Defensive default: negative N is treated as 0 (no backend calls)."""
    backend = FakeBackend(responses=[])
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        auto_iterations=-5,
        choice_reader=_make_choice_reader(["a"]),
    )
    assert backend.calls == []
    assert outcome.final_prompt == "v1"


def test_auto_iterations_displays_only_once_before_choice():
    """The display callback fires once after the final auto-iteration."""
    backend = FakeBackend(
        responses=[
            BackendResponse(text="v2", input_tokens=1, output_tokens=1),
            BackendResponse(text="v3", input_tokens=1, output_tokens=1),
        ]
    )
    displays: list[tuple[str, str]] = []
    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        auto_iterations=2,
        choice_reader=_make_choice_reader(["a"]),
        display=lambda o, i: displays.append((o, i)),
    )
    assert displays == [("user prompt", "v3")]


def test_auto_iterations_then_iterate_continues_conversation():
    """User can iterate further after --iterations N completes."""
    backend = FakeBackend(
        responses=[
            BackendResponse(text="v2", input_tokens=1, output_tokens=1),
            BackendResponse(text="v3", input_tokens=1, output_tokens=1),
            BackendResponse(text="v4", input_tokens=1, output_tokens=1),
        ]
    )
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        auto_iterations=2,
        choice_reader=_make_choice_reader(["i", "a"]),
        feedback_reader=_make_feedback_reader(["one more"]),
    )
    assert outcome.final_prompt == "v4"
    assert len(backend.calls) == 3


# ---------------------------------------------------------------------------
# Defensive paths — unrecognized input, EOF, etc.
# ---------------------------------------------------------------------------


def test_unrecognized_choice_re_presents_prompt():
    """A typo'd choice falls through to a re-prompt without crashing."""
    backend = FakeBackend(responses=[])
    stderr = io.StringIO()
    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["q", "x", "?", "a"]),
        stderr=stderr,
    )
    # Choice line was written multiple times by the default reader path,
    # but we used a custom choice_reader so the choice prompt count
    # depends on the chosen seam. The fact that the test completed
    # without infinite-looping is the primary assertion.


def test_empty_choice_line_re_presents_prompt():
    """A bare Enter on the choice line re-prompts."""
    backend = FakeBackend(responses=[])
    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["\n", "a"]),
    )


def test_eof_on_choice_treated_as_discard():
    """A closed stdin (readline -> '') bails as discard, never infinite-loops."""
    backend = FakeBackend(responses=[])

    def eof_reader() -> str:
        return ""

    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        choice_reader=eof_reader,
    )
    assert outcome.status == STATUS_DISCARDED


def test_choice_matches_first_character_only():
    """'accept' is treated as 'a'; 'idiot' is treated as 'i'."""
    backend = FakeBackend(
        responses=[BackendResponse(text="v2", input_tokens=1, output_tokens=1)]
    )
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["iterate now", "accept yes"]),
        feedback_reader=_make_feedback_reader(["fb"]),
    )
    assert outcome.status == STATUS_ACCEPTED
    assert outcome.final_prompt == "v2"


def test_choice_is_case_insensitive():
    """'A' is accepted just like 'a'."""
    backend = FakeBackend(responses=[])
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["A"]),
    )
    assert outcome.status == STATUS_ACCEPTED


# ---------------------------------------------------------------------------
# Defensive copy guarantees — backend mutation cannot scramble our state
# ---------------------------------------------------------------------------


def test_backend_mutation_of_messages_does_not_corrupt_state():
    """A backend that mutates its messages arg cannot poison the loop's state."""

    class MutatingBackend(Backend):
        @property
        def name(self) -> str:
            return "mutator"

        def complete(
            self, system: str, messages: list[Message], stream: bool = False
        ) -> BackendResponse:
            messages.clear()  # ATTACK: try to wipe the loop's history
            messages.append({"role": "user", "content": "INJECTED"})
            return BackendResponse(text="v2", input_tokens=1, output_tokens=1)

        def check_auth(self) -> bool:
            return True

    initial = [
        {"role": "user", "content": "user prompt"},
        {"role": "assistant", "content": "v1"},
    ]
    outcome = run_refinement_loop(
        backend=MutatingBackend(),
        system="sys",
        initial_messages=initial,
        initial_improved="v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["i", "a"]),
        feedback_reader=_make_feedback_reader(["tweak"]),
    )
    # The injection didn't reach the outcome — our internal log is
    # intact (4 messages: original user, v1 assistant, tweak user,
    # v2 assistant).
    assert outcome.messages == (
        {"role": "user", "content": "user prompt"},
        {"role": "assistant", "content": "v1"},
        {"role": "user", "content": "tweak"},
        {"role": "assistant", "content": "v2"},
    )


def test_initial_messages_are_not_aliased():
    """Mutating the caller's initial_messages list after the loop doesn't change outcome.messages."""
    initial = _initial_messages()
    backend = FakeBackend(responses=[])
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=initial,
        initial_improved="v1",
        original_prompt="user prompt",
        choice_reader=_make_choice_reader(["a"]),
    )
    initial.clear()
    # outcome.messages survived the caller-side mutation.
    assert outcome.messages == (
        {"role": "user", "content": "user prompt"},
        {"role": "assistant", "content": "improved v1"},
    )


# ---------------------------------------------------------------------------
# Default I/O seam smoke tests (without monkeypatching sys)
# ---------------------------------------------------------------------------


def test_default_choice_reader_writes_prompt_to_stderr():
    """When no explicit choice_reader is passed, the default writes
    the canonical prompt to stderr before reading from stdin.
    """
    backend = FakeBackend(responses=[])
    stdin = io.StringIO("a\n")
    stderr = io.StringIO()

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        stdin=stdin,
        stderr=stderr,
    )
    assert CHOICE_PROMPT in stderr.getvalue()


def test_default_feedback_reader_writes_banner_to_stderr():
    """The default feedback reader emits the iterate banner to stderr."""
    backend = FakeBackend(
        responses=[BackendResponse(text="v2", input_tokens=1, output_tokens=1)]
    )
    stdin = io.StringIO("i\nfb\na\n")
    stderr = io.StringIO()

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="v1",
        original_prompt="user prompt",
        stdin=stdin,
        stderr=stderr,
    )
    assert ITERATE_FEEDBACK_PROMPT in stderr.getvalue()


def test_default_display_writes_improved_to_stderr():
    """The default display callback writes the improved text to stderr."""
    backend = FakeBackend(
        responses=[BackendResponse(text="improved v2", input_tokens=1, output_tokens=1)]
    )
    stdin = io.StringIO("i\nfb\na\n")
    stderr = io.StringIO()

    run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=_initial_messages(),
        initial_improved="improved v1",
        original_prompt="user prompt",
        stdin=stdin,
        stderr=stderr,
    )
    assert "improved v2" in stderr.getvalue()


def test_human_colon_in_user_prompt_survives_round_trip():
    """AC-MT-01 regression bait: a prompt containing 'Human:' must not be
    role-prefix-confused at the messages layer.
    """
    backend = FakeBackend(
        responses=[BackendResponse(text="ok", input_tokens=1, output_tokens=1)]
    )
    initial = [
        {"role": "user", "content": "Hi Human: do this"},
        {"role": "assistant", "content": "first response"},
    ]
    outcome = run_refinement_loop(
        backend=backend,
        system="sys",
        initial_messages=initial,
        initial_improved="first response",
        original_prompt="Hi Human: do this",
        choice_reader=_make_choice_reader(["a"]),
    )
    assert outcome.messages[0]["content"] == "Hi Human: do this"
