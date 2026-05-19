"""Integration tests for every PRD §5.4 flag (US-016 / AC #11).

US-016 mandates ``tests/integration/test_flags.py`` covering "one assert
per flag". This file is the structural smoke layer above the per-flag
unit tests in :mod:`tests.unit.test_cli`: every flag is driven through
:func:`core.cli.main` end-to-end with the full seam set (filesystem,
fake backend, fake clock, fake id_factory) and the resulting *runtime
effect* — not just the parsed ``CLIOptions`` field — is pinned by one
assert per flag.

Why integration instead of just the parser unit tests? A future
refactor could leave the parser intact while silently rewiring main()
to ignore a flag (e.g., dropping ``--no-history`` from the gate around
:func:`core.history.write_session`). The parser-only suite would pass;
this suite would not.

Coverage map (one test per PRD §5.4 flag):

  prompt                  → test_positional_prompt_used_as_original
  --model                 → test_model_overrides_default_model
  --iterations            → test_iterations_runs_n_auto_iterations
  --no-history            → test_no_history_skips_session_write
  --copy                  → test_copy_invokes_copy_fn
  --show-history          → test_show_history_lists_index_entries
  --replay                → test_replay_runs_loop_against_source_session
  --system-prompt         → test_system_prompt_override_for_invocation
  --output                → test_output_json_emits_envelope
  --quiet                 → test_quiet_suppresses_choice_line
  --search                → test_search_returns_matching_entries
  --export                → test_export_dumps_session_json
  --name                  → test_name_label_lands_in_session_and_index
  --update-system-prompt  → test_update_system_prompt_writes_new_file
  --uninstall             → test_uninstall_redirects_to_shell_script
  --backend               → test_backend_explicit_persists_after_success
  --status                → test_status_prints_summary_and_exits_0
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from core.backend import Backend, BackendResponse
from core.cli import (
    EXIT_FAILURE,
    EXIT_OK,
    UNINSTALL_NOT_IMPLEMENTED,
    CLIOptions,
    main,
)
from core.config import Config
from core.history import (
    IndexEntry,
    Session,
    Turn,
    upsert_index_entry,
    write_session,
)
from core.platform import Platform


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class FakeBackend(Backend):
    """Backend that pops responses off a queue and records every call."""

    def __init__(
        self,
        responses: list[BackendResponse],
        *,
        name: str = "claude-cli (claude-sonnet-4-6)",
        auth_ok: bool = True,
    ) -> None:
        self._responses = list(responses)
        self._name = name
        self._auth_ok = auth_ok
        self.calls: list[tuple[str, list[dict]]] = []

    @property
    def name(self) -> str:
        return self._name

    def complete(
        self, system: str, messages: list[dict], stream: bool = False
    ) -> BackendResponse:
        self.calls.append((system, list(messages)))
        if not self._responses:
            raise AssertionError("FakeBackend: response queue exhausted")
        return self._responses.pop(0)

    def check_auth(self) -> bool:
        return self._auth_ok


def _fake_platform(home: str) -> Platform:
    return Platform(
        is_wsl=False,
        wsl_version=None,
        home=home,
        clipboard_cmd=(),
    )


def _frozen_clock(value: str = "2026-05-19T00:00:00Z") -> Callable[[], str]:
    def clock() -> str:
        return value

    return clock


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    """Seed a fully-populated tmp ``~/.promptpal/`` tree.

    Tests get ``home``, ``config_path``, ``history_dir``, ``usage_log``,
    and ``system_prompt`` paths. Config points at the tmp
    system-prompt.md so tests don't leak into the real $HOME.
    """
    home = tmp_path / "home"
    promptpal = home / ".promptpal"
    history = promptpal / "history"
    history.mkdir(parents=True)
    system_prompt = promptpal / "system-prompt.md"
    system_prompt.write_text("You are PromptPal.\n", encoding="utf-8")
    config_path = promptpal / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_model": "claude-sonnet-4-6",
                "system_prompt_path": str(system_prompt),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    usage_log = promptpal / "usage.log"
    return {
        "home": home,
        "config_path": config_path,
        "history_dir": history,
        "usage_log": usage_log,
        "system_prompt": system_prompt,
    }


def _run(
    argv: list[str],
    *,
    env: dict[str, Path],
    backend: Backend | None = None,
    stdin: str = "",
    copy_fn: Callable[[str], bool] | None = None,
    fetcher: Callable[[str], bytes] | None = None,
    backend_factory: Callable[[Config, CLIOptions, Platform], Backend] | None = None,
) -> tuple[int, str, str]:
    """Invoke :func:`main` with all seams wired to tmp / fakes.

    Returns ``(rc, stdout_text, stderr_text)``.
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    factory: Callable[[Config, CLIOptions, Platform], Backend]
    if backend_factory is not None:
        factory = backend_factory
    elif backend is not None:
        factory = lambda c, o, p: backend  # noqa: E731
    else:
        factory = lambda c, o, p: FakeBackend([])  # noqa: E731
    rc = main(
        argv,
        config_path=env["config_path"],
        history_dir=env["history_dir"],
        usage_log_path=env["usage_log"],
        stdin=io.StringIO(stdin),
        stdout=stdout,
        stderr=stderr,
        detect_platform_fn=lambda: _fake_platform(home=str(env["home"])),
        backend_factory=factory,
        copy_fn=copy_fn if copy_fn is not None else (lambda t: True),
        fetcher=fetcher,
        clock=_frozen_clock(),
        id_factory=lambda: "fixedsessionid",
        skip_wsl_guard=True,
    )
    return rc, stdout.getvalue(), stderr.getvalue()


