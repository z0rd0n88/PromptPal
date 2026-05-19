"""Unit tests for core/cli.py (US-011 / SPEC §5, P1-FLAG-*).

Coverage map (one cluster per AC):

  AC #1 all PRD §5.4 flags wired                → test_parse_args_*
  AC #2 --output plain|json|markdown            → test_format_output_*,
                                                  test_main_output_*
  AC #3 --quiet auto-accepts, only stdout       → test_main_quiet_*
  AC #4 --show-history newest-first exit 0      → test_cmd_show_history_*,
                                                  test_main_show_history_*
  AC #5 --search KEYWORD index→sessions         → test_cmd_search_*,
                                                  test_main_search_*
  AC #6 --status backend/model/auth/platform/   → test_cmd_status_*,
        config/history count, <20 lines, exit 0   test_main_status_*
  AC #7 --export SESSION_ID dumps JSON, exit 0  → test_cmd_export_*,
                                                  test_main_export_*

Production seams are injected through ``main``'s kwargs — no monkey-
patching of ``sys`` globals, no real filesystem, no real backend.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Callable

import pytest

from core.backend import Backend, BackendResponse, NoBackendError
from core.cli import (
    BACKEND_API,
    BACKEND_AUTO,
    BACKEND_CLI,
    EXIT_FAILURE,
    EXIT_OK,
    OUTPUT_JSON,
    OUTPUT_MARKDOWN,
    OUTPUT_PLAIN,
    REPLAY_NOT_IMPLEMENTED,
    UNINSTALL_NOT_IMPLEMENTED,
    UPDATE_SUCCESS_TEMPLATE,
    CLIOptions,
    _config_overrides_from_options,
    _platform_label,
    cmd_export,
    cmd_search,
    cmd_show_history,
    cmd_status,
    cmd_update_system_prompt,
    format_output,
    main,
    parse_args,
)
from core.config import (
    Config,
)
from core.history import (
    IndexEntry,
    Session,
    Turn,
    upsert_index_entry,
    write_session,
)
from core.platform import Platform
from core.system_prompt import (
    CHECKSUM_MISMATCH_MESSAGE,
    SystemPromptDownloadError,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class FakeBackend(Backend):
    """Backend that pops responses off a queue; records every call."""

    def __init__(
        self,
        responses: list[BackendResponse],
        *,
        name: str = "fake-backend (test)",
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
            raise AssertionError("FakeBackend: out of responses")
        return self._responses.pop(0)

    def check_auth(self) -> bool:
        return self._auth_ok


def _fake_platform(
    *,
    home: str = "/home/test",
    is_wsl: bool = False,
    wsl_version: int | None = None,
) -> Platform:
    return Platform(
        is_wsl=is_wsl,
        wsl_version=wsl_version,
        home=home,
        clipboard_cmd=(),
    )


@pytest.fixture
def tmp_promptpal(tmp_path: Path) -> dict[str, Path]:
    """Return a populated set of ``~/.promptpal/*`` paths inside tmp_path.

    Seeds a config.json, system-prompt.md, history/ dir, and usage.log path.
    """
    home = tmp_path / "home"
    promptpal = home / ".promptpal"
    history = promptpal / "history"
    history.mkdir(parents=True)
    system_prompt_path = promptpal / "system-prompt.md"
    system_prompt_path.write_text("You are PromptPal.\n", encoding="utf-8")
    config_path = promptpal / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_model": "claude-sonnet-4-6",
                "system_prompt_path": str(system_prompt_path),
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
        "system_prompt": system_prompt_path,
    }


def _seed_index_entry(
    history_dir: Path,
    *,
    session_id: str,
    created_at: str,
    label: str | None = None,
    status: str = "accepted",
    preview: str = "hello world",
) -> None:
    upsert_index_entry(
        history_dir,
        IndexEntry(
            session_id=session_id,
            created_at=created_at,
            label=label,
            status=status,
            original_prompt_preview=preview,
        ),
    )


def _frozen_clock(*values: str) -> Callable[[], str]:
    """A clock that returns successive values, repeating the last one."""
    state = {"i": 0}
    seq = list(values)

    def clock() -> str:
        i = state["i"]
        state["i"] = min(i + 1, len(seq) - 1)
        return seq[i]

    return clock


# ===========================================================================
# AC #1 — parser wires every flag
# ===========================================================================


class TestParseArgs:
    def test_defaults(self) -> None:
        opts = parse_args([])
        assert opts == CLIOptions(
            prompt=None,
            model=None,
            iterations=None,
            no_history=False,
            copy=False,
            show_history=False,
            replay=None,
            system_prompt=None,
            output=OUTPUT_PLAIN,
            quiet=False,
            search=None,
            export=None,
            name=None,
            update_system_prompt=False,
            uninstall=False,
            backend=None,
            status=False,
        )

    def test_positional_prompt(self) -> None:
        assert parse_args(["hello world"]).prompt == "hello world"

    def test_model_override(self) -> None:
        assert parse_args(["--model", "claude-haiku-4-5"]).model == "claude-haiku-4-5"

    def test_iterations_int(self) -> None:
        assert parse_args(["--iterations", "3"]).iterations == 3

    def test_no_history_flag(self) -> None:
        assert parse_args(["--no-history"]).no_history is True

    def test_copy_flag(self) -> None:
        assert parse_args(["--copy"]).copy is True

    def test_show_history_flag(self) -> None:
        assert parse_args(["--show-history"]).show_history is True

    def test_replay(self) -> None:
        assert parse_args(["--replay", "abc123"]).replay == "abc123"

    def test_system_prompt(self) -> None:
        assert parse_args(["--system-prompt", "/tmp/x.md"]).system_prompt == "/tmp/x.md"

    @pytest.mark.parametrize("fmt", [OUTPUT_PLAIN, OUTPUT_JSON, OUTPUT_MARKDOWN])
    def test_output_choices(self, fmt: str) -> None:
        assert parse_args(["--output", fmt]).output == fmt

    def test_output_rejects_invalid(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--output", "yaml"])

    def test_quiet(self) -> None:
        assert parse_args(["--quiet"]).quiet is True

    def test_search(self) -> None:
        assert parse_args(["--search", "needle"]).search == "needle"

    def test_export(self) -> None:
        assert parse_args(["--export", "sess123"]).export == "sess123"

    def test_name(self) -> None:
        assert parse_args(["--name", "draft 1"]).name == "draft 1"

    def test_update_system_prompt(self) -> None:
        assert parse_args(["--update-system-prompt"]).update_system_prompt is True

    def test_uninstall(self) -> None:
        assert parse_args(["--uninstall"]).uninstall is True

    @pytest.mark.parametrize("b", [BACKEND_AUTO, BACKEND_CLI, BACKEND_API])
    def test_backend_choices(self, b: str) -> None:
        assert parse_args(["--backend", b]).backend == b

    def test_backend_rejects_invalid(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--backend", "ollama"])

    def test_status(self) -> None:
        assert parse_args(["--status"]).status is True

    def test_complex_combination(self) -> None:
        opts = parse_args(
            [
                "improve me",
                "--model",
                "claude-sonnet-4-6",
                "--iterations",
                "2",
                "--copy",
                "--name",
                "session-x",
                "--output",
                "json",
                "--backend",
                "api-key",
            ]
        )
        assert opts.prompt == "improve me"
        assert opts.iterations == 2
        assert opts.copy is True
        assert opts.name == "session-x"
        assert opts.output == OUTPUT_JSON
        assert opts.backend == BACKEND_API


# ===========================================================================
# Helpers — _config_overrides_from_options, _platform_label
# ===========================================================================


class TestConfigOverridesFromOptions:
    def test_empty_when_no_flags(self) -> None:
        assert _config_overrides_from_options(CLIOptions()) == {}

    def test_model_overrides_default_model(self) -> None:
        assert _config_overrides_from_options(
            CLIOptions(model="claude-haiku-4-5")
        ) == {"default_model": "claude-haiku-4-5"}

    def test_iterations_overrides_default_iterations(self) -> None:
        assert _config_overrides_from_options(CLIOptions(iterations=4)) == {
            "default_iterations": 4
        }

    def test_system_prompt_overrides_path(self) -> None:
        assert _config_overrides_from_options(
            CLIOptions(system_prompt="/tmp/x.md")
        ) == {"system_prompt_path": "/tmp/x.md"}

    def test_no_history_overrides_history_enabled(self) -> None:
        assert _config_overrides_from_options(CLIOptions(no_history=True)) == {
            "history_enabled": False
        }

    def test_quiet_copy_etc_not_overrides(self) -> None:
        # Behavioral flags don't persist to Config.
        assert _config_overrides_from_options(
            CLIOptions(quiet=True, copy=True, name="x")
        ) == {}


class TestPlatformLabel:
    def test_wsl2(self) -> None:
        assert _platform_label(_fake_platform(is_wsl=True, wsl_version=2)) == "WSL2"

    def test_wsl1(self) -> None:
        assert _platform_label(_fake_platform(is_wsl=True, wsl_version=1)) == "WSL1"

    def test_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        assert _platform_label(_fake_platform()) == "Linux"

    def test_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        assert _platform_label(_fake_platform()) == "macOS"


# ===========================================================================
# AC #2 — format_output for plain / json / markdown
# ===========================================================================


class TestFormatOutput:
    def test_plain(self) -> None:
        assert (
            format_output(
                output=OUTPUT_PLAIN,
                original="orig",
                improved="new",
                turns=1,
                session_id="s1",
                backend_name="api-key",
                model="claude-sonnet-4-6",
            )
            == "new"
        )

    def test_markdown_fences(self) -> None:
        out = format_output(
            output=OUTPUT_MARKDOWN,
            original="orig",
            improved="new line\nsecond",
            turns=1,
            session_id="s1",
            backend_name="api-key",
            model="m",
        )
        assert out.startswith("```\n") and out.endswith("\n```")
        assert "new line\nsecond" in out

    def test_json_envelope_keys(self) -> None:
        out = format_output(
            output=OUTPUT_JSON,
            original="orig",
            improved="new",
            turns=2,
            session_id="abc",
            backend_name="api-key",
            model="claude-sonnet-4-6",
        )
        parsed = json.loads(out)
        assert set(parsed.keys()) == {
            "original",
            "improved",
            "turns",
            "session_id",
            "backend",
            "model",
        }
        assert parsed == {
            "original": "orig",
            "improved": "new",
            "turns": 2,
            "session_id": "abc",
            "backend": "api-key",
            "model": "claude-sonnet-4-6",
        }

    def test_json_round_trip_with_unicode(self) -> None:
        """Non-ASCII content in original/improved survives JSON encode-decode (UTF-8, not escaped)."""
        out = format_output(
            output=OUTPUT_JSON,
            original="héllo 🚀",
            improved="café ✨",
            turns=1,
            session_id="x",
            backend_name="api-key",
            model="m",
        )
        # ensure_ascii=False means the raw glyphs are in the output.
        assert "🚀" in out
        assert "✨" in out
        # And it still round-trips through json.loads.
        assert json.loads(out)["original"] == "héllo 🚀"


# ===========================================================================
# AC #4 — cmd_show_history
# ===========================================================================


class TestCmdShowHistory:
    def test_empty_index(self, tmp_path: Path) -> None:
        stdout = io.StringIO()
        rc = cmd_show_history(history_dir=tmp_path, stdout=stdout)
        assert rc == EXIT_OK
        assert stdout.getvalue() == "(no history yet)\n"

    def test_populated_newest_first(self, tmp_path: Path) -> None:
        _seed_index_entry(tmp_path, session_id="b" * 32, created_at="2026-05-01T00:00:00Z")
        _seed_index_entry(tmp_path, session_id="a" * 32, created_at="2026-05-02T00:00:00Z")
        stdout = io.StringIO()
        cmd_show_history(history_dir=tmp_path, stdout=stdout)
        lines = stdout.getvalue().splitlines()
        # Newest (a's date) appears first.
        assert lines[0].startswith("2026-05-02T00:00:00Z")
        assert lines[1].startswith("2026-05-01T00:00:00Z")

    def test_pagination_truncates(self, tmp_path: Path) -> None:
        for i in range(25):
            _seed_index_entry(
                tmp_path,
                session_id=f"{i:032d}",
                created_at=f"2026-05-{i+1:02d}T00:00:00Z",
            )
        stdout = io.StringIO()
        cmd_show_history(history_dir=tmp_path, stdout=stdout, page_size=20)
        out = stdout.getvalue()
        # 20 entries + a footer line summarizing the rest.
        assert out.count("\n") == 21
        assert "5 older sessions not shown" in out

    def test_label_preferred_over_preview(self, tmp_path: Path) -> None:
        _seed_index_entry(
            tmp_path,
            session_id="a" * 32,
            created_at="2026-05-01T00:00:00Z",
            label="draft",
            preview="raw prompt body",
        )
        stdout = io.StringIO()
        cmd_show_history(history_dir=tmp_path, stdout=stdout)
        out = stdout.getvalue()
        assert "draft" in out
        assert "raw prompt body" not in out


# ===========================================================================
# AC #5 — cmd_search
# ===========================================================================


class TestCmdSearch:
    def test_no_matches(self, tmp_path: Path) -> None:
        _seed_index_entry(tmp_path, session_id="a" * 32, created_at="2026-05-01T00:00:00Z", preview="hello world")
        stdout = io.StringIO()
        rc = cmd_search(keyword="xyz", history_dir=tmp_path, stdout=stdout)
        assert rc == EXIT_OK
        assert "no sessions matched" in stdout.getvalue()

    def test_preview_match(self, tmp_path: Path) -> None:
        _seed_index_entry(
            tmp_path,
            session_id="a" * 32,
            created_at="2026-05-01T00:00:00Z",
            preview="explain DataFrame.merge",
        )
        stdout = io.StringIO()
        cmd_search(keyword="DataFrame", history_dir=tmp_path, stdout=stdout)
        assert "a" * 8 in stdout.getvalue()

    def test_label_match(self, tmp_path: Path) -> None:
        _seed_index_entry(
            tmp_path,
            session_id="b" * 32,
            created_at="2026-05-01T00:00:00Z",
            label="grocery-list",
            preview="something else",
        )
        stdout = io.StringIO()
        cmd_search(keyword="grocery", history_dir=tmp_path, stdout=stdout)
        assert "grocery-list" in stdout.getvalue()

    def test_search_results_newest_first(self, tmp_path: Path) -> None:
        _seed_index_entry(tmp_path, session_id="b" * 32, created_at="2026-05-01T00:00:00Z", preview="foo bar")
        _seed_index_entry(tmp_path, session_id="a" * 32, created_at="2026-05-02T00:00:00Z", preview="foo baz")
        stdout = io.StringIO()
        cmd_search(keyword="foo", history_dir=tmp_path, stdout=stdout)
        lines = stdout.getvalue().splitlines()
        assert lines[0].startswith("2026-05-02T00:00:00Z")


# ===========================================================================
# AC #7 — cmd_export
# ===========================================================================


class TestCmdExport:
    def test_missing_session_exits_1(self, tmp_path: Path) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        rc = cmd_export(
            session_id="nonexistent",
            history_dir=tmp_path,
            stdout=stdout,
            stderr=stderr,
        )
        assert rc == EXIT_FAILURE
        assert "not found" in stderr.getvalue()
        assert stdout.getvalue() == ""

    def test_found_session_writes_json(self, tmp_path: Path) -> None:
        session = Session(
            session_id="abc123",
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:01Z",
            status="accepted",
            label="x",
            original_prompt="orig",
            model="claude-sonnet-4-6",
            backend="api-key (test)",
            turns=(
                Turn(
                    role="user",
                    content="orig",
                    backend="api-key (test)",
                    input_tokens=None,
                    output_tokens=None,
                    timestamp="2026-05-01T00:00:00Z",
                ),
            ),
            final_prompt="improved",
        )
        write_session(session, tmp_path)
        stdout = io.StringIO()
        stderr = io.StringIO()
        rc = cmd_export(
            session_id="abc123",
            history_dir=tmp_path,
            stdout=stdout,
            stderr=stderr,
        )
        assert rc == EXIT_OK
        parsed = json.loads(stdout.getvalue())
        assert parsed["session_id"] == "abc123"
        assert parsed["final_prompt"] == "improved"
        assert parsed["status"] == "accepted"


# ===========================================================================
# AC #6 — cmd_status
# ===========================================================================


class TestCmdStatus:
    def test_format_under_20_lines(self, tmp_path: Path) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        backend = FakeBackend([], name="api-key (claude-sonnet-4-6)", auth_ok=True)
        rc = cmd_status(
            options=CLIOptions(),
            config=Config(),
            config_path=tmp_path / "config.json",
            history_dir=tmp_path,
            platform=_fake_platform(is_wsl=True, wsl_version=2),
            stdout=stdout,
            stderr=stderr,
            backend_factory=lambda c, o, p: backend,
        )
        assert rc == EXIT_OK
        out = stdout.getvalue()
        assert out.splitlines()[0] == "Platform: WSL2"
        assert "Backend: api-key (claude-sonnet-4-6)" in out
        assert "Model: claude-sonnet-4-6" in out
        assert "Auth: ok" in out
        assert "Config:" in out
        assert "History: 0 sessions" in out
        # NFR-09 — < 20 lines.
        assert len(out.splitlines()) < 20

    def test_auth_failure_shown(self, tmp_path: Path) -> None:
        backend = FakeBackend([], name="api-key (claude-sonnet-4-6)", auth_ok=False)
        stdout = io.StringIO()
        cmd_status(
            options=CLIOptions(),
            config=Config(),
            config_path=tmp_path / "config.json",
            history_dir=tmp_path,
            platform=_fake_platform(),
            stdout=stdout,
            stderr=io.StringIO(),
            backend_factory=lambda c, o, p: backend,
        )
        assert "Auth: failed" in stdout.getvalue()

    def test_no_backend_available_shows_unavailable(self, tmp_path: Path) -> None:
        def fail_factory(c: Config, o: CLIOptions, p: Platform) -> Backend:
            raise NoBackendError(NoBackendError.MESSAGE)

        stdout = io.StringIO()
        stderr = io.StringIO()
        rc = cmd_status(
            options=CLIOptions(),
            config=Config(),
            config_path=tmp_path / "config.json",
            history_dir=tmp_path,
            platform=_fake_platform(),
            stdout=stdout,
            stderr=stderr,
            backend_factory=fail_factory,
        )
        assert rc == EXIT_OK  # Always exits 0 — the point of --status is to diagnose.
        out = stdout.getvalue()
        assert "Backend: (unavailable)" in out
        assert "Reason:" in out
        assert "Auth:" not in out  # No auth probe when no backend.

    def test_history_count_reflects_index(self, tmp_path: Path) -> None:
        _seed_index_entry(tmp_path, session_id="x" * 32, created_at="2026-05-01T00:00:00Z")
        _seed_index_entry(tmp_path, session_id="y" * 32, created_at="2026-05-02T00:00:00Z")
        backend = FakeBackend([], auth_ok=True)
        stdout = io.StringIO()
        cmd_status(
            options=CLIOptions(),
            config=Config(),
            config_path=tmp_path / "config.json",
            history_dir=tmp_path,
            platform=_fake_platform(),
            stdout=stdout,
            stderr=io.StringIO(),
            backend_factory=lambda c, o, p: backend,
        )
        assert "History: 2 sessions" in stdout.getvalue()


# ===========================================================================
# --update-system-prompt
# ===========================================================================


import hashlib  # noqa: E402 — keep import close to its sole user block


class TestCmdUpdateSystemPrompt:
    def test_success_writes_file_and_prints_path(self, tmp_path: Path) -> None:
        target = tmp_path / "system-prompt.md"
        target.write_text("old\n", encoding="utf-8")
        payload = b"new system prompt\n"
        digest = hashlib.sha256(payload).hexdigest()

        def fetcher(url: str) -> bytes:
            if url.endswith(".sha256"):
                return digest.encode("ascii")
            return payload

        config = Config(system_prompt_update_url="https://example.com/sp.txt")
        stdout = io.StringIO()
        stderr = io.StringIO()
        rc = cmd_update_system_prompt(
            config=config,
            target_path=target,
            stdout=stdout,
            stderr=stderr,
            fetcher=fetcher,
        )
        assert rc == EXIT_OK
        assert target.read_bytes() == payload
        assert UPDATE_SUCCESS_TEMPLATE.format(path=target) in stdout.getvalue()
        # Backup mention goes to stderr (informational).
        assert "backed up" in stderr.getvalue()

    def test_checksum_mismatch_returns_failure(self, tmp_path: Path) -> None:
        target = tmp_path / "system-prompt.md"
        target.write_text("old\n", encoding="utf-8")

        def bad_fetcher(url: str) -> bytes:
            if url.endswith(".sha256"):
                return (b"0" * 64) + b"\n"
            return b"some payload"

        config = Config(system_prompt_update_url="https://example.com/sp.txt")
        stderr = io.StringIO()
        rc = cmd_update_system_prompt(
            config=config,
            target_path=target,
            stdout=io.StringIO(),
            stderr=stderr,
            fetcher=bad_fetcher,
        )
        assert rc == EXIT_FAILURE
        assert CHECKSUM_MISMATCH_MESSAGE in stderr.getvalue()
        # Target untouched on mismatch.
        assert target.read_text(encoding="utf-8") == "old\n"

    def test_download_error_returns_failure(self, tmp_path: Path) -> None:
        target = tmp_path / "system-prompt.md"

        def failing_fetcher(url: str) -> bytes:
            raise SystemPromptDownloadError(
                f"Could not fetch system prompt from {url}: timeout"
            )

        config = Config(system_prompt_update_url="https://example.com/sp.txt")
        stderr = io.StringIO()
        rc = cmd_update_system_prompt(
            config=config,
            target_path=target,
            stdout=io.StringIO(),
            stderr=stderr,
            fetcher=failing_fetcher,
        )
        assert rc == EXIT_FAILURE
        assert "Could not fetch" in stderr.getvalue()


# ===========================================================================
# main() — full integration paths
# ===========================================================================


class TestMainEarlyExits:
    def test_status_exits_0(self, tmp_promptpal: dict[str, Path]) -> None:
        stdout = io.StringIO()
        backend = FakeBackend([], name="api-key (claude-sonnet-4-6)", auth_ok=True)
        rc = main(
            ["--status"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=stdout,
            stderr=io.StringIO(),
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            backend_factory=lambda c, o, p: backend,
            skip_wsl_guard=True,
        )
        assert rc == EXIT_OK
        assert "Platform:" in stdout.getvalue()
        assert "Backend:" in stdout.getvalue()

    def test_show_history_empty(self, tmp_promptpal: dict[str, Path]) -> None:
        stdout = io.StringIO()
        rc = main(
            ["--show-history"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=stdout,
            stderr=io.StringIO(),
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            skip_wsl_guard=True,
        )
        assert rc == EXIT_OK
        assert "(no history yet)" in stdout.getvalue()

    def test_search_no_matches(self, tmp_promptpal: dict[str, Path]) -> None:
        stdout = io.StringIO()
        rc = main(
            ["--search", "foo"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=stdout,
            stderr=io.StringIO(),
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            skip_wsl_guard=True,
        )
        assert rc == EXIT_OK
        assert "no sessions matched" in stdout.getvalue()

    def test_export_missing_session(self, tmp_promptpal: dict[str, Path]) -> None:
        stderr = io.StringIO()
        rc = main(
            ["--export", "nothere"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=io.StringIO(),
            stderr=stderr,
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            skip_wsl_guard=True,
        )
        assert rc == EXIT_FAILURE
        assert "not found" in stderr.getvalue()

    def test_update_system_prompt(self, tmp_promptpal: dict[str, Path]) -> None:
        payload = b"updated\n"
        digest = hashlib.sha256(payload).hexdigest()

        def fetcher(url: str) -> bytes:
            return digest.encode("ascii") if url.endswith(".sha256") else payload

        rc = main(
            ["--update-system-prompt"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            fetcher=fetcher,
            skip_wsl_guard=True,
        )
        assert rc == EXIT_OK
        assert tmp_promptpal["system_prompt"].read_bytes() == payload

    def test_uninstall_not_implemented(self, tmp_promptpal: dict[str, Path]) -> None:
        stderr = io.StringIO()
        rc = main(
            ["--uninstall"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=io.StringIO(),
            stderr=stderr,
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            skip_wsl_guard=True,
        )
        assert rc == EXIT_FAILURE
        assert UNINSTALL_NOT_IMPLEMENTED in stderr.getvalue()

    def test_replay_not_implemented(self, tmp_promptpal: dict[str, Path]) -> None:
        stderr = io.StringIO()
        rc = main(
            ["--replay", "abc"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=io.StringIO(),
            stderr=stderr,
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            skip_wsl_guard=True,
        )
        assert rc == EXIT_FAILURE
        assert REPLAY_NOT_IMPLEMENTED in stderr.getvalue()


# ===========================================================================
# main() — normal pipeline
# ===========================================================================


def _run_main(
    argv: list[str],
    *,
    tmp_promptpal: dict[str, Path],
    backend: Backend,
    stdin: str = "",
    stdout: io.StringIO | None = None,
    stderr: io.StringIO | None = None,
    copy_fn: Callable[[str], bool] | None = None,
) -> tuple[int, io.StringIO, io.StringIO]:
    stdout = stdout if stdout is not None else io.StringIO()
    stderr = stderr if stderr is not None else io.StringIO()
    rc = main(
        argv,
        config_path=tmp_promptpal["config_path"],
        history_dir=tmp_promptpal["history_dir"],
        usage_log_path=tmp_promptpal["usage_log"],
        stdin=io.StringIO(stdin),
        stdout=stdout,
        stderr=stderr,
        detect_platform_fn=lambda: _fake_platform(
            home=str(tmp_promptpal["home"])
        ),
        backend_factory=lambda c, o, p: backend,
        copy_fn=copy_fn if copy_fn is not None else (lambda t: False),
        clock=_frozen_clock("2026-05-19T00:00:00Z"),
        id_factory=lambda: "fixedsessionid",
        skip_wsl_guard=True,
    )
    return rc, stdout, stderr


class TestMainQuietPipeline:
    def test_first_turn_only_emits_improved_to_stdout(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved text", input_tokens=None, output_tokens=None)],
            name="claude-cli",
        )
        rc, stdout, stderr = _run_main(
            ["--quiet", "raw prompt"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
        )
        assert rc == EXIT_OK
        # AC #3: only improved prompt on stdout — single trailing newline.
        assert stdout.getvalue() == "improved text\n"
        # AC #3: no diff, no choice line on stderr.
        assert "[a]ccept" not in stderr.getvalue()

    def test_iterations_runs_n_extra_turns(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [
                BackendResponse(text="turn1", input_tokens=1, output_tokens=1),
                BackendResponse(text="turn2", input_tokens=1, output_tokens=1),
                BackendResponse(text="turn3", input_tokens=1, output_tokens=1),
            ],
            name="api-key",
        )
        rc, stdout, _ = _run_main(
            ["--quiet", "--iterations", "2", "p"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
        )
        assert rc == EXIT_OK
        # Final improved is the LAST turn (turn3).
        assert stdout.getvalue() == "turn3\n"
        # First call + 2 iterations = 3 total backend calls.
        assert len(backend.calls) == 3

    def test_json_output_envelope(self, tmp_promptpal: dict[str, Path]) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=10, output_tokens=20)],
            name="api-key (claude-sonnet-4-6)",
        )
        rc, stdout, _ = _run_main(
            ["--quiet", "--output", "json", "orig prompt"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
        )
        assert rc == EXIT_OK
        # AC-PIPE-02: single JSON object that parses with json.loads.
        parsed = json.loads(stdout.getvalue())
        assert parsed == {
            "original": "orig prompt",
            "improved": "improved",
            "turns": 1,
            "session_id": "fixedsessionid",
            "backend": "api-key",
            "model": "claude-sonnet-4-6",
        }

    def test_markdown_output(self, tmp_promptpal: dict[str, Path]) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)],
            name="claude-cli",
        )
        rc, stdout, _ = _run_main(
            ["--quiet", "--output", "markdown", "p"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
        )
        assert rc == EXIT_OK
        assert stdout.getvalue() == "```\nimproved\n```\n"

    def test_no_history_skips_session_write(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        _run_main(
            ["--quiet", "--no-history", "p"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
        )
        # No session JSON should have been written.
        history_files = list(tmp_promptpal["history_dir"].glob("*.json"))
        assert history_files == []

    def test_history_written_by_default(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        _run_main(
            ["--quiet", "p"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
        )
        # Index + session file exist.
        session_path = tmp_promptpal["history_dir"] / "fixedsessionid.json"
        assert session_path.exists()
        data = json.loads(session_path.read_text(encoding="utf-8"))
        assert data["status"] == "accepted"
        assert data["final_prompt"] == "improved"
        assert data["original_prompt"] == "p"

    def test_copy_invokes_copy_fn(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        copied: list[str] = []
        _run_main(
            ["--quiet", "--copy", "p"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
            copy_fn=lambda t: (copied.append(t), True)[1],
        )
        assert copied == ["improved"]


class TestMainPipelineErrors:
    def test_empty_prompt_exits_1(self, tmp_promptpal: dict[str, Path]) -> None:
        backend = FakeBackend([])
        rc, stdout, stderr = _run_main(
            ["--quiet"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
            stdin="   \n",
        )
        assert rc == EXIT_FAILURE
        assert stdout.getvalue() == ""
        assert "empty prompt" in stderr.getvalue().lower()

    def test_config_corrupt_exits_1(self, tmp_promptpal: dict[str, Path]) -> None:
        tmp_promptpal["config_path"].write_text("{not json", encoding="utf-8")
        stderr = io.StringIO()
        rc = main(
            ["--quiet", "p"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=io.StringIO(),
            stderr=stderr,
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            skip_wsl_guard=True,
        )
        assert rc == EXIT_FAILURE
        assert "corrupt" in stderr.getvalue().lower()

    def test_no_backend_available_exits_1(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        def failing(c: Config, o: CLIOptions, p: Platform) -> Backend:
            raise NoBackendError(NoBackendError.MESSAGE)

        stderr = io.StringIO()
        rc = main(
            ["--quiet", "p"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=io.StringIO(),
            stderr=stderr,
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"])
            ),
            backend_factory=failing,
            skip_wsl_guard=True,
        )
        assert rc == EXIT_FAILURE
        assert "No backend available" in stderr.getvalue()


class TestMainInteractivePipeline:
    """Refinement loop integration — non-quiet path."""

    def test_accept_writes_session_and_emits_stdout(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved\nline2\nline3\nline4", input_tokens=None, output_tokens=None)]
        )
        rc, stdout, _ = _run_main(
            ["my prompt that is longer than three lines"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
            stdin="a\n",  # accept on first choice prompt
        )
        assert rc == EXIT_OK
        assert "improved\nline2\nline3\nline4" in stdout.getvalue()
        # Session file written with status=accepted.
        session_path = tmp_promptpal["history_dir"] / "fixedsessionid.json"
        data = json.loads(session_path.read_text(encoding="utf-8"))
        assert data["status"] == "accepted"

    def test_discard_writes_session_no_stdout(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="improved", input_tokens=None, output_tokens=None)]
        )
        rc, stdout, _ = _run_main(
            ["p"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
            stdin="d\n",  # discard
        )
        assert rc == EXIT_OK
        # Discarded — no stdout.
        assert stdout.getvalue() == ""
        # Session still written, but with status=discarded.
        session_path = tmp_promptpal["history_dir"] / "fixedsessionid.json"
        data = json.loads(session_path.read_text(encoding="utf-8"))
        assert data["status"] == "discarded"
        assert data["final_prompt"] is None


# ===========================================================================
# AC #6 stdout structure — verified once more via main, not just cmd_status
# ===========================================================================


class TestMainStatusOutputStructure:
    def test_first_line_is_platform(self, tmp_promptpal: dict[str, Path]) -> None:
        backend = FakeBackend([], name="api-key", auth_ok=True)
        stdout = io.StringIO()
        rc = main(
            ["--status"],
            config_path=tmp_promptpal["config_path"],
            history_dir=tmp_promptpal["history_dir"],
            usage_log_path=tmp_promptpal["usage_log"],
            stdout=stdout,
            stderr=io.StringIO(),
            detect_platform_fn=lambda: _fake_platform(
                home=str(tmp_promptpal["home"]),
                is_wsl=True,
                wsl_version=2,
            ),
            backend_factory=lambda c, o, p: backend,
            skip_wsl_guard=True,
        )
        assert rc == EXIT_OK
        first_line = stdout.getvalue().splitlines()[0]
        # P1-PLAT-09: Platform: WSL2 on the first line.
        assert first_line == "Platform: WSL2"


# ===========================================================================
# Pipe-safety smoke test — improved prompt on stdout, everything else stderr
# ===========================================================================


class TestPipeSafetyContract:
    def test_quiet_mode_stdout_has_only_improved_prompt(
        self, tmp_promptpal: dict[str, Path]
    ) -> None:
        backend = FakeBackend(
            [BackendResponse(text="just this", input_tokens=None, output_tokens=None)]
        )
        rc, stdout, stderr = _run_main(
            ["--quiet", "p"],
            tmp_promptpal=tmp_promptpal,
            backend=backend,
        )
        assert rc == EXIT_OK
        # stdout: exactly the improved prompt + one newline.
        assert stdout.getvalue() == "just this\n"
        # stderr: empty in the happy path (no warnings).
        assert stderr.getvalue() == ""
