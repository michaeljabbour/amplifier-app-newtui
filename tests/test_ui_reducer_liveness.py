"""Working-line liveness phases + delegate lane booting (joint enhancement).

Validated dead windows from a real session's ui-events.jsonl:

1. ``prompt_submit → execution_start`` (~15s of backend pre-turn hooks);
2. ``execution_start → first content_block`` (~11s of model prefill);
3. thinking blocks arriving as instant start/end pairs (content withheld);
4. ``agent_spawned → child session_start`` (~37s of bundle composition per
   delegate) — lanes read ``running · 0.0k tokens · $0.00`` (hung).

Both apps ship the same labels: ``starting turn`` / ``waiting on model`` /
``thinking`` on the working line, ``booting`` on freshly spawned lanes.
Mirrored 1:1 by the Rust suite (src/ui/reducer.rs liveness section).

Offline: fake events straight into the reducer, no Textual.
"""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import BlockIdAllocator, TodoItem, TranscriptBlock
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.model.turn import OutcomeLedger
from amplifier_app_newtui.ui.reducer import LaneSeed, TranscriptReducer

SID = "root-session"


class FakeHost:
    """Minimal ReducerHost: records blocks, ignores presentation."""

    def __init__(self, mode_id: str = "chat") -> None:
        self.mode_id = mode_id
        self.blocks: list[TranscriptBlock] = []
        self.notices: list[str] = []

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
        pass

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        pass

    def decision_deferred(self, message: str, decision_id: str = "") -> None:
        pass

    def stream_opened(self, block_type: str) -> None:
        pass

    def stream_delta(self, text: str) -> None:
        pass

    def stream_closed(self) -> None:
        pass


def make_reducer(mode_id: str = "chat", **kwargs) -> tuple[TranscriptReducer, FakeHost]:
    host = FakeHost(mode_id)
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
        **kwargs,
    )
    return reducer, host


def last_working(host: FakeHost):
    working = [b for b in host.blocks if b.kind == "working_status"]
    assert working, "working line mounted"
    return working[-1]


def spawn(reducer: TranscriptReducer, agent: str, sub: str, ts: float) -> None:
    reducer.handle(
        ev.AgentSpawned(
            session_id=SID,
            parent_session_id=SID,
            sub_session_id=sub,
            agent=agent,
            ts=ts,
        )
    )


def test_working_line_phase_transitions() -> None:
    """The validated dead windows get honest notes — submit→``starting
    turn``, execution_start→``waiting on model``, thinking block→
    ``thinking`` — and real tool activity always wins."""
    reducer, host = make_reducer("build")
    reducer.handle(ev.PromptSubmit(session_id=SID, prompt="fix the bug", ts=0.0))
    # Submitted, not executing: backend pre-turn hooks (~15s window).
    assert last_working(host).activity == "starting turn"
    # A child's execution_start must not advance the root phase.
    reducer.handle(ev.ExecutionStart(session_id="some-child", ts=0.5))
    assert last_working(host).activity == "starting turn"
    # Executing, no blocks yet: model prefill (~11s window).
    reducer.handle(ev.ExecutionStart(session_id=SID, ts=1.0))
    assert last_working(host).activity == "waiting on model"
    # Thinking blocks arrive as instant start/end pairs (content
    # withheld) — the note reflects thinking while no tool runs.
    reducer.handle(ev.ContentBlockStart(session_id=SID, block_type="thinking", ts=2.0))
    assert last_working(host).activity == "thinking"
    reducer.handle(
        ev.ContentBlockEnd(
            session_id=SID,
            block_type="thinking",
            block={"type": "thinking", "thinking": ""},
            ts=2.0,
        )
    )
    assert last_working(host).activity == "thinking"
    # Real tool activity wins over any phase note...
    reducer.handle(
        ev.ToolPre(
            session_id=SID,
            tool_call_id="t1",
            tool_name="bash",
            tool_input={"command": "cargo test"},
            ts=3.0,
        )
    )
    assert last_working(host).activity == "$ cargo test"
    # ...and a late execution_start never regresses the phase.
    reducer.handle(ev.ExecutionStart(session_id=SID, ts=3.5))
    reducer.handle(
        ev.ToolPost(
            session_id=SID,
            tool_call_id="t1",
            tool_name="bash",
            tool_input={"command": "cargo test"},
            result={"output": "ok"},
            ts=4.0,
        )
    )
    # Tool done → back to model time: the streaming-phase note.
    assert last_working(host).activity == "thinking"


