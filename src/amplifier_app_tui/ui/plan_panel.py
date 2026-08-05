"""Ambient plan strip (design 2026-07-21 D1/D2): the ``todo`` tool's live
checklist, rendered in the bottom strip's right column instead of the
transcript.

Header: ``Plan N/M`` (``Plan`` bright bold, counts dim). Rows: ``✔`` green
done (dim text), ``▶`` orange bold in-progress (bright bold text), ``○``
dimmer pending (dim text). Overflow: at most :data:`PLAN_MAX_ROWS` item
rows, windowed around the in-progress item, then one ``⋮ +N more``
control. All complete: collapses to the header line alone (completion stays
visible — same "done stays visible" rule as the lanes panel). Formatting
is a pure function of the items (like ``ui/transcript.py`` renderers) so
tests pin plain strings via ``ui/segments.py:line_plain``.

S7 compliance (2026-08-02): the ``⋮ +N more`` line used to be a plain
:class:`~textual.widgets.Static` segment — no focus, no click, the row
window (ctrl+n, ``plan_drilldown`` in ``ui/keymap.py``) was the only way to
see more. It is now its own small focusable/clickable control
(:class:`_PlanOverflowControl`) that expands the FULL list in place and
flips to a ``▾ Show less`` control to reverse it — Enter, Space, and click
all activate it (keyboard/mouse parity). ``expanded`` is view state owned
by :class:`PlanPanel`, kept independent of both the ``todo`` model (nothing
in ``model/blocks.py`` changed) and the existing ctrl+n drill level
(``_drill``): the two disclosure mechanisms compose by ``expanded``
overriding the row window while it is active — see
:func:`format_plan_body_and_control` — so collapsing ("Show less") always
lands back on whatever drill window was current, never resets it. At
short viewports the panel is a bounded, independently-scrolling region
(``VerticalScroll``) sized by :func:`plan_panel_max_height`, so an expanded
long plan can never grow the bottom strip enough to push the composer
off-screen (``ui/app_support.py:sync_plan_surfaces``).

S7 follow-up (2026-08-04, two outstanding gaps closed): (1) the control was
keyboard-*activatable* once focused, but nothing gave a keyboard-only user
a way to *reach* it -- Tab is not a general focus chain in this app (it is
claimed by mention-accept/approval nav, and shift+tab is ``cycle_mode``).
The dedicated global chord, ``toggle_plan_overflow`` (ctrl+h,
``ui/keymap.py``), now places focus on the SAME control before toggling it.
That makes the selected ``Show less`` action explicit after expansion;
Enter/Space can reverse it and Esc returns to the composer. See
:func:`plan_overflow_notice` and ``TuiApp.action_toggle_plan_overflow``.
(2) At narrow widths/short heights the control stays present, focusable,
and click/keyboard-activatable throughout (pinned at 40/80/97/120 cols and
a short height in ``test_ui_plan_panel_expand.py``): the collapsed default
view is always header + at most :data:`PLAN_MAX_ROWS` rows + the control,
comfortably inside even :data:`PLAN_PANEL_HEIGHT_FLOOR`, so the control a
keyboard-only user needs first is never itself scrolled out of reach.
ctrl+h is doubly robust here: unlike a click, it acts on the panel
directly rather than screen coordinates, so it keeps working even if an
already-expanded list has scrolled the control off-screen.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ..model.blocks import GLYPH_CHEVRON_EXPANDED, Segment, StyleToken, TodoItem, TodoStatus
from .segments import Line, line_plain

PLAN_MAX_ROWS = 5
"""Max item rows before collapsing the rest into ``⋮ +N more`` (collapsed
default view — see :data:`PLAN_DRILL_EXTRA` and the ``expanded`` override)."""

PLAN_DRILL_EXTRA: tuple[int, ...] = (0, 2, 3)
"""Drilldown ladder for the visible-plan window: default → +2 rows → +3
rows → back (ctrl+n while the panel is shown). The ``todo`` data model is
FLAT today (``TodoItem`` has content + status only, no children), so
"deeper" honestly means MORE rows of the same list, not nested sub-items.
Independent of :attr:`PlanPanel.expanded` (S7): the drill level keeps
advancing while expanded, it just has no visible effect until the user
collapses back (see :func:`format_plan_body_and_control`)."""


def plan_drill_notice(extra: int) -> str:
    """The notice shown when the drill level changes (both apps verbatim)."""
    return f"plan · +{extra} rows" if extra else "plan · default rows"


def plan_overflow_notice(expanded: bool) -> str:
    """The notice shown when the ``toggle_plan_overflow`` chord (ctrl+h,
    S7 gap 1) fires -- mirrors :func:`plan_drill_notice`'s shape for the
    sibling ctrl+n chord, so both plan-panel keyboard actions confirm
    themselves the same way."""
    return "plan · expanded" if expanded else "plan · collapsed"


PLAN_PANEL_WIDTH = 37
"""Fixed column width of the panel in the bottom strip (design §1 mockup)."""

PLAN_PANEL_HEIGHT_FLOOR = 8
"""The panel's bounded max-height (S7 AC5) never drops below this — header
+ a handful of rows still fit even on a very short terminal."""

PLAN_COLLAPSE_LABEL = "Show less"
"""The control's label once expanded (S7 AC2) — a clear, reversible action,
paired with the same chevron the transcript's thinking/delegate-summary
blocks already use for "this is expanded, click to collapse"."""

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


def plan_panel_max_height(screen_height: int) -> int:
    """Bound the (possibly expanded) panel's height (S7 AC5).

    However many items a plan carries, the panel itself must never grow
    tall enough to push the composer/footer off-screen: capped at half the
    terminal's rows, floored at :data:`PLAN_PANEL_HEIGHT_FLOOR` so a
    handful of rows always fit even at a 24-line terminal. Beyond this the
    panel (a ``VerticalScroll``) scrolls its own content instead of
    growing — recomputed on every resize (``sync_plan_surfaces``), like
    :func:`plan_panel_width`.
    """
    return max(PLAN_PANEL_HEIGHT_FLOOR, screen_height // 2)


def format_plan_body_and_control(
    items: Sequence[TodoItem], *, max_rows: int = PLAN_MAX_ROWS, expanded: bool = False
) -> tuple[tuple[Line, ...], Line | None]:
    """Split the panel's content into ``(header + rows, control-or-None)``.

    The pure core the panel paints into two widgets (S7): a non-interactive
    body (:class:`_PlanBody`) and the one focusable/clickable overflow
    line (:class:`_PlanOverflowControl`), which needs its own text +
    style — not just a suffix of one big block of text.

    ``expanded`` overrides the row window rather than replacing the drill
    ladder: when True every item renders regardless of ``max_rows``, but
    ``max_rows`` (the ctrl+n-controlled window) still decides whether
    there is anything to fall back to, and what it looks like when there
    is (that's what the ``hidden`` count below the "Show less" control
    would collapse back to). No items, or all complete, ⇒ no control.
    """
    if not items:
        return (), None
    done, total = plan_counts(items)
    header: Line = (
        Segment(text="Plan", style_token="bright", bold=True),
        Segment(text=f" {done}/{total}", style_token="dim"),
    )
    if done == total:
        return (header,), None  # collapse: completion stays visible as one line
    active = next((i for i, item in enumerate(items) if item.status == "in_progress"), 0)
    hidden = max(0, total - max_rows)
    if expanded:
        visible = items
    else:
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
    if hidden <= 0:
        return tuple(lines), None
    control: Line = (
        (Segment(text=f"  {GLYPH_CHEVRON_EXPANDED} {PLAN_COLLAPSE_LABEL}", style_token="dimmer"),)
        if expanded
        else (Segment(text=f"  ⋮ +{hidden} more", style_token="dimmer"),)
    )
    return tuple(lines), control


def format_plan_lines(
    items: Sequence[TodoItem], *, max_rows: int = PLAN_MAX_ROWS, expanded: bool = False
) -> tuple[Line, ...]:
    """Render the plan as Segment lines — a pure function of the items
    (the plain-text test/measurement surface; :class:`PlanPanel` paints
    from :func:`format_plan_body_and_control` directly instead, so the
    trailing control line can be its own focusable widget)."""
    body, control = format_plan_body_and_control(items, max_rows=max_rows, expanded=expanded)
    return body + ((control,) if control is not None else ())


class PlanOverflowToggled(Message):
    """Enter, Space, or a click on the overflow/collapse control (S7).

    Bubbles from :class:`_PlanOverflowControl` to its owning
    :class:`PlanPanel`, which stops it there — nothing outside
    ``plan_panel.py`` needs to react.
    """


class _PlanBody(Static):
    """Non-interactive header + visible item rows.

    Split out of the old single-``Static`` ``PlanPanel`` (S7) so the
    trailing overflow/collapse line can be its own focusable widget
    without disturbing this half's plain, no-interaction rendering.
    """

    DEFAULT_CSS = """
    _PlanBody { width: 100%; height: auto; }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._lines: tuple[Line, ...] = ()

    def set_lines(self, lines: tuple[Line, ...]) -> None:
        if lines == self._lines:
            return
        self._lines = lines
        self.refresh(layout=True)

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text()
        for index, line in enumerate(self._lines):
            if index:
                text.append("\n")
            for seg in line:
                text.append(
                    seg.text,
                    style=Style(color=tokens.get(seg.style_token), bold=seg.bold),
                )
        return text


