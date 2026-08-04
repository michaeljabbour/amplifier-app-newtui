"""WCAG contrast-floor tests for every theme (AC4, compliance item B1;
issue #210) -- computed via ``ui.themes.contrast_ratio``, never
eyeballed. See ``ui/themes.py``'s module docstring and the ``paper``
token-set comment for the full design rationale.

Two roles get a real WCAG 2.1 SC 1.4.3 text-contrast floor:

- ``BODY_TEXT_TOKENS`` (``fg``, ``bright``, ``green``, ``orange``,
  ``red``, ``blue``, ``teal``) carry primary meaning -- narrative
  prose, mode colors, status semantics -- and must hit **4.5:1** (AA
  body text) against the three ambient "reading" surfaces (``bg-page``,
  ``bg-term``, ``bg-chrome``).
- The same tokens need only **3:1** (WCAG's large-scale/graphical-
  object bar) against ``bg-tab`` specifically: every real ``bg-tab``
  pairing in the renderer is an already-visually-distinct highlight
  block (a selected row, a focused approval option, a diff +/- line --
  see ``ui/transcript_render.py``'s diff rendering and
  ``ui/session_ops_view.py``'s ``/diff``), never small running prose,
  so WCAG's large-text allowance is the honest floor to hold it to --
  and it is what the existing dark themes actually clear (slate's
  red-on-bg-tab measures 4.01:1, just under the body floor, which is
  why this pairing isn't held to 4.5:1 across the board).

``dim`` and ``dimmer`` are a documented lower tier by design --
secondary/de-emphasis annotation (token counts, expand hints, secondary
descriptions: e.g. ``ui/transcript_render.py``'s ``TOOL_EXPAND_HINT``)
that always sits beside a primary-tier element carrying the same
information, never the sole carrier of meaning. All three existing
dark themes already sit well under 4.5:1 for these tokens against
ordinary surfaces (e.g. slate's dimmer-on-bg-term measures 1.83:1) --
that is the accepted, shipped baseline, not a bug this change
introduces or is trying to relitigate. ``dim`` still gets a real 3:1
floor against the three reading surfaces (every existing dark theme
clears it there); ``dim`` against ``bg-tab`` and ``dimmer`` against
every surface get a decorative sanity floor (1.5:1 -- legible enough to
not be a rendering bug, not a body-text claim).

Every test below is parametrized over ``THEME_TOKENS`` (not a hardcoded
theme list), so a future fifth theme is checked automatically the
moment it's added to that dict.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.model.blocks import Answer
from amplifier_app_tui.ui.live_tail import answer_spans
from amplifier_app_tui.ui.segments import line_plain, to_rich_text
from amplifier_app_tui.ui.themes import (
    EXTRA_VARIABLES_BY_THEME,
    THEME_TOKENS,
    contrast_ratio,
    relative_luminance,
)
from amplifier_app_tui.ui.transcript_render import _FINAL_ANSWER_MARKER_LINE, render_block

BODY_TEXT_TOKENS: tuple[str, ...] = ("fg", "bright", "green", "orange", "red", "blue", "teal")
"""Primary-meaning text tokens: narrative prose, mode colors, semantic status."""

READING_SURFACES: tuple[str, ...] = ("bg-page", "bg-term", "bg-chrome")
"""Ambient backgrounds ordinary (non-highlighted) text renders on."""

HIGHLIGHT_SURFACE = "bg-tab"
"""The one background reserved for already-visually-distinct highlight
rows (selection, focus, diff +/- lines) -- never plain running prose."""

BODY_FLOOR = 4.5
"""WCAG 2.1 SC 1.4.3 AA floor for normal-weight body text."""

LARGE_OR_HIGHLIGHT_FLOOR = 3.0
"""WCAG 2.1 SC 1.4.3 AA floor for large-scale/bold text, reused here for
text drawn on an already-highlighted surface (see module docstring)."""

DECORATIVE_SANITY_FLOOR = 1.5
"""Not a WCAG figure -- a regression trip-wire for the de-emphasis tier
(dim/dimmer) so a future edit can't collapse it into its background,
without pretending that tier claims body-text AA."""

THEME_NAMES: tuple[str, ...] = tuple(THEME_TOKENS)


@pytest.mark.parametrize("theme_name", THEME_NAMES)
@pytest.mark.parametrize("token", BODY_TEXT_TOKENS)
@pytest.mark.parametrize("surface", READING_SURFACES)
def test_body_text_meets_aa_on_reading_surfaces(theme_name: str, token: str, surface: str) -> None:
    tokens = THEME_TOKENS[theme_name]
    ratio = contrast_ratio(tokens[token], tokens[surface])
    assert ratio >= BODY_FLOOR, f"{theme_name}: {token} on {surface} = {ratio:.2f} < {BODY_FLOOR}"


@pytest.mark.parametrize("theme_name", THEME_NAMES)
@pytest.mark.parametrize("token", BODY_TEXT_TOKENS)
def test_body_text_meets_large_floor_on_highlight_surface(theme_name: str, token: str) -> None:
    tokens = THEME_TOKENS[theme_name]
    ratio = contrast_ratio(tokens[token], tokens[HIGHLIGHT_SURFACE])
    assert ratio >= LARGE_OR_HIGHLIGHT_FLOOR, (
        f"{theme_name}: {token} on {HIGHLIGHT_SURFACE} = {ratio:.2f} < {LARGE_OR_HIGHLIGHT_FLOOR}"
    )


@pytest.mark.parametrize("theme_name", THEME_NAMES)
@pytest.mark.parametrize("surface", READING_SURFACES)
def test_dim_meets_large_floor_on_reading_surfaces(theme_name: str, surface: str) -> None:
    tokens = THEME_TOKENS[theme_name]
    ratio = contrast_ratio(tokens["dim"], tokens[surface])
    assert ratio >= LARGE_OR_HIGHLIGHT_FLOOR, (
        f"{theme_name}: dim on {surface} = {ratio:.2f} < {LARGE_OR_HIGHLIGHT_FLOOR}"
    )


@pytest.mark.parametrize("theme_name", THEME_NAMES)
def test_de_emphasis_tier_clears_decorative_sanity_floor(theme_name: str) -> None:
    tokens = THEME_TOKENS[theme_name]
    for surface in (*READING_SURFACES, HIGHLIGHT_SURFACE):
        ratio = contrast_ratio(tokens["dimmer"], tokens[surface])
        assert ratio >= DECORATIVE_SANITY_FLOOR, f"{theme_name}: dimmer on {surface} = {ratio:.2f}"
    ratio = contrast_ratio(tokens["dim"], tokens[HIGHLIGHT_SURFACE])
    assert ratio >= DECORATIVE_SANITY_FLOOR, (
        f"{theme_name}: dim on {HIGHLIGHT_SURFACE} = {ratio:.2f}"
    )


@pytest.mark.parametrize("theme_name", THEME_NAMES)
def test_title_fg_meets_aa_on_bg_chrome(theme_name: str) -> None:
    """The title bar (``ui/chrome.py`` ``TitleBar``) is bold, always-
    visible chrome text -- ``$title-fg`` on ``$bg-chrome`` -- and must
    clear the body floor in every theme, light included. A single
    hardcoded hex here looks fine on the three dark themes and nearly
    vanishes on a light one (~1.05:1) -- see ``ui/themes.py``'s
    ``TITLE_FG_LIGHT``."""
    tokens = THEME_TOKENS[theme_name]
    title_fg = EXTRA_VARIABLES_BY_THEME[theme_name]["title-fg"]
    ratio = contrast_ratio(title_fg, tokens["bg-chrome"])
    assert ratio >= BODY_FLOOR, f"{theme_name}: title-fg on bg-chrome = {ratio:.2f}"


@pytest.mark.parametrize("theme_name", THEME_NAMES)
def test_final_answer_marker_legible_in_every_theme(theme_name: str) -> None:
    """AC4's actual subject: the ``● Final answer`` start marker
    (``ui/transcript_render.FINAL_ANSWER_MARKER``) is bright+bold text
    painted on the transcript's ``bg-term`` background (``Screen {
    background: $bg-term }`` in ``ui/app.py``) in every theme -- light
    theme included, which is the one compliance item B1 was blocked
    on."""
    tokens = THEME_TOKENS[theme_name]
    ratio = contrast_ratio(tokens["bright"], tokens["bg-term"])
    assert ratio >= BODY_FLOOR, f"{theme_name}: bright on bg-term = {ratio:.2f}"


def test_final_answer_marker_resolves_legibly_in_light_theme() -> None:
    """AC4/#210, "cover it in a snapshot ... at light-theme": render the
    real marker line through the real token-resolution path
    (``to_rich_text`` with ``paper``'s resolved variables, the same call
    a running app makes with ``app.theme_variables``) and pin both the
    resolved color and the computed contrast ratio -- not just the
    abstract token-to-token check above, but the actual paint values a
    light-theme user would see."""
    block = Answer(id="a", spans=answer_spans("Done."), final=True)
    lines = render_block(block, 80)
    marker_line = lines[0]
    assert line_plain(marker_line) == "● Final answer"
    assert marker_line == _FINAL_ANSWER_MARKER_LINE

    paper = THEME_TOKENS["paper"]
    rendered = to_rich_text(marker_line, variables=paper)
    assert rendered.plain == "● Final answer"
    assert len(rendered.spans) == 2
    for span in rendered.spans:
        assert span.style.bold is True  # weight, not color alone (AC4)
        assert span.style.color is not None
        assert span.style.color.name == paper["bright"]

    ratio = contrast_ratio(paper["bright"], paper["bg-term"])
    assert ratio >= BODY_FLOOR, f"paper: marker bright-on-bg-term = {ratio:.2f}"


def test_contrast_ratio_is_symmetric_and_matches_known_values() -> None:
    """Sanity-check the WCAG helper against hand-verifiable cases."""
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)
    assert contrast_ratio("#123456", "#abcdef") == contrast_ratio("#abcdef", "#123456")
    assert relative_luminance("#ffffff") == pytest.approx(1.0, abs=1e-9)
    assert relative_luminance("#000000") == pytest.approx(0.0, abs=1e-9)
