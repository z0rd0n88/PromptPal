"""Tests for ``core.history`` — US-008 / P1-HIST-01..08.

Coverage map:

- AC #1 / P1-HIST-01 — one JSON file per session at ``<uuid>.json``
- AC #2 / P1-HIST-02 — index.json with session_id, created_at, label,
  status, original_prompt_preview
- AC #3 / P1-HIST-03 — atomic tempfile.mkstemp -> os.replace; cleanup
- AC #4 / P1-HIST-04 — incremental writes during ``in-progress``;
  finalize to ``accepted`` / ``discarded``
- AC #5 / P1-HIST-05 — append-only NDJSON usage log; null token counts
- AC #6 / P1-HIST-06 — eviction of oldest past ``max_history_entries``
- AC #7 / P1-HIST-08 — non-fatal warning helper

The clock and id factory are injected through every helper that
otherwise calls into ``datetime.now`` / ``uuid.uuid4`` so the tests pin
both fields deterministically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import history as h
from core._io import atomic_write_bytes
from core.history import (
    HISTORY_WRITE_WARNING,
    INDEX_FILE_NAME,
    AmbiguousSessionIdError,
    IndexEntry,
    ORIGINAL_PROMPT_PREVIEW_LIMIT,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_IN_PROGRESS,
    Session,
    SessionNotFoundError,
    Turn,
    USAGE_LOG_NAME,
    append_turn,
    append_usage_entry,
    enforce_max_entries,
    finalize_session,
    index_entry_from_session,
    index_path,
    new_session,
    read_index,
    read_session,
    resolve_session_id,
    search_history,
    session_path,
    upsert_index_entry,
    warn_history_failure,
    write_index,
    write_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixed_clock(value: str):
    return lambda: value


def _seq_clock(values: list[str]):
    """Returns a clock callable that pops one value per call."""
    remaining = list(values)

    def _clock() -> str:
        return remaining.pop(0)

    return _clock


def _fixed_id(value: str):
    return lambda: value


def _make_session(
    *,
    session_id: str = "abc",
    created_at: str = "2026-05-19T12:00:00Z",
    original_prompt: str = "Make it better.",
    model: str = "claude-sonnet-4-6",
    backend: str = "claude-cli",
    label: str | None = None,
) -> Session:
    return new_session(
        original_prompt=original_prompt,
        model=model,
        backend=backend,
        label=label,
        clock=_fixed_clock(created_at),
        id_factory=_fixed_id(session_id),
    )


# ---------------------------------------------------------------------------
# Session factory + immutable updates
# ---------------------------------------------------------------------------


def test_new_session_starts_in_progress_with_no_turns():
    session = _make_session()
    assert session.session_id == "abc"
    assert session.status == STATUS_IN_PROGRESS
    assert session.turns == ()
    assert session.final_prompt is None
    assert session.created_at == session.updated_at


def test_new_session_uses_default_factories_when_not_injected():
    s1 = new_session(
        original_prompt="p", model="m", backend="api-key"
    )
    s2 = new_session(
        original_prompt="p", model="m", backend="api-key"
    )
    assert s1.session_id != s2.session_id  # uuid.uuid4 fresh each call
    assert s1.created_at  # ISO timestamp populated
    # Default UUIDs are 32-char hex.
    assert len(s1.session_id) == 32
    assert all(c in "0123456789abcdef" for c in s1.session_id)


def test_new_session_carries_label_through():
    session = _make_session(label="refactor")
    assert session.label == "refactor"


def test_append_turn_is_immutable_and_updates_timestamp():
    base = _make_session(created_at="2026-05-19T12:00:00Z")
    updated = append_turn(
        base,
        role="user",
        content="hi",
        backend="claude-cli",
        input_tokens=None,
        output_tokens=None,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    assert base.turns == ()
    assert len(updated.turns) == 1
    turn = updated.turns[0]
    assert turn.role == "user"
    assert turn.content == "hi"
    assert turn.backend == "claude-cli"
    assert turn.input_tokens is None
    assert turn.output_tokens is None
    assert turn.timestamp == "2026-05-19T12:00:30Z"
    assert updated.updated_at == "2026-05-19T12:00:30Z"
    assert updated.created_at == base.created_at  # unchanged


def test_append_turn_carries_numeric_tokens_for_api():
    base = _make_session(backend="api-key")
    updated = append_turn(
        base,
        role="assistant",
        content="response",
        backend="api-key",
        input_tokens=42,
        output_tokens=17,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    turn = updated.turns[0]
    assert turn.input_tokens == 42
    assert turn.output_tokens == 17


def test_finalize_session_sets_status_and_final_prompt():
    base = _make_session()
    accepted = finalize_session(
        base,
        status=STATUS_ACCEPTED,
        final_prompt="improved",
        clock=_fixed_clock("2026-05-19T12:05:00Z"),
    )
    assert accepted.status == STATUS_ACCEPTED
    assert accepted.final_prompt == "improved"
    assert accepted.updated_at == "2026-05-19T12:05:00Z"


def test_finalize_session_discard_does_not_require_final_prompt():
    base = _make_session()
    discarded = finalize_session(base, status=STATUS_DISCARDED)
    assert discarded.status == STATUS_DISCARDED
    assert discarded.final_prompt is None


def test_finalize_session_rejects_in_progress_status():
    base = _make_session()
    with pytest.raises(ValueError, match="accepted|discarded"):
        finalize_session(base, status=STATUS_IN_PROGRESS)


def test_finalize_session_rejects_unknown_status():
    base = _make_session()
    with pytest.raises(ValueError):
        finalize_session(base, status="weird")


def test_turn_dict_roundtrip():
    original = Turn(
        role="user",
        content="hi",
        backend="claude-cli",
        input_tokens=None,
        output_tokens=None,
        timestamp="2026-05-19T12:00:30Z",
    )
    restored = Turn.from_dict(original.to_dict())
    assert restored == original


def test_session_dict_roundtrip_preserves_all_fields():
    base = _make_session(label="lbl")
    with_turn = append_turn(
        base,
        role="user",
        content="hi",
        backend="claude-cli",
        input_tokens=None,
        output_tokens=None,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    finished = finalize_session(
        with_turn,
        status=STATUS_ACCEPTED,
        final_prompt="done",
        clock=_fixed_clock("2026-05-19T12:01:00Z"),
    )
    restored = Session.from_dict(finished.to_dict())
    assert restored == finished


# ---------------------------------------------------------------------------
# Session I/O (P1-HIST-01, P1-HIST-03, P1-HIST-04)
# ---------------------------------------------------------------------------


def test_write_session_creates_uuid_named_file(tmp_path):
    session = _make_session(session_id="deadbeef")
    target = write_session(session, tmp_path)
    assert target == tmp_path / "deadbeef.json"
    assert target.is_file()


def test_write_session_uses_atomic_write_no_temp_leftovers(tmp_path):
    """AC #3 / P1-HIST-03 — no .session-* tempfiles after a clean write."""
    session = _make_session(session_id="deadbeef")
    write_session(session, tmp_path)
    leftovers = [
        p for p in tmp_path.iterdir() if p.name.startswith(".session-")
    ]
    assert leftovers == [], f"unexpected leftovers: {leftovers}"


