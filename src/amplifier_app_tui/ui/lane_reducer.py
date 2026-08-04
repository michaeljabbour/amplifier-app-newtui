"""Agent-lane presentation state: live tail + focused-lane transcripts.

Extracted from :class:`~amplifier_app_tui.ui.reducer.TranscriptReducer`
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
from collections.abc import Callable
from typing import Any, Literal, Protocol

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

LANE_ROWS_NOTIFY_SECONDS = 0.05
"""Lane-panel repaint floor for high-volume row updates (D5 AC5) — the
same accumulate-then-notify cadence as :data:`LANE_TAIL_NOTIFY_SECONDS`,
applied to the lane rows themselves (activity/state churn from rapid child
tool/stream events) rather than the tail text.

Only :data:`"progress"` notifications are ever subject to this window.
``LaneRegistry`` is a last-write-wins snapshot store — every ``update()``/
``complete()`` call already applied the latest state before ``notify_*``
is even called — so coalescing a progress repaint never loses data, only
repaint timing. The three privileged kinds (``final``/``error``/
``attention``) and any kind that isn't literally ``"progress"`` always
flush immediately: the throttle is an allow-list of exactly one kind, not
a deny-list, so a new/forgotten kind fails open (flushes) rather than
silently coalescing."""

LaneNotifyKind = Literal["progress", "final", "error", "attention"]
"""The lane-repaint privilege classes (D5 AC5).

``"progress"`` is the only coalescable kind (narration churn: thinking /
writing / reviewing / tool labels). ``"final"`` (a lane completing, success
or failure), ``"error"`` (a discrete failure surfaced against a still-running
lane: a tool error, or a failed tool result) and ``"attention"`` (a signal
that needs the user's notice independent of ordinary progress) always
bypass coalescing — see :meth:`LaneReducer.notify_lanes_changed`."""

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

    A structural subset of :class:`~amplifier_app_tui.ui.reducer.ReducerHost`
    — the two lane callbacks are all this unit touches, so it never has to
    know about the rest of the host (and there is no import cycle with the
    turn reducer that owns the full protocol).
    """

    def lane_tail_updated(self, text: str) -> None: ...
    def lane_tail_cleared(self) -> None: ...
    def lanes_changed(self) -> None: ...


class LaneReducer:
    """Lane presentation state: focus transcripts + the live tail.

    Driven by :class:`~amplifier_app_tui.ui.reducer.TranscriptReducer`,
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
        schedule_flush: Callable[[float, Callable[[], None]], object] | None = None,
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
        # -- lane-rows repaint coalescing (D5 AC5) ---------------------------
        self._lanes_notify_last = 0.0
        """Same accumulate-then-notify shape as the tail above, applied to
        the lane rows themselves. Only ``kind="progress"`` respects the
        window; ``final``/``error``/``attention`` always flush (see
        :meth:`notify_lanes_changed`)."""
        self._schedule_flush = schedule_flush
        """Host-provided one-shot timer (``TuiApp.set_timer`` in production)
        used ONLY to guarantee a coalesced progress repaint is never
        stranded: without it, a lane that goes quiet right after a
        coalesced update would show stale telemetry until its next event.
        ``None`` (tests, or hosts that don't care) degrades safely to
        "never coalesce" — see :meth:`notify_lanes_changed`."""
        self._lanes_flush_pending = False
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

    def pending_brief(self, agent: str) -> str:
        """Peek the stashed brief for *agent* without consuming it.

        The turn reducer reads it at spawn for the chat's compact
        ``started`` lifecycle marker; :meth:`seed_transcript` still pops
        it into the lane's focus transcript."""
        return self._pending_briefs.get(agent, "")

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
        # D6 AC4: the registry is the single authority for which turn
        # spawned this lane -- _agent_spawned registers it there BEFORE
        # calling here, so the record (when known) already carries it.
        turn = record.turn if record is not None else 0
        # The envelope session_id IS the parent for agent_spawned and sits
        # on the redaction module's structural allowlist; the payload's
        # parent_session_id may arrive scrubbed.
        parent = event.session_id or event.parent_session_id
        blocks: list[TranscriptBlock] = [
            SessionBanner(
                id=self._ids.next_id(),
                headline="",
                focus_note=focused_lane_banner(event.agent, _display_short(parent), turn),
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
                        record.lane.name, _display_short(record.parent_id or ""), record.turn
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

    # -- lane-rows repaint coalescing (D5 AC5) -------------------------------

    def notify_lanes_changed(self, *, kind: LaneNotifyKind = "progress") -> None:
        """Repaint the lane rows — throttled for high-volume progress churn,
        but PROVABLY lossless for the three privileged classes.

        ``LaneRegistry`` is a last-write-wins snapshot: every ``update()``/
        ``complete()`` call the turn reducer makes already applied the
        latest activity/state/telemetry before this is even called, so
        coalescing a repaint never drops DATA — only its timing. Only
        ``kind="progress"`` (narration churn: thinking / writing /
        reviewing / tool labels, and per-lane token/cost ticking) respects
        :data:`LANE_ROWS_NOTIFY_SECONDS`; a call within the window is
        coalesced (skipped) exactly once the window reopens — AND a
        trailing flush is scheduled (when the host wired ``schedule_flush``)
        so a lane that goes quiet right after a coalesced update is never
        stranded showing stale telemetry: the repaint still lands on its
        own, it is just batched with whatever arrived in the same window.

        ``kind="final"`` (a lane completed, success or failure),
        ``kind="error"`` (a discrete failure surfaced against a still-running
        lane: a tool error, or a failed tool result) and ``kind="attention"``
        (a signal that needs the user's notice independent of ordinary
        progress) — and any other kind, should one ever be added — always
        call the host immediately, synchronously, unconditionally: the
        throttle is a one-item allow-list (``kind == "progress"``), not a
        deny-list, so an unrecognized or future kind fails open (flushes)
        rather than silently coalescing. This is what makes the guarantee
        provable rather than probabilistic — there is no code path by
        which a privileged kind can be dropped, delayed, or merged away.
        """
        now = self._tail_clock()
        if kind == "progress":
            # 1e-9 slack: a clock landing exactly on the boundary must still
            # paint (float subtraction alone under-reports elapsed time) —
            # mirrors the tail's own throttle in :meth:`tail_delta`.
            if now - self._lanes_notify_last < LANE_ROWS_NOTIFY_SECONDS - 1e-9:
                self._schedule_trailing_flush()
                return
        self._lanes_notify_last = now
        self._lanes_flush_pending = False
        self._host.lanes_changed()

    def _schedule_trailing_flush(self) -> None:
        """Guarantee a coalesced progress update still lands on its own.

        Without this, a lane whose LAST event before going quiet happened
        to land inside the throttle window would show stale telemetry
        forever (nothing else would ever call :meth:`notify_lanes_changed`
        again to notice the window had reopened). One pending flush covers
        arbitrarily many coalesced calls in the same window — it always
        paints whatever is current when it actually fires, not whatever
        was current when it was scheduled.
        """
        if self._schedule_flush is None or self._lanes_flush_pending:
            return
        self._lanes_flush_pending = True
        self._schedule_flush(LANE_ROWS_NOTIFY_SECONDS, self._flush_pending_lanes)

    def _flush_pending_lanes(self) -> None:
        self._lanes_flush_pending = False
        self._lanes_notify_last = self._tail_clock()
        self._host.lanes_changed()


__all__ = [
    "LANE_ROWS_NOTIFY_SECONDS",
    "LANE_TAIL_NOTIFY_SECONDS",
    "LaneNotifyKind",
    "LaneReducer",
    "LaneTailHost",
]