class _PlanOverflowControl(Static):
    """The plan panel's ``+N more`` / ``Show less`` control (S7).

    The one interactive surface this module exposes: focusable, and
    Enter/Space/click all activate it (keyboard + mouse parity). Dim
    styling matches the passive line it replaces; a focus highlight
    (``:focus``) is the only visual addition, mirroring the ``-selected``
    background used for list-style rows elsewhere in the UI
    (``ui/lanes_panel.py``, ``ui/palette.py``).
    """

    can_focus = True

    DEFAULT_CSS = """
    _PlanOverflowControl {
        width: 100%;
        height: 1;
    }
    _PlanOverflowControl:focus {
        background: $bg-tab;
    }
    """

    BINDINGS = [
        Binding("enter", "activate", "toggle", show=False),
        Binding("space", "activate", "toggle", show=False),
    ]

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._line: Line = ()
        # Bonus discoverability channel (S7 gap 1: "the panel itself"): a
        # mouse-hover hint naming the ctrl+h reach path, mirroring
        # ui/transcript.py's turn_rule tooltip idiom. Static text (not
        # derived from set_line()) since the rendered label itself is
        # pinned verbatim by tests/goldens and must not gain a suffix.
        self.tooltip = "click, enter/space when focused, or ctrl-h · toggle hidden plan rows"

    def set_line(self, line: Line) -> None:
        if line == self._line:
            return
        self._line = line
        self.refresh()

    @property
    def label_text(self) -> str:
        """The exact plain-text label currently shown (test surface)."""
        return line_plain(self._line)

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text()
        for seg in self._line:
            text.append(seg.text, style=Style(color=tokens.get(seg.style_token), bold=seg.bold))
        return text

    def on_click(self) -> None:
        self.post_message(PlanOverflowToggled())

    def action_activate(self) -> None:
        self.post_message(PlanOverflowToggled())

    def on_key(self, event: events.Key) -> None:
        """Esc leaves the plan action and returns to normal composition.

        Ctrl-H is the keyboard reach path and intentionally leaves this
        control selected so Enter/Space can reverse the disclosure.  A
        concrete escape route keeps that focus handoff from trapping typing.
        """
        if event.key != "escape":
            return
        event.stop()
        composer = getattr(self.app, "composer", None)
        focus_input = getattr(composer, "focus_input", None)
        if callable(focus_input):
            focus_input()


