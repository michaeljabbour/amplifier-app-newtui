"""Safe, session-local reconciliation for MCP server connections.

The preferred contract is an upstream ``mcp.reconcile`` coordinator
capability.  Current pinned ``tool-mcp`` (0.2.2) does not expose one: its
public mount starts every configured server and owns one aggregate cleanup.
Remounting that module would duplicate connections and its visibility hook.

For a server proven new to the running session, this module can use the
pinned manager's *targeted* single-server seam in an isolated manager.  It
connects and discovers first, rejects tool-name collisions, then mounts the
discovered wrappers as one rollback-protected transaction.  No visibility
hook is registered.  The reconciler owns those connections and tools and can
therefore reload/remove them safely.  A server mounted by the boot-time
``tool-mcp`` instance is deliberately left alone unless upstream supplies the
reconcile capability; that instance does not publish per-server cleanup
handles, so pretending otherwise would be unsafe.

Callers persist configuration separately and pass that truth into each
operation.  Results always report configured state separately from live
connection state; ``connected=None`` means the old aggregate module does not
expose enough information to verify it.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

MCP_RECONCILE_CAPABILITY = "mcp.reconcile"
_SUPPORTED_TARGETED_TOOL_MCP_VERSIONS = frozenset({"0.2.2"})

Operation = Literal["add", "remove", "reload"]
ManagerFactory = Callable[[dict[str, Any], Any], Any]


@dataclass(frozen=True)
class MCPReconcileResult:
    """One configuration-to-runtime reconciliation outcome.

    ``configured`` describes the durable desired state supplied by the
    caller. ``connected`` describes the running session (or ``None`` when it
    cannot be observed safely).  They intentionally may disagree after a
    failure, which lets the UI say "saved, but not connected" instead of
    falsely reporting success.
    """

    ok: bool
    operation: Operation
    server: str
    configured: bool
    connected: bool | None
    changed: bool
    supported: bool
    backend: Literal["upstream", "targeted", "none"]
    tool_names: tuple[str, ...] = ()
    message: str = ""


@dataclass
class _OwnedServer:
    spec: dict[str, Any]
    manager: Any
    wrappers: dict[str, Any]


async def _maybe_await(value: Any) -> Any:
    return await value if isinstance(value, Awaitable) else value


def _configured_prefix(configured: bool) -> str:
    return "configuration saved" if configured else "configuration removed"


def _normalise_spec(spec: Mapping[str, Any] | None) -> dict[str, Any]:
    if spec is None:
        return {}
    return deepcopy(dict(spec))


def _tool_mapping(coordinator: Any) -> Mapping[str, Any]:
    try:
        tools = coordinator.get("tools")
    except Exception:  # noqa: BLE001 - observation must not break reconciliation
        return {}
    return tools if isinstance(tools, Mapping) else {}


def _server_tools(coordinator: Any, server: str) -> tuple[str, ...]:
    """Mounted wrappers that identify themselves as belonging to *server*."""
    return tuple(
        sorted(
            str(name)
            for name, wrapper in _tool_mapping(coordinator).items()
            if str(getattr(wrapper, "server_name", "")) == server
        )
    )


def _default_targeted_manager_factory() -> ManagerFactory | None:
    """Return the audited pinned manager class, never an unreviewed private API."""
    try:
        tool_mcp = importlib.import_module("amplifier_module_tool_mcp")
    except (ImportError, ModuleNotFoundError):
        return None

    if str(getattr(tool_mcp, "__version__", "")) not in (_SUPPORTED_TARGETED_TOOL_MCP_VERSIONS):
        return None
    manager = getattr(tool_mcp, "MCPManager", None)
    return manager if callable(manager) else None


class LiveMCPReconciler:
    """Reconcile MCP config changes into one running coordinator.

    ``previously_configured=False`` is required before the private targeted
    add fallback will run.  It is the caller's assertion that the server name
    was absent from the *effective* boot configuration, not merely one config
    file.  Existing/boot-loaded servers require upstream ``mcp.reconcile``.
    """

    def __init__(
        self,
        coordinator: Any,
        *,
        targeted_manager_factory: ManagerFactory | None = None,
        enable_targeted_fallback: bool = True,
    ) -> None:
        self._coordinator = coordinator
        self._factory_override = targeted_manager_factory
        self._enable_targeted_fallback = enable_targeted_fallback
        self._owned: dict[str, _OwnedServer] = {}
        self._stranded: list[_OwnedServer] = []
        self._lock = asyncio.Lock()

    @property
    def owned_servers(self) -> tuple[str, ...]:
        """Server names whose connection/tool cleanup belongs to this object."""
        return tuple(sorted(self._owned))

    async def add(
        self,
        server: str,
        spec: Mapping[str, Any],
        *,
        configured: bool = True,
        previously_configured: bool | None = None,
    ) -> MCPReconcileResult:
        """Connect a configured server, reloading an already-owned name."""
        return await self.reconcile(
            "add",
            server,
            spec,
            configured=configured,
            previously_configured=previously_configured,
        )

    async def reload(
        self,
        server: str,
        spec: Mapping[str, Any],
        *,
        configured: bool = True,
    ) -> MCPReconcileResult:
        """Replace one live server while retaining the old one on failure."""
        return await self.reconcile("reload", server, spec, configured=configured)

    async def remove(
        self,
        server: str,
        *,
        configured: bool = False,
        previously_configured: bool | None = None,
    ) -> MCPReconcileResult:
        """Disconnect a server when its per-server cleanup is owned/available."""
        return await self.reconcile(
            "remove",
            server,
            None,
            configured=configured,
            previously_configured=previously_configured,
        )

    async def reconcile(
        self,
        operation: Operation,
        server: str,
        spec: Mapping[str, Any] | None,
        *,
        configured: bool,
        previously_configured: bool | None = None,
    ) -> MCPReconcileResult:
        """Apply one add/remove/reload operation under a serialization lock."""
        name = str(server).strip()
        if not name:
            return MCPReconcileResult(
                ok=False,
                operation=operation,
                server=name,
                configured=configured,
                connected=False,
                changed=False,
                supported=True,
                backend="none",
                message=f"{_configured_prefix(configured)}; server name is empty",
            )
        if operation not in ("add", "remove", "reload"):
            raise ValueError(f"unsupported MCP reconciliation operation: {operation}")
        if operation != "remove" and not isinstance(spec, Mapping):
            return MCPReconcileResult(
                ok=False,
                operation=operation,
                server=name,
                configured=configured,
                connected=False,
                changed=False,
                supported=True,
                backend="none",
                message=f"{_configured_prefix(configured)}; server spec is missing",
            )

        desired = _normalise_spec(spec)
        async with self._lock:
            capability = self._upstream_capability()
            if capability is not None:
                return await self._call_upstream(capability, operation, name, desired, configured)
            if operation == "remove":
                return await self._remove_targeted(
                    name,
                    configured=configured,
                    previously_configured=previously_configured,
                )
            return await self._upsert_targeted(
                operation,
                name,
                desired,
                configured=configured,
                previously_configured=previously_configured,
            )

    async def close(self) -> tuple[MCPReconcileResult, ...]:
        """Unmount and stop only managers created by this reconciler."""
        results: list[MCPReconcileResult] = []
        async with self._lock:
            for name in tuple(self._owned):
                results.append(await self._remove_owned(name, configured=False, closing=True))
            for stranded in tuple(self._stranded):
                _removed, error = await self._unmount_names(tuple(reversed(stranded.wrappers)))
                if error is not None:
                    continue
                try:
                    await _maybe_await(stranded.manager.stop())
                except Exception:  # noqa: BLE001 - best-effort session cleanup
                    continue
                self._stranded.remove(stranded)
        return tuple(results)

    def _upstream_capability(self) -> Any | None:
        getter = getattr(self._coordinator, "get_capability", None)
        if not callable(getter):
            return None
        try:
            return getter(MCP_RECONCILE_CAPABILITY)
        except Exception:  # noqa: BLE001 - feature detection is fail-closed
            return None

    async def _call_upstream(
        self,
        capability: Any,
        operation: Operation,
        server: str,
        spec: dict[str, Any],
        configured: bool,
    ) -> MCPReconcileResult:
        target = getattr(capability, "reconcile", None)
        if not callable(target):
            target = capability if callable(capability) else getattr(capability, operation, None)
        if not callable(target):
            return self._unsupported(
                operation,
                server,
                configured,
                "mcp.reconcile is registered but is not callable",
            )
        try:
            raw = await _maybe_await(
                target(
                    operation=operation,
                    server=server,
                    spec=None if operation == "remove" else deepcopy(spec),
                )
            )
        except Exception as error:  # noqa: BLE001 - report saved-vs-live truth
            connected = bool(_server_tools(self._coordinator, server)) or None
            return MCPReconcileResult(
                ok=False,
                operation=operation,
                server=server,
                configured=configured,
                connected=connected,
                changed=False,
                supported=True,
                backend="upstream",
                tool_names=_server_tools(self._coordinator, server),
                message=(
                    f"{_configured_prefix(configured)}; live MCP reconciliation failed: {error}"
                ),
            )
        return self._normalise_upstream(raw, operation, server, configured)

    def _normalise_upstream(
        self,
        raw: Any,
        operation: Operation,
        server: str,
        configured: bool,
    ) -> MCPReconcileResult:
        if isinstance(raw, MCPReconcileResult):
            return MCPReconcileResult(
                ok=raw.ok,
                operation=operation,
                server=server,
                configured=configured,
                connected=raw.connected,
                changed=raw.changed,
                supported=raw.supported,
                backend="upstream",
                tool_names=raw.tool_names,
                message=raw.message,
            )
        if not isinstance(raw, Mapping) or "connected" not in raw:
            return MCPReconcileResult(
                ok=False,
                operation=operation,
                server=server,
                configured=configured,
                connected=None,
                changed=False,
                supported=True,
                backend="upstream",
                message=(
                    f"{_configured_prefix(configured)}; mcp.reconcile returned no explicit "
                    "connection state"
                ),
            )
        connected_raw = raw.get("connected")
        connected = connected_raw if isinstance(connected_raw, bool) else None
        supported = bool(raw.get("supported", True))
        expected = not connected if operation == "remove" else connected is True
        ok = bool(raw.get("ok", expected)) and connected is not None and supported
        names = raw.get("tool_names", raw.get("tools", ()))
        tool_names = (
            tuple(sorted(str(item) for item in names))
            if isinstance(names, (list, tuple, set, frozenset))
            else ()
        )
        message = str(raw.get("message", raw.get("detail", "")) or "")
        return MCPReconcileResult(
            ok=ok,
            operation=operation,
            server=server,
            configured=configured,
            connected=connected,
            changed=bool(raw.get("changed", ok)),
            supported=supported,
            backend="upstream",
            tool_names=tool_names,
            message=message,
        )

    def _manager_factory(self) -> ManagerFactory | None:
        if not self._enable_targeted_fallback:
            return None
        return self._factory_override or _default_targeted_manager_factory()

    async def _discover_targeted(
        self, server: str, spec: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        factory = self._manager_factory()
        if factory is None:
            raise RuntimeError(
                "the mounted tool-mcp exposes no supported single-server reconcile seam"
            )
        manager = factory(
            {"servers": {server: deepcopy(spec)}, "visibility": {"enabled": False}},
            self._coordinator,
        )
        start_one = getattr(manager, "_start_server", None)
        all_capabilities = getattr(manager, "get_all_capabilities", None)
        server_names = getattr(manager, "get_server_names", None)
        stop = getattr(manager, "stop", None)
        if (
            not callable(start_one)
            or not callable(all_capabilities)
            or not callable(server_names)
            or not callable(stop)
        ):
            raise RuntimeError("the targeted tool-mcp manager contract is incomplete")
        try:
            await _maybe_await(start_one(server, deepcopy(spec)))
            reported_names = server_names()
            if not isinstance(reported_names, (list, tuple, set, frozenset)) or server not in {
                str(name) for name in reported_names
            }:
                raise RuntimeError("server did not establish a live client")
            raw_wrappers = all_capabilities()
            if not isinstance(raw_wrappers, Mapping):
                raise RuntimeError("server capability discovery returned an invalid registry")
            wrappers = {str(name): wrapper for name, wrapper in raw_wrappers.items()}
            for name, wrapper in wrappers.items():
                wrapper_name = str(getattr(wrapper, "name", name))
                if wrapper_name != name:
                    raise RuntimeError(f"capability registry name mismatch: {name}")
                owner = getattr(wrapper, "server_name", server)
                if str(owner) != server:
                    raise RuntimeError(f"capability {name} belongs to another server")
            return manager, wrappers
        except BaseException:
            try:
                await _maybe_await(stop())
            except Exception:  # noqa: BLE001 - preserve the original discovery failure
                self._stranded.append(_OwnedServer(spec, manager, {}))
            raise

    async def _upsert_targeted(
        self,
        operation: Operation,
        server: str,
        spec: dict[str, Any],
        *,
        configured: bool,
        previously_configured: bool | None,
    ) -> MCPReconcileResult:
        old = self._owned.get(server)
        if old is not None and old.spec == spec:
            return MCPReconcileResult(
                ok=True,
                operation=operation,
                server=server,
                configured=configured,
                connected=True,
                changed=False,
                supported=True,
                backend="targeted",
                tool_names=tuple(sorted(old.wrappers)),
                message="already connected with the requested configuration",
            )
        if old is None:
            visible = _server_tools(self._coordinator, server)
            if visible:
                return self._unsupported(
                    operation,
                    server,
                    configured,
                    "server belongs to the boot-time tool-mcp instance; restart or use an "
                    "upstream mcp.reconcile provider",
                    connected=True,
                    tool_names=visible,
                )
            if operation == "reload" or previously_configured is not False:
                return self._unsupported(
                    operation,
                    server,
                    configured,
                    "targeted fallback requires proof that the server was absent from the "
                    "effective boot configuration",
                )

        try:
            manager, wrappers = await self._discover_targeted(server, spec)
        except Exception as error:  # noqa: BLE001 - saved config may outlive live failure
            return MCPReconcileResult(
                ok=False,
                operation=operation,
                server=server,
                configured=configured,
                connected=old is not None,
                changed=False,
                supported=self._manager_factory() is not None,
                backend="targeted" if self._manager_factory() is not None else "none",
                tool_names=tuple(sorted(old.wrappers)) if old else (),
                message=f"{_configured_prefix(configured)}; live MCP connection failed: {error}",
            )

        collisions = set(wrappers).intersection(_tool_mapping(self._coordinator))
        allowed = set(old.wrappers) if old else set()
        unexpected = tuple(sorted(collisions - allowed))
        if unexpected:
            await self._stop_discarded(manager)
            return MCPReconcileResult(
                ok=False,
                operation=operation,
                server=server,
                configured=configured,
                connected=old is not None,
                changed=False,
                supported=True,
                backend="targeted",
                tool_names=tuple(sorted(old.wrappers)) if old else (),
                message=(
                    f"{_configured_prefix(configured)}; live tools collide with existing "
                    f"mounts: {', '.join(unexpected)}"
                ),
            )

        if old is None:
            mounted, error = await self._mount_wrappers(wrappers)
            if error is not None:
                _removed, rollback_error = await self._unmount_names(tuple(reversed(mounted)))
                if rollback_error is None:
                    await self._stop_discarded(manager)
                    connected: bool | None = False
                    failure_detail = f"live MCP mount rolled back after failure: {error}"
                else:
                    self._stranded.append(_OwnedServer(spec, manager, wrappers))
                    connected = None
                    failure_detail = (
                        f"live MCP mount failed: {error}; rollback also failed: {rollback_error}"
                    )
                return MCPReconcileResult(
                    ok=False,
                    operation=operation,
                    server=server,
                    configured=configured,
                    connected=connected,
                    changed=False,
                    supported=True,
                    backend="targeted",
                    tool_names=_server_tools(self._coordinator, server),
                    message=f"{_configured_prefix(configured)}; {failure_detail}",
                )
            self._owned[server] = _OwnedServer(spec, manager, wrappers)
            return MCPReconcileResult(
                ok=True,
                operation=operation,
                server=server,
                configured=configured,
                connected=True,
                changed=True,
                supported=True,
                backend="targeted",
                tool_names=tuple(sorted(wrappers)),
                message=f"connected live · {len(wrappers)} capability(s) mounted",
            )

        return await self._replace_owned(
            operation,
            server,
            spec,
            manager,
            wrappers,
            old,
            configured=configured,
        )

    async def _replace_owned(
        self,
        operation: Operation,
        server: str,
        spec: dict[str, Any],
        manager: Any,
        wrappers: dict[str, Any],
        old: _OwnedServer,
        *,
        configured: bool,
    ) -> MCPReconcileResult:
        removed, remove_error = await self._unmount_names(tuple(reversed(old.wrappers)))
        if remove_error is not None:
            rollback_error = await self._remount_subset(old.wrappers, removed)
            await self._stop_discarded(manager)
            return self._rollback_failure(
                operation,
                server,
                configured,
                old,
                f"could not unmount old tools: {remove_error}",
                rollback_error,
            )

        mounted, mount_error = await self._mount_wrappers(wrappers)
        if mount_error is not None:
            _removed, new_cleanup_error = await self._unmount_names(tuple(reversed(mounted)))
            old_missing = tuple(
                name for name in old.wrappers if name not in _tool_mapping(self._coordinator)
            )
            rollback_error = await self._remount_subset(old.wrappers, old_missing)
            if new_cleanup_error is None:
                await self._stop_discarded(manager)
            else:
                self._stranded.append(_OwnedServer(spec, manager, wrappers))
                rollback_error = rollback_error or new_cleanup_error
            return self._rollback_failure(
                operation,
                server,
                configured,
                old,
                f"could not mount replacement tools: {mount_error}",
                rollback_error,
            )

        self._owned[server] = _OwnedServer(spec, manager, wrappers)
        try:
            await _maybe_await(old.manager.stop())
        except Exception:  # noqa: BLE001 - new live state is valid; retain cleanup ownership
            self._stranded.append(_OwnedServer(old.spec, old.manager, {}))
            cleanup_note = " · previous connection cleanup deferred"
        else:
            cleanup_note = ""
        return MCPReconcileResult(
            ok=True,
            operation=operation,
            server=server,
            configured=configured,
            connected=True,
            changed=True,
            supported=True,
            backend="targeted",
            tool_names=tuple(sorted(wrappers)),
            message=f"reloaded live · {len(wrappers)} capability(s) mounted{cleanup_note}",
        )

    async def _remove_targeted(
        self,
        server: str,
        *,
        configured: bool,
        previously_configured: bool | None,
    ) -> MCPReconcileResult:
        if server in self._owned:
            return await self._remove_owned(server, configured=configured)
        visible = _server_tools(self._coordinator, server)
        if visible:
            return self._unsupported(
                "remove",
                server,
                configured,
                "server belongs to the boot-time tool-mcp instance; its per-server cleanup "
                "is not exposed",
                connected=True,
                tool_names=visible,
            )
        if previously_configured is False:
            return MCPReconcileResult(
                ok=True,
                operation="remove",
                server=server,
                configured=configured,
                connected=False,
                changed=False,
                supported=True,
                backend="targeted",
                message="already absent from the live session",
            )
        return self._unsupported(
            "remove",
            server,
            configured,
            "connection ownership is unavailable; live disconnection cannot be verified",
        )

    async def _remove_owned(
        self, server: str, *, configured: bool, closing: bool = False
    ) -> MCPReconcileResult:
        owned = self._owned[server]
        removed, error = await self._unmount_names(tuple(reversed(owned.wrappers)))
        if error is not None:
            rollback_error = await self._remount_subset(owned.wrappers, removed)
            return self._rollback_failure(
                "remove",
                server,
                configured,
                owned,
                f"could not unmount live tools: {error}",
                rollback_error,
            )
        try:
            await _maybe_await(owned.manager.stop())
        except Exception as error:  # noqa: BLE001 - preserve ownership for a retry
            rollback_error = await self._remount_subset(owned.wrappers, tuple(owned.wrappers))
            return self._rollback_failure(
                "remove",
                server,
                configured,
                owned,
                f"could not stop live connection: {error}",
                rollback_error,
            )
        del self._owned[server]
        return MCPReconcileResult(
            ok=True,
            operation="remove",
            server=server,
            configured=configured,
            connected=False,
            changed=True,
            supported=True,
            backend="targeted",
            message="session cleanup complete" if closing else "disconnected live",
        )

    async def _mount_wrappers(
        self, wrappers: Mapping[str, Any]
    ) -> tuple[tuple[str, ...], Exception | None]:
        mounted: list[str] = []
        for name, wrapper in wrappers.items():
            try:
                await _maybe_await(self._coordinator.mount("tools", wrapper, name=str(name)))
            except Exception as error:  # noqa: BLE001 - caller rolls back transaction
                return tuple(mounted), error
            mounted.append(str(name))
        return tuple(mounted), None

    async def _unmount_names(
        self, names: tuple[str, ...]
    ) -> tuple[tuple[str, ...], Exception | None]:
        removed: list[str] = []
        for name in names:
            try:
                await _maybe_await(self._coordinator.unmount("tools", name=name))
            except Exception as error:  # noqa: BLE001 - caller rolls back transaction
                return tuple(removed), error
            removed.append(name)
        return tuple(removed), None

    async def _remount_subset(
        self, wrappers: Mapping[str, Any], names: tuple[str, ...]
    ) -> Exception | None:
        first_error: Exception | None = None
        for name in names:
            wrapper = wrappers.get(name)
            if wrapper is None:
                continue
            try:
                await _maybe_await(self._coordinator.mount("tools", wrapper, name=name))
            except Exception as error:  # noqa: BLE001 - collect first rollback failure
                first_error = first_error or error
        return first_error

    async def _stop_discarded(self, manager: Any) -> None:
        try:
            await _maybe_await(manager.stop())
        except Exception:  # noqa: BLE001 - retain ownership for close retry
            self._stranded.append(_OwnedServer({}, manager, {}))

    def _rollback_failure(
        self,
        operation: Operation,
        server: str,
        configured: bool,
        old: _OwnedServer,
        reason: str,
        rollback_error: Exception | None,
    ) -> MCPReconcileResult:
        if rollback_error is None:
            message = f"{_configured_prefix(configured)}; {reason}; prior live state retained"
            connected: bool | None = True
        else:
            message = (
                f"{_configured_prefix(configured)}; {reason}; rollback also failed: "
                f"{rollback_error}"
            )
            connected = None
        return MCPReconcileResult(
            ok=False,
            operation=operation,
            server=server,
            configured=configured,
            connected=connected,
            changed=False,
            supported=True,
            backend="targeted",
            tool_names=tuple(sorted(old.wrappers)),
            message=message,
        )

    def _unsupported(
        self,
        operation: Operation,
        server: str,
        configured: bool,
        reason: str,
        *,
        connected: bool | None = None,
        tool_names: tuple[str, ...] = (),
    ) -> MCPReconcileResult:
        return MCPReconcileResult(
            ok=False,
            operation=operation,
            server=server,
            configured=configured,
            connected=connected,
            changed=False,
            supported=False,
            backend="none",
            tool_names=tool_names,
            message=f"{_configured_prefix(configured)}; live MCP change unsupported: {reason}",
        )


__all__ = [
    "MCP_RECONCILE_CAPABILITY",
    "LiveMCPReconciler",
    "MCPReconcileResult",
]
