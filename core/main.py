"""Module entry point for ``python3 -m core.main`` (US-012).

The bash launcher at :file:`bin/promptpal` invokes
``python3 -m core.main "$@"`` after duplicating the WSL HOME guard.
This module exists so that the launcher has a stable target whose
location is part of the public interface; the implementation lives in
:mod:`core.cli`.
"""

from __future__ import annotations

import sys

from core.cli import main as _cli_main


def main() -> int:
    """Run the PromptPal CLI and return its exit code."""
    return _cli_main()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
