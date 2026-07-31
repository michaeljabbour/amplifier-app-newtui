"""Cross-session prompt history wiring: adapter seam + app boot seeding.

A fresh session in a directory with prior sessions must recall those
prompts on ↑ (the regression this fixes). All disk I/O runs against tmp
directories — the real ``~/.amplifier`` is never touched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.prompt_history import PromptHistoryStore
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.runtime_adapter import RealRuntimeAdapter, RuntimeAdapter


async def _wait_for(pilot, predicate: Callable[[], bool], *, tries: int = 80) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


# --------------------------------------------------------------------------
# adapter seam: base no-ops, real persists per project dir
# --------------------------------------------------------------------------


def test_base_adapter_prompt_history_is_noop() -> None:
    adapter = RuntimeAdapter()
    adapter.record_prompt("nothing persists here")
    assert adapter.prompt_history() == ()


def test_real_adapter_persists_and_reloads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    project = tmp_path / "work" / "proj"
    project.mkdir(parents=True)

    adapter = RealRuntimeAdapter(bundle="offline")
    adapter._config_project_dir = project  # learned from the runtime at start()

    adapter.record_prompt("command A")
    adapter.record_prompt("command A")  # consecutive dupe skipped
    adapter.record_prompt("command B")

    # A fresh adapter for the same dir recalls both, oldest-first.
    fresh = RealRuntimeAdapter(bundle="offline")
    fresh._config_project_dir = project
    assert fresh.prompt_history() == ("command A", "command B")


# --------------------------------------------------------------------------
# app boot: a fresh session seeds ↑ from a prior session's history
# --------------------------------------------------------------------------


class _SeededAdapter(RuntimeAdapter):
    """Base adapter whose persisted history stands in for a prior session
    in the same working directory."""

    def __init__(self, prior: tuple[str, ...]) -> None:
        super().__init__()
        self._prior = prior
        self.recorded: list[str] = []

    def prompt_history(self) -> tuple[str, ...]:
        return self._prior

    def record_prompt(self, text: str) -> None:
        self.recorded.append(text)


@pytest.mark.asyncio
async def test_fresh_session_recalls_prior_session_prompts() -> None:
    adapter = _SeededAdapter(("older prompt", "command A"))
    app = TuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: bool(app.composer._history))
        app.composer.focus_input()
        await pilot.press("up")
        assert app.composer.text == "command A"  # most recent first
        await pilot.press("up")
        assert app.composer.text == "older prompt"


@pytest.mark.asyncio
async def test_submitting_a_prompt_records_it() -> None:
    adapter = _SeededAdapter(())
    app = TuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: app._splash is None)
        app.composer.focus_input()
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        assert adapter.recorded == ["hi"]


@pytest.mark.asyncio
async def test_end_to_end_kill_then_fresh_session_same_dir(tmp_path: Path, monkeypatch) -> None:
    """The reported scenario: submit in session 1, kill it, then a fresh
    session 2 in the same dir recalls it — proven through the real store."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    project = tmp_path / "work" / "same-dir"
    project.mkdir(parents=True)

    # Session 1 submits "command A" (what the app does on submit).
    PromptHistoryStore(project_dir=project).append("command A")

    # Session 2 boots fresh in the same dir and seeds ↑ from the store.
    prior = tuple(PromptHistoryStore(project_dir=project).load())
    adapter = _SeededAdapter(prior)
    app = TuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: bool(app.composer._history))
        app.composer.focus_input()
        await pilot.press("up")
        assert app.composer.text == "command A"
