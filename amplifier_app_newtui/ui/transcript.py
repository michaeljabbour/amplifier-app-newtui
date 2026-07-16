"""The transcript: durable history rendered per DESIGN-SPEC §3 + §11.

Two-region model (ADR-0007): this module is the *durable history* region —
one :class:`BlockWidget` per :class:`TranscriptBlock`, mounted inside a
:class:`TranscriptView` (``VerticalScroll``). The mutable streaming region
lives in ``ui/live_tail.py`` and consolidates into an ``Answer`` block that
gets appended here.

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
- :class:`LaneFocusChanged` — the view swapped to a subagent's block list
  or back (DESIGN-SPEC §8).

Resize (DESIGN-SPEC §12, RESEARCH-BRIEF risk 3): a width change starts a
75ms trailing debounce; while a stream is painting the reflow is deferred,
and exactly one forced reflow runs when streaming ends
(``set_streaming(False)``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from rich.cells import cell_len
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Static

from ..model.blocks import (
    GLYPH_SPINNER_FRAMES,
    Answer,
    Blocked,
    BrainstormIdea,
    ContextBlock,
    DoctorBlock,
    EvidenceBlock,
    ImproveBlock,
    LedgerBlock,
    LiveCommand,
    Narration,
    NeedsYouBlock,
    PlanBlock,
    Recap,
    Segment,
    SessionBanner,
    SteerEcho,
    ToolLine,
    TranscriptBlock,
    TurnRule,
    UserLine,
    WorkingStatus,
)
from ..model.evidence import EvidenceLink
from ..model.modes import get_mode
from .segments import Line, lines_markup

REFLOW_DEBOUNCE_SECONDS = 0.075
"""Trailing debounce for resize reflow (per ADR-0007 / codex precedent)."""

SPINNER_INTERVAL_SECONDS = 0.26
"""Working-status spinner pulse period (DESIGN-SPEC §2 title spinner cadence)."""

TOOL_EXPAND_HINT = " · click to expand"
"""Exact collapsed-tool-line hint (DESIGN-SPEC §3)."""

FALLBACK_WIDTH = 80
"""Width used before first layout (corrected by the first real resize)."""

_SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹"

# Context-bar segment colors, cycled in order conversation/tools/memory;
# the "free" segment always renders ░ in dimmer.
_CONTEXT_BAR_TOKENS = ("blue", "teal", "orange", "green")


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


class ToolLineToggled(Message):
    """A tool line's body was expanded/collapsed in place."""

    def __init__(self, block_id: str, expanded: bool) -> None:
        super().__init__()
        self.block_id = block_id
        self.expanded = expanded


class LaneFocusChanged(Message):
    """The transcript swapped to a subagent lane (or back: ``lane_id=None``)."""

    def __init__(self, lane_id: str | None) -> None:
        super().__init__()
        self.lane_id = lane_id


class NeedsYouDecision(Message):
    """A needs-you block was activated → act on its first pending choice.

    (v1 limitation: the transcript-rendered needs-you block is one click
    target; per-chip hit testing lives in ``ui/needs_you.NeedsYouList``.)
    """

    def __init__(self, decision_id: str, answer: str) -> None:
        super().__init__()
        self.decision_id = decision_id
        self.answer = answer


# --------------------------------------------------------------------------
# Pure renderer: (block, width) -> lines of Segments
# --------------------------------------------------------------------------


def _split_lines(segments: Iterable[Segment]) -> tuple[Line, ...]:
    """Split segments containing newlines into per-line segment runs."""
    lines: list[list[Segment]] = [[]]
    for segment in segments:
        parts = segment.text.split("\n")
        for index, part in enumerate(parts):
            if index:
                lines.append([])
            if part:
                lines[-1].append(segment.model_copy(update={"text": part}))
    return tuple(tuple(line) for line in lines)


