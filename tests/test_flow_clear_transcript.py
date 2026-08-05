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

import asyncio

import pytest

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import EvidenceBlock
from amplifier_app_tui.model.evidence import EvidenceLink
from amplifier_app_tui.ui import app_support
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.composer import ComposerInput
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.transcript import BlockWidget

from .test_flow_helpers import SIZE, blocks_of, seed_done, type_text, wait_for

ROOT = "root-session"


async def _fake_clear_context() -> tuple[bool, int]:
    return (True, 4)


async def _run_clear(pilot, app: TuiApp) -> None:
    """Type ``/clear`` + enter, the same path a real user takes."""
    await type_text(pilot, "/clear")
    await pilot.press("enter")
    await wait_for(pilot, lambda: not app.transcript.blocks)


async def _start_active_clear(pilot, app: TuiApp) -> None:
    """Request /clear during a turn and wait for its interrupt fence."""
    await type_text(pilot, "/clear")
    await pilot.press("enter")
    assert await wait_for(pilot, lambda: app.session_ops.clear_pending)


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
        assert app.ledger.checkpoints == ()
        assert app.reducer.turn_base == 0
        assert app._checkpoint_drafts == {}


@pytest.mark.asyncio
async def test_clear_rebases_next_checkpoint_and_restore_to_empty_context() -> None:
    """After /clear, the next prompt is t1/before-turn 0 and is restorable."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await _run_clear(pilot, app)

        await type_text(pilot, "first after clear")
        await pilot.press("enter")
        assert await wait_for(
            pilot,
            lambda: len(app.ledger.checkpoints) == 1 and not app.turn_active,
        )
        checkpoint = app.ledger.checkpoints[0]
        assert checkpoint.id == "t1"
        assert checkpoint.turn_id == 1
        assert checkpoint.before_turn_id == 0
        assert checkpoint.label == "first after clear"

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.current == checkpoint
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: not app.fork_pending)

        assert app.ledger.checkpoints == ()
        assert app.composer.text == "first after clear"


@pytest.mark.asyncio
async def test_prompt_entered_during_delayed_clear_is_kept_then_sends_afterward() -> None:
    """A late clear result cannot erase a prompt entered on the next UI tick."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _delayed_clear_context() -> tuple[bool, int]:
        started.set()
        await release.wait()
        return (True, 4)

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _delayed_clear_context
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        await type_text(pilot, "/clear")
        await pilot.press("enter")
        assert await wait_for(pilot, started.is_set)
        assert app.session_ops.clear_pending

        await type_text(pilot, "keep this after clear")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.composer.text == "keep this after clear")
        assert len(app.ledger.checkpoints) == 1  # only the pre-clear seed
        assert "context clear in progress" in app.notice_slot.current

        release.set()
        assert await wait_for(pilot, lambda: not app.session_ops.clear_pending)
        assert app.transcript.blocks == ()
        assert app.ledger.checkpoints == ()
        assert app.composer.text == "keep this after clear"

        await pilot.press("enter")
        assert await wait_for(
            pilot,
            lambda: len(app.ledger.checkpoints) == 1 and not app.turn_active,
        )
        checkpoint = app.ledger.checkpoints[0]
        assert checkpoint.id == "t1"
        assert checkpoint.before_turn_id == 0
        assert checkpoint.label == "keep this after clear"


@pytest.mark.parametrize("submit_key", ["enter", "shift+enter"])
@pytest.mark.asyncio
async def test_input_during_active_clear_is_kept_not_steered_or_queued(
    submit_key: str,
) -> None:
    """An active-turn clear owns admission until the interrupted turn closes.

    Enter would normally steer and Shift+Enter would normally queue while a
    turn is active.  Once /clear has installed its pending fence, both paths
    must restore the exact draft instead so the confirmed clear cannot erase
    input typed after the command.
    """
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context

    async def _accept_interrupt() -> bool:
        return True

    app.adapter.interrupt = _accept_interrupt
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="still running", ts=1.0))
        await pilot.pause()

        await _start_active_clear(pilot, app)
        await type_text(pilot, "keep this through active clear")
        await pilot.press(submit_key)
        assert await wait_for(
            pilot,
            lambda: app.composer.text == "keep this through active clear",
        )
        assert app.adapter.steering.pending_steers == ()
        assert app.adapter.steering.pending_next_turn == ()
        assert "context clear in progress" in app.notice_slot.current

        app.reducer.handle(ev.PromptComplete(session_id=ROOT, response="stopped", ts=2.0))
        assert await wait_for(pilot, lambda: not app.session_ops.clear_pending)
        assert app.transcript.blocks == ()
        assert app.composer.text == "keep this through active clear"


@pytest.mark.asyncio
async def test_checkpoint_restore_cannot_race_a_delayed_clear() -> None:
    """Clear and rewind are mutually exclusive context mutations."""
    started = asyncio.Event()
    release = asyncio.Event()
    restore_called = False

    async def _delayed_clear_context() -> tuple[bool, int]:
        started.set()
        await release.wait()
        return (True, 4)

    async def _unexpected_restore(*_args, **_kwargs):
        nonlocal restore_called
        restore_called = True
        raise AssertionError("restore crossed the clear fence")

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _delayed_clear_context
    app.adapter.restore_checkpoint = _unexpected_restore
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        checkpoint = app.ledger.checkpoints[-1]

        await type_text(pilot, "/clear")
        await pilot.press("enter")
        assert await wait_for(pilot, started.is_set)

        app_support.handle_restore(app, checkpoint.id, "both")
        await pilot.pause()
        assert not restore_called
        assert not app.fork_pending
        assert app.notice_slot.current == "context clear in progress · rewind unavailable"

        release.set()
        assert await wait_for(pilot, lambda: not app.session_ops.clear_pending)
        assert app.transcript.blocks == ()


