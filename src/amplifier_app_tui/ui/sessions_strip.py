"""Sessions picker overlay strip (S2 compliance gap 2: a canonical
interactive selection surface for the session table).

A bordered strip docked ABOVE the composer -- never a ``ModalScreen``,
matching every other picker in this app (:class:`~.palette.PaletteStrip`,
:class:`~.rewind_strip.RewindStrip`) -- opened by ``/sessions``. Rows are
focusable/activatable with keyboard AND mouse parity:

- ``↑``/``↓`` move the highlighted row (clamped, no wrap-around).
- ``enter`` on the highlighted row -- or a CLICK on any row, highlighted
  or not (mirrors the palette's "click runs any row") -- activates it.
- ``r`` on the highlighted row -- or a CLICK on any row's trailing
  :data:`RESUME_GLYPH` -- requests its ready-to-run resume command (S2
  gap 2, see :class:`SessionsStrip.ResumeRequested` below).

Activating a session (Enter/click on the row body) posts
:class:`SessionsStrip.SessionActivated`; the app opens that session's full
detail (``session_ops_view.session_detail_spans``) rather than attempting
an in-place resume -- the stored-session roster has always been read-only
here: switching sessions is a fresh ``amplifier-tui resume SESSION_ID``,
never a live teardown of the running one. ``r``/the glyph click instead
post :class:`SessionsStrip.ResumeRequested`; the app answers with
``session_ops_view.session_resume_spans`` -- the exact command, copied to
the clipboard -- which reconciles "give me a keyboard path to resume" with
that same read-only contract by making the fastest reachable thing a
ready-to-paste command rather than a live teardown.

Rows render as a small table (Session id · name/bundle or state ·
msgs/turns/age · resume glyph), matching the CLI's ``_print_session_table``
column shape and the console style set by PRs #186/#188: dim secondary
columns, bright/teal identifiers, a bold state chip (orange for a session
that is still identifiable, red for one with no trustworthy identity at
all) for a damaged session instead of blank or misleading fields (S2 gap
3). The Turns figure (S2 gap 1) drops out of the meta cell below
:data:`NARROW_ROW_WIDTH` so a narrow terminal keeps the name/bundle/state
column readable rather than crushing it to an ellipsis.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ..kernel.session_manager import SessionSummary
from .session_ops_view import STATE_LABELS, STATE_STYLE_TOKENS

ID_COL_MIN_WIDTH = 10
"""Session-id column minimum width (short id is 8 chars + breathing room)."""

NARROW_ROW_WIDTH = 60
"""Below this rendered row width (cells), :func:`session_row_cells` drops
the Turns figure from the meta cell -- the same "drop the newest/least
critical decoration first" idea as the footer's own width ladder
(``ui/footer.py``'s ``_fit_drops``), just a single rung: Turns is the cell
S2 gap 1 added, so it is the one that yields first, protecting the
pre-existing name/bundle/state column from being crushed to an unreadable
ellipsis. A round number verified empirically against the golden 40-column
width rather than derived from exact Rich grid arithmetic (the flexible
detail column's own ellipsis overflow means there is no single "correct"
cutover to derive)."""

RESUME_GLYPH = "\u27f3"
"""Trailing per-row resume glyph (S2 gap 2) -- clicking it (see
:meth:`_SessionRow.on_click`) requests that row's resume command directly,
giving mouse users the same "any row" reach the keyboard's ``r`` chord and
the existing "click any row" activation already have, without a second
select-then-act step."""

RESUME_COL_WIDTH = 3
"""Rendered width of the trailing glyph column."""

RESUME_HIT_WIDTH = RESUME_COL_WIDTH + 1
"""Trailing cells (glyph column + the grid's own 1-cell gap) that count as
a resume click rather than a row-activate click."""


def session_row_cells(
    summary: SessionSummary, *, current: bool, width: int | None = None
) -> tuple[str, str, str]:
    """The three text cells of one row: (session id, name/state, meta).

    A damaged session (``state != "ok"``) shows its state instead of the
    name/bundle pair -- both would otherwise be blank or misleading (S2
    compliance: never render a corrupted/recovered row as if healthy).
    ``current`` marks the live session (its short id is a prefix of the
    adapter's own session id), matching the existing ``/sessions`` roster.

    ``meta`` carries Turns alongside the existing msgs/age pair (S2 gap 1:
    AC1 asks the row for name, session, bundle, msgs, turns AND age,
    matching the CLI table and the detail view -- this was the one surface
    still missing it). ``width`` is the row's current rendered width in
    cells; below :data:`NARROW_ROW_WIDTH` the Turns figure is dropped
    rather than crushing the flexible detail column into an unreadable
    ellipsis. ``None`` (the default -- used by callers with no live widget
    size, e.g. pure unit tests) always shows the full form.
    """
    del current  # kept for signature symmetry with the row's render(); marker is separate
    if summary.state != "ok":
        detail = f"\u26a0 {STATE_LABELS[summary.state]}"
    else:
        detail = f"{summary.name or '\u2014'}  \xb7  {summary.bundle}"
    turns_text = "\u2014" if summary.turns is None else str(summary.turns)
    if width is not None and width < NARROW_ROW_WIDTH:
        meta = f"{summary.messages} msgs  \xb7  {summary.time_ago}"
    else:
        meta = f"{summary.messages} msgs  \xb7  {turns_text} turns  \xb7  {summary.time_ago}"
    return (summary.short_id, detail, meta)


class _SessionRow(Static):
    """One clickable session row: marker + id + name/state + meta + resume glyph."""

    DEFAULT_CSS = """
    _SessionRow {
        width: 100%;
        height: 1;
        padding: 0 2;
    }
    _SessionRow.-selected {
        background: $bg-tab;
    }
    """

    def __init__(self, summary: SessionSummary, index: int, *, current: bool) -> None:
        super().__init__(id=f"sessions-row-{index}")
        self.summary = summary
        self.index = index
        self.current = current

    def render(self) -> Table:
        tokens = self.app.theme_variables
        selected = self.has_class("-selected")
        damaged = self.summary.state != "ok"
        width = self.size.width or None  # 0 before first layout -> "unknown", full form
        session_id, detail, meta = session_row_cells(
            self.summary, current=self.current, width=width
        )
        id_token = "green" if self.current else "teal"
        if damaged:
            detail_token = STATE_STYLE_TOKENS[self.summary.state]
        else:
            detail_token = "fg" if selected else "dim"
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(min_width=ID_COL_MIN_WIDTH, no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_column(width=RESUME_COL_WIDTH, justify="right", no_wrap=True)
        grid.add_row(
            Text("\u25b8" if self.current else " ", style=Style(color=tokens.get("green"))),
            Text(session_id, style=Style(color=tokens.get(id_token), bold=self.current)),
            Text(detail, style=Style(color=tokens.get(detail_token), bold=damaged)),
            Text(meta, style=Style(color=tokens.get("dimmer"))),
            Text(RESUME_GLYPH, style=Style(color=tokens.get("dim"))),
        )
        return grid

    def on_click(self, event: events.Click) -> None:
        """Click parity for both row actions (S2 gap 2): the trailing
        :data:`RESUME_GLYPH` zone requests resume for THIS row directly
        (mirrors "click any row" activation -- any row is reachable by
        mouse, not only the keyboard-highlighted one); anywhere else on
        the row still activates/opens detail, unchanged."""
        if self.size.width and event.x >= self.size.width - RESUME_HIT_WIDTH:
            self.post_message(SessionsStrip.ResumeRequested(self.summary.session_id))
            return
        self.post_message(SessionsStrip.SessionActivated(self.summary.session_id))


class SessionsStrip(VerticalScroll):
    """The sessions picker strip (S2 compliance).

    Open with :meth:`show_sessions`. Posts:

    - :class:`SessionActivated` -- Enter on the highlighted row, or a
      click on any row (click always activates immediately -- no separate
      select-then-activate step for the mouse, mirroring
      ``PaletteStrip``).
    - :class:`ResumeRequested` -- ``r`` on the highlighted row, or a click
      on any row's trailing :data:`RESUME_GLYPH` (S2 gap 2). Read-only
      stays read-only: the app answers with the exact ready-to-run
      ``amplifier-tui resume SESSION_ID`` command (copied to the
      clipboard), never an in-place teardown of the running session.
    - :class:`Closed` -- :meth:`close_strip` ran (Esc itself is resolved
      by the app via ``keymap.ESC_CHAIN``, never a local binding here --
      matches every other picker strip).
    """

    can_focus = True

    DEFAULT_CSS = """
    SessionsStrip {
        display: none;
        width: 100%;
        height: auto;
        max-height: 12;
        border-top: solid $rule;
        background: $bg-page;
        padding: 0;
        scrollbar-size-vertical: 1;
        /* All UI color comes from the §1 tokens -- never Textual-derived. */
        scrollbar-color: $rule;
        scrollbar-color-hover: $dim;
        scrollbar-color-active: $dim;
        scrollbar-background: $bg-page;
        scrollbar-background-hover: $bg-page;
        scrollbar-background-active: $bg-page;
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", "↑↓ select", show=False),
        Binding("down", "cursor_down", "↑↓ select", show=False),
        Binding("enter", "activate", "enter open", show=False),
        Binding("r", "resume_selected", "r resume", show=False),
        # No local escape binding: Esc must bubble to the app so it
        # resolves via keymap.ESC_CHAIN (matches PaletteStrip/RewindStrip).
    ]

    class SessionActivated(Message):
        """A session row was activated (Enter on selection, or click)."""

        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            super().__init__()

    class ResumeRequested(Message):
        """A session's resume command was requested (``r``, or a click on
        the row's :data:`RESUME_GLYPH`) -- S2 compliance gap 2."""

        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            super().__init__()

    class Closed(Message):
        """:meth:`close_strip` ran while the picker was open."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual widget API
        super().__init__(id=id)
        self._summaries: tuple[SessionSummary, ...] = ()
        self._current: str = ""
        self._selected = 0

    # -- public API ----------------------------------------------------

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    @property
    def summaries(self) -> tuple[SessionSummary, ...]:
        """Currently displayed sessions, in row order."""
        return self._summaries

    @property
    def selected_summary(self) -> SessionSummary | None:
        if not self._summaries:
            return None
        return self._summaries[self._selected]

    def show_sessions(self, summaries: Sequence[SessionSummary], *, current: str = "") -> None:
        """Open the picker on *summaries* (in the order supplied -- callers
        pass the newest-first roster from ``session_manager.list_summaries``).

        An empty sequence keeps the strip hidden -- the app shows a "no
        stored sessions" notice instead (mirrors ``RewindStrip.
        show_checkpoints`` on an empty checkpoint list).
        """
        self._summaries = tuple(summaries)
        self._current = current
        self._selected = 0
        if not self._summaries:
            self.display = False
            return
        self.display = True
        # remove_children is asynchronous: await it before remounting so
        # rebuilt rows never collide with the ids of outgoing ones
        # (mirrors PaletteStrip._rebuild).
        self.call_later(self._remount_rows)
        self.focus()

    def close_strip(self) -> None:
        self.display = False
        self.post_message(self.Closed())

    def move_selection(self, delta: int) -> None:
        """Move the highlighted row by *delta*, clamped to the list."""
        if not self._summaries:
            return
        self._selected = max(0, min(len(self._summaries) - 1, self._selected + delta))
        self._apply_selection()

    def activate_selected(self) -> None:
        """Post :class:`SessionActivated` for the highlighted row."""
        summary = self.selected_summary
        if summary is not None:
            self.post_message(self.SessionActivated(summary.session_id))

    def resume_selected(self) -> None:
        """Post :class:`ResumeRequested` for the highlighted row (S2 gap 2)."""
        summary = self.selected_summary
        if summary is not None:
            self.post_message(self.ResumeRequested(summary.session_id))

    # -- key actions ----------------------------------------------------

    def action_cursor_up(self) -> None:
        self.move_selection(-1)

    def action_cursor_down(self) -> None:
        self.move_selection(1)

    def action_activate(self) -> None:
        self.activate_selected()

    def action_resume_selected(self) -> None:
        self.resume_selected()

    # -- internals -------------------------------------------------------

    async def _remount_rows(self) -> None:
        await self.remove_children()
        if not self._summaries:
            return
        current = self._current
        rows = [
            _SessionRow(
                summary,
                index,
                current=bool(current) and summary.session_id.startswith(current),
            )
            for index, summary in enumerate(self._summaries)
        ]
        await self.mount(*rows)
        self._apply_selection()

    def _apply_selection(self) -> None:
        rows = list(self.query(_SessionRow))
        for row in rows:
            row.set_class(row.index == self._selected, "-selected")
        if 0 <= self._selected < len(rows):
            rows[self._selected].scroll_visible()


__all__ = [
    "ID_COL_MIN_WIDTH",
    "NARROW_ROW_WIDTH",
    "RESUME_COL_WIDTH",
    "RESUME_GLYPH",
    "RESUME_HIT_WIDTH",
    "SessionsStrip",
    "session_row_cells",
]
