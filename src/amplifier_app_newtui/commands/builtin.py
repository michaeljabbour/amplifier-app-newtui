"""The built-in command set — descriptions verbatim from the mockup.

Each handler acts on the app only through the
:class:`~amplifier_app_newtui.commands.registry.CommandContext` protocol
(posting messages / mutating model state). The table below IS the mockup
``COMMANDS`` array (group, name, description, tag) — the palette, help
and keybinds all read this one registry (DESIGN-SPEC §6).
"""

from __future__ import annotations

from decimal import Decimal

from ..model.blocks import LedgerBlock, SessionBanner
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
    """``/mode`` — cycle postures; ``/mode plan`` — jump to a posture;
    ``/mode <bundle-mode>`` — activate a native, bundle-composed mode
    (superpowers, careful, audit, …) through the mounted mode tool;
    ``/mode off`` — clear the native mode."""
    target = args.strip().lower()
    if not target:
        ctx.cycle_mode()
    elif target in MODE_PROFILES:
        ctx.set_mode(target)
    elif target == "off":
        ctx.set_native_mode(None)
    else:
        ctx.set_native_mode(target)


def _cmd_modes(ctx: CommandContext, args: str) -> None:
    """``/modes`` — list the bundle-composed native modes + postures."""
    del args
    ctx.show_modes()


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


def _cmd_config(ctx: CommandContext, args: str) -> None:
    """``/config`` — show/toggle/set/diff/save the live session config."""
    ctx.manage_config(args.strip())


def _cmd_tasks(ctx: CommandContext, args: str) -> None:
    del args
    ctx.toggle_lanes()


def _cmd_status(ctx: CommandContext, args: str) -> None:
    """``/status`` — live session snapshot (model, mode, messages, cost)."""
    del args
    ctx.show_status()


def _cmd_model(ctx: CommandContext, args: str) -> None:
    """``/model`` — list models; ``/model <name>`` switches the live model."""
    ctx.show_model(args.strip())


def _cmd_effort(ctx: CommandContext, args: str) -> None:
    """``/effort`` — show reasoning effort; ``/effort <level>`` sets it."""
    ctx.apply_effort(args.strip())


def _cmd_compact(ctx: CommandContext, args: str) -> None:
    """``/compact`` — compact context; ``/compact <focus>`` steers it."""
    ctx.compact_context(args.strip())


def _cmd_clear(ctx: CommandContext, args: str) -> None:
    """``/clear`` — clear the conversation context."""
    del args
    ctx.clear_context()


def _cmd_tools(ctx: CommandContext, args: str) -> None:
    """``/tools`` — list the mounted tools."""
    del args
    ctx.show_tools()


def _cmd_agents(ctx: CommandContext, args: str) -> None:
    """``/agents`` — list the delegatable agents."""
    del args
    ctx.show_agents()


def _cmd_diff(ctx: CommandContext, args: str) -> None:
    """``/diff`` — working-tree patch; ``/diff staged`` for the cached diff."""
    ctx.show_diff(args.strip())


def _cmd_skills(ctx: CommandContext, args: str) -> None:
    """``/skills`` — list the available skills."""
    del args
    ctx.show_skills()


def _cmd_skill(ctx: CommandContext, args: str) -> None:
    """``/skill <name>`` — load a skill via the mounted skills tool."""
    ctx.load_skill(args.strip())


def _cmd_mcp(ctx: CommandContext, args: str) -> None:
    """``/mcp`` — list; ``/mcp add|remove`` manages MCP servers (mcp.json)."""
    ctx.manage_mcp(args.strip())


def _cmd_ledger(ctx: CommandContext, args: str) -> None:
    del args
    ledger = ctx.ledger
    ctx.post_block(
        LedgerBlock(
            id=ctx.next_block_id(),
            session=ctx.session_short,
            bundle=ctx.bundle_name,
            turns=ledger.turn_count,
            # Mockup cmdLedger prints ``this.cost`` — the session cost the
            # footer shows (includes any pre-session baseline).
            spend=Decimal(ctx.session_cost),
            shipped=ledger.shipped_count,
            answer_only=ledger.answer_only_count,
            cache_hit_pct=ledger.cache_hit_pct,
        )
    )
    # Mockup cmdLedger ends with this exact notice.
    ctx.show_notice("ledger printed to scrollback")


def _cmd_rewind(ctx: CommandContext, args: str) -> None:
    del args
    ctx.open_rewind()