@pytest.mark.asyncio
async def test_clear_cannot_race_an_inflight_manual_compaction() -> None:
    """The two context mutators never run concurrently from slash commands."""
    compact_started = asyncio.Event()
    compact_release = asyncio.Event()
    clear_called = False

    async def _delayed_compact(_focus: str = "") -> tuple[bool, str]:
        compact_started.set()
        await compact_release.wait()
        return (True, "4 → 1 messages")

    async def _unexpected_clear() -> tuple[bool, int]:
        nonlocal clear_called
        clear_called = True
        raise AssertionError("clear crossed the compaction fence")

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.compact = _delayed_compact
    app.adapter.clear_context = _unexpected_clear
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        await type_text(pilot, "/compact tests")
        await pilot.press("enter")
        assert await wait_for(pilot, compact_started.is_set)

        await type_text(pilot, "/clear")
        await pilot.press("enter")
        await pilot.pause()
        assert not clear_called
        assert not app.session_ops.clear_pending
        assert app.notice_slot.current == "context compaction in progress · clear unavailable"

        compact_release.set()
        assert await wait_for(
            pilot,
            lambda: app.notice_slot.current == "compacted · 4 → 1 messages",
        )


@pytest.mark.asyncio
async def test_prompt_entered_during_manual_compaction_is_kept_then_sends() -> None:
    """Manual compaction owns admission without eating the next prompt."""
    compact_started = asyncio.Event()
    compact_release = asyncio.Event()

    async def _delayed_compact(_focus: str = "") -> tuple[bool, str]:
        compact_started.set()
        await compact_release.wait()
        return (True, "4 → 1 messages")

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.compact = _delayed_compact
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        await type_text(pilot, "/compact tests")
        await pilot.press("enter")
        assert await wait_for(pilot, compact_started.is_set)

        await type_text(pilot, "send after compaction")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.composer.text == "send after compaction")
        assert app.notice_slot.current == "context compaction in progress · message kept"

        compact_release.set()
        assert await wait_for(
            pilot,
            lambda: app.notice_slot.current == "compacted · 4 → 1 messages",
        )
        await pilot.press("enter")
        assert await wait_for(
            pilot,
            lambda: (
                app.ledger.checkpoints[-1].label == "send after compaction" and not app.turn_active
            ),
        )


@pytest.mark.asyncio
async def test_clear_mid_stream_fences_stale_events_but_new_turns_still_render() -> None:
    """AC5 streaming + AC3: clearing mid-answer empties the view immediately;
    the rest of that SAME (now-stale) turn cannot repaint anything
    afterward, but a genuinely new turn renders completely normally."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context

    async def _accept_interrupt() -> bool:
        return True

    app.adapter.interrupt = _accept_interrupt
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="write a poem", ts=1.0))
        app.reducer.handle(
            ev.StreamBlockStart(session_id=ROOT, request_id="r1", block_type="text", ts=2.0)
        )
        await pilot.pause()
        assert app.transcript.blocks

        await _start_active_clear(pilot, app)
        assert app.turn_active

        # Clear first interrupts and waits for PromptComplete; it never mutates
        # the live context concurrently with a provider turn. The in-flight
        # tail may settle internally, then the confirmed clear removes it all.
        app.reducer.tick(10.0)
        app.reducer.handle(
            ev.ContentBlockEnd(
                session_id=ROOT,
                block_type="text",
                block={"type": "text", "text": "Roses are red, violets are blue."},
                ts=2.5,
            )
        )
        app.reducer.handle(ev.PromptComplete(session_id=ROOT, response="Roses are red.", ts=3.0))
        assert await wait_for(pilot, lambda: not app.session_ops.clear_pending)
        assert app.transcript.blocks == ()
        assert not app.turn_active
        assert app.ledger.checkpoints == ()

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

    async def _accept_interrupt() -> bool:
        return True

    app.adapter.interrupt = _accept_interrupt
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

        await _start_active_clear(pilot, app)
        assert app.turn_active

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
        app.reducer.handle(ev.PromptComplete(session_id=ROOT, response="done", ts=4.0))
        assert await wait_for(pilot, lambda: not app.session_ops.clear_pending)
        assert app.transcript.blocks == ()
        assert not app.turn_active
        assert app.ledger.checkpoints == ()


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


@pytest.mark.asyncio
async def test_clear_while_evidence_panel_open_closes_the_panel_too() -> None:
    """D7 x D3 composition guarantee: the evidence detail panel (D7 AC3)
    keys its captured focus/scroll anchor to one specific block, but
    ``clear_view()`` (D3) unmounts EVERY block unconditionally. A panel
    left open across a ``/clear`` would otherwise keep showing detail for
    a row that no longer exists -- a dangling reference to an unmounted
    block. Mirrors ``on_close_evidence``'s existing "whole block gone"
    handling (tests/test_ui_evidence_detail_flow.py), just triggered by a
    bulk clear instead of a single esc."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _fake_clear_context
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        link = EvidenceLink(claim_quote="tests pass", tool_ref="pytest run", tool_call_id="c1")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        widget.action_evidence_detail()
        await pilot.pause()
        assert app.evidence_panel.is_open  # sanity: the panel is actually open first

        await _run_clear(pilot, app)

        assert app.transcript.blocks == ()
        assert not app.evidence_panel.is_open
        assert app.evidence_panel.detail is None
        assert app.composer.query_one(ComposerInput).has_focus