def test_write_session_atomic_goes_through_os_replace(tmp_path, monkeypatch):
    """tempfile.mkstemp + os.replace; tempfile co-located with target."""
    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def tracking_replace(src, dst, *args, **kwargs):
        replace_calls.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("core._io.os.replace", tracking_replace)
    session = _make_session(session_id="deadbeef")
    write_session(session, tmp_path)
    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert Path(src).parent == tmp_path
    assert Path(dst) == tmp_path / "deadbeef.json"


def test_write_session_cleans_up_tempfile_on_failure(tmp_path, monkeypatch):
    """When os.replace raises, the tempfile is unlinked (no leak)."""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core._io.os.replace", boom)

    session = _make_session(session_id="deadbeef")
    with pytest.raises(OSError):
        write_session(session, tmp_path)
    leftovers = [
        p for p in tmp_path.iterdir() if p.name.startswith(".session-")
    ]
    assert leftovers == []


def test_write_session_creates_parent_directory(tmp_path):
    """First-run callers don't have to pre-create history/."""
    nested = tmp_path / "nested" / "history"
    session = _make_session(session_id="deadbeef")
    write_session(session, nested)
    assert (nested / "deadbeef.json").is_file()


def test_write_session_writes_utf8_lf_with_trailing_newline(tmp_path):
    session = _make_session(session_id="deadbeef")
    target = write_session(session, tmp_path)
    raw = target.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw


