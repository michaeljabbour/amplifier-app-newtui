"""The ``/config`` domain model: parser, state, diff, save serialization.

Pure logic (no Textual, no amplifier session), mirroring the donor's
behavioral contract (amplifier-app-cli ``ui/command_config*.py`` +
``amplifier_foundation.configurator.SessionConfigurator``).
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.model.config import (
    CONFIG_CATEGORIES,
    ConfigItem,
    ConfigSnapshotView,
    SessionConfigState,
    default_config_state,
    parse_config_command,
    parse_value,
    state_from_mount_plan,
)


# -- value type inference (donor _handle_config_set) ------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("false", False),
        ("False", False),
        ("42", 42),
        ("-3", -3),
        ("0.8", 0.8),
        ("claude-opus", "claude-opus"),
        ("", ""),
    ],
)
def test_parse_value_infers_type(raw: str, expected: object) -> None:
    assert parse_value(raw) == expected
    assert type(parse_value(raw)) is type(expected)


# -- argument routing (donor _get_config_display) ---------------------------


def test_parse_empty_is_help() -> None:
    assert parse_config_command("").kind == "help"
    assert parse_config_command("   ").kind == "help"


def test_parse_show_variants() -> None:
    assert parse_config_command("show").kind == "show"
    cat = parse_config_command("show tools")
    assert (cat.kind, cat.category) == ("category", "tools")
    item = parse_config_command("show tools bash")
    assert (item.kind, item.category, item.name) == ("item", "tools", "bash")


def test_parse_bare_category_and_item() -> None:
    assert parse_config_command("hooks").kind == "category"
    item = parse_config_command("providers anthropic")
    assert (item.kind, item.category, item.name) == ("item", "providers", "anthropic")


def test_parse_toggle() -> None:
    off = parse_config_command("tools disable bash")
    assert (off.kind, off.category, off.name, off.enable) == (
        "toggle",
        "tools",
        "bash",
        False,
    )
    on = parse_config_command("tools enable bash")
    assert on.enable is True


def test_parse_set_requires_path_and_value() -> None:
    assert parse_config_command("set default_model claude").kind == "set"
    err = parse_config_command("set default_model")
    assert err.kind == "error" and "usage" in err.message


def test_parse_diff_and_save_scope() -> None:
    assert parse_config_command("diff").kind == "diff"
    assert parse_config_command("save").scope == "global"
    assert parse_config_command("save --scope project").scope == "project"
    assert parse_config_command("save local").scope == "local"
    assert parse_config_command("save --scope=global").scope == "global"


def test_parse_unknown_scope_and_subcommand_error() -> None:
    assert parse_config_command("save --scope bogus").kind == "error"
    assert parse_config_command("frobnicate").kind == "error"
    assert parse_config_command("show boguscat").kind == "error"


# -- state: toggle / set / diff / snapshot ----------------------------------


def _state() -> SessionConfigState:
    return SessionConfigState(
        [
            ConfigItem("tools", "bash", True, "tool-shell"),
            ConfigItem("tools", "read_file", True, "tool-filesystem"),
            ConfigItem("hooks", "hooks-mode", True, "hooks-mode"),
            ConfigItem("providers", "anthropic", True, "provider-anthropic"),
        ],
        bundle="anchors",
    )


def test_toggle_round_trips_and_shows_in_diff() -> None:
    state = _state()
    assert state.diff() == ()
    ok, msg = state.toggle("tools", "bash", enable=False)
    assert ok and msg == "\u2713 Disabled bash"
    item = state.find("tools", "bash")
    assert item is not None and item.enabled is False
    (change,) = state.diff()
    assert (change.category, change.name, change.action) == ("tools", "bash", "disabled")
    # Re-enable returns to origin -> no diff.
    state.toggle("tools", "bash", enable=True)
    assert state.diff() == ()


def test_toggle_hooks_is_read_only() -> None:
    state = _state()
    ok, msg = state.toggle("hooks", "hooks-mode", enable=False)
    assert not ok and "read-only" in msg
    assert state.diff() == ()


def test_toggle_unknown_item_refused() -> None:
    state = _state()
    ok, msg = state.toggle("tools", "nope", enable=False)
    assert not ok and "no tools item" in msg


def test_toggle_noop_when_already_in_state() -> None:
    state = _state()
    ok, msg = state.toggle("tools", "bash", enable=True)
    assert not ok and "already enabled" in msg


def test_set_value_round_trips_and_diffs() -> None:
    state = _state()
    ok, msg = state.set_value("session.reasoning_effort", "high")
    assert ok and msg == "\u2713 Set session.reasoning_effort = 'high'"
    assert state.value("session.reasoning_effort") == "high"
    (change,) = state.diff()
    assert change.category == "set" and change.name == "session.reasoning_effort"


def test_snapshot_resets_diff_origin() -> None:
    state = _state()
    state.toggle("tools", "bash", enable=False)
    state.set_value("x", "1")
    assert len(state.diff()) == 2
    state.snapshot()  # adopt the mutated state as the new origin
    assert state.diff() == ()


def test_to_settings_serializes_disables_and_overrides() -> None:
    state = _state()
    state.toggle("tools", "bash", enable=False)
    state.set_value("session.reasoning_effort", "high")
    assert state.to_settings() == {
        "disabled": {"tools": ["bash"]},
        "overrides": {"session.reasoning_effort": "high"},
    }
    # Read-only hooks never land in the serialized disable set.
    assert "hooks" not in state.to_settings().get("disabled", {})


def test_to_settings_empty_when_unchanged() -> None:
    assert _state().to_settings() == {}


# -- seeding from a mount plan ----------------------------------------------


def test_state_from_mount_plan_reads_every_section() -> None:
    plan = {
        "session": {"context": {"module": "context-window"}},
        "providers": [{"module": "provider-anthropic", "id": "anthropic"}],
        "tools": [{"module": "tool-filesystem", "name": "read_file"}, "bash"],
        "hooks": [{"module": "hooks-mode"}],
        "agents": [{"name": "coding"}],
    }
    state = state_from_mount_plan(plan, bundle="anchors")
    names = {(i.category, i.name) for i in state.items()}
    assert ("context", "context-window") in names
    assert ("providers", "anthropic") in names
    assert ("tools", "read_file") in names
    assert ("tools", "bash") in names
    assert ("hooks", "hooks-mode") in names
    assert ("agents", "coding") in names
    # Every category the model advertises renders in a fixed order.
    assert set(CONFIG_CATEGORIES) >= {i.category for i in state.items()}


def test_snapshot_view_is_frozen_and_filterable() -> None:
    state = default_config_state("anchors")
    state.toggle("tools", "bash", enable=False)
    view = ConfigSnapshotView.of(state)
    assert view.bundle == "anchors"
    assert len(view.changes) == 1
    tool_names = {i.name for i in view.items_in("tools")}
    assert "bash" in tool_names
    # Mutating the state afterwards does not mutate the captured view.
    state.toggle("tools", "read_file", enable=False)
    assert len(view.changes) == 1