def _cmd_rename(ctx: CommandContext, args: str) -> None:
    """``/rename <name>`` — label the current session for the resume picker."""
    ctx.rename_session(args.strip())


def _cmd_sessions(ctx: CommandContext, args: str) -> None:
    """``/sessions`` — list this project's stored sessions."""
    del args
    ctx.show_sessions()


def _cmd_branch(ctx: CommandContext, args: str) -> None:
    """``/branch [name]`` — snapshot this conversation into a new session."""
    ctx.branch_session(args.strip())


def _cmd_fork(ctx: CommandContext, args: str) -> None:
    """``/fork <directive>`` — snapshot into a new session primed to run it."""
    ctx.fork_session(args.strip())


def _cmd_permissions(ctx: CommandContext, args: str) -> None:
    del args
    ctx.open_permissions()


def _cmd_allowed_dirs(ctx: CommandContext, args: str) -> None:
    ctx.manage_directories("allowed", args)


def _cmd_denied_dirs(ctx: CommandContext, args: str) -> None:
    ctx.manage_directories("denied", args)


def _cmd_doctor(ctx: CommandContext, args: str) -> None:
    del args
    mcp_stats = tuple(stat for stat in ctx.mcp_server_stats() if isinstance(stat, McpServerStats))
    tallies = tuple(tally for tally in ctx.approval_tallies() if isinstance(tally, ApprovalTally))
    report = run_checks(mcp_stats=mcp_stats, approval_tallies=tallies)
    ctx.post_block(build_doctor_block(ctx.next_block_id(), report))


def _cmd_export(ctx: CommandContext, args: str) -> None:
    """``/export`` — write the transcript markdown, notice the path."""
    del args
    ctx.show_notice(f"transcript exported · {ctx.export_transcript()}")


def _cmd_copy(ctx: CommandContext, args: str) -> None:
    """``/copy`` — copy the last answer to the clipboard, notice the char count."""
    del args
    n = ctx.copy_answer()
    if n == 0:
        ctx.show_notice("no answer to copy yet")
        return
    ctx.show_notice(f"copied · {n} chars · empty clipboard? allow terminal clipboard access")


def _cmd_about(ctx: CommandContext, args: str) -> None:
    """``/about`` — post the app/core/bundle/session identity as a block
    (the same data the session banner shows)."""
    del args
    app_version, core_version, bundle, session = ctx.about_info()
    ctx.post_block(
        SessionBanner(
            id=ctx.next_block_id(),
            headline=f"Amplifier {app_version} · core {core_version}",
            detail=f"Bundle: {bundle} | session {session}",
        )
    )


def _cmd_quit(ctx: CommandContext, args: str) -> None:
    """``/quit`` — exit the app (amplifier-app-cli parity: exit/quit)."""
    del args
    ctx.quit_app()


def _cmd_theme(ctx: CommandContext, args: str) -> None:
    """``/theme`` — cycle; ``/theme graphite`` — jump to a theme (spec §1)."""
    ctx.set_theme(args.strip().lower())


