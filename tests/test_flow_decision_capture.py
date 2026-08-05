"""End-to-end custom decision capture through the real Textual app."""

from __future__ import annotations

import asyncio
import threading
from typing import cast

import pytest

from amplifier_app_tui.ui import app_support
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.decision_capture import DecisionCaptureStrip, compact_question
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.runtime_adapter import RealRuntimeAdapter

from .test_flow_helpers import SIZE, blocks_of, seed_done, type_text, wait_for
from .test_runtime_adapter_real import FakeRealRuntime, SEAM


def _item(adapter: DemoRuntimeAdapter):
    return adapter.needs_you.defer(
        "Where should this live?",
        "location",
        choices=("Local", "Upstream"),
        custom=True,
    )


def test_compact_question_is_single_line_and_bounded() -> None:
    assert compact_question("  one\n two  ") == "one two"
    compact = compact_question("x" * 500)
    assert len(compact) == 240
    assert compact.endswith("…")


async def _begin_active_clear(app: TuiApp, pilot) -> None:
    async def _accept_interrupt() -> bool:
        return True

    async def _clear_context() -> tuple[bool, int]:
        return (True, 4)

    app.adapter.interrupt = _accept_interrupt
    app.adapter.clear_context = _clear_context
    app.turn_started()
    app.session_ops.clear_context()
    assert await wait_for(pilot, lambda: app.session_ops.clear_pending)


@pytest.mark.asyncio
async def test_custom_decision_answer_waits_for_active_clear_without_losing_text() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        item = _item(adapter)
        await _begin_active_clear(app, pilot)

        app_support.begin_custom_decision_capture(app, item.decision_id)
        await type_text(pilot, "violet-otter")
        await pilot.press("enter")
        await pilot.pause()

        assert adapter.needs_you.pending_count == 1
        assert app.composer.capturing_decision
        assert app.composer.text == "violet-otter"
        assert app.notice_slot.current == "context clear in progress · decision kept"

        app.turn_finished()
        assert await wait_for(pilot, lambda: not app.session_ops.clear_pending)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: adapter.needs_you.pending_count == 0)
        answered = next(
            row for row in adapter.needs_you.items if row.decision_id == item.decision_id
        )
        assert answered.answer == "violet-otter"


@pytest.mark.asyncio
async def test_decision_chip_waits_for_active_clear_and_remains_pending() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        item = _item(adapter)
        app.action_show_needs_you()
        await pilot.pause()
        await pilot.pause()
        await _begin_active_clear(app, pilot)
        narrations_before = tuple(
            block.text for block in app.transcript.blocks if block.kind == "narration"
        )

        await pilot.click(f"#chip-{item.decision_id}-0")
        await pilot.pause()

        assert adapter.needs_you.pending_count == 1
        assert app.notice_slot.current == "context clear in progress · decision kept"
        assert (
            tuple(block.text for block in app.transcript.blocks if block.kind == "narration")
            == narrations_before
        )

        app.turn_finished()
        assert await wait_for(pilot, lambda: not app.session_ops.clear_pending)
        assert adapter.needs_you.pending_count == 1


@pytest.mark.asyncio
async def test_running_custom_answer_is_decision_not_steer_or_queued_turn() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.composer.set_draft("keep this draft")
        app.turn_started()
        item = _item(adapter)

        app.action_show_needs_you()
        await pilot.pause()
        await pilot.pause()
        await pilot.click(f"#custom-{item.decision_id}")
        await pilot.pause()

        assert app.composer.capturing_decision
        assert app.decision_capture.display
        assert app.footer_context() == "needs_you"
        await type_text(pilot, "/status")
        await pilot.press("enter")
        await pilot.pause()

        answered = next(
            row for row in adapter.needs_you.items if row.decision_id == item.decision_id
        )
        assert answered.status == "answered"
        assert answered.answer == "/status"  # slash is literal, not a command
        assert not adapter.steering.pending
        assert not app.queued_strip.display
        assert not app.decision_capture.display
        assert not app.composer.capturing_decision
        assert app.composer.text == "keep this draft"
        assert any(
            block.text == "Applying decision: /status" for block in blocks_of(app, "narration")
        )
        app.turn_finished()


@pytest.mark.asyncio
async def test_real_adapter_shared_queue_accepts_exact_custom_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native-runtime queue and Textual answer path are the same object.

    This is deliberately one layer deeper than the demo flow above: the real
    adapter boots its runtime on a second event-loop thread, that thread parks
    the native-shaped question, and the UI loop answers it through the exact
    composer path used by a live provider session.
    """
    monkeypatch.setattr(SEAM, FakeRealRuntime)
    adapter = RealRuntimeAdapter(bundle="x")
    app = TuiApp(adapter)

    async with app.run_test(size=SIZE) as pilot:
        assert await wait_for(pilot, lambda: adapter._runtime is not None)
        runtime = cast(FakeRealRuntime, adapter._runtime)
        assert runtime.started_loop is not None
        assert runtime.kwargs["needs_you"] is adapter.needs_you

        parked = threading.Event()

        def defer_on_runtime_loop() -> None:
            adapter.needs_you.defer(
                "Which test label should I use?",
                "Test label · Auto continues while this waits",
                choices=("Alpha", "Beta"),
                descriptions=(
                    'Use "Alpha" as the test label.',
                    'Use "Beta" as the test label.',
                ),
                custom=True,
            )
            parked.set()

        runtime.started_loop.call_soon_threadsafe(defer_on_runtime_loop)
        assert await asyncio.to_thread(parked.wait, 5.0)
        assert adapter.needs_you.pending_count == 1

        app.action_show_needs_you()
        await pilot.pause()
        await pilot.pause()
        await pilot.press("3")  # Alpha=1, Beta=2, custom=3
        await pilot.pause()
        assert app.composer.capturing_decision
        assert app.decision_capture.display

        await type_text(pilot, "violet-otter")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: adapter.needs_you.pending_count == 0)

        answered = adapter.needs_you.items[0]
        assert answered.status == "answered"
        assert answered.answer == "violet-otter"
        assert not app.decision_capture.display
        assert any(
            block.text == "Applying decision: violet-otter" for block in blocks_of(app, "narration")
        )


@pytest.mark.asyncio
async def test_escape_cancels_custom_answer_without_interrupting_turn() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.composer.set_draft("original")
        app.turn_started()
        item = _item(adapter)
        app_support.begin_custom_decision_capture(app, item.decision_id)
        await type_text(pilot, "not this")

        await pilot.press("escape")
        await pilot.pause()

        assert app.turn_active  # Esc cancelled capture; it did not interrupt
        assert adapter.needs_you.pending_count == 1
        assert adapter.needs_you.pending[0].decision_id == item.decision_id
        assert not app.decision_capture.display
        assert not app.composer.capturing_decision
        assert app.composer.text == "original"
        assert app.notice_slot.current == "custom answer cancelled · decision still waiting"
        app.turn_finished()


@pytest.mark.asyncio
async def test_decision_band_renders_question_and_instructions() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        strip = app.query_one(DecisionCaptureStrip)
        strip.show_question("Pick a direction")
        await pilot.pause()
        assert strip.display
        assert strip.question == "Pick a direction"
        plain = strip.render().plain
        assert "Decision · Pick a direction" in plain
        assert "Enter submits answer" in plain
        assert "Esc cancels" in plain