def test_write_session_records_full_schema(tmp_path):
    session = _make_session(session_id="deadbeef", label="lbl")
    with_turn = append_turn(
        session,
        role="user",
        content="hi",
        backend="claude-cli",
        input_tokens=None,
        output_tokens=None,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    target = write_session(with_turn, tmp_path)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["session_id"] == "deadbeef"
    assert data["status"] == STATUS_IN_PROGRESS
    assert data["label"] == "lbl"
    assert data["original_prompt"] == "Make it better."
    assert data["model"] == "claude-sonnet-4-6"
    assert data["backend"] == "claude-cli"
    assert len(data["turns"]) == 1
    assert data["turns"][0]["input_tokens"] is None
    assert data["turns"][0]["output_tokens"] is None
    assert data["final_prompt"] is None


def test_write_session_preserves_unicode_bytes(tmp_path):
    """ensure_ascii=False so emoji ride as raw UTF-8."""
    session = _make_session(
        session_id="deadbeef", original_prompt="🚀 Greek: ωωω, CJK: 测试"
    )
    target = write_session(session, tmp_path)
    raw = target.read_bytes()
    assert "🚀".encode("utf-8") in raw
    assert "测试".encode("utf-8") in raw


def test_write_session_overwrites_in_place_for_incremental_updates(tmp_path):
    """AC #4 / P1-HIST-04 — same path used for every turn write."""
    session = _make_session(session_id="deadbeef")
    write_session(session, tmp_path)
    updated = append_turn(
        session,
        role="user",
        content="hi",
        backend="claude-cli",
        input_tokens=None,
        output_tokens=None,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    target = write_session(updated, tmp_path)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data["turns"]) == 1
    files = [p for p in tmp_path.iterdir() if p.suffix == ".json"]
    assert len(files) == 1  # no proliferation of files


def test_read_session_roundtrip(tmp_path):
    session = _make_session(session_id="deadbeef")
    with_turn = append_turn(
        session,
        role="user",
        content="hi",
        backend="claude-cli",
        input_tokens=None,
        output_tokens=None,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    write_session(with_turn, tmp_path)
    restored = read_session("deadbeef", tmp_path)
    assert restored == with_turn


def test_read_session_missing_raises_session_not_found(tmp_path):
    with pytest.raises(SessionNotFoundError):
        read_session("nonexistent", tmp_path)


# ---------------------------------------------------------------------------
# resolve_session_id — git-style full-id-or-unique-prefix lookup
# ---------------------------------------------------------------------------


def _seed_session(tmp_path, session_id: str) -> None:
    write_session(_make_session(session_id=session_id), tmp_path)


def test_resolve_exact_full_id_returns_it(tmp_path):
    _seed_session(tmp_path, "b4449351af81440ca0f1e7085dadf89d")
    assert (
        resolve_session_id("b4449351af81440ca0f1e7085dadf89d", tmp_path)
        == "b4449351af81440ca0f1e7085dadf89d"
    )


def test_resolve_unique_prefix_returns_full_id(tmp_path):
    """The 8-char id shown by --show-history resolves to the full id."""
    _seed_session(tmp_path, "b4449351af81440ca0f1e7085dadf89d")
    _seed_session(tmp_path, "c7ff0fc895cc4bc3b5cf360a2edcafaf")
    assert (
        resolve_session_id("b4449351", tmp_path)
        == "b4449351af81440ca0f1e7085dadf89d"
    )


def test_resolve_no_match_raises_not_found(tmp_path):
    _seed_session(tmp_path, "b4449351af81440ca0f1e7085dadf89d")
    with pytest.raises(SessionNotFoundError):
        resolve_session_id("ffffffff", tmp_path)


def test_resolve_ambiguous_prefix_raises(tmp_path):
    _seed_session(tmp_path, "abcd1111111111111111111111111111")
    _seed_session(tmp_path, "abcd2222222222222222222222222222")
    with pytest.raises(AmbiguousSessionIdError) as exc:
        resolve_session_id("abcd", tmp_path)
    assert exc.value.prefix == "abcd"
    assert len(exc.value.matches) == 2
    assert exc.value.matches == [
        "abcd1111111111111111111111111111",
        "abcd2222222222222222222222222222",
    ]


def test_resolve_empty_string_is_not_found_not_match_all(tmp_path):
    _seed_session(tmp_path, "b4449351af81440ca0f1e7085dadf89d")
    with pytest.raises(SessionNotFoundError):
        resolve_session_id("", tmp_path)


def test_resolve_ignores_index_json(tmp_path):
    """A prefix that would match index.json's stem must not resolve to it."""
    _seed_session(tmp_path, "abc12345")
    # index.json sits alongside session files; its stem is "index". The
    # resolver must skip it (resolve only globs filenames, never parses it).
    (tmp_path / INDEX_FILE_NAME).write_text("[]", encoding="utf-8")
    with pytest.raises(SessionNotFoundError):
        resolve_session_id("index", tmp_path)


def test_session_path_helper_uses_uuid_naming(tmp_path):
    assert session_path(tmp_path, "abc") == tmp_path / "abc.json"


# ---------------------------------------------------------------------------
# Index I/O (P1-HIST-02)
# ---------------------------------------------------------------------------


def test_index_path_returns_index_json(tmp_path):
    assert index_path(tmp_path) == tmp_path / INDEX_FILE_NAME


def test_index_entry_from_session_carries_only_index_fields():
    session = _make_session(session_id="abc", label="lbl")
    entry = index_entry_from_session(session)
    assert entry.session_id == "abc"
    assert entry.created_at == session.created_at
    assert entry.label == "lbl"
    assert entry.status == STATUS_IN_PROGRESS
    assert entry.original_prompt_preview == "Make it better."


def test_index_entry_preview_is_truncated_with_no_ellipsis():
    text = "x" * (ORIGINAL_PROMPT_PREVIEW_LIMIT + 20)
    session = _make_session(original_prompt=text)
    entry = index_entry_from_session(session)
    assert entry.original_prompt_preview == "x" * ORIGINAL_PROMPT_PREVIEW_LIMIT
    assert entry.original_prompt_preview == text[:ORIGINAL_PROMPT_PREVIEW_LIMIT]


def test_index_entry_dict_roundtrip():
    entry = IndexEntry(
        session_id="abc",
        created_at="2026-05-19T12:00:00Z",
        label="lbl",
        status=STATUS_IN_PROGRESS,
        original_prompt_preview="preview",
    )
    restored = IndexEntry.from_dict(entry.to_dict())
    assert restored == entry


def test_read_index_missing_file_returns_empty_list(tmp_path):
    assert read_index(tmp_path) == []


def test_read_index_tolerates_non_list_root(tmp_path):
    """A corrupt index that's a dict shouldn't crash startup."""
    (tmp_path / INDEX_FILE_NAME).write_text("{}", encoding="utf-8")
    assert read_index(tmp_path) == []


def test_write_index_atomic_no_temp_leftovers(tmp_path):
    entry = IndexEntry(
        session_id="abc",
        created_at="2026-05-19T12:00:00Z",
        label=None,
        status=STATUS_IN_PROGRESS,
        original_prompt_preview="p",
    )
    write_index([entry], tmp_path)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".index-")]
    assert leftovers == []


