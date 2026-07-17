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
from time import monotonic
from typing import cast

from rich.cells import cell_len
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import Reactive, ReactiveType
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
    NeedsYouEntry,
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
from .keymap import KEYMAP
from .needs_you import NeedsYouList
from .segments import Line, lines_markup

REFLOW_DEBOUNCE_SECONDS = 0.075
"""Trailing debounce for resize reflow (per ADR-0007 / codex precedent)."""

SPINNER_INTERVAL_SECONDS = 1.0
"""Working-line glyph cadence: the mockup advances ✳/✦/✧/✦ inside the
1000ms telemetry tick (design-v3-cohesive.html runTurn, ``secs % 4``) —
the faster 260ms spinTimer is the §2 TITLE-bar spinner only."""

TOOL_EXPAND_HINT = " · click to expand"
"""Exact collapsed-tool-line hint (DESIGN-SPEC §3)."""

FALLBACK_WIDTH = 80
"""Width used before first layout (corrected by the first real resize)."""

_SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹"

IMPROVE_HEADER = "from ledger + denial log · proposes, never applies silently"
"""Exact ``/improve`` header suffix (mockup cmdImprove, verbatim)."""


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


def _render_session_banner(block: SessionBanner, width: int) -> tuple[Line, ...]:
    if block.focus_note:
        # Mockup focusLane: 'focused: <name> ' bright bold + '· subagent of
        # …' dim — split at the first '·' of the joined banner string.
        head, sep, tail = block.focus_note.partition("·")
        if sep:
            return (
                (
                    Segment(text=head, style_token="bright", bold=True),
                    Segment(text=f"{sep}{tail}", style_token="dim"),
                ),
            )
        return ((Segment(text=block.focus_note, style_token="dim"),),)
    lines: list[Line] = [
        (Segment(text=block.headline, style_token="bright", bold=True),)
    ]
    if block.detail:
        lines.append((Segment(text=block.detail, style_token="dim"),))
    return tuple(lines)


def _render_user_line(block: UserLine, width: int) -> tuple[Line, ...]:
    # '[delegated]' (focused-subagent brief) is teal per the mockup;
    # any other non-mode badge falls back to the chat profile (dim).
    mode_token = "teal" if block.mode == "delegated" else get_mode(block.mode).color_token
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
    # The mockup never mutates the head on toggle: the hint stays visible
    # while the body is expanded.
    if block.body:
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
        header.append(Segment(text=f" {block.telemetry.suffix()}", style_token="dim"))
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
            # Mockup L331: the '  ■ ' prefix is plain orange (weight 400);
            # only the step text is bright bold.
            lines.append(
                (
                    Segment(text="  ■ ", style_token="orange"),
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
    if block.agent_count > 1:
        # Fan-out turn (mockup runAgentsTurn): 'Coordinating N agents · …'
        # dim + 'esc to interrupt' dimmer — no 'working ·', no steer hint.
        return (
            (
                Segment(text=f"{frame} ", style_token="orange"),
                Segment(
                    text=f"Coordinating {block.agent_count} agents · {inner} · ",
                    style_token="dim",
                ),
                Segment(text=block.interrupt_hint, style_token="dimmer"),
            ),
        )
    # Single-agent pulse: the live activity tree beneath carries the ops
    # (spec §3). Before any tool runs, fall back to the inline note
    # (``thinking``) so the supervisor still sees the turn breathing.
    pulse: list[Segment] = [Segment(text=f"{frame} ", style_token="orange")]
    if block.activity_lines:
        pulse.append(Segment(text=f"working · {inner} · ", style_token="dim"))
    else:
        note = block.activity or "1 agent"
        pulse.append(Segment(text=f"working · {inner} · {note} · ", style_token="dim"))
    pulse.append(
        Segment(
            text=f"{block.interrupt_hint} · {block.steer_hint}",
            style_token="dimmer",
        )
    )
    lines: list[Line] = [tuple(pulse)]
    last = len(block.activity_lines) - 1
    for i, branch in enumerate(block.activity_lines):
        glyph = "  └ " if i == last else "  ├ "
        text_token = "dim" if branch.running else "dimmer"
        lines.append(
            (
                Segment(text=glyph, style_token="dimmer"),
                Segment(text=branch.text, style_token=text_token),
            )
        )
    return tuple(lines)


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
            Segment(
                text=(
                    f"  {block.selected + 1}/{total}"
                    " · ←/→ select · enter expand · esc close"
                ),
                style_token="dimmer",
            ),
        )
    ]
    for index, link in enumerate(block.links):
        lines.append(
            (
                Segment(text=f"  {_superscript(index + 1)} ", style_token="teal"),
                Segment(text=f'"{link.claim_quote}"', style_token="fg"),
                Segment(text=" → ", style_token="dim"),
                Segment(text=link.tool_ref, style_token="dim"),
            )
        )
    return tuple(lines)


