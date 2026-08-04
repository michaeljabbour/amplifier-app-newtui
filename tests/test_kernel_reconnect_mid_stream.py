"""D6 AC5: reconnect/replay genuinely MID-STREAM (compliance follow-up).

Every other resume/replay test (``test_kernel_rewind_replay.py``) starts
its persisted log at a clean turn boundary. This file drives the other
real scenario AC5 asks for: the persisted ``ui-events.jsonl`` log for a
fan-out turn that is cut off WHILE genuinely streaming -- two child agents
spawned, real durable content already landed for each (``content_block:end``
records -- the normalize boundary Channel A deltas never reach the log
through, see ``kernel/runtime.py:_REPLAY_STREAM_KINDS``), but NEITHER child
completed and the turn itself never closed. That is what "the connection
dropped mid-stream" looks like on disk.

Drives the real production path throughout: :class:`SessionStore` writes
the log, :func:`restored_ui_events` reads it back, and the real
:class:`TranscriptReducer` (not a bespoke helper) replays it -- exactly
:func:`amplifier_app_tui.ui.app_support.announce_ready`'s own call. Then a
brand-new, genuinely live turn continues on the SAME reducer (the
"reconnected session keeps going" half), proving the reconnect boundary
neither duplicates nor reorders anything, and that D6 AC4's turn labeling
correctly tells the replayed (turn 1, cancelled) lane apart from the fresh
one (turn 2, running).
"""

from __future__ import annotations

from pathlib import Path

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.kernel.runtime import restored_ui_events
from amplifier_app_tui.model.blocks import DelegateSummaryBlock, TranscriptBlock, TurnRule

from .test_ui_reducer_delegates import SID, _env, make_reducer

RESEARCHER_SUB = "s1"
CODER_SUB = "s2"


def _child_env(sub: str, ts: float, n: int = 0) -> dict:
    return {"event_id": f"c{ts}-{n}", "session_id": sub, "parent_id": SID, "ts": ts}


def _mid_stream_cut_log() -> list[ev.UIEvent]:
    """A fan-out turn's persisted log, cut off mid-stream (no completions,
    no ``prompt_complete``): the scenario a real reconnect resumes from."""
    return [
        ev.SessionStart(**_env(0.0)),
        ev.PromptSubmit(**_env(1.0), prompt="research the flaky suite and fix it"),
        ev.ToolPre(
            **_env(1.2),
            tool_name="delegate",
            tool_call_id="d1",
            tool_input={"agent": "researcher", "instruction": "find the flaky tests"},
        ),
        ev.AgentSpawned(
            **_env(1.5), agent="researcher", sub_session_id=RESEARCHER_SUB, parent_session_id=SID
        ),
        ev.ToolPre(
            **_env(1.6),
            tool_name="delegate",
            tool_call_id="d2",
            tool_input={"agent": "coder", "instruction": "fix whatever researcher finds"},
        ),
        ev.AgentSpawned(
            **_env(1.7), agent="coder", sub_session_id=CODER_SUB, parent_session_id=SID
        ),
        # Real durable content already landed for BOTH children -- the
        # connection dropped while they were genuinely still going, not
        # before either produced anything.
        ev.ContentBlockEnd(
            **_child_env(RESEARCHER_SUB, 2.0),
            block_type="text",
            block={"text": "Scanning CI history for retries."},
        ),
        ev.ToolPost(
            **_child_env(RESEARCHER_SUB, 2.5),
            tool_name="read_file",
            tool_call_id="t1",
            tool_input={"path": "ci.log"},
            result={"success": True},
        ),
        ev.ContentBlockEnd(
            **_child_env(CODER_SUB, 2.2),
            block_type="text",
            block={"text": "Patching the retry decorator."},
        ),
        # -- log ends here: no AgentCompleted for either child, no
        # PromptComplete for the turn. Genuinely mid-stream.
    ]


def _persist(tmp_path: Path, sid: str, log: list[ev.UIEvent]) -> SessionStore:
    store = SessionStore(base_dir=tmp_path)
    for event in log:
        store.append_event(sid, event)
    return store


def _delegate_summaries(blocks: list[TranscriptBlock]) -> list[DelegateSummaryBlock]:
    return [b for b in blocks if isinstance(b, DelegateSummaryBlock)]


def _turn_rules(blocks: list[TranscriptBlock]) -> list[TurnRule]:
    return [b for b in blocks if isinstance(b, TurnRule)]