def _superscript(number: int) -> str:
    return "".join(_SUPERSCRIPTS[int(digit)] for digit in str(number))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _render_session_banner(block: SessionBanner, width: int) -> tuple[Line, ...]:
    if block.focus_note:
        return ((Segment(text=block.focus_note, style_token="dim"),),)
    lines: list[Line] = [
        (Segment(text=block.headline, style_token="bright", bold=True),)
    ]
    if block.detail:
        lines.append((Segment(text=block.detail, style_token="dim"),))
    return tuple(lines)


def _render_user_line(block: UserLine, width: int) -> tuple[Line, ...]:
    # get_mode falls back to chat (dim) for non-mode badges like [delegated].
    mode_token = get_mode(block.mode).color_token
    return (
        (
            Segment(text="❯ ", style_token="green", bold=True),
            Segment(text=f"[{block.mode}] ", style_token=mode_token),
            Segment(text=block.text, style_token="bright"),
        ),
    )


def _render_narration(block: Narration, width: int) -> tuple[Line, ...]:
    return (
        (
            Segment(text="● ", style_token="bright"),
            Segment(text=block.text, style_token="fg"),
        ),
    )


def _render_tool_line(block: ToolLine, width: int) -> tuple[Line, ...]:
    summary_token = "red" if block.status == "failed" else "dim"
    head: list[Segment] = [
        Segment(text="  ● ", style_token=summary_token),
        Segment(text=block.summary, style_token=summary_token),
    ]
    if block.body and not block.expanded:
        head.append(Segment(text=TOOL_EXPAND_HINT, style_token="dimmer"))
    lines: list[Line] = [tuple(head)]
    if block.expanded:
        for body_line in block.body:
            lines.append((Segment(text=f"      {body_line}", style_token="dimmer"),))
    return tuple(lines)


def _render_live_command(block: LiveCommand, width: int) -> tuple[Line, ...]:
    return (
        (
            Segment(text="  └ ", style_token="dimmer"),
            Segment(text=f"$ {block.command}", style_token="dim"),
        ),
    )


def _render_plan(block: PlanBlock, width: int) -> tuple[Line, ...]:
    header: list[Segment] = [
        Segment(text="· ", style_token="orange"),
        Segment(text=block.title, style_token="fg"),
    ]
    if block.read_only:
        header.append(Segment(text=" (read-only)", style_token="dim"))
    if block.telemetry is not None:
        header.append(Segment(text=f"  {block.telemetry.suffix()}", style_token="dim"))
    lines: list[Line] = [tuple(header)]
    for item in block.items:
        if item.state == "done":
            lines.append(
                (
                    Segment(text="  ✔ ", style_token="green"),
                    Segment(text=item.text, style_token="dim"),
                )
            )
        elif item.state == "active":
            lines.append(
                (
                    Segment(text="  ■ ", style_token="orange", bold=True),
                    Segment(text=item.text, style_token="bright", bold=True),
                )
            )
        else:
            lines.append(
                (
                    Segment(text="  □ ", style_token="dimmer"),
                    Segment(text=item.text, style_token="dim"),
                )
            )
    return tuple(lines)


def _render_blocked(block: Blocked, width: int) -> tuple[Line, ...]:
    line: list[Segment] = [
        Segment(text="  ⊘ blocked · ", style_token="red"),
        Segment(text=block.cmd, style_token="red"),
    ]
    if block.reason:
        line.append(Segment(text=f" · {block.reason}", style_token="dim"))
    if block.continuation:
        line.append(Segment(text=f" · {block.continuation}", style_token="dim"))
    return (tuple(line),)


def _render_working_status(block: WorkingStatus, width: int) -> tuple[Line, ...]:
    frame = GLYPH_SPINNER_FRAMES[block.spinner_frame % len(GLYPH_SPINNER_FRAMES)]
    inner = block.telemetry.suffix()[1:-1]  # "(8s · ↓ 3.2k tok)" -> "8s · ↓ 3.2k tok"
    working = f"working · {inner}"
    if block.agent_count:
        working += f" · {_plural(block.agent_count, 'agent')}"
    return (
        (
            Segment(text=f"{frame} ", style_token="orange"),
            Segment(text=f"{working} · ", style_token="dim"),
            Segment(
                text=f"{block.interrupt_hint} · {block.steer_hint}",
                style_token="dimmer",
            ),
        ),
    )


