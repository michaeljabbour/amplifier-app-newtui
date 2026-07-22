"""Tracker tests: stream status, runtime status, task status, queue bridge,
display system. Pure asyncio; fake hooks; no Textual; no network."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from amplifier_app_newtui.kernel.display import DisplaySystem
from amplifier_app_newtui.kernel.events import Notification, PromptSubmit, ToolPre
from amplifier_app_newtui.kernel.queue_bridge import CONSUMED_EVENTS, QueueBridge
from amplifier_app_newtui.kernel.trackers.runtime_status import RuntimeStatusTracker
from amplifier_app_newtui.kernel.trackers.stream_status import StreamStatusTracker
from amplifier_app_newtui.kernel.trackers.task_status import TaskStatusTracker

ROOT = "sess-root"


class FakeHooks:
    def __init__(self) -> None:
        self.registered: list[tuple[str, int, str]] = []
        self.unregistered: list[str] = []
        self.handlers: dict[str, list[Any]] = {}

    def register(self, event: str, handler: Any, *, priority: int = 0, name: str = "") -> Any:
        self.registered.append((event, priority, name))
        self.handlers.setdefault(event, []).append(handler)
        return lambda: self.unregistered.append(name)

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        for handler in self.handlers.get(event, []):
            await handler(event, data)


# =============================================================================
# StreamStatusTracker
# =============================================================================


def delta(text_key: str, text: str, index: int = 0) -> dict[str, Any]:
    return {
        "session_id": ROOT,
        "request_id": "req-1",
        "block_index": index,
        "block_type": "text",
        text_key: text,
    }


def test_stream_tracker_accumulates_and_consolidates() -> None:
    tracker = StreamStatusTracker(ROOT, clock=lambda: 0.0)
    tracker.consume("llm:stream_block_start", delta("delta", ""))
    tracker.consume("llm:stream_block_delta", delta("delta", "Hello "))
    tracker.consume("llm:stream_block_delta", delta("text", "wor"))
    tracker.consume("llm:stream_block_delta", delta("content", "ld"))
    preview = tracker.preview
    assert preview == ("text", "Hello world")
    assert tracker.estimated_tokens == 3  # ceil(11 / 4)
    tracker.consume(
        "llm:stream_block_end",
        {"session_id": ROOT, "request_id": "req-1", "block_index": 0},
    )
    assert tracker.preview is None
    assert tracker.active_block_count == 0


def test_stream_tracker_ignores_child_sessions() -> None:
    tracker = StreamStatusTracker(ROOT)
    tracker.consume("llm:stream_block_delta", delta("delta", "child text") | {"session_id": "kid"})
    assert tracker.preview is None


def test_stream_tracker_hides_thinking_unless_enabled() -> None:
    tracker = StreamStatusTracker(ROOT)
    payload = delta("delta", "hmm") | {"block_type": "thinking"}
    tracker.consume("llm:stream_block_start", payload)
    tracker.consume("llm:stream_block_delta", payload)
    assert tracker.preview is None

    showing = StreamStatusTracker(ROOT, show_thinking=True, clock=lambda: 0.0)
    showing.consume("llm:stream_block_start", payload)
    showing.consume("llm:stream_block_delta", payload)
    assert showing.preview == ("thinking", "hmm")


def test_stream_tracker_resets_on_lifecycle_events() -> None:
    for reset_event in ("prompt:submit", "orchestrator:complete", "llm:stream_aborted"):
        tracker = StreamStatusTracker(ROOT, clock=lambda: 0.0)
        tracker.consume("llm:stream_block_delta", delta("delta", "abc"))
        assert tracker.preview is not None
        tracker.consume(reset_event, {"session_id": ROOT})
        assert tracker.preview is None


def test_stream_tracker_throttles_delta_notifications() -> None:
    now = {"t": 0.0}
    tracker = StreamStatusTracker(ROOT, clock=lambda: now["t"])
    calls: list[int] = []
    tracker.add_listener(lambda: calls.append(1))
    now["t"] = 1.0
    tracker.consume("llm:stream_block_delta", delta("delta", "a"))
    first = len(calls)
    now["t"] = 1.01  # within the 50ms window — suppressed
    tracker.consume("llm:stream_block_delta", delta("delta", "b"))
    assert len(calls) == first
    now["t"] = 1.2
    tracker.consume("llm:stream_block_delta", delta("delta", "c"))
    assert len(calls) == first + 1


def test_stream_tracker_register_hooks_roundtrip() -> None:
    hooks = FakeHooks()
    tracker = StreamStatusTracker(ROOT)
    unregister = tracker.register_hooks(hooks)
    assert [event for event, _, _ in hooks.registered] == list(tracker.EVENTS)
    unregister()
    assert len(hooks.unregistered) == len(tracker.EVENTS)


# =============================================================================
# RuntimeStatusTracker
# =============================================================================


def usage(session_id: str = ROOT, **overrides: Any) -> dict[str, Any]:
    payload = {
        "session_id": session_id,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 10,
        },
        "model": "claude-fable-5",
    }
    payload["usage"].update(overrides)
    return payload


def test_runtime_tracker_turn_boundaries() -> None:
    now = {"t": 100.0}
    tracker = RuntimeStatusTracker(ROOT, clock=lambda: now["t"])
    assert not tracker.running
    tracker.consume("prompt:submit", {"session_id": ROOT, "prompt": "go"})
    assert tracker.running
    now["t"] = 103.5
    assert tracker.turn_elapsed == pytest.approx(3.5)
    tracker.consume("provider:response", usage())
    snap = tracker.snapshot()
    assert snap.turn.output_tokens == 40
    assert snap.turn.cache_hit_pct == 75  # 300 / (100+300)
    tracker.consume("prompt:complete", {"session_id": ROOT})
    assert not tracker.running
    # New root turn resets turn totals but keeps session totals.
    tracker.consume("prompt:submit", {"session_id": ROOT, "prompt": "next"})
    snap = tracker.snapshot()
    assert snap.turn.requests == 0
    assert snap.session.requests == 1


def test_runtime_tracker_child_usage_counts_toward_turn_and_session() -> None:
    tracker = RuntimeStatusTracker(ROOT)
    tracker.consume("prompt:submit", {"session_id": ROOT, "prompt": "go"})
    tracker.consume("provider:response", usage(session_id="sess-child_worker"))
    snap = tracker.snapshot()
    assert snap.turn.requests == 1
    assert snap.session.requests == 1
    # …but a CHILD prompt:submit never resets the root turn.
    tracker.consume("prompt:submit", {"session_id": "sess-child_worker", "prompt": "x"})
    assert tracker.snapshot().turn.requests == 1


def test_runtime_tracker_cost_fn_and_seed() -> None:
    tracker = RuntimeStatusTracker(
        ROOT, cost_fn=lambda event: Decimal("0.25") if event.output_tokens else Decimal("0")
    )
    tracker.consume("prompt:submit", {"session_id": ROOT})
    tracker.consume("provider:response", usage())
    tracker.consume("provider:response", usage())
    assert tracker.snapshot().turn.cost == Decimal("0.50")
    tracker.seed_session_cost(Decimal("1.00"))
    assert tracker.snapshot().session.cost == Decimal("1.50")


def test_runtime_tracker_provider_notices() -> None:
    tracker = RuntimeStatusTracker(ROOT)
    tracker.consume("provider:throttle", {"session_id": ROOT, "message": "rate limited"})
    notice = tracker.snapshot().last_notice
    assert notice is not None
    assert notice.notice == "throttle"
    assert notice.message == "rate limited"
    # Cleared at the next root turn.
    tracker.consume("prompt:submit", {"session_id": ROOT})
    assert tracker.snapshot().last_notice is None


def test_runtime_tracker_broken_cost_fn_does_not_crash() -> None:
    def broken(event: Any) -> Decimal:
        raise RuntimeError("no pricing table")

    tracker = RuntimeStatusTracker(ROOT, cost_fn=broken)
    tracker.consume("provider:response", usage())
    assert tracker.snapshot().session.cost == Decimal("0")


# =============================================================================
# TaskStatusTracker
# =============================================================================


def test_task_tracker_opens_and_completes_lanes() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "task:agent_spawned",
        {
            "session_id": ROOT,
            "agent": "test-writer",
            "sub_session_id": f"{ROOT}-abc123_test-writer",
            "parent_session_id": ROOT,
        },
    )
    assert tracker.active_count == 1
    lane = tracker.lane(f"{ROOT}-abc123_test-writer")
    assert lane is not None
    assert lane.lane.name == "test-writer"
    assert lane.lane.state == "running"
    assert lane.lane.glyph == "◐"

    tracker.consume(
        "task:agent_completed",
        {
            "session_id": ROOT,
            "agent": "test-writer",
            "sub_session_id": f"{ROOT}-abc123_test-writer",
            "parent_session_id": ROOT,
            "success": True,
        },
    )
    assert tracker.active_count == 0
    lane = tracker.lane(f"{ROOT}-abc123_test-writer")
    assert lane is not None and lane.lane.state == "done"
    assert lane.lane.glyph == "✔"


def test_task_tracker_legacy_event_names() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "task:spawned",
        {"session_id": ROOT, "agent": "worker", "sub_session_id": "kid-1_worker"},
    )
    assert tracker.active_count == 1
    tracker.consume(
        "task:completed",
        {"session_id": ROOT, "sub_session_id": "kid-1_worker", "success": False},
    )
    lane = tracker.lane("kid-1_worker")
    assert lane is not None
    assert lane.lane.state == "done"
    assert "failed" in lane.lane.activity


def test_task_tracker_depth_race_child_before_parent() -> None:
    tracker = TaskStatusTracker(ROOT)
    # Grandchild spawn event arrives before its parent's.
    tracker.consume(
        "task:agent_spawned",
        {
            "session_id": "kid-1_worker",
            "sub_session_id": "kid-1_worker-9f_helper",
            "parent_session_id": "kid-1_worker",
            "agent": "helper",
        },
    )
    grandchild = tracker.lane("kid-1_worker-9f_helper")
    assert grandchild is not None and grandchild.depth == 1  # parent unknown yet
    tracker.consume(
        "task:agent_spawned",
        {
            "session_id": ROOT,
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
            "agent": "worker",
        },
    )
    grandchild = tracker.lane("kid-1_worker-9f_helper")
    assert grandchild is not None and grandchild.depth == 2  # retro-patched


def test_task_tracker_session_start_races_agent_spawned() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume("session:start", {"session_id": "kid-1_worker", "parent_id": ROOT})
    assert tracker.active_count == 1
    # Later duplicate registration is idempotent.
    tracker.consume(
        "task:agent_spawned",
        {"session_id": ROOT, "sub_session_id": "kid-1_worker", "agent": "worker"},
    )
    assert tracker.active_count == 1
    tracker.consume("session:end", {"session_id": "kid-1_worker", "parent_id": ROOT})
    assert tracker.active_count == 0


def test_task_tracker_completion_races_ahead_of_spawn() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "task:agent_completed",
        {"session_id": ROOT, "sub_session_id": "kid-2_scout", "success": True},
    )
    lane = tracker.lane("kid-2_scout")
    assert lane is not None
    assert lane.lane.state == "done"
    assert lane.lane.name == "scout"


def test_task_tracker_ignores_root_session_events() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume("session:start", {"session_id": ROOT, "parent_id": None})
    tracker.consume("task:agent_spawned", {"session_id": ROOT, "sub_session_id": ROOT})
    assert tracker.active_count == 0


def test_task_tracker_subscribes_to_delegate_lifecycle() -> None:
    """anchors' tool-delegate emits delegate:* — the lanes panel and the
    working-line agent count go blind without these subscriptions."""
    for name in (
        "delegate:agent_spawned",
        "delegate:agent_completed",
        "delegate:agent_resumed",
        "delegate:agent_cancelled",
        "delegate:error",
    ):
        assert name in TaskStatusTracker.EVENTS, name


def test_task_tracker_delegate_spawn_and_complete() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "delegate:agent_spawned",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
        },
    )
    assert tracker.active_count == 1
    tracker.consume(
        "delegate:agent_completed",
        {
            "session_id": ROOT,
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
            "success": True,
        },
    )
    assert tracker.active_count == 0


def test_task_tracker_delegate_resume_reopens_lane() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "delegate:agent_resumed",
        {"session_id": "kid-1_worker", "parent_session_id": ROOT},
    )
    assert tracker.active_count == 1
    lane = tracker.lane("kid-1_worker")
    assert lane is not None
    assert lane.lane.name == "worker"  # recovered from the session-id suffix


def test_task_tracker_delegate_cancelled_shows_cancelled() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "delegate:agent_spawned",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
        },
    )
    tracker.consume(
        "delegate:agent_cancelled",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
        },
    )
    lane = tracker.lane("kid-1_worker")
    assert lane is not None
    assert lane.lane.state == "done"
    assert "cancelled" in lane.lane.activity


# =============================================================================
# QueueBridge
# =============================================================================


@pytest.mark.asyncio
async def test_queue_bridge_normalizes_into_queue() -> None:
    bridge = QueueBridge()
    result = await bridge.handle_event(
        "tool:pre",
        {"session_id": ROOT, "tool_name": "bash", "tool_call_id": "c1", "tool_input": {}},
    )
    assert result.action == "continue"
    event = bridge.queue.get_nowait()
    assert isinstance(event, ToolPre)
    assert event.tool_name == "bash"
    assert event.session_id == ROOT


@pytest.mark.asyncio
async def test_queue_bridge_drops_unknown_events_silently() -> None:
    bridge = QueueBridge()
    await bridge.handle_event("some:unknown_event", {"session_id": ROOT})
    assert bridge.queue.empty()


@pytest.mark.asyncio
async def test_queue_bridge_counts_drops_when_bounded_queue_full() -> None:
    bridge = QueueBridge(asyncio.Queue(maxsize=1))
    await bridge.handle_event("prompt:submit", {"session_id": ROOT, "prompt": "a"})
    await bridge.handle_event("prompt:submit", {"session_id": ROOT, "prompt": "b"})
    assert bridge.dropped == 1
    assert isinstance(bridge.queue.get_nowait(), PromptSubmit)


@pytest.mark.asyncio
async def test_queue_bridge_registers_every_consumed_event() -> None:
    hooks = FakeHooks()
    bridge = QueueBridge()
    unregister = bridge.register_hooks(hooks)
    assert [event for event, _, _ in hooks.registered] == list(CONSUMED_EVENTS)
    await hooks.emit("user:notification", {"session_id": ROOT, "message": "hi"})
    assert isinstance(bridge.queue.get_nowait(), Notification)
    unregister()
    assert len(hooks.unregistered) == len(CONSUMED_EVENTS)


def test_consumed_events_cover_delegate_lifecycle() -> None:
    for name in (
        "delegate:agent_spawned",
        "delegate:agent_completed",
        "delegate:agent_resumed",
        "delegate:agent_cancelled",
        "delegate:error",
    ):
        assert name in CONSUMED_EVENTS


@pytest.mark.asyncio
async def test_queue_bridge_normalizes_delegate_error() -> None:
    bridge = QueueBridge()
    await bridge.handle_event(
        "delegate:error",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
            "error": "boom",
        },
    )
    event = bridge.queue.get_nowait()
    assert event.kind == "agent_completed"
    assert event.success is False


@pytest.mark.asyncio
async def test_queue_bridge_synthesizes_agent_completed_result() -> None:
    """Ground truth: tool-delegate's delegate:agent_completed payload has
    NO result field (agent/sub_session_id/parent_session_id/success/
    tool_call_id/parallel_group_id only) — the bridge fills it from the
    spawner-recorded child output so snippets/recaps aren't blank."""
    bridge = QueueBridge(agent_result_lookup={"kid-1_worker": "child said hi"}.get)
    await bridge.handle_event(
        "delegate:agent_completed",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
            "success": True,
            "tool_call_id": "call-1",
            "parallel_group_id": None,
        },
    )
    event = bridge.queue.get_nowait()
    assert event.kind == "agent_completed"
    assert event.result == "child said hi"


