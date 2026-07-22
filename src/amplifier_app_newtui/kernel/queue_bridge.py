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

from .events import (
    AgentCompleted,
    ContentBlockEnd,
    Notification,
    UIEvent,
    normalize,
    usage_from_content_block_end,
)

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
    # Context telemetry
    "context:compaction",
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
    "delegate:agent_spawned",
    "delegate:agent_completed",
    "delegate:agent_resumed",
    "delegate:agent_cancelled",
    "delegate:error",
    "user:notification",
    # Recipes (tool-recipes approval gates → durable ApprovalRequired
    # record; presentation + answer routing live in kernel/recipes.py)
    "recipe:approval",
)
"""Every raw hook event :func:`normalize` produces a UIEvent for."""

IGNORED_EVENTS: frozenset[str] = frozenset(
    {
        # Mutation point consumed by dedicated hooks (kernel/steering.py,
        # kernel/clipboard.py) — never a display event.
        "provider:request",
        # Channel A consumes the provider streaming family
        # (``llm:stream_block_*``); the kernel's canonical delta/thinking
        # stream and raw LLM round-trip records duplicate it.
        "content_block:delta",
        "thinking:delta",
        "thinking:final",
        "llm:request",
        "llm:response",
        # ``context:compaction`` is the consumed summary; the pre/post
        # pair is engine-internal detail.
        "context:pre_compact",
        "context:post_compact",
        # Module-contributed informational events observed steady-state in
        # real sessions (canary graduation, 2026-07-22): memory-hook
        # bookkeeping and mention resolution carry no UI-worthy payload.
        "memory:briefing_skipped",
        "memory:drawer_filed",
        "memory:interject_skipped",
        "mentions:resolved",
    }
)
"""Hook events the app deliberately leaves unbridged — exempt from the
drift canary. Anything else the engine publishes but CONSUMED_EVENTS
does not name is upstream drift and must surface, not vanish."""


def _published_event_names() -> tuple[str, ...]:
    """The installed core's canonical hook-event names (drift ground truth)."""
    try:
        from amplifier_core import events as core_events
    except ImportError:  # canary degrades to normalize-time detection only
        logger.debug("amplifier_core.events unavailable")
        return ()
    # getattr: ALL_EVENTS is re-exported from the Rust engine and absent
    # from type stubs; its disappearance must degrade, not crash.
    return tuple(str(name) for name in getattr(core_events, "ALL_EVENTS", ()))


class QueueBridge:
    """Registers one fast handler per consumed event, feeding the queue."""

    EVENTS = CONSUMED_EVENTS

    def __init__(
        self,
        queue: asyncio.Queue[UIEvent] | None = None,
        *,
        tap: Callable[[UIEvent], None] | None = None,
        events: tuple[str, ...] | None = None,
        agent_result_lookup: Callable[[str], str] | None = None,
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
        self._canaried: set[str] = set()
        """Event kinds already surfaced by the drift canary — one
        notification per kind per session, never spam."""
        self._agent_result_lookup = agent_result_lookup
        """Fills an empty ``AgentCompleted.result`` by sub-session id.
        Foundation tool-delegate's ``delegate:agent_completed`` payload
        carries no result field (verified against the pinned module), so
        without this the delegate-summary snippets and lane recaps stay
        blank; the spawner records each child's final output."""

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

    def _canary(self, event_name: str) -> None:
        """Surface an event kind the pipeline would otherwise silently drop.

        Once per kind per session: a logger line plus a debug-level
        Notification UIEvent — visible, not spammy.
        """
        if event_name in self._canaried:
            return
        self._canaried.add(event_name)
        logger.info("unbridged event kind: %s", event_name)
        self.emit(
            Notification(
                message=f"unbridged event kind · {event_name}",
                level="debug",
                source="event-canary",
            )
        )

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        normalized = normalize(event, data or {})
        if normalized is None:
            # A subscribed name normalize() no longer recognizes:
            # CONSUMED_EVENTS drifted from the normalization boundary.
            self._canary(event)
        if normalized is not None:
            # The streaming orchestrator never fires ``provider:response``;
            # per-response usage (tokens + provider-computed cost) rides on
            # the final content block. Synthesize the telemetry event first
            # so cost/token consumers stay single-sourced (spec §11).
            if isinstance(normalized, ContentBlockEnd):
                usage = usage_from_content_block_end(normalized)
                if usage is not None:
                    self.emit(usage)
            if (
                isinstance(normalized, AgentCompleted)
                and not normalized.result
                and self._agent_result_lookup is not None
            ):
                try:
                    result = self._agent_result_lookup(
                        normalized.sub_session_id or normalized.session_id
                    )
                except Exception:  # noqa: BLE001 — lookup is best-effort enrichment
                    result = ""
                if result:
                    normalized = normalized.model_copy(update={"result": result})
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

    async def register_canary(self, coordinator: Any, *, priority: int = 990) -> Callable[[], None]:
        """Observe hook events the app neither bridges nor deliberately ignores.

        The Rust hook registry matches exact names only (no wildcard
        subscription — ground-truthed against amplifier-core 1.6:
        ``register("*", ...)`` stores a literal name that never fires), so
        drift is observed the same way native ``hooks-logging`` does it:
        subscribe per-name to the core's published ``ALL_EVENTS`` plus
        module-contributed names from the ``observability.events``
        capability / contribution channel. First occurrence of any name
        outside CONSUMED_EVENTS ∪ IGNORED_EVENTS canaries once.
        """
        names: list[str] = list(_published_event_names())
        getter = getattr(coordinator, "get_capability", None)
        if callable(getter):
            discovered: Any = getter("observability.events") or ()
            names.extend(str(name) for name in discovered)
        collect = getattr(coordinator, "collect_contributions", None)
        if callable(collect):
            contributions: Any
            try:
                pending: Any = collect("observability.events")
                contributions = await pending
            except Exception:  # noqa: BLE001 — discovery is best-effort observation
                logger.debug("observability.events contributions failed", exc_info=True)
                contributions = []
            for contribution in contributions or []:
                if isinstance(contribution, str):
                    names.append(contribution)
                elif isinstance(contribution, list):
                    names.extend(str(name) for name in contribution)

        async def handle_unbridged(event: str, data: dict[str, Any]) -> HookResult:
            self._canary(event)
            return HookResult(action="continue")

        known = set(CONSUMED_EVENTS) | set(self._events) | IGNORED_EVENTS
        hooks = coordinator.hooks
        unregister_callbacks: list[Callable[..., object]] = []
        for event in dict.fromkeys(names):
            if event in known:
                continue
            unregister = hooks.register(
                event,
                handle_unbridged,
                priority=priority,
                name=f"newtui-event-canary-{event.replace(':', '-')}",
            )
            if callable(unregister):
                unregister_callbacks.append(unregister)

        def unregister_all() -> None:
            for unregister in reversed(unregister_callbacks):
                unregister()

        return unregister_all


__all__ = ["CONSUMED_EVENTS", "IGNORED_EVENTS", "QueueBridge"]
