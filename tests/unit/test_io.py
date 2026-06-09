"""Tests for core/_io.py — atomic-write primitive used by every state file.

Pins the durability recipe documented in the module docstring (NFR-04) and
the fd-leak guard for ``tempfile.mkstemp`` + ``os.fdopen`` (P1-PLAT-04).

Coverage map:

  C1  fsync(file_fd) before os.replace                 → test_*fsync_file*
  C1  fsync(parent_dir_fd) after os.replace            → test_*fsync_parent_dir*
  C1  parent-dir fsync best-effort on Windows          → test_*swallows*
  C2  os.close(fd) when os.fdopen raises               → test_*closes_raw_fd*
  C2  tempfile unlinked when os.fdopen raises          → test_*unlinks_tempfile*
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from core._io import append_ndjson_line, atomic_write_bytes


# ---------------------------------------------------------------------------
# Happy path — pin the public contract before exercising failure modes
# ---------------------------------------------------------------------------


def test_atomic_write_bytes_writes_payload(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_bytes(target, b"hello", prefix=".x-", suffix=".json")
    assert target.read_bytes() == b"hello"


def test_atomic_write_bytes_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "x.json"
    atomic_write_bytes(target, b"hi", prefix=".x-", suffix=".json")
    assert target.read_bytes() == b"hi"


def test_atomic_write_bytes_no_tempfile_leftovers_on_success(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_bytes(target, b"hi", prefix=".x-", suffix=".json")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "x.json"]
    assert leftovers == [], f"unexpected leftovers: {leftovers}"


def test_atomic_write_bytes_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_bytes(target, b"first", prefix=".x-", suffix=".json")
    atomic_write_bytes(target, b"second", prefix=".x-", suffix=".json")
    assert target.read_bytes() == b"second"


# ---------------------------------------------------------------------------
# C1 — fsync recipe (NFR-04 durability)
# ---------------------------------------------------------------------------


def test_atomic_write_bytes_fsyncs_file_then_replaces_then_fsyncs_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: durable atomic write recipe is fsync(file) → replace → fsync(parent dir).

    Skipping any step turns the supposedly atomic+durable rename into a best-effort
    rename: after kernel panic or power loss following ``os.replace``, the renamed
    target inode can be correct on the directory entry side but the data blocks
    are still in the page cache and never made it to disk, leaving zero-byte or
    stale content. The module docstring promises NFR-04; only the full recipe
    delivers it.
    """
    target = tmp_path / "x.json"

    log: list[str] = []
    real_open = os.open
    real_replace = os.replace

    dir_fds: set[int] = set()

    def hooked_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        try:
            if os.path.isdir(path):
                dir_fds.add(fd)
        except OSError:
            pass
        return fd

    def hooked_fsync(fd: int) -> None:
        log.append("fsync_dir" if fd in dir_fds else "fsync_file")

    def hooked_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        log.append("replace")
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("core._io.os.open", hooked_open)
    monkeypatch.setattr("core._io.os.fsync", hooked_fsync)
    monkeypatch.setattr("core._io.os.replace", hooked_replace)

    atomic_write_bytes(target, b"hello", prefix=".x-", suffix=".json")

    assert log == ["fsync_file", "replace", "fsync_dir"], (
        f"durability recipe broken — expected [fsync_file, replace, fsync_dir], "
        f"got {log}"
    )


