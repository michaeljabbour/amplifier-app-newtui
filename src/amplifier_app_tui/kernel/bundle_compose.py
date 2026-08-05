"""In-session overlay composition: mount a deferred bundle on demand.

Fast boot (RCA): a user's ``bundle.app`` list can carry ~18 overlays, each
composed on EVERY session boot; ``bundle.deferred`` (kernel/config.py) holds
the heavy ones back so boot stays quick, and this module composes one of them
into the ALREADY-RUNNING session when the user asks (``/bundle load <name>``).

What tui controls vs foundation (honest boundary):

- Foundation composes a bundle's full module stack (providers, orchestrator,
  context, tools, hooks, agents) inside ``AmplifierSession.initialize()`` — a
  one-shot step; there is no supported public API to re-run it for an extra
  bundle against a live session.
- What IS supported live is the coordinator's ``loader.load(module_id, …)``
  seam (the same one ``initialize`` drives per module): it returns a mount
  function that instantiates a module and mounts it onto the running
  coordinator. This module drives that seam for the *additive* mount points
  only — ``providers`` / ``tools`` / ``hooks`` / ``agents``. Providers are a
  named multi-slot coordinator mount just like tools; adding one does not
  replace the current root provider until an explicit ``/model`` selection.
- Single-slot points (``orchestrator`` / ``context`` /
  ``module-source-resolver``) are deliberately NOT hot-swapped: replacing the
  live provider or context mid-conversation is not composition, it is a
  session identity change. An overlay that carries them is reported as
  partially composed so the boundary is never hidden — the user can move it
  back to the boot set (undefer) to get it fully at the next session start.

Everything here is duck-typed over the coordinator (``loader.load`` +
``mount``/``hooks``), so it unit-tests with a plain fake — no real session,
no amplifier-core import at module load.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Additive, multi-slot mount points a behavior overlay contributes — safe to
# mount onto a live coordinator. Order is load order (providers before tools,
# hooks, and agents mirrors Foundation's session initialization ordering for
# the additive set). Single-slot points are intentionally excluded (see module doc).
COMPOSABLE_SECTIONS: tuple[str, ...] = ("providers", "tools", "hooks", "agents")

# Mount points an overlay may carry that cannot be hot-swapped into a live
# session — reported, never mounted.
_NON_COMPOSABLE_SECTIONS: tuple[str, ...] = ("orchestrator", "context")


@dataclass
class ComposeResult:
    """Outcome of composing one overlay's additive modules into a session.

    ``cleanups`` are the per-module teardown callables the loader handed back;
    the runtime keeps them so the mounted overlay unwinds with the session
    (mirrors ``InitializedSession.unregister_handles``)."""

    ok: bool
    mounted: tuple[str, ...] = ()
    already_mounted: tuple[str, ...] = ()
    """Module ids already active in the session ledger.  They are treated as
    successful no-ops and never contribute a second cleanup handle."""
    skipped: tuple[str, ...] = ()
    """Module ids that could not mount (per-module failure) — best-effort
    composition never aborts the whole overlay for one bad module."""
    deferred_sections: tuple[str, ...] = ()
    """Non-composable mount points the overlay carried (providers/context/…);
    named so the "attaches fully at next boot" boundary is explicit."""
    message: str = ""
    cleanups: list[Callable[..., Any]] = field(default_factory=list)

    def summary(self, name: str) -> str:
        """One-line user-facing summary for the load command notice."""
        if self.message:
            return self.message
        parts: list[str] = []
        if self.mounted:
            parts.append(f"{len(self.mounted)} module(s) mounted")
        if self.already_mounted:
            parts.append(f"{len(self.already_mounted)} already active")
        if self.skipped:
            parts.append(f"{len(self.skipped)} failed")
        if self.deferred_sections:
            parts.append(f"{', '.join(self.deferred_sections)} attach at next session start")
        detail = " · ".join(parts) if parts else "nothing to mount"
        verb = "loaded" if self.ok else "load incomplete"
        return f"{verb} · {name} · {detail}"


@dataclass
class _MountOutcome:
    """One live module mount plus its deferred-ready lifecycle callback."""

    cleanup: Callable[..., Any] | None = None
    on_session_ready: Callable[..., Any] | None = None
    on_session_ready_id: str | None = None


@dataclass(frozen=True)
class _ReadyCallback:
    """A lifecycle callback retained until the live batch is fully mounted."""

    module_id: str
    callback: Callable[..., Any]


def provider_mount_name(entry: dict[str, Any]) -> str:
    """Coordinator identity a provider entry is expected to own.

    Foundation copies settings ``id`` to ``instance_id`` before boot and then
    remaps a provider module's self-mounted default name to that identity.  A
    live overlay has not passed through that boot-only normalization, so both
    spellings must be honored here.
    """
    raw_config = entry.get("config")
    config = raw_config if isinstance(raw_config, dict) else {}
    instance = entry.get("instance_id") or entry.get("id") or config.get("name")
    if instance:
        return str(instance)
    module_id = str(entry.get("module") or "")
    canonical = module_id.removeprefix("amplifier-module-")
    return canonical.removeprefix("provider-")


def _provider_mapping(coordinator: Any) -> Mapping[str, Any]:
    getter = getattr(coordinator, "get", None)
    if not callable(getter):
        return {}
    try:
        providers = getter("providers")
    except Exception:  # noqa: BLE001 - observation must not make rollback impossible
        return {}
    return providers if isinstance(providers, Mapping) else {}


def _provider_priority(provider: Any) -> int:
    value = getattr(provider, "priority", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    config = getattr(provider, "config", None)
    value = config.get("priority", 100) if isinstance(config, dict) else 100
    return value if isinstance(value, int) and not isinstance(value, bool) else 100


async def _maybe_await(value: Any) -> Any:
    return await value if isinstance(value, Awaitable) else value


async def _run_cleanup(cleanup: Callable[..., Any] | None) -> None:
    if callable(cleanup):
        await _maybe_await(cleanup())


def _provider_snapshot_matches(current: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return tuple(current) == tuple(expected) and all(
        current.get(name) is provider for name, provider in expected.items()
    )


async def _restore_provider_mapping(coordinator: Any, expected: Mapping[str, Any]) -> None:
    """Best-effort exact identity-and-order restore for a provider mapping."""
    current = dict(_provider_mapping(coordinator))
    if _provider_snapshot_matches(current, expected):
        return
    for name, provider in current.items():
        try:
            if _provider_mapping(coordinator).get(name) is provider:
                await _maybe_await(coordinator.unmount("providers", name=name))
        except Exception:  # noqa: BLE001 - continue rebuilding the snapshot
            logger.warning("could not unmount provider %s during restore", name, exc_info=True)
    for name, provider in expected.items():
        try:
            await _maybe_await(coordinator.mount("providers", provider, name=name))
        except Exception:  # noqa: BLE001 - cleanup/rollback must keep making progress
            logger.warning("could not restore provider identity %s", name, exc_info=True)


def _module_entries(mount_plan: dict[str, Any], section: str) -> list[dict[str, Any]]:
    """Normalized additive entries under *section* (junk entries dropped).

    Foundation's tools/hooks sections are lists of module specs, while agents
    are a name -> definition mapping copied directly into the session config.
    Agent entries are tagged so the live composer merges the definition rather
    than trying to resolve the agent name as a Python module.
    """
    raw = mount_plan.get(section)
    if section == "agents" and isinstance(raw, dict):
        return [
            {
                "module": f"agent:{name}",
                "name": str(name),
                "config": definition,
                "_agent_definition": True,
            }
            for name, definition in raw.items()
            if str(name) and isinstance(definition, dict)
        ]
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("module")]


def _non_composable_present(mount_plan: dict[str, Any]) -> tuple[str, ...]:
    """Non-composable singleton sections the overlay actually carries.

    Providers are a top-level list. Foundation places the orchestrator and
    context specs under ``session`` (with legacy top-level config still
    possible), so list-only module normalization cannot be used here.
    """
    present: list[str] = []
    session = mount_plan.get("session")
    session = session if isinstance(session, dict) else {}
    for section in _NON_COMPOSABLE_SECTIONS:
        if mount_plan.get(section) or session.get(section):
            present.append(section)
    return tuple(present)


def module_identity(section: str, entry: dict[str, Any]) -> str:
    """Stable live-session identity for one additive module entry.

    The coordinator's mount points are keyed by instance id/name, not by the
    whole config payload.  Mirroring that identity prevents a second bundle
    (or an explicit ``/module load``) from mounting the same live instance a
    second time and registering a duplicate cleanup.
    """
    if section == "providers":
        return f"{section}:{provider_mount_name(entry)}"
    instance = entry.get("instance_id") or entry.get("id") or entry.get("name")
    module_id = str(entry.get("module") or "").removeprefix("amplifier-module-")
    return f"{section}:{instance or module_id}"


def module_identities(mount_plan: dict[str, Any]) -> set[str]:
    """All additive module identities declared by *mount_plan*.

    The runtime seeds its live-load ledger from the boot plan with this helper,
    so a module that was mounted during session initialization is also
    idempotent when requested later.
    """
    return {
        module_identity(section, entry)
        for section in COMPOSABLE_SECTIONS
        for entry in _module_entries(mount_plan, section)
    }


def _config_targets(coordinator: Any, parent_config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Unique mutable configs that feed the live root and future children."""
    targets: list[dict[str, Any]] = []
    seen: set[int] = set()
    for candidate in (getattr(coordinator, "config", None), parent_config):
        if isinstance(candidate, dict) and id(candidate) not in seen:
            targets.append(candidate)
            seen.add(id(candidate))
    if not targets:
        raise RuntimeError("session exposes no mutable config for live inheritance")
    return targets


