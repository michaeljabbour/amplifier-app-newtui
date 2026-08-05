"""Generation fencing: /clear must not let a pre-clear turn resurrect rows.

D3 (Compliance 2026-08-02): the remaining scope after context-clear was
wired up was that ``/clear`` cleared the conversation context and showed a
notice, but never removed the displayed transcript rows. Fixing the view
(``TranscriptView.clear_view``, proven in test_ui_transcript_view.py) is
only half the story: without fencing, an event already in flight when the
clear lands -- a streaming delta, a tool result, a turn close-out -- can
still reach the host afterward and repaint a row into the just-emptied
view (notably via ``replace_block``'s KeyError-to-append fallback).

This file proves the fencing half: once :meth:`TranscriptReducer.
bump_generation` runs (what ``TuiApp.clear_transcript_view`` calls
alongside the view clear), any further event for the turn that was
running BEFORE the bump dispatches through a silenced host -- no new
block, no notice, no stream delta -- even though that turn's own internal
bookkeeping (cost, ledger, lane completion) keeps running to completion
undisturbed. A brand new turn started AFTER the bump dispatches
completely normally, proving the fence lifts for genuinely new content
(AC3: new output still appears in the cleared view).

Pure reducer + FakeHost, no Textual -- mirrors test_ui_reducer_outcomes.py
and test_ui_reducer_replay.py's style.
"""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import BlockIdAllocator, TurnRule
from amplifier_app_tui.model.lanes import LaneRegistry
from amplifier_app_tui.model.turn import OutcomeLedger
from amplifier_app_tui.ui.reducer import TranscriptReducer

from .test_ui_reducer_outcomes import FakeHost, make_reducer

ROOT = "root-session"


class _LanesProbeHost(FakeHost):
    """FakeHost + a lanes_changed() counter.

    Proves the ONE deliberate pass-through in ``_StaleTurnHost``: the
    lanes panel tracks real background-agent state independently of the
    transcript, so it must keep repainting even while the transcript side
    of a stale turn is fenced.
    """

    def __init__(self, mode_id: str = "chat") -> None:
        super().__init__(mode_id)
        self.lanes_repaints = 0

    def lanes_changed(self) -> None:
        self.lanes_repaints += 1


def _reducer_with(host: FakeHost) -> TranscriptReducer:
    """Same wiring as ``make_reducer``, but for a caller-supplied host."""
    return TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )


def _snapshot(host: FakeHost) -> tuple[int, int, int]:
    """(blocks, notices, stream_events) -- the reducer's whole visible surface."""
    return (len(host.blocks), len(host.notices), len(host.stream_events))


# -- idle (AC5) ---------------------------------------------------------------


def test_idle_clear_bumps_generation_with_no_active_turn() -> None:
    """AC5 idle: /clear with nothing running is a safe, inert bump -- and
    the NEXT turn (stamped with the new generation) is unaffected."""
    reducer, host = make_reducer()
    assert reducer.generation == 0

    assert reducer.bump_generation() == 1
    assert reducer.generation == 1

    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="hello", ts=1.0))
    assert len(host.blocks) == 2  # UserLine + working_status, dispatched normally


def test_confirmed_idle_context_clear_resets_checkpoint_lineage_only() -> None:
    """A successful backend clear drops old rewind boundaries and resets
    the context offset without changing generation-fencing semantics."""
    reducer, _host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="old context", ts=1.0))
    reducer.handle(ev.PromptComplete(session_id=ROOT, response="done", ts=2.0))
    reducer.turn_base = 7
    assert reducer.ledger.checkpoints

    reducer.context_cleared()

    assert reducer.ledger.turns == ()
    assert reducer.ledger.checkpoints == ()
    assert reducer.turn_base == 0
    assert reducer.generation == 0  # presentation fencing remains a separate operation


# -- mid-stream (AC5) -----------------------------------------------------------


def test_clear_mid_stream_fences_the_delayed_tail() -> None:
    """AC5 streaming: a /clear mid-answer must not let the rest of that
    answer (already in flight when the clear landed) paint into the view,
    but a fresh turn afterward renders completely normally (AC3)."""
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="write a poem", ts=1.0))
    reducer.handle(ev.StreamBlockStart(session_id=ROOT, request_id="r1", block_type="text", ts=2.0))
    before = _snapshot(host)

    reducer.bump_generation()  # /clear lands mid-stream

    # The rest of the SAME (now-stale) turn keeps arriving: delta, the
    # durable content_block:end, and the turn's close-out.
    reducer.handle(
        ev.StreamBlockDelta(session_id=ROOT, request_id="r1", text="Roses are red", ts=2.1)
    )
    reducer.handle(
        ev.ContentBlockEnd(
            session_id=ROOT,
            block_type="text",
            block={"type": "text", "text": "Roses are red, violets are blue."},
            ts=2.5,
        )
    )
    reducer.handle(
        ev.PromptComplete(session_id=ROOT, response="Roses are red, violets are blue.", ts=3.0)
    )

    assert _snapshot(host) == before  # nothing from the stale turn reached the view

    # A brand new turn dispatches completely normally afterward.
    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="try again", ts=4.0))
    assert len(host.blocks) == before[0] + 2
    new_user_line = host.blocks[before[0]]
    assert getattr(new_user_line, "text", None) == "try again"  # genuinely new, not resurrected