def _render_recap(block: Recap, width: int) -> tuple[Line, ...]:
    return (
        (
            Segment(text="✳ ", style_token="dimmer"),
            Segment(
                text=f"Goal: {block.goal}. Next: {block.next}.",
                style_token="dim",
                italic=True,
            ),
        ),
    )


def _render_answer(block: Answer, width: int) -> tuple[Line, ...]:
    return _split_lines(block.spans)


def _render_steer_echo(block: SteerEcho, width: int) -> tuple[Line, ...]:
    return (
        (
            Segment(text="  ↳ ", style_token="teal"),
            Segment(text=f'steer queued: "{block.text}" ', style_token="teal"),
            Segment(text=f"· {block.note}", style_token="dimmer"),
        ),
    )


def _render_turn_rule(block: TurnRule, width: int) -> tuple[Line, ...]:
    """Full-width 1px rule + right-aligned label; dim/dimmer by shipped."""
    label_token = "dim" if block.shipped else "dimmer"
    label = block.label
    label_width = cell_len(label)
    if width >= label_width + 4:
        fill = width - label_width - 1
        return (
            (
                Segment(text="─" * fill, style_token="rule"),
                Segment(text=" ", style_token="rule"),
                Segment(text=label, style_token=label_token),
            ),
        )
    # Too narrow to share a line: full rule, then the label right-aligned.
    pad = max(0, width - label_width)
    return (
        (Segment(text="─" * max(1, width), style_token="rule"),),
        (
            Segment(text=" " * pad, style_token="rule"),
            Segment(text=label, style_token=label_token),
        ),
    )


def _render_evidence(block: EvidenceBlock, width: int) -> tuple[Line, ...]:
    total = len(block.links)
    lines: list[Line] = [
        (
            Segment(text="· ", style_token="teal"),
            Segment(text="Evidence", style_token="teal", bold=True),
            Segment(text=f"  {block.selected + 1}/{total}", style_token="fg"),
            Segment(
                text=" · ←/→ select · enter expand · esc close",
                style_token="dimmer",
            ),
        )
    ]
    for index, link in enumerate(block.links):
        bg = "bg-tab" if index == block.selected else None
        lines.append(
            (
                Segment(text=f"  {_superscript(index + 1)} ", style_token="teal", bg_token=bg),
                Segment(text=f'"{link.claim_quote}"', style_token="fg", bg_token=bg),
                Segment(text=" → ", style_token="dim", bg_token=bg),
                Segment(text=link.tool_ref, style_token="dim", bg_token=bg),
            )
        )
    return tuple(lines)


def _render_ledger(block: LedgerBlock, width: int) -> tuple[Line, ...]:
    return (
        (
            Segment(text="· ", style_token="blue"),
            Segment(text="Session ledger", style_token="bright", bold=True),
            Segment(text=f"  {block.session} · {block.bundle}", style_token="dim"),
        ),
        (
            Segment(
                text=(
                    f"  {block.turns} turns · ${block.spend:.2f} · "
                    f"{block.shipped} shipped · {block.answer_only} answer-only · "
                    f"cache hit {block.cache_hit_pct}%"
                ),
                style_token="fg",
            ),
        ),
    )


