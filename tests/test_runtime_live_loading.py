"""Same-session bundle/module loading over the real runtime seam.

No network or real Amplifier session is required: the Foundation prepare step
and coordinator loader are duck-typed so these tests pin target resolution,
the additive-only boundary, idempotency, and teardown registration directly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from amplifier_app_tui.kernel import runtime as runtime_module
from amplifier_app_tui.kernel.runtime import RealRuntime
from amplifier_app_tui.kernel.spawner import _merged_config


class _Loader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.cleaned: list[str] = []

    def load(self, module_id, config=None, source_hint=None, coordinator=None):  # noqa: ANN001
        del coordinator

        async def mount(coord):  # noqa: ANN001
            self.calls.append((module_id, dict(config or {}), source_hint))
            canonical = str(module_id).removeprefix("amplifier-module-")
            if canonical.startswith("provider-"):
                provider_config = dict(config or {})
                await coord.mount(
                    "providers",
                    SimpleNamespace(
                        priority=provider_config.get("priority", 100),
                        config=provider_config,
                    ),
                    name=canonical.removeprefix("provider-"),
                )
            return lambda: self.cleaned.append(module_id)

        return mount


class _Coordinator:
    def __init__(self) -> None:
        self.loader = _Loader()
        self.context = _Context()
        self.config: dict[str, Any] = {}
        self.mount_points: dict[str, dict[str, Any]] = {"providers": {}, "tools": {}}

    def get(self, mount: str):  # noqa: ANN201
        return self.context if mount == "context" else self.mount_points.get(mount)

    async def mount(self, mount: str, value: Any, *, name: str) -> None:
        self.mount_points.setdefault(mount, {})[name] = value

    async def unmount(self, mount: str, *, name: str) -> None:
        self.mount_points.setdefault(mount, {}).pop(name, None)


class _Context:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def add_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def get_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self.messages = list(messages)


def _prepared(plan: dict[str, Any], rendered: str = "") -> SimpleNamespace:
    bundle = SimpleNamespace(name="overlay", instruction=rendered)

    def factory_builder(bundle_arg, session_arg, session_cwd=None):  # noqa: ANN001
        del bundle_arg, session_arg, session_cwd

        async def render() -> str:
            return rendered

        return render

    return SimpleNamespace(
        mount_plan=plan,
        bundle=bundle,
        _create_system_prompt_factory=factory_builder,
    )


def _live_runtime(
    tmp_path,
    *,
    settings: dict[str, Any] | None = None,
    bundle_uri: str = "root.md",
    overlays: tuple[str, ...] = (),
) -> tuple[RealRuntime, _Coordinator, list[Any]]:
    runtime = RealRuntime(project_dir=tmp_path)
    coordinator = _Coordinator()
    parent_config: dict[str, Any] = {}
    cleanups: list[Any] = []
    runtime._resolved = SimpleNamespace(  # type: ignore[assignment]
        settings=settings or {},
        project_dir=tmp_path,
        bundle_uri=bundle_uri,
        overlays=overlays,
    )
    runtime._initialized = SimpleNamespace(  # type: ignore[assignment]
        coordinator=coordinator,
        session=SimpleNamespace(coordinator=coordinator, config=parent_config),
        unregister_handles=cleanups,
    )
    return runtime, coordinator, cleanups


@pytest.mark.asyncio
async def test_registered_bundle_loads_once_and_registers_one_cleanup(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = "git+https://example.test/heavy@abc"
    runtime, coordinator, cleanups = _live_runtime(
        tmp_path,
        settings={"bundle": {"added": {"heavy": uri}}},
    )
    prepared: list[str] = []

    async def prepare(target, settings, **kwargs):  # noqa: ANN001
        del settings, kwargs
        prepared.append(target)
        return _prepared({"tools": [{"module": "tool-extra"}]})

    monkeypatch.setattr(runtime_module, "prepare_live_overlay_bundle", prepare)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())

    first = await runtime.load_deferred_bundle("heavy")
    second = await runtime.load_deferred_bundle("heavy")

    assert first[0] is True
    assert first[1] == "loaded · heavy · 1 module(s) mounted"
    assert second[0] is True and "already loaded this session" in second[1]
    assert prepared == [uri]
    assert [call[0] for call in coordinator.loader.calls] == ["tool-extra"]
    assert len(cleanups) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_bundle_requests_share_one_mount(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = "git+https://example.test/heavy@abc"
    runtime, coordinator, cleanups = _live_runtime(tmp_path)
    prepared: list[str] = []

    async def prepare(target, settings, **kwargs):  # noqa: ANN001
        del settings, kwargs
        prepared.append(target)
        await asyncio.sleep(0)
        return _prepared({"tools": [{"module": "tool-extra"}]})

    monkeypatch.setattr(runtime_module, "prepare_live_overlay_bundle", prepare)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())

    results = await asyncio.gather(
        runtime.load_deferred_bundle(uri),
        runtime.load_deferred_bundle(uri),
    )

    assert all(ok for ok, _detail in results)
    assert prepared == [uri]
    assert [call[0] for call in coordinator.loader.calls] == ["tool-extra"]
    assert len(cleanups) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["deferred", "local", "direct"])
async def test_bundle_target_accepts_deferred_local_and_direct_uri(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    uri = "git+https://example.test/overlay@abc"
    target = uri
    settings: dict[str, Any] = {}
    if target_kind == "deferred":
        settings = {
            "bundle": {
                "app": [uri],
                "added": {"overlay": uri},
                "deferred": ["overlay"],
            }
        }
        target = "overlay"
    elif target_kind == "local":
        local = tmp_path / "local.md"
        local.write_text("---\nbundle:\n  name: local\n---\n", encoding="utf-8")
        target = str(local)
        uri = str(local)

    runtime, _coordinator, _cleanups = _live_runtime(tmp_path, settings=settings)
    prepared: list[str] = []

    async def prepare(source, effective_settings, **kwargs):  # noqa: ANN001
        del effective_settings, kwargs
        prepared.append(source)
        return _prepared({"hooks": [{"module": "hook-extra"}]})

    monkeypatch.setattr(runtime_module, "prepare_live_overlay_bundle", prepare)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())

    ok, detail = await runtime.load_deferred_bundle(target)

    assert ok and "1 module(s) mounted" in detail
    assert prepared == [uri]


@pytest.mark.asyncio
async def test_bundle_already_active_at_boot_is_a_noop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = "git+https://example.test/already@abc"
    runtime, coordinator, cleanups = _live_runtime(tmp_path, bundle_uri=uri)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())

    ok, detail = await runtime.load_deferred_bundle(uri)

    assert ok and detail == f"already active from session start · {uri}"
    assert coordinator.loader.calls == []
    assert cleanups == []


@pytest.mark.asyncio
async def test_bundle_provider_is_mounted_live_without_implicit_selection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = "git+https://example.test/provider-overlay@abc"
    runtime, coordinator, cleanups = _live_runtime(tmp_path)

    async def prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        return _prepared({"providers": [{"module": "provider-openai"}]})

    monkeypatch.setattr(runtime_module, "prepare_live_overlay_bundle", prepare)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())

    ok, detail = await runtime.load_deferred_bundle(uri)

    assert ok is True
    assert "1 module(s) mounted" in detail
    assert "attach at next session start" not in detail
    assert coordinator.loader.calls == [("provider-openai", {}, None)]
    assert len(cleanups) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_id", "section"),
    [
        ("provider-openai", "providers"),
        ("tool-extra", "tools"),
        ("hook-extra", "hooks"),
    ],
)
async def test_live_module_is_inherited_by_future_child_and_cleanup_restores_config(
    tmp_path,
    module_id: str,
    section: str,
) -> None:
    runtime, coordinator, cleanups = _live_runtime(tmp_path)
    parent = runtime._initialized.session  # type: ignore[union-attr]

    ok, _detail = await runtime.load_module(module_id)

    assert ok
    assert coordinator.config[section][0]["module"] == module_id
    assert parent.config[section][0]["module"] == module_id
    assert _merged_config(parent, {})[section][0]["module"] == module_id
    assert len(cleanups) == 1

    result = cleanups[0]()
    if asyncio.iscoroutine(result):
        await result
    assert section not in coordinator.config
    assert section not in parent.config


@pytest.mark.asyncio
async def test_live_bundle_agent_is_inherited_by_future_child_and_cleaned(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, coordinator, cleanups = _live_runtime(tmp_path)
    parent = runtime._initialized.session  # type: ignore[union-attr]

    async def prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        return _prepared({"agents": {"reviewer": {"description": "Review changes"}}})

    monkeypatch.setattr(runtime_module, "prepare_live_overlay_bundle", prepare)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())

    ok, _detail = await runtime.load_deferred_bundle("git+https://example.test/agents@abc")

    assert ok
    assert coordinator.config["agents"]["reviewer"]["description"] == "Review changes"
    assert parent.config["agents"]["reviewer"]["description"] == "Review changes"
    assert _merged_config(parent, {})["agents"]["reviewer"]["description"] == "Review changes"

    result = cleanups[0]()
    if asyncio.iscoroutine(result):
        await result
    assert "agents" not in coordinator.config
    assert "agents" not in parent.config


@pytest.mark.asyncio
async def test_bundle_instruction_and_context_are_active_for_next_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = "git+https://example.test/behavior@abc"
    runtime, coordinator, cleanups = _live_runtime(tmp_path)

    async def prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        return _prepared({}, "Use the distinctive live behavior.")

    monkeypatch.setattr(runtime_module, "prepare_live_overlay_bundle", prepare)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())

    ok, detail = await runtime.load_deferred_bundle(uri)

    assert ok is True
    assert "instructions/context active for next turn" in detail
    assert coordinator.context.messages == [
        {
            "role": "system",
            "content": "Use the distinctive live behavior.",
            "metadata": {
                "source": "hook",
                "kind": "amplifier-tui-live-bundle",
                "bundle": "overlay",
                "activation_id": coordinator.context.messages[0]["metadata"]["activation_id"],
            },
        }
    ]
    assert len(cleanups) == 1


@pytest.mark.asyncio
async def test_explicit_tool_module_load_is_live_idempotent_and_keeps_source(
    tmp_path,
) -> None:
    runtime, coordinator, cleanups = _live_runtime(tmp_path)
    source = "git+https://example.test/tool-extra@abc"

    first = await runtime.load_module("tool-extra", source)
    second = await runtime.load_module("tool-extra", source)

    assert first == (True, "loaded · tool-extra · 1 module(s) mounted")
    assert second == (True, "loaded · tool-extra · 1 already active")
    assert coordinator.loader.calls == [("tool-extra", {}, source)]
    assert len(cleanups) == 1


@pytest.mark.asyncio
async def test_explicit_provider_module_load_is_live(tmp_path) -> None:
    runtime, coordinator, cleanups = _live_runtime(tmp_path)

    ok, detail = await runtime.load_module("provider-anthropic")

    assert ok is True
    assert "1 module(s) mounted" in detail
    assert coordinator.loader.calls == [("provider-anthropic", {}, None)]
    assert len(cleanups) == 1


@pytest.mark.asyncio
async def test_explicit_module_rejects_singletons_without_touching_loader(tmp_path) -> None:
    runtime, coordinator, cleanups = _live_runtime(tmp_path)

    ok, detail = await runtime.load_module("orchestrator-loop-streaming")

    assert ok is False
    assert "attach at next session start" in detail
    assert coordinator.loader.calls == []
    assert cleanups == []


@pytest.mark.asyncio
async def test_explicit_module_refuses_tui_suppressed_hook(tmp_path) -> None:
    runtime, coordinator, cleanups = _live_runtime(tmp_path)

    ok, detail = await runtime.load_module("hooks-streaming-ui")

    assert ok is False
    assert "bypasses TUI rendering" in detail
    assert coordinator.loader.calls == []
    assert cleanups == []


@pytest.mark.asyncio
async def test_native_computer_use_bundle_composes_live_and_future_child_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = "git+https://example.test/amplifier-bundle-computer-use@abc"
    awareness = (
        "Native computer use is available in this live session. "
        "Delegate visual interaction to computer-use:computer-operator and use "
        "the mounted computer-use tool only for tasks that require local UI control."
    )
    operator = {
        "description": "Operate the local computer through native computer use.",
        "instruction": "Use the mounted computer-use tool and report visible outcomes.",
    }
    plan = {
        "tools": [{"module": "tool-computer-use"}],
        "hooks": [{"module": "hook-computer-use"}],
        "agents": {"computer-use:computer-operator": operator},
    }
    runtime, coordinator, cleanups = _live_runtime(tmp_path)
    parent = runtime._initialized.session  # type: ignore[union-attr]
    prepared: list[str] = []

    async def prepare(target, settings, **kwargs):  # noqa: ANN001
        del settings, kwargs
        prepared.append(target)
        return _prepared(plan, awareness)

    monkeypatch.setattr(runtime_module, "prepare_live_overlay_bundle", prepare)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())

    first = await runtime.load_deferred_bundle(uri)
    first_calls = list(coordinator.loader.calls)
    first_messages = await coordinator.context.get_messages()
    first_cleanup_count = len(cleanups)
    second = await runtime.load_deferred_bundle(uri)

    assert first == (
        True,
        f"loaded · {uri} · 3 module(s) mounted · instructions/context active for next turn",
    )
    assert second[0] is True and "already loaded this session" in second[1]
    assert prepared == [uri]
    assert [call[0] for call in first_calls] == [
        "tool-computer-use",
        "hook-computer-use",
    ]
    assert coordinator.loader.calls == first_calls

    expected_tool = {"module": "tool-computer-use"}
    expected_hook = {"module": "hook-computer-use"}
    for config in (coordinator.config, parent.config):
        assert config["tools"] == [expected_tool]
        assert config["hooks"] == [expected_hook]
        assert config["agents"]["computer-use:computer-operator"] == operator
    child_config = _merged_config(parent, {})
    assert child_config["tools"] == [expected_tool]
    assert child_config["hooks"] == [expected_hook]
    assert child_config["agents"]["computer-use:computer-operator"] == operator

    assert len(first_messages) == 1
    assert first_messages[0]["role"] == "system"
    assert first_messages[0]["content"] == awareness
    assert first_messages[0]["metadata"]["kind"] == "amplifier-tui-live-bundle"
    assert await coordinator.context.get_messages() == first_messages
    # Tool, hook, agent definition, and awareness message each own teardown.
    assert first_cleanup_count == 4
    assert len(cleanups) == first_cleanup_count

    for cleanup in reversed(tuple(cleanups)):
        result = cleanup()
        if asyncio.iscoroutine(result):
            await result
    assert coordinator.loader.cleaned == ["hook-computer-use", "tool-computer-use"]
    assert coordinator.config == {}
    assert parent.config == {}
    assert await coordinator.context.get_messages() == []


@pytest.mark.asyncio
async def test_native_computer_use_missing_tool_reports_failure_without_inheritance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = "git+https://example.test/amplifier-bundle-computer-use-missing@abc"
    runtime, coordinator, cleanups = _live_runtime(tmp_path)
    parent = runtime._initialized.session  # type: ignore[union-attr]
    prepared: list[str] = []

    async def prepare(target, settings, **kwargs):  # noqa: ANN001
        del settings, kwargs
        prepared.append(target)
        return _prepared({"tools": [{"module": "tool-computer-use"}]})

    def missing_tool(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise ModuleNotFoundError("computer-use backend is not installed")

    monkeypatch.setattr(runtime_module, "prepare_live_overlay_bundle", prepare)
    monkeypatch.setattr(runtime_module, "list_known_bundles", lambda *a, **k: ())
    monkeypatch.setattr(coordinator.loader, "load", missing_tool)

    first = await runtime.load_deferred_bundle(uri)
    second = await runtime.load_deferred_bundle(uri)

    assert first == (False, f"load incomplete · {uri} · 1 failed")
    assert second[0] is False and "already attempted this session" in second[1]
    assert prepared == [uri]
    assert "tools" not in coordinator.config
    assert "tools" not in parent.config
    assert "tools" not in _merged_config(parent, {})
    assert cleanups == []
    assert await coordinator.context.get_messages() == []
