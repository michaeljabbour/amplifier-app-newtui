"""ctrl+f return-to-answer action (AC2, compliance 2026-08-02 item B1).

Jumps back to the current/most-recent turn's final-answer start anchor --
the ``Answer`` block the reducer stamped ``final=True`` -- so a long
answer's START comes back into view after scrolling away, mirroring
ctrl-g's block-id targeting for the durable Thinking block
(test_flow_thinking_block.py).
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.ui.app import TuiApp

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    seed_done,
)


@pytest.mark.asyncio
async def test_ctrl_f_returns_to_the_seeded_turns_final_answer() -> None:
    """End to end: the demo seed turn's scripted answer is stamped
    ``final`` by the reducer, and ctrl+f finds and scrolls back to it."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        await pilot.press("ctrl+f")
        await pilot.pause()
        assert app.notice_slot.current == "back to the final answer"
