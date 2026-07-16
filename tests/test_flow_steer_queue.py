"""Flow tests — DESIGN-SPEC §5: steer vs queue composer semantics.

End-to-end over the gated DemoRuntime: Enter mid-turn steers (↳ echo,
notice, applied at the next step boundary as ``Applying steer: <text>``
with the consumed echo removed); Shift+Enter mid-turn queues a full
next-turn message (``▹ queued next:`` strip + footer ``q1`` + auto-drain
at turn end); a second steer queues; idle Shift+Enter just sends.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import AUTO_MODE_NOTICE, BUILD_END_NOTICE
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.app_support import QUEUED_NOTICE, STEER_NOTICE
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import footer_left_text, footer_right_text
from amplifier_app_newtui.ui.transcript import render_block

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    rules,
    seed_done,
    set_mode,
    type_text,
    wait_for,
)


async def _start_gated_turn(pilot, app: NewTuiApp) -> None:
    """Seed, switch to chat (the app boots in auto — §4 amendment) so the
    build turn keeps its pytest approval, then park it mid-turn on the gate."""
    await seed_done(pilot, app)
    await set_mode(pilot, app, "chat")
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

        # Running + Shift+Enter → full next-turn message queued + notice.
        await type_text(pilot, "ship the follow-up")
        await pilot.press("shift+enter")
        await pilot.pause()
        assert app.queued_strip.display
        assert app.queued_strip.text == (
            '▹ queued next: "ship the follow-up" · runs when this turn ends'
        )
        assert app.notice_slot.current == QUEUED_NOTICE
        state = app.footer_bar.state
        assert state.queued == 1
        assert footer_left_text(state).endswith(" · q1")

        # A second Shift+Enter REPLACES the queued message (mockup single
        # slot, ``this.queued = text``) — the badge never exceeds q1.
        await type_text(pilot, "actually, this instead")
        await pilot.press("shift+enter")
        await pilot.pause()
        assert app.queued_strip.text == (
            '▹ queued next: "actually, this instead" · runs when this turn ends'
        )
        state = app.footer_bar.state
        assert state.queued == 1
        assert footer_left_text(state).endswith(" · q1")

        # Turn end → the queued message auto-runs as its own turn. Record
        # the notice order: the pickup notice must land AFTER the
        # runtime's end notice (mockup drainQueue), so it stays visible.
        seen: list[str] = []
        original_show = app.notice_slot.show_notice

        def _spy(text: str, duration: float | None = None) -> None:
            seen.append(text)
            original_show(text, duration)

        app.notice_slot.show_notice = _spy  # type: ignore[method-assign]
        adapter.release()
        assert await wait_for(pilot, lambda: app.approval_bar is not None)
        await pilot.press("enter")  # Allow once
        assert await wait_for(pilot, lambda: rules(app) >= 2)
        assert await wait_for(pilot, lambda: rules(app) >= 3 and not app.turn_active)
        assert seen.index(BUILD_END_NOTICE) < seen.index("queued message picked up")
        # The drained turn runs without a setMode (mockup drainQueue), so
        # its scripted mode notice never overwrites the pickup notice.
        assert AUTO_MODE_NOTICE not in seen
        # Auto-drained: strip cleared, footer back to q0.
        assert app.queued_strip.queued is None and not app.queued_strip.display
        assert app.footer_bar.state.queued == 0
        assert not adapter.steering.pending
        # The drained message is echoed verbatim as the user line (mockup
        # drainQueue: ``this.userLine(next)``) before the scripted turn runs.
        assert any(
            b.text == "actually, this instead" for b in blocks_of(app, "user_line")
        )


@pytest.mark.asyncio
async def test_leftover_steer_discarded_at_turn_end() -> None:
    """Mockup state machine: a steer not consumed by a step boundary is
    silently discarded at turn end (runTurn start resets ``this.steer``)
    — it never rolls forward as a turn the user never sent."""
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        await type_text(pilot, "never applied")
        await pilot.press("enter")
        await pilot.pause()
        assert len(blocks_of(app, "steer_echo")) == 1

        # Interrupt: the turn ends before any boundary consumes the steer.
        await pilot.press("escape")
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        await pilot.pause(0.2)

        # Discarded silently: nothing queued, no auto-run, echo removed.
        assert not adapter.steering.pending
        assert not blocks_of(app, "steer_echo")
        assert rules(app) == 2 and not app.turn_active
        assert app.footer_bar.state.queued == 0


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
        # chat mode: the build turn parks at its pytest approval, giving a
        # stable mid-turn state for the assertions below (§4 amendment:
        # the app boots in auto, where the instant turn races to done).
        await set_mode(pilot, app, "chat")
        await type_text(pilot, "hi")
        await pilot.press("shift+enter")
        # Mockup send(): the typed text is echoed verbatim as the user line.
        assert await wait_for(
            pilot,
            lambda: any(b.text == "hi" for b in blocks_of(app, "user_line")),
        )
        assert app.turn_active
        assert not app.adapter.steering.pending  # nothing queued
