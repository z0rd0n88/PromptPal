"""CLI argument parsing and main entry point (US-011 / SPEC §5, P1-FLAG-*).

This module is the **single** orchestration layer for ``promptpal``. Every
helper module below it (``core.config``, ``core.resolve``,
``core.system_prompt``, ``core.history``, ``core.input``, ``core.diff``,
``core.loop``, ``core.platform``) is pure-functional and side-effect-free
on import; the CLI is where flags, the real filesystem, the real
backend, the real stdin/stdout/stderr, and the real exit codes meet.

Pipe-safety contract (P1-PIPE-09)
---------------------------------

Every write to ``stdout`` from PromptPal happens in *this* module and is
either:

  - the final improved prompt (``--output plain``), or
  - the ``--output json`` envelope, or
  - the ``--show-history`` / ``--search`` / ``--export`` / ``--status``
    report (these flags short-circuit the pipeline, so the "improved
    prompt to stdout" rule doesn't apply).

Everything else (diffs, banners, spinner output, error messages, warning
lines) goes to ``stderr``. Helper modules already uphold this discipline;
the CLI doesn't break it.

Test seam pattern
-----------------

:func:`main` accepts a long list of injectable kwargs — ``config_path``,
``history_dir``, ``usage_log_path``, ``stdin``/``stdout``/``stderr``,
``detect_platform_fn``, ``backend_factory``, ``copy_fn``, ``fetcher``,
``clock``, ``id_factory`` — so tests exercise full pipelines without
touching the real filesystem, network, or subprocess. Production callers
pass nothing and get the real ``os.environ`` / ``sys.stdin`` / etc.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from core.api_backend import ApiKeyMissingError
from core.backend import Backend, BackendResponse, Message, NoBackendError
from core.cli_backend import CliNotFoundError
from core.config import (
    Config,
    ConfigCorruptError,
    apply_overrides,
    load_config,
)
from core.diff import format_diff, should_show_diff
from core.history import (
    HISTORY_WRITE_WARNING,
    STATUS_ACCEPTED,
    AmbiguousSessionIdError,
    IndexEntry,
    InvalidSessionIdError,
    Session,
    SessionNotFoundError,
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
    write_session,
)
from core.input import EmptyPromptError, read_prompt
from core.loop import (
    SYNTHESIZED_FEEDBACK,
    LoopOutcome,
    run_refinement_loop,
)
from core.platform import (
    Platform,
    assert_wsl_home_safe,
    copy_to_clipboard,
    detect_platform,
)
from core.resolve import (
    clear_backend_preference,
    persist_backend_preference,
    resolve_backend,
)
from core.system_prompt import (
    SystemPromptChecksumError,
    SystemPromptDownloadError,
    SystemPromptError,
    SystemPromptMissingError,
    apply_xml_tags_directive,
    read_system_prompt,
    resolve_system_prompt_path,
    seed_system_prompt,
    update_system_prompt,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUTPUT_PLAIN: str = "plain"
OUTPUT_JSON: str = "json"
OUTPUT_MARKDOWN: str = "markdown"
OUTPUT_FORMATS: tuple[str, ...] = (OUTPUT_PLAIN, OUTPUT_JSON, OUTPUT_MARKDOWN)

BACKEND_AUTO: str = "auto"
BACKEND_CLI: str = "claude-cli"
BACKEND_API: str = "api-key"
BACKEND_CHOICES: tuple[str, ...] = (BACKEND_AUTO, BACKEND_CLI, BACKEND_API)

DEFAULT_CONFIG_PATH: str = "~/.promptpal/config.json"
DEFAULT_HISTORY_DIR: str = "~/.promptpal/history"
DEFAULT_USAGE_LOG: str = "~/.promptpal/usage.log"

SHOW_HISTORY_PAGE_SIZE: int = 20
"""Number of rows shown by ``--show-history`` before truncating with a footer."""

# Canonical messages — pinned verbatim by tests.

UNINSTALL_NOT_IMPLEMENTED: str = (
    "The --uninstall flag is a launcher convenience that delegates to "
    "uninstall.sh. Run that script directly: bash uninstall.sh [--purge]."
)
"""Surfaced when ``--uninstall`` is passed to the CLI.

The actual removal logic lives in ``uninstall.sh`` (US-015) so the
Python process doesn't have to delete itself mid-run. The flag stays
wired so ``--help`` advertises it and the parser doesn't reject it.
"""

REPLAY_NOT_FOUND_TEMPLATE: str = "Error: session {session_id!r} not found."
"""Stderr line when ``--replay SESSION_ID`` can't locate the source session."""

REPLAY_EMPTY_TEMPLATE: str = (
    "Error: session {session_id!r} has no assistant turn to replay from."
)
"""Stderr line when ``--replay`` targets a session that never got an assistant reply."""

EXPORT_NOT_FOUND_TEMPLATE: str = "Error: session {session_id!r} not found."
AMBIGUOUS_SESSION_ID_TEMPLATE: str = (
    "Error: session id {prefix!r} is ambiguous ({count} matches: {sample}). "
    "Use more characters."
)
"""Stderr line when a session-id prefix matches more than one session
(shared by ``--export`` and ``--replay``)."""

UPDATE_SUCCESS_TEMPLATE: str = "System prompt updated at {path}."
UPDATE_BACKUP_TEMPLATE: str = "Previous prompt backed up at {path}."

# Exit codes
EXIT_OK: int = 0
EXIT_FAILURE: int = 1
EXIT_DISCARDED: int = 0
"""``[d]iscard`` is not an error — the user explicitly chose to drop the result."""


