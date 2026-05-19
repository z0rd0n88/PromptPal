"""Tests for core/diff.py (US-009, P1-PIPE-06).

Coverage map (1 test cluster → 1 acceptance criterion or sub-rule):

  AC #4   short prompt (<= 3 lines) → no diff                     → test_should_show_diff_short_*
          long prompt (> 3 lines)   → unified diff                → test_should_show_diff_long_*
          unified diff format (---, +++, @@ markers)              → test_format_diff_*

All assertions pin behavior against an injected pair of strings; the
module is pure so no monkeypatching is required.
"""

from __future__ import annotations

import pytest

from core.diff import (
    DEFAULT_FROMFILE,
    DEFAULT_TOFILE,
    DIFF_LINE_THRESHOLD,
    format_diff,
    line_count,
    should_show_diff,
)


# ---------------------------------------------------------------------------
# Constants / module surface
# ---------------------------------------------------------------------------


def test_diff_threshold_is_3():
    """The threshold constant is exactly 3 — P1-PIPE-06 says '> 3 lines'."""
    assert DIFF_LINE_THRESHOLD == 3


def test_default_filenames_are_stable():
    """Header filenames are 'original' and 'improved' — pinned for the UI."""
    assert DEFAULT_FROMFILE == "original"
    assert DEFAULT_TOFILE == "improved"


# ---------------------------------------------------------------------------
# line_count
# ---------------------------------------------------------------------------


def test_line_count_empty():
    assert line_count("") == 0


def test_line_count_single_line_no_trailing_newline():
    assert line_count("hello") == 1


def test_line_count_single_line_with_trailing_newline():
    assert line_count("hello\n") == 1


def test_line_count_two_lines():
    assert line_count("a\nb") == 2
    assert line_count("a\nb\n") == 2


def test_line_count_three_lines():
    assert line_count("a\nb\nc") == 3
    assert line_count("a\nb\nc\n") == 3


def test_line_count_four_lines():
    assert line_count("a\nb\nc\nd") == 4
    assert line_count("a\nb\nc\nd\n") == 4


# ---------------------------------------------------------------------------
# should_show_diff — AC #4 boundary
# ---------------------------------------------------------------------------


def test_should_show_diff_empty_is_false():
    assert should_show_diff("") is False


def test_should_show_diff_one_line_is_false():
    assert should_show_diff("only one line") is False


def test_should_show_diff_two_lines_is_false():
    assert should_show_diff("line1\nline2") is False


def test_should_show_diff_exactly_three_lines_is_false():
    """Boundary — '> 3 lines' means 3 is NOT enough."""
    assert should_show_diff("line1\nline2\nline3") is False
    assert should_show_diff("line1\nline2\nline3\n") is False


def test_should_show_diff_four_lines_is_true():
    """Boundary — 4 lines crosses the '> 3 lines' threshold."""
    assert should_show_diff("line1\nline2\nline3\nline4") is True
    assert should_show_diff("line1\nline2\nline3\nline4\n") is True


def test_should_show_diff_many_lines_is_true():
    long = "\n".join(f"line{n}" for n in range(50))
    assert should_show_diff(long) is True


# ---------------------------------------------------------------------------
# format_diff — output shape
# ---------------------------------------------------------------------------


def test_format_diff_identical_inputs_returns_empty_string():
    """unified_diff yields nothing when the two sequences are equal."""
    text = "alpha\nbeta\ngamma\ndelta"
    assert format_diff(text, text) == ""


def test_format_diff_contains_unified_diff_markers():
    """Output carries the canonical '---' / '+++' / '@@' marker triad."""
    original = "alpha\nbeta\ngamma\ndelta"
    improved = "alpha\nBETA\ngamma\ndelta"
    diff = format_diff(original, improved)
    assert diff.startswith("--- original")
    assert "\n+++ improved" in diff
    assert "@@" in diff


def test_format_diff_marks_removed_and_added_lines():
    original = "keep\nremove\nkeep"
    improved = "keep\nadded\nkeep"
    diff = format_diff(original, improved)
    assert "-remove" in diff
    assert "+added" in diff


def test_format_diff_custom_filenames_are_used():
    diff = format_diff(
        "a\nb",
        "a\nc",
        fromfile="prompt_v1.md",
        tofile="prompt_v2.md",
    )
    assert "--- prompt_v1.md" in diff
    assert "+++ prompt_v2.md" in diff


def test_format_diff_no_blank_double_newlines():
    """``lineterm=""`` keeps the joined output free of empty intermediate lines."""
    original = "alpha\nbeta\ngamma\ndelta"
    improved = "alpha\nbeta\nGAMMA\ndelta"
    diff = format_diff(original, improved)
    assert "\n\n" not in diff


def test_format_diff_empty_original_against_text():
    """Inserting content into an empty original is a pure-add diff."""
    diff = format_diff("", "added\nlines")
    assert "+added" in diff
    assert "+lines" in diff


def test_format_diff_text_against_empty_improved():
    """Erasing everything is a pure-remove diff."""
    diff = format_diff("gone\nlines", "")
    assert "-gone" in diff
    assert "-lines" in diff


@pytest.mark.parametrize(
    "improved",
    [
        "one liner",
        "two\nlines",
        "three\nshort\nlines",
        "three\nshort\nlines\n",
    ],
)
def test_should_show_diff_false_for_three_lines_or_fewer(improved: str):
    assert should_show_diff(improved) is False


@pytest.mark.parametrize(
    "improved",
    [
        "a\nb\nc\nd",
        "a\nb\nc\nd\ne",
        "one\ntwo\nthree\nfour\nfive\n",
    ],
)
def test_should_show_diff_true_for_more_than_three_lines(improved: str):
    assert should_show_diff(improved) is True
