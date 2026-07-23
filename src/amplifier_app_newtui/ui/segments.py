"""Segment lists → Textual/Rich renderables, styled ONLY by theme tokens.

The transcript renderer (``ui/transcript.py``) produces lines of
:class:`~amplifier_app_newtui.model.blocks.Segment` — plain data naming
DESIGN-SPEC §1 tokens. This module converts those segments into paintable
form without ever touching a color value:

- :func:`segment_style` / :func:`line_markup` / :func:`lines_markup` emit
  Textual *content markup* whose styles reference theme **variables**
  (``[bold $green]…[/]``). Textual resolves ``$green`` against the active
  theme's variables at paint time (our themes register every spec token as
  a variable — see ``ui/themes.py``), so a runtime theme switch is a
  repaint, not a rebuild (ADR-0007 resolution 11).
- :func:`to_rich_text` builds a ``rich.text.Text`` for callers that hold a
  resolved token→color mapping (``app.theme_variables``); the mapping is
  the only place a concrete color ever appears, and it comes from the
  theme, never from this module.
- :func:`line_plain` / :func:`lines_plain` are the style-free projections
  the golden tests assert exact glyph/label text against.

No hex values appear here; ``tests/test_ui_themes.py`` enforces that
repo-wide.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from rich.style import Style
from rich.text import Text
from textual.markup import escape

from ..model.blocks import Segment

Line = tuple[Segment, ...]
"""One rendered transcript line: a run of styled segments."""


def segment_style(segment: Segment) -> str:
    """The Textual style string for a segment: ``bold italic $teal on $bg-tab``.

    Tokens are referenced by variable name (``$<token>``) — never by value.
    """
    parts: list[str] = []
    if segment.bold:
        parts.append("bold")
    if segment.italic:
        parts.append("italic")
    parts.append(f"${segment.style_token}")
    if segment.bg_token is not None:
        parts.append(f"on ${segment.bg_token}")
    return " ".join(parts)


def segment_markup(segment: Segment) -> str:
    """One segment as Textual content markup (text escaped, style by token).

    A segment carrying a ``link`` nests a ``[link=…]`` tag so the terminal
    paints a real OSC 8 hyperlink (Textual emits the escape); the URL is
    kept clean of ``]`` at the source so it never breaks the markup.
    """
    if not segment.text:
        return ""
    body = escape(segment.text)
    if segment.link:
        body = f"[link={segment.link}]{body}[/link]"
    return f"[{segment_style(segment)}]{body}[/]"


def line_markup(line: Iterable[Segment]) -> str:
    """A whole line of segments as one markup string."""
    return "".join(segment_markup(segment) for segment in line)


def lines_markup(lines: Iterable[Iterable[Segment]]) -> str:
    """Multiple lines joined with newlines — the form widgets paint."""
    return "\n".join(line_markup(line) for line in lines)


def line_plain(line: Iterable[Segment]) -> str:
    """Style-free text of a line (what golden tests assert against)."""
    return "".join(segment.text for segment in line)


def lines_plain(lines: Iterable[Iterable[Segment]]) -> str:
    """Style-free text of many lines, newline-joined."""
    return "\n".join(line_plain(line) for line in lines)


def to_rich_text(line: Iterable[Segment], variables: Mapping[str, str] | None = None) -> Text:
    """A line as ``rich.text.Text``.

    ``variables`` maps token name → resolved color (pass
    ``app.theme_variables``); with ``None`` the Text carries structure but
    no colors (useful for width measurement and tests). Colors resolved
    this way still come exclusively from the theme.
    """
    text = Text()
    for segment in line:
        if not segment.text:
            continue
        color = variables.get(segment.style_token) if variables else None
        bgcolor = (
            variables.get(segment.bg_token) if variables and segment.bg_token is not None else None
        )
        text.append(
            segment.text,
            style=Style(
                color=color,
                bgcolor=bgcolor,
                bold=segment.bold or None,
                italic=segment.italic or None,
                link=segment.link,
            ),
        )
    return text


__all__ = [
    "Line",
    "line_markup",
    "line_plain",
    "lines_markup",
    "lines_plain",
    "segment_markup",
    "segment_style",
    "to_rich_text",
]
