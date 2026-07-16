"""SessionSpawner tests: depth enforcement, tracker re-attachment,
cancellation linkage, approval/display inheritance, unwind-on-failure.

Fake sessions/coordinators throughout — no real amplifier-core session.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplifier_app_newtui.kernel.spawner import (
    DEPTH_CAPABILITY,
    SPAWN_CAPABILITY,
    SessionSpawner,
    generate_sub_session_id,
)


class FakeHooks:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register(
        self, event: str, handler: Any, *, priority: int = 0, name: str = ""
    ) -> Any:
        self.registered.append(event)
        return lambda: self.unregistered.append(event)


class FakeCancellation:
    def __init__(self) -> None:
        self.children: list[Any] = []

    def register_child(self, child: Any) -> None:
        self.children.append(child)

    def unregister_child(self, child: Any) -> None:
        self.children.remove(child)


class FakeCoordinator:
    def __init__(self) -> None:
        self.hooks = FakeHooks()
        self.cancellation = FakeCancellation()
        self.capabilities: dict[str, Any] = {}
        self.approval_system = object()
        self.display_system = None

    def get(self, name: str) -> Any:
        return self.hooks if name == "hooks" else None

    def get_capability(self, name: str) -> Any:
        return self.capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self.capabilities[name] = value


class FakeSession:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        session_id: str,
        parent_id: str | None = None,
        approval_system: Any = None,
        display_system: Any = None,
        fail_execute: bool = False,
    ) -> None:
        self.config = config
        self.session_id = session_id
        self.parent_id = parent_id
        self.approval_system = approval_system
        self.display_system = display_system
        self.coordinator = FakeCoordinator()
        self.coordinator.approval_system = approval_system
        self.coordinator.display_system = display_system
        self.initialized = False
        self.executed: list[str] = []
        self.cleaned_up = False
        self._fail_execute = fail_execute

    async def initialize(self) -> None:
        self.initialized = True

    async def execute(self, instruction: str) -> str:
        self.executed.append(instruction)
        if self._fail_execute:
            raise RuntimeError("boom")
        return f"done: {instruction}"

    async def cleanup(self) -> None:
        self.cleaned_up = True


class RecordingTracker:
    def __init__(self) -> None:
        self.attached: list[Any] = []
        self.detached = 0

    def register_hooks(self, hooks: Any, *, priority: int = 50) -> Any:
        self.attached.append(hooks)

        def unregister() -> None:
            self.detached += 1

        return unregister


class FakeDisplay:
    def __init__(self) -> None:
        self.nesting = 0
        self.max_nesting = 0

    def push_nesting(self) -> None:
        self.nesting += 1
        self.max_nesting = max(self.max_nesting, self.nesting)

    def pop_nesting(self) -> None:
        self.nesting -= 1


def make_parent(session_id: str = "sess-root") -> FakeSession:
    return FakeSession(config={"providers": ["anthropic"]}, session_id=session_id)


def make_spawner(**kwargs: Any) -> tuple[SessionSpawner, list[FakeSession]]:
    created: list[FakeSession] = []
    fail_execute = kwargs.pop("fail_execute", False)

    def factory(**factory_kwargs: Any) -> FakeSession:
        session = FakeSession(**factory_kwargs, fail_execute=fail_execute)
        created.append(session)
        return session

    spawner = SessionSpawner(session_factory=factory, **kwargs)
    return spawner, created


def test_generate_sub_session_id_is_hierarchical() -> None:
    child_id = generate_sub_session_id("sess-root", "test writer")
    assert child_id.startswith("sess-root-")
    assert child_id.endswith("_test-writer")


@pytest.mark.asyncio
async def test_spawn_executes_and_returns_result_dict() -> None:
    spawner, created = make_spawner()
    parent = make_parent()
    result = await spawner.spawn("scout", "find the bug", parent)
    assert result["status"] == "success"
    assert result["output"] == "done: find the bug"
    assert result["session_id"] == created[0].session_id
    assert result["parent_id"] == "sess-root"
    child = created[0]
    assert child.initialized
    assert child.cleaned_up
    assert child.parent_id == "sess-root"


@pytest.mark.asyncio
async def test_spawn_inherits_parent_approval_and_display() -> None:
    display = FakeDisplay()
    parent = make_parent()
    parent.coordinator.display_system = display
    spawner, created = make_spawner()
    await spawner.spawn("scout", "go", parent)
    child = created[0]
    assert child.approval_system is parent.coordinator.approval_system
    assert child.display_system is display
    assert display.max_nesting == 1
    assert display.nesting == 0  # popped on unwind


@pytest.mark.asyncio
async def test_spawn_prefers_injected_approval_display() -> None:
    approval, display = object(), FakeDisplay()
    spawner, created = make_spawner(approval_system=approval, display_system=display)
    await spawner.spawn("scout", "go", make_parent())
    assert created[0].approval_system is approval
    assert created[0].display_system is display


@pytest.mark.asyncio
async def test_spawn_reattaches_trackers_and_unwinds_them() -> None:
    tracker_a, tracker_b = RecordingTracker(), RecordingTracker()
    spawner, created = make_spawner(trackers=[tracker_a, tracker_b])
    await spawner.spawn("scout", "go", make_parent())
    child_hooks = created[0].coordinator.hooks
    assert tracker_a.attached == [child_hooks]
    assert tracker_b.attached == [child_hooks]
    assert tracker_a.detached == 1
    assert tracker_b.detached == 1


@pytest.mark.asyncio
async def test_spawn_links_and_unlinks_child_cancellation() -> None:
    linked: list[Any] = []
    spawner, created = make_spawner()
    parent = make_parent()
    original_register = parent.coordinator.cancellation.register_child

    def recording_register(child: Any) -> None:
        linked.append(child)
        original_register(child)

    parent.coordinator.cancellation.register_child = recording_register  # type: ignore[method-assign]
    await spawner.spawn("scout", "go", parent)
    assert linked == [created[0].coordinator.cancellation]
    assert parent.coordinator.cancellation.children == []  # unlinked in finally


@pytest.mark.asyncio
async def test_recursion_depth_enforced_default_two() -> None:
    spawner, created = make_spawner()
    parent = make_parent()
    first = await spawner.spawn("a", "level 1", parent)
    assert first["status"] == "success"
    child = created[0]
    assert child.coordinator.capabilities[DEPTH_CAPABILITY] == 1
    assert child.coordinator.capabilities[SPAWN_CAPABILITY] == spawner.spawn

    second = await spawner.spawn("b", "level 2", child)
    assert second["status"] == "success"
    grandchild = created[1]
    assert grandchild.coordinator.capabilities[DEPTH_CAPABILITY] == 2

    third = await spawner.spawn("c", "level 3", grandchild)
    assert third["status"] == "error"
    assert "recursion depth" in third["error"]
    assert len(created) == 2  # nothing was created for the refused spawn


@pytest.mark.asyncio
async def test_execute_failure_returns_error_and_still_unwinds() -> None:
    tracker = RecordingTracker()
    display = FakeDisplay()
    spawner, created = make_spawner(
        trackers=[tracker], display_system=display, fail_execute=True
    )
    parent = make_parent()
    result = await spawner.spawn("scout", "explode", parent)
    assert result["status"] == "error"
    assert "boom" in result["output"]
    assert created[0].cleaned_up
    assert tracker.detached == 1
    assert display.nesting == 0
    assert parent.coordinator.cancellation.children == []


@pytest.mark.asyncio
async def test_agent_overlay_merges_over_parent_config() -> None:
    spawner, created = make_spawner()
    parent = make_parent()
    await spawner.spawn(
        "scout",
        "go",
        parent,
        agent_configs={"scout": {"model": "fast", "extra": True}},
    )
    config = created[0].config
    assert config["providers"] == ["anthropic"]  # inherited
    assert config["model"] == "fast"  # overlay wins


def test_register_installs_spawn_capability() -> None:
    spawner, _ = make_spawner()
    coordinator = FakeCoordinator()
    spawner.register(coordinator)
    assert coordinator.capabilities[SPAWN_CAPABILITY] == spawner.spawn


def test_max_depth_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SessionSpawner(session_factory=lambda **kwargs: None, max_depth=0)
