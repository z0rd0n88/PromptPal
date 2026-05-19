"""Unified diff display for original vs improved prompts (US-009, P1-PIPE-06).

The pipeline shows a unified diff when the improved prompt is **more than
3 lines**; shorter prompts are displayed in full instead, because a diff
adds more noise than signal for tiny refinements.

This module is pure: :func:`should_show_diff` and :func:`format_diff` are
side-effect-free and return their result as values. The CLI layer is
responsible for writing the diff to ``stderr`` (P1-PIPE-09 — stdout is
reserved for the improved prompt; all diff chrome goes to stderr).
"""

from __future__ import annotations

import difflib


DIFF_LINE_THRESHOLD: int = 3

DEFAULT_FROMFILE: str = "original"
DEFAULT_TOFILE: str = "improved"


def line_count(text: str) -> int:
    """Return the number of lines in ``text``.

    Uses :py:meth:`str.splitlines`, which treats a missing trailing newline
    the same as a present one (``"a\\nb"`` and ``"a\\nb\\n"`` both have
    two lines). The empty string has zero lines.
    """
    return len(text.splitlines())


def should_show_diff(improved: str) -> bool:
    """Return ``True`` iff the improved prompt has more than 3 lines.

    Matches AC #4 — "Unified diff displayed between original and improved
    prompt when improved length > 3 lines" (P1-PIPE-06).
    """
    return line_count(improved) > DIFF_LINE_THRESHOLD


def format_diff(
    original: str,
    improved: str,
    *,
    fromfile: str = DEFAULT_FROMFILE,
    tofile: str = DEFAULT_TOFILE,
) -> str:
    """Return a unified diff between ``original`` and ``improved``.

    Uses :func:`difflib.unified_diff` with ``lineterm=""`` so the joined
    string is newline-terminated cleanly without double newlines. When
    the two inputs are identical, the result is the empty string (the
    caller may still choose to print a header).
    """
    original_lines = original.splitlines()
    improved_lines = improved.splitlines()
    diff_lines = difflib.unified_diff(
        original_lines,
        improved_lines,
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
    )
    return "\n".join(diff_lines)
