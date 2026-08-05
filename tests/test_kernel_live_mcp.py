"""Focused contract tests for session-local MCP reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest

from amplifier_app_tui.kernel.live_mcp import (
    MCP_RECONCILE_CAPABILITY,
    LiveMCPReconciler,
)


@dataclass
class _Wrapper:
    name: str
    server_name: str
    generation: str = "one"


class _Hooks:
    def __init__(self) -> None:
        self.register_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def register(self, *args: Any, **kwargs: Any) -> None:
        self.register_calls.append((args, kwargs))


class _Coordinator:
    def __init__(
        self,
        *,
        capabilities: Mapping[str, Any] | None = None,
        tools: Mapping[str, Any] | None = None,
    ) -> None:
        self.capabilities = dict(capabilities or {})
        self.tools = dict(tools or {})
        self.hooks = _Hooks()
        self.mount_calls: list[tuple[str, Any]] = []
        self.unmount_calls: list[str] = []
        self.fail_mount_wrapper: Any | None = None
        self.fail_unmount_name: str | None = None

    def get_capability(self, name: str) -> Any:
        return self.capabilities.get(name)

    def get(self, category: str) -> dict[str, Any]:
        assert category == "tools"
        return self.tools

    async def mount(self, category: str, wrapper: Any, *, name: str) -> None:
        assert category == "tools"
        self.mount_calls.append((name, wrapper))
        if wrapper is self.fail_mount_wrapper:
            raise RuntimeError(f"mount failed: {name}")
        if name in self.tools:
            raise RuntimeError(f"duplicate tool: {name}")
        self.tools[name] = wrapper

    async def unmount(self, category: str, *, name: str) -> None:
        assert category == "tools"
        self.unmount_calls.append(name)
        if name == self.fail_unmount_name:
            raise RuntimeError(f"unmount failed: {name}")
        self.tools.pop(name, None)


class _Manager:
    def __init__(
        self,
        wrappers: Mapping[str, Any],
        *,
        start_error: Exception | None = None,
        report_connected: bool = True,
    ) -> None:
        self.wrappers = dict(wrappers)
        self.start_error = start_error
        self.report_connected = report_connected
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.stopped = 0

    async def _start_server(self, name: str, spec: dict[str, Any]) -> None:
        self.started.append((name, deepcopy(spec)))
        if self.start_error is not None:
            raise self.start_error

    def get_server_names(self) -> list[str]:
        return [self.started[-1][0]] if self.started and self.report_connected else []

    def get_all_capabilities(self) -> dict[str, Any]:
        return dict(self.wrappers)

    async def stop(self) -> None:
        self.stopped += 1


class _ManagerFactory:
    def __init__(self, *managers: _Manager) -> None:
        self.managers = list(managers)
        self.calls: list[tuple[dict[str, Any], Any]] = []

    def __call__(self, config: dict[str, Any], coordinator: Any) -> _Manager:
        self.calls.append((deepcopy(config), coordinator))
        if not self.managers:
            raise AssertionError("unexpected manager construction")
        return self.managers.pop(0)


@pytest.mark.asyncio
async def test_upstream_reconcile_capability_is_preferred_and_reports_both_states() -> None:
    calls: list[dict[str, Any]] = []

    async def upstream(**kwargs: Any) -> dict[str, Any]:
        calls.append(deepcopy(kwargs))
        return {
            "ok": True,
            "connected": True,
            "changed": True,
            "tools": ["mcp_docs_search"],
            "message": "connected by upstream",
        }

    coordinator = _Coordinator(capabilities={MCP_RECONCILE_CAPABILITY: upstream})
    reconciler = LiveMCPReconciler(coordinator, enable_targeted_fallback=False)
    spec = {"command": "docs", "args": ["--stdio"]}

    result = await reconciler.add("docs", spec, configured=True)

    assert calls == [{"operation": "add", "server": "docs", "spec": spec}]
    assert result.ok is True
    assert result.configured is True
    assert result.connected is True
    assert result.backend == "upstream"
    assert result.tool_names == ("mcp_docs_search",)


@pytest.mark.asyncio
async def test_upstream_failure_does_not_confuse_saved_with_connected() -> None:
    async def upstream(**_kwargs: Any) -> None:
        raise RuntimeError("handshake refused")

    coordinator = _Coordinator(capabilities={MCP_RECONCILE_CAPABILITY: upstream})
    result = await LiveMCPReconciler(coordinator).add(
        "broken", {"url": "https://invalid.test"}, configured=True
    )

    assert result.ok is False
    assert result.configured is True
    assert result.connected is None
    assert result.supported is True
    assert "configuration saved" in result.message
    assert "handshake refused" in result.message


@pytest.mark.asyncio
async def test_upstream_must_return_explicit_connection_state() -> None:
    async def upstream(**_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "message": "done"}

    coordinator = _Coordinator(capabilities={MCP_RECONCILE_CAPABILITY: upstream})
    result = await LiveMCPReconciler(coordinator).reload("docs", {"command": "docs"})

    assert result.ok is False
    assert result.connected is None
    assert "no explicit connection state" in result.message


@pytest.mark.asyncio
async def test_targeted_add_connects_only_one_server_without_registering_hooks() -> None:
    wrapper = _Wrapper("mcp_docs_search", "docs")
    manager = _Manager({wrapper.name: wrapper})
    factory = _ManagerFactory(manager)
    coordinator = _Coordinator()
    reconciler = LiveMCPReconciler(coordinator, targeted_manager_factory=factory)
    spec = {"command": "docs", "args": ["--stdio"]}

    result = await reconciler.add("docs", spec, configured=True, previously_configured=False)

    assert result.ok is True
    assert result.connected is True
    assert result.backend == "targeted"
    assert result.tool_names == (wrapper.name,)
    assert coordinator.tools == {wrapper.name: wrapper}
    assert coordinator.hooks.register_calls == []
    assert manager.started == [("docs", spec)]
    assert factory.calls[0][0] == {
        "servers": {"docs": spec},
        "visibility": {"enabled": False},
    }
    assert reconciler.owned_servers == ("docs",)


@pytest.mark.asyncio
async def test_targeted_add_is_idempotent_for_same_owned_spec() -> None:
    wrapper = _Wrapper("mcp_docs_search", "docs")
    manager = _Manager({wrapper.name: wrapper})
    factory = _ManagerFactory(manager)
    coordinator = _Coordinator()
    reconciler = LiveMCPReconciler(coordinator, targeted_manager_factory=factory)
    spec = {"command": "docs"}

    first = await reconciler.add("docs", spec, previously_configured=False)
    second = await reconciler.add("docs", spec, previously_configured=True)

    assert first.changed is True
    assert second.ok is True
    assert second.changed is False
    assert len(factory.calls) == 1
    assert len(coordinator.mount_calls) == 1


@pytest.mark.asyncio
async def test_targeted_reload_replaces_owned_tools_and_stops_old_connection() -> None:
    old_wrapper = _Wrapper("mcp_docs_old", "docs", "old")
    new_wrapper = _Wrapper("mcp_docs_new", "docs", "new")
    old_manager = _Manager({old_wrapper.name: old_wrapper})
    new_manager = _Manager({new_wrapper.name: new_wrapper})
    coordinator = _Coordinator()
    reconciler = LiveMCPReconciler(
        coordinator, targeted_manager_factory=_ManagerFactory(old_manager, new_manager)
    )
    await reconciler.add("docs", {"command": "old"}, previously_configured=False)

    result = await reconciler.reload("docs", {"command": "new"})

    assert result.ok is True
    assert result.changed is True
    assert result.tool_names == (new_wrapper.name,)
    assert coordinator.tools == {new_wrapper.name: new_wrapper}
    assert old_manager.stopped == 1
    assert new_manager.stopped == 0


@pytest.mark.asyncio
async def test_targeted_add_mount_failure_rolls_back_every_tool_and_connection() -> None:
    first = _Wrapper("mcp_docs_first", "docs")
    second = _Wrapper("mcp_docs_second", "docs")
    manager = _Manager({first.name: first, second.name: second})
    coordinator = _Coordinator()
    coordinator.fail_mount_wrapper = second
    reconciler = LiveMCPReconciler(coordinator, targeted_manager_factory=_ManagerFactory(manager))

    result = await reconciler.add("docs", {"command": "docs"}, previously_configured=False)

    assert result.ok is False
    assert result.configured is True
    assert result.connected is False
    assert coordinator.tools == {}
    assert coordinator.unmount_calls == [first.name]
    assert manager.stopped == 1
    assert reconciler.owned_servers == ()
    assert "rolled back" in result.message


@pytest.mark.asyncio
async def test_targeted_reload_failure_restores_old_wrappers() -> None:
    old = _Wrapper("mcp_docs_search", "docs", "old")
    new = _Wrapper("mcp_docs_search", "docs", "new")
    old_manager = _Manager({old.name: old})
    new_manager = _Manager({new.name: new})
    coordinator = _Coordinator()
    reconciler = LiveMCPReconciler(
        coordinator, targeted_manager_factory=_ManagerFactory(old_manager, new_manager)
    )
    await reconciler.add("docs", {"command": "old"}, previously_configured=False)
    coordinator.fail_mount_wrapper = new

    result = await reconciler.reload("docs", {"command": "new"})

    assert result.ok is False
    assert result.connected is True
    assert result.changed is False
    assert coordinator.tools == {old.name: old}
    assert old_manager.stopped == 0
    assert new_manager.stopped == 1
    assert reconciler.owned_servers == ("docs",)
    assert "prior live state retained" in result.message


@pytest.mark.asyncio
async def test_targeted_collision_never_duplicates_an_existing_tool() -> None:
    existing = _Wrapper("mcp_docs_search", "boot")
    discovered = _Wrapper("mcp_docs_search", "docs")
    manager = _Manager({discovered.name: discovered})
    coordinator = _Coordinator(tools={existing.name: existing})
    reconciler = LiveMCPReconciler(coordinator, targeted_manager_factory=_ManagerFactory(manager))

    result = await reconciler.add("docs", {"command": "docs"}, previously_configured=False)

    assert result.ok is False
    assert coordinator.tools == {existing.name: existing}
    assert coordinator.mount_calls == []
    assert manager.stopped == 1
    assert "collide" in result.message


@pytest.mark.asyncio
async def test_remove_owned_is_live_and_then_idempotent() -> None:
    wrapper = _Wrapper("mcp_docs_search", "docs")
    manager = _Manager({wrapper.name: wrapper})
    coordinator = _Coordinator()
    reconciler = LiveMCPReconciler(coordinator, targeted_manager_factory=_ManagerFactory(manager))
    await reconciler.add("docs", {"command": "docs"}, previously_configured=False)

    first = await reconciler.remove("docs", configured=False)
    second = await reconciler.remove("docs", configured=False, previously_configured=False)

    assert first.ok is True
    assert first.connected is False
    assert first.changed is True
    assert manager.stopped == 1
    assert coordinator.tools == {}
    assert second.ok is True
    assert second.changed is False
    assert reconciler.owned_servers == ()


@pytest.mark.asyncio
async def test_boot_owned_server_is_never_unmounted_by_targeted_fallback() -> None:
    boot = _Wrapper("mcp_docs_search", "docs")
    coordinator = _Coordinator(tools={boot.name: boot})
    reconciler = LiveMCPReconciler(coordinator, enable_targeted_fallback=False)

    remove = await reconciler.remove("docs", configured=False)
    reload_result = await reconciler.reload("docs", {"command": "new"})

    assert remove.ok is False
    assert remove.supported is False
    assert remove.connected is True
    assert reload_result.ok is False
    assert reload_result.supported is False
    assert reload_result.connected is True
    assert coordinator.tools == {boot.name: boot}
    assert coordinator.unmount_calls == []


@pytest.mark.asyncio
async def test_targeted_add_requires_proof_server_was_not_in_boot_config() -> None:
    manager = _Manager({})
    factory = _ManagerFactory(manager)
    coordinator = _Coordinator()
    reconciler = LiveMCPReconciler(coordinator, targeted_manager_factory=factory)

    result = await reconciler.add("docs", {"command": "docs"})

    assert result.ok is False
    assert result.supported is False
    assert result.connected is None
    assert factory.calls == []
    assert "requires proof" in result.message


@pytest.mark.asyncio
async def test_unavailable_targeted_seam_reports_saved_but_not_connected() -> None:
    coordinator = _Coordinator()
    reconciler = LiveMCPReconciler(coordinator, enable_targeted_fallback=False)

    result = await reconciler.add("docs", {"command": "docs"}, previously_configured=False)

    assert result.ok is False
    assert result.configured is True
    assert result.connected is False
    assert result.supported is False
    assert result.backend == "none"
    assert "configuration saved" in result.message
    assert "no supported single-server reconcile seam" in result.message


@pytest.mark.asyncio
async def test_close_cleans_up_only_connections_owned_by_reconciler() -> None:
    boot = _Wrapper("mcp_boot_search", "boot")
    docs = _Wrapper("mcp_docs_search", "docs")
    files = _Wrapper("mcp_files_read", "files")
    docs_manager = _Manager({docs.name: docs})
    files_manager = _Manager({files.name: files})
    coordinator = _Coordinator(tools={boot.name: boot})
    reconciler = LiveMCPReconciler(
        coordinator,
        targeted_manager_factory=_ManagerFactory(docs_manager, files_manager),
    )
    await reconciler.add("docs", {"command": "docs"}, previously_configured=False)
    await reconciler.add("files", {"command": "files"}, previously_configured=False)

    results = await reconciler.close()

    assert all(result.ok for result in results)
    assert coordinator.tools == {boot.name: boot}
    assert docs_manager.stopped == 1
    assert files_manager.stopped == 1
    assert reconciler.owned_servers == ()
    assert coordinator.hooks.register_calls == []
