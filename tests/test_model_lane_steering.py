"""LaneSteeringQueue tests (issue #39): per-lane bounded steer FIFOs.

Pure model — no Textual, no kernel. Mirrors test_model_turn_queues_lanes
style: exercise enqueue/consume/drain, per-lane isolation, bounds, and the
change-listener that repaints the ``▸ N queued`` badge.
"""

from __future__ import annotations

import itertools

import pytest

from amplifier_app_newtui.model.queues import (
    MAX_QUEUE_ITEMS,
    LaneSteeringQueue,
)


def test_enqueue_is_per_lane_fifo() -> None:
    queue = LaneSteeringQueue(clock=lambda: 0.0)
    queue.enqueue("lane-a", "focus on the parser")
    queue.enqueue("lane-a", "then the docs")
    queue.enqueue("lane-b", "run the migration")

    assert [m.text for m in queue.pending_for("lane-a")] == [
        "focus on the parser",
        "then the docs",
    ]
    assert [m.text for m in queue.pending_for("lane-b")] == ["run the migration"]
    assert queue.queued_count("lane-a") == 2
    assert queue.queued_count("lane-b") == 1
    assert queue.counts() == {"lane-a": 2, "lane-b": 1}
    assert queue.total_pending == 3


def test_enqueue_stamps_steer_kind_and_unique_ids() -> None:
    queue = LaneSteeringQueue(clock=lambda: 1.5)
    first = queue.enqueue("lane-a", "one")
    second = queue.enqueue("lane-b", "two")
    assert first.kind == "steer" and second.kind == "steer"
    assert first.message_id != second.message_id
    assert first.created_at == 1.5


def test_consume_next_pops_oldest_then_none() -> None:
    queue = LaneSteeringQueue(clock=lambda: 0.0)
    queue.enqueue("lane-a", "first")
    queue.enqueue("lane-a", "second")

    assert queue.consume_next("lane-a").text == "first"
    assert queue.consume_next("lane-a").text == "second"
    # Empty now: the lane key is dropped and further reads are None.
    assert queue.consume_next("lane-a") is None
    assert queue.queued_count("lane-a") == 0
    assert queue.counts() == {}


def test_consume_unknown_lane_is_none() -> None:
    queue = LaneSteeringQueue()
    assert queue.consume_next("never-seen") is None


def test_drain_drops_a_finished_lanes_backlog() -> None:
    queue = LaneSteeringQueue(clock=lambda: 0.0)
    queue.enqueue("lane-a", "undelivered one")
    queue.enqueue("lane-a", "undelivered two")
    queue.enqueue("lane-b", "still live")

    drained = queue.drain("lane-a")
    assert [m.text for m in drained] == ["undelivered one", "undelivered two"]
    assert queue.queued_count("lane-a") == 0
    assert queue.queued_count("lane-b") == 1  # other lanes untouched
    assert queue.drain("lane-a") == ()  # idempotent on an empty lane


def test_empty_text_and_missing_session_raise() -> None:
    queue = LaneSteeringQueue()
    with pytest.raises(ValueError):
        queue.enqueue("lane-a", "   ")
    with pytest.raises(ValueError):
        queue.enqueue("", "has text")


def test_bound_is_per_lane() -> None:
    queue = LaneSteeringQueue(clock=lambda: 0.0)
    for n in range(MAX_QUEUE_ITEMS):
        queue.enqueue("lane-a", f"steer {n}")
    with pytest.raises(ValueError):
        queue.enqueue("lane-a", "one too many")
    # A different lane still has its full budget.
    queue.enqueue("lane-b", "fresh budget")
    assert queue.queued_count("lane-b") == 1


def test_control_chars_are_stripped_but_newlines_kept() -> None:
    queue = LaneSteeringQueue()
    message = queue.enqueue("lane-a", "line1\nline2\x07tail")
    assert message.text == "line1\nline2tail"


def test_listener_fires_on_enqueue_consume_and_drain() -> None:
    queue = LaneSteeringQueue(clock=lambda: 0.0)
    counter = itertools.count()
    fires: list[int] = []
    remove = queue.add_listener(lambda: fires.append(next(counter)))

    queue.enqueue("lane-a", "a")
    queue.enqueue("lane-a", "b")
    queue.consume_next("lane-a")
    queue.drain("lane-a")
    assert len(fires) == 4

    remove()
    queue.enqueue("lane-a", "c")
    assert len(fires) == 4  # removed listener is silent
