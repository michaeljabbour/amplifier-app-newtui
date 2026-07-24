"""Show/hide the live thinking/response box (root stream peek).

The streaming box defaults to a one-line peek hint; ctrl-g (or a click on
the box) reveals the last few lines of the in-flight block and hides it
again. Reveal is a session preference — it sticks across turns. The
durable, full-length answer is unaffected: it lands on the consolidated
Answer regardless of reveal state.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import BUILD_PROMPT
from amplifier_app_newtui.ui.app import NewTuiApp

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    seed_done,
    type_text,
    wait_for,
)


@pytest.mark.asyncio
async def test_ctrl_g_toggles_thinking_box_and_defaults_hidden() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, BUILD_PROMPT)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.turn_active)

        # Default: the box is hidden (a peek hint, not the content).
        assert app.live_tail.revealed is False

        await pilot.press("ctrl+g")
        await pilot.pause()
        assert app.live_tail.revealed is True
        assert app.notice_slot.current == "thinking · shown"

        await pilot.press("ctrl+g")
        await pilot.pause()
        assert app.live_tail.revealed is False
        assert app.notice_slot.current == "thinking · hidden"

        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
