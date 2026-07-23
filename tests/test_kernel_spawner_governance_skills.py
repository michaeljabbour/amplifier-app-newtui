"""Issue #38 — child sessions inherit the root's trust-posture gating and
runtime skill overlays through the SessionSpawner seam.

Before this change spawned children bypassed the TUI's own GovernanceHook
(native approval inheritance applied, but the app posture — careful/plan —
never reached lanes) and never saw skills the user loaded at runtime.

These are integration tests THROUGH ``SessionSpawner``: a real
:class:`GovernanceHook` is attached via ``set_governance_hook`` and the fake
child emits a governed ``tool:pre`` during ``execute`` so the lane is gated
by the same live posture as the root. Fakes throughout — no amplifier-core
session — but the hook bus really runs the registered handlers.
"""

from __future__ import annotations

from typing import Any

import pytest
from amplifier_foundation import RUNTIME_SKILL_OVERLAY_CAPABILITY

from amplifier_app_newtui.kernel.governance_hook import GovernanceHook
from amplifier_app_newtui.kernel.spawner import SessionSpawner
from amplifier_app_newtui.model.queues import NeedsYouQueue
from amplifier_app_newtui.model.trust import DenialLog

WRITE_PROBE = ("write_file", {"file_path": "/repo/a.py"})
READ_PROBE = ("read_file", {"path": "/repo/a.py"})