def _render_ledger(block: LedgerBlock, width: int) -> tuple[Line, ...]:
    return (
        (
            Segment(text="· ", style_token="blue"),
            Segment(
                text=f"Session ledger  {block.session} · {block.bundle}",
                style_token="fg",
            ),
        ),
        (
            Segment(
                text=(
                    f"  {block.turns} turns · ${block.spend:.2f} · "
                    f"{block.shipped} shipped · {block.answer_only} answer-only · "
                    f"cache hit {block.cache_hit_pct}%"
                ),
                style_token="dim",
            ),
        ),
    )


def _render_context(block: ContextBlock, width: int) -> tuple[Line, ...]:
    lines: list[Line] = [
        (
            Segment(text="· ", style_token="blue"),
            Segment(
                text=f"Context  {block.used_pct}% of {block.window_label}",
                style_token="fg",
            ),
        )
    ]
    if block.segments:
        # Mockup cmdContext: ONE dim line — '  ████████░░░░  <legend>'.
        # Labels carry the legend value ("free 116k"); the first word is
        # the bucket name — free renders ░, used buckets █.
        bar = "".join(
            ("░" if label.split(" ", 1)[0] == "free" else "█") * cells
            for label, cells in block.segments
            if cells > 0
        )
        legend = " · ".join(label for label, _cells in block.segments)
        lines.append((Segment(text=f"  {bar}  {legend}", style_token="dim"),))
    return tuple(lines)


def _needs_you_question_segments(entry: NeedsYouEntry) -> list[Segment]:
    """The fg question text, with the entry's highlight run in teal
    (mockup: 'Push to fork ' fg + 'mj/waypoint' teal + ' instead?' fg)."""
    question = entry.question
    if entry.highlight and entry.highlight in question:
        before, _, after = question.partition(entry.highlight)
        segments: list[Segment] = []
        if before:
            segments.append(Segment(text=before, style_token="fg"))
        segments.append(Segment(text=entry.highlight, style_token="teal"))
        if after:
            segments.append(Segment(text=after, style_token="fg"))
        return segments
    return [Segment(text=question, style_token="fg")]


def _render_needs_you(block: NeedsYouBlock, width: int) -> tuple[Line, ...]:
    # Header is ONE plain orange run, count never pluralized (mockup
    # showNeedsYou: 'Needs you  N deferred decision').
    count = len(block.items)
    lines: list[Line] = [
        (
            Segment(text="· ", style_token="orange"),
            Segment(text=f"Needs you  {count} deferred decision", style_token="orange"),
        )
    ]
    for index, entry in enumerate(block.items, start=1):
        row: list[Segment] = [Segment(text=f"  {index} ", style_token="orange")]
        row.extend(_needs_you_question_segments(entry))
        if entry.reason:
            row.append(Segment(text=f" · {entry.reason}", style_token="dim"))
        for choice in entry.choices:
            row.append(Segment(text="  ", style_token="fg"))
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
    if block.headline:
        lines.append(
            (
                Segment(text="· ", style_token="blue"),
                Segment(text=f"Doctor  {block.headline}", style_token="fg"),
            )
        )
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
                Segment(text=f"  {finding.number} ", style_token="orange"),
                Segment(text=finding.text, style_token="dim"),
            )
        )
    return tuple(lines)


def _render_improve(block: ImproveBlock, width: int) -> tuple[Line, ...]:
    lines: list[Line] = [
        (
            Segment(text="· ", style_token="blue"),
            Segment(text=f"Improve  {IMPROVE_HEADER}", style_token="fg"),
        )
    ]
    if not block.proposals:
        lines.append(
            (
                Segment(
                    text=(
                        "  no proposals yet · repeated approvals and overridden"
                        " denials become evidence here"
                    ),
                    style_token="dimmer",
                ),
            )
        )
    for index, proposal in enumerate(block.proposals, start=1):
        if proposal.action:
            # 'allowlist:' rows name the action once, in green (mockup
            # cmdImprove: dim '  1 allowlist: ' + green action + dim tail).
            lines.append(
                (
                    Segment(text=f"  {index} {proposal.title} ", style_token="dim"),
                    Segment(text=proposal.action, style_token="green"),
                    Segment(text=f" {proposal.rationale}", style_token="dim"),
                )
            )
        else:
            lines.append(
                (
                    Segment(
                        text=f"  {index} {proposal.title} {proposal.rationale}",
                        style_token="dim",
                    ),
                )
            )
    return tuple(lines)


