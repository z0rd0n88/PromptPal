"""History and persistence layer (US-008 / SPEC §4, §9, P1-HIST-01..08).

PromptPal persists every refinement session under ``~/.promptpal/`` so a
crash mid-turn does not lose work and so the user can ``--search`` or
``--replay`` past prompts. This module owns the three files involved:

  - ``~/.promptpal/history/<uuid>.json`` — one per session (P1-HIST-01).
  - ``~/.promptpal/history/index.json``  — newest-first list of sessions
    with the search-relevant fields denormalized for fast lookup
    (P1-HIST-02).
  - ``~/.promptpal/usage.log``           — append-only NDJSON, one line
    per backend turn, for usage accounting (P1-HIST-05).

Schema decisions
----------------

The session record carries every field a future ``--replay`` or
``--export`` needs to recreate the conversation: the original prompt,
the model + backend used, each turn's role/content/token-counts, the
status, and an optional ``final_prompt`` set on accept. ``label`` (from
``--name``) is denormalized to both the session and the index entry so
``--search`` can match it without opening each session file.

Token-count semantics follow P1-BKND-10: API turns carry numeric
``input_tokens`` / ``output_tokens``; CLI turns carry ``None`` for both.
The usage-log line preserves the ``None`` as JSON ``null``.

Atomic writes
-------------

Every JSON write (session + index) goes through
:func:`core._io.atomic_write_bytes`, which uses ``tempfile.mkstemp`` +
``os.replace`` for a same-FS atomic rename. A crash mid-write leaves
either the previous file intact or the new file complete — never a
half-written file (NFR-04 / P1-HIST-03). The usage log is append-only;
NDJSON's one-record-per-line shape means the worst-case failure mode
under ``kill -9`` is a single truncated last line, which a consumer
can detect and skip.

Eviction
--------

After every accept/discard, :func:`enforce_max_entries` removes the
oldest sessions (by ``created_at``) past ``Config.max_history_entries``,
deleting both the on-disk file and the index entry (P1-HIST-06).

Non-fatal write failures
------------------------

Per P1-HIST-08, history failures must not abort the run. Callers are
expected to wrap :func:`write_session` / :func:`upsert_index_entry` /
:func:`append_usage_entry` in try/except for :class:`OSError`. The
:func:`warn_history_failure` helper is provided so the warning text
stays consistent across call sites.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core._io import append_ndjson_line, atomic_write_bytes


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_FILE_SUFFIX: str = ".json"
"""On-disk extension for ``<uuid>.json`` session files (P1-HIST-01)."""

INDEX_FILE_NAME: str = "index.json"
"""Newest-first index of sessions inside ``~/.promptpal/history/``."""

USAGE_LOG_NAME: str = "usage.log"
"""Append-only NDJSON log under ``~/.promptpal/`` (P1-HIST-05)."""

ORIGINAL_PROMPT_PREVIEW_LIMIT: int = 80
"""Number of characters from ``original_prompt`` stored in the index
entry; long prompts are truncated with no ellipsis to keep the preview
greppable byte-identical against ``original_prompt[:N]``."""

STATUS_IN_PROGRESS: str = "in-progress"
STATUS_ACCEPTED: str = "accepted"
STATUS_DISCARDED: str = "discarded"

VALID_STATUSES: tuple[str, ...] = (
    STATUS_IN_PROGRESS,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
)

HISTORY_WRITE_WARNING: str = "Warning: could not save session to history."
"""Canonical P1-ERR-07 message — pinned verbatim by tests."""


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class HistoryError(Exception):
    """Base class for history-layer errors that aren't plain :class:`OSError`."""


class SessionNotFoundError(HistoryError):
    """Raised when :func:`read_session` cannot locate the requested session."""


class AmbiguousSessionIdError(HistoryError):
    """Raised when a session-id prefix matches more than one session.

    Carries the offending ``prefix`` and the sorted list of full
    ``matches`` so the CLI can tell the user how to disambiguate.
    """

    def __init__(self, prefix: str, matches: list[str]) -> None:
        self.prefix = prefix
        self.matches = matches
        super().__init__(
            f"Session id {prefix!r} is ambiguous ({len(matches)} matches)"
        )


