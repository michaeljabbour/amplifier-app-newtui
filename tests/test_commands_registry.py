"""Registry mechanics: registration, palette filtering, grouping, dispatch."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.commands.registry import (
    GROUP_ORDER,
    CommandContext,
    CommandRegistry,
    CommandSpec,
)


def _spec(name: str, group: str = "During", key_action: str | None = None) -> CommandSpec:
    def handler(ctx, args: str) -> None:
        ctx.calls.append(f"ran:{name}:{args}")

    return CommandSpec(
        group=group,  # type: ignore[arg-type]
        name=name,
        desc=f"desc for {name}",
        tag="built-in",
        handler=handler,
        key_action=key_action,
    )


def test_fake_context_satisfies_protocol(fake_command_context) -> None:
    assert isinstance(fake_command_context, CommandContext)


def test_register_and_lookup() -> None:
    registry = CommandRegistry()
    registry.register(_spec("/mode"))
    assert registry.get("/mode") is not None
    assert registry.get(" /mode ") is not None
    assert registry.get("/nope") is None
    assert registry.names == ("/mode",)


def test_duplicate_name_rejected() -> None:
    registry = CommandRegistry((_spec("/mode"),))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec("/mode"))


def test_name_must_be_slash_trigger() -> None:
    with pytest.raises(ValueError):
        _spec("mode")
    with pytest.raises(ValueError):
        _spec("/")
    with pytest.raises(ValueError):
        _spec("/two words")


def test_filter_rows_substring_semantics() -> None:
    registry = CommandRegistry(
        (_spec("/rewind", "Between"), _spec("/brainstorm"), _spec("/mode"))
    )
    # "/" and empty show everything, in registration order.
    assert registry.filter_rows("/") == registry.specs
    assert registry.filter_rows("") == registry.specs
    # Substring of the command name, mockup semantics.
    assert [s.name for s in registry.filter_rows("/re")] == ["/rewind"]
    assert [s.name for s in registry.filter_rows("rain")] == ["/brainstorm"]
    assert registry.filter_rows("/zzz") == ()


def test_group_headers_only_for_bare_slash() -> None:
    assert CommandRegistry.show_group_headers("/")
    assert CommandRegistry.show_group_headers(" / ")
    assert not CommandRegistry.show_group_headers("/re")
    assert not CommandRegistry.show_group_headers("")


def test_grouped_rows_follow_group_order_and_skip_empty() -> None:
    registry = CommandRegistry(
        (
            _spec("/rewind", "Between"),
            _spec("/mode", "During"),
            _spec("/doctor", "Repair"),
        )
    )
    grouped = registry.grouped_rows("/")
    assert [group for group, _ in grouped] == ["During", "Between", "Repair"]
    assert list(GROUP_ORDER) == ["During", "Parallel", "Ship", "Between", "Repair"]


def test_run_echoes_user_line_then_dispatches(fake_command_context) -> None:
    registry = CommandRegistry((_spec("/mode"),))
    registry.run("/mode", fake_command_context, "plan")
    # DESIGN-SPEC §6: running a command echoes it as a user line first.
    assert fake_command_context.user_lines == ["/mode plan"]
    assert fake_command_context.calls == ["ran:/mode:plan"]


def test_run_without_args_echoes_bare_command(fake_command_context) -> None:
    registry = CommandRegistry((_spec("/tasks", "Parallel"),))
    registry.run("/tasks", fake_command_context)
    assert fake_command_context.user_lines == ["/tasks"]


def test_run_unknown_raises(fake_command_context) -> None:
    registry = CommandRegistry()
    with pytest.raises(KeyError):
        registry.run("/nope", fake_command_context)


def test_parse_and_run(fake_command_context) -> None:
    registry = CommandRegistry((_spec("/mode"),))
    assert registry.parse_and_run(fake_command_context, "/mode build")
    assert fake_command_context.calls == ["ran:/mode:build"]
    assert not registry.parse_and_run(fake_command_context, "hello world")
    assert not registry.parse_and_run(fake_command_context, "/unknown")


def test_keybound_maps_key_actions_to_specs() -> None:
    tasks = _spec("/tasks", "Parallel", key_action="toggle_lanes")
    registry = CommandRegistry((_spec("/mode"), tasks))
    assert registry.keybound() == {"toggle_lanes": tasks}