def _configured_identity(section: str, value: Any) -> str:
    entry = value if isinstance(value, dict) else {"module": str(value)}
    return module_identity(section, entry)


def _inherit_module_config(
    coordinator: Any,
    parent_config: dict[str, Any] | None,
    section: str,
    entry: dict[str, Any],
) -> Callable[[], None]:
    """Transactionally mirror a proven live module into child-spawn config."""
    identity = module_identity(section, entry)
    clean_entry = {key: deepcopy(value) for key, value in entry.items() if not key.startswith("_")}
    mutations: list[tuple[dict[str, Any], list[Any], int, Any, Any, bool]] = []
    seen_lists: set[int] = set()
    try:
        for config in _config_targets(coordinator, parent_config):
            raw = config.get(section)
            created = raw is None
            if created:
                raw = []
                config[section] = raw
            if not isinstance(raw, list):
                raise RuntimeError(f"session {section} config is not a list")
            if id(raw) in seen_lists:
                continue
            seen_lists.add(id(raw))
            index = next(
                (
                    i
                    for i, value in enumerate(raw)
                    if _configured_identity(section, value) == identity
                ),
                len(raw),
            )
            old = raw[index] if index < len(raw) else None
            inserted = deepcopy(clean_entry)
            if index < len(raw):
                raw[index] = inserted
            else:
                raw.append(inserted)
            mutations.append((config, raw, index, old, inserted, created))
    except Exception:
        for config, values, index, old, inserted, created in reversed(mutations):
            if index < len(values) and values[index] is inserted:
                if old is None:
                    values.pop(index)
                else:
                    values[index] = old
            if created and not values and config.get(section) is values:
                config.pop(section, None)
        raise

    def cleanup() -> None:
        for config, values, index, old, inserted, created in reversed(mutations):
            if index < len(values) and values[index] is inserted:
                if old is None:
                    values.pop(index)
                else:
                    values[index] = old
            if created and not values and config.get(section) is values:
                config.pop(section, None)

    return cleanup


