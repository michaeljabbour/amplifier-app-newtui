"""Hooks → asyncio.Queue[UIEvent] bridge.

Hook handlers must return fast (RESEARCH-BRIEF §2): this bridge does the
minimum — normalize the raw payload at the one boundary
(:func:`kernel.events.normalize`) and ``put_nowait`` the typed event.
The Textual app consumes the queue on its own loop and throttles paints;
nothing here blocks the engine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from amplifier_core import HookResult

from .events import ContentBlockEnd, UIEvent, normalize, usage_from_content_block_end

logger = logging.getLogger(__name__)

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

    def __init__(
        self,
        queue: asyncio.Queue[UIEvent] | None = None,
        *,
        tap: Callable[[UIEvent], None] | None = None,
        events: tuple[str, ...] | None = None,
    ) -> None:
        self.queue: asyncio.Queue[UIEvent] = queue if queue is not None else asyncio.Queue()
        self.dropped = 0
        """Events lost to a full (bounded) queue — should stay 0 with the
        default unbounded queue; surfaced for tests/diagnostics."""
        self._tap = tap
        """Optional synchronous observer of every emitted event — the
        kernel-side seam for evidence derivation / event logging
        (ADR-0007 resolution 9). Best-effort: a tap failure never blocks
        the queue."""
        self._events: tuple[str, ...] = events if events is not None else CONSUMED_EVENTS
        """Raw hook events this bridge instance registers for. The real
        runtime excludes ``prompt:complete`` here and synthesizes its own
        enriched close-out event after the end-of-turn git snapshot."""

    def emit(self, event: UIEvent) -> None:
        """Push one already-typed event (used by DisplaySystem notices and
        app-synthesized events)."""
        if self._tap is not None:
            try:
                self._tap(event)
            except Exception:  # noqa: BLE001 — taps are best-effort observers
                logger.warning("event tap failed", exc_info=True)
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        normalized = normalize(event, data or {})
        if normalized is not None:
            # The streaming orchestrator never fires ``provider:response``;
            # per-response usage (tokens + provider-computed cost) rides on
            # the final content block. Synthesize the telemetry event first
            # so cost/token consumers stay single-sourced (spec §11).
            if isinstance(normalized, ContentBlockEnd):
                usage = usage_from_content_block_end(normalized)
                if usage is not None:
                    self.emit(usage)
            self.emit(normalized)
        return HookResult(action="continue")

    def register_hooks(self, hooks: Any, *, priority: int = 10) -> Callable[[], None]:
        unregister_callbacks: list[Callable[..., object]] = []
        for event in self._events:
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