def test_write_index_cleans_up_tempfile_on_failure(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core._io.os.replace", boom)
    entry = IndexEntry(
        session_id="abc",
        created_at="2026-05-19T12:00:00Z",
        label=None,
        status=STATUS_IN_PROGRESS,
        original_prompt_preview="p",
    )
    with pytest.raises(OSError):
        write_index([entry], tmp_path)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".index-")]
    assert leftovers == []


def test_upsert_index_entry_inserts_new(tmp_path):
    entry = IndexEntry(
        session_id="abc",
        created_at="2026-05-19T12:00:00Z",
        label=None,
        status=STATUS_IN_PROGRESS,
        original_prompt_preview="p",
    )
    upsert_index_entry(tmp_path, entry)
    assert read_index(tmp_path) == [entry]


def test_upsert_index_entry_updates_existing_in_place(tmp_path):
    initial = IndexEntry(
        session_id="abc",
        created_at="2026-05-19T12:00:00Z",
        label=None,
        status=STATUS_IN_PROGRESS,
        original_prompt_preview="p",
    )
    upsert_index_entry(tmp_path, initial)
    updated = IndexEntry(
        session_id="abc",
        created_at="2026-05-19T12:00:00Z",
        label="accepted-label",
        status=STATUS_ACCEPTED,
        original_prompt_preview="p",
    )
    upsert_index_entry(tmp_path, updated)
    rows = read_index(tmp_path)
    assert len(rows) == 1
    assert rows[0].label == "accepted-label"
    assert rows[0].status == STATUS_ACCEPTED