# ---------------------------------------------------------------------------
# CLIOptions dataclass (SPEC §5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CLIOptions:
    """Parsed view of every PRD §5.4 flag.

    All fields default to ``None`` / ``False`` so a caller can construct
    a partial ``CLIOptions`` in tests without listing every flag. Build
    real instances via :func:`parse_args`.
    """

    prompt: str | None = None
    model: str | None = None
    iterations: int | None = None
    no_history: bool = False
    copy: bool = False
    show_history: bool = False
    replay: str | None = None
    system_prompt: str | None = None
    output: str = OUTPUT_PLAIN
    quiet: bool = False
    search: str | None = None
    export: str | None = None
    name: str | None = None
    update_system_prompt: bool = False
    uninstall: bool = False
    backend: str | None = None
    status: bool = False
    xml_tags: bool = False


# ---------------------------------------------------------------------------
# Argparse plumbing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for every PRD §5.4 flag."""
    parser = argparse.ArgumentParser(
        prog="promptpal",
        description=(
            "Improve prompts via the Claude CLI or the Anthropic API. "
            "Run `promptpal --status` to confirm which backend is active."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Prompt text to improve. If omitted, read from stdin or prompt interactively.",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Override default model (Config: default_model).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        metavar="N",
        help="Run N auto-iterations before presenting the interactive choice.",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Skip writing the session file and index entry for this run.",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy the final improved prompt to the clipboard on accept.",
    )
    parser.add_argument(
        "--show-history",
        action="store_true",
        help="Print a paginated list of sessions (newest first) and exit.",
    )
    parser.add_argument(
        "--replay",
        default=None,
        metavar="SESSION_ID",
        help="Replay a saved session and enter the refinement loop.",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        metavar="FILE",
        help="Override Config.system_prompt_path for this invocation only.",
    )
    parser.add_argument(
        "--output",
        choices=OUTPUT_FORMATS,
        default=OUTPUT_PLAIN,
        help=f"Output format. Default: {OUTPUT_PLAIN}.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress diff, spinner, streaming, and choice line. "
            "Emit only the improved prompt on stdout; auto-accept after the first turn."
        ),
    )
    parser.add_argument(
        "--search",
        default=None,
        metavar="KEYWORD",
        help="Search history for KEYWORD, print results, and exit.",
    )
    parser.add_argument(
        "--export",
        default=None,
        metavar="SESSION_ID",
        help="Dump the full session JSON to stdout and exit.",
    )
    parser.add_argument(
        "--name",
        default=None,
        metavar="LABEL",
        help="Assign a human-readable label to the session.",
    )
    parser.add_argument(
        "--update-system-prompt",
        action="store_true",
        help="Fetch and verify a new system prompt from Config.system_prompt_update_url.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the installed binary and (with confirmation) ~/.promptpal/.",
    )
    parser.add_argument(
        "--backend",
        choices=BACKEND_CHOICES,
        default=None,
        help="Force a backend. 'auto' clears persisted preference; others persist after success.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print backend, model, auth, platform, config path, history count, then exit.",
    )
    parser.add_argument(
        "--xml-tags",
        action="store_true",
        help=(
            "Let the model use XML-style tags (<task>, <input>, ...) to "
            "structure the rewrite. Default: plain section headings only."
        ),
    )

    return parser


def parse_args(argv: Sequence[str] | None = None) -> CLIOptions:
    """Parse ``argv`` (or :data:`sys.argv` when ``None``) into :class:`CLIOptions`."""
    ns = build_parser().parse_args(argv)
    return CLIOptions(
        prompt=ns.prompt,
        model=ns.model,
        iterations=ns.iterations,
        no_history=ns.no_history,
        copy=ns.copy,
        show_history=ns.show_history,
        replay=ns.replay,
        system_prompt=ns.system_prompt,
        output=ns.output,
        quiet=ns.quiet,
        search=ns.search,
        export=ns.export,
        name=ns.name,
        update_system_prompt=ns.update_system_prompt,
        uninstall=ns.uninstall,
        backend=ns.backend,
        status=ns.status,
        xml_tags=ns.xml_tags,
    )


# ---------------------------------------------------------------------------
# Config + path helpers
# ---------------------------------------------------------------------------


def _config_overrides_from_options(options: CLIOptions) -> dict[str, object]:
    """Translate :class:`CLIOptions` into a Config-override dict.

    Only the fields a flag legitimately overrides on Config are emitted —
    e.g. ``--model`` overrides ``default_model``; the no-history /
    quiet / copy flags are *not* config overrides because they
    intentionally don't persist. ``preferred_backend`` lives in
    :func:`core.resolve.resolve_backend`'s persistence helpers, not here.
    """
    overrides: dict[str, object] = {}
    if options.model is not None:
        overrides["default_model"] = options.model
    if options.iterations is not None:
        overrides["default_iterations"] = options.iterations
    if options.system_prompt is not None:
        overrides["system_prompt_path"] = options.system_prompt
    if options.no_history:
        overrides["history_enabled"] = False
    return overrides


def _expand(path: str | Path) -> Path:
    return Path(path).expanduser()


# ---------------------------------------------------------------------------
# --status helpers (AC #6, NFR-09, P1-PLAT-09)
# ---------------------------------------------------------------------------


def _platform_label(platform: Platform) -> str:
    """Return the human-friendly platform string for ``--status``'s first line.

    P1-PLAT-09 fixes WSL1/WSL2/Linux/macOS as the displayed values. The
    Platform snapshot only carries WSL info, so non-WSL detection falls
    back to :data:`sys.platform` (``'darwin'`` → macOS, otherwise Linux).
    """
    if platform.is_wsl:
        return f"WSL{platform.wsl_version}" if platform.wsl_version else "WSL"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


def _history_count(history_dir: Path) -> int:
    """Return the number of index entries on disk; missing index → 0."""
    if not index_path(history_dir).exists():
        return 0
    try:
        return len(read_index(history_dir))
    except (OSError, json.JSONDecodeError):
        return 0


