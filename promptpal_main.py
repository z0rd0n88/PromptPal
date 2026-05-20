"""Path-invoked entry point for ``promptpal`` (avoids the ``-m`` CWD footgun).

Both launchers (the dev ``bin/promptpal`` and the installer-generated
``~/.local/bin/promptpal``) invoke this file by **absolute path**::

    python3 /abs/path/to/promptpal_main.py "$@"

Why a file instead of ``python3 -m core.main``? ``-m`` prepends the
*current working directory* to ``sys.path`` (``sys.path[0] == ""``). If the
user happens to run ``promptpal`` from a directory that contains its own
``core/`` package (e.g. the PromptPal source checkout, or any Python
project with a ``core`` module), that stray package shadows the installed
one and PromptPal imports the wrong code — silently.

Running a *script by path* instead makes Python put the **script's own
directory** at ``sys.path[0]`` (never the CWD). Because this file sits
beside the ``core/`` package it belongs to, ``import core`` always resolves
to the correct sibling regardless of where the user invoked the command.
"""

from __future__ import annotations

import sys

from core.main import main

if __name__ == "__main__":
    sys.exit(main())
