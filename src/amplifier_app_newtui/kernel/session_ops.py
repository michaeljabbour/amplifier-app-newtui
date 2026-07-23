"""In-session operations over the amplifier coordinator.

The interactive slash commands ``/model``, ``/effort``, ``/compact``,
``/clear``, ``/status``, ``/tools`` and ``/agents`` act on the LIVE
session. amplifier-app-cli implements them in ``CoreCommandService`` /
``CommandSessionMixin`` against the amplifier-core coordinator surface;
this module is the port onto the SAME surface that
:class:`~amplifier_app_newtui.kernel.runtime.RealRuntime` already holds
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


def _primary_provider(coordinator: Any) -> tuple[str, Any]:
    """The first mounted provider (name, object), or ``("", None)``."""
    for name, provider in _providers(coordinator).items():
        return (str(name), provider)
    return ("", None)


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
    plus a ``ui.model_override`` session-state marker. Applies to the
    provider that advertises *model* when known, else the primary one."""
    model = model.strip()
    if not model:
        return (False, "usage: /model <name>")
    providers = _providers(coordinator)
    if not providers:
        return (False, "no provider mounted")

    target_name, target = _primary_provider(coordinator)
    for name, provider in providers.items():
        lister = getattr(provider, "list_models", None)
        if callable(lister):
            try:
                if model in _model_ids(await _maybe_await(lister())):
                    target_name, target = str(name), provider
                    break
            except Exception:  # noqa: BLE001
                continue
    if target is None:
        return (False, "no provider mounted")

    try:
        target.default_model = model
    except Exception:  # noqa: BLE001 — some providers freeze attributes
        return (False, f"provider {target_name} does not allow model override")
    config = getattr(target, "config", None)
    if isinstance(config, dict):
        config["default_model"] = model
    _set_session_state(coordinator, "ui.model_override", {"provider": target_name, "model": model})
    return (True, f"{target_name} · {model}")


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
    """Current ``reasoning_effort`` from the mounted orchestrator config."""
    config = _orchestrator_config(coordinator)
    if config is None:
        return None
    value = config.get("reasoning_effort")
    return str(value) if value else None


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
    name = name.strip()
    if not name:
        return (False, "usage: /skill <name>")
    tool = _tool(coordinator, "load_skill")
    if tool is None:
        return (False, "no skills tool mounted")
    try:
        result = await tool.execute({"skill_name": name})
    except Exception as error:  # noqa: BLE001
        return (False, str(error))
    if not getattr(result, "success", False):
        err = getattr(result, "error", None)
        message = err.get("message") if isinstance(err, dict) else err
        return (False, str(message) if message else f"skill not found: {name}")
    output = getattr(result, "output", None)
    content = output.get("content", "") if isinstance(output, dict) else ""
    return (True, str(content))


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
