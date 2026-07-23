"""In-process subagent spawner (ADR-0007 resolution 7 — v1 is in-process only).

Implements the app side of the ``session.spawn`` capability contract as
foundation's tool-delegate actually calls it (ground truth:
``amplifier_module_tool_delegate._spawn_new_session`` passes ``agent_name``,
``instruction``, ``parent_session``, ``agent_configs``, ``sub_session_id``,
``tool_inheritance``, ``hook_inheritance``, ``orchestrator_config``,
``provider_preferences``, ``self_delegation_depth``, ``session_metadata``).
Reference implementation: amplifier-app-cli ``session_spawner.py`` +
``runtime/session_spawn_config.py`` + ``runtime/session_spawn_inprocess.py``.

On every spawn it:

1. enforces recursion depth (default 2) BEFORE creating anything — the
   kernel documents but does not implement depth limiting;
2. merges the agent overlay into the parent config with the reference
   semantics (module lists merge by module id, dicts merge deep, scalars
   override) so an agent's partial ``session`` overlay never wipes the
   parent's streaming orchestrator;
3. honors ``tool_inheritance`` / ``hook_inheritance`` (exclusions apply to
   inheritance only; agent-declared modules are always kept) and merges
   ``orchestrator_config`` into ``session.orchestrator.config``;
4. inherits the parent's ``module-source-resolver`` mount and
   ``session.working_dir`` capability BEFORE ``initialize()`` — without
   the resolver a child whose config carries ``git+``/``file:`` module
   sources cannot mount anything (the loader falls back to entry-point
   discovery only) and the spawn dies before a single event fires;
5. creates the child session with the parent's approval/display systems
   (ephemeral hooks do NOT propagate to children — inheritance must be
   explicit);
6. re-attaches the shared tracker set to the child coordinator's hooks so
   lanes/telemetry stay lit (the "subagent lanes going dark" risk) — the
   child's own ``hooks.set_default_fields`` stamps its session_id on
   every payload, so bridged events arrive child-stamped;
7. registers the child's cancellation with the parent's so esc-interrupt
   reaches the whole tree;
8. registers itself on the child so grandchildren spawn through the same
   depth-enforced path;
9. records the delegate brief (lane seeding) and the child's final output
   (``delegate:agent_completed`` carries no ``result`` field — verified
   against the pinned tool-delegate module — so the app synthesizes the
   snippet from the output captured here);
10. always unwinds (tracker unregistration, cancellation unlink, cleanup)
    in ``finally``.

Everything is duck-typed against the amplifier-core session surface
(``.coordinator``, ``.initialize()``, ``.execute()``, ``.cleanup()``) so
tests drive it with fakes; amplifier-core/foundation imports stay lazy.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Sequence
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SPAWN_CAPABILITY = "session.spawn"
DEPTH_CAPABILITY = "newtui.spawn_depth"
DEFAULT_MAX_DEPTH = 2

_MEMO_MAX = 64
"""Bound on remembered briefs/results — fan-outs are small; never grow."""

_BRIEF_MAX_CHARS = 80
_RESULT_MAX_CHARS = 240


class Tracker(Protocol):
    """A hook surface the spawner re-attaches to child coordinators.

    Both the telemetry trackers (lanes/cost stay lit on children) and the
    app's trust ``GovernanceHook`` share this one shape: ``register_hooks``
    installs handlers on a coordinator's hook bus and returns the unregister
    callback the spawner runs on unwind.
    """

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
        governance_hook: Tracker | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        id_generator: Callable[[str, str], str] = generate_sub_session_id,
    ) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        self._session_factory = session_factory or _default_session_factory
        self._trackers = tuple(trackers)
        self._approval_system = approval_system
        self._display_system = display_system
        self._governance_hook = governance_hook
        self._max_depth = max_depth
        self._id_generator = id_generator
        self._briefs: dict[str, str] = {}
        """Latest delegate brief per agent name — the lane-seed activity
        line. Keyed by agent name because ``AgentSpawned`` reaches the
        adapter before any child-stamped event does; a same-named
        parallel fan-out may show the sibling's brief until the child's
        own activity ticker takes over."""
        self._results: dict[str, str] = {}
        """Child final output per sub-session id — the source for the
        synthesized ``AgentCompleted.result`` (tool-delegate's completion
        payload has no result field)."""

    def register(self, coordinator: Any) -> None:
        """Install this spawner as the coordinator's ``session.spawn``
        capability — MUST run after ``create_session`` and before
        ``execute`` (integration-guide timing contract)."""
        coordinator.register_capability(SPAWN_CAPABILITY, self.spawn)

    def set_governance_hook(self, hook: Tracker | None) -> None:
        """Attach the app's trust ``GovernanceHook`` so child lanes inherit
        the TUI's posture gating (issue #38: children bypassed it — native
        approval inheritance applied, but careful/plan never reached lanes).

        The runtime builds the hook AFTER this spawner (it needs the root
        session id), so the wire-up is post-construction. The same instance
        is re-registered on every child coordinator's hook bus at spawn time
        and torn down on unwind — one live ``mode()`` source, so a mode
        change in the root gates in-flight lanes with no session teardown.
        """
        self._governance_hook = hook

    def brief_for(self, agent_name: str) -> str:
        """The latest recorded delegate brief for *agent_name* ("" unknown)."""
        return self._briefs.get(agent_name, "")

    def result_for(self, sub_session_id: str) -> str:
        """The recorded final output summary for a child ("" unknown)."""
        return self._results.get(sub_session_id, "")

    async def spawn(
        self,
        agent_name: str,
        instruction: str,
        parent_session: Any,
        agent_configs: dict[str, dict[str, Any]] | None = None,
        sub_session_id: str | None = None,
        tool_inheritance: dict[str, Any] | None = None,
        hook_inheritance: dict[str, Any] | None = None,
        orchestrator_config: dict[str, Any] | None = None,
        parent_messages: list[dict[str, Any]] | None = None,
        provider_preferences: list[Any] | None = None,
        model_role: str | list[str] | None = None,
        self_delegation_depth: int = 0,
        session_metadata: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Spawn, execute, persist-nothing, and unwind one child session.

        The keyword surface is tool-delegate's spawn contract verbatim
        (module docstring); ``**_kwargs`` absorbs only future additions.
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

        child_id = sub_session_id or self._id_generator(str(parent_session.session_id), agent_name)
        overlay = _agent_overlay(agent_configs, agent_name)
        config = _merged_config(parent_session, overlay)
        _apply_inheritance_filter(config, "tools", tool_inheritance, overlay.get("tools"))
        _apply_inheritance_filter(config, "hooks", hook_inheritance, overlay.get("hooks"))
        if orchestrator_config:
            _apply_orchestrator_override(config, orchestrator_config)
        if session_metadata:
            _apply_session_metadata(config, session_metadata)
        # Model routing (hooks-routing): apply per-role provider preferences to
        # the child's mount plan so a delegated agent runs on its role's model.
        # Explicit prefs win; else resolve model_role via the capability; else
        # any prefs the routing hook wrote onto the agent config. Best-effort:
        # a single-provider setup or missing resolver leaves the child on the
        # parent provider (apply_* skips unmounted providers). Never raises.
        config = await _apply_routing(config, parent_coordinator, provider_preferences, model_role)
        approval_system = self._approval_system or getattr(
            parent_coordinator, "approval_system", None
        )
        display_system = self._display_system or getattr(parent_coordinator, "display_system", None)
        _remember(self._briefs, agent_name, _brief(instruction))
        child = self._session_factory(
            config=config,
            session_id=child_id,
            parent_id=parent_session.session_id,
            approval_system=approval_system,
            display_system=display_system,
        )
        child_coordinator = child.coordinator
        # Module resolution + working dir must exist BEFORE initialize():
        # modules resolve sources and read the cwd capability while mounting
        # (reference: session_spawn_inprocess.py, PreparedBundle.spawn).
        await _inherit_module_resolver(parent_coordinator, child_coordinator)
        _inherit_capabilities(parent_coordinator, child_coordinator, ("session.working_dir",))
        await child.initialize()

        unregisters: list[Callable[[], None]] = []
        hooks = child_coordinator.get("hooks")
        if hooks is not None:
            # Governance first, high precedence: the child lane inherits the
            # root's live trust posture so a gated mode (plan/careful) blocks
            # the SAME actions in the lane as in the root (issue #38). Native
            # approval inheritance already flowed; the TUI's own posture did
            # not. Registered before the telemetry trackers so it settles a
            # tool:pre before any display hook paints it.
            if self._governance_hook is not None:
                unregisters.append(self._governance_hook.register_hooks(hooks))
            for tracker in self._trackers:
                unregisters.append(tracker.register_hooks(hooks))
        child_coordinator.register_capability(DEPTH_CAPABILITY, depth)
        child_coordinator.register_capability(SPAWN_CAPABILITY, self.spawn)
        # tool-delegate reads this in the child for its own depth limiting.
        child_coordinator.register_capability("self_delegation_depth", self_delegation_depth)
        _inherit_capabilities(
            parent_coordinator,
            child_coordinator,
            ("mention_resolver", "mention_deduplicator"),
        )
        # Runtime skill overlays (issue #38): skills loaded into the root at
        # runtime live under the parent coordinator's runtime_skill_overlay
        # capability; copy the list onto the child so a delegated agent sees
        # the same runtime-loaded skills (reference: session_spawn_inprocess).
        _inherit_skill_overlays(parent_coordinator, child_coordinator)
        await _seed_child_context(child_coordinator, overlay, parent_messages)

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
            if cancellation_linked and parent_cancellation is not None:
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

        summary = _result_summary(output)
        if summary:
            _remember(self._results, child_id, summary)
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


def _agent_overlay(
    agent_configs: dict[str, dict[str, Any]] | None, agent_name: str
) -> dict[str, Any]:
    overlay = (agent_configs or {}).get(agent_name)
    return dict(overlay) if isinstance(overlay, dict) else {}


def _merged_config(parent_session: Any, overlay: dict[str, Any]) -> dict[str, Any]:
    """Parent config + agent overlay, reference merge semantics.

    Mirrors amplifier-app-cli ``merge_agent_dicts`` via foundation's own
    dict primitives: ``tools``/``hooks``/``providers`` merge by module id,
    dict values merge deep (an agent's partial ``session`` overlay keeps
    the parent's streaming orchestrator), scalars override.
    """
    parent_config = getattr(parent_session, "config", None)
    merged: dict[str, Any] = dict(parent_config) if isinstance(parent_config, dict) else {}
    for key, value in overlay.items():
        current = merged.get(key)
        if (
            key in ("tools", "hooks", "providers")
            and isinstance(current, list)
            and isinstance(value, list)
        ):
            merged[key] = _merge_module_lists(current, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _deep_merge(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    from amplifier_foundation.dicts import deep_merge

    return deep_merge(parent, child)


def _merge_module_lists(parent: list[Any], child: list[Any]) -> list[dict[str, Any]]:
    from amplifier_foundation.dicts import merge_module_lists

    def normalized(entries: list[Any]) -> list[dict[str, Any]]:
        # Agent frontmatter allows bare-string shorthand (tools: [tool-x]).
        result: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, dict):
                result.append(entry)
            elif isinstance(entry, str):
                result.append({"module": entry})
        return result

    return merge_module_lists(normalized(parent), normalized(child))


def _module_id(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("id") or entry.get("module") or "")
    return str(entry)


def _apply_inheritance_filter(
    config: dict[str, Any],
    section: str,
    inheritance: dict[str, Any] | None,
    agent_declared: Any,
) -> None:
    """Reference ``filter_tools``/``filter_hooks`` semantics: allow/block
    lists apply to INHERITANCE only; agent-declared modules always stay."""
    if not isinstance(inheritance, dict) or not inheritance:
        return
    entries = config.get(section)
    if not isinstance(entries, list) or not entries:
        return
    explicit = {
        _module_id(entry) for entry in (agent_declared if isinstance(agent_declared, list) else ())
    }
    inherit = inheritance.get(f"inherit_{section}")
    exclude = inheritance.get(f"exclude_{section}") or ()
    if isinstance(inherit, list):
        kept = [e for e in entries if _module_id(e) in inherit or _module_id(e) in explicit]
    elif exclude:
        kept = [e for e in entries if _module_id(e) not in exclude or _module_id(e) in explicit]
    else:
        return
    config[section] = kept


def _apply_orchestrator_override(config: dict[str, Any], override: dict[str, Any]) -> None:
    """Merge tool-delegate's inherited orchestrator config into the child's
    ``session.orchestrator.config`` (reference ``_apply_orchestrator_override``,
    PreparedBundle.spawn). Rebuilds the nested dicts — ``config["session"]``
    may still be the parent session's own object after a shallow merge."""
    session_cfg = dict(config.get("session") or {})
    orchestrator = session_cfg.get("orchestrator")
    orchestrator = dict(orchestrator) if isinstance(orchestrator, dict) else {}
    orch_config = orchestrator.get("config")
    orchestrator["config"] = {
        **(orch_config if isinstance(orch_config, dict) else {}),
        **override,
    }
    session_cfg["orchestrator"] = orchestrator
    config["session"] = session_cfg


def _apply_session_metadata(config: dict[str, Any], metadata: dict[str, Any]) -> None:
    """``session.metadata`` carries agent_name/tool_call_id/parallel_group_id
    for the child (reference: session_spawn_config.prepare_spawn)."""
    session_cfg = dict(config.get("session") or {})
    session_cfg["metadata"] = dict(metadata)
    config["session"] = session_cfg


async def _inherit_module_resolver(parent_coordinator: Any, child_coordinator: Any) -> None:
    """Mount the parent's ``module-source-resolver`` on the child.

    Foundation mounts a BundleModuleResolver on the root session; the
    child's config references the same ``git+``/``file:`` sources, and
    without the resolver amplifier-core's loader can only do entry-point
    discovery — the child fails to mount its orchestrator/provider and no
    telemetry ever fires. Must run BEFORE ``initialize()``.
    """
    get: Any = getattr(parent_coordinator, "get", None)
    mount: Any = getattr(child_coordinator, "mount", None)
    if not callable(get) or not callable(mount):
        return
    try:
        resolver = get("module-source-resolver")
    except Exception:
        resolver = None
    if resolver is None:
        return
    try:
        mounted = mount("module-source-resolver", resolver)
        if asyncio.iscoroutine(mounted):
            await mounted
    except Exception:
        logger.debug("module resolver inheritance failed", exc_info=True)


def _inherit_capabilities(
    parent_coordinator: Any, child_coordinator: Any, names: Sequence[str]
) -> None:
    """Copy parent coordinator capabilities the reference spawner shares."""
    get_capability = getattr(parent_coordinator, "get_capability", None)
    register = getattr(child_coordinator, "register_capability", None)
    if not callable(get_capability) or not callable(register):
        return
    for name in names:
        try:
            value = get_capability(name)
        except Exception:
            value = None
        if value is None:
            continue
        try:
            register(name, value)
        except Exception:
            logger.debug("capability inheritance failed: %s", name, exc_info=True)


def _inherit_skill_overlays(parent_coordinator: Any, child_coordinator: Any) -> None:
    """Copy the parent's runtime skill-overlay list onto the child.

    Skills loaded into the root at runtime are recorded on the parent
    coordinator under foundation's ``runtime_skill_overlay`` capability (a
    ``list[str]`` of skill keys/paths). The child config is a static mount
    plan, so without this a delegated agent never sees a skill the user
    loaded mid-run. Copies the list by value (never shares the parent's
    mutable list) and registers it before ``execute`` so tool-skills /
    hooks-mode read it while the child runs. Best-effort — never raises.
    Reference: amplifier-app-cli ``session_spawn_inprocess.py``.
    """
    from amplifier_foundation import RUNTIME_SKILL_OVERLAY_CAPABILITY

    get_capability = getattr(parent_coordinator, "get_capability", None)
    register = getattr(child_coordinator, "register_capability", None)
    if not callable(get_capability) or not callable(register):
        return
    overlays: Any = None
    try:
        overlays = get_capability(RUNTIME_SKILL_OVERLAY_CAPABILITY)
    except Exception:
        overlays = None
    if not overlays:
        return
    try:
        register(RUNTIME_SKILL_OVERLAY_CAPABILITY, list(overlays))
    except Exception:
        logger.debug("skill overlay inheritance failed", exc_info=True)


async def _seed_child_context(
    child_coordinator: Any,
    overlay: dict[str, Any],
    parent_messages: list[dict[str, Any]] | None,
) -> None:
    """Inherited context + the agent's persona as its system prompt.

    The agent overlay's ``instruction`` (the agent .md body) is the child's
    system prompt — this spawner builds the child from a merged mount plan,
    so foundation's system-prompt factory never runs for it (reference:
    session_spawn_inprocess.py injects it the same way). tool-delegate
    folds parent context into the instruction and never passes
    ``parent_messages``; honored anyway per the reference signature.
    """
    get: Any = getattr(child_coordinator, "get", None)
    if not callable(get):
        return
    context: Any = None
    try:
        context = get("context")
    except Exception:
        context = None
    if context is None:
        return
    if parent_messages and hasattr(context, "set_messages"):
        await context.set_messages([dict(m) for m in parent_messages])
    instruction = overlay.get("instruction")
    if instruction and hasattr(context, "add_message"):
        await context.add_message({"role": "system", "content": str(instruction)})


def _brief(instruction: str) -> str:
    """Lane-seed activity line from the delegate instruction.

    tool-delegate prepends inherited history and marks the task with
    ``[YOUR TASK]`` — the brief is the task text itself, one line, capped.
    """
    text = instruction or ""
    marker = "[YOUR TASK]"
    if marker in text:
        text = text.split(marker, 1)[1]
    text = " ".join(text.split())
    if len(text) > _BRIEF_MAX_CHARS:
        text = text[: _BRIEF_MAX_CHARS - 1].rstrip() + "…"
    return text


def _result_summary(output: Any) -> str:
    """Single-line snippet of the child's final output (delegate summary)."""
    text = " ".join(str(output or "").split())
    if len(text) > _RESULT_MAX_CHARS:
        text = text[: _RESULT_MAX_CHARS - 1].rstrip() + "…"
    return text


def _remember(store: dict[str, str], key: str, value: str) -> None:
    if not key:
        return
    store.pop(key, None)
    store[key] = value
    while len(store) > _MEMO_MAX:
        store.pop(next(iter(store)))


def _as_preferences(raw: Any) -> list[Any]:
    """Coerce provider-preference dicts/objects into ``ProviderPreference``s."""
    from amplifier_foundation.spawn_utils import ProviderPreference

    prefs: list[Any] = []
    for item in raw or ():
        if isinstance(item, ProviderPreference):
            prefs.append(item)
        elif isinstance(item, dict) and item.get("provider") and item.get("model"):
            prefs.append(
                ProviderPreference(
                    provider=str(item["provider"]),
                    model=str(item["model"]),
                    config=item.get("config") or {},
                )
            )
    return prefs


async def _apply_routing(
    config: dict[str, Any],
    parent_coordinator: Any,
    provider_preferences: list[Any] | None,
    model_role: str | list[str] | None,
) -> dict[str, Any]:
    """Apply per-role model routing to a child mount plan (best-effort).

    Resolution order: explicit ``provider_preferences`` → ``model_role`` via
    the ``model_role_resolver`` capability → any ``provider_preferences`` the
    routing hook wrote onto the (merged) agent config. Applies via foundation's
    ``apply_provider_preferences_with_resolution``, which returns a NEW mount
    plan (skips unmounted providers, so single-provider setups degrade to the
    parent model). Swallows all errors — routing must never break a spawn."""
    try:
        prefs = provider_preferences
        if not prefs and model_role:
            resolver = None
            try:
                resolver = parent_coordinator.get_capability("model_role_resolver")
            except Exception:  # noqa: BLE001 — capability registry variance
                resolver = None
            if resolver is not None:
                prefs = await resolver.resolve(model_role)
        if not prefs:
            prefs = config.get("provider_preferences")
        coerced = _as_preferences(prefs)
        if not coerced:
            return config
        from amplifier_foundation.spawn_utils import (
            apply_provider_preferences_with_resolution,
        )

        return await apply_provider_preferences_with_resolution(config, coerced, parent_coordinator)
    except Exception:  # noqa: BLE001 — routing is best-effort; never break spawn
        logger.debug("routing application failed for spawn", exc_info=True)
        return config


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEPTH_CAPABILITY",
    "SPAWN_CAPABILITY",
    "SessionSpawner",
    "generate_sub_session_id",
]