class InvalidSessionIdError(SessionNotFoundError):
    """Raised when a session_id would escape ``history_dir`` on join.

    Inherits :class:`SessionNotFoundError` so existing ``--resume`` /
    ``--export`` error handlers cover it without code change; the
    distinct class lets tests pin the path-traversal guard separately
    from a genuine "not found" miss.
    """


# ---------------------------------------------------------------------------
# Data classes (frozen — immutable session shape, AC for safe replay)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One backend turn (request + response).

    Token counts are ``None`` for CLI turns and numeric for API turns
    per P1-BKND-10. ``timestamp`` is an ISO-8601 string in UTC.
    """

    role: str
    content: str
    backend: str
    input_tokens: int | None
    output_tokens: int | None
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "backend": self.backend,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        return cls(
            role=data["role"],
            content=data["content"],
            backend=data["backend"],
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            timestamp=data["timestamp"],
        )


@dataclass(frozen=True)
class Session:
    """One refinement session (P1-HIST-01).

    Mutation is via :func:`dataclasses.replace`-style helpers
    (:func:`append_turn`, :func:`finalize_session`) so the in-flight
    object stays immutable and threadable through the loop without
    aliasing surprises.
    """

    session_id: str
    created_at: str
    updated_at: str
    status: str
    label: str | None
    original_prompt: str
    model: str
    backend: str
    turns: tuple[Turn, ...] = field(default_factory=tuple)
    final_prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "label": self.label,
            "original_prompt": self.original_prompt,
            "model": self.model,
            "backend": self.backend,
            "turns": [t.to_dict() for t in self.turns],
            "final_prompt": self.final_prompt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            session_id=data["session_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            status=data["status"],
            label=data.get("label"),
            original_prompt=data["original_prompt"],
            model=data["model"],
            backend=data["backend"],
            turns=tuple(Turn.from_dict(t) for t in data.get("turns", [])),
            final_prompt=data.get("final_prompt"),
        )


@dataclass(frozen=True)
class IndexEntry:
    """A single row in ``index.json`` (P1-HIST-02).

    Holds only the fields the index needs for paginated display and
    keyword search; the full conversation lives in the session file.
    """

    session_id: str
    created_at: str
    label: str | None
    status: str
    original_prompt_preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "label": self.label,
            "status": self.status,
            "original_prompt_preview": self.original_prompt_preview,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IndexEntry:
        return cls(
            session_id=data["session_id"],
            created_at=data["created_at"],
            label=data.get("label"),
            status=data["status"],
            original_prompt_preview=data.get("original_prompt_preview", ""),
        )


# ---------------------------------------------------------------------------
# Time + ID injection seams (mirrors transport/runner/fetcher from prior US)
# ---------------------------------------------------------------------------


Clock = Callable[[], str]
"""Signature: ``() -> str``. Returns an ISO-8601 UTC timestamp."""

IdFactory = Callable[[], str]
"""Signature: ``() -> str``. Returns a unique session id."""


def _utc_now_iso() -> str:
    """Default clock — ISO-8601 UTC with a ``Z`` suffix (no microseconds)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _default_id_factory() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Session factory + immutable updates
# ---------------------------------------------------------------------------


def new_session(
    *,
    original_prompt: str,
    model: str,
    backend: str,
    label: str | None = None,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
) -> Session:
    """Build a fresh :class:`Session` in ``in-progress`` state.

    No turns yet — those are appended via :func:`append_turn` as the
    refinement loop calls into the backend. The clock and id factory
    are injectable so tests can pin both fields deterministically.
    """
    now = (clock or _utc_now_iso)()
    sid = (id_factory or _default_id_factory)()
    return Session(
        session_id=sid,
        created_at=now,
        updated_at=now,
        status=STATUS_IN_PROGRESS,
        label=label,
        original_prompt=original_prompt,
        model=model,
        backend=backend,
        turns=(),
        final_prompt=None,
    )


