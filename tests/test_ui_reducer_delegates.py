"""Delegate fan-out → ONE DelegateSummaryBlock per turn, replaced in place (D5)."""

from __future__ import annotations

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import (
    BlockIdAllocator,
    DelegateSummaryBlock,
    TodoItem,
    TranscriptBlock,
)
from amplifier_app_tui.model.lanes import LaneRegistry
from amplifier_app_tui.model.turn import OutcomeLedger
from amplifier_app_tui.ui.reducer import TranscriptReducer


class FakeHost:
    """Minimal ReducerHost: records blocks, ignores presentation."""

    def __init__(self, mode_id: str = "chat") -> None:
        self.mode_id = mode_id
        self.blocks: list[TranscriptBlock] = []
        self.notices: list[str] = []
        self.stream_events: list[tuple[str, str]] = []
        self.plan_changes: list[tuple[TodoItem, ...]] = []
        self.attention_errors: list[tuple[str, str]] = []

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

    def decision_deferred(self, message: str, decision_id: str = "") -> None:
        pass

    def attention_error(self, detail: str, *, occasion: str) -> None:
        self.attention_errors.append((detail, occasion))

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
    """The old per-agent tree lines stay gone; the only agent-named answer
    lines are the compact ✳ lifecycle markers (started / done / failed)."""
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "researcher", "s1", 1.0)
    _complete(reducer, "researcher", "s1", 3.0, result="3 findings")
    agent_lines = [
        "".join(s.text for s in b.spans)
        for b in host.blocks
        if b.kind == "answer" and "researcher" in "".join(s.text for s in b.spans)
    ]
    assert agent_lines == ["✳ researcher started", "✳ researcher done · 3 findings"]


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


# -- chat lifecycle markers (agent-lane chat dedup) ------------------------------
# Child thinking/prose stream to lanes only (the foreign-turn divert); the
# chat carries one compact dim ✳ marker per delegate lifecycle beat instead
# of mirroring lane content.


def _markers(host) -> list[str]:
    return [
        "".join(s.text for s in b.spans)
        for b in host.blocks
        if b.kind == "answer" and "".join(s.text for s in b.spans).startswith("✳ ")
    ]


def test_spawn_marker_names_the_agent_and_its_brief() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    reducer.handle(
        ev.ToolPre(
            **_env(0.5),
            tool_name="delegate",
            tool_call_id="d1",
            tool_input={"agent": "researcher", "instruction": "scan provider docs"},
        )
    )
    _spawn(reducer, "researcher", "s1", 1.0)
    assert _markers(host) == ["✳ researcher started · scan provider docs"]


def test_completion_markers_carry_result_hint_and_failure_reason() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "researcher", "s1", 1.0)
    _spawn(reducer, "coder", "s2", 1.0)
    _spawn(reducer, "tester", "s3", 1.0)
    _complete(reducer, "researcher", "s1", 2.0, result="## Findings\n3 flaky tests")
    _complete(reducer, "coder", "s2", 3.0, success=False, result="migration blew up")
    _complete(reducer, "tester", "s3", 4.0, success=False)  # reasonless failure
    assert _markers(host) == [
        "✳ researcher started",
        "✳ coder started",
        "✳ tester started",
        "✳ researcher done · Findings",  # markdown distilled, not pasted raw
        "✳ coder failed · migration blew up",
        "✳ tester failed",  # never "failed · failed"
    ]


def test_child_thinking_and_prose_never_create_chat_blocks() -> None:
    """The routing pin: child thinking (both channels) and child prose stay
    out of the chat; the root's own Thinking block renders untouched."""
    reducer, host = make_reducer()
    _start(reducer)
    reducer.handle(ev.ContentBlockStart(**_env(0.2), block_type="thinking"))
    reducer.handle(
        ev.ContentBlockEnd(**_env(0.3), block_type="thinking", block={"thinking": "root plan"})
    )
    _spawn(reducer, "researcher", "s1", 1.0)
    child = {"event_id": "c1", "session_id": "s1", "parent_id": SID, "ts": 2.0}
    reducer.handle(ev.StreamBlockDelta(**child, block_type="thinking", text="child secret"))
    reducer.handle(ev.ContentBlockStart(**child, block_type="thinking"))
    reducer.handle(
        ev.ContentBlockEnd(**child, block_type="thinking", block={"thinking": "child secret"})
    )
    reducer.handle(ev.ContentBlockEnd(**child, block_type="text", block={"text": "child prose"}))
    thinking = [b.text for b in host.blocks if b.kind == "thinking"]
    assert thinking == ["root plan"]
    chat_text = " ".join(
        "".join(s.text for s in b.spans) for b in host.blocks if b.kind == "answer"
    )
    assert "child secret" not in chat_text and "child prose" not in chat_text


