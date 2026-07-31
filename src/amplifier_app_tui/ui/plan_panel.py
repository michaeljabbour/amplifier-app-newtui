"""Ambient plan strip (design 2026-07-21 D1/D2): the ``todo`` tool's live
checklist, rendered in the bottom strip's right column instead of the
transcript.

Header: ``Plan N/M`` (``Plan`` bright bold, counts dim). Rows: ``✔`` green
done (dim text), ``▶`` orange bold in-progress (bright bold text), ``○``
dimmer pending (dim text). Overflow: at most :data:`PLAN_MAX_ROWS` item
rows, windowed around the in-progress item, then one ``⋮ +N more`` dimmer
line. All complete: collapses to the header line alone (completion stays
visible — same "done stays visible" rule as the lanes panel). Formatting
is a pure function of the items (like ``ui/transcript.py`` renderers) so
tests pin plain strings via ``ui/segments.py:line_plain``.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..model.blocks import Segment, StyleToken, TodoItem, TodoStatus
from .segments import Line, line_plain

PLAN_MAX_ROWS = 5
"""Max item rows before collapsing the rest into ``⋮ +N more``."""

PLAN_DRILL_EXTRA: tuple[int, ...] = (0, 2, 3)
"""Drilldown ladder for the visible-plan window: default → +2 rows → +3
rows → back (ctrl+n while the panel is shown). The ``todo`` data model is
FLAT today (``TodoItem`` has content + status only, no children), so
"deeper" honestly means MORE rows of the same list, not nested sub-items."""


def plan_drill_notice(extra: int) -> str:
    """The notice shown when the drill level changes (both apps verbatim)."""
    return f"plan · +{extra} rows" if extra else "plan · default rows"


PLAN_PANEL_WIDTH = 37
"""Fixed column width of the panel in the bottom strip (design §1 mockup)."""

_GLYPHS: dict[TodoStatus, tuple[str, StyleToken, bool]] = {
    # status -> (prefix, content token, content bold)
    "completed": ("  ✔ ", "dim", False),
    "in_progress": ("  ▶ ", "bright", True),
    "pending": ("  ○ ", "dim", False),
}
_PREFIX_TOKENS: dict[TodoStatus, StyleToken] = {
    "completed": "green",
    "in_progress": "orange",
    "pending": "dimmer",
}


def plan_counts(items: Sequence[TodoItem]) -> tuple[int, int]:
    """``(done, total)`` for the header and the footer fallback."""
    return (sum(1 for item in items if item.status == "completed"), len(items))


def plan_panel_width(items: Sequence[TodoItem], strip_width: int) -> int:
    """Bottom-strip panel width: the mockup's 37 minimum, grown to the
    widest rendered row, capped at a third of the strip.

    Found live in a 198-col real fan-out: fixed 37 wraps real plan items
    while the lanes half sits mostly empty. The cap keeps lanes dominant;
    the floor keeps the demo/goldens geometry unchanged.
    """
    chrome = 4  # PlanPanel CSS `padding: 0 2` — content width is panel − 4
    needed = chrome + max(
        (cell_len(line_plain(line)) for line in format_plan_lines(items)),
        default=0,
    )
    return max(PLAN_PANEL_WIDTH, min(needed, strip_width // 3))


def format_plan_lines(
    items: Sequence[TodoItem], *, max_rows: int = PLAN_MAX_ROWS
) -> tuple[Line, ...]:
    """Render the plan as Segment lines — a pure function of the items."""
    if not items:
        return ()
    done, total = plan_counts(items)
    header: Line = (
        Segment(text="Plan", style_token="bright", bold=True),
        Segment(text=f" {done}/{total}", style_token="dim"),
    )
    if done == total:
        return (header,)  # collapse: completion stays visible as one line
    active = next((i for i, item in enumerate(items) if item.status == "in_progress"), 0)
    start = max(0, min(active - 1, total - max_rows))
    visible = items[start : start + max_rows]
    lines: list[Line] = [header]
    for item in visible:
        prefix, token, bold = _GLYPHS[item.status]
        lines.append(
            (
                Segment(text=prefix, style_token=_PREFIX_TOKENS[item.status]),
                Segment(text=item.content, style_token=token, bold=bold),
            )
        )
    hidden = total - len(visible)
    if hidden > 0:
        lines.append((Segment(text=f"  ⋮ +{hidden} more", style_token="dimmer"),))
    return tuple(lines)


class PlanPanel(Static):
    """The plan strip widget (``#plan-panel``) — bottom strip, right column.

    Feed it with :meth:`update_plan`; the app decides visibility via
    :meth:`show_panel` / :meth:`hide_panel` (responsive ladder lives in
    ``app_support.sync_plan_surfaces``, not here). Rendering is
    :func:`format_plan_lines` painted with theme tokens — no interaction,
    no focus, no timers.
    """

    DEFAULT_CSS = """
    PlanPanel {
        display: none;
        width: 100%;
        height: auto;
        border-top: solid $rule;
        padding: 0 2;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._items: tuple[TodoItem, ...] = ()
        self._drill = 0
        """Index into :data:`PLAN_DRILL_EXTRA` (ctrl+n cycles it)."""

    @property
    def items(self) -> tuple[TodoItem, ...]:
        return self._items

    @property
    def drill_extra(self) -> int:
        """Extra visible rows at the current drill level (0 at default)."""
        return PLAN_DRILL_EXTRA[self._drill]

    @property
    def max_rows(self) -> int:
        """The current visible-row cap: the mockup 5 plus the drill extra."""
        return PLAN_MAX_ROWS + self.drill_extra

    def cycle_drill(self) -> int:
        """Advance the drill ladder (default → +2 → +3 → back); returns the
        new extra-row count. The data is flat (see :data:`PLAN_DRILL_EXTRA`),
        so each step widens the window rather than descending a tree."""
        self._drill = (self._drill + 1) % len(PLAN_DRILL_EXTRA)
        if self.is_mounted:
            self.refresh(layout=True)
        return self.drill_extra

    @property
    def plan_lines(self) -> tuple[str, ...]:
        """The exact plain-text lines currently displayed (test surface)."""
        return tuple(
            line_plain(line) for line in format_plan_lines(self._items, max_rows=self.max_rows)
        )

    def update_plan(self, items: Sequence[TodoItem]) -> None:
        """Replace the listing (the ``todo`` tool replaces the whole list)."""
        self._items = tuple(items)
        if self.is_mounted:
            self.refresh(layout=True)

    def show_panel(self) -> None:
        self.display = True

    def hide_panel(self) -> None:
        self.display = False

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text()
        for index, line in enumerate(format_plan_lines(self._items, max_rows=self.max_rows)):
            if index:
                text.append("\n")
            for seg in line:
                text.append(
                    seg.text,
                    style=Style(color=tokens.get(seg.style_token), bold=seg.bold),
                )
        return text


__all__ = [
    "PLAN_DRILL_EXTRA",
    "PLAN_MAX_ROWS",
    "PLAN_PANEL_WIDTH",
    "PlanPanel",
    "format_plan_lines",
    "plan_counts",
    "plan_drill_notice",
    "plan_panel_width",
]
