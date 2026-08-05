"""In-session overlay composition (``kernel/bundle_compose.py``).

Duck-typed over the coordinator's ``loader.load`` seam — the same contract
``AmplifierSession.initialize()`` drives per module — so the mount logic tests
with a plain fake, no real session. Covers: additive sections mount, per-module
failure is best-effort (never aborts the overlay), non-composable sections are
reported (honest boundary), and cleanups are collected for teardown.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from amplifier_app_tui.kernel.bundle_compose import (
    COMPOSABLE_SECTIONS,
    additive_module_section,
    boot_module_identities,
    module_identities,
    mount_additive_module,
    mount_overlay_modules,
)


class _FakeProvider:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.priority = config.get("priority", 100)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeLoader:
    """A ModuleLoader stand-in: ``load`` returns an async mount fn.

    Records every mounted module id and hands back a cleanup callable so the
    caller can prove teardown handles are collected. ``fail`` names module ids
    whose mount raises (per-module failure path)."""

    def __init__(
        self,
        fail: set[str] | None = None,
        *,
        ready: dict[str, Any] | None = None,
        ready_ids: dict[str, str] | None = None,
        provider_no_mount: set[str] | None = None,
        provider_cleanup_unmount_default: set[str] | None = None,
        provider_raise_after_mount: set[str] | None = None,
        provider_honors_config_name: set[str] | None = None,
        provider_extra_mount_name: dict[str, str] | None = None,
    ) -> None:
        self.loaded: list[str] = []
        self.cleaned: list[str] = []
        self._fail = fail or set()
        self._ready = ready or {}
        self._ready_ids = ready_ids or {}
        self._provider_no_mount = provider_no_mount or set()
        self._provider_cleanup_unmount_default = provider_cleanup_unmount_default or set()
        self._provider_raise_after_mount = provider_raise_after_mount or set()
        self._provider_honors_config_name = provider_honors_config_name or set()
        self._provider_extra_mount_name = provider_extra_mount_name or {}
        self.providers: list[_FakeProvider] = []

    def load(self, module_id, config=None, source_hint=None, coordinator=None):  # noqa: ANN001
        del source_hint, coordinator

        async def _mount(coord):  # noqa: ANN001
            if module_id in self._fail:
                raise RuntimeError(f"boom: {module_id}")
            self.loaded.append(module_id)
            canonical = str(module_id).removeprefix("amplifier-module-")
            if canonical.startswith("provider-") and module_id not in self._provider_no_mount:
                name = canonical.removeprefix("provider-")
                provider_config = dict(config or {})
                if module_id in self._provider_honors_config_name:
                    name = str(provider_config.get("name") or name)
                provider = _FakeProvider(provider_config)
                self.providers.append(provider)
                await coord.mount("providers", provider, name=name)
                extra_name = self._provider_extra_mount_name.get(module_id)
                if extra_name:
                    await coord.mount("providers", provider, name=extra_name)
                if module_id in self._provider_raise_after_mount:
                    raise RuntimeError(f"raised after mounting: {module_id}")
                if module_id in self._provider_cleanup_unmount_default:

                    async def cleanup_provider() -> None:
                        self.cleaned.append(module_id)
                        await provider.close()
                        await coord.unmount("providers", name=name)

                    return cleanup_provider

                async def cleanup_provider() -> None:
                    self.cleaned.append(module_id)
                    await provider.close()

                return cleanup_provider
            return lambda: self.cleaned.append(module_id)

        ready_callback = self._ready.get(module_id)
        if ready_callback is not None:
            _mount.__on_session_ready__ = (
                self._ready_ids.get(module_id, module_id),
                ready_callback,
            )
        return _mount


class _FakeCoordinator:
    def __init__(self, loader: _FakeLoader | None) -> None:
        self.loader = loader
        self.config: dict = {}
        self.mount_points: dict[str, dict[str, Any]] = {
            "providers": {},
            "tools": {},
        }
        self.hooks = SimpleNamespace(emit=self._emit)
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    def get(self, mount: str):  # noqa: ANN201
        return self.mount_points.get(mount)

    async def mount(self, mount: str, value: Any, *, name: str) -> None:
        self.mount_points.setdefault(mount, {})[name] = value

    async def unmount(self, mount: str, *, name: str) -> None:
        self.mount_points.setdefault(mount, {}).pop(name, None)

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        self.emitted.append((event, payload))


def _plan() -> dict:
    return {
        "tools": [{"module": "tool-x", "config": {"a": 1}}, {"module": "tool-y"}],
        "hooks": [{"module": "hook-z", "source": "git+..."}],
        "agents": [{"module": "agent-q"}],
        "providers": [{"module": "provider-anthropic"}],
    }


@pytest.mark.asyncio
async def test_mounts_additive_sections_only() -> None:
    loader = _FakeLoader()
    result = await mount_overlay_modules(_FakeCoordinator(loader), _plan())
    assert result.ok is True
    # Named providers are additive coordinator mounts, just like tools. The
    # current root provider does not change until a separate /model selection.
    assert set(result.mounted) == {
        "provider-anthropic",
        "tool-x",
        "tool-y",
        "hook-z",
        "agent-q",
    }
    assert result.deferred_sections == ()
    assert result.skipped == ()
    # Every mounted module contributed a cleanup handle.
    assert len(result.cleanups) == 5


@pytest.mark.asyncio
async def test_composable_sections_constant_is_the_additive_set() -> None:
    assert COMPOSABLE_SECTIONS == ("providers", "tools", "hooks", "agents")


@pytest.mark.asyncio
async def test_per_module_failure_is_best_effort() -> None:
    loader = _FakeLoader(fail={"tool-y"})
    result = await mount_overlay_modules(_FakeCoordinator(loader), _plan())
    assert "tool-y" in result.skipped
    assert "tool-x" in result.mounted and "hook-z" in result.mounted
    assert result.ok is True  # at least one module mounted


@pytest.mark.asyncio
async def test_no_loader_skips_every_module_without_raising() -> None:
    result = await mount_overlay_modules(_FakeCoordinator(None), _plan())
    assert result.mounted == ()
    assert set(result.skipped) == {
        "provider-anthropic",
        "tool-x",
        "tool-y",
        "hook-z",
        "agent-q",
    }
    assert result.ok is False


@pytest.mark.asyncio
async def test_empty_plan_is_ok_with_nothing_mounted() -> None:
    result = await mount_overlay_modules(_FakeCoordinator(_FakeLoader()), {})
    assert result.ok is True
    assert result.mounted == () and result.skipped == ()
    assert result.deferred_sections == ()


@pytest.mark.asyncio
async def test_summary_reports_mounted_and_deferred() -> None:
    result = await mount_overlay_modules(_FakeCoordinator(_FakeLoader()), _plan())
    summary = result.summary("heavy")
    assert "loaded" in summary and "heavy" in summary
    assert "5 module(s) mounted" in summary
    assert "attach at next session start" not in summary


@pytest.mark.asyncio
async def test_junk_entries_are_dropped() -> None:
    plan = {"tools": ["notadict", {"no_module_key": 1}, {"module": "tool-ok"}]}
    result = await mount_overlay_modules(_FakeCoordinator(_FakeLoader()), plan)
    assert result.mounted == ("tool-ok",)


@pytest.mark.asyncio
async def test_shared_ledger_makes_overlay_mount_idempotent() -> None:
    loader = _FakeLoader()
    seen = module_identities({"tools": [{"module": "tool-boot"}]})
    plan = {"tools": [{"module": "tool-boot"}, {"module": "tool-new"}]}

    first = await mount_overlay_modules(_FakeCoordinator(loader), plan, seen=seen)
    second = await mount_overlay_modules(_FakeCoordinator(loader), plan, seen=seen)

    assert first.mounted == ("tool-new",)
    assert first.already_mounted == ("tool-boot",)
    assert len(first.cleanups) == 1
    assert second.mounted == ()
    assert second.already_mounted == ("tool-boot", "tool-new")
    assert second.cleanups == []
    assert loader.loaded == ["tool-new"]


@pytest.mark.parametrize(
    ("module_id", "section"),
    [
        ("tool-mcp", "tools"),
        ("amplifier-module-tool-filesystem", "tools"),
        ("hook-redaction", "hooks"),
        ("hooks-routing", "hooks"),
        ("provider-anthropic", "providers"),
        ("orchestrator-loop-streaming", None),
        ("context-simple", None),
        ("agent-explorer", None),
        ("mystery", None),
    ],
)
def test_explicit_module_kind_boundary(module_id: str, section: str | None) -> None:
    assert additive_module_section(module_id) == section


@pytest.mark.asyncio
async def test_mount_additive_module_forwards_source_and_registers_one_cleanup() -> None:
    loader = _FakeLoader()
    seen: set[str] = set()

    first = await mount_additive_module(
        _FakeCoordinator(loader),
        "tool-extra",
        source_hint="git+https://example.test/tool@abc",
        seen=seen,
    )
    second = await mount_additive_module(
        _FakeCoordinator(loader),
        "tool-extra",
        source_hint="git+https://example.test/tool@abc",
        seen=seen,
    )

    assert first.ok and first.mounted == ("tool-extra",)
    assert len(first.cleanups) == 1
    assert second.ok and second.already_mounted == ("tool-extra",)
    assert second.cleanups == []
    assert loader.loaded == ["tool-extra"]


@pytest.mark.asyncio
async def test_mount_additive_provider_is_live_and_idempotent() -> None:
    loader = _FakeLoader()
    seen: set[str] = set()
    first = await mount_additive_module(
        _FakeCoordinator(loader),
        "provider-anthropic",
        seen=seen,
    )
    second = await mount_additive_module(
        _FakeCoordinator(loader),
        "provider-anthropic",
        seen=seen,
    )
    assert first.ok and first.mounted == ("provider-anthropic",)
    assert second.ok and second.already_mounted == ("provider-anthropic",)
    assert loader.loaded == ["provider-anthropic"]


@pytest.mark.asyncio
async def test_live_provider_stays_behind_existing_serving_provider() -> None:
    loader = _FakeLoader()
    coordinator = _FakeCoordinator(loader)
    parent_config: dict[str, Any] = {}
    serving = SimpleNamespace(priority=1, config={"priority": 1})
    coordinator.mount_points["providers"]["anthropic"] = serving

    result = await mount_overlay_modules(
        coordinator,
        {"providers": [{"module": "provider-openai", "config": {"priority": -100}}]},
        parent_config=parent_config,
    )

    assert result.ok and result.mounted == ("provider-openai",)
    providers = coordinator.mount_points["providers"]
    assert providers["anthropic"] is serving
    assert providers["openai"].priority == 2
    assert providers["openai"].config["priority"] == 2
    assert min(providers, key=lambda name: providers[name].priority) == "anthropic"
    assert coordinator.config["providers"][0]["config"]["priority"] == 2
    assert parent_config["providers"][0]["config"]["priority"] == 2


@pytest.mark.asyncio
async def test_live_provider_instance_remap_restores_default_and_cleanup() -> None:
    loader = _FakeLoader()
    coordinator = _FakeCoordinator(loader)
    original = SimpleNamespace(priority=4, config={"priority": 4})
    coordinator.mount_points["providers"]["anthropic"] = original

    result = await mount_overlay_modules(
        coordinator,
        {
            "providers": [
                {
                    "module": "provider-anthropic",
                    "instance_id": "anthropic-alt",
                    "config": {"priority": 0},
                }
            ]
        },
    )

    assert result.ok and result.mounted == ("provider-anthropic",)
    providers = coordinator.mount_points["providers"]
    assert providers["anthropic"] is original
    assert providers["anthropic-alt"] is not original
    assert providers["anthropic-alt"].priority == 5
    assert len(result.cleanups) == 1
    await result.cleanups[0]()
    assert coordinator.mount_points["providers"] == {"anthropic": original}
    assert loader.cleaned == ["provider-anthropic"]


@pytest.mark.asyncio
async def test_remapped_provider_raw_cleanup_cannot_remove_restored_default() -> None:
    loader = _FakeLoader(
        provider_cleanup_unmount_default={"provider-anthropic"},
    )
    coordinator = _FakeCoordinator(loader)
    original = SimpleNamespace(priority=4, config={"priority": 4})
    peer = SimpleNamespace(priority=4, config={"priority": 4})
    coordinator.mount_points["providers"]["anthropic"] = original
    coordinator.mount_points["providers"]["openai"] = peer

    result = await mount_overlay_modules(
        coordinator,
        {"providers": [{"module": "provider-anthropic", "instance_id": "anthropic-alt"}]},
    )
    assert result.ok and len(result.cleanups) == 1

    await result.cleanups[0]()

    assert coordinator.mount_points["providers"] == {
        "anthropic": original,
        "openai": peer,
    }
    assert list(coordinator.mount_points["providers"]) == ["anthropic", "openai"]
    assert loader.cleaned == ["provider-anthropic"]


@pytest.mark.asyncio
async def test_remapped_cleanup_preserves_later_default_replacement() -> None:
    loader = _FakeLoader(
        provider_cleanup_unmount_default={"provider-anthropic"},
    )
    coordinator = _FakeCoordinator(loader)
    original = SimpleNamespace(priority=4, config={"priority": 4})
    peer = SimpleNamespace(priority=4, config={"priority": 4})
    replacement = SimpleNamespace(priority=2, config={"priority": 2})
    coordinator.mount_points["providers"].update({"anthropic": original, "openai": peer})

    result = await mount_overlay_modules(
        coordinator,
        {"providers": [{"module": "provider-anthropic", "instance_id": "anthropic-alt"}]},
    )
    await coordinator.mount("providers", replacement, name="anthropic")

    await result.cleanups[0]()

    assert coordinator.mount_points["providers"] == {
        "anthropic": replacement,
        "openai": peer,
    }
    assert list(coordinator.mount_points["providers"]) == ["anthropic", "openai"]


@pytest.mark.asyncio
async def test_provider_mount_raise_restores_full_mapping_order_and_closes_orphan() -> None:
    loader = _FakeLoader(provider_raise_after_mount={"provider-anthropic"})
    coordinator = _FakeCoordinator(loader)
    anthropic = SimpleNamespace(priority=1, config={"priority": 1})
    openai = SimpleNamespace(priority=1, config={"priority": 1})
    coordinator.mount_points["providers"].update({"anthropic": anthropic, "openai": openai})
    before = dict(coordinator.mount_points["providers"])

    result = await mount_overlay_modules(
        coordinator,
        {"providers": [{"module": "provider-anthropic", "instance_id": "anthropic-alt"}]},
        seen=set(),
    )

    assert result.ok is False and result.skipped == ("provider-anthropic",)
    assert coordinator.mount_points["providers"] == before
    assert list(coordinator.mount_points["providers"]) == ["anthropic", "openai"]
    assert min(before, key=lambda name: before[name].priority) == "anthropic"
    assert loader.providers[-1].closed is True


@pytest.mark.asyncio
async def test_config_selected_provider_mount_name_is_preserved_without_orphan() -> None:
    loader = _FakeLoader(
        provider_honors_config_name={"provider-chat-completions"},
    )
    coordinator = _FakeCoordinator(loader)
    serving = SimpleNamespace(priority=1, config={"priority": 1})
    coordinator.mount_points["providers"]["anthropic"] = serving

    result = await mount_overlay_modules(
        coordinator,
        {
            "providers": [
                {
                    "module": "provider-chat-completions",
                    "config": {"name": "surprise"},
                }
            ]
        },
    )

    assert result.ok and result.mounted == ("provider-chat-completions",)
    assert coordinator.mount_points["providers"] == {
        "anthropic": serving,
        "surprise": loader.providers[-1],
    }
    assert loader.providers[-1].closed is False


@pytest.mark.asyncio
async def test_provider_that_self_mounts_at_requested_identity_is_accepted() -> None:
    loader = _FakeLoader(
        provider_honors_config_name={"provider-chat-completions"},
    )
    coordinator = _FakeCoordinator(loader)
    serving = SimpleNamespace(priority=1, config={"priority": 1})
    coordinator.mount_points["providers"]["anthropic"] = serving

    result = await mount_overlay_modules(
        coordinator,
        {
            "providers": [
                {
                    "module": "provider-chat-completions",
                    "id": "openmj",
                    "config": {"name": "openmj", "priority": -10},
                }
            ]
        },
    )

    assert result.ok and result.mounted == ("provider-chat-completions",)
    assert list(coordinator.mount_points["providers"]) == ["anthropic", "openmj"]
    assert coordinator.mount_points["providers"]["openmj"].priority == 2


@pytest.mark.asyncio
async def test_provider_extra_identity_is_rejected_and_fully_rolled_back() -> None:
    loader = _FakeLoader(
        provider_extra_mount_name={"provider-openai": "orphan"},
    )
    coordinator = _FakeCoordinator(loader)
    serving = SimpleNamespace(priority=1, config={"priority": 1})
    coordinator.mount_points["providers"]["anthropic"] = serving

    result = await mount_overlay_modules(
        coordinator,
        {"providers": [{"module": "provider-openai", "id": "openai-alt"}]},
    )

    assert result.ok is False and result.skipped == ("provider-openai",)
    assert coordinator.mount_points["providers"] == {"anthropic": serving}
    assert loader.providers[-1].closed is True


@pytest.mark.asyncio
async def test_existing_provider_identity_is_never_overwritten() -> None:
    loader = _FakeLoader()
    coordinator = _FakeCoordinator(loader)
    existing = SimpleNamespace(priority=3, config={"priority": 3})
    coordinator.mount_points["providers"]["runpod"] = existing

    result = await mount_overlay_modules(
        coordinator,
        {"providers": [{"module": "provider-vllm", "id": "runpod", "config": {"priority": 0}}]},
        seen=set(),
    )

    assert result.ok is False
    assert result.skipped == ("provider-vllm",)
    assert coordinator.mount_points["providers"] == {"runpod": existing}
    assert loader.loaded == []


@pytest.mark.asyncio
async def test_provider_that_does_not_mount_remains_retryable() -> None:
    loader = _FakeLoader(provider_no_mount={"provider-openai"})
    coordinator = _FakeCoordinator(loader)
    seen: set[str] = set()

    result = await mount_overlay_modules(
        coordinator,
        {"providers": [{"module": "provider-openai"}]},
        seen=seen,
    )

    assert result.ok is False
    assert result.skipped == ("provider-openai",)
    assert "providers:openai" not in seen
    assert coordinator.mount_points["providers"] == {}
    assert loader.cleaned == ["provider-openai"]


@pytest.mark.asyncio
async def test_session_ready_runs_after_whole_batch_and_mount_cleanup_is_retained() -> None:
    observed: list[str] = []

    async def ready(coordinator: _FakeCoordinator) -> None:
        assert "explorer" in coordinator.config["agents"]
        observed.append("ready")

    loader = _FakeLoader(ready={"hook-lifecycle": ready})
    coordinator = _FakeCoordinator(loader)
    result = await mount_overlay_modules(
        coordinator,
        {
            "hooks": [{"module": "hook-lifecycle"}],
            "agents": {"explorer": {"description": "Inspect"}},
        },
    )

    assert result.ok
    assert observed == ["ready"]
    assert len(result.cleanups) == 2
    for cleanup in reversed(result.cleanups):
        value = cleanup()
        if value is not None:
            await value
    assert loader.cleaned == ["hook-lifecycle"]


@pytest.mark.asyncio
async def test_session_ready_failure_emits_callback_declared_module_id() -> None:
    async def ready(_coordinator: _FakeCoordinator) -> None:
        raise RuntimeError("ready failed")

    loader = _FakeLoader(
        ready={"hook-alias": ready},
        ready_ids={"hook-alias": "hook-declared"},
    )
    coordinator = _FakeCoordinator(loader)

    result = await mount_overlay_modules(
        coordinator,
        {"hooks": [{"module": "hook-alias"}]},
    )

    assert result.ok and result.mounted == ("hook-alias",)
    assert coordinator.emitted == [
        (
            "module:on_session_ready_failed",
            {"module_id": "hook-declared", "error": "ready failed"},
        )
    ]


@pytest.mark.asyncio
async def test_existing_agent_identity_is_never_overwritten() -> None:
    coordinator = _FakeCoordinator(_FakeLoader())
    original = {"description": "Boot agent"}
    coordinator.config["agents"] = {"explorer": original}
    seen: set[str] = set()

    result = await mount_overlay_modules(
        coordinator,
        {"agents": {"explorer": {"description": "Overlay agent"}}},
        seen=seen,
    )

    assert result.ok is False
    assert result.skipped == ("agent:explorer",)
    assert coordinator.config["agents"]["explorer"] is original
    assert "agents:explorer" not in seen


@pytest.mark.asyncio
async def test_parent_agent_collision_rolls_back_coordinator_staging() -> None:
    coordinator = _FakeCoordinator(_FakeLoader())
    original = {"description": "Parent agent"}
    parent_config = {"agents": {"explorer": original}}

    result = await mount_overlay_modules(
        coordinator,
        {"agents": {"explorer": {"description": "Overlay agent"}}},
        parent_config=parent_config,
    )

    assert result.ok is False and result.skipped == ("agent:explorer",)
    assert "agents" not in coordinator.config
    assert parent_config["agents"]["explorer"] is original


@pytest.mark.asyncio
async def test_config_inheritance_failure_unmounts_module_and_restores_every_target() -> None:
    loader = _FakeLoader()
    coordinator = _FakeCoordinator(loader)
    coordinator.config["tools"] = {"invalid": "shape"}
    parent_config: dict[str, Any] = {}

    result = await mount_overlay_modules(
        coordinator,
        {"tools": [{"module": "tool-extra"}]},
        seen=set(),
        parent_config=parent_config,
    )

    assert result.ok is False and result.skipped == ("tool-extra",)
    assert coordinator.config == {"tools": {"invalid": "shape"}}
    assert parent_config == {}
    assert loader.cleaned == ["tool-extra"]


@pytest.mark.asyncio
async def test_existing_failed_config_entry_is_replaced_then_restored_exactly() -> None:
    loader = _FakeLoader()
    coordinator = _FakeCoordinator(loader)
    coordinator_old = {"module": "tool-extra", "config": {"version": "boot"}}
    parent_old = {"module": "tool-extra", "config": {"version": "parent"}}
    coordinator.config["tools"] = [coordinator_old]
    parent_config = {"tools": [parent_old]}

    result = await mount_overlay_modules(
        coordinator,
        {"tools": [{"module": "tool-extra", "config": {"version": "live"}}]},
        seen=set(),
        parent_config=parent_config,
    )

    assert coordinator.config["tools"][0]["config"]["version"] == "live"
    assert parent_config["tools"][0]["config"]["version"] == "live"
    cleanup_result = result.cleanups[0]()
    if cleanup_result is not None:
        await cleanup_result
    assert coordinator.config["tools"][0] is coordinator_old
    assert parent_config["tools"][0] is parent_old


def test_boot_ledger_retains_only_verified_provider_and_tool_mounts() -> None:
    coordinator = _FakeCoordinator(_FakeLoader())
    coordinator.mount_points["providers"]["anthropic"] = object()
    plan = {
        "providers": [
            {"module": "provider-anthropic"},
            {"module": "provider-vllm", "id": "runpod"},
        ],
        "tools": [{"module": "tool-filesystem"}, {"module": "tool-team-pulse"}],
        "hooks": [{"module": "hook-safe"}],
    }

    identities = boot_module_identities(
        plan,
        coordinator,
        missing_tools=("tool-team-pulse",),
    )

    assert "providers:anthropic" in identities
    assert "providers:runpod" not in identities
    assert "tools:tool-filesystem" in identities
    assert "tools:tool-team-pulse" not in identities
    assert "hooks:hook-safe" in identities


@pytest.mark.asyncio
async def test_equivalent_module_prefix_alias_is_idempotent() -> None:
    loader = _FakeLoader()
    coordinator = _FakeCoordinator(loader)
    provider = SimpleNamespace(priority=1, config={"priority": 1})
    coordinator.mount_points["providers"]["openai"] = provider
    seen = module_identities({"providers": [{"module": "provider-openai"}]})

    result = await mount_additive_module(
        coordinator,
        "amplifier-module-provider-openai",
        seen=seen,
    )

    assert result.ok and result.already_mounted == ("amplifier-module-provider-openai",)
    assert loader.loaded == []


@pytest.mark.asyncio
async def test_mount_additive_module_rejects_singletons_before_loader() -> None:
    loader = _FakeLoader()
    result = await mount_additive_module(
        _FakeCoordinator(loader),
        "orchestrator-loop-streaming",
        seen=set(),
    )
    assert result.ok is False
    assert "next session start" in result.message
    assert loader.loaded == []


@pytest.mark.asyncio
async def test_loader_load_itself_may_be_async() -> None:
    class _AsyncLoader(_FakeLoader):
        async def load(  # type: ignore[override]
            self,
            module_id,
            config=None,
            source_hint=None,
            coordinator=None,  # noqa: ANN001
        ):
            return super().load(module_id, config, source_hint, coordinator)

    loader = _AsyncLoader()
    result = await mount_overlay_modules(
        _FakeCoordinator(loader),
        {"tools": [{"module": "tool-async-loader"}]},
    )
    assert result.ok
    assert result.mounted == ("tool-async-loader",)
    assert loader.loaded == ["tool-async-loader"]


@pytest.mark.asyncio
async def test_foundation_agents_mapping_is_merged_live_and_cleaned_once() -> None:
    coordinator = _FakeCoordinator(_FakeLoader())
    seen: set[str] = set()
    plan = {
        "agents": {
            "explorer": {
                "description": "Inspect the repository",
                "providers": [{"module": "provider-anthropic"}],
            }
        }
    }

    first = await mount_overlay_modules(coordinator, plan, seen=seen)
    second = await mount_overlay_modules(coordinator, plan, seen=seen)

    assert first.ok and first.mounted == ("agent:explorer",)
    assert coordinator.config["agents"]["explorer"]["description"] == ("Inspect the repository")
    assert len(first.cleanups) == 1
    assert second.already_mounted == ("agent:explorer",)
    assert second.cleanups == []
    first.cleanups[0]()
    assert "agents" not in coordinator.config


@pytest.mark.asyncio
async def test_bundle_content_loss_is_explicitly_reported() -> None:
    result = await mount_overlay_modules(
        _FakeCoordinator(_FakeLoader()),
        {},
        bundle_content_deferred=True,
    )
    assert result.ok is False
    assert result.deferred_sections == ("bundle instructions/context",)
    assert "bundle instructions/context attach at next session start" in result.summary("policy")


@pytest.mark.asyncio
async def test_session_singleton_dict_shapes_are_reported() -> None:
    result = await mount_overlay_modules(
        _FakeCoordinator(_FakeLoader()),
        {
            "session": {
                "orchestrator": {"module": "orchestrator-loop"},
                "context": {"module": "context-simple"},
            }
        },
    )
    assert result.ok is False
    assert result.deferred_sections == ("orchestrator", "context")
