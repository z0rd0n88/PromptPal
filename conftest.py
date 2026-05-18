"""Top-level conftest: makes the repo root importable so tests can `import core.*`.

Phase 1 ships as a thin bash entrypoint that runs `python3 -m core.main` from
the repo, so there is no installable package; this conftest mirrors that
runtime model for the test process.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