def test_upsert_index_entry_sorts_newest_first(tmp_path):
    older = IndexEntry(
        session_id="old",
        created_at="2026-05-18T12:00:00Z",
        label=None,
        status=STATUS_ACCEPTED,
        original_prompt_preview="o",
    )
    newer = IndexEntry(
        session_id="new",
        created_at="2026-05-19T12:00:00Z",
        label=None,
        status=STATUS_IN_PROGRESS,
        original_prompt_preview="n",
    )
    upsert_index_entry(tmp_path, older)
    upsert_index_entry(tmp_path, newer)
    rows = read_index(tmp_path)
    assert [r.session_id for r in rows] == ["new", "old"]


# ---------------------------------------------------------------------------
# Eviction (P1-HIST-06)
# ---------------------------------------------------------------------------


def _populate_history(tmp_path, count: int) -> list[str]:
    """Write ``count`` sessions and matching index entries.

    Returns the session_ids in creation order (oldest first).
    """
    ids: list[str] = []
    for i in range(count):
        sid = f"s{i:03d}"
        session = _make_session(
            session_id=sid,
            created_at=f"2026-05-{19 + i:02d}T12:00:00Z",
        )
        write_session(session, tmp_path)
        upsert_index_entry(tmp_path, index_entry_from_session(session))
        ids.append(sid)
    return ids


def test_enforce_max_entries_noop_when_under_limit(tmp_path):
    _populate_history(tmp_path, 3)
    evicted = enforce_max_entries(tmp_path, max_entries=10)
    assert evicted == []
    assert len(read_index(tmp_path)) == 3


def test_enforce_max_entries_evicts_oldest_only(tmp_path):
    ids = _populate_history(tmp_path, 5)
    evicted = enforce_max_entries(tmp_path, max_entries=3)
    # ids list is oldest-first; first two should be evicted.
    assert set(evicted) == {ids[0], ids[1]}
    kept_ids = {row.session_id for row in read_index(tmp_path)}
    assert kept_ids == {ids[2], ids[3], ids[4]}


