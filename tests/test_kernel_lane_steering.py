"""Per-lane steering through the StepBoundaryBridge (issue #39).

The ONE steering hook now handles BOTH the root SteeringQueue (coordinator)
and a per-lane LaneSteeringQueue (delegates): a child ``provider:request``
drains that child's own lane queue and injects it as a user-role message,
exactly like the root path but keyed by child session id. Pure asyncio.
"""

from __future__ import annotations

import pytest
from amplifier_core import HookResult

from amplifier_app_newtui.kernel.steering import StepBoundaryBridge
from amplifier_app_newtui.model.queues import (
    LaneSteeringQueue,
    QueuedMessage,
    SteeringQueue,
)

ROOT = "sess-root"
CHILD = "sess-child_worker"
OTHER = "sess-child_other"


def _injection(result: HookResult) -> str:
    assert result.context_injection is not None
    return result.context_injection


@pytest.mark.asyncio
async def test_child_step_boundary_delivers_its_lane_steer() -> None:
    lane = LaneSteeringQueue()
    lane.enqueue(CHILD, "prefer the fast path")
    bridge = StepBoundaryBridge(ROOT, SteeringQueue(), lane_steering=lane)

    result = await bridge.handle_event("provider:request", {"session_id": CHILD})
    assert result.action == "inject_context"
    assert "prefer the fast path" in _injection(result)
    assert "delegate" in _injection(result).lower()
    assert result.context_injection_role == "user"
    assert result.suppress_output is True
    # Consumed: the next child boundary injects nothing.
    again = await bridge.handle_event("provider:request", {"session_id": CHILD})
    assert again.action == "continue"


@pytest.mark.asyncio
async def test_one_lane_steer_per_step_fifo() -> None:
    lane = LaneSteeringQueue()
    lane.enqueue(CHILD, "first")
    lane.enqueue(CHILD, "second")
    bridge = StepBoundaryBridge(ROOT, SteeringQueue(), lane_steering=lane)

    first = await bridge.handle_event("provider:request", {"session_id": CHILD})
    assert "first" in _injection(first) and "second" not in _injection(first)
    second = await bridge.handle_event("provider:request", {"session_id": CHILD})
    assert "second" in _injection(second)


@pytest.mark.asyncio
async def test_lanes_are_isolated_by_session() -> None:
    lane = LaneSteeringQueue()
    lane.enqueue(CHILD, "for the worker")
    bridge = StepBoundaryBridge(ROOT, SteeringQueue(), lane_steering=lane)

    # A different child's boundary must not drain the worker's queue.
    other = await bridge.handle_event("provider:request", {"session_id": OTHER})
    assert other.action == "continue"
    assert lane.queued_count(CHILD) == 1


@pytest.mark.asyncio
async def test_root_steer_never_leaks_to_a_child() -> None:
    steering = SteeringQueue()
    steering.enqueue("steer the coordinator")
    lane = LaneSteeringQueue()
    bridge = StepBoundaryBridge(ROOT, steering, lane_steering=lane)

    child = await bridge.handle_event("provider:request", {"session_id": CHILD})
    assert child.action == "continue"
    assert len(steering.pending_steers) == 1  # root queue untouched by the child

    root = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert "steer the coordinator" in _injection(root)


@pytest.mark.asyncio
async def test_child_without_lane_steering_configured_continues() -> None:
    # Regression guard for the historical "root only" contract: with no
    # lane_steering wired, a child boundary is still a no-op.
    bridge = StepBoundaryBridge(ROOT, SteeringQueue())
    result = await bridge.handle_event("provider:request", {"session_id": CHILD})
    assert result.action == "continue"


@pytest.mark.asyncio
async def test_on_lane_applied_receives_session_and_steer() -> None:
    lane = LaneSteeringQueue()
    queued = lane.enqueue(CHILD, "narrate me")
    applied: list[tuple[str, QueuedMessage]] = []
    bridge = StepBoundaryBridge(
        ROOT,
        SteeringQueue(),
        lane_steering=lane,
        on_lane_applied=lambda sid, steer: applied.append((sid, steer)),
    )
    await bridge.handle_event("provider:request", {"session_id": CHILD})
    assert applied == [(CHILD, queued)]


def test_real_runtime_lane_steer_applied_emits_child_stamped_narration() -> None:
    # Delivery echo: consuming a lane steer emits an "Applying steer: <text>"
    # narration stamped with the CHILD session id so the reducer diverts it
    # into that lane's focus transcript (DESIGN-SPEC §8).
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    runtime = RealRuntime()
    runtime._lane_steer_applied(
        CHILD, QueuedMessage(message_id="lane-1", text="focus on the tests")
    )
    event = runtime.queue.get_nowait()
    assert event.kind == "content_block_end"
    assert event.session_id == CHILD
    assert event.block["text"] == "Applying steer: focus on the tests"
    assert event.block["demo_role"] == "narration"


def test_real_runtime_wires_a_shared_lane_steering_queue() -> None:
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    shared = LaneSteeringQueue()
    runtime = RealRuntime(lane_steering=shared)
    assert runtime.lane_steering is shared