def test_mid_stream_cut_replay_settles_both_lanes_exactly_once(tmp_path: Path) -> None:
    """Replaying a mid-stream cut never duplicates a lifecycle beat: both
    lanes settle to cancelled exactly once, the delegate summary appears
    exactly once, and exactly one interrupted TurnRule closes it out."""
    store = _persist(tmp_path, SID, _mid_stream_cut_log())
    restored = restored_ui_events(store, SID)

    reducer, host = make_reducer()
    assert reducer.replay(restored, turn_base=1) is True

    # Both lanes exist, both settled cancelled (a still-running delegate at
    # turn close-out is stranded, never left claiming live work), and both
    # correctly stamped with the turn that spawned them (D6 AC4).
    records = {r.session_id: r for r in reducer.lanes.lanes}
    assert set(records) == {RESEARCHER_SUB, CODER_SUB}
    assert records[RESEARCHER_SUB].lane.state == "cancelled"
    assert records[CODER_SUB].lane.state == "cancelled"
    assert records[RESEARCHER_SUB].turn == 1
    assert records[CODER_SUB].turn == 1

    # The durable content that DID land survived the replay, exactly once
    # each -- no duplication, no loss.
    researcher_transcript = reducer.lane_transcript(RESEARCHER_SUB)
    coder_transcript = reducer.lane_transcript(CODER_SUB)
    assert researcher_transcript is not None and coder_transcript is not None
    researcher_text = " ".join(
        "".join(s.text for s in b.spans) for b in researcher_transcript if b.kind == "answer"
    )
    coder_text = " ".join(
        "".join(s.text for s in b.spans) for b in coder_transcript if b.kind == "answer"
    )
    assert researcher_text.count("Scanning CI history for retries.") == 1
    assert coder_text.count("Patching the retry decorator.") == 1

    # Exactly one delegate summary (append-once/replace-in-place, D5) and
    # exactly one turn rule (interrupted, never shipped) -- a replay bug
    # duplicating either would be immediately visible here.
    summaries = _delegate_summaries(host.blocks)
    assert len(summaries) == 1
    assert {e.agent for e in summaries[0].entries} == {"researcher", "coder"}
    rules = _turn_rules(host.blocks)
    assert len(rules) == 1
    assert rules[0].shipped is False

    # D6's own guarantee holds through a replay too: neither child's own
    # prose ever reached the root transcript.
    root_text = " ".join(
        "".join(s.text for s in b.spans) for b in host.blocks if b.kind == "answer"
    )
    assert "Scanning CI history" not in root_text
    assert "Patching the retry decorator" not in root_text


def test_reconnect_replay_then_live_continuation_never_duplicates_or_reorders(
    tmp_path: Path,
) -> None:
    """The other half of "reconnect during streaming": after the
    mid-stream-cut log replays, the SAME reducer keeps going with a
    genuinely NEW live turn (the reconnected session continuing) -- proving
    the reconnect boundary neither duplicates nor reorders anything, and
    that a same-named agent in the new turn is told apart from the
    replayed one by D6 AC4's turn label, not just by sub-session id."""
    store = _persist(tmp_path, SID, _mid_stream_cut_log())
    reducer, host = make_reducer()
    assert reducer.replay(restored_ui_events(store, SID), turn_base=1) is True

    replayed_block_count = len(host.blocks)
    replayed_rule_count = len(_turn_rules(host.blocks))
    assert replayed_rule_count == 1

    # The reconnected session resumes with a fresh turn: a NEW sub-session
    # (a real orchestrator never reuses a dead child's id) for the SAME
    # agent name as before.
    reducer.handle(ev.PromptSubmit(**_env(10.0), prompt="keep going"))
    reducer.handle(
        ev.AgentSpawned(
            **_env(10.5), agent="researcher", sub_session_id="s3", parent_session_id=SID
        )
    )
    reducer.handle(
        ev.ContentBlockEnd(
            **_child_env("s3", 11.0),
            block_type="text",
            block={"text": "Re-scanning CI history with the new flag."},
        )
    )
    reducer.handle(
        ev.AgentCompleted(
            **_env(12.0),
            agent="researcher",
            sub_session_id="s3",
            parent_session_id=SID,
            success=True,
            result="fixed",
        )
    )
    reducer.handle(ev.PromptComplete(**_env(13.0), response="done"))

    # No duplication: the two OLD lanes are exactly as replay left them,
    # PLUS one new one -- three total, never two-replayed-become-four or a
    # collapsed/overwritten pair.
    records = {r.session_id: r for r in reducer.lanes.lanes}
    assert set(records) == {RESEARCHER_SUB, CODER_SUB, "s3"}
    assert records[RESEARCHER_SUB].lane.state == "cancelled"
    assert records[CODER_SUB].lane.state == "cancelled"
    assert records["s3"].lane.state == "done"

    # D6 AC4: same agent name, told apart by turn -- the replayed
    # researcher stays turn 1, the reconnected one is turn 2.
    assert records[RESEARCHER_SUB].turn == 1
    assert records["s3"].turn == 2

    # No reordering: everything the replay produced still comes first, in
    # the same order, then the new turn's own content strictly after it --
    # never interleaved, never rewound.
    first_new_block = host.blocks[replayed_block_count]
    assert first_new_block.kind == "user_line"
    assert first_new_block.text == "keep going"
    rules = _turn_rules(host.blocks)
    assert len(rules) == 2
    assert rules[0].shipped is False  # the replayed turn's own interrupted rule
    assert host.blocks.index(rules[0]) < host.blocks.index(rules[1])

    # The new turn's own child content still never mirrors to the root.
    root_text = " ".join(
        "".join(s.text for s in b.spans) for b in host.blocks if b.kind == "answer"
    )
    assert "Re-scanning CI history" not in root_text


def test_replaying_the_same_mid_stream_cut_twice_is_deterministic(tmp_path: Path) -> None:
    """No hidden nondeterminism in replay itself: the SAME persisted,
    mid-stream-cut log replayed into two independent fresh reducers
    produces byte-identical transcripts -- the strongest form of "no
    duplication/reordering" a reconnect can be held to."""
    store = _persist(tmp_path, SID, _mid_stream_cut_log())
    restored = restored_ui_events(store, SID)

    reducer_a, host_a = make_reducer()
    reducer_b, host_b = make_reducer()
    assert reducer_a.replay(restored, turn_base=1) is True
    assert reducer_b.replay(restored, turn_base=1) is True

    assert host_a.blocks == host_b.blocks
    assert [r.turn for r in reducer_a.lanes.lanes] == [r.turn for r in reducer_b.lanes.lanes]
    assert [r.lane.state for r in reducer_a.lanes.lanes] == [
        r.lane.state for r in reducer_b.lanes.lanes
    ]
