"""Integration tests for core/input.py (US-009, P1-PIPE-01..03).

Coverage map (1 test cluster → 1 acceptance criterion):

  AC #1   positional arg path                          → test_read_prompt_positional_*
  AC #2   stdin pipe when stdin is NOT a TTY          → test_read_prompt_stdin_*
  AC #3   interactive TTY input when no arg + TTY     → test_read_prompt_interactive_*

The module is constructed with explicit ``stdin`` / ``stderr`` / ``isatty``
test seams so each path is exercised with an io.StringIO without
monkeypatching ``sys`` globals (which would leak across tests).
"""

from __future__ import annotations

import io

import pytest

from core.input import (
    EMPTY_PROMPT_MESSAGE,
    INTERACTIVE_PROMPT_BANNER,
    EmptyPromptError,
    read_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdin(text: str) -> io.StringIO:
    return io.StringIO(text)


def _stderr() -> io.StringIO:
    return io.StringIO()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_empty_prompt_message_is_actionable():
    """The canonical empty-prompt message names all three input modes."""
    assert "argument" in EMPTY_PROMPT_MESSAGE
    assert "stdin" in EMPTY_PROMPT_MESSAGE
    assert "interactive" in EMPTY_PROMPT_MESSAGE


def test_interactive_banner_ends_with_newline():
    """The banner is a complete line so the user's first keystroke lands fresh."""
    assert INTERACTIVE_PROMPT_BANNER.endswith("\n")


# ---------------------------------------------------------------------------
# AC #1 — positional arg path
# ---------------------------------------------------------------------------


def test_read_prompt_positional_arg_returned_verbatim():
    result = read_prompt(
        "Refine this sentence please.",
        stdin=_stdin("this should be ignored"),
        stderr=_stderr(),
        isatty=lambda: True,
    )
    assert result == "Refine this sentence please."


def test_read_prompt_positional_arg_preserves_human_literal():
    """AC-MT-01 regression bait — 'Human:' must round-trip byte-identical."""
    payload = "Human: write a story\nAssistant: ..."
    assert read_prompt(payload, stdin=_stdin(""), stderr=_stderr(),
                       isatty=lambda: True) == payload


def test_read_prompt_positional_arg_does_not_strip_trailing_newline():
    """A user-supplied positional arg keeps its bytes — no trim."""
    payload = "with trailing newline\n"
    assert read_prompt(payload, stdin=_stdin(""), stderr=_stderr(),
                       isatty=lambda: True) == payload


def test_read_prompt_positional_arg_with_whitespace_only_raises():
    """An explicit empty/whitespace-only arg surfaces as an actionable error,
    NOT a silent fallthrough to stdin."""
    with pytest.raises(EmptyPromptError) as exc:
        read_prompt(
            "   \n\t  ",
            stdin=_stdin("would have read this"),
            stderr=_stderr(),
            isatty=lambda: False,
        )
    assert str(exc.value) == EMPTY_PROMPT_MESSAGE


def test_read_prompt_positional_arg_empty_string_raises():
    with pytest.raises(EmptyPromptError):
        read_prompt("", stdin=_stdin("ignored"), stderr=_stderr(),
                    isatty=lambda: False)


def test_read_prompt_positional_arg_does_not_print_banner():
    """No interactive banner should appear when an arg is provided."""
    stderr = _stderr()
    read_prompt("some prompt", stdin=_stdin(""), stderr=stderr,
                isatty=lambda: True)
    assert stderr.getvalue() == ""


# ---------------------------------------------------------------------------
# AC #2 — stdin pipe (non-TTY)
# ---------------------------------------------------------------------------


def test_read_prompt_stdin_pipe_reads_full_payload():
    """``echo "prompt" | promptpal`` lands the piped bytes verbatim."""
    payload = "prompt from stdin\n"
    result = read_prompt(
        None,
        stdin=_stdin(payload),
        stderr=_stderr(),
        isatty=lambda: False,
    )
    assert result == payload


def test_read_prompt_stdin_pipe_multi_line():
    payload = "line one\nline two\nline three\n"
    assert (
        read_prompt(None, stdin=_stdin(payload), stderr=_stderr(),
                    isatty=lambda: False)
        == payload
    )


def test_read_prompt_stdin_pipe_preserves_utf8():
    """UTF-8 round-trip — emoji and CJK must survive the read."""
    payload = "Rocket 🚀 and 你好\n"
    assert (
        read_prompt(None, stdin=_stdin(payload), stderr=_stderr(),
                    isatty=lambda: False)
        == payload
    )


def test_read_prompt_stdin_pipe_does_not_print_banner():
    """No banner when stdin is piped — the user isn't sitting at the keyboard."""
    stderr = _stderr()
    read_prompt(None, stdin=_stdin("hi"), stderr=stderr,
                isatty=lambda: False)
    assert stderr.getvalue() == ""


def test_read_prompt_stdin_empty_pipe_raises():
    """A truly empty pipe surfaces as an actionable error."""
    with pytest.raises(EmptyPromptError) as exc:
        read_prompt(None, stdin=_stdin(""), stderr=_stderr(),
                    isatty=lambda: False)
    assert str(exc.value) == EMPTY_PROMPT_MESSAGE


def test_read_prompt_stdin_whitespace_only_raises():
    with pytest.raises(EmptyPromptError):
        read_prompt(None, stdin=_stdin("   \n\t\n"), stderr=_stderr(),
                    isatty=lambda: False)


# ---------------------------------------------------------------------------
# AC #3 — interactive TTY path
# ---------------------------------------------------------------------------


def test_read_prompt_interactive_prints_banner_to_stderr():
    """The interactive banner lands on stderr — never stdout (P1-PIPE-09)."""
    stderr = _stderr()
    read_prompt(
        None,
        stdin=_stdin("interactive prompt body\n"),
        stderr=stderr,
        isatty=lambda: True,
    )
    assert stderr.getvalue() == INTERACTIVE_PROMPT_BANNER


def test_read_prompt_interactive_reads_until_eof():
    """Interactive read consumes the full stdin payload (Ctrl-D semantics)."""
    payload = "line a\nline b\nline c"
    result = read_prompt(
        None,
        stdin=_stdin(payload),
        stderr=_stderr(),
        isatty=lambda: True,
    )
    assert result == payload


def test_read_prompt_interactive_empty_input_raises():
    with pytest.raises(EmptyPromptError):
        read_prompt(None, stdin=_stdin(""), stderr=_stderr(),
                    isatty=lambda: True)


def test_read_prompt_interactive_default_isatty_uses_stdin_isatty():
    """When ``isatty`` is omitted, the helper falls back to ``stdin.isatty``."""
    class FakeStdin(io.StringIO):
        def __init__(self, text: str, tty: bool) -> None:
            super().__init__(text)
            self._tty = tty

        def isatty(self) -> bool:  # type: ignore[override]
            return self._tty

    stderr = _stderr()
    fake = FakeStdin("hello\n", tty=True)
    result = read_prompt(None, stdin=fake, stderr=stderr)
    assert result == "hello\n"
    assert stderr.getvalue() == INTERACTIVE_PROMPT_BANNER


def test_read_prompt_interactive_default_isatty_pipe_path():
    """When stdin.isatty() is False, no banner is printed."""
    class FakeStdin(io.StringIO):
        def isatty(self) -> bool:  # type: ignore[override]
            return False

    stderr = _stderr()
    fake = FakeStdin("piped payload\n")
    result = read_prompt(None, stdin=fake, stderr=stderr)
    assert result == "piped payload\n"
    assert stderr.getvalue() == ""
