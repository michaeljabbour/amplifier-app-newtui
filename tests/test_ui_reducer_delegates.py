"""Delegate fan-out → ONE DelegateSummaryBlock per turn, replaced in place (D5)."""

from __future__ import annotations

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


class FakeHost:
    """Minimal ReducerHost: records blocks, ignores presentation."""

    def __init__(self, mode_id: str = "chat") -> None:
        self.mode_id = mode_id
        self.blocks: list[TranscriptBlock] = []
        self.notices: list[str] = []
        self.stream_events: list[tuple[str, str]] = []
        self.plan_changes: list[tuple[TodoItem, ...]] = []

    def append_block(self, block: TranscriptBlock) -> None:
        self.blocks.append(block)

    def replace_block(self, block: TranscriptBlock) -> None:
        for i, existing in enumerate(self.blocks):
            if existing.id == block.id:
                self.blocks[i] = block
                return

    def remove_block(self, block_id: str) -> None:
        self.blocks = [b for b in self.blocks if b.id != block_id]

    def show_notice(self, text: str) -> None:
        self.notices.append(text)

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        pass

    def turn_started(self) -> None:
        pass

    def turn_finished(self) -> None:
        pass

    def lanes_changed(self) -> None:
        pass

    def plan_changed(self, items: tuple[TodoItem, ...]) -> None:
        self.plan_changes.append(items)

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        pass

    def decision_deferred(self, message: str) -> None:
        pass

    def stream_opened(self, block_type: str) -> None:
        self.stream_events.append(("opened", block_type))

    def stream_delta(self, text: str) -> None:
        self.stream_events.append(("delta", text))

    def stream_closed(self) -> None:
        self.stream_events.append(("closed", ""))


def make_reducer(mode_id: str = "chat") -> tuple[TranscriptReducer, FakeHost]:
    host = FakeHost(mode_id)
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    return reducer, host


SID = "root-session"


def _env(ts: float, n: int = 0) -> dict:
    return {"event_id": f"e{ts}-{n}", "session_id": SID, "parent_id": None, "ts": ts}


def _start(reducer) -> None:
    reducer.handle(ev.PromptSubmit(**_env(0.0), prompt="fan out"))


def _spawn(reducer, agent: str, sub: str, ts: float) -> None:
    reducer.handle(
        ev.AgentSpawned(**_env(ts), agent=agent, sub_session_id=sub, parent_session_id=SID)
    )


def _complete(reducer, agent: str, sub: str, ts: float, *, success=True, result="") -> None:
    reducer.handle(
        ev.AgentCompleted(
            **_env(ts),
            agent=agent,
            sub_session_id=sub,
            parent_session_id=SID,
            success=success,
            result=result,
        )
    )


def _summaries(host) -> list[DelegateSummaryBlock]:
    return [b for b in host.blocks if isinstance(b, DelegateSummaryBlock)]


def test_fanout_appends_exactly_one_summary_block() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "researcher", "s1", 1.0)
    _spawn(reducer, "coder", "s2", 1.0)
    _spawn(reducer, "tester", "s3", 1.0)
    blocks = _summaries(host)
    assert len(blocks) == 1
    block = blocks[0]
    assert [e.agent for e in block.entries] == ["researcher", "coder", "tester"]
    assert all(e.state == "running" for e in block.entries)
    assert block.expanded is False


def test_no_tree_line_answer_blocks_anymore() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "researcher", "s1", 1.0)
    _complete(reducer, "researcher", "s1", 3.0, result="3 findings")
    assert not [
        b
        for b in host.blocks
        if b.kind == "answer" and "researcher" in "".join(s.text for s in b.spans)
    ]


def test_completion_updates_in_place_with_elapsed_and_snippet() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "researcher", "s1", 1.0)
    _spawn(reducer, "coder", "s2", 1.0)
    _complete(reducer, "researcher", "s1", 5.4, result="3 findings")
    block = _summaries(host)[0]
    done = block.entries[0]
    assert (done.state, done.snippet) == ("done", "3 findings")
    assert done.elapsed_s == 4.4
    assert block.entries[1].state == "running"
    assert len(_summaries(host)) == 1  # replaced, never re-appended


def test_all_complete_finalizes_duration_and_failure_state() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "coder", "s1", 1.0)
    _spawn(reducer, "tester", "s2", 1.0)
    _complete(reducer, "tester", "s2", 3.6, result="tests ✔")
    _complete(reducer, "coder", "s1", 7.0, success=False)
    block = _summaries(host)[0]
    assert block.entries[0].state == "error"
    assert block.entries[0].snippet == "failed"
    assert block.duration_s == 6.0  # last completion − first spawn


