"""The three spec themes as Textual Theme objects (DESIGN-SPEC §1).

This is the ONLY module in the codebase allowed to contain hex color
values. Every theme exposes ALL fourteen spec tokens as Textual theme
*variables* named exactly after the spec tokens (``$bg-page``,
``$bg-term``, … in TCSS), so widgets/styles reference tokens by name and
a runtime theme switch (``App.theme = "amplifier-graphite"``) is a
repaint, not a rebuild (ADR-0007 resolution 11).

Default theme: ``slate``.
"""

from __future__ import annotations

from textual.theme import Theme

TOKEN_NAMES: tuple[str, ...] = (
    "bg-page",
    "bg-term",
    "bg-chrome",
    "bg-tab",
    "fg",
    "bright",
    "dim",
    "dimmer",
    "green",
    "orange",
    "red",
    "blue",
    "teal",
    "rule",
)
"""Every DESIGN-SPEC §1 token, in spec-table order."""

# Exact hex values from the DESIGN-SPEC §1 table — do not adjust.
THEME_TOKENS: dict[str, dict[str, str]] = {
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
"""Theme name → {token name → exact spec hex}."""

DEFAULT_THEME = "slate"
THEME_NAME_PREFIX = "amplifier-"


def theme_id(name: str) -> str:
    """Registered Textual theme name for a spec theme (``amplifier-slate``)."""
    return f"{THEME_NAME_PREFIX}{name}"


def _build_theme(name: str, tokens: dict[str, str]) -> Theme:
    """Assemble one spec theme.

    Textual's semantic slots map onto spec tokens (background/surface/
    panel/foreground etc.) so built-in widgets look right, and the full
    token table rides in ``variables`` so app TCSS uses ``$bg-page`` …
    ``$rule`` directly — the token names ARE the variable names.
    """
    return Theme(
        name=theme_id(name),
        primary=tokens["blue"],
        secondary=tokens["teal"],
        background=tokens["bg-term"],
        surface=tokens["bg-chrome"],
        panel=tokens["bg-tab"],
        foreground=tokens["fg"],
        success=tokens["green"],
        warning=tokens["orange"],
        error=tokens["red"],
        accent=tokens["orange"],
        dark=True,
        variables=dict(tokens),
    )


THEMES: dict[str, Theme] = {
    name: _build_theme(name, tokens) for name, tokens in THEME_TOKENS.items()
}
"""Spec theme name (``slate``/``graphite``/``carbon``) → Textual Theme."""


def register_themes(app) -> None:  # type: ignore[no-untyped-def]
    """Register all three spec themes on a Textual App.

    Call from ``App.__init__`` (right after ``super().__init__()``),
    then set ``app.theme = theme_id(DEFAULT_THEME)``. ``on_mount`` is
    TOO LATE: widget ``DEFAULT_CSS`` referencing the spec token
    variables (``$bg-chrome``, …) is parsed against the current theme's
    variables before ``on_mount`` fires, and the app crashes with
    "reference to undefined variable". (Typed loosely to avoid a hard
    textual.App import at module scope.)
    """
    for theme in THEMES.values():
        app.register_theme(theme)


__all__ = [
    "DEFAULT_THEME",
    "THEME_NAME_PREFIX",
    "THEME_TOKENS",
    "THEMES",
    "TOKEN_NAMES",
    "register_themes",
    "theme_id",
]