class HookBus:
    """A minimal real hook bus: register handlers, emit runs them by
    descending priority and returns each handler's HookResult."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[tuple[int, Any]]] = {}

    def register(self, event: str, handler: Any, *, priority: int = 0, name: str = "") -> Any:
        bucket = self.handlers.setdefault(event, [])
        bucket.append((priority, handler))
        bucket.sort(key=lambda item: -item[0])

        def unregister() -> None:
            self.handlers[event] = [(p, h) for (p, h) in self.handlers.get(event, ()) if h is not handler]

        return unregister

    async def emit(self, event: str, data: dict[str, Any]) -> list[Any]:
        return [await handler(event, data) for _, handler in list(self.handlers.get(event, ()))]

    def registered(self, event: str) -> int:
        return len(self.handlers.get(event, ()))


class FakeCancellation:
    def __init__(self) -> None:
        self.children: list[Any] = []

    def register_child(self, child: Any) -> None:
        self.children.append(child)

    def unregister_child(self, child: Any) -> None:
        self.children.remove(child)


class FakeContext:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def add_message(self, message: dict[str, Any]) -> None:
        self.messages.append(dict(message))

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self.messages = [dict(m) for m in messages]


class FakeCoordinator:
    def __init__(self) -> None:
        self.hooks = HookBus()
        self.cancellation = FakeCancellation()
        self.capabilities: dict[str, Any] = {}
        self.approval_system = object()
        self.display_system: Any = None
        self.context = FakeContext()
        self.mounts: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        if name == "hooks":
            return self.hooks
        if name == "context":
            return self.context
        return self.mounts.get(name)

    async def mount(self, mount_point: str, module: Any, name: str | None = None) -> None:
        del name
        self.mounts[mount_point] = module

    def get_capability(self, name: str) -> Any:
        return self.capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self.capabilities[name] = value


class FakeSession:
    """A fake child that emits one governed ``tool:pre`` during execute so
    the lane's gating verdict is observable after the spawn returns."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        session_id: str,
        parent_id: str | None = None,
        approval_system: Any = None,
        display_system: Any = None,
        probe: tuple[str, dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.session_id = session_id
        self.parent_id = parent_id
        self.coordinator = FakeCoordinator()
        self.coordinator.approval_system = approval_system
        self.coordinator.display_system = display_system
        self._probe = probe
        self.probe_results: list[Any] = []

    async def initialize(self) -> None:
        return None

    async def execute(self, instruction: str) -> str:
        if self._probe is not None:
            tool_name, tool_input = self._probe
            self.probe_results = await self.coordinator.hooks.emit(
                "tool:pre",
                {
                    "session_id": self.session_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_call_id": "call-child",
                },
            )
        return f"done: {instruction}"

    async def cleanup(self) -> None:
        return None


def make_parent(session_id: str = "sess-root") -> FakeSession:
    return FakeSession(config={"providers": ["anthropic"]}, session_id=session_id)


def make_governance(mode: str, denial_log: DenialLog, needs_you: NeedsYouQueue) -> GovernanceHook:
    return GovernanceHook(
        "sess-root",
        mode=lambda: mode,
        denial_log=denial_log,
        needs_you=needs_you,
    )


def make_spawner(
    probe: tuple[str, dict[str, Any]] | None = WRITE_PROBE, **kwargs: Any
) -> tuple[SessionSpawner, list[FakeSession]]:
    created: list[FakeSession] = []

    def factory(**factory_kwargs: Any) -> FakeSession:
        session = FakeSession(**factory_kwargs, probe=probe)
        created.append(session)
        return session

    return SessionSpawner(session_factory=factory, **kwargs), created


# -- child governance ---------------------------------------------------------


def _actions(results: list[Any]) -> list[str]:
    return [getattr(r, "action", "") for r in results if r is not None]


@pytest.mark.asyncio
async def test_gated_posture_blocks_same_action_in_lane_as_root() -> None:
    """Acceptance: a gated posture (plan) blocks the SAME action in a lane
    as in the root, driven through SessionSpawner."""
    denial_log = DenialLog()
    needs_you = NeedsYouQueue()
    governance = make_governance("plan", denial_log, needs_you)

    # Root: plan denies a write.
    root_hooks = HookBus()
    governance.register_hooks(root_hooks)
    root_results = await root_hooks.emit("tool:pre", {"tool_name": "write_file", "tool_input": {"file_path": "/repo/a.py"}})
    assert _actions(root_results) == ["deny"]

    # Lane: the same posture reaches the child through the spawner.
    spawner, created = make_spawner()
    spawner.set_governance_hook(governance)
    result = await spawner.spawn("scout", "edit the file", make_parent())

    assert result["status"] == "success"
    assert _actions(created[0].probe_results) == ["deny"]
    # Deny counted on the shared log for BOTH root and lane.
    assert denial_log.total_count == 2


@pytest.mark.asyncio
async def test_child_write_ungated_without_governance_hook() -> None:
    """Regression guard for the pre-fix behavior: with no governance hook
    attached the lane is ungated — the child's write is never denied."""
    spawner, created = make_spawner()  # no set_governance_hook
    await spawner.spawn("scout", "edit the file", make_parent())
    assert _actions(created[0].probe_results) == []  # no gate fired


@pytest.mark.asyncio
async def test_child_read_allowed_under_plan_posture() -> None:
    """Same posture, benign action: plan allows a read in the lane exactly
    as it would in the root (continue, no denial)."""
    denial_log = DenialLog()
    governance = make_governance("plan", denial_log, NeedsYouQueue())
    spawner, created = make_spawner(probe=READ_PROBE)
    spawner.set_governance_hook(governance)
    await spawner.spawn("scout", "read the file", make_parent())
    assert _actions(created[0].probe_results) == ["continue"]
    assert denial_log.total_count == 0


@pytest.mark.asyncio
async def test_governance_detached_from_child_after_unwind() -> None:
    """The governance hook is torn down with the lane — the child hook bus
    holds no governance handlers once the spawn returns."""
    governance = make_governance("plan", DenialLog(), NeedsYouQueue())
    spawner, created = make_spawner()
    spawner.set_governance_hook(governance)
    await spawner.spawn("scout", "go", make_parent())
    child_hooks = created[0].coordinator.hooks
    assert child_hooks.registered("tool:pre") == 0
    assert child_hooks.registered("prompt:submit") == 0


# -- runtime skill overlays ---------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_skill_overlay_propagates_to_child() -> None:
    """Skills loaded at runtime (root's runtime_skill_overlay capability)
    are visible to the child after spawn."""
    spawner, created = make_spawner(probe=None)
    parent = make_parent()
    parent.coordinator.register_capability(
        RUNTIME_SKILL_OVERLAY_CAPABILITY, ["skill-alpha", "skill-beta"]
    )
    await spawner.spawn("scout", "go", parent)
    assert created[0].coordinator.get_capability(RUNTIME_SKILL_OVERLAY_CAPABILITY) == [
        "skill-alpha",
        "skill-beta",
    ]


@pytest.mark.asyncio
async def test_runtime_skill_overlay_is_copied_not_shared() -> None:
    """The child gets a copy — mutating the parent's list after the spawn
    never leaks into the child's overlay."""
    spawner, created = make_spawner(probe=None)
    parent = make_parent()
    overlay = ["skill-alpha"]
    parent.coordinator.register_capability(RUNTIME_SKILL_OVERLAY_CAPABILITY, overlay)
    await spawner.spawn("scout", "go", parent)
    overlay.append("skill-mutated-later")
    assert created[0].coordinator.get_capability(RUNTIME_SKILL_OVERLAY_CAPABILITY) == ["skill-alpha"]


@pytest.mark.asyncio
async def test_no_skill_overlay_registers_nothing_on_child() -> None:
    """No runtime overlay on the parent → the child stays clean (no empty
    capability planted)."""
    spawner, created = make_spawner(probe=None)
    await spawner.spawn("scout", "go", make_parent())
    assert created[0].coordinator.get_capability(RUNTIME_SKILL_OVERLAY_CAPABILITY) is None
