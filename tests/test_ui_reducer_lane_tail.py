"""Lane live tail: focused-lane child deltas → ReducerHost (design doc D4).

Offline unit tests with a fake host + fake clock: buffering, focus
selection, ctrl-o pinning, the 0.05s repaint throttle, root-stream
preemption, and ephemerality (cleared on lane completion / turn end;
never a transcript block).
"""

from __future__ import annotations

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import BlockIdAllocator, TranscriptBlock
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.model.turn import OutcomeLedger
from amplifier_app_newtui.ui.reducer import LANE_TAIL_NOTIFY_SECONDS, TranscriptReducer

ROOT = "root-session"
CHILD_A = "child-aaaaaaaaaaaaaaaa"
CHILD_B = "child-bbbbbbbbbbbbbbbb"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeHost:
    """Minimal ReducerHost recording lane-tail traffic + appended blocks."""

    mode_id = "auto"

    def __init__(self) -> None:
        self.blocks: list[TranscriptBlock] = []
        self.tail_updates: list[str] = []
        self.tail_cleared = 0

    def append_block(self, block: TranscriptBlock) -> None:
        self.blocks.append(block)

    def replace_block(self, block: TranscriptBlock) -> None: ...
    def remove_block(self, block_id: str) -> None: ...
    def show_notice(self, text: str) -> None: ...
    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None: ...
    def turn_started(self) -> None: ...
    def turn_finished(self) -> None: ...
    def lanes_changed(self) -> None: ...
    def plan_changed(self) -> None: ...  # Phase 1
    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None: ...
    def decision_deferred(self, message: str, decision_id: str = "") -> None: ...
    def stream_opened(self, block_type: str) -> None: ...
    def stream_delta(self, text: str) -> None: ...
    def stream_closed(self) -> None: ...

    def lane_tail_updated(self, text: str) -> None:
        self.tail_updates.append(text)

    def lane_tail_cleared(self) -> None:
        self.tail_cleared += 1


def make() -> tuple[TranscriptReducer, FakeHost, FakeClock]:
    host = FakeHost()
    clock = FakeClock()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
        tail_clock=clock,
    )
    reducer.handle(ev.PromptSubmit(prompt="fan out", ts=1.0, session_id=ROOT))
    return reducer, host, clock


def spawn(reducer: TranscriptReducer, sub: str, name: str) -> None:
    reducer.handle(
        ev.AgentSpawned(
            session_id=ROOT,
            ts=1.0,
            agent=name,
            sub_session_id=sub,
            parent_session_id=ROOT,
        )
    )


def delta(reducer: TranscriptReducer, sub: str, text: str, *, block_type: str = "text") -> None:
    reducer.handle(
        ev.StreamBlockDelta(
            session_id=sub,
            request_id=f"req-{sub}",
            block_index=0,
            block_type=block_type,
            sequence=0,
            text=text,
        )
    )


def test_child_text_delta_paints_the_accumulated_buffer() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "reading the ")
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, "queue bridge")
    assert host.tail_updates == ["reading the ", "reading the queue bridge"]


def test_thinking_deltas_never_reach_the_tail() -> None:
    reducer, host, _ = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "hmm", block_type="thinking")
    assert host.tail_updates == []


def test_deltas_within_the_notify_window_coalesce_without_losing_text() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "one ")
    delta(reducer, CHILD_A, "two ")  # same clock instant — paint throttled
    assert host.tail_updates == ["one "]
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, "three")
    assert host.tail_updates == ["one ", "one two three"]  # nothing lost


def test_focus_follows_the_most_recently_streaming_lane() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    spawn(reducer, CHILD_B, "coder")
    delta(reducer, CHILD_A, "aaa")
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_B, "bbb")
    assert host.tail_updates == ["aaa", "bbb"]
    tailed = reducer.lanes.tail_lane
    assert tailed is not None and tailed.session_id == CHILD_B


def test_explicit_cycle_pin_wins_over_recent_activity() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    spawn(reducer, CHILD_B, "coder")
    delta(reducer, CHILD_A, "aaa")
    pinned = reducer.lanes.cycle_tail_focus()  # A (current) → B
    assert pinned is not None and pinned.session_id == CHILD_B
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, "more-a")  # not focused: buffered, not painted
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_B, "bbb")
    assert host.tail_updates == ["aaa", "bbb"]


def test_root_stream_preempts_clears_and_suppresses_the_tail() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "child text")
    assert host.tail_updates == ["child text"]
    reducer.handle(
        ev.StreamBlockStart(
            session_id=ROOT, request_id="req-root", block_index=0, block_type="text"
        )
    )
    assert host.tail_cleared == 1  # cleared the instant the root speaks
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, " while root streams")  # buffered, never painted
    assert host.tail_updates == ["child text"]
    reducer.handle(
        ev.StreamBlockEnd(session_id=ROOT, request_id="req-root", block_index=0, block_type="text")
    )
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, ", resumes")
    # Preemption DISCARDED the old buffer (ephemeral, D4) — the tail
    # restarts from whatever streamed after the root went idle again.
    assert host.tail_updates[-1] == " while root streams, resumes"


def test_lane_completion_clears_a_shown_tail() -> None:
    reducer, host, _ = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "child text")
    reducer.handle(
        ev.AgentCompleted(
            session_id=ROOT,
            agent="researcher",
            sub_session_id=CHILD_A,
            parent_session_id=ROOT,
            success=True,
            result="3 findings",
        )
    )
    assert host.tail_cleared == 1


def test_turn_end_discards_all_tail_state_and_leaves_no_block_behind() -> None:
    reducer, host, _ = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "ephemeral child prose")
    reducer.handle(ev.PromptComplete(ts=2.0, session_id=ROOT))
    assert host.tail_cleared == 1
    # Ephemeral: the tail text never became transcript content.
    assert not any(
        "ephemeral child prose" in getattr(span, "text", "")
        for block in host.blocks
        for span in getattr(block, "spans", ())
    )


def test_repaint_lane_tail_paints_the_newly_pinned_buffer() -> None:
    """ctrl+o repaints immediately — without this the tail keeps showing
    the previous lane's text until the pinned lane's next delta (found
    live in forge: demo bursts are one-shot, so it never updated)."""
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    spawn(reducer, CHILD_B, "coder")
    delta(reducer, CHILD_A, "aaa")
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_B, "bbb")  # focus follows B, painted
    reducer.lanes.cycle_tail_focus()  # pin cycles B → A
    reducer.repaint_lane_tail()
    assert host.tail_updates[-1] == "aaa"


def test_repaint_lane_tail_clears_when_pinned_lane_has_no_buffer() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    spawn(reducer, CHILD_B, "coder")
    delta(reducer, CHILD_A, "aaa")
    reducer.lanes.cycle_tail_focus()  # A (current) → B, which never streamed
    reducer.repaint_lane_tail()
    assert host.tail_cleared == 1
