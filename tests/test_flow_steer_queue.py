"""Flow tests — DESIGN-SPEC §5: steer vs queue composer semantics.

End-to-end over the gated DemoRuntime: Enter mid-turn steers (↳ echo,
notice, applied at the next step boundary as ``Applying steer: <text>``
with the consumed echo removed); Shift+Enter mid-turn queues a full
next-turn message (``▹ queued next:`` strip + footer ``q1`` + auto-drain
at turn end); a second steer queues; idle Shift+Enter just sends.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import AUTO_PROMPT, BUILD_PROMPT
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.app_support import STEER_NOTICE
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import footer_left_text, footer_right_text
from amplifier_app_newtui.ui.transcript import render_block

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    rules,
    seed_done,
    type_text,
    wait_for,
)


async def _start_gated_turn(pilot, app: NewTuiApp) -> None:
    """Seed, then start the build turn and park it mid-turn on the gate."""
    await seed_done(pilot, app)
    await type_text(pilot, "hi")
    await pilot.press("enter")
    assert await wait_for(
        pilot, lambda: app.turn_active and blocks_of(app, "narration")
    )


@pytest.mark.asyncio
async def test_enter_mid_turn_steers_echo_and_applies_at_step_boundary() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        assert app.footer_bar.state.context == "running"
        assert (
            footer_right_text(app.footer_bar.state)
            == "esc interrupt · enter steer · shift+enter queue"
        )

        # Running + Enter → steer with the exact ↳ echo line + notice.
        await type_text(pilot, "focus on the tests")
        await pilot.press("enter")
        await pilot.pause()
        echoes = blocks_of(app, "steer_echo")
        assert len(echoes) == 1 and echoes[0].text == "focus on the tests"
        line = "".join(s.text for s in render_block(echoes[0], 200)[0])
        assert line == (
            '  ↳ steer queued: "focus on the tests" · applies at next step boundary'
        )
        assert app.notice_slot.current == STEER_NOTICE
        assert app.footer_bar.state.queued == 0  # steers are not the qN badge

        # Release the turn: the steer applies at the next step boundary.
        adapter.release()
        assert await wait_for(pilot, lambda: app.approval_bar is not None)
        assert any(
            b.text == "Applying steer: focus on the tests"
            for b in blocks_of(app, "narration")
        )
        # Consumed steer removed: echo gone, queue empty.
        assert not blocks_of(app, "steer_echo")
        assert not adapter.steering.pending_steers

        # Finish the turn; a consumed steer does NOT roll forward.
        await pilot.press("enter")  # Allow once
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        await pilot.pause(0.2)
        assert rules(app) == 2
        assert not adapter.steering.pending


@pytest.mark.asyncio
async def test_shift_enter_mid_turn_queues_strip_q1_and_auto_drains() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)

        # Running + Shift+Enter → full next-turn message queued.
        await type_text(pilot, "ship the follow-up")
        await pilot.press("shift+enter")
        await pilot.pause()
        assert app.queued_strip.display
        assert app.queued_strip.text == (
            '▹ queued next: "ship the follow-up" · runs when this turn ends'
        )
        state = app.footer_bar.state
        assert state.queued == 1
        assert footer_left_text(state).endswith(" · q1")

        # Turn end → the queued message auto-runs as its own turn.
        adapter.release()
        assert await wait_for(pilot, lambda: app.approval_bar is not None)
        await pilot.press("enter")  # Allow once
        assert await wait_for(pilot, lambda: rules(app) >= 2)
        assert await wait_for(pilot, lambda: rules(app) >= 3 and not app.turn_active)
        # Auto-drained: strip cleared, footer back to q0.
        assert app.queued_strip.queued is None and not app.queued_strip.display
        assert app.footer_bar.state.queued == 0
        assert not adapter.steering.pending
        # The drained message ran as the next scripted turn.
        assert any(b.text == AUTO_PROMPT for b in blocks_of(app, "user_line"))


@pytest.mark.asyncio
async def test_second_steer_queues_full_next_turn_message() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        await type_text(pilot, "first steer")
        await pilot.press("enter")
        await pilot.pause()
        assert len(blocks_of(app, "steer_echo")) == 1

        # Enter again while a steer is pending → queues (spec §5).
        await type_text(pilot, "second message")
        await pilot.press("enter")
        await pilot.pause()
        assert len(blocks_of(app, "steer_echo")) == 1  # no second echo
        assert app.queued_strip.text == (
            '▹ queued next: "second message" · runs when this turn ends'
        )
        assert app.footer_bar.state.queued == 1
        assert len(adapter.steering.pending_steers) == 1
        adapter.release()  # let the parked script finish cleanly


@pytest.mark.asyncio
async def test_idle_shift_enter_just_sends() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "hi")
        await pilot.press("shift+enter")
        assert await wait_for(
            pilot,
            lambda: any(b.text == BUILD_PROMPT for b in blocks_of(app, "user_line")),
        )
        assert app.turn_active
        assert not app.adapter.steering.pending  # nothing queued
