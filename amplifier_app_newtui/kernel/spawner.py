"""In-process subagent spawner (ADR-0007 resolution 7 — v1 is in-process only).

Wraps the ``session.spawn`` capability. On every spawn it:

1. enforces recursion depth (default 2) BEFORE creating anything — the
   kernel documents but does not implement depth limiting;
2. creates the child session with the parent's approval/display systems
   (ephemeral hooks do NOT propagate to children — inheritance must be
   explicit);
3. re-attaches the shared tracker set to the child coordinator's hooks so
   lanes/telemetry stay lit (the "subagent lanes going dark" risk);
4. registers the child's cancellation with the parent's so esc-interrupt
   reaches the whole tree;
5. registers itself on the child so grandchildren spawn through the same
   depth-enforced path;
6. always unwinds (tracker unregistration, cancellation unlink, cleanup)
   in ``finally``.

Everything is duck-typed against the amplifier-core session surface
(``.coordinator``, ``.initialize()``, ``.execute()``, ``.cleanup()``) so
tests drive it with fakes; the default factory imports amplifier-core
lazily. Reference: amplifier-app-cli ``session_spawner.py`` +
``runtime/session_spawn_inprocess.py``.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Sequence
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SPAWN_CAPABILITY = "session.spawn"
DEPTH_CAPABILITY = "newtui.spawn_depth"
DEFAULT_MAX_DEPTH = 2


class Tracker(Protocol):
    """The shared hook-tracker surface the spawner re-attaches to children."""

    def register_hooks(self, hooks: Any, *, priority: int = ...) -> Callable[[], None]: ...


def _default_session_factory(**kwargs: Any) -> Any:
    from amplifier_core import AmplifierSession

    return AmplifierSession(**kwargs)


def generate_sub_session_id(parent_id: str, agent_name: str) -> str:
    """Hierarchical child id: ``{parent}-{16hex}_{agent_name}``."""
    clean_agent = "-".join(str(agent_name or "agent").split()) or "agent"
    return f"{parent_id}-{secrets.token_hex(8)}_{clean_agent}"


class SessionSpawner:
    """The app's ``session.spawn`` capability implementation."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any] | None = None,
        trackers: Sequence[Tracker] = (),
        approval_system: Any | None = None,
        display_system: Any | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        id_generator: Callable[[str, str], str] = generate_sub_session_id,
    ) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        self._session_factory = session_factory or _default_session_factory
        self._trackers = tuple(trackers)
        self._approval_system = approval_system
        self._display_system = display_system
        self._max_depth = max_depth
        self._id_generator = id_generator

    def register(self, coordinator: Any) -> None:
        """Install this spawner as the coordinator's ``session.spawn``
        capability — MUST run after ``create_session`` and before
        ``execute`` (integration-guide timing contract)."""
        coordinator.register_capability(SPAWN_CAPABILITY, self.spawn)

    async def spawn(
        self,
        agent_name: str,
        instruction: str,
        parent_session: Any,
        agent_configs: dict[str, dict[str, Any]] | None = None,
        sub_session_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Spawn, execute, persist-nothing, and unwind one child session.

        Returns the tool-facing result dict ``{output, session_id, status}``;
        depth violations return ``status="error"`` without spawning
        (deny-and-continue — the orchestrator turns it into a tool result).
        """
        parent_coordinator = parent_session.coordinator
        depth = _current_depth(parent_coordinator) + 1
        if depth > self._max_depth:
            reason = (
                f"agent recursion depth {depth} exceeds the limit of "
                f"{self._max_depth}; complete this work directly instead of delegating"
            )
            logger.warning("Refused spawn of %s: %s", agent_name, reason)
            return {
                "output": reason,
                "session_id": "",
                "status": "error",
                "error": reason,
            }

        child_id = sub_session_id or self._id_generator(
            str(parent_session.session_id), agent_name
        )
        config = _merged_config(parent_session, agent_configs or {}, agent_name)
        approval_system = self._approval_system or getattr(
            parent_coordinator, "approval_system", None
        )
        display_system = self._display_system or getattr(
            parent_coordinator, "display_system", None
        )
        child = self._session_factory(
            config=config,
            session_id=child_id,
            parent_id=parent_session.session_id,
            approval_system=approval_system,
            display_system=display_system,
        )
        await child.initialize()

        child_coordinator = child.coordinator
        unregisters: list[Callable[[], None]] = []
        hooks = child_coordinator.get("hooks")
        if hooks is not None:
            for tracker in self._trackers:
                unregisters.append(tracker.register_hooks(hooks))
        child_coordinator.register_capability(DEPTH_CAPABILITY, depth)
        child_coordinator.register_capability(SPAWN_CAPABILITY, self.spawn)

        parent_cancellation = getattr(parent_coordinator, "cancellation", None)
        child_cancellation = getattr(child_coordinator, "cancellation", None)
        cancellation_linked = False
        if parent_cancellation is not None and child_cancellation is not None:
            parent_cancellation.register_child(child_cancellation)
            cancellation_linked = True

        if display_system is not None and hasattr(display_system, "push_nesting"):
            display_system.push_nesting()

        try:
            output = await child.execute(instruction)
            status = "success"
        except Exception as error:
            logger.debug("Child session %s failed", child_id, exc_info=True)
            output = f"agent failed: {error}"
            status = "error"
        finally:
            for unregister in reversed(unregisters):
                try:
                    unregister()
                except Exception:
                    logger.debug("Tracker unregister failed", exc_info=True)
            if cancellation_linked:
                try:
                    parent_cancellation.unregister_child(child_cancellation)
                except Exception:
                    logger.debug("Cancellation unlink failed", exc_info=True)
            if display_system is not None and hasattr(display_system, "pop_nesting"):
                display_system.pop_nesting()
            try:
                await child.cleanup()
            except Exception:
                logger.debug("Child cleanup failed", exc_info=True)

        return {
            "output": output,
            "session_id": child_id,
            "status": status,
            "parent_id": str(parent_session.session_id),
        }


def _current_depth(coordinator: Any) -> int:
    get_capability = getattr(coordinator, "get_capability", None)
    if not callable(get_capability):
        return 0
    try:
        depth = get_capability(DEPTH_CAPABILITY)
    except Exception:
        return 0
    return depth if isinstance(depth, int) and depth >= 0 else 0


def _merged_config(
    parent_session: Any,
    agent_configs: dict[str, dict[str, Any]],
    agent_name: str,
) -> dict[str, Any]:
    """Shallow parent-config + agent-overlay merge (overlay wins)."""
    parent_config = getattr(parent_session, "config", None)
    merged: dict[str, Any] = dict(parent_config) if isinstance(parent_config, dict) else {}
    overlay = agent_configs.get(agent_name)
    if isinstance(overlay, dict):
        merged.update(overlay)
    return merged


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEPTH_CAPABILITY",
    "SPAWN_CAPABILITY",
    "SessionSpawner",
    "generate_sub_session_id",
]