def _render_brainstorm_idea(block: BrainstormIdea, width: int) -> tuple[Line, ...]:
    # Mockup brainstorm ideas are single fg runs: '  1 Ambient tab color: …'
    # (number + space, no period, no accent color).
    prefix = f"  {block.number} " if block.number > 0 else "  "
    return ((Segment(text=f"{prefix}{block.text}", style_token="fg"),),)


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
        if block.kind in ("tool_line", "evidence"):
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
            self._spin_timer = self.set_interval(
                SPINNER_INTERVAL_SECONDS, self._advance_spinner
            )

    def on_unmount(self) -> None:
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None

    def _advance_spinner(self) -> None:
        """Pulse ✳/✦/✧ (and tick wall-clock secs) between event replaces."""
        self._spinner_offset += 1
        self.repaint_block()

    def update_block(self, block: TranscriptBlock) -> None:
        """Replace this widget's block in place (same stable id)."""
        if block.id != self._block.id:
            raise ValueError(
                f"block id mismatch: widget has {self._block.id!r}, got {block.id!r}"
            )
        self._block = block
        if isinstance(block, PlanBlock):
            self.set_class(block.read_only, "read-only")
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
        self._activate()

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
                f"block id mismatch: widget has {self._needs_you_block.id!r},"
                f" got {block.id!r}"
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


class TranscriptView(VerticalScroll):
    """Scrollable durable-history region: one BlockWidget per block.

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
        self._widgets: dict[str, TranscriptWidget] = {}
        self._order: list[str] = []
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
        return tuple(self._widgets[block_id].block for block_id in self._order)

    def get_block(self, block_id: str) -> TranscriptBlock | None:
        widget = self._widgets.get(block_id)
        return widget.block if widget is not None else None

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
        if block.id in self._widgets:
            raise ValueError(f"duplicate block id: {block.id!r}")
        widget = build_block_widget(block, reflow_router=self._route_reflow)
        self._widgets[block.id] = widget
        self._order.append(block.id)
        # No one-shot scroll here: while the tail anchor is engaged the
        # compositor keeps the view at the bottom through this mount AND
        # any later height growth of the mounted widget (async child rows,
        # wrap reflow); while released (user scrolled up) it must not move.
        self.mount(widget)
        return widget

    def replace(self, block: TranscriptBlock) -> None:
        """Swap a block's content in place, keyed by its stable id.

        While a lane is focused the replace addresses the stashed parent
        list — the focused child transcript is a read-only snapshot.
        """
        if self._focused_lane is not None and self._main_stash is not None:
            for index, stashed in enumerate(self._main_stash):
                if stashed.id == block.id:
                    self._main_stash[index] = block
                    return
            raise KeyError(f"unknown block id: {block.id!r}")
        widget = self._widgets.get(block.id)
        if widget is None:
            raise KeyError(f"unknown block id: {block.id!r}")
        widget.update_block(block)

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
        widget = self._widgets.pop(block_id, None)
        if widget is None:
            raise KeyError(f"unknown block id: {block_id!r}")
        self._order.remove(block_id)
        widget.remove()

    # -- tail-follow anchor --------------------------------------------------

    def set_reactive(
        self, reactive: Reactive[ReactiveType], value: ReactiveType
    ) -> None:
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
        if reactive.name in ("scroll_y", "scroll_target_y") and isinstance(
            value, (int, float)
        ):
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
        widgets: list[TranscriptWidget] = []
        for block in blocks:
            widget = build_block_widget(block, reflow_router=self._route_reflow)
            self._widgets[block.id] = widget
            self._order.append(block.id)
            widgets.append(widget)
        if widgets:
            await self.mount(*widgets)
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
    "CloseEvidence",
    "ExpandEvidenceClaim",
    "LaneFocusChanged",
    "NeedsYouBlockWidget",
    "OpenRewind",
    "ShowEvidence",
    "ToolLineToggled",
    "TranscriptView",
    "TranscriptWidget",
    "build_block_widget",
    "render_block",
    "render_block_markup",
]
