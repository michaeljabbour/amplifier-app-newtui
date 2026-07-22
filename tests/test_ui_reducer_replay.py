"""Resume replay: stored UIEvents → full transcript reconstruction.

DESIGN-SPEC §3/§11 claim tool digests, delegate summaries and turn rules
are "reconstructed from events.jsonl on resume" — previously resume
rebuilt prompts + prose only. ``TranscriptReducer.replay`` feeds the
stored events back through the live dispatch behind a side-effect-proof
host proxy; these tests pin the reconstruction AND the suppression
contract (no notices, no approval presentation, no needs-you deferrals,
no turn-lifecycle host callbacks).
"""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import (
    BlockIdAllocator,
    DelegateSummaryBlock,
    TodoItem,
    TranscriptBlock,
)
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.model.turn import OutcomeLedger
from amplifier_app_newtui.ui.reducer import TranscriptReducer

from .test_ui_reducer_delegates import SID, FakeHost, _env


class ProbeHost(FakeHost):
    """FakeHost + recorders for every side-effect surface replay must gag."""

    def __init__(self, mode_id: str = "chat") -> None:
        super().__init__(mode_id)
        self.approvals: list[str] = []
        self.deferred: list[str] = []
        self.turn_events: list[str] = []
        self.lanes_repaints = 0

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        self.approvals.append(prompt)

    def decision_deferred(self, message: str) -> None:
        self.deferred.append(message)

    def turn_started(self) -> None:
        self.turn_events.append("started")

    def turn_finished(self) -> None:
        self.turn_events.append("finished")

    def lanes_changed(self) -> None:
        self.lanes_repaints += 1


def make_reducer() -> tuple[TranscriptReducer, ProbeHost]:
    host = ProbeHost()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    return reducer, host


def _kinds(blocks: list[TranscriptBlock]) -> list[str]:
    return [b.kind for b in blocks]


def _one_turn_events() -> list[ev.UIEvent]:
    """One real shipped turn, as the runtime would have persisted it."""
    return [
        ev.PromptSubmit(**_env(0.0), prompt="fix the bug"),
        ev.ToolPre(
            **_env(1.0),
            tool_name="bash",
            tool_call_id="c1",
            tool_input={"command": "uv run pytest -q"},
        ),
        ev.ToolPost(
            **_env(2.0),
            tool_name="bash",
            tool_call_id="c1",
            tool_input={"command": "uv run pytest -q"},
            result={"success": True, "output": "ok"},
        ),
        ev.ProviderResponseUsage(**_env(2.5), input_tokens=100, output_tokens=700),
        ev.ContentBlockEnd(
            **_env(3.0),
            block_type="text",
            block={"type": "text", "text": "All done."},
        ),
        ev.PromptComplete(
            **_env(4.0),
            response="All done.",
            files_changed=2,
            diffstat="+10/−2",
            tests_ok=True,
        ),
    ]


def test_replay_rebuilds_digest_answer_and_shipped_rule() -> None:
    reducer, host = make_reducer()
    assert reducer.replay(_one_turn_events(), turn_base=1) is True

    kinds = _kinds(host.blocks)
    assert "user_line" in kinds
    assert "tool_line" in kinds  # the burst digest, not prose-only replay
    assert "answer" in kinds
    assert "turn_rule" in kinds
    assert "working_status" not in kinds  # the pulse never survives replay
    digest = next(b for b in host.blocks if b.kind == "tool_line")
    assert digest.summary == "Ran 1 shell command"
    rule = next(b for b in host.blocks if b.kind == "turn_rule")
    assert rule.shipped is True
    assert "2 files" in rule.label and "tests ✔" in rule.label
    assert "0.7k tok" in rule.label  # telemetry from the stored usage event

    # Checkpoint math stays the existing resume math (spec §9): the one
    # replayed turn IS user message 1, and the next live turn continues.
    assert [c.turn_id for c in reducer.ledger.checkpoints] == [1]
    reducer.handle(ev.PromptSubmit(**_env(10.0), prompt="next"))
    reducer.handle(ev.PromptComplete(**_env(11.0), response="ok"))
    assert [c.turn_id for c in reducer.ledger.checkpoints] == [1, 2]


