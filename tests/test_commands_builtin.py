"""Built-in command table + handler wiring (DESIGN-SPEC §6)."""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_newtui.commands.builtin import BUILTIN_COMMANDS, build_registry
from amplifier_app_newtui.commands.doctor import McpServerStats
from amplifier_app_newtui.commands.improve import ApprovalTally, OverriddenDenial
from amplifier_app_newtui.model.blocks import (
    ContextBlock,
    DoctorBlock,
    ImproveBlock,
    LedgerBlock,
)
from amplifier_app_newtui.model.turn import TurnOutcome, TurnTelemetry

# The mockup COMMANDS table, verbatim: (group, name, desc, tag).
MOCKUP_TABLE = [
    ("During", "/mode", "cycle or jump posture: chat, plan, brainstorm, build, auto", "built-in"),
    ("During", "/plan", "read-only planning; hands the plan to build", "built-in"),
    ("During", "/brainstorm", "no tools, divergent output; /plan to converge", "built-in"),
    ("During", "/context", "context usage grid + suggestions", "built-in"),
    ("Parallel", "/tasks", "agent lanes: one line per subagent", "built-in"),
    ("Ship", "/ledger", "session outcome ledger: spend vs yield", "built-in"),
    ("Between", "/rewind", "fork from any turn-rule checkpoint", "built-in"),
    ("Repair", "/permissions", "edit trust slots: boundary, blocks, exceptions", "built-in"),
    ("Repair", "/doctor", "setup checkup; reports, then fixes on confirm", "skill"),
    ("Repair", "/improve", "tune config from ledger + denial log", "skill"),
]


def test_table_matches_mockup_exactly() -> None:
    actual = [(s.group, s.name, s.desc, s.tag) for s in BUILTIN_COMMANDS]
    assert actual == MOCKUP_TABLE


def test_registry_holds_all_ten() -> None:
    registry = build_registry()
    assert len(registry.specs) == 10
    grouped = registry.grouped_rows("/")
    assert [g for g, _ in grouped] == ["During", "Parallel", "Ship", "Between", "Repair"]


def test_mode_cycles_without_args_and_jumps_with_mode_arg(fake_command_context) -> None:
    registry = build_registry()
    ctx = fake_command_context
    registry.run("/mode", ctx)
    assert ctx.calls == ["cycle_mode"]
    registry.run("/mode", ctx, "plan")
    assert ctx.calls == ["cycle_mode", "set_mode:plan"]
    # Unknown mode arg falls back to cycling, never crashes.
    registry.run("/mode", ctx, "warp")
    assert ctx.calls[-1] == "cycle_mode"


def test_plan_and_brainstorm_jump_modes(fake_command_context) -> None:
    registry = build_registry()
    ctx = fake_command_context
    registry.run("/plan", ctx)
    registry.run("/brainstorm", ctx)
    assert ctx.calls == ["set_mode:plan", "set_mode:brainstorm"]


def test_context_posts_context_block(fake_command_context) -> None:
    registry = build_registry()
    ctx = fake_command_context
    registry.run("/context", ctx)
    assert ctx.user_lines == ["/context"]
    (block,) = ctx.blocks
    assert isinstance(block, ContextBlock)
    assert block.used_pct == 39  # 78k of 200k
    assert block.window_label == "200k"
    labels = [label for label, _ in block.segments]
    assert labels == ["conversation 52k", "tools 18k", "memory 8k", "free 122k"]


def test_tasks_rewind_permissions_dispatch_actions(fake_command_context) -> None:
    registry = build_registry()
    ctx = fake_command_context
    registry.run("/tasks", ctx)
    registry.run("/rewind", ctx)
    registry.run("/permissions", ctx)
    assert ctx.calls == ["toggle_lanes", "open_rewind", "open_permissions"]


def test_ledger_posts_ledger_block_with_aggregates(fake_command_context) -> None:
    registry = build_registry()
    ctx = fake_command_context
    ctx.ledger.record_turn(
        TurnTelemetry(secs=12, tokens_down=3_200, cached_pct=80, cost=Decimal("0.31")),
        TurnOutcome(kind="shipped", files_changed=3, diffstat="+142/−38", tests_ok=True),
        turn_id=1,
        message_index=4,
        label="ship it",
    )
    ctx.ledger.record_turn(
        TurnTelemetry(secs=5, tokens_down=800, cached_pct=40, cost=Decimal("0.05")),
        TurnOutcome(kind="answer"),
        turn_id=2,
        message_index=8,
    )
    registry.run("/ledger", ctx)
    (block,) = ctx.blocks
    assert isinstance(block, LedgerBlock)
    assert block.session == "a1b2c3"
    assert block.bundle == "dev-bundle"
    assert block.turns == 2
    assert block.spend == Decimal("0.36")
    assert block.shipped == 1
    assert block.answer_only == 1
    assert block.cache_hit_pct == 72  # token-weighted


def test_doctor_posts_doctor_block_with_findings(fake_command_context) -> None:
    registry = build_registry()
    ctx = fake_command_context
    ctx.mcp_stats = (
        McpServerStats(name="alpha", last_used_days_ago=45, tokens_per_session=2_100),
        McpServerStats(name="beta", last_used_days_ago=None, tokens_per_session=2_000),
    )
    ctx.tallies = (
        ApprovalTally(action="read docs/", approved=14, asked=14, capability="read"),
    )
    registry.run("/doctor", ctx)
    (block,) = ctx.blocks
    assert isinstance(block, DoctorBlock)
    texts = [finding.text for finding in block.findings]
    assert "2 MCP servers unused in 30 days · cost 4.1k tok/session" in texts
    assert (
        "14 identical read-only approvals this week · candidate allowlist" in texts
    )


def test_improve_posts_proposals_and_never_mutates(fake_command_context) -> None:
    registry = build_registry()
    ctx = fake_command_context
    ctx.tallies = (
        ApprovalTally(action="uv run pytest", approved=22, asked=22, capability="test"),
    )
    ctx.overrides = (OverriddenDenial(action="push-to-fork", denied=3, overridden=3),)
    registry.run("/improve", ctx)
    (block,) = ctx.blocks
    assert isinstance(block, ImproveBlock)
    assert [p.title for p in block.proposals] == [
        "allowlist: uv run pytest",
        "trust slot: push-to-fork",
    ]
    assert block.proposals[0].rationale == "approved 22/22 times · add to auto"
    # Proposals only — nothing was applied to any surface.
    assert ctx.calls == []
    assert ctx.notices == []


def test_key_actions_exist_in_keymap() -> None:
    """Registry key_action ids must be real keymap actions (single source)."""
    from amplifier_app_newtui.ui.keymap import KEYMAP

    keymap_actions = {binding.action for binding in KEYMAP}
    registry = build_registry()
    assert set(registry.keybound()) <= keymap_actions
    assert set(registry.keybound()) == {
        "cycle_mode",
        "toggle_lanes",
        "show_ledger",
        "open_rewind",
    }
