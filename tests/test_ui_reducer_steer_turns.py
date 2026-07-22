"""Checkpoint turn ids vs mid-turn context injections (DESIGN-SPEC §9).

Foundation's ``fork_session[_in_memory]`` slices at the Nth user-role
message — and a consumed steer / answered deferred decision is injected
as a PERSISTENT user-role message (StepBoundaryBridge, ephemeral=False).
The reducer must therefore advance checkpoint turn ids past every
applied injection (``ContextInjected``), or rewind forks one-plus user
messages early on any steered turn.
"""

from __future__ import annotations

from typing import Any

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import BlockIdAllocator, TodoItem, TranscriptBlock
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.model.turn import OutcomeLedger
from amplifier_app_newtui.ui.reducer import TranscriptReducer


class FakeHost:
    """Minimal ReducerHost: records blocks, ignores presentation."""

    mode_id = "chat"

    def __init__(self) -> None:
        self.blocks: list[TranscriptBlock] = []

    def append_block(self, block: TranscriptBlock) -> None:
        self.blocks.append(block)

    def replace_block(self, block: TranscriptBlock) -> None:
        pass

    def remove_block(self, block_id: str) -> None:
        pass

    def show_notice(self, text: str) -> None:
        pass

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


def make_reducer() -> TranscriptReducer:
    return TranscriptReducer(
        FakeHost(),
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )


def run_turn(reducer: TranscriptReducer, prompt: str, *, injections: int = 0) -> None:
    reducer.handle(ev.PromptSubmit(prompt=prompt, ts=1.0))
    for _ in range(injections):
        reducer.handle(ev.ContextInjected())
    reducer.handle(ev.PromptComplete(ts=2.0))


def test_plain_turns_keep_sequential_turn_ids() -> None:
    reducer = make_reducer()
    run_turn(reducer, "one")
    run_turn(reducer, "two")
    assert [cp.turn_id for cp in reducer.ledger.checkpoints] == [1, 2]


def test_steer_injection_shifts_checkpoint_to_last_user_message() -> None:
    # Turn 2 consumes one steer → its transcript is [U1, A1, U2, partial,
    # U-steer, final]; the checkpoint must address user message 3 (the
    # steer) so a fork keeps the whole steered turn (spec §9).
    reducer = make_reducer()
    run_turn(reducer, "one")
    run_turn(reducer, "two", injections=1)
    run_turn(reducer, "three")
    assert [cp.turn_id for cp in reducer.ledger.checkpoints] == [1, 3, 4]


def test_multiple_injections_accumulate() -> None:
    reducer = make_reducer()
    run_turn(reducer, "one", injections=2)  # steer + answered decision steps
    run_turn(reducer, "two")
    assert [cp.turn_id for cp in reducer.ledger.checkpoints] == [3, 4]


def test_turn_base_offsets_resume_history_before_first_checkpoint() -> None:
    reducer = make_reducer()
    reducer.turn_base = 5  # resumed session: 5 user messages restored
    run_turn(reducer, "one", injections=1)
    assert [cp.turn_id for cp in reducer.ledger.checkpoints] == [7]


def test_trim_rewinds_turn_ids_past_dropped_injections() -> None:
    reducer = make_reducer()
    run_turn(reducer, "one")
    run_turn(reducer, "two", injections=1)
    reducer.ledger.trim_to("t1")  # confirmed fork back to turn 1
    run_turn(reducer, "two-b")
    assert [cp.turn_id for cp in reducer.ledger.checkpoints] == [1, 2]


def test_checkpoint_addresses_foundation_fork_slice_on_steered_turn() -> None:
    # End-to-end against the REAL foundation slicer: fork at the steered
    # turn's checkpoint keeps the steer injection and the steered answer.
    from amplifier_foundation.session.fork import fork_session_in_memory

    reducer = make_reducer()
    run_turn(reducer, "one")
    run_turn(reducer, "two", injections=1)
    steered = reducer.ledger.checkpoints[-1]

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "partial step"},
        {"role": "user", "content": "User steering received during this turn. …"},
        {"role": "assistant", "content": "steered final answer"},
    ]
    result = fork_session_in_memory(messages, turn=steered.turn_id)
    assert result.messages == messages  # nothing dropped mid-turn

    # And rewinding to the PRE-steer turn still cuts before turn two.
    first = reducer.ledger.checkpoints[0]
    result = fork_session_in_memory(messages, turn=first.turn_id)
    assert result.messages == messages[:2]