def test_replay_suppresses_every_interactive_side_effect() -> None:
    reducer, host = make_reducer()
    events = [
        *_one_turn_events(),
        # Skipped kinds mixed in, as a real log would carry them.
        ev.StreamBlockDelta(**_env(1.5), text="partial"),
        ev.Notification(**_env(1.6), message="mode plan · read-only", source="mode"),
        ev.Notification(**_env(1.7), message="decision deferred", source="needs_you"),
        ev.ApprovalRequired(**_env(1.8), prompt="Allow rm?", options=("Deny",)),
        ev.ProviderNotice(**_env(1.9), notice="retry", message="throttled"),
    ]
    assert reducer.replay(events, turn_base=1) is True
    assert host.notices == []
    assert host.stream_events == []
    assert host.approvals == []
    assert host.deferred == []
    assert host.turn_events == []  # no timers/bells/queue drains from history
    assert host.lanes_repaints == 1  # exactly the one final repaint


def test_replay_closes_a_dangling_turn_as_interrupted() -> None:
    """A log that ends mid-turn (crash/kill) settles like a live Esc did."""
    reducer, host = make_reducer()
    events: list[ev.UIEvent] = [
        ev.PromptSubmit(**_env(0.0), prompt="never finished"),
        ev.ToolPre(**_env(1.0), tool_name="bash", tool_call_id="c1", tool_input={}),
    ]
    assert reducer.replay(events, turn_base=1) is True
    assert reducer.running is False
    rule = next(b for b in host.blocks if b.kind == "turn_rule")
    assert "interrupted" in rule.label
    recap_texts = [
        "".join(s.text for s in b.spans) for b in host.blocks if b.kind == "answer"
    ]
    assert any("Interrupted." in text for text in recap_texts)


def test_replay_degrades_ledger_on_transcript_mismatch() -> None:
    """Post-rewind ghost turns / truncated logs: events.jsonl is append-only
    while a confirmed fork trims the context, so the replayed checkpoint
    chain can disagree with the restored transcript's user-message count.
    The blocks stay as scrollback but the checkpoints are dropped — forking
    through them would slice the live context at the wrong turns."""
    reducer, host = make_reducer()
    two_turns = [
        ev.PromptSubmit(**_env(0.0), prompt="turn one"),
        ev.PromptComplete(**_env(1.0), response="one"),
        ev.PromptSubmit(**_env(2.0), prompt="ghost turn (forked away)"),
        ev.PromptComplete(**_env(3.0), response="two"),
    ]
    assert reducer.replay(two_turns, turn_base=1) is True
    assert reducer.ledger.checkpoints == ()
    assert reducer.turn_base == 1  # new checkpoints use the transcript base
    assert sum(1 for b in host.blocks if b.kind == "turn_rule") == 2
    reducer.handle(ev.PromptSubmit(**_env(10.0), prompt="next"))
    reducer.handle(ev.PromptComplete(**_env(11.0), response="ok"))
    assert [c.turn_id for c in reducer.ledger.checkpoints] == [2]


def test_replay_without_a_turn_reports_false_and_touches_nothing() -> None:
    """No prompt_submit in the log (foreign/absent events file) → the
    caller falls back to the prose restored_history path."""
    reducer, host = make_reducer()
    reducer.turn_base = 5
    reducer.session_cost = Decimal("2.50")
    events: list[ev.UIEvent] = [ev.Notification(**_env(0.0), message="stale")]
    assert reducer.replay(events, turn_base=9, session_cost=Decimal("9")) is False
    assert host.blocks == []
    assert reducer.turn_base == 5
    assert reducer.session_cost == Decimal("2.50")


