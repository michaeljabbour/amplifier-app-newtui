"""In-session operations over the amplifier coordinator.

The interactive slash commands ``/model``, ``/effort``, ``/compact``,
``/clear``, ``/status``, ``/tools`` and ``/agents`` act on the LIVE
session. amplifier-app-cli implements them in ``CoreCommandService`` /
``CommandSessionMixin`` against the amplifier-core coordinator surface;
this module is the port onto the SAME surface that
:class:`~amplifier_app_tui.kernel.runtime.RealRuntime` already holds
(``coordinator.get(...)`` / ``get_capability(...)`` / ``session_state`` /
``session_id``).

Everything here is a plain async function over a duck-typed coordinator
so it unit-tests with a ``SimpleNamespace`` fake — no Textual, no runtime
thread. Functions never raise into the UI: a missing mechanism returns a
``(False, reason)`` tuple or an empty listing, never an exception.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

# amplifier-app-cli's ``_EFFORTS`` plus the ``max`` alias it accepts.
EFFORT_LEVELS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh")
_EFFORT_ALIASES = {"max": "xhigh"}


async def _maybe_await(value: Any) -> Any:
    """Await *value* when it is awaitable, else return it as-is.

    Coordinator mechanisms are duck-typed: ``provider.list_models`` and
    ``context.compact`` are sync in some modules and coroutines in
    others (found across amplifier provider/context modules)."""
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class ModelListing:
    """Current model + the models each mounted provider advertises."""

    provider: str
    current: str
    available: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatusInfo:
    """The coordinator-derived half of ``/status`` (the app adds mode/cost)."""

    session_id: str = ""
    provider: str = ""
    model: str = ""
    effort: str | None = None
    messages: int = 0
    tools: int = 0
    agents: tuple[str, ...] = field(default_factory=tuple)


def _tool(coordinator: Any, name: str) -> Any:
    """A mounted tool object by name, or ``None``."""
    try:
        tools = coordinator.get("tools")
    except Exception:  # noqa: BLE001
        return None
    return tools.get(name) if isinstance(tools, dict) else None


def _providers(coordinator: Any) -> dict[str, Any]:
    try:
        providers = coordinator.get("providers")
    except Exception:  # noqa: BLE001 — duck-typed coordinator
        return {}
    return providers if isinstance(providers, dict) else {}


def _provider_priority(provider: Any) -> int:
    """A mounted provider's selection priority — lower wins, absent means 100.

    Reads the same two places the orchestrator reads
    (``loop-streaming::_select_provider``): the ``priority`` attribute the
    provider stashes at construction, else its ``config`` dict.
    """
    priority = getattr(provider, "priority", None)
    if isinstance(priority, int) and not isinstance(priority, bool):
        return priority
    config = getattr(provider, "config", None)
    if isinstance(config, dict):
        priority = config.get("priority", 100)
        if isinstance(priority, int) and not isinstance(priority, bool):
            return priority
    return 100


def _primary_provider(coordinator: Any) -> tuple[str, Any]:
    """The mounted provider that will serve the turn, or ``("", None)``.

    Lowest priority wins — matching ``loop-streaming::_select_provider``
    exactly. Mount ORDER is not the rule: it follows the mount plan, whose
    index 0 is pinned to the bundle-declared provider, so "first mounted"
    made ``/model`` and ``/status`` report (and mutate) a provider that was
    not the one answering. Ties fall back to mount order, which is stable.
    """
    providers = _providers(coordinator)
    if not providers:
        return ("", None)
    name = min(providers, key=lambda key: _provider_priority(providers[key]))
    return (str(name), providers[name])


def _model_ids(models: Any) -> tuple[str, ...]:
    """Best-effort model-id extraction from a ``list_models()`` result."""
    ids: list[str] = []
    for model in models or ():
        ident = (
            getattr(model, "id", None)
            or getattr(model, "name", None)
            or (model if isinstance(model, str) else None)
        )
        if ident:
            ids.append(str(ident))
    return tuple(ids)


async def list_models(coordinator: Any) -> ModelListing:
    """Active provider name, its ``default_model`` and advertised models."""
    name, provider = _primary_provider(coordinator)
    if provider is None:
        return ModelListing(provider="", current="")
    current = str(getattr(provider, "default_model", "") or "")
    available: tuple[str, ...] = ()
    lister = getattr(provider, "list_models", None)
    if callable(lister):
        try:
            available = _model_ids(await _maybe_await(lister()))
        except Exception:  # noqa: BLE001 — a broken lister must not kill the UI
            available = ()
    return ModelListing(provider=name, current=current, available=available)


async def set_model(coordinator: Any, model: str) -> tuple[bool, str]:
    """Switch the live model by mutating the mounted provider.

    amplifier exposes no coordinator ``set_model``; app-cli sets
    ``provider.default_model`` (and the provider ``config`` dict) directly,
    plus a ``ui.model_override`` session-state marker. Target resolution is:

    1. the explicit ``/model <provider> <model>`` form — a two-token arg
       whose first token names a mounted provider;
    2. the ONE provider whose ``list_models()`` advertises *model* (beyond
       the reference, so a bare model name can cross providers).

    A sticky prior override never beats present-tense provider evidence. Bare
    names advertised by zero or multiple providers fail explicitly and direct
    the user to ``/model <provider> <model>``; otherwise a stale override could
    silently send a valid model name to the wrong API.

    When the target is not the serving provider its priority is lowered
    below every other mounted provider's, or the switch would mutate a
    provider that never answers: the orchestrator selects strictly by
    priority-min (``loop-streaming::_select_provider``), with no per-turn
    override. A ``"/model foo bar"`` whose first token is NOT a mounted
    provider is treated as a single (space-containing) bare model name and
    must therefore be advertised exactly once too.
    """
    model = model.strip()
    if not model:
        return (False, "usage: /model [provider] <name>")
    providers = _providers(coordinator)
    if not providers:
        return (False, "no provider mounted")

    target_name: str | None = None
    parts = model.split(maxsplit=1)
    if len(parts) == 2 and parts[0] in providers:
        target_name, model = parts[0], parts[1].strip()
        if not model:
            return (False, "usage: /model [provider] <name>")
    if target_name is None:
        advertised_by: list[str] = []
        for name, provider in providers.items():
            lister = getattr(provider, "list_models", None)
            if callable(lister):
                try:
                    if model in _model_ids(await _maybe_await(lister())):
                        advertised_by.append(str(name))
                except Exception:  # noqa: BLE001
                    continue
        if len(advertised_by) > 1:
            choices = ", ".join(advertised_by)
            return (
                False,
                f"model {model!r} is advertised by multiple providers ({choices}); "
                "use /model <provider> <model>",
            )
        if not advertised_by:
            return (
                False,
                f"model {model!r} is not advertised by any mounted provider; "
                "use /model <provider> <model> to override explicitly",
            )
        target_name = advertised_by[0]
    target = providers.get(target_name) if target_name else None
    if target is None:
        return (False, "no provider mounted")

    old_default = getattr(target, "default_model", "")
    config = getattr(target, "config", None)
    missing = object()
    old_config_default = (
        config.get("default_model", missing) if isinstance(config, dict) else missing
    )
    try:
        target.default_model = model
    except Exception:  # noqa: BLE001 — some providers freeze attributes
        return (False, f"provider {target_name} does not allow model override")
    if isinstance(config, dict):
        config["default_model"] = model
    if not _promote_to_serving(providers, target_name):
        # Never report a switch that cannot actually route a turn. Restore
        # the model mutation as well as the helper's priority rollback so a
        # failed cross-provider switch is atomic from the user's point of
        # view.
        try:
            target.default_model = old_default
        except Exception:  # noqa: BLE001 — best-effort rollback after a successful set
            pass
        if isinstance(config, dict):
            if old_config_default is missing:
                config.pop("default_model", None)
            else:
                config["default_model"] = old_config_default
        return (
            False,
            f"provider {target_name} has a read-only routing priority; model switch not applied",
        )
    _set_session_state(coordinator, "ui.model_override", {"provider": target_name, "model": model})
    detail = f"{target_name} · {model}"
    try:
        from .model_routing import LiveMatrixSelection, activate_live_matrix

        matrix = await activate_live_matrix(coordinator, target_name)
    except Exception as error:  # noqa: BLE001 — the exact root-model switch already succeeded
        matrix = LiveMatrixSelection(reason=f"live routing update failed unexpectedly: {error}")
    if matrix.live and matrix.matrix:
        detail += f" · routing {matrix.matrix}"
    else:
        reason = matrix.reason or "live routing update was unavailable"
        _set_session_state(
            coordinator,
            "ui.routing_matrix",
            {
                "name": matrix.matrix,
                "live": False,
                "reason": reason,
                "divergent": True,
            },
        )
        detail += f" · delegated routing unchanged ({reason}); root/delegates may diverge"
    return (True, detail)


def _promote_to_serving(providers: dict[str, Any], name: str) -> bool:
    """Make *name* the priority-min provider so IT serves the next turn.

    The orchestrator reads the ``priority`` ATTRIBUTE a provider snaps hot
    at construction before falling back to its ``config`` dict — both must
    be written, attribute first, so ``loop-streaming::_select_provider``
    and :func:`_primary_provider` agree. Skipped when *name* is already
    strictly lowest (a tie can still resolve elsewhere in mount order, so
    ties promote). One below the others' minimum mirrors the boot-time
    ``--provider`` promotion (``config.apply_run_overrides`` stamps 0).
    """
    target = providers.get(name)
    others = [provider for key, provider in providers.items() if key != name]
    if target is None or not others:
        return target is not None
    min_others = min(_provider_priority(provider) for provider in others)
    if _provider_priority(target) < min_others:
        return True
    promoted = min_others - 1
    missing = object()
    old_attr = getattr(target, "priority", missing)
    config = getattr(target, "config", None)
    old_config = config.get("priority", missing) if isinstance(config, dict) else missing
    try:
        target.priority = promoted
    except Exception:  # noqa: BLE001 — config-backed properties may still promote
        pass
    if isinstance(config, dict):
        config["priority"] = promoted
    if _provider_priority(target) < min_others:
        return True

    # The orchestrator always prefers an existing ``priority`` attribute
    # over config. A stale read-only snapshot therefore cannot be rescued
    # by changing config alone. Roll back and fail closed rather than claim
    # that a provider/model is active while another provider keeps serving.
    if old_attr is not missing:
        try:
            target.priority = old_attr
        except Exception:  # noqa: BLE001 — it was read-only in this failure shape
            pass
    if isinstance(config, dict):
        if old_config is missing:
            config.pop("priority", None)
        else:
            config["priority"] = old_config
    return False


def _session_state(coordinator: Any) -> dict[str, Any]:
    state = getattr(coordinator, "session_state", None)
    return state if isinstance(state, dict) else {}


def _orchestrator_config(coordinator: Any) -> dict[str, Any] | None:
    try:
        orchestrator = coordinator.get("orchestrator")
    except Exception:  # noqa: BLE001
        return None
    config = getattr(orchestrator, "config", None)
    return config if isinstance(config, dict) else None


def normalize_effort(value: str) -> str | None:
    """Canonical effort level for *value* (``max``→``xhigh``), or None."""
    lowered = value.strip().lower()
    lowered = _EFFORT_ALIASES.get(lowered, lowered)
    return lowered if lowered in EFFORT_LEVELS else None


def get_effort(coordinator: Any) -> str | None:
    """The effort the next turn will effectively run at.

    The per-turn override in the orchestrator config
    (``request.reasoning_effort``) wins when set; otherwise the SERVING
    provider's own ``reasoning_effort``/``effort`` config applies (the
    provider's config-level fallback), so a bare ``/effort`` and the
    footer reflect what the provider will actually do instead of
    reporting "default" while e.g. ``effort: max`` is configured.
    """
    config = _orchestrator_config(coordinator)
    if config is not None:
        value = config.get("reasoning_effort")
        if value:
            return str(value)
    _, provider = _primary_provider(coordinator)
    provider_config = getattr(provider, "config", None)
    if isinstance(provider_config, dict):
        value = provider_config.get("reasoning_effort") or provider_config.get("effort")
        if value:
            return str(value)
    return None


def set_effort(coordinator: Any, level: str) -> tuple[bool, str]:
    """Set ``reasoning_effort`` on the orchestrator config (app-cli parity)."""
    canonical = normalize_effort(level)
    if canonical is None:
        return (False, f"effort must be one of: {', '.join(EFFORT_LEVELS)} (or max)")
    config = _orchestrator_config(coordinator)
    if config is None:
        return (False, "no orchestrator mounted — effort unavailable")
    config["reasoning_effort"] = canonical
    _set_session_state(coordinator, "ui.effort_override", canonical)
    return (True, canonical)


def _set_session_state(coordinator: Any, key: str, value: Any) -> None:
    state = getattr(coordinator, "session_state", None)
    if isinstance(state, dict):
        state[key] = value


def _context(coordinator: Any) -> Any:
    try:
        return coordinator.get("context")
    except Exception:  # noqa: BLE001
        return None


async def _message_count(context: Any) -> int:
    getter = getattr(context, "get_messages", None)
    if not callable(getter):
        return 0
    try:
        return len(list(await _maybe_await(getter())))
    except Exception:  # noqa: BLE001
        return 0


async def compact_context(coordinator: Any, focus: str = "") -> tuple[bool, str]:
    """Trigger the mounted context's own compaction (app-cli primary path)."""
    context = _context(coordinator)
    if context is None:
        return (False, "no context mounted")
    compact = getattr(context, "compact", None)
    if not callable(compact):
        return (False, "this context does not support /compact")
    before = await _message_count(context)
    try:
        await _maybe_await(compact(focus=focus) if focus else compact())
    except TypeError:
        # Some context modules take no ``focus`` kwarg.
        try:
            await _maybe_await(compact())
        except Exception as error:  # noqa: BLE001
            return (False, str(error))
    except Exception as error:  # noqa: BLE001
        return (False, str(error))
    after = await _message_count(context)
    if before == after:
        return (
            True,
            f"{after} messages · no persistent change; request-view compaction may be automatic",
        )
    return (True, f"{before} → {after} messages")