def test_working_line_phase_note_skips_scripted_turns() -> None:
    """Demo turns keep their scripted presentation (lazy mount, empty
    note) — the phase machinery must not have minted a working line."""

    class Spec:
        duration_ms = 6000

    reducer, host = make_reducer(spec_lookup=lambda prompt: Spec())
    reducer.handle(ev.PromptSubmit(session_id=SID, prompt="scripted", ts=0.0))
    reducer.handle(ev.ExecutionStart(session_id=SID, ts=1.0))
    assert not any(b.kind == "working_status" for b in host.blocks)


def test_lane_booting_flips_to_running_on_first_child_event() -> None:
    """Spawn → child session_start is ~tens of seconds of bundle
    composition — the lane opens as ``booting`` and flips on the child's
    first event instead of showing zeroed telemetry."""
    reducer, _host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id=SID, prompt="fan out", ts=0.0))
    spawn(reducer, "researcher", "child-a", 1.0)
    lane = reducer.lanes.get("child-a")
    assert lane is not None
    assert lane.lane.state == "booting"
    assert lane.lane.activity == "booting"
    # The child's session_start is its first sign of life.
    reducer.handle(ev.SessionStart(session_id="child-a", parent_id=SID, ts=38.0))
    lane = reducer.lanes.get("child-a")
    assert lane is not None
    assert lane.lane.state == "running"
    assert lane.lane.activity == "running"
    # Idempotent: later child events keep the normal running flow.
    reducer.handle(ev.ExecutionStart(session_id="child-a", ts=39.0))
    lane = reducer.lanes.get("child-a")
    assert lane is not None
    assert lane.lane.state == "running"


def test_lane_booting_keeps_seeded_brief_activity() -> None:
    """A runtime-seeded delegate brief stays the activity line through the
    booting window and past the wake."""
    reducer, _host = make_reducer(
        lane_seed_lookup=lambda name: LaneSeed(activity="survey crates")
    )
    reducer.handle(ev.PromptSubmit(session_id=SID, prompt="fan out", ts=0.0))
    spawn(reducer, "researcher", "child-a", 1.0)
    lane = reducer.lanes.get("child-a")
    assert lane is not None
    assert lane.lane.state == "booting"
    assert lane.lane.activity == "survey crates"
    reducer.handle(ev.ExecutionStart(session_id="child-a", ts=30.0))
    lane = reducer.lanes.get("child-a")
    assert lane is not None
    assert lane.lane.state == "running"
    assert lane.lane.activity == "survey crates"  # brief kept


def test_lane_scripted_seed_never_boots() -> None:
    """Demo seeds carrying scripted telemetry keep their mockup-verbatim
    state."""
    reducer, _host = make_reducer(
        lane_seed_lookup=lambda name: LaneSeed(
            activity="scanning provider docs",
            elapsed=41.0,
            cost=Decimal("0.09"),
            tokens=100_100,
            state="running",
        )
    )
    reducer.handle(ev.PromptSubmit(session_id=SID, prompt="fan out", ts=0.0))
    spawn(reducer, "researcher", "child-a", 1.0)
    lane = reducer.lanes.get("child-a")
    assert lane is not None
    assert lane.lane.state == "running"
    assert lane.lane.activity == "scanning provider docs"