def test_context_cleared_turn_accounts_cost_without_recreating_checkpoint() -> None:
    """A stale PromptComplete settles the in-flight turn but cannot write a
    checkpoint or TurnRule whose indices refer to the deleted context."""
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="expensive work", ts=1.0))
    reducer.handle(
        ev.ProviderResponseUsage(
            session_id=ROOT,
            input_tokens=100,
            output_tokens=25,
            cost_usd=Decimal("0.42"),
            ts=2.0,
        )
    )
    before = _snapshot(host)

    reducer.context_cleared()
    reducer.bump_generation()
    reducer.handle(ev.PromptComplete(session_id=ROOT, response="late", ts=3.0))

    assert not reducer.running
    assert reducer.session_cost == Decimal("0.42")
    assert reducer.ledger.turns == ()
    assert reducer.ledger.checkpoints == ()
    assert not any(isinstance(block, TurnRule) for block in host.blocks)
    assert _snapshot(host) == before


# -- tool-running (AC5) ---------------------------------------------------------


def test_clear_tool_running_fences_the_delayed_result() -> None:
    """AC5 tool-running: a /clear while a tool call is in flight must not
    let its belated tool:post/tool:error paint a row afterward."""
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="run the tests", ts=1.0))
    reducer.handle(
        ev.ToolPre(
            session_id=ROOT,
            tool_call_id="c1",
            tool_name="bash",
            tool_input={"command": "pytest"},
            ts=2.0,
        )
    )
    before = _snapshot(host)

    reducer.bump_generation()

    reducer.handle(
        ev.ToolPost(
            session_id=ROOT,
            tool_call_id="c1",
            tool_name="bash",
            tool_input={"command": "pytest"},
            result={"status": "ok", "success": True},
            ts=3.0,
        )
    )
    reducer.handle(
        ev.ToolError(
            session_id=ROOT, tool_call_id="c2", tool_name="bash", error_message="boom", ts=3.1
        )
    )
    reducer.handle(ev.PromptComplete(session_id=ROOT, response="done", ts=4.0))

    assert _snapshot(host) == before


# -- resumed session (AC5) -------------------------------------------------------


def test_clear_after_resume_replay_still_fences_a_new_post_resume_turn() -> None:
    """AC5 resumed session: replay() swaps to its own internal host proxy
    and must restore the live host cleanly -- a /clear on a turn started
    AFTER resume has to fence exactly like it would on a since-boot turn."""
    reducer, host = make_reducer()
    replayed = [
        ev.PromptSubmit(session_id=ROOT, prompt="earlier turn", ts=1.0),
        ev.ContentBlockEnd(
            session_id=ROOT,
            block_type="text",
            block={"type": "text", "text": "Done earlier."},
            ts=2.0,
        ),
        ev.PromptComplete(session_id=ROOT, response="Done earlier.", ts=3.0),
    ]
    assert reducer.replay(replayed, turn_base=0) is True
    assert reducer.generation == 0  # replay never touches the clear-generation counter
    resumed_block_count = len(host.blocks)
    assert resumed_block_count  # sanity: the resumed transcript actually rendered

    # The user keeps chatting live after resume, then clears mid-answer.
    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="keep going", ts=10.0))
    before = _snapshot(host)

    reducer.bump_generation()
    reducer.handle(
        ev.ContentBlockEnd(
            session_id=ROOT,
            block_type="text",
            block={"type": "text", "text": "stale continuation"},
            ts=11.0,
        )
    )
    reducer.handle(ev.PromptComplete(session_id=ROOT, response="stale continuation", ts=12.0))
    assert _snapshot(host) == before

    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="fresh", ts=13.0))
    assert len(host.blocks) == before[0] + 2


# -- repeated clears (AC5) --------------------------------------------------------


def test_repeated_clear_keeps_fencing_and_stays_idempotent() -> None:
    """AC5 repeated clear: two /clear in a row (nothing in between) must
    not un-fence the stale turn or otherwise misbehave."""
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="one", ts=1.0))
    before = _snapshot(host)

    assert reducer.bump_generation() == 1
    assert reducer.bump_generation() == 2  # a second /clear before anything else happens

    reducer.handle(
        ev.ContentBlockEnd(
            session_id=ROOT, block_type="text", block={"type": "text", "text": "late"}, ts=1.5
        )
    )
    assert _snapshot(host) == before

    # Close the fenced turn before submitting another prompt.  Besides
    # mirroring the runtime lifecycle, this consumes its pre-prompt
    # checkpoint without allowing the stale rule or notice into the view.
    reducer.handle(ev.PromptComplete(session_id=ROOT, response="late", ts=1.75))
    assert _snapshot(host) == before

    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="two", ts=2.0))
    assert len(host.blocks) == before[0] + 2


# -- lanes_changed keeps forwarding even while fenced ------------------------------


def test_stale_turn_agent_spawn_still_repaints_lanes_panel() -> None:
    """The lanes panel is out of D3's scope and must stay accurate even
    while the transcript side of a stale turn's fan-out is silenced."""
    host = _LanesProbeHost()
    reducer = _reducer_with(host)
    reducer.handle(ev.PromptSubmit(session_id=ROOT, prompt="fan out", ts=1.0))
    reducer.bump_generation()
    before_blocks = len(host.blocks)
    before_repaints = host.lanes_repaints

    reducer.handle(
        ev.AgentSpawned(
            session_id=ROOT,
            agent="researcher",
            sub_session_id="child-1",
            parent_session_id=ROOT,
            ts=2.0,
        )
    )

    assert len(host.blocks) == before_blocks  # the delegate-summary block is fenced
    assert host.lanes_repaints == before_repaints + 1  # but the lanes panel still learns of it