async def clear_context(coordinator: Any) -> tuple[bool, int]:
    """Clear conversation context via ``context.clear()`` (app-cli parity).

    Returns ``(ok, cleared_count)``. This is the mounted context's own
    clear capability, not a raw ``set_messages([])``."""
    context = _context(coordinator)
    if context is None:
        return (False, 0)
    clear = getattr(context, "clear", None)
    if not callable(clear):
        return (False, 0)
    count = await _message_count(context)
    try:
        await _maybe_await(clear())
    except Exception:  # noqa: BLE001
        return (False, 0)
    # app-cli parity: /clear ends any autonomous native loop as well as
    # clearing its conversation context.  The orchestrator reads this exact
    # shared state between turns, so no TUI-owned cancellation loop exists.
    state = getattr(coordinator, "session_state", None)
    if isinstance(state, dict):
        state["goal"] = None
    return (True, count)


async def list_tools(coordinator: Any) -> tuple[str, ...]:
    """Names of the mounted tools (``coordinator.get("tools")`` keys)."""
    try:
        tools = coordinator.get("tools")
    except Exception:  # noqa: BLE001
        return ()
    if not isinstance(tools, dict):
        return ()
    return tuple(sorted(str(name) for name in tools))


@dataclass(frozen=True)
class ToolDescriptor:
    """One mounted tool's CLI-facing summary (``tool list`` row)."""

    name: str
    description: str = ""
    invokable: bool = True
    """False only when the mounted object exposes no ``execute`` -- a
    listable-but-not-callable entry, surfaced honestly rather than hidden."""