def test_replay_rebuilds_delegate_summary_lane_transcript_and_plan() -> None:
    reducer, host = make_reducer()
    child = {"event_id": "c1", "session_id": "sub1", "parent_id": SID, "ts": 3.0}
    events: list[ev.UIEvent] = [
        ev.PromptSubmit(**_env(0.0), prompt="fan out"),
        ev.ToolPre(
            **_env(1.0),
            tool_name="todo",
            tool_call_id="t1",
            tool_input={"todos": [{"content": "step", "status": "completed"}]},
        ),
        ev.ToolPre(
            **_env(2.0),
            tool_name="delegate",
            tool_call_id="d1",
            tool_input={"agent": "researcher", "instruction": "dig in"},
        ),
        ev.AgentSpawned(
            **_env(2.5), agent="researcher", sub_session_id="sub1", parent_session_id=SID
        ),
        ev.ContentBlockEnd(
            **child, block_type="text", block={"type": "text", "text": "found it"}
        ),
        ev.AgentCompleted(
            **_env(4.0),
            agent="researcher",
            sub_session_id="sub1",
            parent_session_id=SID,
            success=True,
            result="1 finding",
        ),
        ev.PromptComplete(**_env(5.0), response="delegated work done"),
    ]
    assert reducer.replay(events, turn_base=1) is True

    summaries = [b for b in host.blocks if isinstance(b, DelegateSummaryBlock)]
    assert len(summaries) == 1
    (entry,) = summaries[0].entries
    assert (entry.agent, entry.state, entry.snippet) == ("researcher", "done", "1 finding")
    assert summaries[0].plan_final == (TodoItem(content="step", status="completed"),)

    lane_blocks = reducer.lane_transcript("sub1")
    assert lane_blocks is not None
    lane_texts = [
        "".join(s.text for s in b.spans) for b in lane_blocks if b.kind == "answer"
    ]
    assert any("found it" in text for text in lane_texts)
    assert host.plan_changes  # restored ambient plan state (spec §2/D3)
    assert all(record.lane.state == "done" for record in reducer.lanes.lanes)


def test_replay_settles_lanes_the_log_never_completed() -> None:
    """A crashed session's dangling lane must not tick wall-clock forever."""
    reducer, _host = make_reducer()
    events: list[ev.UIEvent] = [
        ev.PromptSubmit(**_env(0.0), prompt="fan out"),
        ev.AgentSpawned(
            **_env(1.0), agent="coder", sub_session_id="sub9", parent_session_id=SID
        ),
    ]
    assert reducer.replay(events, turn_base=1) is True
    assert all(record.lane.state == "done" for record in reducer.lanes.lanes)


def test_replay_reconciles_cost_to_the_kernel_baseline() -> None:
    """restore_session_cost stays the single cost authority on resume —
    replay's own accumulation is presentation-level and never adds on top."""
    reducer, _host = make_reducer()
    assert (
        reducer.replay(_one_turn_events(), turn_base=1, session_cost=Decimal("1.23"))
        is True
    )
    assert reducer.session_cost == Decimal("1.23")


def test_replay_stamps_historical_mode_on_the_user_line() -> None:
    """The stored prompt_submit carries the posture the turn ran under, so
    replay stamps that HISTORICAL mode badge \u2014 not the current live one."""
    reducer, host = make_reducer()  # ProbeHost live mode is 'chat'
    events: list[ev.UIEvent] = [
        ev.PromptSubmit(**_env(0.0), prompt="draft the plan", mode="plan"),
        ev.PromptComplete(**_env(1.0), response="planned"),
    ]
    assert reducer.replay(events, turn_base=1) is True
    user_line = next(b for b in host.blocks if b.kind == "user_line")
    assert user_line.mode == "plan"  # recorded posture, not the live 'chat'


def test_replay_falls_back_to_live_mode_on_legacy_logs() -> None:
    """Pre-stamp logs have no mode field; the badge falls back to the live
    posture rather than an empty/blank badge (backward compatible)."""
    reducer, host = make_reducer()
    host.mode_id = "auto"
    events: list[ev.UIEvent] = [
        ev.PromptSubmit(**_env(0.0), prompt="legacy turn"),  # mode == ""
        ev.PromptComplete(**_env(1.0), response="done"),
    ]
    assert reducer.replay(events, turn_base=1) is True
    user_line = next(b for b in host.blocks if b.kind == "user_line")
    assert user_line.mode == "auto"


def test_live_turn_prefers_event_mode_over_host_posture() -> None:
    """Live dispatch honours the event's stamped mode too, so the durable
    user line matches the posture at submit even if the app later flips."""
    reducer, host = make_reducer()
    host.mode_id = "auto"
    reducer.handle(ev.PromptSubmit(**_env(0.0), prompt="build it", mode="build"))
    user_line = next(b for b in host.blocks if b.kind == "user_line")
    assert user_line.mode == "build"