def test_plan_final_captured_from_turn_todos() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    reducer.handle(
        ev.ToolPre(
            **_env(0.5),
            tool_name="todo",
            tool_call_id="t1",
            tool_input={
                "todos": [
                    {"content": "scan docs", "status": "completed"},
                    {"content": "synthesize", "status": "in_progress"},
                ]
            },
        )
    )
    _spawn(reducer, "researcher", "s1", 1.0)
    _complete(reducer, "researcher", "s1", 2.0, result="ok")
    block = _summaries(host)[0]
    assert block.plan_final is not None
    assert [i.content for i in block.plan_final] == ["scan docs", "synthesize"]


def test_todo_beat_after_last_completion_folds_into_plan_final() -> None:
    """The runtime closes the plan AFTER the last AgentCompleted (demo:
    ``…agent_completed + TODO``) — the durable summary must fold that
    final todo state in, so its header ends ``Plan 4/4``, not one beat
    behind (design D3 plan-fold)."""
    reducer, host = make_reducer()
    _start(reducer)
    reducer.handle(
        ev.ToolPre(
            **_env(0.5),
            tool_name="todo",
            tool_call_id="t1",
            tool_input={"todos": [{"content": "scan docs", "status": "in_progress"}]},
        )
    )
    _spawn(reducer, "coder", "s1", 1.0)
    _complete(reducer, "coder", "s1", 2.0, result="ok")
    reducer.handle(
        ev.ToolPre(
            **_env(2.1),
            tool_name="todo",
            tool_call_id="t2",
            tool_input={"todos": [{"content": "scan docs", "status": "completed"}]},
        )
    )
    block = _summaries(host)[0]
    assert block.plan_final is not None
    assert [i.status for i in block.plan_final] == ["completed"]
    assert len(_summaries(host)) == 1  # replaced in place, never re-appended


def test_no_todos_means_plan_final_none() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "coder", "s1", 1.0)
    _complete(reducer, "coder", "s1", 2.0, result="ok")
    assert _summaries(host)[0].plan_final is None


def test_cancelled_turn_marks_running_entries_cancelled() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "coder", "s1", 1.0)
    reducer.handle(ev.CancelCompleted(**_env(4.0)))
    reducer.handle(ev.PromptComplete(**_env(5.0)))
    block = _summaries(host)[0]
    assert block.entries[0].state == "cancelled"


def test_second_turn_gets_a_fresh_summary_block() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "coder", "s1", 1.0)
    _complete(reducer, "coder", "s1", 2.0, result="ok")
    reducer.handle(ev.PromptComplete(**_env(3.0)))
    reducer.handle(ev.PromptSubmit(**_env(10.0), prompt="again"))
    _spawn(reducer, "tester", "s9", 11.0)
    assert len(_summaries(host)) == 2


# -- heartbeat vs scripted lanes (found live in forge, 2026-07-21) --------------


def test_demo_turn_heartbeat_keeps_virtual_lane_clocks() -> None:
    """Scripted lanes are stamped with the demo's virtual clock (~seconds);
    the app heartbeat passes wall time. Advancing them with wall time paints
    epoch-scale elapsed (``29744551m 45s``) in the lanes panel."""

    class Spec:
        duration_ms = 6000

    host = FakeHost()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
        spec_lookup=lambda prompt: Spec(),
    )
    reducer.handle(ev.PromptSubmit(**_env(0.0), prompt="fan out"))
    _spawn(reducer, "researcher", "s1", 1.0)
    # Precondition: the working pulse is mounted, so tick() reaches the lanes.
    assert any(b.kind == "working_status" for b in host.blocks)
    reducer.tick(1_753_000_000.0)  # wall clock, ~55 years after ts=1.0
    lane = reducer.lanes.active[0].lane
    assert lane.elapsed < 60.0  # virtual-clock telemetry kept, not clobbered


def test_real_turn_heartbeat_advances_lane_clocks() -> None:
    """Spec-less (real) turns DO tick per-lane clocks on the heartbeat —
    both spawn ts and tick now are wall clock there."""
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(**_env(100.0), prompt="fan out"))
    _spawn(reducer, "researcher", "s1", 100.0)
    reducer.tick(103.0)
    lane = reducer.lanes.active[0].lane
    assert lane.elapsed == 3.0


def test_fanout_at_virtual_clock_zero_keeps_duration_and_elapsed() -> None:
    """The demo's virtual clock legitimately starts at ts=0.0; a falsy-ts
    fallback to wall time mixes clock domains and clamps the fan-out
    duration to 0 (found live in forge: ``· 0s ▸`` after ``seed → agents``,
    where the waitless seed turn leaves the clock at zero)."""
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "researcher", "s1", 0.0)
    _spawn(reducer, "coder", "s2", 0.0)
    _complete(reducer, "researcher", "s1", 2.6, result="3 findings")
    _complete(reducer, "coder", "s2", 6.0, result="2 files")
    block = _summaries(host)[0]
    assert block.duration_s == 6.0
    assert block.entries[0].elapsed_s == 2.6
    assert block.entries[1].elapsed_s == 6.0
