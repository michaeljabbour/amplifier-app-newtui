"""Event-drift canary (kernel/queue_bridge.py).

CONSUMED_EVENTS is a hardcoded list; upstream renames/additions used to
silently disappear. The canary makes them observable exactly once per
kind per session, via two seams:

- ``register_canary``: per-name subscription to the installed core's
  ``ALL_EVENTS`` plus ``observability.events`` module contributions —
  the same native discovery hooks-logging uses (the Rust hook registry
  has no wildcard matching; ground-truthed against amplifier-core 1.6).
- ``handle_event``: a subscribed name ``normalize()`` no longer
  recognizes (CONSUMED_EVENTS drifted from the normalization boundary).
"""

from __future__ import annotations

import asyncio
from typing import Any

from amplifier_app_newtui.kernel.events import Notification, UIEvent
from amplifier_app_newtui.kernel.queue_bridge import (
    CONSUMED_EVENTS,
    IGNORED_EVENTS,
    QueueBridge,
)


class FakeHooks:
    def __init__(self) -> None:
        self.handlers: dict[str, list[tuple[str | None, Any]]] = {}

    def register(self, event: str, handler: Any, priority: int = 100, name: str | None = None):
        self.handlers.setdefault(event, []).append((name, handler))

        def unregister() -> None:
            self.handlers[event] = [
                entry for entry in self.handlers[event] if entry[1] is not handler
            ]

        return unregister

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        for _, handler in list(self.handlers.get(event, [])):
            await handler(event, data)


class FakeCoordinator:
    def __init__(
        self,
        capability: list[str] | None = None,
        contributions: list[Any] | None = None,
    ) -> None:
        self.hooks = FakeHooks()
        self._capability = capability
        self._contributions = contributions or []

    def get_capability(self, name: str) -> Any:
        return self._capability if name == "observability.events" else None

    async def collect_contributions(self, channel: str) -> list[Any]:
        return self._contributions if channel == "observability.events" else []


def _drain(queue: asyncio.Queue[UIEvent]) -> list[UIEvent]:
    events: list[UIEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _canary_notices(events: list[UIEvent]) -> list[Notification]:
    return [
        event
        for event in events
        if isinstance(event, Notification) and event.source == "event-canary"
    ]


def test_unknown_kind_canaries_exactly_once_per_kind() -> None:
    async def run() -> list[UIEvent]:
        queue: asyncio.Queue[UIEvent] = asyncio.Queue()
        bridge = QueueBridge(queue)
        await bridge.handle_event("provider:new_thing", {})
        await bridge.handle_event("provider:new_thing", {})  # repeat: no second notice
        await bridge.handle_event("plan:mystery", {})
        return _drain(queue)

    notices = _canary_notices(asyncio.run(run()))
    assert [notice.message for notice in notices] == [
        "unbridged event kind · provider:new_thing",
        "unbridged event kind · plan:mystery",
    ]
    assert all(notice.level == "debug" for notice in notices)


def test_consumed_kind_never_canaries() -> None:
    async def run() -> list[UIEvent]:
        queue: asyncio.Queue[UIEvent] = asyncio.Queue()
        bridge = QueueBridge(queue)
        await bridge.handle_event("tool:pre", {"tool_name": "bash", "tool_call_id": "t1"})
        return _drain(queue)

    events = asyncio.run(run())
    assert _canary_notices(events) == []
    assert [event.kind for event in events] == ["tool_pre"]


def test_register_canary_observes_published_and_contributed_names() -> None:
    async def run() -> tuple[FakeCoordinator, list[UIEvent], Any]:
        queue: asyncio.Queue[UIEvent] = asyncio.Queue()
        bridge = QueueBridge(queue)
        coordinator = FakeCoordinator(
            capability=["custom:module_event"],
            # Both contribution shapes hooks-logging accepts: str and list.
            contributions=[["delegate:new_kind"], "another:kind"],
        )
        unregister = await bridge.register_canary(coordinator)
        return coordinator, _drain(queue), unregister

    coordinator, _, _ = asyncio.run(run())
    registered = set(coordinator.hooks.handlers)
    # Core-published drift names are observed…
    assert "policy:violation" in registered  # in ALL_EVENTS, not consumed
    assert "artifact:write" in registered
    # …and so are module-contributed names, both shapes.
    assert {"custom:module_event", "delegate:new_kind", "another:kind"} <= registered
    # Consumed and deliberately-ignored kinds are exempt.
    assert not registered & set(CONSUMED_EVENTS)
    assert not registered & IGNORED_EVENTS


def test_canary_fires_once_per_kind_and_unregisters() -> None:
    async def run() -> tuple[list[UIEvent], dict[str, list[Any]]]:
        queue: asyncio.Queue[UIEvent] = asyncio.Queue()
        bridge = QueueBridge(queue)
        coordinator = FakeCoordinator()
        unregister = await bridge.register_canary(coordinator)
        await coordinator.hooks.emit("policy:violation", {"detail": "x"})
        await coordinator.hooks.emit("policy:violation", {"detail": "y"})
        await coordinator.hooks.emit("artifact:write", {})
        unregister()
        return _drain(queue), coordinator.hooks.handlers

    events, handlers = asyncio.run(run())
    notices = _canary_notices(events)
    assert [notice.message for notice in notices] == [
        "unbridged event kind · policy:violation",
        "unbridged event kind · artifact:write",
    ]
    assert all(not entries for entries in handlers.values())
