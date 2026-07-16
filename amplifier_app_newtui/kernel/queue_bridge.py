"""Hooks → asyncio.Queue[UIEvent] bridge.

Hook handlers must return fast (RESEARCH-BRIEF §2): this bridge does the
minimum — normalize the raw payload at the one boundary
(:func:`kernel.events.normalize`) and ``put_nowait`` the typed event.
The Textual app consumes the queue on its own loop and throttles paints;
nothing here blocks the engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from amplifier_core import HookResult

from .events import UIEvent, normalize

CONSUMED_EVENTS: tuple[str, ...] = (
    # Channel A — live deltas
    "llm:stream_block_start",
    "llm:stream_block_delta",
    "llm:stream_block_end",
    "llm:stream_aborted",
    # Channel B — durable records
    "tool:pre",
    "tool:post",
    "tool:error",
    "content_block:start",
    "content_block:end",
    "orchestrator:complete",
    # Turn / execution lifecycle
    "prompt:submit",
    "prompt:complete",
    "execution:start",
    "execution:end",
    # Provider telemetry / notices
    "provider:response",
    "provider:error",
    "provider:retry",
    "provider:throttle",
    # Session lifecycle
    "session:start",
    "session:end",
    "session:fork",
    "session:resume",
    # Approvals / cancellation
    "approval:required",
    "approval:granted",
    "approval:denied",
    "cancel:requested",
    "cancel:completed",
    # Subagents (canonical + legacy names) / notifications
    "task:agent_spawned",
    "task:agent_completed",
    "task:spawned",
    "task:completed",
    "user:notification",
)
"""Every raw hook event :func:`normalize` produces a UIEvent for."""


class QueueBridge:
    """Registers one fast handler per consumed event, feeding the queue."""

    EVENTS = CONSUMED_EVENTS

    def __init__(self, queue: asyncio.Queue[UIEvent] | None = None) -> None:
        self.queue: asyncio.Queue[UIEvent] = queue if queue is not None else asyncio.Queue()
        self.dropped = 0
        """Events lost to a full (bounded) queue — should stay 0 with the
        default unbounded queue; surfaced for tests/diagnostics."""

    def emit(self, event: UIEvent) -> None:
        """Push one already-typed event (used by DisplaySystem notices and
        app-synthesized events)."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        normalized = normalize(event, data or {})
        if normalized is not None:
            self.emit(normalized)
        return HookResult(action="continue")

    def register_hooks(self, hooks: Any, *, priority: int = 10) -> Callable[[], None]:
        unregister_callbacks: list[Callable[[], None]] = []
        for event in self.EVENTS:
            unregister = hooks.register(
                event,
                self.handle_event,
                priority=priority,
                name=f"newtui-queue-bridge-{event.replace(':', '-')}",
            )
            if callable(unregister):
                unregister_callbacks.append(unregister)

        def unregister_all() -> None:
            for unregister in reversed(unregister_callbacks):
                unregister()

        return unregister_all


__all__ = ["CONSUMED_EVENTS", "QueueBridge"]
