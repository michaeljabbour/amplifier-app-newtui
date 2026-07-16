"""Agent lanes overlay strip (DESIGN-SPEC §8, §2 overlay strips).

A bordered strip docked ABOVE the composer, toggled by ctrl-t / ``/tasks``:

- Header: ``Agent lanes · ↑↓ select · enter focus · esc close``
  (``Agent lanes`` bright bold, the hint dimmer).
- One aligned line per subagent:
  ``  <glyph> <name> · <activity> · <elapsed> · $<cost>`` — name /
  activity / elapsed columns padded to their widest entry so the ``·``
  separators line up exactly like the mockup. Line color comes from the
  lane state's theme token (``◐`` teal running, ``■`` fg working, ``✔``
  dim done).

``↑``/``↓`` move the selection (highlighted ``bg-tab``), Enter or a
click posts :class:`LanesPanel.FocusLane`; Esc posts
:class:`LanesPanel.Closed` and hides the panel. The panel never swaps
transcripts itself — focusing a lane is the app's job.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from ..model.lanes import LaneRecord, LaneState

LANES_HEADER_TITLE = "Agent lanes"
LANES_HEADER_HINT = "· ↑↓ select · enter focus · esc close"
LANES_HEADER = f"{LANES_HEADER_TITLE} {LANES_HEADER_HINT}"
"""Exact header line per DESIGN-SPEC §8."""


def lane_elapsed(seconds: float) -> str:
    """Compact lane elapsed format per the mockup: ``41s`` / ``2m``."""
    if seconds < 60:
        return f"{round(seconds)}s"
    return f"{round(seconds) // 60}m"


def format_lane_lines(lanes: Sequence[LaneState]) -> tuple[str, ...]:
    """Aligned lane lines: ``  <glyph> <name> · <activity> · <elapsed> · $<cost>``.

    Name, activity and elapsed columns are padded to the widest entry so
    every ``·`` separator column lines up (mockup alignment).
    """
    if not lanes:
        return ()
    elapsed = [lane_elapsed(lane.elapsed) for lane in lanes]
    name_w = max(len(lane.name) for lane in lanes)
    act_w = max(len(lane.activity) for lane in lanes)
    el_w = max(len(text) for text in elapsed)
    return tuple(
        f"  {lane.glyph} {lane.name:<{name_w}} · {lane.activity:<{act_w}}"
        f" · {elapsed[i]:<{el_w}} · ${lane.cost:.2f}"
        for i, lane in enumerate(lanes)
    )


class _LanesHeader(Static):
    """``Agent lanes`` bright bold + dimmer hint."""

    DEFAULT_CSS = """
    _LanesHeader {
        width: 100%;
        height: 1;
    }
    """

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text()
        text.append(LANES_HEADER_TITLE, style=Style(color=tokens.get("bright"), bold=True))
        text.append(" ")
        text.append(LANES_HEADER_HINT, style=Style(color=tokens.get("dimmer")))
        return text


class _LaneRow(Static):
    """One clickable aligned lane line, colored by lane state."""

    DEFAULT_CSS = """
    _LaneRow {
        width: 100%;
        height: 1;
    }
    _LaneRow.-selected {
        background: $bg-tab;
    }
    """

    def __init__(self, record: LaneRecord, line: str, index: int) -> None:
        super().__init__(id=f"lane-row-{index}")
        self.record = record
        self.line = line
        self.index = index

    def render(self) -> Text:
        tokens = self.app.theme_variables
        return Text(
            self.line, style=Style(color=tokens.get(self.record.lane.color_token))
        )

    def on_click(self) -> None:
        self.post_message(
            LanesPanel.FocusLane(self.record.lane.name, session_id=self.record.session_id)
        )


class LanesPanel(Vertical):
    """The agent-lanes overlay strip (DESIGN-SPEC §8).

    Feed it with :meth:`update_lanes` (LaneRegistry records) and toggle it
    with :meth:`show_panel` / :meth:`hide_panel`. Posts:

    - :class:`FocusLane` — Enter on the selection or click on a row.
    - :class:`Closed` — Esc (the panel also hides itself).
    """

    can_focus = True

    DEFAULT_CSS = """
    LanesPanel {
        display: none;
        width: 100%;
        height: auto;
        border-top: solid $rule;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", "↑↓ select", show=False),
        Binding("down", "cursor_down", "↑↓ select", show=False),
        Binding("enter", "focus_lane", "enter focus", show=False),
        Binding("escape", "close", "esc close", show=False),
    ]

    class FocusLane(Message):
        """The user focused a lane (Enter or click)."""

        def __init__(self, name: str, *, session_id: str = "") -> None:
            self.name = name
            self.session_id = session_id
            super().__init__()

    class Closed(Message):
        """Esc pressed while the lanes panel was open."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._records: tuple[LaneRecord, ...] = ()
        self._selected = 0

    # -- public API ----------------------------------------------------

    @property
    def records(self) -> tuple[LaneRecord, ...]:
        return self._records

    @property
    def lane_lines(self) -> tuple[str, ...]:
        """The exact aligned lane line strings currently displayed."""
        return format_lane_lines(tuple(r.lane for r in self._records))

    @property
    def selected_record(self) -> LaneRecord | None:
        if not self._records:
            return None
        return self._records[self._selected]

    def update_lanes(self, records: Sequence[LaneRecord]) -> None:
        """Replace the lane listing (registration order, per LaneRegistry)."""
        self._records = tuple(records)
        self._selected = min(self._selected, max(0, len(self._records) - 1))
        self._rebuild()

    def show_panel(self) -> None:
        self.display = True
        self.focus()

    def hide_panel(self) -> None:
        self.display = False

    def set_focused(self, name: str | None) -> None:
        """Snap the highlight to the currently focused lane (or leave as-is)."""
        if name is None:
            return
        for index, record in enumerate(self._records):
            if record.lane.name == name:
                self._selected = index
                self._apply_selection()
                return

    def move_selection(self, delta: int) -> None:
        if not self._records:
            return
        self._selected = max(0, min(len(self._records) - 1, self._selected + delta))
        self._apply_selection()

    def focus_selected(self) -> None:
        """Post :class:`FocusLane` for the highlighted lane."""
        record = self.selected_record
        if record is not None:
            self.post_message(
                self.FocusLane(record.lane.name, session_id=record.session_id)
            )

    # -- key actions ----------------------------------------------------

    def action_cursor_up(self) -> None:
        self.move_selection(-1)

    def action_cursor_down(self) -> None:
        self.move_selection(1)

    def action_focus_lane(self) -> None:
        self.focus_selected()

    def action_close(self) -> None:
        self.hide_panel()
        self.post_message(self.Closed())

    # -- internals -------------------------------------------------------

    def _rebuild(self) -> None:
        # remove_children is asynchronous: await it before remounting so
        # rebuilt rows never collide with the ids of outgoing ones.
        self.call_later(self._remount_rows)

    async def _remount_rows(self) -> None:
        await self.remove_children()
        lines = self.lane_lines
        rows: list[Static] = [_LanesHeader()]
        rows.extend(
            _LaneRow(record, lines[index], index)
            for index, record in enumerate(self._records)
        )
        await self.mount(*rows)
        self._apply_selection()

    def _apply_selection(self) -> None:
        for row in self.query(_LaneRow):
            row.set_class(row.index == self._selected, "-selected")


__all__ = [
    "LANES_HEADER",
    "LANES_HEADER_HINT",
    "LANES_HEADER_TITLE",
    "LanesPanel",
    "format_lane_lines",
    "lane_elapsed",
]
