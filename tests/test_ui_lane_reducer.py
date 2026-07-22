"""Direct unit tests for the extracted :class:`LaneReducer`.

The lane presentation state (per-lane live tail + focused-lane
transcripts) was carved out of ``TranscriptReducer`` along the lane seam
(issue #32). These tests drive the unit in isolation with a fake host —
proving lane-handling is covered directly on the extracted reducer, not
only through the turn reducer's dispatch.
"""

from __future__ import annotations

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import (
    Answer,
    BlockIdAllocator,
    Segment,
    SessionBanner,
    ToolLine,
    TranscriptBlock,
    UserLine,
)
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.ui.lane_reducer import (
    LANE_TAIL_NOTIFY_SECONDS,
    LaneReducer,
    _LANE_TRANSCRIPT_MAX_BLOCKS,
    _LANE_TRANSCRIPT_MAX_LANES,
)

ROOT = "root-session"
CHILD_A = "child-aaaaaaaaaaaaaaaa"
CHILD_B = "child-bbbbbbbbbbbbbbbb"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeHost:
    """Only the two lane-tail callbacks the LaneReducer actually drives."""

    def __init__(self) -> None:
        self.tail_updates: list[str] = []
        self.tail_cleared = 0

    def lane_tail_updated(self, text: str) -> None:
        self.tail_updates.append(text)

    def lane_tail_cleared(self) -> None:
        self.tail_cleared += 1


def make() -> tuple[LaneReducer, FakeHost, FakeClock, LaneRegistry]:
    host = FakeHost()
    clock = FakeClock()
    lanes = LaneRegistry()
    lane = LaneReducer(host, allocator=BlockIdAllocator(), lanes=lanes, tail_clock=clock)
    return lane, host, clock, lanes


def register(lanes: LaneRegistry, sub: str, name: str) -> None:
    lanes.register(sub, parent_id=ROOT, name=name, now=1.0)


def spawned(sub: str, name: str) -> ev.AgentSpawned:
    return ev.AgentSpawned(
        session_id=ROOT, ts=1.0, agent=name, sub_session_id=sub, parent_session_id=ROOT
    )


def delta(sub: str, text: str, *, block_type: str = "text") -> ev.StreamBlockDelta:
    return ev.StreamBlockDelta(
        session_id=sub,
        request_id=f"req-{sub}",
        block_index=0,
        block_type=block_type,
        sequence=0,
        text=text,
    )


def _texts(blocks: list[TranscriptBlock]) -> list[str]:
    return ["".join(s.text for s in b.spans) for b in blocks if isinstance(b, Answer)]


# -- focused-lane transcripts -------------------------------------------------


def test_seed_transcript_opens_banner_then_delegated_brief() -> None:
    lane, _host, _clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    lane.remember_brief("researcher", "find the flaky tests")
    lane.seed_transcript(spawned(CHILD_A, "researcher"))
    blocks = lane.transcript(CHILD_A)
    assert blocks is not None
    banner, brief = blocks
    assert isinstance(banner, SessionBanner)
    assert "focused: researcher" in banner.focus_note
    assert ROOT[:6] in banner.focus_note
    assert isinstance(brief, UserLine)
    assert brief.text == "find the flaky tests" and brief.mode == "delegated"


def test_seed_transcript_without_brief_is_banner_only() -> None:
    lane, _host, _clock, lanes = make()
    register(lanes, CHILD_A, "coder")
    lane.seed_transcript(spawned(CHILD_A, "coder"))
    blocks = lane.transcript(CHILD_A)
    assert blocks is not None and len(blocks) == 1
    assert isinstance(blocks[0], SessionBanner)


def test_append_block_seeds_a_banner_for_an_unknown_lane() -> None:
    lane, _host, _clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    record = lanes.get(CHILD_A)
    assert record is not None
    lane.append_block(record, Answer(id="x", spans=(Segment(text="hi"),), clickable=False))
    blocks = lane.transcript(CHILD_A)
    assert blocks is not None
    assert isinstance(blocks[0], SessionBanner)  # banner-only seed prepended
    assert _texts(blocks) == ["hi"]


def test_transcript_resolves_by_name_and_misses_cleanly() -> None:
    lane, _host, _clock, lanes = make()
    register(lanes, CHILD_A, "modular-builder")
    lane.seed_transcript(spawned(CHILD_A, "modular-builder"))
    assert lane.transcript("modular-builder") is not None
    assert lane.transcript(CHILD_A) is not None
    assert lane.transcript("nope") is None


def test_transcript_is_bounded_and_keeps_seed_rows() -> None:
    lane, _host, _clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    lane.remember_brief("researcher", "the brief")
    lane.seed_transcript(spawned(CHILD_A, "researcher"))
    record = lanes.get(CHILD_A)
    assert record is not None
    for n in range(_LANE_TRANSCRIPT_MAX_BLOCKS + 25):
        lane.append_block(
            record, Answer(id=f"a{n}", spans=(Segment(text=f"row {n}"),), clickable=False)
        )
    blocks = lane.transcript(CHILD_A)
    assert blocks is not None
    assert len(blocks) <= _LANE_TRANSCRIPT_MAX_BLOCKS
    assert isinstance(blocks[0], SessionBanner)
    assert isinstance(blocks[1], UserLine)  # seed rows survive the trim
    assert f"row {_LANE_TRANSCRIPT_MAX_BLOCKS + 24}" in _texts(blocks)[-1]


