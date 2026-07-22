"""Pure transcript renderers: ``(block, width)`` → lines of Segments.

Lifted out of :mod:`ui.transcript` (issue #33): the 21 pure ``_render_*``
block→markup transforms and the ``_RENDERERS`` dispatch table have no widget
state, so they live here as plain functions unit-tested by the golden width
matrix (ADR-0007: pure renderers are golden-tested). The widget layer in
:mod:`ui.transcript` imports :func:`render_block` / :func:`render_block_markup`
from this module; nothing here touches Textual widgets.

Rendering emits :class:`Segment` runs — exact spec glyphs and strings, no
Textual objects — so every visual detail is testable as plain text. Styles
are theme-variable references (``$dim`` …), never colors.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence

from rich.cells import cell_len

from ..model.blocks import (
    GLYPH_BLOCKED,
    GLYPH_CHEVRON_COLLAPSED,
    GLYPH_CHEVRON_EXPANDED,
    GLYPH_ERROR,
    GLYPH_LANE_RUNNING,
    GLYPH_PLAN_DONE,
    GLYPH_QUOTE_GUTTER,
    GLYPH_SPINNER_FRAMES,
    Answer,
    Blocked,
    BrainstormIdea,
    ContextBlock,
    DelegateSummaryBlock,
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
    StyleToken,
    ToolLine,
    TranscriptBlock,
    TurnRule,
    UserLine,
    WorkingStatus,
)
from ..model.modes import get_mode
from .motion import shimmer_band
from .segments import Line, lines_markup

TOOL_EXPAND_HINT = " · click to expand"
"""Exact collapsed-tool-line hint (DESIGN-SPEC §3)."""

_SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹"

IMPROVE_HEADER = "from ledger + denial log · proposes, never applies silently"
"""Exact ``/improve`` header suffix (mockup cmdImprove, verbatim)."""


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
    lines: list[Line] = [(Segment(text=block.headline, style_token="bright", bold=True),)]
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
            token = "dimmer"
            background = None
            bold = False
            if block.body_style == "diff":
                if body_line.startswith("@@"):
                    token, bold = "blue", True
                elif body_line.startswith(("--- ", "+++ ")):
                    token = "teal"
                elif body_line.startswith("+"):
                    token, background = "green", "bg-tab"
                elif body_line.startswith("-"):
                    token, background = "red", "bg-tab"
                elif " · " in body_line:
                    token = "dim"
            lines.append(
                (
                    Segment(
                        text=f"      {body_line}",
                        style_token=token,
                        bold=bold,
                        bg_token=background,
                    ),
                )
            )
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


def _shimmer_segments(label: str, frame: int) -> tuple[Segment, ...]:
    """A soft five-cell highlight band that travels across *label*.

    The quiet gap keeps this from reading like a marquee; plain text never
    changes, so copy/paste and snapshots remain stable.
    """
    band: dict[int, tuple[StyleToken, bool]] = {
        index: (token, bold) for index, token, bold in shimmer_band(len(label), frame)
    }
    base_style: tuple[StyleToken, bool] = ("dim", False)
    segments: list[Segment] = []
    for index, character in enumerate(label):
        token, bold = band.get(index, base_style)
        if segments and segments[-1].style_token == token and segments[-1].bold == bold:
            previous = segments[-1]
            segments[-1] = previous.model_copy(update={"text": previous.text + character})
        else:
            segments.append(Segment(text=character, style_token=token, bold=bold))
    return tuple(segments)


def _render_working_status(block: WorkingStatus, width: int) -> tuple[Line, ...]:
    frame = GLYPH_SPINNER_FRAMES[block.spinner_frame % len(GLYPH_SPINNER_FRAMES)]
    inner = block.telemetry.suffix()[1:-1]  # "(8s · ↓ 3.2k tok)" -> "8s · ↓ 3.2k tok"
    if block.agent_count > 1:
        # Fan-out turn (mockup runAgentsTurn): 'Coordinating N agents · …'
        # dim + 'esc to interrupt' dimmer — no 'working ·', no steer hint.
        label = f"Coordinating {block.agent_count} agents"
        return (
            (
                Segment(text=f"{frame} ", style_token="orange"),
                *_shimmer_segments(label, block.motion_frame),
                Segment(text=f" · {inner} · ", style_token="dim"),
                Segment(text=block.interrupt_hint, style_token="dimmer"),
            ),
        )
    # Single-agent pulse: the live activity tree beneath carries the ops
    # (spec §3). Before any tool runs, fall back to the inline note
    # (``thinking``) so the supervisor still sees the turn breathing.
    pulse: list[Segment] = [Segment(text=f"{frame} ", style_token="orange")]
    pulse.extend(_shimmer_segments("working", block.motion_frame))
    if block.activity_lines:
        pulse.append(Segment(text=f" · {inner} · ", style_token="dim"))
    else:
        note = block.activity or "1 agent"
        pulse.append(Segment(text=f" · {inner} · {note} · ", style_token="dim"))
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


_ANSWER_MARKER_RE = re.compile(r"^\s*(?:•|\d+[.)])\s+$")
"""A list marker segment (``• `` / ``1. `` / indented) at the head of a
logical answer line — its cell width becomes the hanging indent."""


def _answer_marker_hang(first: Segment) -> int:
    """Cell width of a leading list marker or blockquote gutter, or 0 if
    the line is neither (continuation lines wrap under the body, not the
    marker)."""
    if first.style_token == "dim" and _ANSWER_MARKER_RE.match(first.text):
        return cell_len(first.text)
    if first.style_token == "blue" and first.text == GLYPH_QUOTE_GUTTER:
        return cell_len(GLYPH_QUOTE_GUTTER)
    return 0


def _answer_line_is_verbatim(line: Line) -> bool:
    """Lines the wrapper must not touch: fenced code (teal, 2-space indent)
    and table rows/rules (grid separators destroy alignment if re-wrapped)."""
    first = line[0]
    if first.style_token == "teal" and first.text.startswith("  "):
        return True
    return any("│" in seg.text or "┼" in seg.text for seg in line)


def _coalesce(segs: Sequence[Segment]) -> Line:
    """Merge adjacent segments that share a style so wrapped lines emit one
    run per style rather than one per word (readable markup, small goldens)."""
    merged: list[Segment] = []
    for seg in segs:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.style_token == seg.style_token
            and prev.bold == seg.bold
            and prev.italic == seg.italic
            and prev.bg_token == seg.bg_token
        ):
            merged[-1] = prev.model_copy(update={"text": prev.text + seg.text})
        else:
            merged.append(seg)
    return tuple(merged)


def _wrap_line(segs: Sequence[Segment], width: int, hang: int) -> tuple[Line, ...]:
    """Greedy word-wrap a run of styled segments to *width* cells.

    Continuation lines are left-padded by *hang* spaces so list-item bodies
    stay flush under their first word (hanging indent). Styles are preserved
    per token; a single word wider than *width* sits alone rather than looping.
    """
    if width <= 0 or sum(cell_len(seg.text) for seg in segs) <= width:
        return (tuple(segs),)  # fits as-is — keep the original segment runs
    pad = " " * hang
    lines: list[list[Segment]] = [[]]
    widths: list[int] = [0]
    pending: Segment | None = None  # a whitespace run awaiting its next word

    def baseline() -> int:
        return hang if len(lines) > 1 else 0

    def wrap() -> None:
        lines.append([Segment(text=pad)] if hang else [])
        widths.append(hang)

    for seg in segs:
        for token in re.split(r"(\s+)", seg.text):
            if not token:
                continue
            if token.isspace():
                if widths[-1] > baseline():
                    pending = seg.model_copy(update={"text": token})
                continue  # drop leading whitespace on a fresh line
            tok_w = cell_len(token)
            space_w = cell_len(pending.text) if pending is not None else 0
            if widths[-1] > baseline() and widths[-1] + space_w + tok_w > width:
                wrap()  # word does not fit — start a continuation line
                pending = None
                space_w = 0
            if pending is not None:
                lines[-1].append(pending)
                widths[-1] += space_w
                pending = None
            lines[-1].append(seg.model_copy(update={"text": token}))
            widths[-1] += tok_w
    return tuple(_coalesce(line) for line in lines)


def _render_answer(block: Answer, width: int) -> tuple[Line, ...]:
    """Long answers read like a document: word-wrapped to the full transcript
    width (no reading-column cap) with hanging indents on list continuations.

    Code and table lines pass through verbatim; every other logical line is
    greedy-wrapped, list items keeping their body aligned under the marker.
    """
    out: list[Line] = []
    for line in _split_lines(block.spans):
        if not line:
            # Inter-block spacing: one blank line max — the block sentinel
            # and a source blank line must not stack into a double gap.
            if out and not out[-1]:
                continue
            out.append(line)
            continue
        if _answer_line_is_verbatim(line):
            out.append(line)
            continue
        out.extend(_wrap_line(line, width, _answer_marker_hang(line[0])))
    # Drop a leading/trailing blank the collapsing may leave.
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return tuple(out)


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
                text=(f"  {block.selected + 1}/{total} · ←/→ select · enter expand · esc close"),
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


def _format_span(seconds: float) -> str:
    """``42s`` under a minute, ``1m 42s`` above (lane-panel zero-pad style)."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