def _render_context(block: ContextBlock, width: int) -> tuple[Line, ...]:
    lines: list[Line] = [
        (
            Segment(text="· ", style_token="blue"),
            Segment(text="Context", style_token="bright", bold=True),
            Segment(
                text=f"  {block.used_pct}% of {block.window_label}",
                style_token="dim",
            ),
        )
    ]
    if block.segments:
        bar: list[Segment] = [Segment(text="  ", style_token="dimmer")]
        color_index = 0
        for label, cells in block.segments:
            if cells <= 0:
                continue
            # Labels carry the legend value ("free 116k"); the first word
            # is the bucket name — free renders ░, used buckets █.
            if label.split(" ", 1)[0] == "free":
                bar.append(Segment(text="░" * cells, style_token="dimmer"))
            else:
                token = _CONTEXT_BAR_TOKENS[color_index % len(_CONTEXT_BAR_TOKENS)]
                color_index += 1
                bar.append(Segment(text="█" * cells, style_token=token))  # type: ignore[arg-type]
        lines.append(tuple(bar))
        legend = " · ".join(label for label, _cells in block.segments)
        lines.append((Segment(text=f"  {legend}", style_token="dim"),))
    return tuple(lines)


def _render_needs_you(block: NeedsYouBlock, width: int) -> tuple[Line, ...]:
    count = len(block.items)
    lines: list[Line] = [
        (
            Segment(text="· ", style_token="orange"),
            Segment(text="Needs you", style_token="orange", bold=True),
            Segment(
                text=f"  {_plural(count, 'deferred decision')}", style_token="fg"
            ),
        )
    ]
    for index, entry in enumerate(block.items, start=1):
        row: list[Segment] = [
            Segment(text=f"  {index}. ", style_token="orange"),
            Segment(text=entry.question, style_token="fg"),
        ]
        if entry.reason:
            row.append(Segment(text=f" · {entry.reason}", style_token="dim"))
        for choice in entry.choices:
            row.append(Segment(text=" ", style_token="fg"))
            row.append(
                Segment(
                    text=f"[{choice.label}]",
                    style_token="green",
                    bg_token="bg-tab",
                )
            )
        lines.append(tuple(row))
    return tuple(lines)


def _render_doctor(block: DoctorBlock, width: int) -> tuple[Line, ...]:
    lines: list[Line] = []
    for healthy in block.healthy:
        lines.append(
            (
                Segment(text="  ✔ ", style_token="green"),
                Segment(text=healthy, style_token="dim"),
            )
        )
    for finding in block.findings:
        lines.append(
            (
                Segment(text=f"  {finding.number}. ", style_token="orange"),
                Segment(text=finding.text, style_token="orange"),
            )
        )
    return tuple(lines)


def _render_improve(block: ImproveBlock, width: int) -> tuple[Line, ...]:
    lines: list[Line] = [
        (
            Segment(text="· ", style_token="blue"),
            Segment(text="Improve", style_token="bright", bold=True),
            Segment(
                text=f"  {_plural(len(block.proposals), 'proposal')}",
                style_token="dim",
            ),
        )
    ]
    for index, proposal in enumerate(block.proposals, start=1):
        lines.append(
            (
                Segment(text=f"  {index}. ", style_token="teal"),
                Segment(text=proposal.title, style_token="fg"),
                Segment(text=f" · {proposal.rationale}", style_token="dim"),
            )
        )
    return tuple(lines)


def _render_brainstorm_idea(block: BrainstormIdea, width: int) -> tuple[Line, ...]:
    marker = f"  {block.number}. " if block.number > 0 else "  · "
    return (
        (
            Segment(text=marker, style_token="teal"),
            Segment(text=block.text, style_token="fg"),
        ),
    )


_RENDERERS: dict[str, Callable[..., tuple[Line, ...]]] = {
    "session_banner": _render_session_banner,
    "user_line": _render_user_line,
    "narration": _render_narration,
    "tool_line": _render_tool_line,
    "live_command": _render_live_command,
    "plan": _render_plan,
    "blocked": _render_blocked,
    "working_status": _render_working_status,
    "recap": _render_recap,
    "answer": _render_answer,
    "steer_echo": _render_steer_echo,
    "turn_rule": _render_turn_rule,
    "evidence": _render_evidence,
    "ledger": _render_ledger,
    "context": _render_context,
    "needs_you": _render_needs_you,
    "doctor": _render_doctor,
    "improve": _render_improve,
    "brainstorm_idea": _render_brainstorm_idea,
}