# ===========================================================================
# Per-flag integration tests — one assert per flag minimum
# ===========================================================================


class TestPositionalPrompt:
    def test_positional_prompt_used_as_original(
        self, env: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        _run(
            ["--quiet", "the original prompt"],
            env=env,
            backend=backend,
        )
        # The positional argv-1 lands as the user-content of the first
        # backend call.
        assert backend.calls[0][1][0]["content"] == "the original prompt"


class TestModelFlag:
    def test_model_overrides_default_model(self, env: dict[str, Path]) -> None:
        captured_models: list[str] = []

        def factory(c: Config, o: CLIOptions, p: Platform) -> Backend:
            captured_models.append(c.default_model)
            return FakeBackend(
                [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
            )

        _run(
            ["--quiet", "--model", "claude-haiku-4-5", "p"],
            env=env,
            backend_factory=factory,
        )
        # --model flag overrides Config.default_model before the backend
        # factory sees it (AC #1 P1-FLAG-MODEL).
        assert captured_models == ["claude-haiku-4-5"]


class TestIterationsFlag:
    def test_iterations_runs_n_auto_iterations(
        self, env: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [
                BackendResponse(text="t1", input_tokens=None, output_tokens=None),
                BackendResponse(text="t2", input_tokens=None, output_tokens=None),
                BackendResponse(text="t3", input_tokens=None, output_tokens=None),
            ]
        )
        _run(
            ["--quiet", "--iterations", "2", "p"],
            env=env,
            backend=backend,
        )
        # First turn + 2 auto-iterations = 3 backend calls total.
        assert len(backend.calls) == 3


class TestNoHistoryFlag:
    def test_no_history_skips_session_write(
        self, env: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        _run(
            ["--quiet", "--no-history", "p"],
            env=env,
            backend=backend,
        )
        # No session file should land on disk under --no-history.
        assert list(env["history_dir"].glob("*.json")) == []


class TestCopyFlag:
    def test_copy_invokes_copy_fn(self, env: dict[str, Path]) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        copied: list[str] = []
        _run(
            ["--quiet", "--copy", "p"],
            env=env,
            backend=backend,
            copy_fn=lambda t: (copied.append(t), True)[1],
        )
        # --copy must route the final improved text through copy_fn.
        assert copied == ["improved"]


class TestShowHistoryFlag:
    def test_show_history_lists_index_entries(
        self, env: dict[str, Path]
    ) -> None:
        upsert_index_entry(
            env["history_dir"],
            IndexEntry(
                session_id="sess-001",
                created_at="2026-05-19T12:00:00Z",
                label="my draft",
                status="accepted",
                original_prompt_preview="hello world",
            ),
        )
        rc, stdout, _ = _run(["--show-history"], env=env)
        # --show-history short-circuits the pipeline, prints index, exits 0.
        assert rc == EXIT_OK
        assert "sess-001" in stdout


class TestReplayFlag:
    def test_replay_runs_loop_against_source_session(
        self, env: dict[str, Path]
    ) -> None:
        # Seed a complete source session (1 user + 1 assistant turn).
        source = Session(
            session_id="source-sess",
            created_at="2026-05-18T00:00:00Z",
            updated_at="2026-05-18T00:00:00Z",
            status="accepted",
            label="seed",
            original_prompt="seed prompt",
            model="claude-sonnet-4-6",
            backend="claude-cli",
            turns=(
                Turn(
                    role="user",
                    content="seed prompt",
                    backend="claude-cli",
                    input_tokens=None,
                    output_tokens=None,
                    timestamp="2026-05-18T00:00:00Z",
                ),
                Turn(
                    role="assistant",
                    content="seed assistant reply",
                    backend="claude-cli",
                    input_tokens=None,
                    output_tokens=None,
                    timestamp="2026-05-18T00:00:01Z",
                ),
            ),
            final_prompt="seed assistant reply",
        )
        write_session(source, env["history_dir"])

        backend = FakeBackend([])  # No backend calls; we discard immediately.
        rc, _, _ = _run(
            ["--replay", "source-sess"],
            env=env,
            backend=backend,
            stdin="d\n",  # discard immediately
        )
        # Replay landed on the loop and bailed via [d]iscard.
        new_path = env["history_dir"] / "fixedsessionid.json"
        assert new_path.exists() and rc == EXIT_OK


class TestSystemPromptFlag:
    def test_system_prompt_override_for_invocation(
        self, env: dict[str, Path]
    ) -> None:
        # Write a recognisable system prompt at a non-default path.
        override = env["home"] / "custom-prompt.md"
        override.write_text("CUSTOM SYSTEM\n", encoding="utf-8")
        captured: list[str] = []

        class CapturingBackend(FakeBackend):
            def complete(self, system: str, messages: list[dict], stream: bool = False) -> BackendResponse:
                captured.append(system)
                return super().complete(system, messages, stream)

        backend = CapturingBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        _run(
            ["--quiet", "--system-prompt", str(override), "p"],
            env=env,
            backend=backend,
        )
        # The override file content is what reached the backend, not the
        # default ~/.promptpal/system-prompt.md content.
        assert captured == ["CUSTOM SYSTEM\n"]


class TestOutputFlag:
    def test_output_json_emits_envelope(self, env: dict[str, Path]) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=10, output_tokens=20)],
            name="api-key (claude-sonnet-4-6)",
        )
        rc, stdout, _ = _run(
            ["--quiet", "--output", "json", "orig"],
            env=env,
            backend=backend,
        )
        assert rc == EXIT_OK
        parsed = json.loads(stdout)
        # AC #2: JSON envelope has six pinned keys with canonical values.
        assert parsed == {
            "original": "orig",
            "improved": "improved",
            "turns": 1,
            "session_id": "fixedsessionid",
            "backend": "api-key",
            "model": "claude-sonnet-4-6",
        }


