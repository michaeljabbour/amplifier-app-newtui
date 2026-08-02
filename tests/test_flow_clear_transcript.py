"""Flow tests -- /clear visibly clears the transcript (D3, Compliance
2026-08-02): "does not remove displayed transcript rows" is fixed end to
end here, driving the REAL command path (composer -> command registry ->
SessionOpsController -> TuiApp.clear_transcript_view) over the demo
runtime through Textual's Pilot, exactly like the other ``test_flow_*``
suites.

``app.adapter.clear_context`` is monkeypatched to a small deterministic
fake per test (the demo/real split of that op is orthogonal to this
compliance item -- kernel/session_ops.py and its own tests already cover
whether the underlying context actually clears); what matters here is
that a SUCCESSFUL clear empties the view, keeps the composer focused, and
fences delayed events, exactly per AC1-AC5.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.composer import ComposerInput
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

from .test_flow_helpers import SIZE, blocks_of, seed_done, type_text, wait_for

ROOT = "root-session"


async def _fake_clear_context() -> tuple[bool, int]:
    return (True, 4)


async def _run_clear(pilot, app: TuiApp) -> None:
    """Type ``/clear`` + enter, the same path a real user takes."""
    await type_text(pilot, "/clear")
    await pilot.press("enter")
    await wait_for(pilot, lambda: not app.transcript.blocks)


@pytest.mark.asyncio
async def test_clear_idle_empties_the_view_and_keeps_composer_focus() -> None:
    """AC1/AC2 idle: /clear removes every row, shows a brief confirmation,
    and the composer keeps focus (not a leftover empty state)."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert app.transcript.blocks  # sanity: the seed turn actually rendered

        await _run_clear(pilot, app)

        assert app.transcript.blocks == ()
        assert app.composer.query_one(ComposerInput).has_focus
        assert app.notice_slot.current is not None
        assert "view cleared" in app.notice_slot.current


@pytest.mark.asyncio
async def test_clear_mid_stream_fences_stale_events_but_new_turns_still_render() -> None:
    """AC5 streaming + AC3: clearing mid-answer empties the view immediately;
    the rest of that SAME (now-stale) turn cannot repaint anything
    afterward, but a genuinely new turn renders completely normally."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="write a poem", ts=1.0))
        app.reducer.handle(
            ev.StreamBlockStart(session_id=ROOT, request_id="r1", block_type="text", ts=2.0)
        )
        await pilot.pause()
        assert app.transcript.blocks

        await _run_clear(pilot, app)
        assert app.transcript.blocks == ()

        # The stale turn's belated tail must not resurrect anything.
        app.reducer.handle(
            ev.ContentBlockEnd(
                session_id=ROOT,
                block_type="text",
                block={"type": "text", "text": "Roses are red, violets are blue."},
                ts=2.5,
            )
        )
        app.reducer.handle(ev.PromptComplete(session_id=ROOT, response="Roses are red.", ts=3.0))
        await pilot.pause()
        assert app.transcript.blocks == ()

        # A brand new turn renders completely normally in the cleared view.
        app.reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="try again", ts=4.0))
        await pilot.pause()
        assert blocks_of(app, "user_line")
        assert app.composer.query_one(ComposerInput).has_focus


@pytest.mark.asyncio
async def test_clear_while_tool_running_fences_the_delayed_result() -> None:
    """AC5 tool-running: clearing while a tool call is in flight leaves no
    trace of its belated result in the view."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="run the tests", ts=1.0))
        app.reducer.handle(
            ev.ToolPre(
                session_id=ROOT,
                tool_call_id="c1",
                tool_name="bash",
                tool_input={"command": "pytest"},
                ts=2.0,
            )
        )
        await pilot.pause()

        await _run_clear(pilot, app)
        assert app.transcript.blocks == ()

        app.reducer.handle(
            ev.ToolPost(
                session_id=ROOT,
                tool_call_id="c1",
                tool_name="bash",
                tool_input={"command": "pytest"},
                result={"status": "ok", "success": True},
                ts=3.0,
            )
        )
        await pilot.pause()
        assert app.transcript.blocks == ()


@pytest.mark.asyncio
async def test_clear_after_resume_replay_empties_the_reconstructed_view() -> None:
    """AC5 resumed session: /clear works identically on a transcript that
    arrived via resume replay, not just one built up live since boot."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        replayed = [
            ev.PromptSubmit(session_id=ROOT, prompt="resumed turn", ts=100.0),
            ev.ContentBlockEnd(
                session_id=ROOT,
                block_type="text",
                block={"type": "text", "text": "Resumed answer."},
                ts=101.0,
            ),
            ev.PromptComplete(session_id=ROOT, response="Resumed answer.", ts=102.0),
        ]
        assert (
            app.reducer.replay(replayed, turn_base=app.reducer.ledger.checkpoints[-1].turn_id)
            is True
        )
        await pilot.pause()
        assert app.transcript.blocks  # sanity: the resumed transcript actually rendered

        await _run_clear(pilot, app)
        assert app.transcript.blocks == ()
        assert app.composer.query_one(ComposerInput).has_focus


@pytest.mark.asyncio
async def test_repeated_clear_stays_empty_and_composer_keeps_focus() -> None:
    """AC5 repeated clear: back-to-back /clear never errors and the
    composer never loses focus."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        await _run_clear(pilot, app)
        assert app.transcript.blocks == ()

        await _run_clear(pilot, app)  # a second /clear on an already-empty view
        assert app.transcript.blocks == ()
        assert app.composer.query_one(ComposerInput).has_focus


@pytest.mark.asyncio
async def test_clear_unavailable_leaves_the_transcript_untouched() -> None:
    """A failed/unavailable clear must not wipe the view for a no-op."""

    async def _unavailable() -> tuple[bool, int]:
        return (False, 0)

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _unavailable
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        before = app.transcript.blocks
        assert before

        await type_text(pilot, "/clear")
        await pilot.press("enter")
        assert await wait_for(
            pilot, lambda: app.notice_slot.current == "clear unavailable in this session"
        )

        # A failed clear leaves every prior row exactly as it was -- the
        # only addition is the "/clear" invocation echoing as a user line
        # like any other command (unrelated to D3; see registry.py.run()).
        assert app.transcript.blocks[: len(before)] == before
        assert len(app.transcript.blocks) == len(before) + 1
