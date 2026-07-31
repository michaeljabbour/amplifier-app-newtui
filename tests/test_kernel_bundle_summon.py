"""Agent-summonable deferred bundles (``kernel/bundle_summon.py``).

Discovery (catalog from cheaply-available boot data + the root-context
injector) and summon (the host-provided ``load_bundle`` tool routing to
``load_deferred_bundle``). Duck-typed over fakes: no real session, no network.
The load-bearing invariants — backward compatibility (nothing deferred ⇒
nothing injected, no tool offered) and #132's single-slot honesty passed
straight through to the model — get their own cases.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from amplifier_app_tui.kernel.bundle_summon import (
    DEFERRED_CATALOG_SOURCE,
    LOAD_BUNDLE_TOOL_NAME,
    DeferredBundleEntry,
    DeferredCatalogInjector,
    LoadBundleTool,
    build_deferred_catalog,
    catalog_instruction_text,
    read_local_bundle_summary,
)
from amplifier_app_tui.kernel.runtime import RealRuntime

A = "git+https://github.com/acme/amplifier-bundle-alpha@main"
B = "git+https://github.com/acme/amplifier-bundle-beta@main#subdirectory=bundles/beta"
ROOT = "sess-root"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeContext:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self._messages = [dict(m) for m in (messages or [])]
        self.set_calls = 0

    async def get_messages(self) -> list[dict[str, Any]]:
        return [dict(m) for m in self._messages]

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self.set_calls += 1
        self._messages = [dict(m) for m in messages]


class FakeHooks:
    def __init__(self) -> None:
        self.registered: list[tuple[str, int, str]] = []
        self.unregistered: list[str] = []

    def register(self, event: str, handler: Any, *, priority: int = 0, name: str = "") -> Any:
        self.registered.append((event, priority, name))
        return lambda: self.unregistered.append(name)


class FakeCoordinator:
    def __init__(self) -> None:
        self.mounted: list[tuple[str, Any, str]] = []
        self.hooks = FakeHooks()

    async def mount(self, mount_point: str, module: Any, name: str | None = None) -> None:
        self.mounted.append((mount_point, module, str(name)))


class FakeInitialized:
    def __init__(self, coordinator: FakeCoordinator) -> None:
        self.coordinator = coordinator
        self.session_id = ROOT
        self.unregister_handles: list[Any] = []


def _catalog_messages(context: FakeContext) -> list[dict[str, Any]]:
    return [
        m
        for m in context._messages
        if isinstance(m.get("metadata"), dict)
        and m["metadata"].get("source") == DEFERRED_CATALOG_SOURCE
    ]


# --------------------------------------------------------------------------
# Discovery — build_deferred_catalog + front-matter reader + catalog text
# --------------------------------------------------------------------------


def test_name_from_registry_when_added_maps_the_uri() -> None:
    settings = {"bundle": {"added": {"alpha": A}}}
    catalog = build_deferred_catalog((A,), settings, ())
    assert catalog == (DeferredBundleEntry(name="alpha", uri=A, description=""),)


def test_name_derived_from_uri_when_not_registered() -> None:
    # git+ prefix, @ref and #subdirectory fragment all stripped to the segment.
    catalog = build_deferred_catalog((A, B), {}, ())
    assert catalog[0].name == "amplifier-bundle-alpha"
    assert catalog[1].name == "amplifier-bundle-beta"


def test_order_follows_the_deferred_list() -> None:
    catalog = build_deferred_catalog((B, A), {}, ())
    assert [entry.uri for entry in catalog] == [B, A]


def test_local_bundle_contributes_name_and_description(tmp_path: Path) -> None:
    bundle_file = tmp_path / "heavy.md"
    bundle_file.write_text(
        "---\n"
        "bundle:\n"
        "  name: heavy-tools\n"
        "  description: |\n"
        "    Heavy analysis tools for deep dives.\n"
        "    Second line ignored.\n"
        "includes: []\n"
        "---\n\n# Heavy\n",
        encoding="utf-8",
    )
    # A bare-path URI resolves locally via discover_bundle(search_paths).
    catalog = build_deferred_catalog((str(bundle_file),), {}, (tmp_path,))
    assert catalog[0].name == "heavy-tools"  # declared name wins over derived
    assert catalog[0].description == "Heavy analysis tools for deep dives."


def test_registered_name_wins_over_local_declared_name(tmp_path: Path) -> None:
    bundle_file = tmp_path / "heavy.md"
    bundle_file.write_text(
        "---\nbundle:\n  name: declared\n  description: One line.\n---\n",
        encoding="utf-8",
    )
    uri = str(bundle_file)
    settings = {"bundle": {"added": {"registry-name": uri}}}
    catalog = build_deferred_catalog((uri,), settings, (tmp_path,))
    assert catalog[0].name == "registry-name"  # registry beats the file's name
    assert catalog[0].description == "One line."  # description still read


def test_read_local_bundle_summary_handles_bad_shapes(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"
    assert read_local_bundle_summary(missing) == ("", "")

    no_front = tmp_path / "plain.md"
    no_front.write_text("# just markdown, no front matter\n", encoding="utf-8")
    assert read_local_bundle_summary(no_front) == ("", "")

    no_bundle = tmp_path / "other.md"
    no_bundle.write_text("---\nincludes: []\n---\n", encoding="utf-8")
    assert read_local_bundle_summary(no_bundle) == ("", "")

    malformed = tmp_path / "bad.md"
    malformed.write_text("---\nbundle: [unclosed\n---\n", encoding="utf-8")
    assert read_local_bundle_summary(malformed) == ("", "")


def test_catalog_instruction_text_lists_names_and_the_tool() -> None:
    catalog = (
        DeferredBundleEntry(name="alpha", uri=A, description="Alpha things."),
        DeferredBundleEntry(name="beta", uri=B),
    )
    text = catalog_instruction_text(catalog)
    assert "load_bundle" in text
    assert "- alpha: Alpha things." in text
    assert "- beta" in text and "- beta:" not in text  # no description => bare name
    # The single-slot honesty is stated up front, not hidden.
    assert "next session start" in text


def test_catalog_instruction_text_empty_when_nothing_deferred() -> None:
    assert catalog_instruction_text(()) == ""


# --------------------------------------------------------------------------
# Discovery — DeferredCatalogInjector (root-only, one message, reconciled)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injector_inserts_one_catalog_after_system_prompt() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = DeferredCatalogInjector(ROOT, "catalog body", context)
    result = await injector.handle_event("provider:request", {"session_id": ROOT})
    assert result.action == "continue"
    catalog = _catalog_messages(context)
    assert len(catalog) == 1
    assert catalog[0]["role"] == "system"
    assert catalog[0]["content"] == "catalog body"
    assert context._messages[0]["content"] == "sp"  # after the system prompt


@pytest.mark.asyncio
async def test_injector_is_idempotent_once_present() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = DeferredCatalogInjector(ROOT, "catalog body", context)
    await injector.handle_event("provider:request", {"session_id": ROOT})
    writes = context.set_calls
    await injector.handle_event("provider:request", {"session_id": ROOT})
    assert context.set_calls == writes  # already present => no redundant write
    assert len(_catalog_messages(context)) == 1


@pytest.mark.asyncio
async def test_injector_reinserts_after_clear() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = DeferredCatalogInjector(ROOT, "catalog body", context)
    await injector.handle_event("provider:request", {"session_id": ROOT})
    context._messages = [{"role": "system", "content": "sp"}]  # /clear or compaction
    await injector.handle_event("provider:request", {"session_id": ROOT})
    assert len(_catalog_messages(context)) == 1


@pytest.mark.asyncio
async def test_injector_leaves_child_sessions_alone() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = DeferredCatalogInjector(ROOT, "catalog body", context)
    result = await injector.handle_event("provider:request", {"session_id": "sess-child"})
    assert result.action == "continue"
    assert _catalog_messages(context) == []
    assert context.set_calls == 0


@pytest.mark.asyncio
async def test_injector_ignores_non_provider_events_and_empty_text() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = DeferredCatalogInjector(ROOT, "catalog body", context)
    await injector.handle_event("tool:pre", {"session_id": ROOT})
    assert context.set_calls == 0
    empty = DeferredCatalogInjector(ROOT, "", context)
    await empty.handle_event("provider:request", {"session_id": ROOT})
    assert context.set_calls == 0


@pytest.mark.asyncio
async def test_injector_context_without_set_messages_is_safe() -> None:
    class ReadOnly:
        async def get_messages(self) -> list[dict[str, Any]]:
            return []

    injector = DeferredCatalogInjector(ROOT, "catalog body", ReadOnly())
    result = await injector.handle_event("provider:request", {"session_id": ROOT})
    assert result.action == "continue"


def test_injector_register_hooks_priority_and_name() -> None:
    hooks = FakeHooks()
    injector = DeferredCatalogInjector(ROOT, "catalog body", FakeContext())
    unregister = injector.register_hooks(hooks)
    assert hooks.registered == [("provider:request", 930, "tui-deferred-catalog")]
    unregister()
    assert hooks.unregistered == ["tui-deferred-catalog"]


def test_injector_register_hooks_tolerates_non_callable_unregister() -> None:
    class NullHooks:
        def register(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    injector = DeferredCatalogInjector(ROOT, "catalog body", FakeContext())
    injector.register_hooks(NullHooks())()  # must hand back a no-op, never crash


# --------------------------------------------------------------------------
# Summon — LoadBundleTool routes to load_deferred_bundle
# --------------------------------------------------------------------------


def _tool(load: Any, catalog: tuple[DeferredBundleEntry, ...] = ()) -> LoadBundleTool:
    return LoadBundleTool(load, catalog)


def test_tool_shape_matches_the_tool_protocol() -> None:
    tool = _tool(None, (DeferredBundleEntry(name="alpha", uri=A),))
    assert tool.name == LOAD_BUNDLE_TOOL_NAME == "load_bundle"
    assert "alpha" in tool.description
    schema = tool.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["name"]
    assert "name" in schema["properties"]


@pytest.mark.asyncio
async def test_tool_summons_and_returns_success_detail() -> None:
    calls: list[str] = []

    async def load(name: str) -> tuple[bool, str]:
        calls.append(name)
        return (True, "loaded · heavy · 3 module(s) mounted")

    result = await _tool(load).execute({"name": "heavy"})
    assert calls == ["heavy"]
    assert result.success is True
    assert result.output == "loaded · heavy · 3 module(s) mounted"


@pytest.mark.asyncio
async def test_tool_passes_through_single_slot_honesty_detail() -> None:
    # #132's honesty: single-slot modules cannot hot-swap. The tool must relay
    # that verbatim to the model rather than claiming a full hot-swap.
    async def load(_name: str) -> tuple[bool, str]:
        return (True, "loaded · heavy · providers attach at next session start")

    result = await _tool(load).execute({"name": "heavy"})
    assert result.success is True
    assert "next session start" in str(result.output)


@pytest.mark.asyncio
async def test_tool_relays_a_load_failure() -> None:
    async def load(_name: str) -> tuple[bool, str]:
        return (False, "'ghost' is not a deferred bundle · deferred: heavy")

    result = await _tool(load).execute({"name": "ghost"})
    assert result.success is False
    assert result.error is not None
    assert "not a deferred bundle" in result.error["message"]


@pytest.mark.asyncio
async def test_tool_requires_a_name_and_lists_options() -> None:
    catalog = (DeferredBundleEntry(name="alpha", uri=A),)

    async def load(_name: str) -> tuple[bool, str]:  # pragma: no cover — never called
        raise AssertionError("load must not run without a name")

    result = await _tool(load, catalog).execute({})
    assert result.success is False
    assert "requires a 'name'" in result.error["message"]
    assert "alpha" in result.error["message"]


@pytest.mark.asyncio
async def test_tool_swallows_a_load_exception() -> None:
    async def load(_name: str) -> tuple[bool, str]:
        raise RuntimeError("boom")

    result = await _tool(load).execute({"name": "heavy"})
    assert result.success is False
    assert "could not summon" in result.error["message"]


# --------------------------------------------------------------------------
# Runtime wiring — _install_deferred_summon (backward compat + degrade)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_summons_nothing_when_no_deferral() -> None:
    runtime = RealRuntime()
    runtime._resolved = SimpleNamespace(  # type: ignore[assignment]
        deferred_overlays=(), settings={}, project_dir=Path.cwd()
    )
    coordinator = FakeCoordinator()
    initialized = FakeInitialized(coordinator)
    await runtime._install_deferred_summon(initialized, FakeContext())
    assert coordinator.mounted == []
    assert coordinator.hooks.registered == []


@pytest.mark.asyncio
async def test_install_mounts_tool_and_injects_catalog() -> None:
    runtime = RealRuntime()
    runtime._resolved = SimpleNamespace(  # type: ignore[assignment]
        deferred_overlays=(A,),
        settings={"bundle": {"added": {"alpha": A}}},
        project_dir=Path.cwd(),
    )
    coordinator = FakeCoordinator()
    initialized = FakeInitialized(coordinator)
    context = FakeContext([{"role": "system", "content": "sp"}])

    await runtime._install_deferred_summon(initialized, context)

    # Tool mounted onto the coordinator's tools point under load_bundle.
    assert len(coordinator.mounted) == 1
    mount_point, tool, name = coordinator.mounted[0]
    assert (mount_point, name) == ("tools", "load_bundle")
    assert isinstance(tool, LoadBundleTool)
    # Catalog hook registered; firing it injects the catalog into context.
    assert coordinator.hooks.registered == [("provider:request", 930, "tui-deferred-catalog")]
    assert len(initialized.unregister_handles) == 1
    injector = DeferredCatalogInjector(
        ROOT,
        catalog_instruction_text(build_deferred_catalog((A,), runtime._resolved.settings, ())),
        context,
    )
    await injector.handle_event("provider:request", {"session_id": ROOT})
    body = _catalog_messages(context)[0]["content"]
    assert "alpha" in body and "load_bundle" in body


@pytest.mark.asyncio
async def test_install_without_context_still_mounts_the_tool() -> None:
    runtime = RealRuntime()
    runtime._resolved = SimpleNamespace(  # type: ignore[assignment]
        deferred_overlays=(A,), settings={}, project_dir=Path.cwd()
    )
    coordinator = FakeCoordinator()
    initialized = FakeInitialized(coordinator)
    # context is None (a session whose context module lacks editing): the tool
    # still mounts (summon works); no catalog hook is registered.
    await runtime._install_deferred_summon(initialized, None)
    assert [name for _mp, _tool, name in coordinator.mounted] == ["load_bundle"]
    assert coordinator.hooks.registered == []


@pytest.mark.asyncio
async def test_install_degrades_when_mount_raises() -> None:
    class BoomCoordinator(FakeCoordinator):
        async def mount(self, mount_point: str, module: Any, name: str | None = None) -> None:
            raise RuntimeError("mount refused")

    runtime = RealRuntime()
    runtime._resolved = SimpleNamespace(  # type: ignore[assignment]
        deferred_overlays=(A,), settings={}, project_dir=Path.cwd()
    )
    coordinator = BoomCoordinator()
    initialized = FakeInitialized(coordinator)
    context = FakeContext([{"role": "system", "content": "sp"}])
    # A mount failure must not blow up boot; the catalog injector still attaches.
    await runtime._install_deferred_summon(initialized, context)
    assert coordinator.hooks.registered == [("provider:request", 930, "tui-deferred-catalog")]
