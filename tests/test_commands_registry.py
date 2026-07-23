"""Registry mechanics: registration, palette filtering, grouping, dispatch."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.commands.registry import (
    GROUP_ORDER,
    CommandContext,
    CommandRegistry,
    CommandSpec,
)


def _spec(
    name: str,
    group: str = "During",
    key_action: str | None = None,
    tag: str = "built-in",
) -> CommandSpec:
    def handler(ctx, args: str) -> None:
        ctx.calls.append(f"ran:{name}:{args}")

    return CommandSpec(
        group=group,  # type: ignore[arg-type]
        name=name,
        desc=f"desc for {name}",
        tag=tag,
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
    registry = CommandRegistry((_spec("/rewind", "Between"), _spec("/brainstorm"), _spec("/mode")))
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


# --- open registry: dynamic contributions tagged by source (story #2) ---


def test_register_returns_true_and_records_source() -> None:
    registry = CommandRegistry((_spec("/mode"),))
    assert registry.register(_spec("/review", tag="skill"), source="skill")
    assert registry.source_of("/review") == "skill"
    assert registry.source_of("/mode") == "builtin"  # seeded specs are built-ins
    assert registry.source_of("/nope") is None


def test_dynamic_collision_skips_with_log_and_builtin_wins(caplog) -> None:
    builtin = _spec("/status")
    registry = CommandRegistry((builtin,))
    import logging

    with caplog.at_level(logging.WARNING):
        assert not registry.register(_spec("/status", tag="skill"), source="skill")
    assert "/status" in caplog.text
    # The built-in survives untouched; order and lookup unchanged.
    assert registry.get("/status") is builtin
    assert registry.names == ("/status",)
    assert registry.source_of("/status") == "builtin"


def test_first_dynamic_registration_wins_over_later_ones() -> None:
    registry = CommandRegistry()
    first = _spec("/approve", tag="skill")
    assert registry.register(first, source="skill")
    assert not registry.register(_spec("/approve", tag="recipe"), source="recipe")
    assert registry.get("/approve") is first
    assert registry.source_of("/approve") == "skill"


def test_builtin_duplicate_still_raises() -> None:
    registry = CommandRegistry((_spec("/mode"),))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec("/mode"))  # default source is builtin


def test_future_sources_register_without_registry_changes(fake_command_context) -> None:
    # Acceptance: recipe/pipeline verbs must be registerable later with no
    # further registry changes — open source label, open display tag.
    registry = CommandRegistry((_spec("/mode"),))
    assert registry.register(_spec("/recipe-approve", "Parallel", tag="recipe"), source="recipe")
    assert registry.register(
        _spec("/pipeline-status", "Parallel", tag="pipeline"), source="pipeline"
    )
    assert registry.parse_and_run(fake_command_context, "/recipe-approve now")
    assert fake_command_context.calls == ["ran:/recipe-approve:now"]
    assert registry.get("/pipeline-status") is not None


def test_contributions_filter_by_source_in_registration_order() -> None:
    registry = CommandRegistry((_spec("/mode"),))
    a = _spec("/aa", tag="skill")
    b = _spec("/bb", tag="recipe")
    c = _spec("/cc", tag="skill")
    registry.register(a, source="skill")
    registry.register(b, source="recipe")
    registry.register(c, source="skill")
    assert registry.contributions("skill") == (a, c)
    assert registry.contributions("recipe") == (b,)
    assert registry.contributions("builtin") == (registry.get("/mode"),)
    assert registry.contributions("pipeline") == ()


def test_unregister_removes_dynamic_command_and_keeps_order() -> None:
    registry = CommandRegistry((_spec("/mode"),))
    registry.register(_spec("/aa", tag="skill"), source="skill")
    registry.register(_spec("/bb", tag="skill"), source="skill")
    assert registry.unregister("/aa")
    assert registry.names == ("/mode", "/bb")  # stable order for palette/help
    assert registry.get("/aa") is None
    assert registry.source_of("/aa") is None
    assert not registry.unregister("/aa")  # already gone → False, no raise


def test_unregister_builtin_is_refused() -> None:
    registry = CommandRegistry((_spec("/mode"),))
    with pytest.raises(ValueError, match="built-in"):
        registry.unregister("/mode")
    assert registry.get("/mode") is not None


def test_subscribers_hear_successful_changes_only() -> None:
    registry = CommandRegistry((_spec("/mode"),))
    pings: list[int] = []
    registry.subscribe(lambda: pings.append(len(registry.specs)))
    registry.register(_spec("/aa", tag="skill"), source="skill")
    registry.register(_spec("/aa", tag="skill"), source="skill")  # skipped: silent
    registry.register(_spec("/mode2"))
    registry.unregister("/aa")
    assert not registry.unregister("/zz")  # no-op: silent
    assert pings == [2, 3, 2]
