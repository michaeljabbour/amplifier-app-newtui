"""The four spec themes as Textual Theme objects (DESIGN-SPEC §1).

This is the ONLY module in the codebase allowed to contain hex color
values. Every theme exposes ALL fourteen spec tokens as Textual theme
*variables* named exactly after the spec tokens (``$bg-page``,
``$bg-term``, … in TCSS), so widgets/styles reference tokens by name and
a runtime theme switch (``App.theme = "amplifier-graphite"``) is a
repaint, not a rebuild (ADR-0007 resolution 11).

Default theme: ``slate``.

AC4 (compliance 2026-08-02, item B1; resolved 2026-08-04): three DARK
token sets (``slate``/``graphite``/``carbon``) plus one LIGHT token set
(``paper``) below. The final-answer start marker (``model/blocks.py``'s
``Answer.final``, rendered by ``ui/transcript_render.py``'s
``FINAL_ANSWER_MARKER``) is built from a label + bold weight, never
color alone, and is now verified legible in all four themes, light
included (``tests/test_ui_theme_contrast.py``). A prior round narrowed
AC4 to the three dark themes and tracked a light theme separately as
issue #210 (see docs/BACKLOG.md); that narrowing is reverted here and
#210 is closed by this change -- ``paper`` is a real, selectable,
contrast-tested theme, not a placeholder.

``paper``'s hex values are original to this module (the mockup,
``design-v3-cohesive.html``, only ever specified dark tokens) -- they
were chosen by computing WCAG 2.1 relative-luminance contrast ratios
against every surface a theme pairs text with, not eyeballed. See
``relative_luminance``/``contrast_ratio`` below and the floor tables in
``tests/test_ui_theme_contrast.py``.
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

# Exact hex values from the DESIGN-SPEC §1 table -- do not adjust the three
# dark themes below (slate/graphite/carbon); ``paper`` is this repo's own
# addition (AC4/#210) and its values are computed, not spec-sourced -- see
# the module docstring and tests/test_ui_theme_contrast.py.
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
    "paper": {
        # Light theme (AC4/#210). Backgrounds run light -> lightest
        # (bg-chrome, bg-page, bg-term) with bg-tab the one deliberately
        # MORE-shaded surface -- the same "selection/highlight" role
        # bg-tab plays in the dark themes above, just inverted: a dark
        # theme elevates a highlight by lightening it (bg-tab is the
        # lightest of its four); a light theme can't go lighter than its
        # background, so paper elevates by shading instead (bg-tab is
        # the darkest of its four). fg/bright/dim/dimmer mirror the dark
        # themes' roles in the same direction (bright is the strongest
        # emphasis -- nearest-black here, nearest-white there).
        # green/orange/red/blue/teal keep each dark theme's hue family
        # but are darkened/saturated so they clear this module's
        # contrast floors against every surface they render on (diff
        # +/- highlight rows, mode badges, semantic status colors --
        # see tests/test_ui_theme_contrast.py for the exact pairs and
        # floors, all computed via ``contrast_ratio`` below).
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
"""Theme name → {token name → exact hex}."""


def relative_luminance(hex_color: str) -> float:
    """WCAG 2.1 relative luminance of a ``#rrggbb`` color (0.0-1.0).

    https://www.w3.org/TR/WCAG21/#dfn-relative-luminance -- each sRGB
    channel is linearized (the standard's piecewise gamma curve) then
    combined with the ITU-R BT.709 luma weights. Pure math, no theme
    lookup, so it works on any hex string -- including ones this module
    doesn't own -- but every hex value it's ever called with in this
    codebase still lives only here (``THEME_TOKENS``/``TITLE_FG*``).
    """
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def _linearize(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG 2.1 contrast ratio between two ``#rrggbb`` colors (1.0-21.0).

    https://www.w3.org/TR/WCAG21/#dfn-contrast-ratio -- ``(L1 + 0.05) /
    (L2 + 0.05)`` with ``L1`` the lighter of the pair's relative
    luminances, so argument order never matters. This is the "compute
    actual contrast ratios" half of AC4's validation
    (``tests/test_ui_theme_contrast.py`` asserts floors with it for
    every theme, light and dark) -- never eyeballed.
    """
    l_a, l_b = relative_luminance(hex_a), relative_luminance(hex_b)
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


TITLE_FG = "#aeb6c6"
"""Title bar text color for the three dark themes -- hardcoded in the
mockup's window chrome (design-v3-cohesive.html line 39, ``color:
#aeb6c6; font-weight: 600``); deliberately NOT part of the §1 token
table. Kept byte-identical for slate/graphite/carbon (this change
alters no existing pixel); see ``TITLE_FG_LIGHT`` for why ``paper``
can't reuse it."""

TITLE_FG_LIGHT = "#2a251c"
"""``paper``'s title-fg (AC4/#210). The mockup never rendered a light
theme, so it never specified a light title-fg either, and reusing
``TITLE_FG`` verbatim would be almost invisible on a light
``bg-chrome`` (~1.05:1 -- title text and background are both light).
``TITLE_FG_LIGHT`` instead occupies the same relative position
``TITLE_FG`` holds in the dark themes -- between that theme's ``fg``
and ``bright`` -- landing at ~12.9:1 against paper's ``bg-chrome``."""

EXTRA_VARIABLES_BY_THEME: dict[str, dict[str, str]] = {
    "slate": {"title-fg": TITLE_FG},
    "graphite": {"title-fg": TITLE_FG},
    "carbon": {"title-fg": TITLE_FG},
    "paper": {"title-fg": TITLE_FG_LIGHT},
}
"""Per-theme extra variables outside the §1 token table (``$title-fg``
in TCSS) -- hex still lives only in this module."""

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
    ``$rule`` directly -- the token names ARE the variable names.
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
        dark=name != "paper",
        variables={**tokens, **EXTRA_VARIABLES_BY_THEME[name]},
    )


THEMES: dict[str, Theme] = {
    name: _build_theme(name, tokens) for name, tokens in THEME_TOKENS.items()
}
"""Spec theme name (``slate``/``graphite``/``carbon``/``paper``) → Textual Theme."""


def register_themes(app) -> None:  # type: ignore[no-untyped-def]
    """Register all four spec themes on a Textual App.

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
    "EXTRA_VARIABLES_BY_THEME",
    "TITLE_FG",
    "TITLE_FG_LIGHT",
    "THEME_NAME_PREFIX",
    "THEME_TOKENS",
    "THEMES",
    "TOKEN_NAMES",
    "contrast_ratio",
    "register_themes",
    "relative_luminance",
    "theme_id",
]