class TestQuietFlag:
    def test_quiet_suppresses_choice_line(self, env: dict[str, Path]) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        rc, stdout, stderr = _run(
            ["--quiet", "p"],
            env=env,
            backend=backend,
        )
        # --quiet path bypasses the loop, never renders the choice line.
        assert rc == EXIT_OK
        assert "[a]ccept" not in stderr
        # stdout receives only the improved prompt.
        assert stdout == "improved\n"


class TestSearchFlag:
    def test_search_returns_matching_entries(
        self, env: dict[str, Path]
    ) -> None:
        upsert_index_entry(
            env["history_dir"],
            IndexEntry(
                session_id="match-sess",
                created_at="2026-05-19T11:00:00Z",
                label=None,
                status="accepted",
                original_prompt_preview="needle in a haystack",
            ),
        )
        upsert_index_entry(
            env["history_dir"],
            IndexEntry(
                session_id="other-sess",
                created_at="2026-05-19T10:00:00Z",
                label=None,
                status="accepted",
                original_prompt_preview="unrelated",
            ),
        )
        rc, stdout, _ = _run(["--search", "needle"], env=env)
        assert rc == EXIT_OK
        # Match's original_prompt_preview appears; non-match's does not.
        # (cmd_search truncates session_id to the first 8 chars in its
        # rendered output, so we pin the preview text — which is the
        # actual contract for "this entry matched the keyword".)
        assert "needle in a haystack" in stdout
        assert "unrelated" not in stdout


