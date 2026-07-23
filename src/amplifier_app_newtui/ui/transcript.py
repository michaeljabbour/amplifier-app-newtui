"""The transcript: durable history rendered per DESIGN-SPEC §3 + §11.

Two-region model (ADR-0007): this module is the *durable history* region.
Recent blocks use one interactive :class:`BlockWidget` each; finalized older
blocks consolidate into one selectable, action-aware :class:`HistoryArchive`
so arbitrarily long chats do not burden Textual's compositor. The mutable
streaming region lives in ``ui/live_tail.py`` and consolidates into an
``Answer`` block that gets appended here.

Rendering is a pure function :func:`render_block` of ``(block, width)``
producing lines of :class:`Segment` — exact spec glyphs and strings, no
Textual objects — so every visual detail is unit-testable as plain text
(golden width matrix 40/80/120). Widgets paint those lines via
``ui/segments.py`` markup, whose styles are theme-variable references
(``$dim`` …), never colors.

Widget communication is Textual messages only (no callbacks into the app):

- :class:`ShowEvidence` — a final answer was clicked (DESIGN-SPEC §10).
- :class:`OpenRewind` — a turn rule was clicked; carries the checkpoint id
  stamped on the block at emit time (DESIGN-SPEC §3/§9).
- :class:`ToolLineToggled` — a collapsed tool line was expanded/collapsed
  in place (click or enter).
- :class:`DelegateSummaryToggled` — a delegate fan-out summary was
  expanded/collapsed in place (click or enter).
- :class:`LaneFocusChanged` — the view swapped to a subagent's block list
  or back (DESIGN-SPEC §8).

Resize (DESIGN-SPEC §12, RESEARCH-BRIEF risk 3): a width change starts a
75ms trailing debounce; while a stream is painting the reflow is deferred,
and exactly one forced reflow runs when streaming ends
(``set_streaming(False)``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from time import monotonic
from typing import Any, cast

from textual import events
from textual.binding import Binding
from textual.content import Content
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import Reactive, ReactiveType
from textual.timer import Timer
from textual.widgets import Static

from ..model.blocks import (
    Answer,
    DelegateSummaryBlock,
    EvidenceBlock,
    NeedsYouBlock,
    PlanBlock,
    Segment,
    ToolLine,
    TranscriptBlock,
)
from ..model.evidence import EvidenceLink
from .keymap import KEYMAP
from .motion import SHIMMER_INTERVAL_SECONDS
from .needs_you import NeedsYouList
from .segments import segment_markup
from .transcript_render import fence_text_at_row, render_block, render_block_markup

REFLOW_DEBOUNCE_SECONDS = 0.075
"""Trailing debounce for resize reflow (per ADR-0007 / codex precedent)."""

SPINNER_INTERVAL_SECONDS = 1.0
"""Working-line glyph cadence: the mockup advances ✳/✦/✧/✦ inside the
1000ms telemetry tick (design-v3-cohesive.html runTurn, ``secs % 4``) —
the faster 260ms spinTimer is the §2 TITLE-bar spinner only."""

MOTION_INTERVAL_SECONDS = SHIMMER_INTERVAL_SECONDS
"""Active-only soft-band cadence for working/coordinating labels."""

FALLBACK_WIDTH = 80
"""Width used before first layout (corrected by the first real resize)."""

HISTORY_WIDGET_LIMIT = 1_000
"""Recent blocks kept as fully independent widgets."""

HISTORY_COMPACT_TRIGGER = 1_200
"""Hysteresis avoids rebuilding the archive for every new durable block."""


# --------------------------------------------------------------------------
# Messages — the ONLY way transcript widgets talk to the app
# --------------------------------------------------------------------------


class ShowEvidence(Message):
    """A final answer was clicked → open its evidence block (spec §10)."""

    def __init__(self, block_id: str, links: tuple[EvidenceLink, ...]) -> None:
        super().__init__()
        self.block_id = block_id
        self.links = links


class OpenRewind(Message):
    """A turn rule was clicked → open the rewind picker at this checkpoint."""

    def __init__(self, checkpoint_id: str) -> None:
        super().__init__()
        self.checkpoint_id = checkpoint_id


class CopyCodeFence(Message):
    """A fenced code block inside an answer was clicked → copy just that
    fence to the clipboard (``/copy`` still grabs the whole answer)."""

    def __init__(self, block_id: str, text: str) -> None:
        super().__init__()
        self.block_id = block_id
        self.text = text


class ToolLineToggled(Message):
    """A tool line's body was expanded/collapsed in place."""

    def __init__(self, block_id: str, expanded: bool) -> None:
        super().__init__()
        self.block_id = block_id
        self.expanded = expanded


class DelegateSummaryToggled(Message):
    """A delegate summary was expanded/collapsed in place (click or enter)."""

    def __init__(self, block_id: str, expanded: bool) -> None:
        super().__init__()
        self.block_id = block_id
        self.expanded = expanded


class ExpandEvidenceClaim(Message):
    """Enter on a focused evidence block (spec §10 ``enter expand``) —
    deep-link the selected claim to the tool call that grounds it."""

    def __init__(self, block_id: str, link: EvidenceLink) -> None:
        super().__init__()
        self.block_id = block_id
        self.link = link


