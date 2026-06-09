"""Shared IO helpers — atomic writes, append-only logs.

Extracted in US-008 because the same atomic-write pattern now lives in
three call sites: ``core.config.save_config`` (US-002),
``core.system_prompt._atomic_write_bytes`` (US-007), and
``core.history.write_session`` / ``upsert_index_entry`` (US-008). The
duplication learning was logged in ``ralph/progress.txt`` after US-007
and acted on here.

Public surface
--------------

- :func:`atomic_write_bytes` — write ``payload`` to ``target`` via
  ``tempfile.mkstemp`` (same-FS) + ``os.replace`` (atomic on POSIX,
  best-effort on Windows). The parent directory is created on demand;
  the tempfile is unlinked on any failure so a crash never leaks a
  ``.<prefix>...`` file next to the target.
- :func:`append_ndjson_line` — append a single JSON-serialized object as
  a newline-terminated line to ``path``, creating the parent directory
  on demand. Used for ``~/.promptpal/usage.log`` per P1-HIST-05.

Both helpers raise :class:`OSError` on filesystem failure; the caller
decides whether the failure is fatal or warn-only (P1-HIST-08).

Durability recipe (NFR-04)
--------------------------

``os.replace`` is atomic, but on its own it is *not durable*: after a
kernel panic or power loss following a successful return, the rename can
land in the directory entry while the file's data blocks are still in the
page cache, leaving zero-byte or stale content on the next boot. The
full recipe :func:`atomic_write_bytes` implements is:

  1. ``f.write(payload); f.flush(); os.fsync(file_fd)`` — push the data
     blocks past the page cache.
  2. ``os.replace(tmp, target)`` — atomic rename.
  3. ``os.fsync(parent_dir_fd)`` — persist the directory entry.

Step 3 is best-effort: Windows / FAT / network mounts can refuse to
fsync a directory fd. Steps 1 and 2 are mandatory.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _fsync_dir_best_effort(directory: Path) -> None:
    """Open ``directory`` and ``os.fsync`` its fd, swallowing OSError.

    POSIX systems persist a rename to the inode/dirent map only after the
    parent directory's metadata is fsynced; without this step, a crash
    between :func:`os.replace` and the next disk flush can lose the rename
    even though the rename call itself returned.

    Best-effort because Windows / FAT / SMB mounts return ``OSError(EINVAL)``
    or ``OSError(EROFS)`` from ``os.fsync`` on a directory fd, and some
    Python builds disallow ``os.open`` on a directory altogether. The
    write itself is already complete by the time we get here, so a failure
    in this step must not propagate.
    """
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        os.close(dir_fd)


def atomic_write_bytes(
    target: Path,
    payload: bytes,
    *,
    prefix: str,
    suffix: str,
) -> None:
    """Atomically and durably write ``payload`` to ``target`` (NFR-04).

    Uses ``tempfile.mkstemp`` in the same directory as ``target`` so the
    final ``os.replace`` runs on a single filesystem (atomic on POSIX,
    best-effort on Windows per the ``os.replace`` contract).

    The parent directory is created with ``parents=True, exist_ok=True``
    so first-run callers don't have to pre-create ``~/.promptpal/...``.
    Any failure cleans up the tempfile and re-raises so the existing
    target is never overwritten with a partial write.

    Durability recipe — see the module docstring for rationale:

      1. ``f.flush()`` + ``os.fsync(file_fd)`` before ``os.replace`` so a
         post-replace crash never sees a zero-byte / stale target file.
      2. ``os.fsync`` on the parent directory fd after ``os.replace`` so
         the rename itself survives the crash.

    The raw fd returned by ``tempfile.mkstemp`` is closed explicitly if
    ``os.fdopen`` raises before adopting it (e.g. under fd-table
    exhaustion / EMFILE) — without this guard the failure would leak a
    file descriptor on every retry, compounding the exhaustion.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=prefix, suffix=suffix, dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        try:
            f = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        _fsync_dir_best_effort(target.parent)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def append_ndjson_line(path: Path, record: dict[str, Any]) -> None:
    """Append ``record`` as a single JSON line (UTF-8, LF) to ``path``.

    Used for the append-only ``usage.log`` (P1-HIST-05). The parent
    directory is created on demand. The payload is serialized with
    ``ensure_ascii=False`` so non-ASCII content rides as raw UTF-8 bytes
    and the file is human-greppable.

    Not atomic by design — append semantics are sufficient for an
    append-only log; the worst-case failure mode is a partial last line
    on ``kill -9``, which the consumer can detect and skip.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "ab") as f:
        f.write(line.encode("utf-8"))