def _tool_summary(instance: Any) -> str:
    """First-line summary of a tool (``description`` attr, else docstring).

    Mirrors amplifier-app-cli ``commands/tool.py`` (``description`` first,
    docstring first line as the fallback), collapsed to a single line so the
    ``tool list`` rows stay compact.
    """
    for source in (getattr(instance, "description", None), getattr(instance, "__doc__", None)):
        if isinstance(source, str) and source.strip():
            return " ".join(source.strip().splitlines()[0].split())
    return ""


async def describe_tools(coordinator: Any) -> tuple[ToolDescriptor, ...]:
    """Mounted tools as ``(name, description, invokable)`` rows for ``tool list``.

    The richer sibling of :func:`list_tools`: same ``coordinator.get("tools")``
    surface, but carrying each tool's one-line summary and whether it exposes an
    ``execute`` method -- exactly what the scriptable CLI ``tool list`` prints.
    """
    try:
        tools = coordinator.get("tools")
    except Exception:  # noqa: BLE001 -- duck-typed coordinator: a broken mount lists nothing
        return ()
    if not isinstance(tools, dict):
        return ()
    return tuple(
        sorted(
            (
                ToolDescriptor(
                    name=str(name),
                    description=_tool_summary(instance),
                    invokable=callable(getattr(instance, "execute", None)),
                )
                for name, instance in tools.items()
            ),
            key=lambda descriptor: descriptor.name,
        )
    )