def _effective_inheritance_entry(
    coordinator: Any,
    section: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Config entry future children must mount, including live enforcement."""
    inherited = deepcopy(entry)
    if section != "providers":
        return inherited
    provider = _provider_mapping(coordinator).get(provider_mount_name(entry))
    provider_config = getattr(provider, "config", None)
    if isinstance(provider_config, dict):
        inherited["config"] = deepcopy(provider_config)
    else:
        config = inherited.get("config")
        config = config if isinstance(config, dict) else {}
        config["priority"] = _provider_priority(provider)
        inherited["config"] = config
    return inherited


def _chain_cleanups(
    module_cleanup: Callable[..., Any] | None,
    config_cleanup: Callable[[], None],
) -> Callable[[], Awaitable[None]]:
    async def cleanup() -> None:
        try:
            config_cleanup()
        finally:
            await _run_cleanup(module_cleanup)

    return cleanup


def boot_module_identities(
    mount_plan: dict[str, Any],
    coordinator: Any,
    *,
    missing_tools: tuple[str, ...] = (),
) -> set[str]:
    """Successful boot mounts that should block a duplicate live mount.

    A boot plan is intent, not proof. Foundation deliberately catches an
    individual provider/tool import failure and continues when another
    provider can serve the session. Seeding every configured identity into
    the live ledger made that degraded module impossible to retry through
    ``/module`` or ``/bundle``. Providers have authoritative named mount
    slots, while the app's mount report provides the authoritative failed
    tool module ids; remove those misses and retain the conservative hook and
    agent protection used before.
    """
    identities = module_identities(mount_plan)
    mounted_providers = _provider_mapping(coordinator)
    for entry in _module_entries(mount_plan, "providers"):
        if provider_mount_name(entry) not in mounted_providers:
            identities.discard(module_identity("providers", entry))
    missing_tool_ids = set(missing_tools)
    for entry in _module_entries(mount_plan, "tools"):
        if str(entry.get("module") or "") in missing_tool_ids:
            identities.discard(module_identity("tools", entry))
    return identities


def additive_module_section(module_id: str) -> str | None:
    """Return the safe live mount point for an explicit module id.

    Explicit same-session loading is intentionally narrower than bundle
    composition: additive provider, tool, and hook modules are accepted.
    Orchestrators, contexts, source resolvers, agents, and unknown module kinds
    need a new session because their identity/lifecycle is not additive through
    the loader seam. A newly mounted provider remains idle until explicitly
    selected, so loading it never silently changes the root model.
    """
    canonical = module_id.strip().lower()
    if canonical.startswith("amplifier-module-"):
        canonical = canonical.removeprefix("amplifier-module-")
    if canonical.startswith("tool-"):
        return "tools"
    if canonical.startswith(("hook-", "hooks-")):
        return "hooks"
    if canonical.startswith("provider-"):
        return "providers"
    return None


async def _mount_one(coordinator: Any, section: str, entry: dict[str, Any]) -> _MountOutcome:
    """Instantiate + mount a single overlay module via the loader seam.

    Returns the module's cleanup and deferred-ready callback. Raises on failure
    so the caller can record the module as skipped without aborting the rest of
    the overlay."""
    loader = getattr(coordinator, "loader", None)
    if loader is None or not callable(getattr(loader, "load", None)):
        raise RuntimeError("coordinator exposes no module loader")
    module_id = str(entry["module"])
    config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    source_hint = entry.get("source")
    # loader.load(...) returns a mount function; awaiting it against the live
    # coordinator performs the actual mount and yields a cleanup callable —
    # the exact contract AmplifierSession.initialize() drives per module.
    mount_fn = loader.load(
        module_id, config=config, source_hint=source_hint, coordinator=coordinator
    )
    if isinstance(mount_fn, Awaitable):
        mount_fn = await mount_fn
    if not callable(mount_fn):
        raise RuntimeError(f"loader returned no mount function for {module_id}")
    result = mount_fn(coordinator)
    if isinstance(result, Awaitable):
        cleanup = await result
    else:
        cleanup = result
    ready_spec = getattr(mount_fn, "__on_session_ready__", None)
    on_session_ready = None
    on_session_ready_id = None
    if isinstance(ready_spec, tuple) and len(ready_spec) == 2 and callable(ready_spec[1]):
        on_session_ready = ready_spec[1]
        on_session_ready_id = str(ready_spec[0])
    del section  # the loader keys off the module's own declared mount point
    return _MountOutcome(
        cleanup=cleanup if callable(cleanup) else None,
        on_session_ready=on_session_ready,
        on_session_ready_id=on_session_ready_id,
    )


async def _rollback_provider_mount(
    coordinator: Any,
    *,
    before: Mapping[str, Any],
    cleanup: Callable[..., Any] | None,
    close_unowned: bool = False,
) -> None:
    """Restore the complete provider mapping after a rejected live mount.

    A third-party mount can overwrite the expected family slot and then raise,
    or mount under a config-selected name the caller could not predict. A
    target/default-only rollback leaves either the serving provider replaced
    or an orphan provider behind, so rollback is defined against the full
    pre-mount snapshot.
    """
    # Run a returned cleanup before restoring old identities. Provider cleanup
    # implementations are allowed to assume their self-mounted family slot
    # still refers to their own instance.
    try:
        await _run_cleanup(cleanup)
    except Exception:  # noqa: BLE001 - preserve the primary failure
        logger.warning("failed live provider cleanup raised", exc_info=True)

    current = dict(_provider_mapping(coordinator))
    old_object_ids = {id(provider) for provider in before.values()}
    unowned: list[Any] = []
    seen_unowned_ids: set[int] = set()
    for provider in current.values():
        provider_id = id(provider)
        if (
            close_unowned
            and provider_id not in old_object_ids
            and provider_id not in seen_unowned_ids
        ):
            unowned.append(provider)
            seen_unowned_ids.add(provider_id)

    # A mount that raised never returned its cleanup. Close any newly observed
    # provider directly when it exposes the conventional provider lifecycle.
    for provider in unowned:
        close = getattr(provider, "close", None)
        if not callable(close):
            continue
        try:
            await _maybe_await(close())
        except Exception:  # noqa: BLE001 - preserve the primary mount failure
            logger.warning("failed live provider close raised", exc_info=True)

    # Rebuild every preexisting slot in snapshot order. Merely replacing the
    # changed key appends it to an ordered mapping; equal-priority selection is
    # stable, so that subtle order change can switch the serving provider even
    # though rollback appears to have restored all names and objects.
    await _restore_provider_mapping(coordinator, before)


async def _mount_provider(coordinator: Any, entry: dict[str, Any]) -> _MountOutcome:
    """Mount a provider without replacing or implicitly selecting one.

    Provider modules self-mount under a family default (``anthropic``,
    ``vllm``...). Foundation's boot path snapshots that slot and remaps an
    ``id``/``instance_id`` entry afterwards. Live composition must perform the
    same transaction explicitly; otherwise adding ``vllm`` instance ``runpod``
    overwrites an existing default ``vllm`` provider.
    """
    module_id = str(entry["module"])
    target_name = provider_mount_name(entry)
    before = dict(_provider_mapping(coordinator))
    if not target_name:
        raise RuntimeError(f"provider {module_id!r} has no stable mount identity")
    if target_name in before:
        raise RuntimeError(f"provider identity {target_name!r} is already mounted")

    try:
        outcome = await _mount_one(coordinator, "providers", entry)
    except Exception:
        await _rollback_provider_mount(
            coordinator,
            before=before,
            cleanup=None,
            close_unowned=True,
        )
        raise
    after_mount = dict(_provider_mapping(coordinator))
    old_object_ids = {id(provider) for provider in before.values()}
    new_slots = [
        name for name, provider in after_mount.items() if id(provider) not in old_object_ids
    ]
    new_providers = {
        id(provider): provider
        for provider in after_mount.values()
        if id(provider) not in old_object_ids
    }
    if len(new_providers) != 1 or len(new_slots) != 1:
        await _rollback_provider_mount(
            coordinator,
            before=before,
            cleanup=outcome.cleanup,
        )
        raise RuntimeError(
            f"provider module {module_id!r} mounted {len(new_slots)} new slots / "
            f"{len(new_providers)} providers; expected exactly 1 of each"
        )
    mounted_provider = next(iter(new_providers.values()))

    try:
        # Normalize its one family-default or config-selected slot into exactly
        # one collision-checked target while restoring every prior identity
        # and its ordering. Multiple slots are rejected above: compliant
        # modules must mount one provider and return complete cleanup for any
        # other coordinator side effects they create.
        expected = dict(before)
        expected[target_name] = mounted_provider
        await _restore_provider_mapping(coordinator, expected)

        # A live provider is inventory until /model explicitly selects it.
        # Put it behind every provider that was already serving this session,
        # regardless of a bundle-supplied priority that would otherwise take
        # over the next root turn silently.
        if before:
            idle_priority = max(_provider_priority(provider) for provider in before.values()) + 1
            try:
                mounted_provider.priority = idle_priority
            except Exception:  # noqa: BLE001 - config-backed providers remain supported
                pass
            provider_config = getattr(mounted_provider, "config", None)
            if isinstance(provider_config, dict):
                provider_config["priority"] = idle_priority
            if _provider_priority(mounted_provider) != idle_priority:
                raise RuntimeError("new provider exposes a read-only priority and cannot stay idle")

        current = _provider_mapping(coordinator)
        if not _provider_snapshot_matches(current, expected):
            raise RuntimeError(
                f"provider {module_id!r} did not retain exactly identity {target_name!r}"
            )
    except Exception:
        await _rollback_provider_mount(
            coordinator,
            before=before,
            cleanup=outcome.cleanup,
        )
        raise

    raw_cleanup = outcome.cleanup

    async def cleanup() -> None:
        # Protect the complete identity and ordering that exists when teardown
        # starts, not merely the default that existed at live-mount time.
        # Another legitimate action may have replaced that family default or
        # appended a provider in the interim.
        cleanup_snapshot = dict(_provider_mapping(coordinator))
        if cleanup_snapshot.get(target_name) is mounted_provider:
            cleanup_snapshot.pop(target_name)
        try:
            current = _provider_mapping(coordinator)
            if current.get(target_name) is mounted_provider:
                await _maybe_await(coordinator.unmount("providers", name=target_name))
        finally:
            try:
                await _run_cleanup(raw_cleanup)
            finally:
                # Raw provider cleanup may unmount the family-default name.
                # Rebuild only when needed, preserving cleanup-time identities
                # and order while intentionally omitting this live instance.
                await _restore_provider_mapping(coordinator, cleanup_snapshot)

    return _MountOutcome(
        cleanup=cleanup,
        on_session_ready=outcome.on_session_ready,
        on_session_ready_id=outcome.on_session_ready_id,
    )


async def _mount_agent(
    coordinator: Any,
    entry: dict[str, Any],
    parent_config: dict[str, Any] | None,
) -> _MountOutcome:
    """Merge one Foundation agent definition into live coordinator config."""
    name = str(entry.get("name") or "").strip()
    definition = entry.get("config")
    if not name or not isinstance(definition, dict):
        raise RuntimeError("invalid live agent definition")
    rosters: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
    seen_rosters: set[int] = set()
    for config in _config_targets(coordinator, parent_config):
        raw = config.get("agents")
        created = raw is None
        if created:
            raw = {}
        if not isinstance(raw, dict):
            raise RuntimeError("coordinator agents config is not a mapping")
        if name in raw:
            raise RuntimeError(f"agent identity {name!r} is already configured")
        if id(raw) not in seen_rosters:
            rosters.append((config, raw, created))
            seen_rosters.add(id(raw))

    mounted: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]] = []
    try:
        for config, roster, created in rosters:
            if created:
                config["agents"] = roster
            mounted_definition = deepcopy(definition)
            roster[name] = mounted_definition
            mounted.append((config, roster, mounted_definition, created))
    except Exception:
        for config, roster, mounted_definition, created in reversed(mounted):
            if roster.get(name) is mounted_definition:
                roster.pop(name, None)
            if created and not roster and config.get("agents") is roster:
                config.pop("agents", None)
        raise

    def cleanup() -> None:
        for config, roster, mounted_definition, created in reversed(mounted):
            if roster.get(name) is mounted_definition:
                roster.pop(name, None)
            if created and not roster and config.get("agents") is roster:
                config.pop("agents", None)

    return _MountOutcome(cleanup=cleanup)


async def _dispatch_ready_callbacks(coordinator: Any, callbacks: list[_ReadyCallback]) -> None:
    """Run live lifecycle hooks after the entire additive batch is present.

    This mirrors Amplifier Core's phase-6 contract. Callback failures are
    non-fatal, are logged, and emit the same lifecycle-failure event when the
    coordinator hook bus is available. The callback's return value is ignored:
    the core contract is ``async (...) -> None`` and teardown remains owned by
    the cleanup returned from ``mount()``.
    """
    for ready in callbacks:
        try:
            await _maybe_await(ready.callback(coordinator))
        except Exception as error:  # noqa: BLE001 - Foundation lifecycle parity
            logger.warning(
                "on_session_ready for live module %s raised",
                ready.module_id,
                exc_info=True,
            )
            hooks = getattr(coordinator, "hooks", None)
            emit = getattr(hooks, "emit", None)
            if callable(emit):
                try:
                    await _maybe_await(
                        emit(
                            "module:on_session_ready_failed",
                            {"module_id": ready.module_id, "error": str(error)},
                        )
                    )
                except Exception:  # noqa: BLE001 - never hide the original failure
                    pass


async def mount_overlay_modules(
    coordinator: Any,
    mount_plan: dict[str, Any],
    *,
    seen: set[str] | None = None,
    bundle_content_deferred: bool = False,
    parent_config: dict[str, Any] | None = None,
) -> ComposeResult:
    """Mount an overlay's additive modules onto a live coordinator.

    Iterates :data:`COMPOSABLE_SECTIONS` and mounts each module through the
    loader seam (:func:`_mount_one`). Best-effort per module: one module that
    fails to mount is recorded in ``skipped`` and never aborts the rest.
    Non-composable sections the overlay carries are reported in
    ``deferred_sections`` (honest boundary — they attach fully at the next
    boot). Lifecycle callbacks run only after the entire additive batch is
    present, matching Foundation's session-ready phase. ``ok`` is True when
    at least one module mounted or the overlay had nothing composable to mount
    and nothing failed."""
    mounted: list[str] = []
    already_mounted: list[str] = []
    skipped: list[str] = []
    cleanups: list[Callable[..., Any]] = []
    ready_callbacks: list[_ReadyCallback] = []
    for section in COMPOSABLE_SECTIONS:
        for entry in _module_entries(mount_plan, section):
            module_id = str(entry["module"])
            identity = module_identity(section, entry)
            if seen is not None and identity in seen:
                already_mounted.append(module_id)
                continue
            try:
                if entry.get("_agent_definition") is True:
                    outcome = await _mount_agent(coordinator, entry, parent_config)
                elif section == "providers":
                    outcome = await _mount_provider(coordinator, entry)
                else:
                    outcome = await _mount_one(coordinator, section, entry)
                if entry.get("_agent_definition") is not True:
                    try:
                        config_cleanup = _inherit_module_config(
                            coordinator,
                            parent_config,
                            section,
                            _effective_inheritance_entry(coordinator, section, entry),
                        )
                    except Exception:
                        try:
                            await _run_cleanup(outcome.cleanup)
                        except Exception:  # noqa: BLE001 - retain config failure
                            logger.warning(
                                "live module cleanup after config failure raised",
                                exc_info=True,
                            )
                        raise
                    outcome.cleanup = _chain_cleanups(outcome.cleanup, config_cleanup)
            except Exception:  # noqa: BLE001 — one bad module never aborts the overlay
                logger.warning("overlay module %s failed to mount", module_id, exc_info=True)
                skipped.append(module_id)
                continue
            mounted.append(module_id)
            if seen is not None:
                seen.add(identity)
            if outcome.cleanup is not None:
                cleanups.append(outcome.cleanup)
            if outcome.on_session_ready is not None:
                ready_callbacks.append(
                    _ReadyCallback(
                        outcome.on_session_ready_id or module_id,
                        outcome.on_session_ready,
                    )
                )
    await _dispatch_ready_callbacks(coordinator, ready_callbacks)
    deferred_sections = list(_non_composable_present(mount_plan))
    if bundle_content_deferred:
        # prepare_overlay_bundle currently returns only PreparedBundle.mount_plan;
        # the root instruction/context content on PreparedBundle.bundle is not
        # available to inject safely. Say so explicitly rather than claiming a
        # behavior-only bundle was live-loaded when its model instructions were
        # dropped.
        deferred_sections.append("bundle instructions/context")
    ok = bool(mounted or already_mounted) or (not skipped and not deferred_sections)
    return ComposeResult(
        ok=ok,
        mounted=tuple(mounted),
        already_mounted=tuple(already_mounted),
        skipped=tuple(skipped),
        deferred_sections=tuple(deferred_sections),
        cleanups=cleanups,
    )


async def mount_additive_module(
    coordinator: Any,
    module_id: str,
    *,
    source_hint: str | None = None,
    config: dict[str, Any] | None = None,
    seen: set[str] | None = None,
    parent_config: dict[str, Any] | None = None,
) -> ComposeResult:
    """Mount one explicit additive tool/hook module into a live session.

    The public loader seam is the same one used by bundle composition.  The
    function refuses every singleton/unknown module kind before loading and
    shares the caller's ledger so repeating a request is a successful no-op.
    """
    target = module_id.strip()
    section = additive_module_section(target)
    if not target or section is None:
        return ComposeResult(
            ok=False,
            message=(
                "only additive provider-/tool-/hook- modules load live; "
                "orchestrators, contexts, agents, and unknown modules "
                "attach at next session start"
            ),
        )
    entry: dict[str, Any] = {"module": target, "config": dict(config or {})}
    if source_hint:
        entry["source"] = source_hint
    identity = module_identity(section, entry)
    if seen is not None and identity in seen:
        return ComposeResult(ok=True, already_mounted=(target,))
    try:
        outcome = (
            await _mount_provider(coordinator, entry)
            if section == "providers"
            else await _mount_one(coordinator, section, entry)
        )
        try:
            config_cleanup = _inherit_module_config(
                coordinator,
                parent_config,
                section,
                _effective_inheritance_entry(coordinator, section, entry),
            )
        except Exception:
            try:
                await _run_cleanup(outcome.cleanup)
            except Exception:  # noqa: BLE001 - retain config failure
                logger.warning(
                    "live module cleanup after config failure raised",
                    exc_info=True,
                )
            raise
        outcome.cleanup = _chain_cleanups(outcome.cleanup, config_cleanup)
    except Exception as error:  # noqa: BLE001 — surfaced to /module, never tears down the UI
        logger.warning("live module %s failed to mount", target, exc_info=True)
        return ComposeResult(
            ok=False,
            skipped=(target,),
            message=f"could not load '{target}': {error or type(error).__name__}",
        )
    if seen is not None:
        seen.add(identity)
    if outcome.on_session_ready is not None:
        await _dispatch_ready_callbacks(
            coordinator,
            [
                _ReadyCallback(
                    outcome.on_session_ready_id or target,
                    outcome.on_session_ready,
                )
            ],
        )
    return ComposeResult(
        ok=True,
        mounted=(target,),
        cleanups=[outcome.cleanup] if outcome.cleanup is not None else [],
    )


__all__ = [
    "COMPOSABLE_SECTIONS",
    "ComposeResult",
    "additive_module_section",
    "boot_module_identities",
    "module_identities",
    "module_identity",
    "mount_additive_module",
    "mount_overlay_modules",
    "provider_mount_name",
]
