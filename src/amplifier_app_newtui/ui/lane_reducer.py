"""Agent-lane presentation state: live tail + focused-lane transcripts.

Extracted from :class:`~amplifier_app_newtui.ui.reducer.TranscriptReducer`
along the lane seam added in PRs #13/#17. This unit owns the lane-scoped
state that the turn reducer used to carry inline:

- the per-lane live-tail buffer (DESIGN-SPEC §8, design doc D4) with its
  accumulate-then-notify throttle and root-stream preemption, and
- the real-runtime focused-lane transcripts (DESIGN-SPEC §8) that child
  events (diverted from the root transcript by the foreign-turn rule)
  accumulate into so lane focus can replay a subagent's own work.

The turn reducer still projects diverted child events onto lanes and
decides *when* lane activity changes; this unit owns *what* the lane
remembers and speaks to the app through the same narrow lane callbacks
(``lane_tail_updated`` / ``lane_tail_cleared``). Keeping the state here
makes lane behavior unit-testable with a fake host in isolation.
"""

from __future__ import annotations

import re
import time
from typing import Any, Protocol

from ..kernel import events as ev
from ..model.blocks import (
    BlockIdAllocator,
    SessionBanner,
    TranscriptBlock,
    UserLine,
)
from ..model.lanes import LaneRecord, LaneRegistry
from .needs_you import focused_lane_banner

LANE_TAIL_NOTIFY_SECONDS = 0.05
"""Lane-tail repaint floor — mirrors ``_DELTA_NOTIFY_SECONDS`` in
``kernel/trackers/stream_status.py``. The per-lane buffer accumulates
between paints, so throttling drops paints — never text."""

_LANE_TAIL_MAX_CHARS = 2_000
"""Per-lane tail buffer cap; the widget paints only the last 3 lines."""

_LANE_TRANSCRIPT_MAX_BLOCKS = 400
"""Per-lane focus-transcript cap; oldest activity rows drop first."""

_LANE_TRANSCRIPT_MAX_LANES = 32
"""Stored focus transcripts; the oldest lane's is evicted past this."""

_LANE_SEED_ROWS = 2
"""Rows the per-lane cap never trims (banner + delegated brief)."""


def _display_short(session_id: str) -> str:
    """First 6 usable chars of a session id for the focused-lane banner.

    Governance redaction can rewrite ids on the live bus
    (``[REDACTED:PII]…`` — found live); bracketed tokens are stripped so
    a mangled id neither leaks into the banner nor reads as markup.
    """
    cleaned = re.sub(r"\[[^\]]*\]", "", session_id)
    cleaned = "".join(ch for ch in cleaned if ch.isalnum() or ch == "-")
    return cleaned[:6]


class LaneTailHost(Protocol):
    """The narrow lane-tail surface the LaneReducer drives.

    A structural subset of :class:`~amplifier_app_newtui.ui.reducer.ReducerHost`
    — the two lane callbacks are all this unit touches, so it never has to
    know about the rest of the host (and there is no import cycle with the
    turn reducer that owns the full protocol).
    """

    def lane_tail_updated(self, text: str) -> None: ...
    def lane_tail_cleared(self) -> None: ...


