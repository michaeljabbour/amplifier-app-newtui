"""Mounted MCP prompts as live, namespaced slash commands."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError

from ..model.blocks import Segment
from .registry import CommandContext, CommandRegistry, CommandSpec


class MCPPromptLike(Protocol):
    @property
    def command(self) -> str: ...
    @property
    def server(self) -> str: ...
    @property
    def prompt(self) -> str: ...
    @property
    def description(self) -> str: ...


class MCPPromptCollision(NamedTuple):
    trigger: str
    prompt: str
    owner: str


class MCPPromptPlan(NamedTuple):
    specs: tuple[CommandSpec, ...]
    collisions: tuple[MCPPromptCollision, ...]


def _run_handler(server: str, prompt: str) -> Any:
    def handler(ctx: CommandContext, args: str) -> None:
        ctx.run_mcp_prompt(server, prompt, args.strip())

    return handler


def _spec(info: MCPPromptLike) -> CommandSpec | None:
    try:
        return CommandSpec(
            group="During",
            name=str(info.command),
            desc=" ".join(str(info.description or "MCP prompt").split()),
            tag="mcp",
            handler=_run_handler(str(info.server), str(info.prompt)),
        )
    except ValidationError:
        return None


def plan_mcp_prompt_commands(
    registry: CommandRegistry,
    prompts: Iterable[MCPPromptLike],
) -> MCPPromptPlan:
    """Plan prompt commands; existing commands and earlier prompts win."""

    owner = {name: registry.source_of(name) or "registered" for name in registry.names}
    specs: list[CommandSpec] = []
    collisions: list[MCPPromptCollision] = []
    for info in prompts:
        spec = _spec(info)
        if spec is None:
            continue
        target = f"{info.server}:{info.prompt}"
        if spec.name in owner:
            collisions.append(MCPPromptCollision(spec.name, target, owner[spec.name]))
            continue
        specs.append(spec)
        owner[spec.name] = f"mcp:{target}"
    return MCPPromptPlan(tuple(specs), tuple(collisions))


def sync_mcp_prompt_commands_reporting(
    registry: CommandRegistry,
    prompts: Iterable[MCPPromptLike],
) -> MCPPromptPlan:
    """Replace only MCP-contributed commands with the live prompt catalog."""

    for spec in registry.contributions("mcp"):
        registry.unregister(spec.name)
    plan = plan_mcp_prompt_commands(registry, prompts)
    added = tuple(spec for spec in plan.specs if registry.register(spec, source="mcp"))
    return MCPPromptPlan(added, plan.collisions)


def mcp_prompt_collision_spans(
    collisions: Sequence[MCPPromptCollision],
) -> tuple[Segment, ...]:
    if not collisions:
        return ()
    count = len(collisions)
    noun = "collision" if count == 1 else "collisions"
    spans: list[Segment] = [
        Segment(text="· ", style_token="blue"),
        Segment(text="MCP prompt commands", style_token="bright", bold=True),
        Segment(text=f"  {count} {noun} · first registration wins\n", style_token="dim"),
    ]
    width = max(len(item.trigger) for item in collisions)
    for item in collisions:
        spans.append(Segment(text=f"  {item.trigger.ljust(width)}  ", style_token="orange"))
        spans.append(
            Segment(
                text=f"wanted by {item.prompt} · already claimed by {item.owner}\n",
                style_token="dim",
            )
        )
    return tuple(spans)


__all__ = [
    "MCPPromptCollision",
    "MCPPromptLike",
    "MCPPromptPlan",
    "mcp_prompt_collision_spans",
    "plan_mcp_prompt_commands",
    "sync_mcp_prompt_commands_reporting",
]