def test_straggler_completion_after_turn_end_adds_no_marker() -> None:
    """Post-close-out completions still update the durable summary in place,
    but never append a marker below the turn rule."""
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "coder", "s1", 1.0)
    reducer.handle(ev.PromptComplete(**_env(3.0)))
    before = _markers(host)
    _complete(reducer, "coder", "s1", 4.0, result="late result")
    assert _markers(host) == before
    assert _summaries(host)[0].entries[0].state == "done"  # summary still settles


# -- D5 AC1: lane-level attention state (reconciled with _delegate_rows) -----
# The delegate-summary tests above cover `_delegate_rows`/`DelegateSummaryBlock`
# state (running/done/error/cancelled). These cover the SAME real events'
# effect on the per-lane `LaneRegistry` state (booting/running/working/
# attention/done/error/cancelled) \u2014 proving the two surfaces are driven by
# the identical signal rather than two independently-maintained notions.


def _child_env2(sub: str, ts: float, n: int = 0) -> dict:
    return {"event_id": f"ac{ts}-{n}", "session_id": sub, "parent_id": SID, "ts": ts}


def test_agent_completed_success_sets_lane_state_done() -> None:
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "researcher", "s1", 1.0)
    _complete(reducer, "researcher", "s1", 2.0, result="3 findings")
    lane = reducer.lanes.get("s1")
    assert lane is not None
    assert lane.lane.state == "done"
    assert lane.lane.activity == "done \u00b7 3 findings"


def test_agent_completed_failure_sets_lane_state_error_not_done() -> None:
    """The gap the reviewer flagged: a failure used to fold into ``done``
    with the distinction living only in free text. Now the STATE itself
    (glyph/color) differs, matching the delegate row's own ``error``."""
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "coder", "s1", 1.0)
    _complete(reducer, "coder", "s1", 3.0, success=False, result="migration blew up")
    lane = reducer.lanes.get("s1")
    assert lane is not None
    assert lane.lane.state == "error"
    assert lane.lane.activity == "failed \u00b7 migration blew up"
    # Reconciliation: the lane and the delegate-summary row agree.
    assert _summaries(host)[0].entries[0].state == "error"


def test_agent_completed_reasonless_failure_lane_activity_is_bare_failed() -> None:
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "tester", "s1", 1.0)
    _complete(reducer, "tester", "s1", 2.0, success=False)
    assert reducer.lanes.get("s1").lane.activity == "failed"  # never "failed \u00b7 failed"


def test_tool_error_on_child_lane_enters_attention_state() -> None:
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "debugger", "s1", 1.0)
    reducer.handle(
        ev.ToolError(
            **_child_env2("s1", 2.0),
            tool_name="read_file",
            tool_call_id="t1",
            error_message="no such file",
        )
    )
    lane = reducer.lanes.get("s1")
    assert lane is not None
    assert lane.lane.state == "attention"
    assert lane.lane.activity == "recovering from read file error"


def test_failed_tool_post_on_child_lane_enters_attention_state() -> None:
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "debugger", "s1", 1.0)
    reducer.handle(
        ev.ToolPost(
            **_child_env2("s1", 2.0),
            tool_name="bash",
            tool_call_id="t1",
            tool_input={"command": "pytest"},
            result={"success": False},
        )
    )
    assert reducer.lanes.get("s1").lane.state == "attention"


