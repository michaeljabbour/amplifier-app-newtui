"""Shared helpers for the end-to-end flow tests (``test_flow_*``).

All flow tests drive the REAL app (``NewTuiApp``) over the DemoRuntime
through Textual's Pilot — the UI cannot tell the demo from a real
session (ADR-0007 §Runtimes). This module holds the polling helper, the
keystroke typing helper and :class:`GatedDemoAdapter`, which parks the
scripted turns on an :class:`asyncio.Event` so tests get a deterministic
*mid-turn* state (needed for steer/queue and live-title assertions).

This file intentionally defines no tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter

SIZE = (120, 50)
"""Default Pilot screen size: everything in the short flows stays visible."""


async def wait_for(pilot, predicate: Callable[[], object], *, tries: int = 100) -> bool:
    """Poll *predicate* while letting the app process events."""
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return bool(predicate())


async def type_text(pilot, text: str) -> None:
    """Type *text* into the focused widget, one key per character."""
    await pilot.press(*("space" if ch == " " else ch for ch in text))


def blocks_of(app: NewTuiApp, kind: str) -> list:
    return [b for b in app.transcript.blocks if b.kind == kind]


def rules(app: NewTuiApp) -> int:
    return sum(b.kind == "turn_rule" for b in app.transcript.blocks)


def line_texts(app: NewTuiApp, width: int = 200) -> list[str]:
    """Every rendered transcript line as plain text (spec-string asserts)."""
    from amplifier_app_newtui.ui.transcript import render_block

    return [
        "".join(segment.text for segment in line)
        for block in app.transcript.blocks
        for line in render_block(block, width)
    ]


async def seed_done(pilot, app: NewTuiApp) -> None:
    """Wait for the demo seed turn to finish (rule t1 cut, app idle)."""
    assert await wait_for(pilot, lambda: rules(app) >= 1 and not app.turn_active)


class GatedDemoAdapter(DemoRuntimeAdapter):
    """Demo adapter whose virtual sleeps park on a controllable gate.

    The seed turn has no waits and plays instantly; every scripted turn
    parks at its first ``_wait`` — a deterministic mid-turn state
    (running flag up, working line ticking, composer live). Call
    :meth:`release` to let the rest of the script play instantly.
    """

    def __init__(self) -> None:
        super().__init__(instant=True)
        self.gate = asyncio.Event()

        async def _gated(_seconds: float) -> None:
            await self.gate.wait()
            await asyncio.sleep(0)

        self._runtime._sleep = _gated  # test seam: deterministic pacing

    def release(self) -> None:
        self.gate.set()