@dataclass(frozen=True)
class ToolInvocation:
    """Normalized outcome of invoking one mounted tool from the CLI.

    ``found`` distinguishes an unknown tool (clear error + nonzero exit) from a
    tool that ran and failed; ``blocked`` marks a governance refusal (a one-shot
    CLI cannot honor an interactive approval) so the caller can say WHY it was
    blocked rather than conflating it with an execution error.
    """

    found: bool
    ok: bool
    output: Any = None
    error: str = ""
    blocked: bool = False
    capability: str = ""


async def invoke_tool(coordinator: Any, name: str, args: dict[str, Any]) -> ToolInvocation:
    """Invoke the mounted tool *name* with *args* via its ``execute`` surface.

    Same invocation contract the in-session ops already speak (``load_skill`` /
    ``set_native_mode`` call ``tool.execute({...})`` and read ``.success`` /
    ``.output`` / ``.error`` off the returned ``ToolResult``); a tool that
    returns a bare value instead is surfaced as-is. Never raises into the CLI: a
    missing tool, a non-callable mount, or an ``execute`` exception all come back
    as a structured :class:`ToolInvocation`.
    """
    tool = _tool(coordinator, name)
    if tool is None:
        return ToolInvocation(found=False, ok=False, error=f"no tool named '{name}' is mounted")
    execute = getattr(tool, "execute", None)
    if not callable(execute):
        return ToolInvocation(
            found=True, ok=False, error=f"tool '{name}' cannot be invoked (no execute method)"
        )
    try:
        result = await _maybe_await(execute(args))
    except Exception as error:  # noqa: BLE001 -- a tool crash is a CLI error record, never a traceback
        return ToolInvocation(found=True, ok=False, error=str(error) or type(error).__name__)
    if hasattr(result, "success"):
        ok = bool(getattr(result, "success"))
        raw_error = getattr(result, "error", None)
        message = raw_error.get("message") if isinstance(raw_error, dict) else raw_error
        return ToolInvocation(
            found=True,
            ok=ok,
            output=getattr(result, "output", None),
            error="" if ok else (str(message) if message else "tool reported failure"),
        )
    return ToolInvocation(found=True, ok=True, output=result)