def _clip(text: str, budget: int) -> str:
    """Cell-width truncation with a trailing ellipsis; '' when it can't fit."""
    if budget <= 1:
        return ""
    if cell_len(text) <= budget:
        return text
    out = ""
    for ch in text:
        if cell_len(out + ch) > budget - 1:
            break
        out += ch
    return out + "…"


_DELEGATE_GLYPHS: dict[str, tuple[str, StyleToken]] = {
    "running": (GLYPH_LANE_RUNNING, "dimmer"),
    "done": (GLYPH_PLAN_DONE, "green"),
    "error": (GLYPH_ERROR, "red"),
    "cancelled": (GLYPH_BLOCKED, "red"),
}

# Reuse the plan-panel checklist glyphs (ui/plan_panel.py:_GLYPHS) — goldens pin both.
_DELEGATE_PLAN_GLYPHS: dict[str, tuple[str, StyleToken]] = {
    "completed": ("✔", "green"),
    "in_progress": ("▶", "orange"),
    "pending": ("○", "dim"),
}


def _render_delegate_summary(block: DelegateSummaryBlock, width: int) -> tuple[Line, ...]:
    """Ambient-progress D5: one-line summary, expandable to the agent tree."""
    running = sum(1 for entry in block.entries if entry.state == "running")
    head: list[Segment] = [Segment(text="● ", style_token="bright")]
    if running:
        noun = "delegate" if running == 1 else "delegates"
        head.append(Segment(text=f"{running} {noun} running…", style_token="dim"))
    else:
        total = len(block.entries)
        noun = "delegate" if total == 1 else "delegates"
        head.append(Segment(text=f"Used {total} {noun}", style_token="fg"))
        detail = ""
        if block.plan_final:
            done = sum(1 for item in block.plan_final if item.status == "completed")
            detail += f" · Plan {done}/{len(block.plan_final)}"
        detail += f" · {_format_span(block.duration_s)}"
        head.append(Segment(text=detail, style_token="dim"))
        chevron = GLYPH_CHEVRON_EXPANDED if block.expanded else GLYPH_CHEVRON_COLLAPSED
        head.append(Segment(text=f" {chevron}", style_token="dimmer"))
    lines: list[Line] = [tuple(head)]
    if not block.expanded:
        return tuple(lines)
    name_width = max((cell_len(e.agent) for e in block.entries), default=0)
    for index, entry in enumerate(block.entries):
        branch = "└─" if index == len(block.entries) - 1 else "├─"
        glyph, token = _DELEGATE_GLYPHS[entry.state]
        row: list[Segment] = [
            Segment(text=f"    {branch} ", style_token="dimmer"),
            Segment(text=f"{glyph} ", style_token=token),
            Segment(text=f"{entry.agent.ljust(name_width)}  ", style_token="dim"),
        ]
        if entry.state == "running":
            row.append(Segment(text="running", style_token="dimmer"))
        else:
            tail = _format_span(entry.elapsed_s)
            if entry.snippet:
                used = sum(cell_len(s.text) for s in row) + cell_len(tail)
                snippet = _clip(entry.snippet, width - used - 5)
                if snippet:
                    tail += f' · "{snippet}"'
            row.append(Segment(text=tail, style_token="dim"))
        lines.append(tuple(row))
    if block.plan_final:
        # One line, clipped to width — real plans carry long items that
        # would otherwise soft-wrap mid-word into an unaligned blob.
        plan_row: list[Segment] = [Segment(text="    Plan  ", style_token="dim")]
        used = 10
        shown = 0
        for item in block.plan_final:
            glyph, token = _DELEGATE_PLAN_GLYPHS[item.status]
            content = _clip(item.content, width - used - 4)  # glyph + trail
            if not content:
                break
            plan_row.append(Segment(text=f"{glyph} ", style_token=token))
            trail = "  " if content == item.content else ""
            plan_row.append(Segment(text=f"{content}{trail}", style_token="dim"))
            used += 2 + cell_len(content) + len(trail)
            shown += 1
            if not trail:
                break
        if shown < len(block.plan_final) and plan_row[-1].text.endswith("  "):
            # Items were dropped whole: spend the reserved trail on a
            # visible "there's more" marker (same width, no overflow).
            last = plan_row[-1]
            plan_row[-1] = Segment(text=f"{last.text[:-2]} …", style_token=last.style_token)
        lines.append(tuple(plan_row))
    return tuple(lines)


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
    "delegate_summary": _render_delegate_summary,
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