def test_enforce_max_entries_deletes_session_files(tmp_path):
    ids = _populate_history(tmp_path, 5)
    enforce_max_entries(tmp_path, max_entries=3)
    for evicted_id in ids[:2]:
        assert not session_path(tmp_path, evicted_id).exists()
    for kept_id in ids[2:]:
        assert session_path(tmp_path, kept_id).exists()


def test_enforce_max_entries_tolerates_missing_session_file(tmp_path):
    """Eviction must be idempotent — a half-evicted state can recover."""
    ids = _populate_history(tmp_path, 5)
    # Pre-delete one session file so eviction must tolerate the miss.
    session_path(tmp_path, ids[0]).unlink()
    evicted = enforce_max_entries(tmp_path, max_entries=3)
    assert set(evicted) == {ids[0], ids[1]}
    assert not session_path(tmp_path, ids[1]).exists()


def test_enforce_max_entries_negative_is_unlimited(tmp_path):
    """A negative limit must not wipe the history."""
    _populate_history(tmp_path, 3)
    evicted = enforce_max_entries(tmp_path, max_entries=-1)
    assert evicted == []
    assert len(read_index(tmp_path)) == 3


def test_enforce_max_entries_zero_evicts_all(tmp_path):
    ids = _populate_history(tmp_path, 3)
    evicted = enforce_max_entries(tmp_path, max_entries=0)
    assert set(evicted) == set(ids)
    assert read_index(tmp_path) == []


# ---------------------------------------------------------------------------
# Usage log (P1-HIST-05)
# ---------------------------------------------------------------------------


def test_append_usage_entry_creates_ndjson_line(tmp_path):
    log = tmp_path / USAGE_LOG_NAME
    append_usage_entry(
        log,
        session_id="abc",
        turn_index=0,
        backend="claude-cli",
        model="claude-sonnet-4-6",
        input_tokens=None,
        output_tokens=None,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        "timestamp": "2026-05-19T12:00:30Z",
        "session_id": "abc",
        "turn_index": 0,
        "backend": "claude-cli",
        "model": "claude-sonnet-4-6",
        "input_tokens": None,
        "output_tokens": None,
    }


def test_append_usage_entry_appends_one_line_per_call(tmp_path):
    log = tmp_path / USAGE_LOG_NAME
    for i in range(3):
        append_usage_entry(
            log,
            session_id="abc",
            turn_index=i,
            backend="api-key",
            model="claude-sonnet-4-6",
            input_tokens=100 + i,
            output_tokens=200 + i,
            clock=_fixed_clock(f"2026-05-19T12:00:{30 + i:02d}Z"),
        )
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for i, line in enumerate(lines):
        record = json.loads(line)
        assert record["turn_index"] == i
        assert record["input_tokens"] == 100 + i