async def list_agents(coordinator: Any) -> tuple[str, ...]:
    """Names of the agents the bundle mounted for delegation.

    amplifier registers the agent roster under the ``agents`` mount point
    (populated from the bundle ``agents: include:`` block); fall back to
    the coordinator config's ``agents`` mapping when no mechanism is
    mounted."""
    try:
        agents = coordinator.get("agents")
    except Exception:  # noqa: BLE001
        agents = None
    if isinstance(agents, dict) and agents:
        return tuple(sorted(str(name) for name in agents))
    config = getattr(coordinator, "config", None)
    if isinstance(config, dict):
        roster = config.get("agents")
        if isinstance(roster, dict):
            return tuple(sorted(str(name) for name in roster))
        if isinstance(roster, (list, tuple)):
            return tuple(str(name) for name in roster)
    return ()


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str = ""
    shortcut: str = ""
    """Optional slash alias from the skill's ``shortcut:`` frontmatter
    (``/cosam`` → ``cranky-old-sam``); empty when the skill has none."""


def _skills_from_catalog(tool: Any) -> tuple[SkillInfo, ...]:
    """Skills via the tool's ``get_effective_skills()`` catalog surface.

    The catalog is the only place shortcuts live — the tool's
    ``{"list": true}`` output carries name + description only. Returns
    ``()`` when the surface is missing or broken (caller falls back)."""
    catalog = getattr(tool, "get_effective_skills", None)
    if not callable(catalog):
        return ()
    try:
        skills = catalog()
    except Exception:  # noqa: BLE001 — degrade to the list output
        return ()
    if not isinstance(skills, dict):
        return ()
    return tuple(
        SkillInfo(
            name=str(name),
            description=str(getattr(meta, "description", "") or ""),
            shortcut=str(getattr(meta, "shortcut", "") or ""),
        )
        for name, meta in sorted(skills.items())
        if name
    )


