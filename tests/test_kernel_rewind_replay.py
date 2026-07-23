"""Post-rewind ghost turns on resume (issue #40).

The ui-events log is append-only, so a confirmed rewind leaves the turns
it discarded in the file. Without honoring rewind boundaries a resume
replays those turns as ghost turns. The app writes a
:class:`~amplifier_app_newtui.kernel.events.RewindMarker` at fork time and
:func:`~amplifier_app_newtui.kernel.events.drop_rewound_events` filters
them at read time (``restored_ui_events``) — the read-side half of the
append-only contract.

These tests pin: the pure filter (single / repeated / nested rewinds), the
end-to-end resume path (restored transcript matches what was on screen, and
the surviving checkpoints stay so rewind still works after resume), the
cost re-seed staying consistent with the marker in the log, and
``RealRuntime.fork`` stamping the marker.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.kernel.cost import CostTracker, restore_session_cost
from amplifier_app_newtui.kernel.persistence import SessionStore
from amplifier_app_newtui.kernel.rewind import RewindError
from amplifier_app_newtui.kernel.runtime import (
    _kept_turns_for,
    restored_ui_events,
)
from amplifier_app_newtui.model.blocks import BlockIdAllocator
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.model.turn import OutcomeLedger, TurnOutcome, TurnTelemetry
from amplifier_app_newtui.ui.reducer import TranscriptReducer

from .test_ui_reducer_delegates import SID, FakeHost, _env

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _turn(prompt: str, *, ts: float, usage_tokens: int = 0) -> list[ev.UIEvent]:
    """One completed answer turn, as the runtime would have persisted it."""
    events: list[ev.UIEvent] = [ev.PromptSubmit(**_env(ts), prompt=prompt)]
    if usage_tokens:
        events.append(
            ev.ProviderResponseUsage(
                **_env(ts + 0.3),
                input_tokens=100,
                output_tokens=usage_tokens,
                model="claude-sonnet-4",
            )
        )
    events.append(
        ev.ContentBlockEnd(
            **_env(ts + 0.5),
            block_type="text",
            block={"type": "text", "text": f"answer to {prompt}"},
        )
    )
    events.append(ev.PromptComplete(**_env(ts + 0.9), response=f"answer to {prompt}"))
    return events


def _prompts(events: tuple[ev.UIEvent, ...] | list[ev.UIEvent]) -> list[str]:
    return [e.prompt for e in events if isinstance(e, ev.PromptSubmit)]


def make_reducer() -> tuple[TranscriptReducer, FakeHost]:
    host = FakeHost()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    return reducer, host


# --------------------------------------------------------------------------
# Pure filter: drop_rewound_events
# --------------------------------------------------------------------------


def test_filter_drops_a_single_rewound_turn() -> None:
    events = [
        ev.SessionStart(**_env(0.0)),
        *_turn("A", ts=1.0),
        *_turn("B", ts=2.0),
        *_turn("C ghost", ts=3.0),
        ev.RewindMarker(**_env(3.9), checkpoint_id="t2", kept_turns=2),
        *_turn("D", ts=4.0),
    ]
    kept = ev.drop_rewound_events(events)
    assert _prompts(kept) == ["A", "B", "D"]
    # The marker itself never survives into the replay stream.
    assert all(not isinstance(e, ev.RewindMarker) for e in kept)
    # Session preamble is always kept.
    assert isinstance(kept[0], ev.SessionStart)


def test_filter_composes_repeated_rewinds_to_the_same_depth() -> None:
    """Rewind to t2 twice: both the original tail and the redo tail drop."""
    events = [
        *_turn("A", ts=1.0),
        *_turn("B", ts=2.0),
        *_turn("C ghost", ts=3.0),
        ev.RewindMarker(**_env(3.9), checkpoint_id="t2", kept_turns=2),
        *_turn("C2 ghost", ts=4.0),
        ev.RewindMarker(**_env(4.9), checkpoint_id="t2", kept_turns=2),
        *_turn("C3", ts=5.0),
    ]
    assert _prompts(ev.drop_rewound_events(events)) == ["A", "B", "C3"]


def test_filter_composes_nested_rewinds() -> None:
    """After redoing turn 3, a deeper rewind to t1 peels both back."""
    events = [
        *_turn("A", ts=1.0),
        *_turn("B ghost", ts=2.0),
        *_turn("C ghost", ts=3.0),
        ev.RewindMarker(**_env(3.9), checkpoint_id="t2", kept_turns=2),
        *_turn("B2 ghost", ts=4.0),
        ev.RewindMarker(**_env(4.9), checkpoint_id="t1", kept_turns=1),
        *_turn("B3", ts=5.0),
    ]
    assert _prompts(ev.drop_rewound_events(events)) == ["A", "B3"]


def test_filter_without_markers_is_identity() -> None:
    events = [*_turn("A", ts=1.0), *_turn("B", ts=2.0)]
    assert ev.drop_rewound_events(events) == events


def test_filter_clamps_kept_turns_beyond_the_stream() -> None:
    """A marker asking to keep more turns than exist keeps them all."""
    events = [*_turn("A", ts=1.0), ev.RewindMarker(**_env(1.9), checkpoint_id="t9", kept_turns=9)]
    assert _prompts(ev.drop_rewound_events(events)) == ["A"]


# --------------------------------------------------------------------------
# _kept_turns_for
# --------------------------------------------------------------------------


def _ledger(turn_ids: list[int]) -> OutcomeLedger:
    ledger = OutcomeLedger()
    for index, turn_id in enumerate(turn_ids, start=1):
        ledger.record_turn(
            TurnTelemetry(secs=1.0, tokens_down=10, cost=Decimal("0.01")),
            TurnOutcome(kind="answer"),
            turn_id=turn_id,
            message_index=index,
            label=f"turn {turn_id}",
        )
    return ledger


def test_kept_turns_for_is_the_checkpoint_ordinal() -> None:
    ledger = _ledger([1, 2, 3])
    assert _kept_turns_for(ledger, "t1") == 1
    assert _kept_turns_for(ledger, "t2") == 2
    assert _kept_turns_for(ledger, "t3") == 3
    assert _kept_turns_for(ledger, "t9") == 0  # unknown -> no marker written


# --------------------------------------------------------------------------
# End-to-end resume: restored_ui_events + reducer.replay
# --------------------------------------------------------------------------


def test_resume_after_rewind_shows_no_ghost_turns(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    sid = SID
    log = [
        ev.SessionStart(**_env(0.0)),
        *_turn("build the parser", ts=1.0),
        *_turn("add tests", ts=2.0),
        *_turn("WRONG direction", ts=3.0),  # rewound away
        ev.RewindMarker(**_env(3.95), checkpoint_id="t2", kept_turns=2),
        *_turn("polish the docs", ts=4.0),
    ]
    for event in log:
        store.append_event(sid, event)

    restored = restored_ui_events(store, sid)
    assert _prompts(restored) == ["build the parser", "add tests", "polish the docs"]

    # The trimmed live context has 3 user messages after the rewind + redo.
    reducer, host = make_reducer()
    assert reducer.replay(restored, turn_base=3) is True
    user_lines = [b.text for b in host.blocks if b.kind == "user_line"]
    assert user_lines == ["build the parser", "add tests", "polish the docs"]
    assert "WRONG direction" not in user_lines

    # The surviving checkpoint chain lines up with the transcript, so the
    # ledger is NOT degraded — rewind still works after the resume.
    assert [c.turn_id for c in reducer.ledger.checkpoints] == [1, 2, 3]
    reducer.handle(ev.PromptSubmit(**_env(10.0), prompt="next"))
    reducer.handle(ev.PromptComplete(**_env(11.0), response="ok"))
    assert [c.turn_id for c in reducer.ledger.checkpoints] == [1, 2, 3, 4]


def test_resume_after_nested_rewinds_shows_no_ghost_turns(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    sid = SID
    log = [
        ev.SessionStart(**_env(0.0)),
        *_turn("A", ts=1.0),
        *_turn("B ghost", ts=2.0),
        *_turn("C ghost", ts=3.0),
        ev.RewindMarker(**_env(3.95), checkpoint_id="t2", kept_turns=2),
        *_turn("B2 ghost", ts=4.0),
        ev.RewindMarker(**_env(4.95), checkpoint_id="t1", kept_turns=1),
        *_turn("B3", ts=5.0),
    ]
    for event in log:
        store.append_event(sid, event)

    reducer, host = make_reducer()
    assert reducer.replay(restored_ui_events(store, sid), turn_base=2) is True
    assert [b.text for b in host.blocks if b.kind == "user_line"] == ["A", "B3"]
    assert [c.turn_id for c in reducer.ledger.checkpoints] == [1, 2]


def test_cost_reseed_stays_consistent_with_a_marker_in_the_log(tmp_path: Path) -> None:
    """The rewind marker line must not corrupt the resume cost re-seed:
    ``restore_session_cost`` still sums every priced response exactly once,
    and the reducer reconciles the footer to that single authority."""
    store = SessionStore(base_dir=tmp_path)
    sid = SID
    log = [
        ev.SessionStart(**_env(0.0)),
        *_turn("A", ts=1.0, usage_tokens=1000),
        *_turn("B", ts=2.0, usage_tokens=1000),
        *_turn("C ghost", ts=3.0, usage_tokens=1000),
        ev.RewindMarker(**_env(3.95), checkpoint_id="t2", kept_turns=2),
        *_turn("D", ts=4.0, usage_tokens=1000),
    ]
    for event in log:
        store.append_event(sid, event)

    tracker = CostTracker()
    prior = restore_session_cost(tracker, *store.events_read_paths(sid))
    assert prior is not None and prior > 0

    # Control: the SAME four priced responses without a marker line. The
    # marker is cost-inert — the re-seed sums every response exactly once
    # whether or not the log records a rewind boundary between them.
    control = SessionStore(base_dir=tmp_path / "control")
    for event in [event for event in log if not isinstance(event, ev.RewindMarker)]:
        control.append_event("ctrl01", event)
    control_tracker = CostTracker()
    control_prior = restore_session_cost(control_tracker, *control.events_read_paths("ctrl01"))
    assert prior == control_prior

    reducer, _host = make_reducer()
    assert reducer.replay(restored_ui_events(store, sid), turn_base=3, session_cost=prior) is True
    # The kernel re-seed stays the single footer authority (spec §11).
    assert reducer.session_cost == prior


# --------------------------------------------------------------------------
# RealRuntime.fork stamps the marker
# --------------------------------------------------------------------------


class _FakeContext:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        for n in range(1, 4):
            self.messages.append({"role": "user", "content": f"turn {n}"})
            self.messages.append({"role": "assistant", "content": f"answer {n}"})

    async def get_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self.messages = list(messages)


class _FakeCoordinator:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context

    def get(self, name: str) -> Any:
        return self._context if name == "context" else None


class _FakeInitialized:
    def __init__(self, context: _FakeContext) -> None:
        self.session_id = SID
        self.coordinator = _FakeCoordinator(context)


@pytest.mark.asyncio
async def test_real_runtime_fork_writes_a_rewind_marker(tmp_path: Path) -> None:
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    runtime = RealRuntime()
    store = SessionStore(base_dir=tmp_path)
    runtime._store = store  # type: ignore[assignment]
    runtime._initialized = _FakeInitialized(_FakeContext())  # type: ignore[assignment]
    ledger = _ledger([1, 2, 3])

    outcome = await runtime.fork("t2", ledger)
    assert outcome.forked_from_turn == 2

    markers = [record for record in store.read_events(SID) if record.get("kind") == "rewind_marker"]
    assert len(markers) == 1
    assert markers[0]["checkpoint_id"] == "t2"
    assert markers[0]["kept_turns"] == 2  # keep turns 1 and 2

    # And that marker really drops the discarded turn on the next read.
    typed = restored_ui_events(store, SID)
    assert all(not isinstance(e, ev.RewindMarker) for e in typed)


@pytest.mark.asyncio
async def test_real_runtime_fork_marker_only_after_successful_trim(tmp_path: Path) -> None:
    """A refused fork (turn running) writes no marker — the log must not
    claim a rewind boundary that never happened."""
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    runtime = RealRuntime()
    store = SessionStore(base_dir=tmp_path)
    runtime._store = store  # type: ignore[assignment]
    runtime._initialized = _FakeInitialized(_FakeContext())  # type: ignore[assignment]
    runtime._executing = True  # a submit() turn is live
    ledger = _ledger([1, 2, 3])

    with pytest.raises(RewindError, match="turn still running"):
        await runtime.fork("t2", ledger)
    assert not any(record.get("kind") == "rewind_marker" for record in store.read_events(SID))