def append_turn(
    session: Session,
    *,
    role: str,
    content: str,
    backend: str,
    input_tokens: int | None,
    output_tokens: int | None,
    clock: Clock | None = None,
) -> Session:
    """Return a new session with the given turn appended.

    ``updated_at`` is set to the clock's ``now``; the rest of the
    session is preserved via :func:`dataclasses.replace`. The original
    session object is not mutated (frozen dataclass).
    """
    now = (clock or _utc_now_iso)()
    turn = Turn(
        role=role,
        content=content,
        backend=backend,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        timestamp=now,
    )
    return replace(session, turns=session.turns + (turn,), updated_at=now)


def finalize_session(
    session: Session,
    *,
    status: str,
    final_prompt: str | None = None,
    clock: Clock | None = None,
) -> Session:
    """Return a new session with the terminal ``status`` set.

    ``status`` must be ``accepted`` or ``discarded`` (P1-LOOP-04/05).
    ``final_prompt`` is set when ``status == accepted`` and carries the
    assistant text of the last turn so ``--export`` doesn't need to
    re-derive it.
    """
    if status not in (STATUS_ACCEPTED, STATUS_DISCARDED):
        raise ValueError(
            f"finalize_session: status must be 'accepted' or 'discarded', "
            f"got {status!r}"
        )
    now = (clock or _utc_now_iso)()
    return replace(
        session,
        status=status,
        final_prompt=final_prompt,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


_SESSION_ID_TRUNC_LIMIT: int = 64
"""Truncation cap for session-id values surfaced in error messages.

A session-id can arrive from a hostile prompt or a corrupted on-disk
row; the error path prints it verbatim to stderr. Truncating at this
limit caps the worst-case leak and keeps the diagnostic readable.
"""


def session_path(history_dir: Path, session_id: str) -> Path:
    """Return ``<history_dir>/<session_id>.json`` (P1-HIST-01).

    Validates that the joined path lives directly inside ``history_dir``
    — a ``session_id`` of ``../../etc/passwd`` would otherwise escape via
    ``Path /`` semantics (``Path("/h") / "/etc/passwd"`` collapses to
    ``Path("/etc/passwd")`` entirely; ``Path("/h") / "../../etc/passwd"``
    resolves to ``/etc/passwd``). UUID-hex ids from
    :func:`_default_id_factory` are safe by construction, but ids that
    flow through ``--resume <id>`` or a corrupted ``index.json`` are
    user-controlled and must not become a file-write primitive anywhere
    on disk.

    The check is "resolved parent must equal resolved ``history_dir``"
    rather than "starts with" so nested-directory ids (``foo/bar``) are
    also rejected — sessions are intentionally flat, one file per id.

    Raises :class:`InvalidSessionIdError` (a :class:`SessionNotFoundError`
    subclass) when the containment check fails. The error message
    truncates ``session_id`` to :data:`_SESSION_ID_TRUNC_LIMIT` so a
    pathological value (e.g. from a hostile prompt injection round-trip)
    can't leak unbounded bytes to stderr.
    """
    history_dir = Path(history_dir)
    candidate = history_dir / f"{session_id}{SESSION_FILE_SUFFIX}"
    history_resolved = history_dir.resolve()
    candidate_resolved = candidate.resolve()
    if candidate_resolved.parent != history_resolved:
        truncated = session_id[:_SESSION_ID_TRUNC_LIMIT]
        if len(session_id) > _SESSION_ID_TRUNC_LIMIT:
            truncated += "..."
        raise InvalidSessionIdError(
            f"Session id {truncated!r} would escape history directory "
            f"{history_dir}"
        )
    return candidate


def index_path(history_dir: Path) -> Path:
    """Return ``<history_dir>/index.json`` (P1-HIST-02)."""
    return Path(history_dir) / INDEX_FILE_NAME


# ---------------------------------------------------------------------------
# Session I/O (P1-HIST-01, P1-HIST-03, P1-HIST-04)
# ---------------------------------------------------------------------------


def write_session(session: Session, history_dir: str | Path) -> Path:
    """Atomically write ``session`` to ``<history_dir>/<session_id>.json``.

    Used both for incremental updates (status=in-progress, after each
    turn) and for the final accept/discard write. The atomic-write
    contract from :func:`core._io.atomic_write_bytes` is inherited.

    Returns the on-disk path so the caller can log it.
    """
    target = session_path(Path(history_dir), session.session_id)
    body = (json.dumps(session.to_dict(), indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(target, body, prefix=".session-", suffix=".json")
    return target


def read_session(session_id: str, history_dir: str | Path) -> Session:
    """Load a session JSON file.

    Raises :class:`SessionNotFoundError` if the file is missing.
    Lets :class:`json.JSONDecodeError` / :class:`OSError` propagate so
    the caller can decide between "corrupt — abort" and "missing turns
    — recover what we can".
    """
    target = session_path(Path(history_dir), session_id)
    if not target.exists():
        raise SessionNotFoundError(f"Session {session_id} not found at {target}")
    data = json.loads(target.read_text(encoding="utf-8"))
    return Session.from_dict(data)


def resolve_session_id(id_or_prefix: str, history_dir: str | Path) -> str:
    """Resolve a full session id *or a unique prefix* to a full session id.

    Git-style lookup so the 8-char id printed by ``--show-history`` /
    ``--search`` is directly usable with ``--export`` / ``--replay``
    (the displayed id is a prefix of the 32-char on-disk id):

    - An exact ``<id>.json`` match wins immediately (fast path).
    - Otherwise, if exactly one stored session id *starts with*
      ``id_or_prefix``, its full id is returned.
    - No match → :class:`SessionNotFoundError`.
    - More than one match → :class:`AmbiguousSessionIdError`.

    Empty ``id_or_prefix`` is treated as "no match" rather than matching
    every session.
    """
    history_dir = Path(history_dir)
    if id_or_prefix:
        # ``session_path`` itself enforces the H1 containment check, so a
        # traversal-style id surfaces here as ``InvalidSessionIdError``
        # rather than reaching ``candidate.exists()``. Re-raise as a plain
        # ``SessionNotFoundError`` so the user-visible diagnostic stays in
        # the "id not found" vocabulary; the security-relevant rejection is
        # already recorded by the layered defense.
        try:
            candidate = session_path(history_dir, id_or_prefix)
        except InvalidSessionIdError:
            raise SessionNotFoundError(
                f"Session {id_or_prefix!r} not found"
            ) from None
        # Guard against id_or_prefix == "index" resolving to index.json,
        # and against an in-directory symlink masquerading as a session.
        # The symlink filter mirrors the M12 fix below in the prefix-scan
        # branch — closed at the resolution boundary instead of leaving
        # the H1 containment check as the sole rescue.
        if (
            candidate.exists()
            and candidate.name != INDEX_FILE_NAME
            and not candidate.is_symlink()
        ):
            return id_or_prefix
    else:
        raise SessionNotFoundError(f"Session {id_or_prefix!r} not found")
    matches = sorted(
        p.stem
        for p in history_dir.glob(f"*{SESSION_FILE_SUFFIX}")
        if p.name != INDEX_FILE_NAME
        and not p.is_symlink()
        and p.stem.startswith(id_or_prefix)
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SessionNotFoundError(f"Session {id_or_prefix!r} not found")
    raise AmbiguousSessionIdError(prefix=id_or_prefix, matches=matches)


# ---------------------------------------------------------------------------
# Index I/O (P1-HIST-02, P1-HIST-03)
# ---------------------------------------------------------------------------


def _truncate_preview(text: str, limit: int = ORIGINAL_PROMPT_PREVIEW_LIMIT) -> str:
    """Truncate ``text`` to ``limit`` characters with no ellipsis.

    The preview is meant to match ``original_prompt[:N]`` byte-identical
    so ``--search`` can substring-match without normalizing.
    """
    if len(text) <= limit:
        return text
    return text[:limit]


def index_entry_from_session(session: Session) -> IndexEntry:
    """Derive the index row for ``session`` (P1-HIST-02)."""
    return IndexEntry(
        session_id=session.session_id,
        created_at=session.created_at,
        label=session.label,
        status=session.status,
        original_prompt_preview=_truncate_preview(session.original_prompt),
    )


def read_index(history_dir: str | Path) -> list[IndexEntry]:
    """Read the newest-first index. Missing/corrupt file → empty list.

    A truncated, non-JSON, non-list-root, or per-row malformed
    ``index.json`` returns the longest valid set of rows it can salvage
    instead of raising — corruption must not abort the run
    (P1-FIX-28-06 / P1-HIST-08).

    Per-entry tolerance: each list element is filtered through
    ``isinstance(row, dict)`` first, and :func:`IndexEntry.from_dict`'s
    ``KeyError`` / ``TypeError`` / ``ValueError`` failures are swallowed
    — one bad row must not orphan the rest of the index. ``ValueError``
    is included for forward-compatibility with future validators on the
    :class:`IndexEntry` constructor.

    Degraded reads are not silent: if any row is dropped, a one-line
    summary lands on stderr so the user can tell "no history" from
    "history present but degraded".
    """
    target = index_path(Path(history_dir))
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    entries: list[IndexEntry] = []
    dropped = 0
    for row in data:
        if not isinstance(row, dict):
            dropped += 1
            continue
        try:
            entries.append(IndexEntry.from_dict(row))
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
    if dropped:
        print(
            f"warning: skipped {dropped} malformed row(s) in {target}",
            file=sys.stderr,
        )
    return entries


def write_index(entries: list[IndexEntry], history_dir: str | Path) -> Path:
    """Atomically write ``entries`` to ``index.json``.

    Entries are written in the order given; callers are responsible for
    sorting newest-first before calling (:func:`upsert_index_entry`
    handles this for the common case).
    """
    target = index_path(Path(history_dir))
    body = (
        json.dumps([e.to_dict() for e in entries], indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(target, body, prefix=".index-", suffix=".json")
    return target


def upsert_index_entry(history_dir: str | Path, entry: IndexEntry) -> None:
    """Insert or update ``entry`` in the on-disk index.

    Behavior:

    - If a row with the same ``session_id`` already exists, it is
      replaced in place (preserving its position-by-created_at order).
    - Otherwise the new entry is inserted at the head (newest-first).
    - The full index is then re-sorted newest-first by ``created_at``
      so callers can render it without re-sorting.

    The write is atomic via :func:`write_index`.
    """
    existing = read_index(history_dir)
    updated: list[IndexEntry] = []
    found = False
    for row in existing:
        if row.session_id == entry.session_id:
            updated.append(entry)
            found = True
        else:
            updated.append(row)
    if not found:
        updated.append(entry)
    updated.sort(key=lambda e: e.created_at, reverse=True)
    write_index(updated, history_dir)


# ---------------------------------------------------------------------------
# Eviction (P1-HIST-06)
# ---------------------------------------------------------------------------


def enforce_max_entries(history_dir: str | Path, max_entries: int) -> list[str]:
    """Evict oldest sessions past ``max_entries``; return evicted ids.

    Eviction strategy:

    1. Read the index. If ``len(index) <= max_entries``, no-op.
    2. Sort newest-first by ``created_at``; keep the first
       ``max_entries`` rows, mark the rest for eviction.
    3. Delete the corresponding ``<session_id>.json`` files (missing
       files are tolerated — eviction is idempotent on a partial run).
    4. Rewrite the index without the evicted rows.

    ``max_entries`` < 0 is treated as "unlimited" (no-op) so a caller
    that accidentally passes a negative value cannot wipe the history.
    """
    if max_entries < 0:
        return []
    entries = read_index(history_dir)
    if len(entries) <= max_entries:
        return []
    entries.sort(key=lambda e: e.created_at, reverse=True)
    keep = entries[:max_entries]
    evict = entries[max_entries:]
    history_dir_path = Path(history_dir)
    evicted_ids: list[str] = []
    for row in evict:
        # ``session_path`` enforces the H1 containment check, so a
        # corrupted index row with a traversal-style id raises
        # ``InvalidSessionIdError`` here rather than producing a
        # write/unlink primitive outside ``history_dir``. Tolerate it
        # alongside the documented ``FileNotFoundError`` so a single bad
        # row cannot abort the whole eviction loop and corrupt the
        # newly-written index.
        try:
            target = session_path(history_dir_path, row.session_id)
            target.unlink()
        except (FileNotFoundError, InvalidSessionIdError):
            pass
        evicted_ids.append(row.session_id)
    write_index(keep, history_dir)
    return evicted_ids


# ---------------------------------------------------------------------------
# Usage log (P1-HIST-05)
# ---------------------------------------------------------------------------


def append_usage_entry(
    usage_log_path: str | Path,
    *,
    session_id: str,
    turn_index: int,
    backend: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    clock: Clock | None = None,
) -> None:
    """Append one NDJSON line to ``usage.log``.

    Token counts may be ``null`` for CLI turns (P1-BKND-10). The
    ``timestamp`` field is set by the injectable ``clock`` so tests can
    pin it.
    """
    now = (clock or _utc_now_iso)()
    record: dict[str, Any] = {
        "timestamp": now,
        "session_id": session_id,
        "turn_index": turn_index,
        "backend": backend,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    append_ndjson_line(Path(usage_log_path), record)


# ---------------------------------------------------------------------------
# Search (P1-HIST-07)
# ---------------------------------------------------------------------------


def search_history(history_dir: str | Path, keyword: str) -> list[IndexEntry]:
    """Return index entries matching ``keyword`` (case-insensitive).

    Matching strategy per P1-HIST-07:

    1. Match against ``original_prompt_preview`` and ``label`` in the
       index entries first (fast path).
    2. For sessions that *didn't* match the index, fall back to scanning
       their session files for ``original_prompt`` and ``final_prompt``
       containing the keyword. Sessions whose files are missing or
       unreadable are skipped silently — search must not crash the run.

    Results are sorted by ``created_at`` descending. The keyword match
    is case-insensitive substring; an empty keyword returns every entry.
    """
    history_dir_path = Path(history_dir)
    entries = read_index(history_dir_path)
    if not keyword:
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries

    needle = keyword.lower()
    matched: list[IndexEntry] = []
    unmatched: list[IndexEntry] = []
    for row in entries:
        label_match = row.label is not None and needle in row.label.lower()
        preview_match = needle in row.original_prompt_preview.lower()
        if label_match or preview_match:
            matched.append(row)
        else:
            unmatched.append(row)

    # Fallback: scan full session files for the rest.
    for row in unmatched:
        try:
            session = read_session(row.session_id, history_dir_path)
        except (SessionNotFoundError, OSError, json.JSONDecodeError):
            continue
        haystack_parts = [session.original_prompt]
        if session.final_prompt is not None:
            haystack_parts.append(session.final_prompt)
        haystack = "\n".join(haystack_parts).lower()
        if needle in haystack:
            matched.append(row)

    matched.sort(key=lambda e: e.created_at, reverse=True)
    return matched


# ---------------------------------------------------------------------------
# Non-fatal warning helper (P1-HIST-08 / P1-ERR-07)
# ---------------------------------------------------------------------------


def warn_history_failure(error: BaseException | None = None) -> None:
    """Print the canonical P1-ERR-07 warning to stderr.

    Used by callers that want to keep the run going after a history
    write fails. Optional ``error`` is appended as ``(reason: ...)``
    when given so the user has a debugging breadcrumb without changing
    the canonical headline.
    """
    if error is None:
        print(HISTORY_WRITE_WARNING, file=sys.stderr)
    else:
        print(f"{HISTORY_WRITE_WARNING} (reason: {error})", file=sys.stderr)
