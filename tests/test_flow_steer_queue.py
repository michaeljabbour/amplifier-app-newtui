"""Flow tests — DESIGN-SPEC §5: steer vs queue composer semantics.

End-to-end over the gated DemoRuntime: Enter mid-turn steers (↳ echo,
notice, applied at the next step boundary as ``Applying steer: <text>``
with the consumed echo removed); Shift+Enter mid-turn queues a full
next-turn message (``▹ queued next:`` strip + footer ``q1`` + auto-drain
at turn end); a second steer queues; idle Shift+Enter just sends.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel.demo import AUTO_MODE_NOTICE, BUILD_END_NOTICE
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.app_support import QUEUED_NOTICE, STEER_NOTICE
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.footer import footer_left_text, footer_right_text
from amplifier_app_tui.ui.queued_strip import RECALL_HINT
from amplifier_app_tui.ui.transcript import render_block

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


async def _start_gated_turn(pilot, app: TuiApp) -> None:
    """Seed, switch to chat (the app boots in auto — §4 amendment) so the
    build turn keeps its pytest approval, then park it mid-turn on the gate."""
    await seed_done(pilot, app)
    await set_mode(pilot, app, "chat")
    await type_text(pilot, "hi")
    await pilot.press("enter")
    assert await wait_for(pilot, lambda: app.turn_active and blocks_of(app, "narration"))


@pytest.mark.asyncio
async def test_enter_mid_turn_steers_echo_and_applies_at_step_boundary() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
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
        assert line == ('  ↳ steer queued: "focus on the tests" · applies at next step boundary')
        assert app.notice_slot.current == STEER_NOTICE
        assert app.footer_bar.state.queued == 0  # steers are not the qN badge

        # Release the turn: the steer applies at the next step boundary.
        adapter.release()
        assert await wait_for(pilot, lambda: app.approval_bar is not None)
        # The narration event can still be in the queue when the approval
        # bar mounts (separate events) — poll, don't assert instantly.
        assert await wait_for(
            pilot,
            lambda: any(
                b.text == "Applying steer: focus on the tests" for b in blocks_of(app, "narration")
            ),
        )
        # Consumed steer removed: echo gone, queue empty.
        assert await wait_for(pilot, lambda: not blocks_of(app, "steer_echo"))
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
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)

        # Running + Shift+Enter → full next-turn message queued + notice.
        await type_text(pilot, "ship the follow-up")
        await pilot.press("shift+enter")
        await pilot.pause()
        assert app.queued_strip.display
        assert app.queued_strip.text == (
            f'▹ queued next: "ship the follow-up" · runs when this turn ends · {RECALL_HINT}'
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
            f'▹ queued next: "actually, this instead" · runs when this turn ends · {RECALL_HINT}'
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
        # The pickup notice is a deferred queue duty — poll for it before
        # asserting its order (it can trail the last rule under load).
        assert await wait_for(pilot, lambda: "queued message picked up" in seen)
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
        assert any(b.text == "actually, this instead" for b in blocks_of(app, "user_line"))


@pytest.mark.asyncio
async def test_alt_up_recalls_queued_message_then_enter_steers_now() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        await type_text(pilot, "interject with this")
        await pilot.press("shift+enter")
        await pilot.pause()

        await pilot.press("alt+up")
        await pilot.pause()
        assert not adapter.steering.pending_next_turn
        assert not app.queued_strip.display
        assert app.footer_bar.state.queued == 0
        assert app.composer.text == "interject with this"
        assert app.notice_slot.current == (
            "queued message recalled · enter steers now · shift+enter requeues"
        )

        await pilot.press("enter")
        await pilot.pause()
        assert [message.text for message in adapter.steering.pending_steers] == [
            "interject with this"
        ]
        assert not adapter.steering.pending_next_turn
        assert [block.text for block in blocks_of(app, "steer_echo")] == ["interject with this"]
        adapter.release()


@pytest.mark.asyncio
async def test_alt_up_recalls_preserved_queue_after_turn_is_idle() -> None:
    """A q1 preserved behind a draft must not become unreachable at turn end.

    Queue drain deliberately refuses to overwrite a composer draft.  Once the
    user parks or clears that draft, the strip's advertised Alt-Up action must
    still work even though the producing turn has already finished.
    """
    adapter = DemoRuntimeAdapter(instant=True)
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        adapter.steering.enqueue("recover this turn", kind="next_turn")
        app.queued_strip.show_queued("recover this turn")
        app._refresh_footer()

        assert not app.turn_active
        assert app.footer_bar.state.context == "idle"
        assert app.footer_bar.state.queued == 1

        await pilot.press("alt+up")
        await pilot.pause()

        assert not adapter.steering.pending_next_turn
        assert not app.queued_strip.display
        assert app.footer_bar.state.queued == 0
        assert app.composer.text == "recover this turn"
        assert app.notice_slot.current == (
            "queued message recalled · enter sends now · shift+enter requeues"
        )


@pytest.mark.asyncio
async def test_recall_preserves_existing_draft_and_queued_message() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        await type_text(pilot, "queued text")
        await pilot.press("shift+enter")
        await type_text(pilot, "draft in progress")

        await pilot.press("alt+up")
        await pilot.pause()
        assert app.composer.text == "draft in progress"
        assert [message.text for message in adapter.steering.pending_next_turn] == ["queued text"]
        assert app.queued_strip.display
        assert app.notice_slot.current == "composer has a draft · queued message kept"
        adapter.release()


@pytest.mark.asyncio
async def test_recall_preserves_rich_draft_sidecars_and_queued_message() -> None:
    """Alt-Up must not flatten or overwrite an in-progress paste/image draft."""
    from amplifier_app_tui.kernel.clipboard import ImageAttachment

    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    payload = "\n".join(f"draft row {index}" for index in range(20))
    image = ImageAttachment(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        await type_text(pilot, "queued text")
        await pilot.press("shift+enter")

        stub = app.composer.register_paste(payload)
        assert stub is not None
        app.composer.insert_text(f"review {stub} ")
        app.composer.add_image(image)
        rich_draft = app.composer.text

        await pilot.press("alt+up")
        await pilot.pause()

        assert app.composer.text == rich_draft
        assert payload in app.composer._expand(app.composer.text)
        assert app.composer._staged_attachments(app.composer.text) == (image,)
        assert [message.text for message in adapter.steering.pending_next_turn] == ["queued text"]
        assert app.queued_strip.display
        assert app.notice_slot.current == "composer has a draft · queued message kept"
        adapter.release()


@pytest.mark.asyncio
async def test_queued_image_survives_recall_and_can_be_requeued() -> None:
    from amplifier_app_tui.kernel.clipboard import ImageAttachment

    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    payload = "\n".join(f"queued row {index}" for index in range(20))
    image = ImageAttachment(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        stub = app.composer.register_paste(payload)
        assert stub is not None
        app.composer.insert_text(f"inspect {stub} ")
        app.composer.add_image(image)
        visible_draft = app.composer.text
        await pilot.press("shift+enter")
        await pilot.pause()

        queued = adapter.steering.pending_next_turn[0]
        assert payload in queued.text
        assert queued.attachments == (image,)
        assert queued.draft is not None
        await pilot.press("alt+up")
        await pilot.pause()
        assert app.composer.text == visible_draft
        assert payload in app.composer._expand(app.composer.text)
        assert app.composer._staged_attachments(app.composer.text) == (image,)

        # Active-turn steering is text-only: Enter keeps the exact rich draft
        # and teaches the full-turn queue chord instead of dropping image bytes.
        await pilot.press("enter")
        await pilot.pause()
        assert not adapter.steering.pending_steers
        assert not adapter.steering.pending_next_turn
        assert app.composer.text == visible_draft
        assert app.notice_slot.current == (
            "images need a full turn · draft kept · shift+enter queues"
        )

        await pilot.press("shift+enter")
        await pilot.pause()
        requeued = adapter.steering.pending_next_turn[0]
        assert requeued.attachments == (image,)
        assert requeued.draft is not None
        adapter.release()


@pytest.mark.asyncio
async def test_oversized_queue_rejection_restores_exact_rich_draft() -> None:
    from amplifier_app_tui.kernel.clipboard import ImageAttachment
    from amplifier_app_tui.model.queues import MAX_ITEM_CHARS

    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    payload = "x" * (MAX_ITEM_CHARS + 1)
    image = ImageAttachment(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        stub = app.composer.register_paste(payload)
        assert stub is not None
        app.composer.insert_text(f"review {stub} ")
        app.composer.add_image(image)
        rich_draft = app.composer.text

        await pilot.press("shift+enter")
        await pilot.pause()

        assert not adapter.steering.pending_next_turn
        assert app.composer.text == rich_draft
        assert app.composer._expand(app.composer.text).startswith("review " + payload)
        assert app.composer._staged_attachments(app.composer.text) == (image,)
        assert "32,768 character limit" in app.notice_slot.current
        adapter.release()


@pytest.mark.asyncio
async def test_recall_keeps_queue_while_current_steer_is_waiting() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _start_gated_turn(pilot, app)
        await type_text(pilot, "first steer")
        await pilot.press("enter")
        await type_text(pilot, "next turn unless recalled")
        await pilot.press("shift+enter")

        await pilot.press("alt+up")
        await pilot.pause()
        assert [message.text for message in adapter.steering.pending_steers] == ["first steer"]
        assert [message.text for message in adapter.steering.pending_next_turn] == [
            "next turn unless recalled"
        ]
        assert app.notice_slot.current == "current steer already waiting · queued message kept"
        adapter.release()


@pytest.mark.asyncio
async def test_leftover_steer_discarded_at_turn_end() -> None:
    """Mockup state machine: a steer not consumed by a step boundary is
    silently discarded at turn end (runTurn start resets ``this.steer``)
    — it never rolls forward as a turn the user never sent."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
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
    app = TuiApp(adapter)
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
            f'▹ queued next: "second message" · runs when this turn ends · {RECALL_HINT}'
        )
        assert app.footer_bar.state.queued == 1
        assert len(adapter.steering.pending_steers) == 1
        adapter.release()  # let the parked script finish cleanly


@pytest.mark.asyncio
async def test_idle_shift_enter_just_sends() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
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
