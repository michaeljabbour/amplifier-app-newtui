"""Esc-interrupt flow (DESIGN-SPEC §3/§11, mockup ``runTurn`` interrupt path).

Esc while a store turn runs stops it at the next step boundary: the
transcript gains the italic ``Interrupted. Goal: …`` recap with the
``✳ `` dimmer marker and the turn rule is cut dimmer with the
``· interrupted`` outcome; the checkpoint records
``store refactor · interrupted`` and nothing ships.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import (
    BUILD_PROMPT,
    DEMO_LANE_BY_NAME,
    INTERRUPTED_RECAP,
)
from amplifier_app_newtui.model.blocks import Answer, BlockIdAllocator
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import lane_focus_blocks

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    line_texts,
    seed_done,
    type_text,
    wait_for,
)


@pytest.mark.asyncio
async def test_esc_interrupts_running_turn_with_recap_and_interrupted_rule() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, BUILD_PROMPT)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.turn_active)

        await pilot.press("escape")
        await pilot.pause()
        # Esc only requests the break — the notice waits for close-out.
        assert app.notice_slot.current != "turn interrupted · context saved"

        # The turn STOPS at the next step boundary (mockup break) — no
        # pytest approval, no shipped close-out; the end notice fires at
        # the actual close-out (mockup end of runTurn, spec §11).
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        assert app.notice_slot.current == "turn interrupted · context saved"
        assert f"✳ {INTERRUPTED_RECAP}" in line_texts(app)

        rule = blocks_of(app, "turn_rule")[-1]
        assert rule.label.endswith(" · interrupted")
        assert not rule.shipped  # dimmer label (spec §3: not shipped)
        checkpoint = app.reducer.ledger.checkpoints[-1]
        assert checkpoint.label == "store refactor · interrupted"
        assert not app.reducer.ledger.last_shipped  # no ▲ yield glyph


def test_lane_focus_state_recap_carries_recap_glyph() -> None:
    """Mockup focusLane: ``✳ `` dimmer + lane state dim italic (spec §8)."""
    lane = DEMO_LANE_BY_NAME["coder"]
    recap = lane_focus_blocks(lane, BlockIdAllocator())[-1]
    assert isinstance(recap, Answer)
    assert [(s.text, s.style_token, s.italic) for s in recap.spans] == [
        ("✳ ", "dimmer", False),
        (lane.state_recap, "dim", True),
    ]
