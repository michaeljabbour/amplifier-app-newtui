"""Tests for theme tokens (ui/themes.py) — exact hex per DESIGN-SPEC §1."""

from __future__ import annotations

from amplifier_app_newtui.ui.themes import (
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
}


def test_three_themes_exist() -> None:
    assert set(THEMES) == {"slate", "graphite", "carbon"}
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
        assert theme.dark is True


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

    import amplifier_app_newtui

    package_root = Path(amplifier_app_newtui.__file__).parent
    hex_pattern = re.compile(r"#[0-9a-fA-F]{6}\b")
    offenders: list[str] = []
    for path in package_root.rglob("*.py"):
        if path.name == "themes.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if hex_pattern.search(line):
                offenders.append(f"{path.relative_to(package_root)}:{number}: {line.strip()}")
    assert not offenders, f"hex colors outside themes.py: {offenders}"