def test_atomic_write_bytes_swallows_parent_dir_fsync_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: parent-dir fsync must be best-effort.

    On Windows, ``os.fsync`` of a directory fd raises ``OSError(EINVAL)``; on
    some filesystems (FAT/exFAT, network mounts) it raises ``OSError(EROFS)``
    or ``ENOTSUP``. The write itself has already succeeded by the time we get
    to the dir-fsync; surfacing this failure to the caller would mean breaking
    every write on those platforms. The recipe must wrap the dir-fsync in a
    try/except and swallow.
    """
    target = tmp_path / "x.json"

    real_open = os.open
    dir_fds: set[int] = set()

    def hooked_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        try:
            if os.path.isdir(path):
                dir_fds.add(fd)
        except OSError:
            pass
        return fd

    def hooked_fsync(fd: int) -> None:
        if fd in dir_fds:
            raise OSError(22, "Invalid argument")  # simulate Windows EINVAL

    monkeypatch.setattr("core._io.os.open", hooked_open)
    monkeypatch.setattr("core._io.os.fsync", hooked_fsync)

    atomic_write_bytes(target, b"hello", prefix=".x-", suffix=".json")
    assert target.read_bytes() == b"hello"


def test_atomic_write_bytes_swallows_parent_dir_open_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C1: if even opening the parent dir for fsync fails, swallow.

    Some platforms (Windows on certain Python builds) don't let you ``os.open``
    a directory at all. The write itself has succeeded by then; the dir-fsync
    is purely best-effort durability beyond what ``os.replace`` already
    guarantees, so it must not fail the call.

    Uses ``OSError(EINVAL)`` here — a plausible platform-tolerance code that
    is NOT auto-promoted to :class:`PermissionError`. ``PermissionError`` is
    tested separately because it indicates a real misconfiguration and is
    logged to stderr rather than silently swallowed.
    """
    target = tmp_path / "x.json"

    real_open = os.open
    parent_str = str(target.parent)

    def hooked_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path) == parent_str:
            raise OSError(22, "Invalid argument")  # EINVAL — platform tolerance
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("core._io.os.open", hooked_open)

    atomic_write_bytes(target, b"hello", prefix=".x-", suffix=".json")
    assert target.read_bytes() == b"hello"
    # Platform-tolerance OSError must NOT print to stderr.
    assert capsys.readouterr().err == ""


def test_atomic_write_bytes_warns_on_parent_dir_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C1: ``PermissionError`` on the parent-dir open is a real misconfiguration
    signal (wrong ACL, wrong owner), not platform tolerance.

    Silent-swallow would hide a genuine durability gap: the user's write
    completes but the directory entry is not fsynced, and the root cause is
    invisible. Surface it on stderr so the user can fix the permissions
    before the next crash eats the rename.
    """
    target = tmp_path / "x.json"

    real_open = os.open
    parent_str = str(target.parent)

    def hooked_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path) == parent_str:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("core._io.os.open", hooked_open)

    # Call must still succeed — the file write itself isn't blocked.
    atomic_write_bytes(target, b"hello", prefix=".x-", suffix=".json")
    assert target.read_bytes() == b"hello"
    err = capsys.readouterr().err
    assert "fsync parent dir" in err
    assert "Permission denied" in err


# ---------------------------------------------------------------------------
# C2 — fd-leak guard around os.fdopen
# ---------------------------------------------------------------------------


def test_atomic_write_bytes_closes_raw_fd_when_fdopen_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: ``tempfile.mkstemp`` returns a raw fd; the cleanup block must call
    ``os.close(fd)`` if ``os.fdopen`` raises before adopting the fd.

    Without this guard, an fd-table-exhaustion failure mode (EMFILE) leaks
    every fd it touches — exactly the situation where leaking another one
    matters most.
    """
    target = tmp_path / "x.json"

    closed_fds: list[int] = []
    real_close = os.close

    def hooked_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    def hooked_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        raise OSError(24, "Too many open files")  # EMFILE

    monkeypatch.setattr("core._io.os.close", hooked_close)
    monkeypatch.setattr("core._io.os.fdopen", hooked_fdopen)

    with pytest.raises(OSError, match="Too many open files"):
        atomic_write_bytes(target, b"hi", prefix=".x-", suffix=".json")

    assert len(closed_fds) == 1, (
        "fd from mkstemp must be os.close'd exactly once when os.fdopen "
        f"raises before adopting it; got {len(closed_fds)} closes ({closed_fds})"
    )


def test_atomic_write_bytes_unlinks_tempfile_when_fdopen_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: the orphaned tempfile must also be cleaned up when fdopen raises.

    Pre-existing behavior the new fd-close path must not regress.
    """
    target = tmp_path / "x.json"

    def hooked_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr("core._io.os.fdopen", hooked_fdopen)

    with pytest.raises(OSError):
        atomic_write_bytes(target, b"hi", prefix=".x-", suffix=".json")

    leftovers = list(tmp_path.iterdir())
    assert leftovers == [], f"tempfile must be cleaned up: {leftovers}"


# ---------------------------------------------------------------------------
# append_ndjson_line — sanity check (no behavior change in this slice)
# ---------------------------------------------------------------------------


def test_append_ndjson_line_creates_parent_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "usage.log"
    append_ndjson_line(path, {"a": 1})
    append_ndjson_line(path, {"b": 2})
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n{"b": 2}\n'