class CloseEvidence(Message):
    """Esc on a focused evidence block (spec §10 ``esc close``)."""

    def __init__(self, block_id: str) -> None:
        super().__init__()
        self.block_id = block_id


class LaneFocusChanged(Message):
    """The transcript swapped to a subagent lane (or back: ``lane_id=None``)."""

    def __init__(self, lane_id: str | None) -> None:
        super().__init__()
        self.lane_id = lane_id


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

_EVIDENCE_BINDINGS: tuple = tuple(
    binding for binding in KEYMAP if binding.contexts == frozenset({"evidence"})
)
"""Evidence-context chords sourced from the keymap table (single source:
the keys that work and the keys the header advertises can never drift)."""

_EVIDENCE_ACTIONS = frozenset(binding.action for binding in _EVIDENCE_BINDINGS)


class BlockWidget(Static):
    """One transcript block as a widget (ADR-0007 open-q 6: widget-per-block).

    State is the block itself; the widget re-derives its content from
    ``render_block(block, width)`` on every repaint. In-place mutation
    (tool expand/collapse, live plan updates, working-line telemetry)
    happens via :meth:`update_block` keyed by the block's stable id; the
    working line's own 1s timer pulses the spinner AND keeps its
    wall-clock seconds counting between event-driven updates (mockup
    1000ms tick — spec §3 "Updates every second", §11 live counting).
    """

    DEFAULT_CSS = """
    BlockWidget {
        height: auto;
    }
    BlockWidget.kind-user-line {
        margin-top: 1;
    }
    /* Mockup rhythm: the turn rule carries the largest vertical margin
       of any block (14px top vs the user line's 10px). */
    BlockWidget.kind-turn-rule {
        margin-top: 1;
    }
    /* Every other mockup mt:10 header maps to the same 1-cell gap as the
       user line (plan L313, ledger L279, context L502, doctor L506,
       improve L512; needs-you lives on NeedsYouWidget). mt:8 and below
       (narration, answer, evidence, working status) stay flush — the
       repo's px→cell mapping rounds sub-10px margins to 0. */
    BlockWidget.kind-plan,
    BlockWidget.kind-ledger,
    BlockWidget.kind-context,
    BlockWidget.kind-doctor,
    BlockWidget.kind-improve {
        margin-top: 1;
    }
    /* Exception: the plan-mode 'Proposed plan … (read-only)' header is
       mt:8 in the mockup (runPlanTurn L434), not mt:10 like the
       executing-turn plan header (L313) — it stays flush. */
    BlockWidget.kind-plan.read-only {
        margin-top: 0;
    }
    /* A final answer opens its own paragraph — separated from the preceding
       working line / turn rule so long answers read as a document. */
    BlockWidget.kind-answer {
        margin-top: 1;
    }
    BlockWidget.kind-answer.compact {
        margin-top: 0;
    }
    """

    BINDINGS = [
        Binding("enter", "activate", "activate", show=False),
        # Evidence-context chords (←/→ select · enter expand · esc close,
        # spec §10) from the keymap table; :meth:`check_action` gates them
        # to focused evidence blocks so they never leak to other kinds.
        *(
            Binding(key, binding.action, binding.label, show=False)
            for binding in _EVIDENCE_BINDINGS
            for key in binding.keys
        ),
    ]

    def __init__(
        self,
        block: TranscriptBlock,
        *,
        reflow_router: Callable[[BlockWidget], bool] | None = None,
    ) -> None:
        super().__init__(id=f"block-{block.id}")
        self._block = block
        self._painted_width: int | None = None
        self._reflow_router = reflow_router
        self._spinner_offset = 0
        self._spin_timer: Timer | None = None
        self._motion_offset = 0
        self._motion_timer: Timer | None = None
        # Wall-clock anchor for the working line's seconds (spec §3
        # "Updates every second" / §11 live counting — mockup 1000ms tick):
        # event-driven replaces reset it; between events (silent tool
        # calls, open approval bars) the displayed secs keep advancing.
        self._telemetry_anchor: float | None = (
            monotonic() if block.kind == "working_status" else None
        )
        self.add_class(f"kind-{block.kind.replace('_', '-')}")
        if isinstance(block, PlanBlock) and block.read_only:
            self.add_class("read-only")
        if isinstance(block, Answer) and block.compact:
            self.add_class("compact")
        if block.kind in ("tool_line", "evidence", "delegate_summary"):
            # Evidence blocks take keyboard focus so the header's
            # advertised keys work (keymap "evidence" context, spec §10).
            self.can_focus = True
        elif block.kind == "turn_rule":
            # Mockup line 46: the rule row advertises its rewind anchor
            # via a hover title (verbatim).
            self.tooltip = "turn rule · click to open rewind picker"

    @property
    def block(self) -> TranscriptBlock:
        return self._block

    def on_mount(self) -> None:
        self.repaint_block()
        if self._block.kind == "working_status":
            self._spin_timer = self.set_interval(SPINNER_INTERVAL_SECONDS, self._advance_spinner)
            self._motion_timer = self.set_interval(MOTION_INTERVAL_SECONDS, self._advance_motion)

    def on_unmount(self) -> None:
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        if self._motion_timer is not None:
            self._motion_timer.stop()
            self._motion_timer = None

    def _advance_spinner(self) -> None:
        """Pulse ✳/✦/✧ (and tick wall-clock secs) between event replaces."""
        self._spinner_offset += 1
        self.repaint_block()

    def _advance_motion(self) -> None:
        """Move the active label highlight without mutating transcript text."""
        self._motion_offset += 1
        self.repaint_block()

    def update_block(self, block: TranscriptBlock) -> None:
        """Replace this widget's block in place (same stable id)."""
        if block.id != self._block.id:
            raise ValueError(f"block id mismatch: widget has {self._block.id!r}, got {block.id!r}")
        self._block = block
        if isinstance(block, PlanBlock):
            self.set_class(block.read_only, "read-only")
        if isinstance(block, Answer):
            self.set_class(block.compact, "compact")
        if block.kind == "working_status":
            # Fresh event telemetry: re-anchor the wall-clock secs tick.
            self._telemetry_anchor = monotonic()
        self.repaint_block()

    def repaint_block(self) -> None:
        """Re-derive content from (block, current width)."""
        width = self.size.width or FALLBACK_WIDTH
        self._painted_width = width
        block = self._block
        if block.kind == "working_status":
            update: dict[str, object] = {}
            if self._spinner_offset:
                update["spinner_frame"] = block.spinner_frame + self._spinner_offset
            if self._motion_offset:
                update["motion_frame"] = block.motion_frame + self._motion_offset
            if self._telemetry_anchor is not None:
                # Whole wall-clock seconds since the last event-driven
                # replace — the working line keeps counting while the
                # runtime is silent (mockup setInterval secs++, spec §11).
                elapsed = int(monotonic() - self._telemetry_anchor)
                if elapsed > 0:
                    update["telemetry"] = block.telemetry.model_copy(
                        update={"secs": block.telemetry.secs + elapsed}
                    )
            if update:
                block = block.model_copy(update=update)
        self.update(render_block_markup(block, width))

    def on_resize(self, event: events.Resize) -> None:
        width = self.size.width
        if width <= 0 or width == self._painted_width:
            return
        if self._reflow_router is not None and self._reflow_router(self):
            return  # deferred: the TranscriptView owns the debounce
        self.repaint_block()

    def on_click(self, event: events.Click) -> None:
        if self._block.kind == "answer":
            text = self._fence_click_text(event)
            if text is not None:
                # Clicking directly on a fenced code block copies just that
                # fence; any other spot on the answer falls through to the
                # normal activate (evidence, spec §10).
                self.post_message(CopyCodeFence(self._block.id, text))
                return
        self._activate()

    def _fence_click_text(self, event: events.Click) -> str | None:
        """Dedented source of the code fence under the click, or ``None``."""
        offset = event.get_content_offset(self)
        if offset is None:
            return None
        width = self._painted_width or self.size.width or FALLBACK_WIDTH
        return fence_text_at_row(render_block(self._block, width), offset.y)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate kind-specific bindings: evidence chords fire only on a
        focused evidence block; enter there means expand, not activate."""
        if action in _EVIDENCE_ACTIONS:
            return self._block.kind == "evidence"
        if action == "activate":
            return self._block.kind != "evidence"
        return True

    def action_activate(self) -> None:
        self._activate()

    def action_evidence_prev(self) -> None:
        self._move_evidence_selection(-1)

    def action_evidence_next(self) -> None:
        self._move_evidence_selection(1)

    def action_evidence_expand(self) -> None:
        block = self._block
        if block.kind == "evidence" and block.links:
            self.post_message(ExpandEvidenceClaim(block.id, block.links[block.selected]))

    def action_close_evidence(self) -> None:
        if self._block.kind == "evidence":
            self.post_message(CloseEvidence(self._block.id))

    def _move_evidence_selection(self, delta: int) -> None:
        """←/→ move the highlighted claim; the header 1/N tracks it."""
        block = self._block
        if block.kind != "evidence" or not block.links:
            return
        selected = max(0, min(len(block.links) - 1, block.selected + delta))
        if selected != block.selected:
            self._block = block.model_copy(update={"selected": selected})
            self.repaint_block()

    def _activate(self) -> None:
        block = self._block
        if block.kind == "tool_line" and block.body:
            toggled = block.model_copy(update={"expanded": not block.expanded})
            self._block = toggled
            self.repaint_block()
            self.post_message(ToolLineToggled(toggled.id, toggled.expanded))
        elif block.kind == "delegate_summary" and block.entries:
            toggled = block.model_copy(update={"expanded": not block.expanded})
            self._block = toggled
            self.repaint_block()
            self.post_message(DelegateSummaryToggled(toggled.id, toggled.expanded))
        elif block.kind == "answer" and block.clickable:
            self.post_message(ShowEvidence(block.id, block.evidence_refs))
        elif block.kind == "turn_rule":
            self.post_message(OpenRewind(block.checkpoint_id))


class NeedsYouBlockWidget(NeedsYouList):
    """A needs-you block mounted in the transcript flow (DESIGN-SPEC §7).

    The mockup attaches the click handler *per decision row*
    (design-v3-cohesive.html:286-292) — acting on one decision applies
    THAT decision, so the transcript mounts the per-row hit-testing
    :class:`~amplifier_app_newtui.ui.needs_you.NeedsYouList` instead of a
    single flat :class:`BlockWidget`. Chip/row clicks post
    :class:`NeedsYouList.DecisionTaken`; the header is not a click target.
    """

    DEFAULT_CSS = """
    NeedsYouBlockWidget {
        /* Mockup showNeedsYou header: mt 10 — the user-line gap. */
        margin-top: 1;
    }
    """

    def __init__(self, block: NeedsYouBlock) -> None:
        super().__init__(block, id=f"block-{block.id}")
        self._needs_you_block = block

    @property
    def block(self) -> NeedsYouBlock:
        return self._needs_you_block

    def update_block(self, block: TranscriptBlock) -> None:
        """Replace this widget's block in place (same stable id)."""
        if block.id != self._needs_you_block.id:
            raise ValueError(
                f"block id mismatch: widget has {self._needs_you_block.id!r}, got {block.id!r}"
            )
        if not isinstance(block, NeedsYouBlock):  # pragma: no cover - defensive
            raise TypeError(f"needs_you widget got block kind {block.kind!r}")
        self._needs_you_block = block
        super().update_block(block)

    def repaint_block(self) -> None:
        """Width-pure rows re-layout themselves; nothing to re-derive."""


