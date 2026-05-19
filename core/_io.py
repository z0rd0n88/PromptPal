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
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(
    target: Path,
    payload: bytes,
    *,
    prefix: str,
    suffix: str,
) -> None:
    """Atomically write ``payload`` to ``target``.

    Uses ``tempfile.mkstemp`` in the same directory as ``target`` so the
    final ``os.replace`` runs on a single filesystem (atomic on POSIX,
    best-effort on Windows per the ``os.replace`` contract).

    The parent directory is created with ``parents=True, exist_ok=True``
    so first-run callers don't have to pre-create ``~/.promptpal/...``.
    Any failure cleans up the tempfile and re-raises so the existing
    target is never overwritten with a partial write.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=prefix, suffix=suffix, dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, target)
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
