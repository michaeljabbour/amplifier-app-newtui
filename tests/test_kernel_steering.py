"""StepBoundaryBridge tests: one steer per provider:request, root only,
answered-decision injection, leftover-steer drain. Pure asyncio."""

from __future__ import annotations

from typing import Any

import pytest
from amplifier_core import HookResult

from amplifier_app_newtui.kernel.steering import StepBoundaryBridge
from amplifier_app_newtui.model.queues import (
    NeedsYouItem,
    NeedsYouQueue,
    QueuedMessage,
    SteeringQueue,
)

ROOT = "sess-root"


def _injection(result: HookResult) -> str:
    """The injected context, asserted present (narrows ``str | None``)."""
    assert result.context_injection is not None
    return result.context_injection


class FakeHooks:
    def __init__(self) -> None:
        self.registered: list[tuple[str, int, str]] = []
        self.unregistered: list[str] = []

    def register(
        self, event: str, handler: Any, *, priority: int = 0, name: str = ""
    ) -> Any:
        self.registered.append((event, priority, name))
        return lambda: self.unregistered.append(name)


@pytest.mark.asyncio
async def test_drains_exactly_one_steer_per_step() -> None:
    steering = SteeringQueue()
    steering.enqueue("focus on the parser")
    steering.enqueue("skip the docs")
    bridge = StepBoundaryBridge(ROOT, steering)

    first = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert first.action == "inject_context"
    assert "focus on the parser" in _injection(first)
    assert "skip the docs" not in _injection(first)
    assert first.context_injection_role == "user"
    assert first.suppress_output is True

    second = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert "skip the docs" in _injection(second)

    third = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert third.action == "continue"


@pytest.mark.asyncio
async def test_root_session_only() -> None:
    steering = SteeringQueue()
    steering.enqueue("steer me")
    bridge = StepBoundaryBridge(ROOT, steering)
    result = await bridge.handle_event(
        "provider:request", {"session_id": "sess-child_worker"}
    )
    assert result.action == "continue"
    assert len(steering.pending_steers) == 1  # untouched for the child


@pytest.mark.asyncio
async def test_next_turn_messages_are_never_injected_mid_turn() -> None:
    steering = SteeringQueue()
    steering.enqueue("full follow-up", kind="next_turn")
    bridge = StepBoundaryBridge(ROOT, steering)
    result = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert result.action == "continue"
    assert len(steering.pending_next_turn) == 1


@pytest.mark.asyncio
async def test_answered_decisions_ride_the_same_boundary() -> None:
    steering = SteeringQueue()
    needs_you = NeedsYouQueue()
    item = needs_you.defer("Push to fork?", "trust boundary")
    needs_you.answer(item.decision_id, "yes · push to fork")
    applied: list[QueuedMessage] = []
    answers: list[tuple[NeedsYouItem, ...]] = []
    bridge = StepBoundaryBridge(
        ROOT,
        steering,
        needs_you=needs_you,
        on_applied=applied.append,
        on_answers=answers.append,
    )
    result = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert result.action == "inject_context"
    assert "Push to fork?" in _injection(result)
    assert "yes · push to fork" in _injection(result)
    assert applied == []  # no steer this step
    assert len(answers) == 1
    # Consumed: the same answer never re-injects.
    again = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert again.action == "continue"


@pytest.mark.asyncio
async def test_steer_and_answers_combine_into_one_injection() -> None:
    steering = SteeringQueue()
    steering.enqueue("prefer the fast path")
    needs_you = NeedsYouQueue()
    item = needs_you.defer("Enable cache?", "")
    needs_you.answer(item.decision_id, "yes")
    bridge = StepBoundaryBridge(ROOT, steering, needs_you=needs_you)
    result = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert "prefer the fast path" in _injection(result)
    assert "Enable cache?" in _injection(result)


@pytest.mark.asyncio
async def test_on_inject_fires_once_per_persistent_injection() -> None:
    # The injection is ONE persistent user-role message (steer + answers
    # combined) — foundation's fork counts it as a turn boundary, so the
    # runtime is told exactly once per applied injection (spec §9).
    steering = SteeringQueue()
    steering.enqueue("prefer the fast path")
    needs_you = NeedsYouQueue()
    item = needs_you.defer("Enable cache?", "")
    needs_you.answer(item.decision_id, "yes")
    injects: list[None] = []
    bridge = StepBoundaryBridge(
        ROOT, steering, needs_you=needs_you, on_inject=lambda: injects.append(None)
    )
    result = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert result.action == "inject_context"
    assert len(injects) == 1  # steer + answer = one injected message
    again = await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert again.action == "continue"
    assert len(injects) == 1  # nothing injected → no callback


@pytest.mark.asyncio
async def test_on_applied_callback_receives_the_steer() -> None:
    steering = SteeringQueue()
    queued = steering.enqueue("steer text")
    applied: list[QueuedMessage] = []
    bridge = StepBoundaryBridge(ROOT, steering, on_applied=applied.append)
    await bridge.handle_event("provider:request", {"session_id": ROOT})
    assert applied == [queued]


def test_leftover_steers_discarded_via_drain() -> None:
    # The bridge leaves un-applied steers in the queue; at turn end the app
    # drains and DISCARDS them (mockup: an unconsumed steer never becomes
    # a turn the user never sent — ADR-0007 §Steering).
    steering = SteeringQueue()
    steering.enqueue("never applied")
    steering.enqueue("also pending")
    steering.enqueue("next turn message", kind="next_turn")
    leftover = steering.drain_steers()
    assert [m.text for m in leftover] == ["never applied", "also pending"]
    assert len(steering.pending_next_turn) == 1  # untouched


def test_register_hooks_priority_950() -> None:
    hooks = FakeHooks()
    bridge = StepBoundaryBridge(ROOT, SteeringQueue())
    unregister = bridge.register_hooks(hooks)
    assert hooks.registered == [
        ("provider:request", 950, "newtui-step-boundary-steering")
    ]
    unregister()
    assert hooks.unregistered == ["newtui-step-boundary-steering"]


@pytest.mark.asyncio
async def test_non_provider_request_events_continue() -> None:
    bridge = StepBoundaryBridge(ROOT, SteeringQueue())
    result = await bridge.handle_event("tool:pre", {"session_id": ROOT})
    assert result.action == "continue"


def test_real_runtime_steer_applied_emits_narration() -> None:
    # DESIGN-SPEC §5 / mockup runTurn L327: consuming a steer at a step
    # boundary logs the "Applying steer: <text>" narration — the real
    # runtime wires the bridge's on_applied to this transcript emission.
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    runtime = RealRuntime()
    runtime._steer_applied(QueuedMessage(message_id="m1", text="focus on the tests"))
    event = runtime.queue.get_nowait()
    assert event.kind == "content_block_end"
    assert event.block["text"] == "Applying steer: focus on the tests"
    assert event.block["demo_role"] == "narration"