@pytest.mark.asyncio
async def test_queue_bridge_keeps_native_result_and_error_markers() -> None:
    """A payload that DOES carry a result is authoritative, and the
    delegate:error normalization marker ('error') is never overwritten."""
    bridge = QueueBridge(agent_result_lookup=lambda _sid: "synthesized")
    await bridge.handle_event(
        "task:agent_completed",
        {"session_id": ROOT, "agent": "w", "sub_session_id": "kid-2_w", "result": "native"},
    )
    assert bridge.queue.get_nowait().result == "native"
    await bridge.handle_event(
        "delegate:error",
        {"session_id": ROOT, "agent": "w", "sub_session_id": "kid-2_w", "error": "boom"},
    )
    assert bridge.queue.get_nowait().result == "error"


@pytest.mark.asyncio
async def test_queue_bridge_without_lookup_leaves_result_empty() -> None:
    bridge = QueueBridge()
    await bridge.handle_event(
        "delegate:agent_completed",
        {"session_id": ROOT, "agent": "w", "sub_session_id": "kid-3_w", "success": True},
    )
    assert bridge.queue.get_nowait().result == ""


# =============================================================================
# DisplaySystem
# =============================================================================


def test_display_system_emits_notification_events() -> None:
    emitted: list[Notification] = []
    display = DisplaySystem(emitted.append, session_id=ROOT)
    display.show_message("bundle loaded", "info", "runtime")
    display.show_error("provider missing")
    assert [n.message for n in emitted] == ["bundle loaded", "provider missing"]
    assert emitted[0].level == "info"
    assert emitted[0].source == "runtime"
    assert emitted[1].level == "error"
    assert all(n.session_id == ROOT for n in emitted)


def test_display_system_nesting_counters() -> None:
    display = DisplaySystem(lambda notification: None)
    assert display.nesting == 0
    display.push_nesting()
    display.push_nesting()
    assert display.nesting == 2
    display.pop_nesting()
    display.pop_nesting()
    display.pop_nesting()  # never negative
    assert display.nesting == 0


def test_display_system_feeds_queue_bridge() -> None:
    bridge = QueueBridge()
    display = DisplaySystem(bridge.emit, session_id=ROOT)
    display.show_message("mode build · auto read,test · ask write,net,spend")
    event = bridge.queue.get_nowait()
    assert isinstance(event, Notification)
    assert event.message.startswith("mode build")