def test_append_usage_entry_creates_parent_directory(tmp_path):
    log = tmp_path / "nested" / "subdir" / USAGE_LOG_NAME
    append_usage_entry(
        log,
        session_id="abc",
        turn_index=0,
        backend="api-key",
        model="m",
        input_tokens=1,
        output_tokens=1,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    assert log.is_file()


def test_append_usage_entry_emits_utf8_lf(tmp_path):
    log = tmp_path / USAGE_LOG_NAME
    append_usage_entry(
        log,
        session_id="abc",
        turn_index=0,
        backend="claude-cli",
        model="m",
        input_tokens=None,
        output_tokens=None,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    raw = log.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw


def test_append_usage_entry_preserves_unicode(tmp_path):
    log = tmp_path / USAGE_LOG_NAME
    append_usage_entry(
        log,
        session_id="🚀sid",
        turn_index=0,
        backend="claude-cli",
        model="m",
        input_tokens=None,
        output_tokens=None,
        clock=_fixed_clock("2026-05-19T12:00:30Z"),
    )
    raw = log.read_bytes()
    assert "🚀".encode("utf-8") in raw


# ---------------------------------------------------------------------------
# Search (P1-HIST-07)
# ---------------------------------------------------------------------------


def test_search_returns_empty_when_index_missing(tmp_path):
    assert search_history(tmp_path, "anything") == []


def test_search_matches_index_label_case_insensitive(tmp_path):
    entry = IndexEntry(
        session_id="abc",
        created_at="2026-05-19T12:00:00Z",
        label="MyRefactor",
        status=STATUS_IN_PROGRESS,
        original_prompt_preview="zzz",
    )
    upsert_index_entry(tmp_path, entry)
    results = search_history(tmp_path, "refactor")
    assert [r.session_id for r in results] == ["abc"]


def test_search_matches_index_preview_substring(tmp_path):
    entry = IndexEntry(
        session_id="abc",
        created_at="2026-05-19T12:00:00Z",
        label=None,
        status=STATUS_IN_PROGRESS,
        original_prompt_preview="Improve my SQL query please",
    )
    upsert_index_entry(tmp_path, entry)
    results = search_history(tmp_path, "sql")
    assert [r.session_id for r in results] == ["abc"]


def test_search_falls_back_to_session_file_when_index_misses(tmp_path):
    """P1-HIST-07: index first, then full-session fallback."""
    long_prompt = (
        "X" * ORIGINAL_PROMPT_PREVIEW_LIMIT  # fills the preview
        + " then a unique-keyword appears later"
    )
    session = _make_session(
        session_id="abc",
        original_prompt=long_prompt,
        created_at="2026-05-19T12:00:00Z",
    )
    write_session(session, tmp_path)
    upsert_index_entry(tmp_path, index_entry_from_session(session))

    # Keyword is past the preview window — only a session-file scan finds it.
    results = search_history(tmp_path, "unique-keyword")
    assert [r.session_id for r in results] == ["abc"]


def test_search_session_fallback_includes_final_prompt(tmp_path):
    session = _make_session(session_id="abc")
    finished = finalize_session(
        session,
        status=STATUS_ACCEPTED,
        final_prompt="improved with magic-string inside",
    )
    write_session(finished, tmp_path)
    upsert_index_entry(tmp_path, index_entry_from_session(finished))
    results = search_history(tmp_path, "magic-string")
    assert [r.session_id for r in results] == ["abc"]


def test_search_empty_keyword_returns_all_newest_first(tmp_path):
    _populate_history(tmp_path, 3)
    results = search_history(tmp_path, "")
    assert [r.session_id for r in results] == ["s002", "s001", "s000"]


def test_search_returns_empty_when_no_match(tmp_path):
    _populate_history(tmp_path, 3)
    assert search_history(tmp_path, "nonexistent-keyword") == []


def test_search_skips_missing_session_files(tmp_path):
    """Index points at a session whose file was removed → skip, don't crash."""
    session = _make_session(session_id="abc", original_prompt="visible-in-preview")
    upsert_index_entry(tmp_path, index_entry_from_session(session))
    # Session file intentionally not written.
    # Keyword does not match the index preview, so the fallback would try
    # to read the missing session file. It must not raise.
    results = search_history(tmp_path, "something-only-in-session-body")
    assert results == []


def test_search_skips_corrupt_session_files(tmp_path):
    """Index points at a session whose file is unparseable → skip, don't crash."""
    session = _make_session(session_id="abc", original_prompt="visible-in-preview")
    write_session(session, tmp_path)
    upsert_index_entry(tmp_path, index_entry_from_session(session))
    # Corrupt the session file with non-JSON content.
    session_path(tmp_path, "abc").write_text("not json", encoding="utf-8")
    results = search_history(tmp_path, "something-only-in-session-body")
    assert results == []


def test_search_results_sorted_newest_first(tmp_path):
    _populate_history(tmp_path, 3)
    # Both preview "Make it better." → all three match the keyword "make".
    results = search_history(tmp_path, "make")
    assert [r.session_id for r in results] == ["s002", "s001", "s000"]


# ---------------------------------------------------------------------------
# Non-fatal warning helper (P1-ERR-07 / P1-HIST-08)
# ---------------------------------------------------------------------------


def test_warn_history_failure_prints_canonical_message(capsys):
    warn_history_failure()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == HISTORY_WRITE_WARNING


def test_warn_history_failure_includes_reason_when_given(capsys):
    err = OSError("disk full")
    warn_history_failure(err)
    captured = capsys.readouterr()
    assert HISTORY_WRITE_WARNING in captured.err
    assert "disk full" in captured.err


def test_history_write_warning_is_canonical_text():
    assert HISTORY_WRITE_WARNING == "Warning: could not save session to history."


# ---------------------------------------------------------------------------
# Integration: full session lifecycle (AC #4)
# ---------------------------------------------------------------------------


def test_full_session_lifecycle_incremental_then_accepted(tmp_path):
    """Simulates the real refinement loop: in-progress on every turn,
    finalized to accepted on loop exit. The session file is rewritten in
    place each turn; the index is updated once.
    """
    session = _make_session(session_id="full")
    # Initial write — in-progress, no turns yet.
    write_session(session, tmp_path)
    upsert_index_entry(tmp_path, index_entry_from_session(session))

    # Three iterations: user → assistant pair each.
    clock_values = [
        f"2026-05-19T12:00:{10 + i:02d}Z" for i in range(6)
    ]
    seq = _seq_clock(clock_values)
    for i in range(3):
        session = append_turn(
            session,
            role="user",
            content=f"iterate {i}",
            backend="claude-cli",
            input_tokens=None,
            output_tokens=None,
            clock=seq,
        )
        session = append_turn(
            session,
            role="assistant",
            content=f"reply {i}",
            backend="claude-cli",
            input_tokens=None,
            output_tokens=None,
            clock=seq,
        )
        write_session(session, tmp_path)  # incremental write per turn pair

    on_disk = read_session("full", tmp_path)
    assert on_disk.status == STATUS_IN_PROGRESS
    assert len(on_disk.turns) == 6  # 3 pairs

    # Finalize: status accepted, final_prompt set.
    accepted = finalize_session(
        session,
        status=STATUS_ACCEPTED,
        final_prompt="reply 2",
        clock=_fixed_clock("2026-05-19T12:01:00Z"),
    )
    write_session(accepted, tmp_path)
    upsert_index_entry(tmp_path, index_entry_from_session(accepted))

    finalized = read_session("full", tmp_path)
    assert finalized.status == STATUS_ACCEPTED
    assert finalized.final_prompt == "reply 2"
    # Index reflects the accepted state.
    rows = read_index(tmp_path)
    assert len(rows) == 1
    assert rows[0].status == STATUS_ACCEPTED


def test_full_session_lifecycle_discarded(tmp_path):
    session = _make_session(session_id="full")
    write_session(session, tmp_path)
    upsert_index_entry(tmp_path, index_entry_from_session(session))

    discarded = finalize_session(session, status=STATUS_DISCARDED)
    write_session(discarded, tmp_path)
    upsert_index_entry(tmp_path, index_entry_from_session(discarded))

    rows = read_index(tmp_path)
    assert len(rows) == 1
    assert rows[0].status == STATUS_DISCARDED
    assert read_session("full", tmp_path).status == STATUS_DISCARDED


# ---------------------------------------------------------------------------
# Module-level sanity
# ---------------------------------------------------------------------------


def test_status_constants_match_spec():
    assert STATUS_IN_PROGRESS == "in-progress"
    assert STATUS_ACCEPTED == "accepted"
    assert STATUS_DISCARDED == "discarded"


def test_preview_limit_is_eighty():
    """Pin the constant — index preview width is part of the file format."""
    assert ORIGINAL_PROMPT_PREVIEW_LIMIT == 80


def test_index_and_usage_log_filenames_match_spec():
    assert INDEX_FILE_NAME == "index.json"
    assert USAGE_LOG_NAME == "usage.log"


def test_module_re_exports_atomic_write():
    """Sanity: atomic_write_bytes is the shared helper from core._io."""
    assert atomic_write_bytes is h.atomic_write_bytes