def test_successful_tool_post_does_not_enter_attention() -> None:
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "debugger", "s1", 1.0)
    reducer.handle(
        ev.ToolPost(
            **_child_env2("s1", 2.0),
            tool_name="read_file",
            tool_call_id="t1",
            tool_input={"path": "ci.log"},
            result={"success": True},
        )
    )
    assert reducer.lanes.get("s1").lane.state == "running"


def test_fresh_tool_attempt_clears_a_prior_attention_state() -> None:
    """A new ToolPre is itself evidence of recovery \u2014 it clears attention
    back to ``working``, the same as any other fresh tool attempt."""
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "debugger", "s1", 1.0)
    reducer.handle(
        ev.ToolError(
            **_child_env2("s1", 2.0), tool_name="bash", tool_call_id="t1", error_message="boom"
        )
    )
    assert reducer.lanes.get("s1").lane.state == "attention"
    reducer.handle(
        ev.ToolPre(
            **_child_env2("s1", 3.0),
            tool_name="bash",
            tool_call_id="t2",
            tool_input={"command": "pytest --lf"},
        )
    )
    assert reducer.lanes.get("s1").lane.state == "working"


def test_attention_survives_ordinary_narration_until_next_tool_attempt() -> None:
    """A stream/content beat right after a failure must NOT silently clear
    attention \u2014 otherwise the signal would vanish before anyone could see
    it (D5 AC1: the state must be real, not a flicker)."""
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "debugger", "s1", 1.0)
    reducer.handle(
        ev.ToolError(
            **_child_env2("s1", 2.0), tool_name="bash", tool_call_id="t1", error_message="boom"
        )
    )
    reducer.handle(
        ev.StreamBlockEnd(
            **_child_env2("s1", 2.2), request_id="r1", block_index=0, block_type="text"
        )
    )
    assert reducer.lanes.get("s1").lane.state == "attention"
    reducer.handle(
        ev.ContentBlockEnd(
            **_child_env2("s1", 2.3),
            block_type="text",
            block={"text": "let me try a different approach"},
        )
    )
    assert reducer.lanes.get("s1").lane.state == "attention"


def test_attention_lane_still_counted_active_and_ticks_elapsed() -> None:
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "debugger", "s1", 1.0)
    reducer.handle(
        ev.ToolError(
            **_child_env2("s1", 2.0), tool_name="bash", tool_call_id="t1", error_message="boom"
        )
    )
    assert reducer.lanes.active_count == 1
    reducer.tick(10.0)
    assert reducer.lanes.get("s1").lane.elapsed > 0  # not frozen like a terminal state


def test_cancelled_turn_settles_lane_state_matching_delegate_row() -> None:
    """Reconciliation (D5 AC1): the SAME turn.cancelled signal that marks
    the delegate-summary row \"cancelled\" must also settle the lane itself
    \u2014 previously the lane stayed \"running\" forever after a cancelled turn."""
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "coder", "s1", 1.0)
    reducer.handle(ev.CancelCompleted(**_env(4.0)))
    reducer.handle(ev.PromptComplete(**_env(5.0)))
    lane = reducer.lanes.get("s1")
    assert lane is not None
    assert lane.lane.state == "cancelled"
    assert lane.lane.activity == "cancelled"
    assert _summaries(host)[0].entries[0].state == "cancelled"  # both surfaces agree
    assert reducer.lanes.active_count == 0  # cancelled is terminal


def test_cancelled_turn_leaves_an_already_done_lane_alone() -> None:
    """A lane that genuinely finished before the cancellation must not be
    clobbered back to \"cancelled\" \u2014 only STILL-RUNNING rows settle."""
    reducer, _host = make_reducer()
    _start(reducer)
    _spawn(reducer, "coder", "s1", 1.0)
    _spawn(reducer, "tester", "s2", 1.0)
    _complete(reducer, "coder", "s1", 2.0, result="ok")
    reducer.handle(ev.CancelCompleted(**_env(4.0)))
    reducer.handle(ev.PromptComplete(**_env(5.0)))
    assert reducer.lanes.get("s1").lane.state == "done"  # untouched
    assert reducer.lanes.get("s2").lane.state == "cancelled"  # settled