class PlanPanel(VerticalScroll):
    """The plan strip widget (``#plan-panel``) — bottom strip, right column.

    Feed it with :meth:`update_plan`; the app decides visibility via
    :meth:`show_panel` / :meth:`hide_panel` (responsive ladder lives in
    ``app_support.sync_plan_surfaces``, not here). Rendering is
    :func:`format_plan_body_and_control` painted into two children with
    theme tokens: a plain body and the one focusable overflow/collapse
    control (S7). At short viewports the panel bounds its own height and
    scrolls internally instead of growing (:func:`plan_panel_max_height`).
    """

    can_focus = True
    """A fallback focus target: if the overflow control has focus and a
    plan update makes it disappear (nothing left to disclose), focus
    lands here instead of vanishing into an unreachable hidden widget."""

    DEFAULT_CSS = """
    PlanPanel {
        display: none;
        width: 100%;
        height: auto;
        max-height: 20;
        border-top: solid $rule;
        padding: 0 2;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._items: tuple[TodoItem, ...] = ()
        self._drill = 0
        """Index into :data:`PLAN_DRILL_EXTRA` (ctrl+n cycles it)."""
        self._expanded = False
        """S7 view state: expand-all, independent of ``_drill`` and the
        ``todo`` model (see the module docstring for the composition rule
        with ctrl+n)."""
        self._body = _PlanBody(id="plan-body")
        self._overflow = _PlanOverflowControl(id="plan-overflow")

    def compose(self) -> ComposeResult:
        yield self._body
        yield self._overflow

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

    @property
    def expanded(self) -> bool:
        """Whether the hidden rows are currently expanded (S7)."""
        return self._expanded

    def cycle_drill(self) -> int:
        """Advance the drill ladder (default → +2 → +3 → back); returns the
        new extra-row count. The data is flat (see :data:`PLAN_DRILL_EXTRA`),
        so each step widens the window rather than descending a tree.
        Independent of :attr:`expanded` — see the module docstring."""
        self._drill = (self._drill + 1) % len(PLAN_DRILL_EXTRA)
        self._repaint()
        return self.drill_extra

    def expand(self) -> None:
        """Reveal every item (S7 AC1). No-op if already expanded."""
        self._set_expanded(True)

    def collapse(self) -> None:
        """Return to the ctrl+n-controlled row window (S7 AC2). No-op if
        already collapsed."""
        self._set_expanded(False)

    def toggle_expand(self) -> bool:
        """Flip :attr:`expanded`; returns the resulting value."""
        self._set_expanded(not self._expanded)
        return self._expanded

    def _set_expanded(self, value: bool) -> None:
        if value == self._expanded:
            return
        self._expanded = value
        self._repaint()

    @property
    def plan_lines(self) -> tuple[str, ...]:
        """The exact plain-text lines currently displayed (test surface)."""
        return tuple(
            line_plain(line)
            for line in format_plan_lines(
                self._items, max_rows=self.max_rows, expanded=self._expanded
            )
        )

    @property
    def overflow_label(self) -> str:
        """The overflow/collapse control's current plain-text label —
        ``""`` when nothing is hidden and the panel isn't expanded (S7
        AC1/AC2 test surface, narrower than :attr:`plan_lines`)."""
        return self._overflow.label_text

    @property
    def overflow_control(self) -> _PlanOverflowControl:
        """The focusable/clickable control widget itself (S7 AC1 test
        surface — asserting ``can_focus`` / focus state / simulating
        clicks)."""
        return self._overflow

    def update_plan(self, items: Sequence[TodoItem]) -> None:
        """Replace the listing (the ``todo`` tool replaces the whole list).

        ``expanded`` (like the ctrl+n drill level already did) is untouched
        here — it is view state independent of the model (S7 design note),
        so a plan update never silently collapses an expanded panel. The
        hidden count and control label are always recomputed fresh from
        *items*, so they stay accurate across updates, completion changes,
        and any future filtering of the list (S7 AC3).
        """
        self._items = tuple(items)
        self._repaint()

    def show_panel(self) -> None:
        self.display = True

    def hide_panel(self) -> None:
        self.display = False

    def on_mount(self) -> None:
        self._repaint()

    def _repaint(self) -> None:
        if not self.is_mounted:
            return
        body_lines, control = format_plan_body_and_control(
            self._items, max_rows=self.max_rows, expanded=self._expanded
        )
        self._body.set_lines(body_lines)
        if control is None:
            if self._overflow.has_focus:
                # AC4: don't strand focus on a control that just vanished
                # (e.g. a plan update shrank the list to fit) — the panel
                # itself is always a valid, harmless fallback target.
                self.focus()
            self._overflow.display = False
            self._overflow.set_line(())
        else:
            self._overflow.set_line(control)
            self._overflow.display = True

    def on_plan_overflow_toggled(self, message: PlanOverflowToggled) -> None:
        message.stop()
        self.toggle_expand()


__all__ = [
    "PLAN_COLLAPSE_LABEL",
    "PLAN_DRILL_EXTRA",
    "PLAN_MAX_ROWS",
    "PLAN_PANEL_HEIGHT_FLOOR",
    "PLAN_PANEL_WIDTH",
    "PlanOverflowToggled",
    "PlanPanel",
    "format_plan_body_and_control",
    "format_plan_lines",
    "plan_counts",
    "plan_drill_notice",
    "plan_overflow_notice",
    "plan_panel_max_height",
    "plan_panel_width",
]