async def list_skills(coordinator: Any) -> tuple[SkillInfo, ...]:
    """Available skills via the ``load_skill`` tool (``{"list": true}``)."""
    tool = _tool(coordinator, "load_skill")
    if tool is None:
        return ()
    if from_catalog := _skills_from_catalog(tool):
        return from_catalog
    try:
        result = await tool.execute({"list": True})
    except Exception:  # noqa: BLE001 — a broken skills tool must not kill the UI
        return ()
    if not getattr(result, "success", False):
        return ()
    output = getattr(result, "output", None)
    if not isinstance(output, dict):
        return ()
    skills = output.get("skills") or []
    return tuple(
        SkillInfo(
            name=str(s.get("name", "")),
            description=str(s.get("description", "")),
            shortcut=str(s.get("shortcut", "") or ""),
        )
        for s in skills
        if isinstance(s, dict) and s.get("name")
    )


async def load_skill(coordinator: Any, name: str) -> tuple[bool, str]:
    """Load a skill by name via the ``load_skill`` tool.

    Returns ``(ok, content_or_error)`` — on success the skill body, else a
    reason. The mounted skills-visibility hook already advertises skills to
    the agent; this is the explicit user-driven load."""
    from .skill_activation import (
        activate_skill_result,
        parse_skill_request,
        skill_payload,
    )

    request = parse_skill_request(name)
    if not request.name:
        return (False, "usage: /skill <name> [arguments]")
    tool = _tool(coordinator, "load_skill")
    if tool is None:
        return (False, "no skills tool mounted")
    try:
        result = await tool.execute(skill_payload(request))
    except Exception as error:  # noqa: BLE001
        return (False, str(error))
    if not getattr(result, "success", False):
        err = getattr(result, "error", None)
        message = err.get("message") if isinstance(err, dict) else err
        return (False, str(message) if message else f"skill not found: {request.name}")
    output = getattr(result, "output", None)
    activation = await activate_skill_result(coordinator, request, output)
    if not activation.context_added:
        reason = activation.reason or "live context activation failed"
        return (False, f"skill loaded but is not active for the next turn: {reason}")
    return (True, activation.display)


async def list_mcp_tools(coordinator: Any) -> tuple[str, ...]:
    """Live MCP tool names (``mcp_<server>_<tool>``) on the tools mount.

    tool-mcp mounts each remote server's tools individually at session
    start; this is what actually connected (empty when no mcp.json)."""
    try:
        tools = coordinator.get("tools")
    except Exception:  # noqa: BLE001
        return ()
    if not isinstance(tools, dict):
        return ()
    return tuple(sorted(str(name) for name in tools if str(name).startswith("mcp_")))


async def status_snapshot(coordinator: Any) -> StatusInfo:
    """The coordinator-derived fields for ``/status``."""
    name, provider = _primary_provider(coordinator)
    model = str(getattr(provider, "default_model", "") or "") if provider is not None else ""
    context = _context(coordinator)
    messages = await _message_count(context) if context is not None else 0
    tools = await list_tools(coordinator)
    agents = await list_agents(coordinator)
    return StatusInfo(
        session_id=str(getattr(coordinator, "session_id", "") or ""),
        provider=name,
        model=model,
        effort=get_effort(coordinator),
        messages=messages,
        tools=len(tools),
        agents=agents,
    )


__all__ = [
    "EFFORT_LEVELS",
    "ModelListing",
    "SkillInfo",
    "ToolDescriptor",
    "ToolInvocation",
    "StatusInfo",
    "clear_context",
    "compact_context",
    "get_effort",
    "list_agents",
    "list_mcp_tools",
    "list_models",
    "list_skills",
    "describe_tools",
    "invoke_tool",
    "list_tools",
    "load_skill",
    "normalize_effort",
    "set_effort",
    "set_model",
    "status_snapshot",
]