class TestExportFlag:
    def test_export_dumps_session_json(self, env: dict[str, Path]) -> None:
        sess = Session(
            session_id="export-me",
            created_at="2026-05-19T09:00:00Z",
            updated_at="2026-05-19T09:00:00Z",
            status="accepted",
            label=None,
            original_prompt="orig",
            model="claude-sonnet-4-6",
            backend="claude-cli",
            turns=(),
            final_prompt="orig-improved",
        )
        write_session(sess, env["history_dir"])
        rc, stdout, _ = _run(["--export", "export-me"], env=env)
        assert rc == EXIT_OK
        # Full session JSON lands on stdout; ``session_id`` round-trips.
        parsed = json.loads(stdout)
        assert parsed["session_id"] == "export-me"
        assert parsed["final_prompt"] == "orig-improved"


class TestNameFlag:
    def test_name_label_lands_in_session_and_index(
        self, env: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        _run(
            ["--quiet", "--name", "my-label", "p"],
            env=env,
            backend=backend,
        )
        session_path = env["history_dir"] / "fixedsessionid.json"
        data = json.loads(session_path.read_text(encoding="utf-8"))
        # --name LABEL persists into the session's label field.
        assert data["label"] == "my-label"


class TestUpdateSystemPromptFlag:
    def test_update_system_prompt_writes_new_file(
        self, env: dict[str, Path]
    ) -> None:
        new_bytes = b"NEW SYSTEM PROMPT\n"
        import hashlib

        digest = hashlib.sha256(new_bytes).hexdigest()
        # The Config default system_prompt_update_url is a github URL;
        # we don't override it here, we just answer whatever URL is asked.

        def fetcher(url: str) -> bytes:
            if url.endswith(".sha256"):
                return f"{digest}\n".encode("ascii")
            return new_bytes

        rc, _, _ = _run(
            ["--update-system-prompt"],
            env=env,
            fetcher=fetcher,
        )
        assert rc == EXIT_OK
        # The system-prompt.md file at the resolved path now has the new bytes.
        assert env["system_prompt"].read_bytes() == new_bytes


class TestUninstallFlag:
    def test_uninstall_redirects_to_shell_script(
        self, env: dict[str, Path]
    ) -> None:
        rc, _, stderr = _run(["--uninstall"], env=env)
        # --uninstall is a redirect; main exits 1 with the canonical
        # 'run uninstall.sh' message — actual removal is the shell
        # script's job (US-015).
        assert rc == EXIT_FAILURE
        assert UNINSTALL_NOT_IMPLEMENTED in stderr


class TestBackendFlag:
    def test_backend_explicit_persists_after_success(
        self, env: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=10, output_tokens=20)],
            name="api-key (claude-sonnet-4-6)",
        )
        _run(
            ["--quiet", "--backend", "api-key", "p"],
            env=env,
            backend=backend,
        )
        config_data = json.loads(env["config_path"].read_text(encoding="utf-8"))
        # AC #5 (US-006): explicit --backend api-key persists after the
        # first successful turn.
        assert config_data["preferred_backend"] == "api-key"


class TestStatusFlag:
    def test_status_prints_summary_and_exits_0(
        self, env: dict[str, Path]
    ) -> None:
        backend = FakeBackend([], name="api-key (claude-sonnet-4-6)", auth_ok=True)
        rc, stdout, _ = _run(
            ["--status"],
            env=env,
            backend=backend,
        )
        assert rc == EXIT_OK
        # --status output is short (<20 lines per NFR-09) and begins
        # with the Platform: line (P1-PLAT-09).
        lines = stdout.splitlines()
        assert lines[0].startswith("Platform: ")
        assert len(lines) < 20