TranscriptWidget = BlockWidget | NeedsYouBlockWidget
"""One mounted transcript block (needs-you blocks get per-row widgets)."""


def build_block_widget(
    block: TranscriptBlock,
    *,
    reflow_router: Callable[[BlockWidget], bool] | None = None,
) -> TranscriptWidget:
    """The widget for one block: per-row needs-you list, else BlockWidget."""
    if isinstance(block, NeedsYouBlock):
        return NeedsYouBlockWidget(block)
    return BlockWidget(block, reflow_router=reflow_router)


def _block_margin_top(block: TranscriptBlock) -> int:
    """Mirror the per-kind CSS rhythm inside the consolidated archive."""

    if block.kind in {
        "user_line",
        "turn_rule",
        "ledger",
        "context",
        "doctor",
        "improve",
        "needs_you",
    }:
        return 1
    if isinstance(block, PlanBlock):
        return 0 if block.read_only else 1
    if isinstance(block, Answer):
        return 0 if block.compact else 1
    return 0


class HistoryArchive(Static):
    """One selectable, interactive visual for finalized older history.

    It removes thousands of children from Textual's compositor without
    removing a single line from the conversation. Theme-token markup keeps
    archived text visually identical, and ``@click`` metadata preserves tool,
    evidence, rewind, and deferred-decision actions even after consolidation.
    """

    DEFAULT_CSS = """
    HistoryArchive {
        width: 100%;
        height: auto;
    }
    """

    BINDINGS = [
        Binding(key, binding.action, binding.label, show=False)
        for binding in _EVIDENCE_BINDINGS
        for key in binding.keys
    ]

    def __init__(self, owner: "TranscriptView") -> None:
        super().__init__("", id="transcript-history-archive")
        self._owner = owner
        self._blocks: tuple[TranscriptBlock, ...] = ()
        self._painted_width: int | None = None
        self._block_offsets: dict[str, int] = {}
        self._active_evidence_id: str | None = None
        self.can_focus = True

    @property
    def blocks(self) -> tuple[TranscriptBlock, ...]:
        return self._blocks

    def update_blocks(self, blocks: Sequence[TranscriptBlock]) -> None:
        self._blocks = tuple(blocks)
        if self._active_evidence_id not in {block.id for block in self._blocks}:
            self._active_evidence_id = None
        if self.is_mounted:
            self.repaint_archive()

    def on_mount(self) -> None:
        self.repaint_archive()

    def on_resize(self, event: events.Resize) -> None:
        if event.size.width <= 0 or event.size.width == self._painted_width:
            return
        if self._owner._route_archive_reflow():
            return
        self.repaint_archive()

    @staticmethod
    def _block_action(block: TranscriptBlock) -> str | None:
        if block.kind == "tool_line" and block.body:
            return f"archive_activate({block.id!r})"
        if block.kind == "delegate_summary" and block.entries:
            return f"archive_activate({block.id!r})"
        if block.kind == "answer" and block.clickable:
            return f"archive_activate({block.id!r})"
        if block.kind in ("turn_rule", "evidence"):
            return f"archive_activate({block.id!r})"
        return None

    @staticmethod
    def _styled_segment(segment: Segment, action: str | None) -> str:
        markup = segment_markup(segment)
        return f"[@click={action}]{markup}[/]" if action and markup else markup

    def _block_markup(self, block: TranscriptBlock, width: int) -> str:
        lines = render_block(block, width)
        default_action = self._block_action(block)
        rendered_lines: list[str] = []
        for line_index, line in enumerate(lines):
            default_line_action = default_action
            choice_index = 0
            item_index = line_index - 1
            if isinstance(block, NeedsYouBlock) and 0 <= item_index < len(block.items):
                entry = block.items[item_index]
                default_line_action = (
                    f"archive_decision({block.id!r}, {item_index}, 0)" if entry.choices else None
                )
            parts: list[str] = []
            for segment in line:
                action = default_line_action
                if (
                    isinstance(block, NeedsYouBlock)
                    and 0 <= item_index < len(block.items)
                    and segment.bg_token == "bg-tab"
                ):
                    action = f"archive_decision({block.id!r}, {item_index}, {choice_index})"
                    choice_index += 1
                parts.append(self._styled_segment(segment, action))
            rendered_lines.append("".join(parts))
        return "\n".join(rendered_lines)

    def repaint_archive(self) -> None:
        width = self.size.width or FALLBACK_WIDTH
        self._painted_width = width
        parts: list[str] = []
        offsets: dict[str, int] = {}
        row = 0
        for index, block in enumerate(self._blocks):
            if index:
                parts.append("\n")
            margin = _block_margin_top(block)
            if margin:
                parts.append("\n" * margin)
                row += margin
            offsets[block.id] = row
            markup = self._block_markup(block, width)
            parts.append(markup)
            content = Content.from_markup(markup)
            row += max(1, content.get_height(cast(Any, self.styles), width))
        self._block_offsets = offsets
        self.update(Content.from_markup("".join(parts)))

    def block_offset(self, block_id: str) -> int | None:
        return self._block_offsets.get(block_id)

    def action_archive_activate(self, block_id: str) -> None:
        block = self._owner.get_block(block_id)
        if block is None:
            return
        if block.kind == "tool_line" and block.body:
            toggled = block.model_copy(update={"expanded": not block.expanded})
            self._owner.replace(toggled)
            self.post_message(ToolLineToggled(toggled.id, toggled.expanded))
        elif block.kind == "delegate_summary" and block.entries:
            toggled = block.model_copy(update={"expanded": not block.expanded})
            # An explicit user toggle: bypass the reducer-replace expansion
            # merge, or collapsing would be undone by _preserve_expansion.
            self._owner.replace(toggled, preserve_expansion=False)
            self.post_message(DelegateSummaryToggled(toggled.id, toggled.expanded))
        elif block.kind == "answer" and block.clickable:
            self.post_message(ShowEvidence(block.id, block.evidence_refs))
        elif block.kind == "turn_rule":
            self.post_message(OpenRewind(block.checkpoint_id))
        elif block.kind == "evidence":
            self._active_evidence_id = block.id
            self.focus()

    def action_archive_decision(self, block_id: str, item_index: int, choice_index: int) -> None:
        block = self._owner.get_block(block_id)
        if not isinstance(block, NeedsYouBlock):
            return
        try:
            entry = block.items[item_index]
            choice = entry.choices[choice_index]
        except IndexError:
            return
        self.post_message(NeedsYouList.DecisionTaken(entry.decision_id, choice.answer))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in _EVIDENCE_ACTIONS:
            return self._active_evidence_id is not None
        return True

    def action_evidence_prev(self) -> None:
        self._move_evidence_selection(-1)

    def action_evidence_next(self) -> None:
        self._move_evidence_selection(1)

    def _active_evidence(self) -> EvidenceBlock | None:
        if self._active_evidence_id is None:
            return None
        block = self._owner.get_block(self._active_evidence_id)
        return block if isinstance(block, EvidenceBlock) else None

    def _move_evidence_selection(self, delta: int) -> None:
        block = self._active_evidence()
        if block is None or not block.links:
            return
        selected = max(0, min(len(block.links) - 1, block.selected + delta))
        if selected != block.selected:
            self._owner.replace(block.model_copy(update={"selected": selected}))

    def action_evidence_expand(self) -> None:
        block = self._active_evidence()
        if block is not None and block.links:
            self.post_message(ExpandEvidenceClaim(block.id, block.links[block.selected]))

    def action_close_evidence(self) -> None:
        block = self._active_evidence()
        if block is not None:
            self.post_message(CloseEvidence(block.id))


