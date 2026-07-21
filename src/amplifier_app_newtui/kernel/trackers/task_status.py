"""Task status tracker: agent lanes from ``task:agent_*`` / ``delegate:*`` events.

Hook-tracker pattern feeding a :class:`~model.lanes.LaneRegistry` — lanes
are keyed by ``session_id`` and routed by ``parent_id`` (the entire
routing key, stamped on every payload by ``hooks.set_default_fields``).

Race tolerance (RESEARCH-BRIEF risk 5): ``session:start`` can race
``task:agent_spawned``, and a grandchild's spawn event can arrive before
its parent's — registration is idempotent and the LaneRegistry
retro-patches depths when a missing parent appears. Legacy ``task:spawned``
/ ``task:completed`` names are adapted at the normalize boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from amplifier_core import HookResult

from ...model.lanes import LaneRecord, LaneRegistry
from ..events import AgentCompleted, AgentResumed, AgentSpawned, normalize

logger = logging.getLogger(__name__)

Listener = Callable[[], None]


class TaskStatusTracker:
    """Open/close agent lanes; pure state, listener-driven."""

    EVENTS = (
        "task:agent_spawned",
        "task:agent_completed",
        "task:spawned",
        "task:completed",
        "delegate:agent_spawned",
        "delegate:agent_completed",
        "delegate:agent_resumed",
        "delegate:agent_cancelled",
        "delegate:error",
        "session:start",
        "session:end",
    )

    def __init__(self, root_session_id: str, *, lanes: LaneRegistry | None = None) -> None:
        self.root_session_id = root_session_id
        self.lanes = lanes if lanes is not None else LaneRegistry()
        self._listeners: list[Listener] = []

    # -- state ---------------------------------------------------------------

    @property
    def active_count(self) -> int:
        """Drives ``N agent(s)`` in the working line and the coordinating title."""
        return self.lanes.active_count

    def lane(self, session_id: str) -> LaneRecord | None:
        return self.lanes.get(session_id)

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # -- hook plumbing ---------------------------------------------------------

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        self.consume(event, data)
        return HookResult(action="continue")

    def register_hooks(self, hooks: Any, *, priority: int = 50) -> Callable[[], None]:
        unregister_callbacks: list[Callable[..., object]] = []
        for event in self.EVENTS:
            unregister = hooks.register(
                event,
                self.handle_event,
                priority=priority,
                name=f"newtui-task-status-{event.replace(':', '-')}",
            )
            if callable(unregister):
                unregister_callbacks.append(unregister)

        def unregister_all() -> None:
            for unregister in reversed(unregister_callbacks):
                unregister()

        return unregister_all

    # -- consumption -----------------------------------------------------------

    def consume(self, event: str, data: dict[str, Any]) -> None:
        payload = data or {}
        if event in {"session:start", "session:end"}:
            self._consume_session(event, payload)
            return
        normalized = normalize(event, payload)
        if isinstance(normalized, AgentSpawned):
            child_id = normalized.sub_session_id or normalized.session_id
            if not child_id or child_id == self.root_session_id:
                return
            parent_id = (
                normalized.parent_session_id or normalized.session_id or self.root_session_id
            )
            if parent_id == child_id:
                parent_id = self.root_session_id
            self.lanes.register(
                child_id,
                parent_id=parent_id,
                name=normalized.agent or _agent_from_session_id(child_id),
                activity="running",
            )
            self._notify()
        elif isinstance(normalized, AgentCompleted):
            child_id = normalized.sub_session_id or normalized.session_id
            if not child_id or child_id == self.root_session_id:
                return
            if self.lanes.get(child_id) is None:
                # Completion raced ahead of the spawn event: open then close.
                self.lanes.register(
                    child_id,
                    parent_id=normalized.parent_session_id or self.root_session_id,
                    name=normalized.agent or _agent_from_session_id(child_id),
                )
            self.lanes.complete(
                child_id,
                result=normalized.result or ("" if normalized.success else "failed"),
            )
            self._notify()
        elif isinstance(normalized, AgentResumed):
            # delegate:agent_resumed carries only the child session_id (the
            # envelope's own field) + parent_session_id -- no `agent` name
            # (intentional, see AgentResumed docstring): the lane already
            # exists from the original spawn, keyed by this same id, so
            # reopening it needs nothing new to key on.
            child_id = normalized.session_id
            if not child_id or child_id == self.root_session_id:
                return
            self.lanes.register(
                child_id,
                parent_id=normalized.parent_session_id or self.root_session_id,
                name=normalized.agent or _agent_from_session_id(child_id),
                activity="running",
                reopen=True,
            )
            self._notify()

    def _consume_session(self, event: str, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "")
        parent_id = payload.get("parent_id")
        if not session_id or session_id == self.root_session_id:
            return
        if event == "session:start":
            if not parent_id:
                return  # a root session starting is not a lane
            self.lanes.register(
                session_id,
                parent_id=str(parent_id),
                name=_agent_from_session_id(session_id),
                activity="running",
            )
            self._notify()
            return
        record = self.lanes.get(session_id)
        if record is not None and record.lane.state != "done":
            self.lanes.complete(session_id)
            self._notify()

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                logger.debug("Task status listener failed", exc_info=True)


def _agent_from_session_id(session_id: str) -> str:
    """Hierarchical sub-session ids end ``_{agent_name}`` — recover it."""
    if "_" in session_id:
        return session_id.rsplit("_", 1)[-1]
    return "agent"


__all__ = ["TaskStatusTracker"]