def render_block(block: TranscriptBlock, width: int) -> tuple[Line, ...]:
    """Render one block to lines of Segments — a pure function of (block, width).

    Every block kind in the union is supported; unknown kinds fail loudly.
    """
    renderer = _RENDERERS.get(block.kind)
    if renderer is None:  # pragma: no cover - union is exhaustive
        raise TypeError(f"unsupported transcript block kind: {block.kind!r}")
    return renderer(block, width)


def render_block_markup(block: TranscriptBlock, width: int) -> str:
    """Markup form of :func:`render_block` (styles = ``$token`` variables)."""
    return lines_markup(render_block(block, width))


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class BlockWidget(Static):
    """One transcript block as a widget (ADR-0007 open-q 6: widget-per-block).

    State is the block itself; the widget re-derives its content from
    ``render_block(block, width)`` on every repaint. In-place mutation
    (tool expand/collapse, live plan updates, per-second working text)
    happens via :meth:`update_block` keyed by the block's stable id.
    """

    DEFAULT_CSS = """
    BlockWidget {
        height: auto;
    }
    BlockWidget.kind-user-line {
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("enter", "activate", "activate", show=False)]

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
        self.add_class(f"kind-{block.kind.replace('_', '-')}")
        if block.kind == "tool_line":
            self.can_focus = True

    @property
    def block(self) -> TranscriptBlock:
        return self._block

    def on_mount(self) -> None:
        self.repaint_block()
        if self._block.kind == "working_status":
            self._spin_timer = self.set_interval(
                SPINNER_INTERVAL_SECONDS, self._advance_spinner
            )

    def on_unmount(self) -> None:
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None

    def _advance_spinner(self) -> None:
        """Pulse ✳/✦/✧ between per-second block replacements."""
        self._spinner_offset += 1
        self.repaint_block()

    def update_block(self, block: TranscriptBlock) -> None:
        """Replace this widget's block in place (same stable id)."""
        if block.id != self._block.id:
            raise ValueError(
                f"block id mismatch: widget has {self._block.id!r}, got {block.id!r}"
            )
        self._block = block
        self.repaint_block()

    def repaint_block(self) -> None:
        """Re-derive content from (block, current width)."""
        width = self.size.width or FALLBACK_WIDTH
        self._painted_width = width
        block = self._block
        if block.kind == "working_status" and self._spinner_offset:
            block = block.model_copy(
                update={"spinner_frame": block.spinner_frame + self._spinner_offset}
            )
        self.update(render_block_markup(block, width))

    def on_resize(self, event: events.Resize) -> None:
        width = self.size.width
        if width <= 0 or width == self._painted_width:
            return
        if self._reflow_router is not None and self._reflow_router(self):
            return  # deferred: the TranscriptView owns the debounce
        self.repaint_block()

    def on_click(self, event: events.Click) -> None:
        self._activate()

    def action_activate(self) -> None:
        self._activate()

    def _activate(self) -> None:
        block = self._block
        if block.kind == "tool_line" and block.body:
            toggled = block.model_copy(update={"expanded": not block.expanded})
            self._block = toggled
            self.repaint_block()
            self.post_message(ToolLineToggled(toggled.id, toggled.expanded))
        elif block.kind == "answer":
            self.post_message(ShowEvidence(block.id, block.evidence_refs))
        elif block.kind == "turn_rule":
            self.post_message(OpenRewind(block.checkpoint_id))
        elif block.kind == "needs_you" and block.items:
            entry = block.items[0]
            if entry.choices:
                self.post_message(NeedsYouDecision(entry.decision_id, entry.choices[0].answer))


