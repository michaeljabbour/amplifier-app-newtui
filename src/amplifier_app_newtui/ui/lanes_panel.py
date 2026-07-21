"""Agent lanes overlay strip (DESIGN-SPEC §8, §2 overlay strips).

A bordered strip docked ABOVE the composer, toggled by ctrl-t / ``/tasks``:

- Header: ``Agent lanes · ↑↓ select · enter focus · esc close``
  (``Agent lanes`` bright bold, the hint dimmer).
- One aligned line per subagent (Claude Code's live agent panel):
  ``  <glyph> <name> · <activity> · <elapsed> · ↓ Nk tokens · $<cost>`` —
  name / activity / elapsed / token columns padded to their widest entry
  so the ``·`` separators line up exactly like the mockup. Line color
  comes from the lane state's theme token (``◐`` teal running, ``■`` fg
  working, ``✔`` dim done).

``↑``/``↓`` move the selection (highlighted ``bg-tab``), Enter or a
click posts :class:`LanesPanel.FocusLane`; Esc posts
:class:`LanesPanel.Closed` and hides the panel. The panel never swaps
transcripts itself — focusing a lane is the app's job.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Static

from ..model.lanes import LaneRecord, LaneState
from ..model.turn import _format_tokens
from .motion import SHIMMER_INTERVAL_SECONDS, shimmer_band

LANES_HEADER_TITLE = "Agent lanes"
LANES_HEADER_HINT = "· ↑↓ select · enter focus · esc close"
LANES_HEADER = f"{LANES_HEADER_TITLE} {LANES_HEADER_HINT}"
"""Exact header line per DESIGN-SPEC §8."""

LANE_MOTION_INTERVAL_SECONDS = SHIMMER_INTERVAL_SECONDS
"""Active-only soft-band cadence for agent names."""


def lane_elapsed(seconds: float) -> str:
    """Claude-Code lane elapsed precision: ``41s`` / ``5m 48s``.

    Under a minute renders whole seconds (``41s``); at or above, minutes
    plus zero-padded seconds (``348`` → ``5m 48s``, ``124`` → ``2m 04s``)
    so the live per-agent clock reads like Claude Code's agent panel.
    """
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60:02d}s"


def format_lane_lines(lanes: Sequence[LaneState]) -> tuple[str, ...]:
    """Aligned lane lines per Claude Code's live agent panel:
    ``  <glyph> <name> · <activity> · <elapsed> · ↓ Nk tokens · $<cost>``.

    Name, activity, elapsed and token columns are padded to the widest
    entry so every ``·`` separator column lines up (mockup alignment).
    """
    if not lanes:
        return ()
    elapsed = [lane_elapsed(lane.elapsed) for lane in lanes]
    tokens = [f"↓ {_format_tokens(lane.tokens)} tokens" for lane in lanes]
    name_w = max(len(lane.name) for lane in lanes)
    act_w = max(len(lane.activity) for lane in lanes)
    el_w = max(len(text) for text in elapsed)
    tok_w = max(len(text) for text in tokens)
    return tuple(
        f"  {lane.glyph} {lane.name:<{name_w}} · {lane.activity:<{act_w}}"
        f" · {elapsed[i]:<{el_w}} · {tokens[i]:<{tok_w}} · ${lane.cost:.2f}"
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

    def __init__(
        self, record: LaneRecord, line: str, index: int, *, motion_frame: int = 0
    ) -> None:
        super().__init__(id=f"lane-row-{index}")
        self.record = record
        self.line = line
        self.index = index
        self.motion_frame = motion_frame

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text(
            self.line, style=Style(color=tokens.get(self.record.lane.color_token))
        )
        lane = self.record.lane
        if lane.state != "done" and lane.name:
            name_start = self.line.find(lane.name)
            for offset, token, bold in shimmer_band(
                len(lane.name), self.motion_frame
            ):
                start = name_start + offset
                text.stylize(
                    Style(color=tokens.get(token), bold=bold), start, start + 1
                )
        return text

    def update_record(
        self, record: LaneRecord, line: str, *, motion_frame: int
    ) -> None:
        """Refresh telemetry in place so motion is not reset by row remounts."""

        self.record = record
        self.line = line
        self.motion_frame = motion_frame
        self.refresh(layout=False)

    def set_motion_frame(self, frame: int) -> None:
        self.motion_frame = frame
        self.refresh(layout=False)

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
        # No local escape binding: Esc must bubble to the app so it resolves
        # via keymap.ESC_CHAIN (spec §5 — palette/rewind close before lanes
        # even while this panel holds keyboard focus). The chain calls
        # ``action_close`` when the lanes step is reached.
    ]

    class FocusLane(Message):
        """The user focused a lane (Enter or click)."""

        def __init__(self, name: str, *, session_id: str = "") -> None:
            self.name = name
            self.session_id = session_id
            super().__init__()

    class Closed(Message):
        """Esc pressed while the lanes panel was open."""

    class TypeThrough(Message):
        """A printable key pressed while the panel held focus.

        Mockup ground truth (document-level keydown, composer input keeps
        focus while ``lanesOpen``): typing is never swallowed by the lanes
        panel — the app forwards the character to the composer, so ``/``
        opens the palette and mid-turn steering text lands in the input.
        """

        def __init__(self, character: str) -> None:
            self.character = character
            super().__init__()

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._records: tuple[LaneRecord, ...] = ()
        self._selected = 0
        self._motion_frame = 0
        self._motion_timer: Timer | None = None
        self._remount_pending = False

    def on_unmount(self) -> None:
        self._stop_motion()

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
        self._sync_motion()
        self._refresh_or_rebuild_rows()

    def show_panel(self, *, focus: bool = True) -> None:
        self.display = True
        self._sync_motion()
        if focus:
            self.focus()

    def hide_panel(self) -> None:
        self.display = False
        self._stop_motion()

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

    def on_key(self, event: events.Key) -> None:
        """Printable keys pass through to the composer (mockup: the
        composer keeps typing rights while ``lanesOpen``); ↑↓/enter stay
        with the panel via BINDINGS, esc bubbles to the app's ESC_CHAIN."""
        if event.is_printable and event.character:
            event.stop()
            event.prevent_default()
            self.post_message(self.TypeThrough(event.character))

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

    def _refresh_or_rebuild_rows(self) -> None:
        """Patch stable rows when shape is unchanged; remount only on fan-out."""

        rows = list(self.query(_LaneRow)) if self.is_mounted else []
        has_header = bool(list(self.query(_LanesHeader))) if self.is_mounted else False
        if has_header and len(rows) == len(self._records):
            lines = self.lane_lines
            for index, row in enumerate(rows):
                row.update_record(
                    self._records[index],
                    lines[index],
                    motion_frame=self._motion_frame,
                )
            self._apply_selection()
            return
        self._rebuild()

    def _rebuild(self) -> None:
        # remove_children is asynchronous: await it before remounting so
        # rebuilt rows never collide with the ids of outgoing ones.
        if self._remount_pending:
            return
        self._remount_pending = True
        self.call_later(self._remount_rows)

    async def _remount_rows(self) -> None:
        try:
            await self.remove_children()
            lines = self.lane_lines
            rows: list[Static] = [_LanesHeader()]
            rows.extend(
                _LaneRow(
                    record, lines[index], index, motion_frame=self._motion_frame
                )
                for index, record in enumerate(self._records)
            )
            await self.mount(*rows)
            self._apply_selection()
        finally:
            self._remount_pending = False

    def _apply_selection(self) -> None:
        for row in self.query(_LaneRow):
            row.set_class(row.index == self._selected, "-selected")

    def _sync_motion(self) -> None:
        active = bool(self.display) and any(
            record.lane.state != "done" for record in self._records
        )
        if active and self.is_mounted and self._motion_timer is None:
            self._motion_timer = self.set_interval(
                LANE_MOTION_INTERVAL_SECONDS, self._advance_motion
            )
        elif not active:
            self._stop_motion()

    def _stop_motion(self) -> None:
        if self._motion_timer is not None:
            self._motion_timer.stop()
            self._motion_timer = None

    def _advance_motion(self) -> None:
        self._motion_frame += 1
        for row in self.query(_LaneRow):
            row.set_motion_frame(self._motion_frame)


__all__ = [
    "LANE_MOTION_INTERVAL_SECONDS",
    "LANES_HEADER",
    "LANES_HEADER_HINT",
    "LANES_HEADER_TITLE",
    "LanesPanel",
    "format_lane_lines",
    "lane_elapsed",
]