def cmd_status(
    *,
    options: CLIOptions,
    config: Config,
    config_path: Path,
    history_dir: Path,
    platform: Platform,
    stdout: TextIO,
    stderr: TextIO,
    backend_factory: Callable[[Config, CLIOptions, Platform], Backend],
) -> int:
    """Print the ``--status`` summary and return exit code 0.

    The output is < 20 lines per NFR-09. The first line is ``Platform:
    ...`` per P1-PLAT-09. ``Auth: ok | failed`` is the result of
    ``backend.check_auth()`` — failures surface here instead of crashing
    the run, which is the whole point of ``--status``.
    """
    print(f"Platform: {_platform_label(platform)}", file=stdout)

    try:
        backend = backend_factory(config, options, platform)
    except (CliNotFoundError, ApiKeyMissingError, NoBackendError) as e:
        print("Backend: (unavailable)", file=stdout)
        print(f"  Reason: {e}".replace("\n", " "), file=stdout)
        print(f"Model: {config.default_model}", file=stdout)
        print(f"Config: {config_path}", file=stdout)
        print(f"History: {_history_count(history_dir)} sessions", file=stdout)
        return EXIT_OK

    print(f"Backend: {backend.name}", file=stdout)
    print(f"Model: {config.default_model}", file=stdout)
    try:
        ok = backend.check_auth()
    except Exception:  # noqa: BLE001 — check_auth is a health probe; any error = failed
        ok = False
    print(f"Auth: {'ok' if ok else 'failed'}", file=stdout)
    print(f"Config: {config_path}", file=stdout)
    print(f"History: {_history_count(history_dir)} sessions", file=stdout)
    return EXIT_OK


# ---------------------------------------------------------------------------
# --show-history (AC #4, P1-HIST-07-adjacent)
# ---------------------------------------------------------------------------


def _format_index_row(entry: IndexEntry) -> str:
    """One-line summary of an index entry.

    Format: ``<created_at>  [<status>]  <id8>  <label or preview>``. The
    ``id8`` is the first 8 chars of the session id — enough to disambiguate
    inside a 500-entry index without forcing the user to read a 32-char hex.
    """
    label_or_preview = entry.label if entry.label else entry.original_prompt_preview
    return (
        f"{entry.created_at}  [{entry.status}]  "
        f"{entry.session_id[:8]}  {label_or_preview}"
    )


