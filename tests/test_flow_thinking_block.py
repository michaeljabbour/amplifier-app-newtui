"""Durable, collapsible thinking block in the transcript (issue #129).

The model's reasoning lands as a default-collapsed Thinking block inline in
the transcript. ctrl-g (or enter on the focused block) expands/collapses it
in place; a withheld (empty-text) block is not expandable. When no durable
block exists, ctrl-g falls back to PR #128's live-tail reveal.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.model.blocks import Thinking
from amplifier_app_tui.ui.app import TuiApp

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    seed_done,
)


@pytest.mark.asyncio
async def test_ctrl_g_toggles_durable_thinking_block_in_place() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.transcript.append(Thinking(id=app.allocator.next_id(), text="weigh A\npick B"))
        await pilot.pause()
        assert blocks_of(app, "thinking")[0].expanded is False  # default collapsed

        await pilot.press("ctrl+g")
        await pilot.pause()
        assert blocks_of(app, "thinking")[0].expanded is True
        assert app.notice_slot.current == "thinking · expanded"

        await pilot.press("ctrl+g")
        await pilot.pause()
        assert blocks_of(app, "thinking")[0].expanded is False
        assert app.notice_slot.current == "thinking · collapsed"


@pytest.mark.asyncio
async def test_enter_on_focused_thinking_block_toggles_and_syncs_history() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        block = Thinking(id=app.allocator.next_id(), text="reason it out")
        app.transcript.append(block)
        await pilot.pause()
        widget = app.transcript.get_widget(block.id)
        assert widget is not None
        widget.focus()
        await pilot.press("enter")
        await pilot.pause()
        # The widget's local toggle is mirrored into canonical history.
        assert app.transcript.get_block(block.id).expanded is True


@pytest.mark.asyncio
async def test_ctrl_g_falls_back_to_live_tail_without_durable_thinking() -> None:
    """No durable thinking block yet → ctrl-g still drives the live-tail
    reveal (PR #128), so the two surfaces coexist."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        # A withheld (empty-text) block is present but not expandable, so the
        # fallback must still fire.
        app.transcript.append(Thinking(id=app.allocator.next_id(), text=""))
        await pilot.pause()
        assert app.live_tail.revealed is False

        await pilot.press("ctrl+g")
        await pilot.pause()
        assert app.live_tail.revealed is True
        assert app.notice_slot.current == "thinking · shown"
        # The withheld block stays collapsed/untouched.
        assert blocks_of(app, "thinking")[0].expanded is False
