"""Tests for theme tokens (ui/themes.py) — exact hex per DESIGN-SPEC §1."""

from __future__ import annotations

import pytest

from amplifier_app_tui.ui.themes import (
    DEFAULT_THEME,
    THEME_TOKENS,
    THEMES,
    TOKEN_NAMES,
    theme_id,
)

# The DESIGN-SPEC §1 table, verbatim.
SPEC_TABLE: dict[str, dict[str, str]] = {
    "slate": {
        "bg-page": "#12151c",
        "bg-term": "#232937",
        "bg-chrome": "#191d27",
        "bg-tab": "#2b3243",
        "fg": "#c9d1e0",
        "bright": "#eef2f8",
        "dim": "#6b7487",
        "dimmer": "#4a5163",
        "green": "#7ec699",
        "orange": "#e0a458",
        "red": "#e06c75",
        "blue": "#7aa2f7",
        "teal": "#6fc3c3",
        "rule": "#333b4d",
    },
    "graphite": {
        "bg-page": "#131110",
        "bg-term": "#211e1a",
        "bg-chrome": "#181512",
        "bg-tab": "#2c2722",
        "fg": "#d6cfc4",
        "bright": "#f2ede4",
        "dim": "#8a8175",
        "dimmer": "#575047",
        "green": "#98c28b",
        "orange": "#dba15c",
        "red": "#d97371",
        "blue": "#90a4d8",
        "teal": "#80bcae",
        "rule": "#3a352e",
    },
    "carbon": {
        "bg-page": "#0c0e12",
        "bg-term": "#14171d",
        "bg-chrome": "#0f1116",
        "bg-tab": "#1f242e",
        "fg": "#cdd6e4",
        "bright": "#f4f7fc",
        "dim": "#65718a",
        "dimmer": "#3d4657",
        "green": "#6fd39c",
        "orange": "#e9b14f",
        "red": "#ef6e7b",
        "blue": "#6f9df2",
        "teal": "#57c8c8",
        "rule": "#2a3140",
    },
    "paper": {
        "bg-page": "#e7e2d3",
        "bg-term": "#f7f5f0",
        "bg-chrome": "#efece3",
        "bg-tab": "#dcd4bf",
        "fg": "#3a352c",
        "bright": "#1c1812",
        "dim": "#6e6656",
        "dimmer": "#948c78",
        "green": "#146536",
        "orange": "#8a4d0a",
        "red": "#9c2f27",
        "blue": "#1a45b8",
        "teal": "#0a5f58",
        "rule": "#cfc6ae",
    },
}

DARK_THEMES = ("slate", "graphite", "carbon")
LIGHT_THEMES = ("paper",)


def test_four_themes_exist() -> None:
    assert set(THEMES) == {"slate", "graphite", "carbon", "paper"}
    assert DEFAULT_THEME == "slate"


def test_every_token_hex_matches_spec_exactly() -> None:
    for theme_name, tokens in SPEC_TABLE.items():
        for token, hex_value in tokens.items():
            assert THEME_TOKENS[theme_name][token] == hex_value, (theme_name, token)


def test_no_extra_or_missing_tokens() -> None:
    for theme_name in SPEC_TABLE:
        assert set(THEME_TOKENS[theme_name]) == set(TOKEN_NAMES)
    assert len(TOKEN_NAMES) == 14


def test_textual_theme_variables_expose_every_token() -> None:
    """Widgets style via $<token> — every spec token must be a theme variable."""
    for theme_name, theme in THEMES.items():
        for token in TOKEN_NAMES:
            assert theme.variables[token] == SPEC_TABLE[theme_name][token], (
                theme_name,
                token,
            )


def test_theme_names_are_registered_ids() -> None:
    for theme_name, theme in THEMES.items():
        assert theme.name == theme_id(theme_name) == f"amplifier-{theme_name}"


def test_dark_flag_matches_theme_kind() -> None:
    """AC4/#210: the three original themes are dark; ``paper`` is light --
    Textual's own ``dark`` flag must say so (it drives fallback ANSI
    color mapping, not just cosmetics)."""
    for theme_name in DARK_THEMES:
        assert THEMES[theme_name].dark is True, theme_name
    for theme_name in LIGHT_THEMES:
        assert THEMES[theme_name].dark is False, theme_name


def test_semantic_slots_come_from_tokens() -> None:
    """Textual's built-in slots must reuse spec tokens, not invent colors."""
    for theme_name, theme in THEMES.items():
        tokens = SPEC_TABLE[theme_name]
        assert theme.background == tokens["bg-term"]
        assert theme.surface == tokens["bg-chrome"]
        assert theme.panel == tokens["bg-tab"]
        assert theme.foreground == tokens["fg"]
        assert theme.success == tokens["green"]
        assert theme.warning == tokens["orange"]
        assert theme.error == tokens["red"]


def test_hex_values_live_only_in_themes_module() -> None:
    """No hard-coded hex colors anywhere outside ui/themes.py."""
    import re
    from pathlib import Path

    import amplifier_app_tui

    package_root = Path(amplifier_app_tui.__file__).parent
    hex_pattern = re.compile(r"#[0-9a-fA-F]{6}\b")
    offenders: list[str] = []
    for path in package_root.rglob("*.py"):
        if path.name == "themes.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if hex_pattern.search(line):
                offenders.append(f"{path.relative_to(package_root)}:{number}: {line.strip()}")
    assert not offenders, f"hex colors outside themes.py: {offenders}"


def test_theme_command_is_registered() -> None:
    """DESIGN-SPEC §1: theme switchable at runtime via a command surface."""
    from amplifier_app_tui.commands.builtin import build_registry

    spec = build_registry().get("/theme")
    assert spec is not None
    assert spec.desc == "switch theme: slate, graphite, carbon, paper"


@pytest.mark.asyncio
async def test_theme_switch_at_runtime_via_command() -> None:
    from amplifier_app_tui.ui.app import TuiApp
    from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

    from .test_flow_helpers import SIZE, seed_done, type_text

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert app.theme == theme_id(DEFAULT_THEME)  # default slate

        # /theme <name> jumps straight to that theme.
        await type_text(pilot, "/theme graphite")
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == theme_id("graphite")
        assert app.notice_slot.current == "theme graphite"

        # Bare /theme cycles graphite → carbon → paper (AC4/#210) → slate.
        await type_text(pilot, "/theme")
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == theme_id("carbon")

        await type_text(pilot, "/theme")
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == theme_id("paper")
        assert app.notice_slot.current == "theme paper"

        await type_text(pilot, "/theme")
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == theme_id("slate")

        # Unknown names change nothing and explain the choices.
        await type_text(pilot, "/theme neon")
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == theme_id("slate")
        assert app.notice_slot.current == (
            "unknown theme · neon · themes: slate, graphite, carbon, paper"
        )

        # The light theme is reachable directly by name too.
        await type_text(pilot, "/theme paper")
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == theme_id("paper")
        assert app.notice_slot.current == "theme paper"