def test_cancelled_lane_settlement_is_not_coalesced_away() -> None:
    """The new cancellation call site must preserve the D5 AC5 guarantee:
    a lane's terminal transition always lands as its own repaint, however
    many progress frames immediately preceded it in the same instant."""
    from amplifier_app_tui.model.lanes import LaneRegistry
    from amplifier_app_tui.model.blocks import BlockIdAllocator
    from amplifier_app_tui.model.turn import OutcomeLedger
    from amplifier_app_tui.ui.reducer import TranscriptReducer

    from .test_ui_lanes_telemetry import CountingHost

    host = CountingHost("chat")
    reducer = TranscriptReducer(
        host, allocator=BlockIdAllocator(), ledger=OutcomeLedger(), lanes=LaneRegistry()
    )
    reducer.handle(ev.PromptSubmit(**_env(0.0), prompt="fan out"))
    _spawn(reducer, "coder", "s1", 1.0)
    # Flood the coalescible progress path immediately beforehand.
    for n in range(200):
        reducer.handle(
            ev.ToolPre(
                **_child_env2("s1", 1.0, n),
                tool_name="bash",
                tool_call_id=f"t{n}",
                tool_input={"command": "echo hi"},
            )
        )
    before = host.lanes_changed_calls
    reducer.handle(ev.CancelCompleted(**_env(1.0)))
    reducer.handle(ev.PromptComplete(**_env(1.0)))
    assert host.lanes_changed_calls > before  # the cancellation ALWAYS repaints


# -- B7 gap 3: production error transition #3 -- a failed delegate -----------


def test_agent_completed_failure_notifies_attention_error() -> None:
    """A delegate settling into the terminal ``error`` state (D5 AC1) must
    notify attention_error exactly once, keyed by its own sub_session_id."""
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "scout", "s1", 1.0)
    _complete(reducer, "scout", "s1", 2.0, success=False, result="tests failed")

    assert host.attention_errors == [("tests failed", "s1")]


def test_agent_completed_success_does_not_notify_attention_error() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "scout", "s1", 1.0)
    _complete(reducer, "scout", "s1", 2.0, success=True, result="done")

    assert host.attention_errors == []


def test_agent_completed_reasonless_failure_still_notifies_with_agent_name() -> None:
    """A failure with no ``result`` text (bare "failed") still gets a
    meaningful detail -- the agent name, not the literal word "failed"."""
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "scout", "s1", 1.0)
    _complete(reducer, "scout", "s1", 2.0, success=False, result="")

    assert host.attention_errors == [("scout failed", "s1")]


def test_cancelled_delegate_does_not_notify_attention_error() -> None:
    """Cancellation (a turn-level interrupt cascading onto still-running
    delegates) is a deliberate user action, never an "error" -- B7 gap 3
    keys off the SAME terminal-state signal TERMINAL_LANE_STATES defines,
    but narrower than it: "cancelled" must not ring the error bell."""
    reducer, host = make_reducer()
    _start(reducer)
    _spawn(reducer, "scout", "s1", 1.0)
    reducer.handle(ev.CancelCompleted(**_env(2.0)))
    reducer.handle(ev.PromptComplete(**_env(2.0)))

    assert reducer.lanes.get("s1").lane.state == "cancelled"
    assert host.attention_errors == []


# -- B7 gap 3: production error transition #2 -- a provider/runtime error ---


def test_provider_error_notice_notifies_attention_error() -> None:
    reducer, host = make_reducer()
    _start(reducer)
    reducer.handle(ev.ProviderNotice(**_env(1.0), notice="error", message="rate limited hard"))

    assert host.notices == ["provider error · rate limited hard"]
    assert len(host.attention_errors) == 1
    detail, occasion = host.attention_errors[0]
    assert detail == "rate limited hard"
    assert occasion.startswith("provider-error-")


def test_provider_retry_and_throttle_notices_do_not_notify_attention_error() -> None:
    """Only "error" is attention-worthy -- retry/throttle are transient noise."""
    reducer, host = make_reducer()
    _start(reducer)
    reducer.handle(ev.ProviderNotice(**_env(1.0), notice="retry", message="retrying"))
    reducer.handle(ev.ProviderNotice(**_env(2.0), notice="throttle", message="slow down"))

    assert host.attention_errors == []
