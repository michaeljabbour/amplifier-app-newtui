"""The built-in command set — descriptions verbatim from the mockup.

Each handler acts on the app only through the
:class:`~amplifier_app_newtui.commands.registry.CommandContext` protocol
(posting messages / mutating model state). The table below IS the mockup
``COMMANDS`` array (group, name, description, tag) — the palette, help
and keybinds all read this one registry (DESIGN-SPEC §6).
"""

from __future__ import annotations

from decimal import Decimal

from ..model.blocks import LedgerBlock
from ..model.modes import MODE_PROFILES
from .context import ContextUsage, build_context_block
from .doctor import McpServerStats, build_doctor_block, run_checks
from .improve import (
    ApprovalTally,
    OverriddenDenial,
    build_improve_block,
    improve_proposals,
)
from .registry import CommandContext, CommandRegistry, CommandSpec


def _cmd_mode(ctx: CommandContext, args: str) -> None:
    """``/mode`` — cycle; ``/mode plan`` — jump straight to a mode."""
    target = args.strip().lower()
    if target in MODE_PROFILES:
        ctx.set_mode(target)
    else:
        ctx.cycle_mode()


def _cmd_plan(ctx: CommandContext, args: str) -> None:
    del args
    ctx.set_mode("plan")


def _cmd_brainstorm(ctx: CommandContext, args: str) -> None:
    del args
    ctx.set_mode("brainstorm")


def _cmd_context(ctx: CommandContext, args: str) -> None:
    del args
    usage = ctx.context_usage()
    assert isinstance(usage, ContextUsage)
    ctx.post_block(build_context_block(ctx.next_block_id(), usage))


def _cmd_tasks(ctx: CommandContext, args: str) -> None:
    del args
    ctx.toggle_lanes()


def _cmd_ledger(ctx: CommandContext, args: str) -> None:
    del args
    ledger = ctx.ledger
    ctx.post_block(
        LedgerBlock(
            id=ctx.next_block_id(),
            session=ctx.session_short,
            bundle=ctx.bundle_name,
            turns=ledger.turn_count,
            spend=Decimal(ledger.spend),
            shipped=ledger.shipped_count,
            answer_only=ledger.answer_only_count,
            cache_hit_pct=ledger.cache_hit_pct,
        )
    )


def _cmd_rewind(ctx: CommandContext, args: str) -> None:
    del args
    ctx.open_rewind()


def _cmd_permissions(ctx: CommandContext, args: str) -> None:
    del args
    ctx.open_permissions()


def _cmd_doctor(ctx: CommandContext, args: str) -> None:
    del args
    mcp_stats = tuple(
        stat for stat in ctx.mcp_server_stats() if isinstance(stat, McpServerStats)
    )
    tallies = tuple(
        tally for tally in ctx.approval_tallies() if isinstance(tally, ApprovalTally)
    )
    report = run_checks(mcp_stats=mcp_stats, approval_tallies=tallies)
    ctx.post_block(build_doctor_block(ctx.next_block_id(), report))


def _cmd_improve(ctx: CommandContext, args: str) -> None:
    del args
    tallies = tuple(
        tally for tally in ctx.approval_tallies() if isinstance(tally, ApprovalTally)
    )
    overrides = tuple(
        row for row in ctx.overridden_denials() if isinstance(row, OverriddenDenial)
    )
    proposals = improve_proposals(
        tallies=tallies, overrides=overrides, ledger=ctx.ledger
    )
    ctx.post_block(build_improve_block(ctx.next_block_id(), proposals))


# The mockup COMMANDS table, verbatim (group, name, description, tag).
BUILTIN_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        group="During",
        name="/mode",
        desc="cycle or jump posture: chat, plan, brainstorm, build, auto",
        tag="built-in",
        handler=_cmd_mode,
        key_action="cycle_mode",
    ),
    CommandSpec(
        group="During",
        name="/plan",
        desc="read-only planning; hands the plan to build",
        tag="built-in",
        handler=_cmd_plan,
    ),
    CommandSpec(
        group="During",
        name="/brainstorm",
        desc="no tools, divergent output; /plan to converge",
        tag="built-in",
        handler=_cmd_brainstorm,
    ),
    CommandSpec(
        group="During",
        name="/context",
        desc="context usage grid + suggestions",
        tag="built-in",
        handler=_cmd_context,
    ),
    CommandSpec(
        group="Parallel",
        name="/tasks",
        desc="agent lanes: one line per subagent",
        tag="built-in",
        handler=_cmd_tasks,
        key_action="toggle_lanes",
    ),
    CommandSpec(
        group="Ship",
        name="/ledger",
        desc="session outcome ledger: spend vs yield",
        tag="built-in",
        handler=_cmd_ledger,
        key_action="show_ledger",
    ),
    CommandSpec(
        group="Between",
        name="/rewind",
        desc="fork from any turn-rule checkpoint",
        tag="built-in",
        handler=_cmd_rewind,
        key_action="open_rewind",
    ),
    CommandSpec(
        group="Repair",
        name="/permissions",
        desc="edit trust slots: boundary, blocks, exceptions",
        tag="built-in",
        handler=_cmd_permissions,
    ),
    CommandSpec(
        group="Repair",
        name="/doctor",
        desc="setup checkup; reports, then fixes on confirm",
        tag="skill",
        handler=_cmd_doctor,
    ),
    CommandSpec(
        group="Repair",
        name="/improve",
        desc="tune config from ledger + denial log",
        tag="skill",
        handler=_cmd_improve,
    ),
)


def build_registry() -> CommandRegistry:
    """A fresh registry loaded with the built-in command set."""
    return CommandRegistry(BUILTIN_COMMANDS)


__all__ = ["BUILTIN_COMMANDS", "build_registry"]
