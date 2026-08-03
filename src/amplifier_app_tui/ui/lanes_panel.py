"""Agent lanes overlay strip (DESIGN-SPEC §8, §2 overlay strips).

A bordered strip docked ABOVE the composer, toggled by ctrl-t / ``/tasks``:

- Header: ``Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close``
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

import unicodedata
from collections.abc import Mapping, Sequence

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Static

from ..model.lanes import LaneRecord, LaneState, lane_labels
from ..model.formatting import format_tokens_k
from .keymap import hint_label
from .live_tail import lane_tail_markup
from .motion import SHIMMER_INTERVAL_SECONDS, shimmer_band

LANES_HEADER_TITLE = "Agent lanes"
LANES_HEADER_HINT = "· ↑↓ select · enter focus · ctrl-o tail · esc close"
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


_MIN_ACTIVITY_WIDTH = 8
"""Floor for the elided activity column — below this, readability is gone
and the tokens column is dropped whole instead."""

_ELLIPSIS = "…"

EXPAND_HINT_TEXT = f"{hint_label('focus_lane')} to expand"
"""Trailing marker on a truncated preview (DESIGN-SPEC §8 / D5 AC3).

Sourced from the keymap table — not a hardcoded string — so the row's own
advertised affordance can never drift from the actual enter/click-to-focus
action (the same one :data:`LANES_HEADER_HINT` already names)."""


def _grapheme_safe_head(text: str, limit: int) -> str:
    """*text* cut to *limit* code points, nudged left off a trailing base
    character whose combining mark would otherwise be severed.

    Only reached when a single token has no whitespace boundary to break
    on at all — even that forced hard cut must never sever one
    user-perceived character (base + combining accent) in two.
    """
    cut = text[:limit]
    while cut and len(cut) < len(text) and unicodedata.combining(text[len(cut)]):
        cut = cut[:-1]
    return cut


def _elide(text: str, budget: int) -> str:
    """Boundary-safe truncation (D5 AC3): never clip mid-word.

    A long preview always ends with exactly one ellipsis, cut at the last
    whitespace boundary that still fits — ``"recovering from bash error"``
    narrows to ``"recovering from…"``, never ``"recovering from bash e…"``.
    Only a single token wider than the whole budget (no space to break on)
    falls back to a grapheme-safe hard cut.
    """
    if len(text) <= budget:
        return text
    limit = max(budget - len(_ELLIPSIS), 1)
    head = text[:limit]
    break_at = head.rfind(" ")
    if break_at > 0:
        return head[:break_at].rstrip() + _ELLIPSIS
    return _grapheme_safe_head(text, limit) + _ELLIPSIS


def format_lane_lines(
    lanes: Sequence[LaneState],
    tailed_index: int | None = None,
    *,
    labels: Sequence[str] | None = None,
    width: int | None = None,
    queued_counts: Sequence[int] | None = None,
) -> tuple[str, ...]:
    """Aligned lane lines per Claude Code's live agent panel:
    ``  <glyph> <name> · <activity> · <elapsed> · ↓ Nk tokens · $<cost>``.

    Name, activity, elapsed and token columns are padded to the widest
    entry so every ``·`` separator column lines up (mockup alignment).
    ``tailed_index`` appends the DESIGN-SPEC §8 ``▸`` tail marker to that
    lane's name (inside the padded name column, so alignment holds).

    ``queued_counts`` (aligned to *lanes*) appends a ``▸ N queued`` steer
    badge after the cost when a lane has messages queued for it (issue
    #39) — it rhymes with the tail-pin ``▸`` and sits last so it never
    disturbs the aligned columns.

    ``width`` is the row budget: rows are height-1 Statics, so overflow is
    CROPPED, and what fell off was the right-side telemetry — the panel's
    whole point. The elastic activity column is elided first; the tokens
    column is dropped whole next. Name, elapsed and cost always survive.

    A row whose activity was actually elided appends :data:`EXPAND_HINT_TEXT`
    (D5 AC3) — the ellipsis alone only implies more text exists; this names
    the actual action (enter/click a row, per the keymap) so it is explicit
    rather than implied. It sits before the queued badge, which keeps the
    ``▸`` badge last per the paragraph above.

    Booting lanes (spawned, no child event yet) end at the elapsed clock —
    ``  ◐ name · booting · Ns`` — because their zeroed tokens/cost cells
    would read as a hung agent during child bundle composition.
    """
    if not lanes:
        return ()
    # ``labels`` disambiguates same-named agent lanes (LaneRegistry order);
    # absent, the raw agent name is the label (unique-name fast path).
    display = list(labels) if labels is not None else [lane.name for lane in lanes]
    badges = [
        f"▸ {queued_counts[index]} queued"
        if queued_counts is not None and index < len(queued_counts) and queued_counts[index] > 0
        else ""
        for index in range(len(lanes))
    ]
    names = [
        f"{display[index]} ▸" if index == tailed_index else display[index]
        for index in range(len(lanes))
    ]
    activities = [lane.activity for lane in lanes]
    elapsed = [lane_elapsed(lane.elapsed) for lane in lanes]
    tokens = [f"↓ {format_tokens_k(lane.tokens)} tokens" for lane in lanes]
    costs = [f"${lane.cost:.2f}" for lane in lanes]
    name_w = max(len(name) for name in names)
    el_w = max(len(text) for text in elapsed)
    tok_w = max(len(text) for text in tokens)
    cost_w = max(len(text) for text in costs)

    def compose(
        acts: list[str],
        act_w: int,
        *,
        show_tokens: bool,
        truncated: Sequence[bool] | None = None,
    ) -> tuple[str, ...]:
        lines = []
        for i, lane in enumerate(lanes):
            line = (
                f"  {lane.glyph} {names[i]:<{name_w}} · {acts[i]:<{act_w}} · {elapsed[i]:<{el_w}}"
            )
            # A booting lane has produced no telemetry yet — rendering
            # ``↓ 0.0k tokens · $0.00`` reads as a hung agent, so the row
            # ends at the honest ``booting · Ns`` clock instead.
            if lane.state == "booting":
                line = line.rstrip()
            else:
                if show_tokens:
                    line += f" · {tokens[i]:<{tok_w}}"
                line += f" · {costs[i]}"
                # D5 AC3: a preview that was actually clipped names the
                # expand action explicitly — never just an implied ellipsis.
                # The core telemetry (through cost) always wins the width
                # budget; the hint only rides along when it still fits —
                # the always-visible header hint is the width-independent
                # fallback (never silently unreachable, just not repeated
                # on every narrow row).
                if truncated is not None and truncated[i]:
                    hinted = f"{line} · {EXPAND_HINT_TEXT}"
                    if width is None or len(hinted) <= width:
                        line = hinted
            if badges[i]:
                line += f" · {badges[i]}"
            lines.append(line)
        return tuple(lines)

    act_w = max(len(activity) for activity in activities)
    fixed = 4 + name_w + 3 + 3 + el_w + 3 + cost_w  # everything but activity/tokens
    if width is None or width - fixed - 3 - tok_w >= act_w:
        return compose(activities, act_w, show_tokens=True)
    budget = width - fixed - 3 - tok_w
    if budget >= _MIN_ACTIVITY_WIDTH:
        acts = [_elide(activity, budget) for activity in activities]
        truncated = [acts[i] != activities[i] for i in range(len(activities))]
        return compose(acts, max(len(a) for a in acts), show_tokens=True, truncated=truncated)
    budget = max(width - fixed, _MIN_ACTIVITY_WIDTH)
    acts = [_elide(activity, budget) for activity in activities]
    truncated = [acts[i] != activities[i] for i in range(len(activities))]
    return compose(acts, max(len(a) for a in acts), show_tokens=False, truncated=truncated)


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

    def __init__(self, record: LaneRecord, line: str, index: int, *, motion_frame: int = 0) -> None:
        super().__init__(id=f"lane-row-{index}")
        self.record = record
        self.line = line
        self.index = index
        self.motion_frame = motion_frame

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text(self.line, style=Style(color=tokens.get(self.record.lane.color_token)))
        lane = self.record.lane
        if lane.state != "done" and lane.name:
            name_start = self.line.find(lane.name)
            for offset, token, bold in shimmer_band(len(lane.name), self.motion_frame):
                start = name_start + offset
                text.stylize(Style(color=tokens.get(token), bold=bold), start, start + 1)
        return text

    def update_record(self, record: LaneRecord, line: str, *, motion_frame: int) -> None:
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


class _LaneTail(Static):
    """Dim ``┆``-guttered live tail of the focused lane, mounted directly
    under that lane's row so the stream sits with the agent it belongs to
    (issue #90) instead of a detached strip. Ephemeral — never a transcript
    block; the reducer owns accumulation + the ~0.05s throttle."""

    DEFAULT_CSS = """
    _LaneTail {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    def set_text(self, text: str) -> None:
        self.update(lane_tail_markup(text))


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
        self._tailed: str | None = None
        self._queued: dict[str, int] = {}
        self._motion_frame = 0
        self._motion_timer: Timer | None = None
        self._remount_pending = False
        self._tail_text = ""
        self._tail_widget: _LaneTail | None = None

    def on_unmount(self) -> None:
        self._stop_motion()

    # -- public API ----------------------------------------------------

    @property
    def records(self) -> tuple[LaneRecord, ...]:
        return self._records

    @property
    def lane_lines(self) -> tuple[str, ...]:
        """The exact aligned lane line strings currently displayed."""
        tailed_index = next(
            (
                index
                for index, record in enumerate(self._records)
                if record.session_id == self._tailed
            ),
            None,
        )
        width = self.container_size.width
        return format_lane_lines(
            tuple(record.lane for record in self._records),
            tailed_index,
            labels=lane_labels(self._records),
            # Pre-layout (width 0) → no budget; rows refit on_resize.
            width=width if width > 0 else None,
            queued_counts=[self._queued.get(record.session_id, 0) for record in self._records],
        )

    @property
    def selected_record(self) -> LaneRecord | None:
        if not self._records:
            return None
        return self._records[self._selected]

    def update_lanes(
        self,
        records: Sequence[LaneRecord],
        *,
        tailed_session_id: str | None = None,
        queued_counts: Mapping[str, int] | None = None,
    ) -> None:
        """Replace the lane listing (registration order, per LaneRegistry).

        ``queued_counts`` (``{session_id: depth}``) drives each lane row's
        ``▸ N queued`` steer badge (issue #39); omitted leaves it unchanged.
        """
        self._records = tuple(records)
        focus_changed = tailed_session_id != self._tailed
        self._tailed = tailed_session_id
        if focus_changed:
            # ctrl+o moved the ▸ focus — drop the old row's tail; the reducer
            # re-feeds show_lane_tail for the newly focused lane.
            self._drop_tail_widget()
        if queued_counts is not None:
            self._queued = dict(queued_counts)
        self._selected = min(self._selected, max(0, len(self._records) - 1))
        self._sync_motion()
        self._refresh_or_rebuild_rows()

    # -- focused-lane live tail (issue #90) ----------------------------------

    def _tailed_index(self) -> int | None:
        return next(
            (i for i, r in enumerate(self._records) if r.session_id == self._tailed),
            None,
        )

    def _tailed_row(self) -> _LaneRow | None:
        index = self._tailed_index()
        if index is None:
            return None
        found = self.query(f"#lane-row-{index}")
        return found.first(_LaneRow) if found else None

    def _drop_tail_widget(self) -> None:
        if self._tail_widget is not None:
            self._tail_widget.remove()
            self._tail_widget = None

    def sync_tailed(self, session_id: str | None) -> None:
        """Update which lane is considered tailed WITHOUT a full row rebuild.

        D5 AC5: ``show_lane_tail`` locates its row via ``self._tailed``,
        which is normally set by ``update_lanes(tailed_session_id=...)`` —
        but that now rides the coalesced ``lanes_changed()`` path (high-
        volume progress churn) and can lag behind. The tail-updated
        callback fires on its OWN, uncoalesced schedule (design doc D4's
        throttle is unrelated to AC5's), so it must not wait on a
        possibly-pending repaint just to learn which row is its own —
        this lets it resync that one field immediately; the eventual
        (possibly-coalesced) full repaint still lands and is a no-op here.
        """
        self._tailed = session_id

    def show_lane_tail(self, text: str) -> None:
        """Paint the focused lane's accumulated tail directly under its row."""
        self._tail_text = text
        row = self._tailed_row()
        if row is None:  # not mounted yet, or focused lane not listed
            return
        if self._tail_widget is None or not self._tail_widget.is_mounted:
            self._tail_widget = _LaneTail()
            self.mount(self._tail_widget, after=row)
        self._tail_widget.set_text(text)

    def clear_lane_tail(self) -> None:
        """Drop the lane tail (root preemption / lane done / turn end)."""
        self._tail_text = ""
        self._drop_tail_widget()

    @property
    def has_lane_tail(self) -> bool:
        """True while a focused-lane tail is mounted under its row."""
        return self._tail_widget is not None and self._tail_widget.is_mounted

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
            self.post_message(self.FocusLane(record.lane.name, session_id=record.session_id))

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

    def on_resize(self, event: events.Resize) -> None:
        # Rows carry a width budget (format_lane_lines) — refit on resize.
        if self._records:
            self._refresh_or_rebuild_rows()

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
            self._tail_widget = None  # remove_children dropped it
            lines = self.lane_lines
            rows: list[Static] = [_LanesHeader()]
            rows.extend(
                _LaneRow(record, lines[index], index, motion_frame=self._motion_frame)
                for index, record in enumerate(self._records)
            )
            await self.mount(*rows)
            self._apply_selection()
            if self._tail_text:  # re-place the focused lane's tail under its row
                self.show_lane_tail(self._tail_text)
        finally:
            self._remount_pending = False

    def _apply_selection(self) -> None:
        for row in self.query(_LaneRow):
            row.set_class(row.index == self._selected, "-selected")

    def _sync_motion(self) -> None:
        active = bool(self.display) and any(record.lane.state != "done" for record in self._records)
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
    "EXPAND_HINT_TEXT",
    "LANE_MOTION_INTERVAL_SECONDS",
    "LANES_HEADER",
    "LANES_HEADER_HINT",
    "LANES_HEADER_TITLE",
    "LanesPanel",
    "format_lane_lines",
    "lane_elapsed",
    "lane_labels",
]
