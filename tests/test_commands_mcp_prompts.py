"""Dynamic MCP prompt slash-command registration."""

from __future__ import annotations

from amplifier_app_tui.commands.mcp_prompts import (
    plan_mcp_prompt_commands,
    sync_mcp_prompt_commands_reporting,
)
from amplifier_app_tui.commands.registry import CommandRegistry, CommandSpec
from amplifier_app_tui.kernel.mcp_prompts import MCPPromptInfo


def _existing(name: str, *, tag: str = "built-in") -> CommandSpec:
    return CommandSpec(
        group="During",
        name=name,
        desc="existing",
        tag=tag,
        handler=lambda _ctx, _args: None,
    )


def _prompt(
    command: str = "/github:triage",
    *,
    server: str = "github",
    prompt: str = "triage",
    description: str = "Triage one issue",
) -> MCPPromptInfo:
    return MCPPromptInfo(command, server, prompt, description)


def test_sync_registers_mcp_source_and_forwards_exact_target(fake_command_context) -> None:
    registry = CommandRegistry()
    plan = sync_mcp_prompt_commands_reporting(registry, (_prompt(),))

    assert tuple(spec.name for spec in plan.specs) == ("/github:triage",)
    assert registry.source_of("/github:triage") == "mcp"
    assert registry.get("/github:triage").tag == "mcp"  # type: ignore[union-attr]

    assert registry.parse_and_run(fake_command_context, "/github:triage #42")
    assert fake_command_context.calls == ["run_mcp_prompt:github:triage:#42"]


def test_sync_updates_description_and_removes_stale_commands() -> None:
    registry = CommandRegistry()
    sync_mcp_prompt_commands_reporting(registry, (_prompt(description="old"),))

    sync_mcp_prompt_commands_reporting(registry, (_prompt(description="new"),))
    assert registry.get("/github:triage").desc == "new"  # type: ignore[union-attr]

    sync_mcp_prompt_commands_reporting(registry, ())
    assert registry.get("/github:triage") is None
    assert registry.contributions("mcp") == ()


def test_existing_command_wins_and_collision_is_reported() -> None:
    existing = _existing("/github:triage")
    registry = CommandRegistry((existing,))

    plan = sync_mcp_prompt_commands_reporting(registry, (_prompt(),))

    assert plan.specs == ()
    assert plan.collisions[0].trigger == "/github:triage"
    assert plan.collisions[0].owner == "builtin"
    assert registry.get("/github:triage") is existing


def test_duplicate_normalized_mcp_command_has_deterministic_first_winner() -> None:
    registry = CommandRegistry()
    first = _prompt(server="alpha", prompt="one")
    second = _prompt(server="beta", prompt="two")

    plan = plan_mcp_prompt_commands(registry, (first, second))

    assert len(plan.specs) == 1
    assert plan.collisions == (("/github:triage", "beta:two", "mcp:alpha:one"),)


def test_sync_removes_only_mcp_contributions() -> None:
    registry = CommandRegistry((_existing("/status"),))
    skill = _existing("/review", tag="skill")
    registry.register(skill, source="skill")
    sync_mcp_prompt_commands_reporting(registry, (_prompt(),))

    sync_mcp_prompt_commands_reporting(registry, ())

    assert registry.get("/status") is not None
    assert registry.get("/review") is skill