class LaneReducer:
    """Lane presentation state: focus transcripts + the live tail.

    Driven by :class:`~amplifier_app_newtui.ui.reducer.TranscriptReducer`,
    which routes child events onto lanes and calls the methods here to
    accumulate a lane's focus transcript and paint the focused lane's tail.
    """

    def __init__(
        self,
        host: LaneTailHost,
        *,
        allocator: BlockIdAllocator,
        lanes: LaneRegistry,
        tail_clock: Any = None,
    ) -> None:
        self._host = host
        self._ids = allocator
        self.lanes = lanes
        # -- lane live tail (DESIGN-SPEC §8, design doc D4) ------------------
        self._tail_clock = tail_clock or time.monotonic
        self._lane_tails: dict[str, str] = {}
        self._lane_tail_last = 0.0
        self._lane_tail_shown: str | None = None
        self.root_streaming = False
        """The root session is streaming right now — it always preempts the
        lane tail (D4). Set by the turn reducer at each root stream
        transition; read only by the tail paths here."""
        # -- focused-lane transcripts (DESIGN-SPEC §8) -----------------------
        # Real sessions have no scripted lane logs (that is the demo
        # adapter's ``lane_blocks``); the child events already diverted
        # from the root transcript accumulate here instead, keyed by
        # canonical lane session id, so lane focus can replay a
        # subagent's own work.
        self._lane_transcripts: dict[str, list[TranscriptBlock]] = {}
        self._pending_briefs: dict[str, str] = {}

    # -- delegated brief retention -------------------------------------------

    def remember_brief(self, agent: str, brief: str) -> None:
        """Stash a delegate call's instruction so the spawned lane's focus
        transcript can open with the delegated brief (the normalized
        AgentSpawned event carries no instruction)."""
        self._pending_briefs[agent] = brief

    # -- focused-lane transcripts (DESIGN-SPEC §8) ---------------------------

    def seed_transcript(self, event: ev.AgentSpawned) -> None:
        """(Re)start a lane's focus transcript at spawn.

        A known sub-session re-spawning is a replayed turn reusing its
        ids (the ``lanes.register`` reopen rule) — its transcript resets
        with it. Opens with the focused-lane banner and, when the parent
        delegate call carried one, the delegated brief as a ``delegated``
        user line (the demo's ``lane_focus_blocks`` shape).
        """
        record = self.lanes.get(event.sub_session_id)
        key = record.session_id if record is not None else event.sub_session_id
        # The envelope session_id IS the parent for agent_spawned and sits
        # on the redaction module's structural allowlist; the payload's
        # parent_session_id may arrive scrubbed.
        parent = event.session_id or event.parent_session_id
        blocks: list[TranscriptBlock] = [
            SessionBanner(
                id=self._ids.next_id(),
                headline="",
                focus_note=focused_lane_banner(event.agent, _display_short(parent)),
            )
        ]
        brief = self._pending_briefs.pop(event.agent, "")
        if brief:
            blocks.append(UserLine(id=self._ids.next_id(), text=brief, mode="delegated"))
        while key not in self._lane_transcripts and (
            len(self._lane_transcripts) >= _LANE_TRANSCRIPT_MAX_LANES
        ):
            del self._lane_transcripts[next(iter(self._lane_transcripts))]
        self._lane_transcripts[key] = blocks

    def append_block(self, record: LaneRecord, block: TranscriptBlock) -> None:
        """Append one block to a lane's focus transcript, bounded.

        Lanes restored without a spawn event get a banner-only seed so
        their activity still accumulates somewhere focusable.
        """
        blocks = self._lane_transcripts.get(record.session_id)
        if blocks is None:
            seeded: list[TranscriptBlock] = [
                SessionBanner(
                    id=self._ids.next_id(),
                    headline="",
                    focus_note=focused_lane_banner(
                        record.lane.name, _display_short(record.parent_id or "")
                    ),
                )
            ]
            while len(self._lane_transcripts) >= _LANE_TRANSCRIPT_MAX_LANES:
                del self._lane_transcripts[next(iter(self._lane_transcripts))]
            blocks = self._lane_transcripts[record.session_id] = seeded
        blocks.append(block)
        if len(blocks) > _LANE_TRANSCRIPT_MAX_BLOCKS:
            del blocks[min(_LANE_SEED_ROWS, len(blocks) - 1)]

    def transcript(self, key: str) -> list[TranscriptBlock] | None:
        """A lane's accumulated focus transcript, by session id or name.

        The real-runtime counterpart of the demo adapter's
        ``lane_blocks`` — ``None`` (not ``[]``) when nothing is known so
        the caller's no-transcript notice stays meaningful.
        """
        record = self.lanes.get(key)
        if record is not None:
            key = record.session_id
        blocks = self._lane_transcripts.get(key)
        if blocks is None:
            for candidate in self.lanes.lanes:
                if candidate.lane.name == key:
                    blocks = self._lane_transcripts.get(candidate.session_id)
                    break
        return list(blocks) if blocks else None

    # -- lane live tail (DESIGN-SPEC §8, design doc D4) ---------------------

    def tail_delta(self, record: LaneRecord, event: ev.StreamBlockDelta) -> None:
        """Buffer a child text delta; repaint the focused lane's tail.

        Accumulate-then-notify (the ``StreamStatusTracker._on_delta``
        shape): the host is repainted with the whole buffer at most every
        ``LANE_TAIL_NOTIFY_SECONDS``, so throttling drops paints, never
        text. The root stream always preempts; thinking blocks stay dark.
        """
        if event.block_type not in ("", "text"):
            return
        if event.text:
            buffered = self._lane_tails.get(record.session_id, "") + event.text
            self._lane_tails[record.session_id] = buffered[-_LANE_TAIL_MAX_CHARS:]
        self.lanes.note_stream_activity(record.session_id)
        if self.root_streaming:
            return  # root always preempts (D4)
        focused = self.lanes.tail_lane
        if focused is None or focused.session_id != record.session_id:
            return
        now = self._tail_clock()
        # 1e-9 slack: a clock landing exactly on the 0.05s boundary must
        # paint (float subtraction alone under-reports the elapsed time).
        if self._lane_tail_shown == record.session_id and (
            now - self._lane_tail_last < LANE_TAIL_NOTIFY_SECONDS - 1e-9
        ):
            return
        self._lane_tail_last = now
        self._lane_tail_shown = record.session_id
        self._host.lane_tail_updated(self._lane_tails.get(record.session_id, ""))

    def clear_tail(self, session_id: str | None = None) -> None:
        """Drop lane-tail state: one lane's buffer, or everything.

        Ephemeral by design — tail text never becomes a transcript block
        (durable content arrives via Channel B; see app.py stream_closed).
        """
        if session_id is None:
            self._lane_tails.clear()
        else:
            self._lane_tails.pop(session_id, None)
        if self._lane_tail_shown is not None and (
            session_id is None or self._lane_tail_shown == session_id
        ):
            self._lane_tail_shown = None
            self._host.lane_tail_cleared()

    def repaint_tail(self) -> None:
        """Paint the focused lane's buffered tail right now (ctrl+o).

        Cycling the pin must not wait for the new lane's next delta —
        otherwise the tail keeps showing the previous lane's text. Skips
        the throttle (a keypress, not a delta storm); clears instead when
        the pinned lane has nothing buffered yet.
        """
        if self.root_streaming:
            return
        focused = self.lanes.tail_lane
        buffered = "" if focused is None else self._lane_tails.get(focused.session_id, "")
        if focused is None or not buffered:
            if self._lane_tail_shown is not None:
                self._lane_tail_shown = None
                self._host.lane_tail_cleared()
            return
        self._lane_tail_last = self._tail_clock()
        self._lane_tail_shown = focused.session_id
        self._host.lane_tail_updated(buffered)


__all__ = [
    "LANE_TAIL_NOTIFY_SECONDS",
    "LaneReducer",
    "LaneTailHost",
]