def _cmd_improve(ctx: CommandContext, args: str) -> None:
    del args
    tallies = tuple(tally for tally in ctx.approval_tallies() if isinstance(tally, ApprovalTally))
    overrides = tuple(row for row in ctx.overridden_denials() if isinstance(row, OverriddenDenial))
    proposals = improve_proposals(tallies=tallies, overrides=overrides, ledger=ctx.ledger)
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
    # Beyond the mockup table: bundle-composed native modes (superpowers
    # et al) — discovered from the session, never hardcoded here.
    CommandSpec(
        group="During",
        name="/modes",
        desc="list native bundle modes; /mode <name> activates",
        tag="built-in",
        handler=_cmd_modes,
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
    # Live session config editor (amplifier-app-cli /config parity).
    CommandSpec(
        group="During",
        name="/config",
        desc="live config: show · toggle · set · diff · save",
        tag="built-in",
        handler=_cmd_config,
    ),
    # In-session ops over the live amplifier coordinator (app-cli parity).
    CommandSpec(
        group="During",
        name="/status",
        desc="session status: model, mode, messages, cost",
        tag="built-in",
        handler=_cmd_status,
    ),
    CommandSpec(
        group="During",
        name="/model",
        desc="list models; /model <name> switches the live model",
        tag="built-in",
        handler=_cmd_model,
    ),
    CommandSpec(
        group="During",
        name="/effort",
        desc="reasoning effort; /effort <none…max> sets it",
        tag="built-in",
        handler=_cmd_effort,
    ),
    CommandSpec(
        group="During",
        name="/compact",
        desc="compact context; /compact <focus> to steer it",
        tag="built-in",
        handler=_cmd_compact,
    ),
    CommandSpec(
        group="During",
        name="/clear",
        desc="clear the conversation context",
        tag="built-in",
        handler=_cmd_clear,
    ),
    CommandSpec(
        group="During",
        name="/tools",
        desc="list the mounted tools",
        tag="built-in",
        handler=_cmd_tools,
    ),
    CommandSpec(
        group="During",
        name="/agents",
        desc="list the delegatable agents",
        tag="built-in",
        handler=_cmd_agents,
    ),
    CommandSpec(
        group="During",
        name="/skills",
        desc="list available skills",
        tag="skill",
        handler=_cmd_skills,
    ),
    CommandSpec(
        group="During",
        name="/skill",
        desc="load a skill by name: /skill <name>",
        tag="skill",
        handler=_cmd_skill,
    ),
    CommandSpec(
        group="During",
        name="/mcp",
        desc="MCP servers: list · add · remove",
        tag="built-in",
        handler=_cmd_mcp,
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
    # Beyond the mockup table: transcript markdown export.
    CommandSpec(
        group="Ship",
        name="/export",
        desc="write transcript markdown to exports/",
        tag="built-in",
        handler=_cmd_export,
    ),
    # Beyond the mockup table: last-answer clipboard copy.
    CommandSpec(
        group="Ship",
        name="/copy",
        desc="copy last answer to clipboard (OSC 52)",
        tag="built-in",
        handler=_cmd_copy,
    ),
    # In-session ops (app-cli parity): review the working-tree diff.
    CommandSpec(
        group="Ship",
        name="/diff",
        desc="working-tree diff; /diff staged for the cached diff",
        tag="built-in",
        handler=_cmd_diff,
    ),
    # Beyond the mockup table: app/core/bundle/session identity block.
    CommandSpec(
        group="Ship",
        name="/about",
        desc="app, core, bundle + session identity",
        tag="built-in",
        handler=_cmd_about,
    ),
    CommandSpec(
        group="Between",
        name="/rewind",
        desc="fork from any turn-rule checkpoint",
        tag="built-in",
        handler=_cmd_rewind,
        key_action="open_rewind",
    ),
    # Stored-session lifecycle (amplifier-app-cli parity: /rename, session
    # picker, the /branch fork family) — the persisted counterparts to the
    # in-memory /rewind.
    CommandSpec(
        group="Between",
        name="/rename",
        desc="name this session for the resume picker",
        tag="built-in",
        handler=_cmd_rename,
    ),
    CommandSpec(
        group="Between",
        name="/sessions",
        desc="list stored sessions for this project",
        tag="built-in",
        handler=_cmd_sessions,
    ),
    CommandSpec(
        group="Between",
        name="/branch",
        desc="snapshot this conversation into a new session",
        tag="built-in",
        handler=_cmd_branch,
    ),
    CommandSpec(
        group="Between",
        name="/fork",
        desc="snapshot into a new session primed to run a directive",
        tag="built-in",
        handler=_cmd_fork,
    ),
    # Beyond the mockup table: exit path (amplifier-app-cli parity).
    CommandSpec(
        group="Between",
        name="/quit",
        desc="exit the app (ctrl-d works too)",
        tag="built-in",
        handler=_cmd_quit,
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
        name="/allowed-dirs",
        desc="list or edit session allowed write directories",
        tag="built-in",
        handler=_cmd_allowed_dirs,
    ),
    CommandSpec(
        group="Repair",
        name="/denied-dirs",
        desc="list or edit session denied write directories",
        tag="built-in",
        handler=_cmd_denied_dirs,
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
    # Runtime theme switch (DESIGN-SPEC §1) — the one command beyond the
    # mockup COMMANDS table ("themes … in Tweaks" has no TUI equivalent).
    CommandSpec(
        group="Repair",
        name="/theme",
        desc="switch theme: slate, graphite, carbon",
        tag="built-in",
        handler=_cmd_theme,
    ),
)


def build_registry() -> CommandRegistry:
    """A fresh registry loaded with the built-in command set."""
    return CommandRegistry(BUILTIN_COMMANDS)


__all__ = ["BUILTIN_COMMANDS", "build_registry"]