class TranscriptView(VerticalScroll):
    """Scrollable durable history with a bounded interactive widget tail.

    The newest ~1k blocks retain their independent widgets. Older blocks are
    painted by one :class:`HistoryArchive`, which remains selectable and keeps
    the same click/keyboard actions through Textual action metadata. This
    preserves the infinite-chat feel while bounding compositor layout work.

    - **Tail-follow anchor**: sticks to the bottom whenever content
      height grows (append, async child mounts, wrap reflow) unless the
      user scrolled up; scrolling back to the bottom re-arms following.
      Implemented on Textual's standing ``anchor()`` facility — the
      compositor re-asserts bottom scroll on every arrange, so late
      height growth (e.g. a needs-you row wrapping 1→2 lines after its
      rows mount asynchronously) can never strand the tail mid-scroll
      the way a one-shot ``scroll_end`` per append could.
    - **Keyed mutation**: :meth:`append` / :meth:`replace` /
      :meth:`remove_block` address blocks by stable id.
    - **Lane focus** (spec §8): :meth:`focus_lane` swaps the visible block
      list to a subagent's transcript; :meth:`restore_main` (the app's esc
      handler) swaps back. While focused, append/replace/remove address
      the *stashed parent* list (mockup: ``this.lines`` keeps accumulating
      separately from ``focusLines``), so a turn that keeps running during
      focus is fully up to date when esc restores the parent transcript.
    - **Resize reflow**: 75ms trailing debounce; deferred during streaming
      with one forced reflow at :meth:`set_streaming` (False).
    """

    DEFAULT_CSS = """
    TranscriptView {
        scrollbar-size-vertical: 1;
    }
    """
    # Scrollbar COLORS are set from the app stylesheet (ui/app.py) with the
    # §1 token variables — DEFAULT_CSS here must stay token-free so the
    # widget mounts in hosts that never registered the spec themes (tests).

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._blocks: dict[str, TranscriptBlock] = {}
        self._widgets: dict[str, TranscriptWidget] = {}
        self._order: list[str] = []
        self._archive: HistoryArchive | None = None
        self._archive_ids: list[str] = []
        self._compaction_pending = False
        self._focused_lane: str | None = None
        self._main_stash: list[TranscriptBlock] | None = None
        self._streaming = False
        self._reflow_hold = False
        self._reflow_deferred = False
        self._reflow_timer: Timer | None = None
        self._last_width: int | None = None

    def on_mount(self) -> None:
        # Standing tail anchor: the compositor re-asserts bottom scroll on
        # EVERY arrange while anchored, so content that grows after an
        # append settles (async needs-you row mounts, 1→2-line wrap on
        # first row resize, long wrapped answer lines) is always followed.
        self.anchor()

    # -- block CRUD --------------------------------------------------------

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    @property
    def blocks(self) -> tuple[TranscriptBlock, ...]:
        # A mounted widget may hold transient UI-local state (for example
        # the selected evidence claim) before it ever becomes archival
        # state. Read that live state first; archived blocks come directly
        # from the canonical store.
        return tuple(
            self._widgets[block_id].block if block_id in self._widgets else self._blocks[block_id]
            for block_id in self._order
        )

    def get_block(self, block_id: str) -> TranscriptBlock | None:
        widget = self._widgets.get(block_id)
        return widget.block if widget is not None else self._blocks.get(block_id)

    def get_widget(self, block_id: str) -> TranscriptWidget | None:
        """The mounted widget for *block_id* (None while stashed/unknown)."""
        return self._widgets.get(block_id)

    def append(self, block: TranscriptBlock) -> TranscriptWidget | None:
        """Mount a new block at the end (follows the tail when anchored).

        While a lane is focused the append lands in the stashed *parent*
        list (spec §8: the parent turn keeps accumulating during focus)
        and returns ``None`` — nothing is mounted until esc restores.
        """
        if self._focused_lane is not None and self._main_stash is not None:
            if any(stashed.id == block.id for stashed in self._main_stash):
                raise ValueError(f"duplicate block id: {block.id!r}")
            self._main_stash.append(block)
            return None
        if block.id in self._blocks:
            raise ValueError(f"duplicate block id: {block.id!r}")
        widget = build_block_widget(block, reflow_router=self._route_reflow)
        self._blocks[block.id] = block
        self._widgets[block.id] = widget
        self._order.append(block.id)
        # No one-shot scroll here: while the tail anchor is engaged the
        # compositor keeps the view at the bottom through this mount AND
        # any later height growth of the mounted widget (async child rows,
        # wrap reflow); while released (user scrolled up) it must not move.
        self.mount(widget)
        self._schedule_compaction()
        return widget

    def _preserve_expansion(self, block: TranscriptBlock) -> TranscriptBlock:
        """Carry a user's UI-local expansion across a reducer replace.

        The reducer always re-renders a delegate summary collapsed
        (expansion is UI-local by design); without this merge, a
        mid-flight update — or a post-turn straggler ``AgentCompleted``,
        which replaces the block after the turn ended — collapses a
        summary the user has opened (review finding H1).
        """
        if block.kind != "delegate_summary" or block.expanded:
            return block
        current: TranscriptBlock | None = None
        widget = self._widgets.get(block.id)
        if isinstance(widget, BlockWidget):
            current = widget.block
        elif self._focused_lane is not None and self._main_stash is not None:
            current = next((s for s in self._main_stash if s.id == block.id), None)
        else:
            current = self._blocks.get(block.id)
        if isinstance(current, DelegateSummaryBlock) and current.expanded:
            return block.model_copy(update={"expanded": True})
        return block

    def replace(
        self, block: TranscriptBlock, *, preserve_expansion: bool = True
    ) -> None:
        """Swap a block's content in place, keyed by its stable id.

        ``preserve_expansion=False`` is for explicit user toggles (the
        archive activate path) — data replaces from the reducer keep the
        default and never collapse an opened summary.

        While a lane is focused the replace addresses the stashed parent
        list — the focused child transcript is a read-only snapshot.
        """
        if preserve_expansion:
            block = self._preserve_expansion(block)
        if self._focused_lane is not None and self._main_stash is not None:
            for index, stashed in enumerate(self._main_stash):
                if stashed.id == block.id:
                    self._main_stash[index] = block
                    return
            raise KeyError(f"unknown block id: {block.id!r}")
        if block.id not in self._blocks:
            raise KeyError(f"unknown block id: {block.id!r}")
        self._blocks[block.id] = block
        if widget := self._widgets.get(block.id):
            widget.update_block(block)
        elif block.id in self._archive_ids and self._archive is not None:
            self._archive.update_blocks(
                tuple(self._blocks[archive_id] for archive_id in self._archive_ids)
            )
        else:  # pragma: no cover - internal representation invariant
            raise RuntimeError(f"block {block.id!r} is neither mounted nor archived")

    def remove_block(self, block_id: str) -> None:
        """Unmount a block (e.g. the working status line at turn end).

        While a lane is focused the removal addresses the stashed parent
        list, so e.g. the working line dropped at turn end never survives
        into the restored parent transcript.
        """
        if self._focused_lane is not None and self._main_stash is not None:
            for index, stashed in enumerate(self._main_stash):
                if stashed.id == block_id:
                    del self._main_stash[index]
                    return
            raise KeyError(f"unknown block id: {block_id!r}")
        block = self._blocks.pop(block_id, None)
        if block is None:
            raise KeyError(f"unknown block id: {block_id!r}")
        self._order.remove(block_id)
        widget = self._widgets.pop(block_id, None)
        if widget is not None:
            widget.remove()
            return
        if block_id in self._archive_ids:
            self._archive_ids.remove(block_id)
            if self._archive is not None:
                if self._archive_ids:
                    self._archive.update_blocks(
                        tuple(self._blocks[item_id] for item_id in self._archive_ids)
                    )
                else:
                    self._archive.remove()
                    self._archive = None

    def _schedule_compaction(self) -> None:
        if (
            len(self._widgets) <= HISTORY_COMPACT_TRIGGER
            or self._compaction_pending
            or not self.is_mounted
        ):
            return
        self._compaction_pending = True
        self.call_later(self._compact_history)

    async def _compact_history(self) -> None:
        """Move the old prefix into one visual without changing its text."""

        try:
            if len(self._widgets) <= HISTORY_COMPACT_TRIGGER:
                return
            archive_count = max(0, len(self._order) - HISTORY_WIDGET_LIMIT)
            archive_ids = self._order[:archive_count]
            newly_archived = [
                self._widgets[block_id] for block_id in archive_ids if block_id in self._widgets
            ]
            if not newly_archived:
                return
            archived_blocks = tuple(self._blocks[block_id] for block_id in archive_ids)
            async with self.batch():
                if self._archive is None:
                    archive = HistoryArchive(self)
                    archive.update_blocks(archived_blocks)
                    self._archive = archive
                    first_recent = self._widgets.get(self._order[archive_count])
                    if first_recent is None:  # pragma: no cover - limit keeps a tail
                        await self.mount(archive)
                    else:
                        await self.mount(archive, before=first_recent)
                else:
                    self._archive.update_blocks(archived_blocks)
                await self.remove_children(newly_archived)
            for block_id in archive_ids:
                self._widgets.pop(block_id, None)
            self._archive_ids = list(archive_ids)
        finally:
            self._compaction_pending = False

    def scroll_block_visible(self, block_id: str) -> None:
        """Reveal a mounted or archived block without rehydrating history."""

        if widget := self._widgets.get(block_id):
            widget.scroll_visible(animate=False)
            return
        if self._archive is None:
            return
        offset = self._archive.block_offset(block_id)
        if offset is None:
            return
        target = self._archive.virtual_region.y + offset
        self.scroll_to(y=max(0, target - 2), animate=False)

    def on_tool_line_toggled(self, message: ToolLineToggled) -> None:
        """Keep canonical history aligned with a tail widget's local toggle."""

        widget = self._widgets.get(message.block_id)
        if isinstance(widget, BlockWidget) and isinstance(widget.block, ToolLine):
            self._blocks[message.block_id] = widget.block

    def on_delegate_summary_toggled(self, message: DelegateSummaryToggled) -> None:
        """Keep canonical history aligned with a tail widget's local toggle."""

        widget = self._widgets.get(message.block_id)
        if isinstance(widget, BlockWidget) and isinstance(
            widget.block, DelegateSummaryBlock
        ):
            self._blocks[message.block_id] = widget.block

    # -- tail-follow anchor --------------------------------------------------

    def set_reactive(self, reactive: Reactive[ReactiveType], value: ReactiveType) -> None:
        """Clamp unvalidated scroll writes so short content stays top-aligned.

        While the standing tail anchor is engaged, Textual's compositor
        asserts ``scroll_y = content_bottom - viewport_height`` on every
        arrange via ``set_reactive`` — which bypasses ``validate_scroll_y``
        and goes *negative* whenever the transcript is shorter than the
        viewport, bottom-aligning it under blank rows. The executable spec
        (design-v3-cohesive.html: plain ``overflow-y:auto`` div whose
        ``scrollTop = scrollHeight`` the browser clamps) keeps short
        content at the top, so floor these writes at 0 here.
        """
        if reactive.name in ("scroll_y", "scroll_target_y") and isinstance(value, (int, float)):
            value = cast("ReactiveType", max(value, 0))
        super().set_reactive(reactive, value)

    @property
    def follow(self) -> bool:
        """True while the view is anchored to the bottom (anchor engaged)."""
        return self._anchored and not self._anchor_released

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.release_anchor()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.call_after_refresh(self._check_reanchor)

    def _check_reanchor(self) -> None:
        if self.is_vertical_scroll_end:
            self.anchor()

    # -- lane focus (DESIGN-SPEC §8) -----------------------------------------

    @property
    def focused_lane(self) -> str | None:
        return self._focused_lane

    async def focus_lane(self, lane_id: str, blocks: Sequence[TranscriptBlock]) -> None:
        """Swap the transcript to a subagent's own block list."""
        if self._focused_lane is None:
            self._main_stash = list(self.blocks)
        self._focused_lane = lane_id
        await self._swap(blocks)
        self.post_message(LaneFocusChanged(lane_id))

    async def restore_main(self) -> None:
        """Esc from a focused lane: restore the parent transcript."""
        if self._focused_lane is None:
            return
        stash = self._main_stash or []
        self._focused_lane = None
        self._main_stash = None
        await self._swap(stash)
        self.post_message(LaneFocusChanged(None))

    async def _swap(self, blocks: Sequence[TranscriptBlock]) -> None:
        await self.remove_children()
        self._blocks.clear()
        self._widgets.clear()
        self._order.clear()
        self._archive = None
        self._archive_ids.clear()
        self._compaction_pending = False
        block_list = list(blocks)
        self._blocks.update((block.id, block) for block in block_list)
        self._order.extend(block.id for block in block_list)
        archive_count = (
            len(block_list) - HISTORY_WIDGET_LIMIT
            if len(block_list) > HISTORY_COMPACT_TRIGGER
            else 0
        )
        mounted: list[HistoryArchive | TranscriptWidget] = []
        if archive_count:
            archive = HistoryArchive(self)
            archive.update_blocks(block_list[:archive_count])
            self._archive = archive
            self._archive_ids = [block.id for block in block_list[:archive_count]]
            mounted.append(archive)
        widgets: list[TranscriptWidget] = []
        for block in block_list[archive_count:]:
            widget = build_block_widget(block, reflow_router=self._route_reflow)
            self._widgets[block.id] = widget
            widgets.append(widget)
        mounted.extend(widgets)
        if mounted:
            await self.mount(*mounted)
        self.anchor()  # a lane swap always lands anchored at the bottom

    # -- resize reflow (75ms trailing debounce; streaming deferral) -----------

    @property
    def streaming(self) -> bool:
        return self._streaming

    def set_streaming(self, streaming: bool) -> None:
        """Mark the live tail active/idle.

        Turning streaming off releases any deferred reflow — exactly one
        forced reflow after consolidation (RESEARCH-BRIEF risk 3).
        """
        self._streaming = streaming
        if not streaming and self._reflow_deferred:
            self._flush_reflow()

    def on_resize(self, event: events.Resize) -> None:
        width = self.size.width
        if width <= 0 or width == self._last_width:
            return
        initial_layout = self._last_width is None
        self._last_width = width
        if initial_layout:
            # First layout is not a reflow: children repaint immediately
            # via their own Resize instead of waiting out the debounce.
            return
        self._reflow_hold = True
        if self._reflow_timer is not None:
            self._reflow_timer.stop()
        self._reflow_timer = self.set_timer(REFLOW_DEBOUNCE_SECONDS, self._debounce_fired)

    def _debounce_fired(self) -> None:
        self._reflow_timer = None
        if self._streaming:
            self._reflow_deferred = True
            return
        self._flush_reflow()

    def _flush_reflow(self) -> None:
        """Repaint every block at the current width (pure fn of width)."""
        self._reflow_hold = False
        self._reflow_deferred = False
        if self._archive is not None:
            self._archive.repaint_archive()
        for widget in self._widgets.values():
            widget.repaint_block()

    def _route_reflow(self, widget: BlockWidget) -> bool:
        """BlockWidget resize hook: True = deferred to the debounced flush.

        Streaming always defers (independently of Resize event ordering
        between the view and its children); otherwise a repaint is held
        only inside the view's debounce window.
        """
        if self._streaming:
            self._reflow_deferred = True
            return True
        return self._reflow_hold

    def _route_archive_reflow(self) -> bool:
        if self._streaming:
            self._reflow_deferred = True
            return True
        return self._reflow_hold


__all__ = [
    "FALLBACK_WIDTH",
    "HISTORY_COMPACT_TRIGGER",
    "HISTORY_WIDGET_LIMIT",
    "REFLOW_DEBOUNCE_SECONDS",
    "MOTION_INTERVAL_SECONDS",
    "SPINNER_INTERVAL_SECONDS",
    "BlockWidget",
    "CloseEvidence",
    "CopyCodeFence",
    "DelegateSummaryToggled",
    "ExpandEvidenceClaim",
    "HistoryArchive",
    "LaneFocusChanged",
    "NeedsYouBlockWidget",
    "OpenRewind",
    "ShowEvidence",
    "ToolLineToggled",
    "TranscriptView",
    "TranscriptWidget",
    "build_block_widget",
    "fence_text_at_row",
    "render_block",
    "render_block_markup",
]