def cmd_show_history(
    *,
    history_dir: Path,
    stdout: TextIO,
    page_size: int = SHOW_HISTORY_PAGE_SIZE,
) -> int:
    """Print the newest-first index list and return exit code 0.

    Empty history → a single ``(no history yet)`` line. Lists longer
    than ``page_size`` truncate with a footer mentioning the omitted
    count; SPEC §9 leaves the "interactive paginator" out of Phase 1.
    """
    entries = read_index(history_dir)
    if not entries:
        print("(no history yet)", file=stdout)
        return EXIT_OK

    entries.sort(key=lambda e: e.created_at, reverse=True)
    shown = entries[:page_size]
    for entry in shown:
        print(_format_index_row(entry), file=stdout)
    remaining = len(entries) - len(shown)
    if remaining > 0:
        print(
            f"... ({remaining} older sessions not shown — use --search to filter)",
            file=stdout,
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# --search (AC #5, P1-HIST-07)
# ---------------------------------------------------------------------------


def cmd_search(
    *,
    keyword: str,
    history_dir: Path,
    stdout: TextIO,
) -> int:
    """Print sessions matching ``keyword`` (newest-first) and return exit 0.

    Delegates to :func:`core.history.search_history`, which implements
    the two-stage matching strategy from P1-HIST-07: index fields
    first, then a fallback scan of session bodies.
    """
    results = search_history(history_dir, keyword)
    if not results:
        print(f"(no sessions matched {keyword!r})", file=stdout)
        return EXIT_OK
    for entry in results:
        print(_format_index_row(entry), file=stdout)
    return EXIT_OK


# ---------------------------------------------------------------------------
# --export (AC #7)
# ---------------------------------------------------------------------------


def _ambiguous_id_message(e: AmbiguousSessionIdError, *, sample_n: int = 3) -> str:
    """Format the shared ambiguous-prefix error from the exception."""
    sample = ", ".join(e.matches[:sample_n])
    if len(e.matches) > sample_n:
        sample += ", ..."
    return AMBIGUOUS_SESSION_ID_TEMPLATE.format(
        prefix=e.prefix, count=len(e.matches), sample=sample
    )


def cmd_export(
    *,
    session_id: str,
    history_dir: Path,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Dump the full session JSON for ``session_id`` to stdout, exit 0/1.

    ``session_id`` may be a full id or a unique prefix (the 8-char id from
    ``--show-history`` resolves here). Returns 1 with an error on
    ``stderr`` when the session is missing, ambiguous, or unreadable; the
    body of the JSON file is the source of truth so we re-serialize
    through :func:`Session.to_dict` for canonical formatting.
    """
    try:
        full_id = resolve_session_id(session_id, history_dir)
        session = read_session(full_id, history_dir)
    except AmbiguousSessionIdError as e:
        print(_ambiguous_id_message(e), file=stderr)
        return EXIT_FAILURE
    except SessionNotFoundError:
        print(
            EXPORT_NOT_FOUND_TEMPLATE.format(session_id=session_id),
            file=stderr,
        )
        return EXIT_FAILURE
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"Error: could not read session {session_id!r}: {e}",
            file=stderr,
        )
        return EXIT_FAILURE
    print(
        json.dumps(session.to_dict(), indent=2, ensure_ascii=False),
        file=stdout,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# --replay (P1-LOOP-*, US-015)
# ---------------------------------------------------------------------------


def _seed_messages_from_session(session: Session) -> tuple[list[Message], str]:
    """Replay a saved session's turns into a fresh ``messages`` list.

    Returns ``(messages, last_assistant_text)``. The ``messages`` list
    is a list-of-dicts projection of the saved ``turns`` in the shape
    the loop and the backends expect (``{"role": ..., "content": ...}``);
    the second element is the most recent assistant ``content`` so the
    caller can pass it to ``run_refinement_loop`` as ``initial_improved``
    without re-walking the list.

    Turns whose role is anything other than ``"user"`` or ``"assistant"``
    (defensive — shouldn't appear in real sessions) are dropped silently
    rather than crashing the replay.
    """
    messages: list[Message] = []
    last_assistant: str | None = None
    for t in session.turns:
        if t.role not in ("user", "assistant"):
            continue
        messages.append({"role": t.role, "content": t.content})
        if t.role == "assistant":
            last_assistant = t.content
    return messages, (last_assistant or "")


def cmd_replay(
    *,
    session_id: str,
    options: CLIOptions,
    config: Config,
    config_path: Path,
    history_dir: Path,
    usage_log_path: Path,
    system_prompt_path: Path,
    platform: Platform,
    backend: Backend,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    copy_fn: Callable[[str], bool],
    clock: Callable[[], str] | None,
    id_factory: Callable[[], str] | None,
) -> int:
    """Run ``--replay SESSION_ID``: load the session, enter the refinement loop.

    AC #3 (US-015) — "load specified session, replay messages into a
    *new* session, enter refinement loop". The source session is read-
    only; a fresh :class:`Session` is created with a new ``session_id``
    and the source's turns are folded in. ``--name LABEL`` overrides the
    label on the new session (the source's label is intentionally not
    carried forward — a replay represents a new line of refinement).

    Failure modes (printed to stderr, return :data:`EXIT_FAILURE`):

    - Source session not found → :data:`REPLAY_NOT_FOUND_TEMPLATE`
    - Source session corrupt/unreadable → ``"Error: could not read ..."``
    - Source session has no assistant turn → :data:`REPLAY_EMPTY_TEMPLATE`
      (the loop can't enter without an ``initial_improved`` value to
      display first).
    """
    try:
        full_id = resolve_session_id(session_id, history_dir)
        source = read_session(full_id, history_dir)
    except AmbiguousSessionIdError as e:
        print(_ambiguous_id_message(e), file=stderr)
        return EXIT_FAILURE
    except SessionNotFoundError:
        print(
            REPLAY_NOT_FOUND_TEMPLATE.format(session_id=session_id),
            file=stderr,
        )
        return EXIT_FAILURE
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"Error: could not read session {session_id!r}: {e}",
            file=stderr,
        )
        return EXIT_FAILURE

    messages, initial_improved = _seed_messages_from_session(source)
    if not initial_improved:
        print(
            REPLAY_EMPTY_TEMPLATE.format(session_id=session_id),
            file=stderr,
        )
        return EXIT_FAILURE

    try:
        system = read_system_prompt(system_prompt_path)
    except SystemPromptMissingError as e:
        print(str(e), file=stderr)
        return EXIT_FAILURE
    system = apply_xml_tags_directive(system, enabled=options.xml_tags)

    backend_short = _short_backend_name(backend)

    # Build the *new* session that this replay produces. The original
    # session on disk is left untouched (read-only source-of-truth);
    # all writes below land at the new session_id.
    new_sess = new_session(
        original_prompt=source.original_prompt,
        model=config.default_model,
        backend=backend_short,
        label=options.name,
        clock=clock,
        id_factory=id_factory,
    )
    # Fold the source's turns into the new session in order so the
    # written file reflects the full replayed history. Token counts and
    # per-turn backend names from the source carry through verbatim —
    # they describe what *originally* produced those turns, not the
    # replay backend.
    for t in source.turns:
        if t.role not in ("user", "assistant"):
            continue
        new_sess = append_turn(
            new_sess,
            role=t.role,
            content=t.content,
            backend=t.backend,
            input_tokens=t.input_tokens,
            output_tokens=t.output_tokens,
            clock=clock,
        )

    if config.history_enabled and not options.no_history:
        _try_write_session(new_sess, history_dir, stderr=stderr)

    iterations = options.iterations if options.iterations is not None else 0

    try:
        outcome = run_refinement_loop(
            backend=backend,
            system=system,
            initial_messages=messages,
            initial_improved=initial_improved,
            original_prompt=source.original_prompt,
            auto_iterations=max(0, iterations),
            copy_on_accept=(options.copy or config.auto_copy),
            stdin=stdin,
            stderr=stderr,
            copy_fn=copy_fn,
        )
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=stderr)
        return EXIT_FAILURE

    new_sess = _fold_outcome_into_session(
        new_sess, outcome=outcome, backend_short=backend_short, clock=clock
    )
    final_status = (
        STATUS_ACCEPTED if outcome.status == STATUS_ACCEPTED else "discarded"
    )
    new_sess = finalize_session(
        new_sess,
        status=final_status,
        final_prompt=outcome.final_prompt
        if outcome.status == STATUS_ACCEPTED
        else None,
        clock=clock,
    )

    if config.history_enabled and not options.no_history:
        for offset, lt in enumerate(outcome.new_turns, start=1):
            try:
                append_usage_entry(
                    usage_log_path,
                    session_id=new_sess.session_id,
                    turn_index=offset,
                    backend=backend_short,
                    model=config.default_model,
                    input_tokens=lt.input_tokens,
                    output_tokens=lt.output_tokens,
                    clock=clock,
                )
            except OSError:
                pass
        _try_write_session(new_sess, history_dir, stderr=stderr)
        try:
            enforce_max_entries(history_dir, config.max_history_entries)
        except OSError:
            pass

    if outcome.status == STATUS_ACCEPTED:
        print(
            format_output(
                output=options.output,
                original=source.original_prompt,
                improved=outcome.final_prompt,
                turns=len(source.turns) + len(outcome.new_turns),
                session_id=new_sess.session_id,
                backend_name=backend_short,
                model=config.default_model,
            ),
            file=stdout,
        )
    return EXIT_OK if outcome.status == STATUS_ACCEPTED else EXIT_DISCARDED


# ---------------------------------------------------------------------------
# --update-system-prompt (P1-SP-03, P1-ERR-14, P1-ERR-15)
# ---------------------------------------------------------------------------


def cmd_update_system_prompt(
    *,
    config: Config,
    target_path: Path,
    stderr: TextIO,
    stdout: TextIO,
    fetcher: Callable[[str], bytes] | None,
) -> int:
    """Run the ``--update-system-prompt`` flow; return 0 on success, 1 on failure."""
    try:
        backup = update_system_prompt(
            config.system_prompt_update_url,
            target_path,
            fetcher=fetcher,
        )
    except SystemPromptChecksumError as e:
        print(str(e), file=stderr)
        return EXIT_FAILURE
    except SystemPromptDownloadError as e:
        print(str(e), file=stderr)
        return EXIT_FAILURE
    except SystemPromptError as e:
        print(str(e), file=stderr)
        return EXIT_FAILURE
    print(UPDATE_SUCCESS_TEMPLATE.format(path=target_path), file=stdout)
    if backup is not None:
        print(UPDATE_BACKUP_TEMPLATE.format(path=backup), file=stderr)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Output formatting (AC #2 — plain / json / markdown)
# ---------------------------------------------------------------------------


def format_output(
    *,
    output: str,
    original: str,
    improved: str,
    turns: int,
    session_id: str,
    backend_name: str,
    model: str,
) -> str:
    """Return the stdout payload for ``options.output``.

    - ``plain``    → improved prompt verbatim, terminated by one newline.
    - ``json``     → single JSON object with the exact six keys from
                     AC #2: ``original`` / ``improved`` / ``turns`` /
                     ``session_id`` / ``backend`` / ``model``.
    - ``markdown`` → a fenced code block around the improved prompt.
    """
    if output == OUTPUT_JSON:
        payload = {
            "original": original,
            "improved": improved,
            "turns": turns,
            "session_id": session_id,
            "backend": backend_name,
            "model": model,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)
    if output == OUTPUT_MARKDOWN:
        return f"```\n{improved}\n```"
    return improved


# ---------------------------------------------------------------------------
# Backend factory (test seam)
# ---------------------------------------------------------------------------


def _default_backend_factory(
    config: Config, options: CLIOptions, platform: Platform
) -> Backend:
    """Production backend factory: thin wrapper around :func:`resolve_backend`.

    Tests inject a different factory to avoid spinning up real backends.
    """
    return resolve_backend(
        options.backend, config.default_model, config=config
    )


def _short_backend_name(backend: Backend) -> str:
    """Return the canonical short backend identifier for the session schema.

    Backend names look like ``"claude-cli (claude-sonnet-4-6)"`` /
    ``"api-key (claude-sonnet-4-6)"`` — the SPEC §4 session schema and
    P1-BKND-10 want only the first token (``"claude-cli"`` /
    ``"api-key"``). Anything without a space passes through verbatim so
    a future test-shaped backend name still works.
    """
    name = backend.name
    if " " in name:
        return name.split(" ", 1)[0]
    return name


# ---------------------------------------------------------------------------
# Session folding helpers (loop outcome → Session)
# ---------------------------------------------------------------------------


def _fold_outcome_into_session(
    session: Session,
    *,
    outcome: LoopOutcome,
    backend_short: str,
    clock: Callable[[], str] | None,
) -> Session:
    """Append every loop turn (as a user+assistant pair) to ``session``.

    The loop tracks turns as ``LoopTurn`` records; ``user_content`` is
    the feedback string that triggered the assistant response. We fold
    them into the session's ``turns`` tuple in the order they were
    produced so ``--replay`` can faithfully reconstruct the conversation.

    The ``backend_short`` argument is the canonical short form
    (``"claude-cli"`` / ``"api-key"``) — see :func:`_short_backend_name`.
    The ``LoopTurn.backend`` field carries the full human-readable name
    from ``Backend.name`` and is not what the session schema wants.
    """
    folded = session
    for lt in outcome.new_turns:
        folded = append_turn(
            folded,
            role="user",
            content=lt.user_content,
            backend=backend_short,
            input_tokens=None,
            output_tokens=None,
            clock=clock,
        )
        folded = append_turn(
            folded,
            role="assistant",
            content=lt.response_text,
            backend=backend_short,
            input_tokens=lt.input_tokens,
            output_tokens=lt.output_tokens,
            clock=clock,
        )
    return folded


# ---------------------------------------------------------------------------
# History persistence wrapper (P1-HIST-08 — non-fatal)
# ---------------------------------------------------------------------------


def _try_write_session(
    session: Session,
    history_dir: Path,
    *,
    stderr: TextIO,
) -> None:
    """Write the session + index entry; warn on failure but never raise.

    P1-HIST-08 mandates that history failures must not abort the run.
    ``HISTORY_WRITE_WARNING`` is the canonical P1-ERR-07 message and is
    pinned by tests.

    Split into two independently-guarded writes:

    1. ``write_session`` — failure aborts the helper with a warning;
       nothing else to clean up because the index hasn't been touched yet.
    2. ``upsert_index_entry`` — failure removes the freshly-written
       session file so it doesn't become a silent orphan that
       ``--show-history`` / ``--search`` can never surface.

    The catch is widened from ``OSError`` to ``Exception`` so a non-IO
    failure in ``upsert_index_entry`` (``TypeError`` from a schema
    drift, ``AttributeError`` from a malformed Session, future
    schema-related errors) is treated as a history-degraded warning
    instead of aborting the whole run.
    """
    try:
        write_session(session, history_dir)
    except Exception as e:
        print(f"{HISTORY_WRITE_WARNING} (reason: {e})", file=stderr)
        return
    try:
        upsert_index_entry(history_dir, index_entry_from_session(session))
    except Exception as e:
        print(f"{HISTORY_WRITE_WARNING} (reason: {e})", file=stderr)
        # Orphan cleanup: the session landed but the index didn't.
        # Remove the orphan so future --show-history / --search aren't
        # blind to it forever. Best-effort — if removal fails too, the
        # session is at worst a stale orphan, not active corruption.
        try:
            session_path(history_dir, session.session_id).unlink()
        except (OSError, InvalidSessionIdError):
            pass


# ---------------------------------------------------------------------------
# The main happy path
# ---------------------------------------------------------------------------


def _run_quiet_pipeline(
    *,
    options: CLIOptions,
    config: Config,
    history_dir: Path,
    backend: Backend,
    backend_short: str,
    system: str,
    initial_messages: list[Message],
    first_response: BackendResponse,
    iterations: int,
    original_prompt: str,
    session: Session,
    stdout: TextIO,
    stderr: TextIO,
    copy_fn: Callable[[str], bool],
    clock: Callable[[], str] | None,
) -> int:
    """``--quiet`` execution path (M6, issue #30).

    Auto-accepts after the first turn (AC #3). Honors ``--iterations N``
    by running N synthesized iterations against the same backend and
    taking the last assistant response. No diff, no choice line, no
    stderr chatter from the loop.

    Extracted from ``_run_pipeline`` in slice 5 of the issue-#30
    review-driven refactor so both execution branches stay readable.
    The shared header (read prompt, first turn, build session, persist
    backend preference, incremental write) lives in ``_run_pipeline``
    and is run before this helper is called.
    """
    improved = first_response.text
    messages = list(initial_messages)
    new_turns_count = 0
    try:
        for _ in range(max(0, iterations)):
            messages.append(
                {"role": "user", "content": SYNTHESIZED_FEEDBACK}
            )
            response = backend.complete(
                system, [dict(m) for m in messages]
            )
            messages.append(
                {"role": "assistant", "content": response.text}
            )
            improved = response.text
            session = append_turn(
                session,
                role="user",
                content=SYNTHESIZED_FEEDBACK,
                backend=backend_short,
                input_tokens=None,
                output_tokens=None,
                clock=clock,
            )
            session = append_turn(
                session,
                role="assistant",
                content=response.text,
                backend=backend_short,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                clock=clock,
            )
            new_turns_count += 1
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=stderr)
        return EXIT_FAILURE

    session = finalize_session(
        session,
        status=STATUS_ACCEPTED,
        final_prompt=improved,
        clock=clock,
    )
    if config.history_enabled and not options.no_history:
        _try_write_session(session, history_dir, stderr=stderr)
        try:
            enforce_max_entries(history_dir, config.max_history_entries)
        except OSError:
            pass

    if options.copy or config.auto_copy:
        ok = copy_fn(improved)
        if ok:
            # No stderr chatter in --quiet mode; success is the
            # absence of a warning.
            pass

    print(
        format_output(
            output=options.output,
            original=original_prompt,
            improved=improved,
            turns=1 + new_turns_count,
            session_id=session.session_id,
            backend_name=backend_short,
            model=config.default_model,
        ),
        file=stdout,
    )
    return EXIT_OK


def _run_pipeline(
    *,
    options: CLIOptions,
    config: Config,
    config_path: Path,
    history_dir: Path,
    usage_log_path: Path,
    system_prompt_path: Path,
    platform: Platform,
    backend: Backend,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    copy_fn: Callable[[str], bool],
    clock: Callable[[], str] | None,
    id_factory: Callable[[], str] | None,
) -> int:
    """Read prompt → first turn → (optional loop) → write history → print output."""
    try:
        original_prompt = read_prompt(
            options.prompt,
            stdin=stdin,
            stderr=stderr,
        )
    except EmptyPromptError as e:
        print(str(e), file=stderr)
        return EXIT_FAILURE

    try:
        system = read_system_prompt(system_prompt_path)
    except SystemPromptMissingError as e:
        print(str(e), file=stderr)
        return EXIT_FAILURE
    system = apply_xml_tags_directive(system, enabled=options.xml_tags)

    initial_messages: list[Message] = [
        {"role": "user", "content": original_prompt}
    ]

    try:
        first_response = backend.complete(
            system,
            [dict(m) for m in initial_messages],
            stream=(not options.quiet),
        )
    except Exception as e:  # noqa: BLE001 — surface every backend error uniformly
        print(f"Error: {e}", file=stderr)
        return EXIT_FAILURE

    # Persist the explicit backend choice now that the first turn succeeded.
    if options.backend in (BACKEND_CLI, BACKEND_API):
        try:
            persist_backend_preference(config_path, options.backend)  # type: ignore[arg-type]
        except (OSError, ConfigCorruptError):
            # M2 (issue #30): persist_backend_preference internally calls
            # load_config, which can raise ConfigCorruptError. Config-write
            # / load failures are non-fatal here — the run already produced
            # a response. Surface a warning to stderr.
            print(
                "Warning: could not persist preferred backend.",
                file=stderr,
            )

    initial_messages.append({"role": "assistant", "content": first_response.text})

    backend_short = _short_backend_name(backend)

    # In-flight session — written incrementally so a kill -9 leaves a
    # partial-but-readable record on disk (P1-HIST-03 / NFR-04). The
    # session's ``backend`` field carries the short form per P1-BKND-10.
    session = new_session(
        original_prompt=original_prompt,
        model=config.default_model,
        backend=backend_short,
        label=options.name,
        clock=clock,
        id_factory=id_factory,
    )
    session = append_turn(
        session,
        role="user",
        content=original_prompt,
        backend=backend_short,
        input_tokens=None,
        output_tokens=None,
        clock=clock,
    )
    session = append_turn(
        session,
        role="assistant",
        content=first_response.text,
        backend=backend_short,
        input_tokens=first_response.input_tokens,
        output_tokens=first_response.output_tokens,
        clock=clock,
    )

    if config.history_enabled and not options.no_history:
        _try_write_session(session, history_dir, stderr=stderr)
        try:
            append_usage_entry(
                usage_log_path,
                session_id=session.session_id,
                turn_index=0,
                backend=backend_short,
                model=config.default_model,
                input_tokens=first_response.input_tokens,
                output_tokens=first_response.output_tokens,
                clock=clock,
            )
        except OSError:
            # Usage log writes are non-fatal too; the session already
            # captures the same metadata.
            pass

    iterations = options.iterations if options.iterations is not None else 0

    if options.quiet:
        return _run_quiet_pipeline(
            options=options,
            config=config,
            history_dir=history_dir,
            backend=backend,
            backend_short=backend_short,
            system=system,
            initial_messages=initial_messages,
            first_response=first_response,
            iterations=iterations,
            original_prompt=original_prompt,
            session=session,
            stdout=stdout,
            stderr=stderr,
            copy_fn=copy_fn,
            clock=clock,
        )

    # ----- interactive path (with optional auto-iterations) -----

    if config.show_diff and options.output == OUTPUT_PLAIN:
        if should_show_diff(first_response.text):
            diff_text = format_diff(original_prompt, first_response.text)
            # M16 (issue #30): format_diff returns None for an identical
            # original/improved pair. Pre-fix code emitted a stray blank
            # line; printing the full response text instead would surprise
            # piped consumers of stderr. A brief "(no changes)" sentinel
            # gives the user a real signal without changing the volume.
            if diff_text is not None:
                print(diff_text, file=stderr)
            else:
                print("(no changes)", file=stderr)
        else:
            print(first_response.text, file=stderr)

    try:
        outcome = run_refinement_loop(
            backend=backend,
            system=system,
            initial_messages=initial_messages,
            initial_improved=first_response.text,
            original_prompt=original_prompt,
            auto_iterations=max(0, iterations),
            copy_on_accept=(options.copy or config.auto_copy),
            stdin=stdin,
            stderr=stderr,
            copy_fn=copy_fn,
        )
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=stderr)
        return EXIT_FAILURE

    session = _fold_outcome_into_session(
        session, outcome=outcome, backend_short=backend_short, clock=clock
    )
    final_status = (
        STATUS_ACCEPTED if outcome.status == STATUS_ACCEPTED else "discarded"
    )
    session = finalize_session(
        session,
        status=final_status,
        final_prompt=outcome.final_prompt
        if outcome.status == STATUS_ACCEPTED
        else None,
        clock=clock,
    )

    if config.history_enabled and not options.no_history:
        # Append one usage-log line per loop iteration (P1-HIST-05); the
        # first turn was logged when it landed (incremental write path).
        for offset, lt in enumerate(outcome.new_turns, start=1):
            try:
                append_usage_entry(
                    usage_log_path,
                    session_id=session.session_id,
                    turn_index=offset,
                    backend=backend_short,
                    model=config.default_model,
                    input_tokens=lt.input_tokens,
                    output_tokens=lt.output_tokens,
                    clock=clock,
                )
            except OSError:
                # Usage log writes are non-fatal (P1-HIST-08-adjacent).
                pass
        _try_write_session(session, history_dir, stderr=stderr)
        try:
            enforce_max_entries(history_dir, config.max_history_entries)
        except OSError:
            pass

    if outcome.status == STATUS_ACCEPTED:
        print(
            format_output(
                output=options.output,
                original=original_prompt,
                improved=outcome.final_prompt,
                turns=1 + len(outcome.new_turns),
                session_id=session.session_id,
                backend_name=backend_short,
                model=config.default_model,
            ),
            file=stdout,
        )
    # Discarded: no stdout output. The user explicitly dropped the result.
    return EXIT_OK if outcome.status == STATUS_ACCEPTED else EXIT_DISCARDED


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    config_path: str | Path | None = None,
    history_dir: str | Path | None = None,
    usage_log_path: str | Path | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    detect_platform_fn: Callable[[], Platform] | None = None,
    backend_factory: Callable[[Config, CLIOptions, Platform], Backend] | None = None,
    fetcher: Callable[[str], bytes] | None = None,
    clock: Callable[[], str] | None = None,
    id_factory: Callable[[], str] | None = None,
    copy_fn: Callable[[str], bool] | None = None,
    skip_wsl_guard: bool = False,
) -> int:
    """Top-level CLI entry point. Returns the process exit code.

    Every external dependency is injectable via kwargs so tests run the
    full pipeline without touching the real filesystem, environment, or
    network. Production callers pass nothing.

    The ``skip_wsl_guard`` kwarg defaults to ``False`` (production runs
    enforce :func:`assert_wsl_home_safe`); tests pass ``True`` to keep
    the suite portable across non-WSL CI runners.
    """
    options = parse_args(argv)

    stdin_in: TextIO = stdin if stdin is not None else sys.stdin
    stdout_in: TextIO = stdout if stdout is not None else sys.stdout
    stderr_in: TextIO = stderr if stderr is not None else sys.stderr
    detect_fn: Callable[[], Platform] = (
        detect_platform_fn if detect_platform_fn is not None else detect_platform
    )
    factory: Callable[[Config, CLIOptions, Platform], Backend] = (
        backend_factory
        if backend_factory is not None
        else _default_backend_factory
    )

    platform = detect_fn()
    if not skip_wsl_guard:
        assert_wsl_home_safe(platform)

    # Resolve paths relative to detected HOME so the WSL-safe HOME
    # resolution propagates everywhere.
    config_path_p = (
        Path(config_path).expanduser()
        if config_path is not None
        else Path(platform.home) / ".promptpal" / "config.json"
    )
    history_dir_p = (
        Path(history_dir).expanduser()
        if history_dir is not None
        else Path(platform.home) / ".promptpal" / "history"
    )
    usage_log_p = (
        Path(usage_log_path).expanduser()
        if usage_log_path is not None
        else Path(platform.home) / ".promptpal" / "usage.log"
    )

    history_dir_p.mkdir(parents=True, exist_ok=True)

    try:
        base_config = load_config(config_path_p)
    except ConfigCorruptError as e:
        print(str(e), file=stderr_in)
        return EXIT_FAILURE

    config = apply_overrides(
        base_config, _config_overrides_from_options(options)
    )

    # ----- early-exit flags (independent of the main pipeline) -----

    if options.status:
        return cmd_status(
            options=options,
            config=config,
            config_path=config_path_p,
            history_dir=history_dir_p,
            platform=platform,
            stdout=stdout_in,
            stderr=stderr_in,
            backend_factory=factory,
        )

    if options.show_history:
        return cmd_show_history(
            history_dir=history_dir_p,
            stdout=stdout_in,
        )

    if options.search is not None:
        return cmd_search(
            keyword=options.search,
            history_dir=history_dir_p,
            stdout=stdout_in,
        )

    if options.export is not None:
        return cmd_export(
            session_id=options.export,
            history_dir=history_dir_p,
            stdout=stdout_in,
            stderr=stderr_in,
        )

    if options.update_system_prompt:
        target = resolve_system_prompt_path(
            config, cli_override=options.system_prompt
        )
        return cmd_update_system_prompt(
            config=config,
            target_path=target,
            stderr=stderr_in,
            stdout=stdout_in,
            fetcher=fetcher,
        )

    if options.uninstall:
        # The actual removal logic lives in ``uninstall.sh`` (US-015) so
        # the Python process doesn't have to delete itself mid-run. The
        # flag stays wired so ``--help`` advertises it and the parser
        # doesn't reject it; ``UNINSTALL_NOT_IMPLEMENTED`` points the
        # user at the right script.
        print(UNINSTALL_NOT_IMPLEMENTED, file=stderr_in)
        return EXIT_FAILURE

    # ----- normal pipeline -----

    # --backend auto: clear persistence BEFORE resolving (per resolve.py
    # contract — explicit-auto is the user's "stop persisting" signal).
    # Then reload so the just-written preference is visible to
    # resolve_backend. Both steps are gated on the BACKEND_AUTO branch
    # per M5 (issue #30) — the previous unconditional reload was a
    # double-read on every run.
    if options.backend == BACKEND_AUTO:
        try:
            clear_backend_preference(config_path_p)
        except (OSError, ConfigCorruptError):
            # M2 (issue #30): clear_backend_preference internally calls
            # load_config which can raise ConfigCorruptError.
            print(
                "Warning: could not reset preferred backend.",
                file=stderr_in,
            )
        # M1 (issue #30): the reload path must catch ConfigCorruptError
        # too — the file may have become corrupt between the first load
        # and this point (concurrent --clear-config, external edit, or
        # disk corruption after the clear_backend_preference write).
        try:
            config = apply_overrides(
                load_config(config_path_p),
                _config_overrides_from_options(options),
            )
        except ConfigCorruptError as e:
            print(str(e), file=stderr_in)
            return EXIT_FAILURE

    system_prompt_path_resolved = resolve_system_prompt_path(
        config, cli_override=options.system_prompt
    )
    if options.system_prompt is None:
        # Auto-seed the default system prompt on first run (P1-SP-01).
        # --system-prompt FILE explicitly aims at a user file — we do
        # NOT seed at that path.
        try:
            seed_system_prompt(system_prompt_path_resolved)
        except OSError as e:
            print(
                f"Warning: could not seed system prompt: {e}",
                file=stderr_in,
            )

    try:
        backend = factory(config, options, platform)
    except (CliNotFoundError, ApiKeyMissingError, NoBackendError) as e:
        print(str(e), file=stderr_in)
        return EXIT_FAILURE

    copy_fn_resolved: Callable[[str], bool] = (
        copy_fn
        if copy_fn is not None
        else (lambda text: copy_to_clipboard(text, platform))
    )

    if options.replay is not None:
        return cmd_replay(
            session_id=options.replay,
            options=options,
            config=config,
            config_path=config_path_p,
            history_dir=history_dir_p,
            usage_log_path=usage_log_p,
            system_prompt_path=system_prompt_path_resolved,
            platform=platform,
            backend=backend,
            stdin=stdin_in,
            stdout=stdout_in,
            stderr=stderr_in,
            copy_fn=copy_fn_resolved,
            clock=clock,
            id_factory=id_factory,
        )

    return _run_pipeline(
        options=options,
        config=config,
        config_path=config_path_p,
        history_dir=history_dir_p,
        usage_log_path=usage_log_p,
        system_prompt_path=system_prompt_path_resolved,
        platform=platform,
        backend=backend,
        stdin=stdin_in,
        stdout=stdout_in,
        stderr=stderr_in,
        copy_fn=copy_fn_resolved,
        clock=clock,
        id_factory=id_factory,
    )


# ``python -m core.cli`` ergonomics — kept for direct debugging.
# Production entry is ``python -m core.main`` via the bin/promptpal
# launcher (US-012).
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
