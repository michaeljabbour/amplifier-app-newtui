"""A raised turn must not tear down the TUI (regression for #21).

``submit_prompt`` schedules ``adapter.submit`` on a Textual worker, which
defaults to ``exit_on_error=True`` — an exception from ``submit`` (provider auth
expiry, network drop mid-turn) used to crash the whole app. The fix wraps the
call so the error surfaces as a notice and the session stays live.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.runtime_adapter import RuntimeAdapter


class _RaisingAdapter(RuntimeAdapter):
    """Boots clean (instant ready), then fails the turn like a dropped provider."""

    async def submit(self, _text: str, _attachments: tuple[Any, ...] = ()) -> None:
        raise RuntimeError("provider auth expired")


async def _wait_for(pilot, predicate, *, tries: int = 120) -> bool:  # noqa: ANN001
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


@pytest.mark.asyncio
async def test_turn_exception_shows_notice_and_keeps_app_alive() -> None:
    adapter = _RaisingAdapter()
    app = NewTuiApp(adapter)

    notices: list[str] = []
    async with app.run_test(size=(110, 40)) as pilot:
        # Boot is instant (base start() calls ready immediately); no splash guard.
        assert await _wait_for(pilot, lambda: app._splash is None)

        real_show_notice = app.show_notice
        app.show_notice = lambda text, duration=None: (  # type: ignore[method-assign]
            notices.append(text),
            real_show_notice(text, duration),
        )[-1]

        app.submit_prompt("hello")

        # The failing turn's notice lands and the app is still running — with the
        # old code the worker's re-raise (exit_on_error=True) would have stopped
        # the app and this wait would time out instead.
        assert await _wait_for(pilot, lambda: any("turn failed" in n for n in notices))
        assert any("provider auth expired" in n for n in notices)
        assert app.is_running

        # Still interactive: a second submit is accepted, not a dead app.
        app.submit_prompt("again")
        await pilot.pause(0.1)
        assert app.is_running


@pytest.mark.asyncio
async def test_cancelled_turn_is_not_reported_as_failure() -> None:
    """A real shutdown mid-turn (CancelledError is BaseException) must not be
    misread as a turn failure — only Exception is caught."""

    class _CancelAdapter(RuntimeAdapter):
        async def submit(self, _text: str, _attachments: tuple[Any, ...] = ()) -> None:
            raise asyncio.CancelledError

    adapter = _CancelAdapter()
    app = NewTuiApp(adapter)
    notices: list[str] = []
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: app._splash is None)
        real = app.show_notice
        app.show_notice = lambda text, duration=None: (  # type: ignore[method-assign]
            notices.append(text),
            real(text, duration),
        )[-1]
        app.submit_prompt("hello")
        await pilot.pause(0.2)
        assert not any("turn failed" in n for n in notices)