def test_stored_transcripts_are_capped_by_lane_count() -> None:
    lane, _host, _clock, lanes = make()
    for n in range(_LANE_TRANSCRIPT_MAX_LANES + 5):
        sub = f"lane-{n:016d}"
        lanes.register(sub, parent_id=ROOT, name=f"agent{n}", now=1.0)
        lane.seed_transcript(spawned(sub, f"agent{n}"))
    # The oldest lanes' transcripts were evicted; the newest survive.
    assert lane.transcript("lane-0000000000000000") is None
    newest = f"lane-{_LANE_TRANSCRIPT_MAX_LANES + 4:016d}"
    assert lane.transcript(newest) is not None


# -- live tail ----------------------------------------------------------------


def test_tail_delta_paints_the_accumulated_buffer() -> None:
    lane, host, clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    record = lanes.get(CHILD_A)
    assert record is not None
    lane.tail_delta(record, delta(CHILD_A, "reading the "))
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    lane.tail_delta(record, delta(CHILD_A, "queue bridge"))
    assert host.tail_updates == ["reading the ", "reading the queue bridge"]


def test_tail_delta_throttle_coalesces_without_losing_text() -> None:
    lane, host, clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    record = lanes.get(CHILD_A)
    assert record is not None
    lane.tail_delta(record, delta(CHILD_A, "one "))
    lane.tail_delta(record, delta(CHILD_A, "two "))  # same instant — throttled
    assert host.tail_updates == ["one "]
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    lane.tail_delta(record, delta(CHILD_A, "three"))
    assert host.tail_updates == ["one ", "one two three"]


def test_thinking_deltas_never_reach_the_tail() -> None:
    lane, host, _clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    record = lanes.get(CHILD_A)
    assert record is not None
    lane.tail_delta(record, delta(CHILD_A, "hmm", block_type="thinking"))
    assert host.tail_updates == []


def test_root_stream_preempts_the_tail() -> None:
    lane, host, clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    record = lanes.get(CHILD_A)
    assert record is not None
    lane.root_streaming = True
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    lane.tail_delta(record, delta(CHILD_A, "buffered but dark"))
    assert host.tail_updates == []  # never painted while the root streams
    lane.root_streaming = False
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    lane.tail_delta(record, delta(CHILD_A, ", resumes"))
    assert host.tail_updates[-1] == "buffered but dark, resumes"  # buffer never lost


def test_clear_tail_clears_a_shown_tail() -> None:
    lane, host, _clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    record = lanes.get(CHILD_A)
    assert record is not None
    lane.tail_delta(record, delta(CHILD_A, "child text"))
    assert host.tail_updates == ["child text"]
    lane.clear_tail(CHILD_A)
    assert host.tail_cleared == 1


def test_repaint_tail_paints_newly_pinned_buffer_and_clears_when_empty() -> None:
    lane, host, clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    register(lanes, CHILD_B, "coder")
    rec_a = lanes.get(CHILD_A)
    assert rec_a is not None
    lane.tail_delta(rec_a, delta(CHILD_A, "aaa"))
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    lanes.cycle_tail_focus()  # A (current) -> B, which never streamed
    lane.repaint_tail()
    assert host.tail_cleared == 1  # pinned lane has no buffer -> clears
    lanes.cycle_tail_focus()  # B -> A, which has "aaa" buffered
    lane.repaint_tail()
    assert host.tail_updates[-1] == "aaa"


def test_lane_activity_recap_row_appends_to_the_transcript() -> None:
    """A completion recap row (built by the turn reducer) still lands in the
    lane transcript via append_block — the extracted unit owns the list."""
    lane, _host, _clock, lanes = make()
    register(lanes, CHILD_A, "researcher")
    lane.seed_transcript(spawned(CHILD_A, "researcher"))
    record = lanes.get(CHILD_A)
    assert record is not None
    lane.append_block(
        record,
        ToolLine(id="t1", summary="read ci.log", status="completed", tool_call_ids=("t1",)),
    )
    lane.append_block(
        record,
        Answer(
            id="r1",
            spans=(
                Segment(text="\u2733 ", style_token="dimmer"),
                Segment(text="completed \u00b7 result reported back to parent", style_token="dim"),
            ),
            clickable=False,
        ),
    )
    blocks = lane.transcript(CHILD_A)
    assert blocks is not None
    tools = [b for b in blocks if isinstance(b, ToolLine)]
    assert tools and tools[0].status == "completed"
    assert "completed \u00b7 result reported back to parent" in _texts(blocks)[-1]
