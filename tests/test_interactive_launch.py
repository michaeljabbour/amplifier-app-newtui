"""Interactive-launch overrides: ``amplifier-tui [-p/-m/--mode]`` (S2, #148).

The bare ``amplifier-tui`` launcher (and ``run`` with no prompt on a TTY)
must boot the full-screen TUI with the same ephemeral per-invocation overrides
the headless ``run`` command documents:

- ``--provider``/``--model`` mutate only the resolved in-memory plan (threaded
  into ``RealRuntimeAdapter`` → ``RealRuntime``, never persisted); and
- ``--mode`` seeds the opening interaction posture on ``TuiApp``.

Three layers are exercised: the CLI wiring (flags reach ``_launch_tui``), the
shared validation rules (``--model`` requires ``--provider``; unknown ``--mode``
fails loud), and the seams the overrides ride (adapter kwargs + app posture).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import amplifier_app_tui.main as main_mod
from amplifier_app_tui.main import main
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.runtime_adapter import RealRuntimeAdapter


@pytest.fixture
def capture_launch(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace ``_launch_tui`` (and the provider gate) so no real TUI boots."""
    launched: dict[str, object] = {}

    async def fake_launch(**kwargs: object) -> int:
        launched.update(kwargs)
        return 0

    async def fake_gate() -> int | None:
        return None

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    monkeypatch.setattr(main_mod, "_first_run_gate", fake_gate)
    return launched


# ---------------------------------------------------------------------------
# CLI wiring: the bare launcher threads each override into _launch_tui
# ---------------------------------------------------------------------------


def test_bare_launch_threads_no_overrides(capture_launch: dict[str, object]) -> None:
    """No flags ⇒ every override is None (untouched default launch)."""
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0
    assert capture_launch["demo"] is False
    assert capture_launch["provider"] is None
    assert capture_launch["model"] is None
    assert capture_launch["mode"] is None


def test_launch_threads_provider_and_model(capture_launch: dict[str, object]) -> None:
    result = CliRunner().invoke(main, ["-p", "anthropic", "-m", "claude-sonnet-5"])
    assert result.exit_code == 0
    assert capture_launch["provider"] == "anthropic"
    assert capture_launch["model"] == "claude-sonnet-5"


def test_launch_threads_mode_posture(capture_launch: dict[str, object]) -> None:
    result = CliRunner().invoke(main, ["--mode", "chat"])
    assert result.exit_code == 0
    assert capture_launch["mode"] == "chat"


def test_launch_threads_all_overrides_with_bundle(capture_launch: dict[str, object]) -> None:
    """Samuel's exact command shape now boots the TUI in chat mode."""
    result = CliRunner().invoke(
        main, ["--mode", "chat", "-p", "anthropic", "-m", "claude-sonnet-5", "--bundle", "custom"]
    )
    assert result.exit_code == 0
    assert capture_launch == {
        "demo": False,
        "bundle": "custom",
        "resume_id": None,
        "mode": "chat",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
    }


# ---------------------------------------------------------------------------
# Shared validation: same rules as the headless `run` command
# ---------------------------------------------------------------------------


def test_launch_model_without_provider_errors(capture_launch: dict[str, object]) -> None:
    result = CliRunner().invoke(main, ["-m", "claude-sonnet-5"])
    assert result.exit_code == 1
    assert "requires --provider" in result.stderr
    assert capture_launch == {}  # never reached a launch


def test_launch_unknown_mode_errors(capture_launch: dict[str, object]) -> None:
    result = CliRunner().invoke(main, ["--mode", "bogus"])
    assert result.exit_code == 1
    assert "unknown mode" in result.stderr
    assert "chat" in result.stderr  # valid ids are listed
    assert capture_launch == {}


def test_gate_nonzero_stops_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing first-run gate returns its exit code without booting the TUI."""
    launched: list[object] = []

    async def fake_launch(**kwargs: object) -> int:
        launched.append(kwargs)
        return 0

    async def fake_gate() -> int | None:
        return 3

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    monkeypatch.setattr(main_mod, "_first_run_gate", fake_gate)
    result = CliRunner().invoke(main, ["--mode", "chat"])
    assert result.exit_code == 3
    assert launched == []


# ---------------------------------------------------------------------------
# `run` with no prompt on a TTY launches interactive (Samuel's exact command)
# ---------------------------------------------------------------------------


def test_run_without_prompt_on_tty_launches_interactive(
    capture_launch: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "_is_interactive_terminal", lambda: True)
    result = CliRunner().invoke(
        main, ["run", "-p", "anthropic", "-m", "claude-x", "--mode", "chat"]
    )
    assert result.exit_code == 0
    assert capture_launch["provider"] == "anthropic"
    assert capture_launch["model"] == "claude-x"
    assert capture_launch["mode"] == "chat"
    assert capture_launch["demo"] is False


def test_run_without_prompt_not_tty_stays_prompt_required(
    capture_launch: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-interactive / piped `run` with no prompt still fails loud (headless)."""
    monkeypatch.setattr(main_mod, "_is_interactive_terminal", lambda: False)
    result = CliRunner().invoke(main, ["run", "--mode", "chat"])
    assert result.exit_code != 0
    assert "Prompt required" in result.output
    assert capture_launch == {}


def test_run_without_prompt_json_output_stays_prompt_required(
    capture_launch: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even on a TTY, a JSON output format keeps `run` headless (prompt-required)."""
    monkeypatch.setattr(main_mod, "_is_interactive_terminal", lambda: True)
    result = CliRunner().invoke(main, ["run", "--output-format", "json"])
    assert result.exit_code != 0
    assert capture_launch == {}


# ---------------------------------------------------------------------------
# Seams the overrides ride: adapter kwargs + app posture
# ---------------------------------------------------------------------------


def test_real_adapter_stores_provider_and_model_overrides() -> None:
    adapter = RealRuntimeAdapter(
        bundle="offline", provider_override="anthropic", model_override="claude-x"
    )
    assert adapter._provider_override == "anthropic"
    assert adapter._model_override == "claude-x"


def test_app_seeds_initial_mode() -> None:
    app = TuiApp(DemoRuntimeAdapter(), initial_mode="chat")
    assert app.mode_id == "chat"


def test_app_defaults_to_auto_without_initial_mode() -> None:
    app = TuiApp(DemoRuntimeAdapter())
    assert app.mode_id == "auto"