class TranscriptView(VerticalScroll):
    """Scrollable durable-history region: one BlockWidget per block.

    - **Tail-follow anchor**: sticks to the bottom on append unless the
      user scrolled up; scrolling back to the bottom re-arms following.
    - **Keyed mutation**: :meth:`append` / :meth:`replace` /
      :meth:`remove_block` address blocks by stable id.
    - **Lane focus** (spec §8): :meth:`focus_lane` swaps the visible block
      list to a subagent's transcript; :meth:`restore_main` (the app's esc
      handler) swaps back. While focused, append/replace address the
      *visible* (subagent) list.
    - **Resize reflow**: 75ms trailing debounce; deferred during streaming
      with one forced reflow at :meth:`set_streaming` (False).
    """

    DEFAULT_CSS = """
    TranscriptView {
        scrollbar-size-vertical: 1;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._widgets: dict[str, BlockWidget] = {}
        self._order: list[str] = []
        self._follow = True
        self._focused_lane: str | None = None
        self._main_stash: list[TranscriptBlock] | None = None
        self._streaming = False
        self._reflow_hold = False
        self._reflow_deferred = False
        self._reflow_timer: Timer | None = None
        self._last_width: int | None = None

    # -- block CRUD --------------------------------------------------------

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    @property
    def blocks(self) -> tuple[TranscriptBlock, ...]:
        return tuple(self._widgets[block_id].block for block_id in self._order)

    def get_block(self, block_id: str) -> TranscriptBlock | None:
        widget = self._widgets.get(block_id)
        return widget.block if widget is not None else None

    def append(self, block: TranscriptBlock) -> BlockWidget:
        """Mount a new block at the end (follows the tail when anchored)."""
        if block.id in self._widgets:
            raise ValueError(f"duplicate block id: {block.id!r}")
        widget = BlockWidget(block, reflow_router=self._route_reflow)
        self._widgets[block.id] = widget
        self._order.append(block.id)
        self.mount(widget)
        if self._follow:
            self.call_after_refresh(self.scroll_end, animate=False)
        return widget

    def replace(self, block: TranscriptBlock) -> None:
        """Swap a block's content in place, keyed by its stable id."""
        widget = self._widgets.get(block.id)
        if widget is None:
            raise KeyError(f"unknown block id: {block.id!r}")
        widget.update_block(block)

    def remove_block(self, block_id: str) -> None:
        """Unmount a block (e.g. the working status line at turn end)."""
        widget = self._widgets.pop(block_id, None)
        if widget is None:
            raise KeyError(f"unknown block id: {block_id!r}")
        self._order.remove(block_id)
        widget.remove()

    # -- tail-follow anchor --------------------------------------------------

    @property
    def follow(self) -> bool:
        """True while the view is anchored to the bottom."""
        return self._follow

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._follow = False

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.call_after_refresh(self._check_reanchor)

    def _check_reanchor(self) -> None:
        if self.is_vertical_scroll_end:
            self._follow = True

    # -- lane focus (DESIGN-SPEC §8) -----------------------------------------

    @property
    def focused_lane(self) -> str | None:
        return self._focused_lane

    async def focus_lane(
        self, lane_id: str, blocks: Sequence[TranscriptBlock]
    ) -> None:
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
        self._widgets.clear()
        self._order.clear()
        widgets: list[BlockWidget] = []
        for block in blocks:
            widget = BlockWidget(block, reflow_router=self._route_reflow)
            self._widgets[block.id] = widget
            self._order.append(block.id)
            widgets.append(widget)
        if widgets:
            await self.mount(*widgets)
        self._follow = True
        self.call_after_refresh(self.scroll_end, animate=False)

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
        self._reflow_timer = self.set_timer(
            REFLOW_DEBOUNCE_SECONDS, self._debounce_fired
        )

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
        for block_id in self._order:
            self._widgets[block_id].repaint_block()

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


__all__ = [
    "FALLBACK_WIDTH",
    "REFLOW_DEBOUNCE_SECONDS",
    "SPINNER_INTERVAL_SECONDS",
    "TOOL_EXPAND_HINT",
    "BlockWidget",
    "LaneFocusChanged",
    "NeedsYouDecision",
    "OpenRewind",
    "ShowEvidence",
    "ToolLineToggled",
    "TranscriptView",
    "render_block",
    "render_block_markup",
]
