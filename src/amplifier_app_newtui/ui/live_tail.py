"""The mutable streaming tail — region two of the two-region transcript.

ADR-0007: durable history (``ui/transcript.py``) is pure and immutable;
THIS widget is the single mutable region. It accumulates raw text deltas
(Channel A ``llm:stream_block_delta``), repaints at a throttled 30–60Hz,
and on ``llm:stream_block_end`` consolidates the accumulated source into
one durable :class:`~amplifier_app_newtui.model.blocks.Answer` block that
the app appends to the TranscriptView (which then gets
``set_streaming(False)`` → any deferred resize reflow runs once).

Table holdback (RESEARCH-BRIEF risk 1, after codex ``streaming/
controller.rs``): a markdown table cannot be laid out until all rows are
known, so a *trailing* table run is withheld from the painted tail until
either a paragraph break completes it or the stream ends — the
consolidated Answer always carries the full source.

Styling: fg text with ``**bold**`` → bright bold and `` `code` `` → teal
segments (DESIGN-SPEC §3 final answer); ``thinking`` blocks paint italic
dim. All styles are theme-variable references — no colors here.
"""

from __future__ import annotations

import asyncio
import re
from time import monotonic

from textual.message import Message
from textual.timer import Timer
from textual.widgets import Static

from ..model.blocks import GLYPH_QUOTE_GUTTER, Answer, Segment
from ..model.evidence import EvidenceLink
from .segments import segment_markup

THROTTLE_SECONDS = 1 / 30
"""Minimum interval between tail repaints (30Hz — inside the 30–60Hz budget)."""

LANE_TAIL_LINES = 3
"""Max painted lines of a focused lane's live tail (design doc D4)."""

ASYNC_RENDER_THRESHOLD = 100_000
"""Long streams parse off-thread so markdown can never stall the UI loop."""

_ANSWER_SPAN_RE = re.compile(
    r"(\*\*.+?\*\*|`[^`\n]+`|\[[^\]\n]+\]\((?:https?|file)://[^)\s]+\))", re.DOTALL
)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:https?|file)://[^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
"""Markdown blockquote line. The insight/machete callouts that the
hooks-inline-blocks module (occams-machete bundle) teaches the model to
emit are blockquotes — ``> ★ **Insight:** …`` / ``> ✂ **MJ:** …`` —
because the line-mode CLI's Rich renderer frames them with a colored
``▌`` gutter. This parser is that frame's TUI-native equivalent."""
_TABLE_GRID_MAX_WIDTH = 96
"""Padded-grid tables wider than this fall back to a definition list —
wrapped cells destroy column alignment (user screenshot, /about run)."""


def _inline(text: str) -> list[Segment]:
    """Inline emphasis: ``**…**`` bright bold, `` `…` `` teal code,
    ``[text](url)`` teal text + dimmer url, everything else fg (§3)."""
    spans: list[Segment] = []
    position = 0
    for match in _ANSWER_SPAN_RE.finditer(text):
        if match.start() > position:
            spans.append(Segment(text=text[position : match.start()]))
        token = match.group(0)
        if token.startswith("**"):
            spans.append(Segment(text=token[2:-2], style_token="bright", bold=True))
        elif token.startswith("`"):
            spans.append(Segment(text=token[1:-1], style_token="teal"))
        else:
            link = _LINK_RE.fullmatch(token)
            assert link is not None  # the alternation guarantees the shape
            spans.append(Segment(text=link.group(1), style_token="teal"))
            spans.append(Segment(text=f" ({link.group(2)})", style_token="dimmer"))
        position = match.end()
    if position < len(text):
        spans.append(Segment(text=text[position:]))
    return spans


def _plain_len(text: str) -> int:
    """Visible cell width of *text* once inline markers are stripped."""
    return sum(len(segment.text) for segment in _inline(text))


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _emit_table(spans: list[Segment], lines: list[str], start: int) -> int:
    """Render a pipe table (header · rule · rows) with aligned columns.

    Raw ``| a | b |`` lines read terribly in the transcript (user
    report); columns are padded to their widest cell, the ``|---|``
    separator becomes a dim rule, and the header row renders bright.
    Returns the index of the first line after the table.
    """
    end = start
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    rows = [_table_cells(lines[i]) for i in range(start, end)]
    body = [row for i, row in enumerate(rows) if not _TABLE_SEP_RE.match(lines[start + i])]
    columns = max(len(row) for row in body)
    widths = [
        max((_plain_len(row[col]) for row in body if col < len(row)), default=0)
        for col in range(columns)
    ]
    if sum(widths) + 3 * (columns - 1) > _TABLE_GRID_MAX_WIDTH:
        # Wide cells wrap and shred a padded grid (found live: the
        # /about run's Piece/Location table). Fall back to a definition
        # list — header dim, cell inline — which reads at any width.
        headers = body[0]
        for row in body[1:]:
            for col in range(columns):
                cell = row[col] if col < len(row) else ""
                if not cell:
                    continue
                header = headers[col] if col < len(headers) else ""
                spans.append(Segment(text=f"  {header or '·'}: ", style_token="dimmer"))
                spans.extend(_inline(cell))
                spans.append(Segment(text="\n"))
            spans.append(Segment(text="\n"))
        if spans and spans[-1].text == "\n":
            spans.pop()
        return end
    for index, row in enumerate(body):
        for col in range(columns):
            cell = row[col] if col < len(row) else ""
            if col:
                spans.append(Segment(text=" │ ", style_token="dimmer"))
            if index == 0:
                spans.append(Segment(text=cell, style_token="bright", bold=True))
            else:
                spans.extend(_inline(cell))
            spans.append(Segment(text=" " * (widths[col] - _plain_len(cell))))
        spans.append(Segment(text="\n"))
        if index == 0:
            rule = "─┼─".join("─" * width for width in widths)
            spans.append(Segment(text=rule, style_token="dimmer"))
            spans.append(Segment(text="\n"))
    return end


def _ensure_blank(spans: list[Segment]) -> None:
    """Append a blank line (a lone ``\\n`` sentinel) unless the last emitted
    line is already blank.

    Inter-block spacing (headings, list runs, tables, fenced code) is the
    main readability win — a block reads as its own paragraph. Every line the
    loop emits terminates in a ``Segment(text="\\n")``; a blank line is a
    second consecutive terminator. Nothing is added at the very start
    (leading blank) or when a blank already separates the previous block.
    """
    if not spans or spans[-1].text != "\n":
        return
    if len(spans) >= 2 and spans[-2].text == "\n":
        return  # the last line is already blank — don't stack another
    spans.append(Segment(text="\n"))


def answer_spans(source: str) -> tuple[Segment, ...]:
    """Raw model text → Answer segments (light markdown, theme tokens only).

    Inline: ``**…**`` bright bold, `` `…` `` teal code, links teal+dim —
    the selective emphasis DESIGN-SPEC §3 specifies. Real model output
    also carries block structure the mockup never had, rendered here so
    it doesn't leak raw (user report): ``#`` headings → bright bold,
    pipe tables → aligned columns with a dim rule, fenced code → teal
    indented block (fence lines dropped), ``- `` bullets → ``• ``, and
    ``> `` blockquotes → a colored ``▌`` gutter (the TUI-native frame for
    the insight/machete callouts, matching the line-mode CLI's Rich edge).
    """
    spans: list[Segment] = []
    lines = source.split("\n")
    index = 0
    in_code = False
    in_list = False  # inside a run of consecutive bullet/numbered items
    in_quote = False  # inside a run of consecutive `> ` blockquote lines
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            if not in_code:
                in_list = False
                in_quote = False
                _ensure_blank(spans)  # fenced code opens its own paragraph
                in_code = True
            else:
                in_code = False
                _ensure_blank(spans)  # …and closes with a trailing gap
            index += 1
            continue
        if in_code:
            spans.append(Segment(text=f"  {line}", style_token="teal"))
            spans.append(Segment(text="\n"))
            index += 1
            continue
        if (
            stripped.startswith("|")
            and index + 1 < len(lines)
            and lines[index + 1].lstrip().startswith("|")
            and _TABLE_SEP_RE.match(lines[index + 1])
        ):
            in_list = False
            in_quote = False
            _ensure_blank(spans)
            index = _emit_table(spans, lines, index)
            _ensure_blank(spans)
            continue
        if heading := _HEADING_RE.match(line):
            in_list = False
            in_quote = False
            _ensure_blank(spans)  # the blank before is what sets a heading off
            spans.append(Segment(text=heading.group(2), style_token="bright", bold=True))
            spans.append(Segment(text="\n"))
            _ensure_blank(spans)
            index += 1
            continue
        if numbered := _NUMBERED_RE.match(line):
            if not in_list:
                _ensure_blank(spans)
                in_list = True
            in_quote = False
            marker = f"{numbered.group(1)}{numbered.group(2)}. "
            spans.append(Segment(text=marker, style_token="dim"))
            spans.extend(_inline(numbered.group(3)))
            spans.append(Segment(text="\n"))
            index += 1
            continue
        if bullet := _BULLET_RE.match(line):
            if not in_list:
                _ensure_blank(spans)
                in_list = True
            in_quote = False
            spans.append(Segment(text=f"{bullet.group(1)}• ", style_token="dim"))
            spans.extend(_inline(bullet.group(2)))
            spans.append(Segment(text="\n"))
            index += 1
            continue
        if quote := _QUOTE_RE.match(line):
            # Insight/machete callouts and any other blockquote: a colored
            # left gutter frames the quote, inline emphasis still applies.
            in_list = False
            if not in_quote:
                _ensure_blank(spans)  # a quote run reads as its own paragraph
                in_quote = True
            spans.append(Segment(text=GLYPH_QUOTE_GUTTER, style_token="blue"))
            spans.extend(_inline(quote.group(1)))
            spans.append(Segment(text="\n"))
            index += 1
            continue
        if in_list:
            _ensure_blank(spans)  # a plain line closes the list run
            in_list = False
        if in_quote:
            _ensure_blank(spans)  # a plain line closes the quote run
            in_quote = False
        spans.extend(_inline(line))
        spans.append(Segment(text="\n"))
        index += 1
    # The per-line loop appends one newline per source line; the final one
    # would fabricate a trailing blank line — drop it. A block that ends the
    # answer (heading/list/table/code) also left a trailing blank sentinel
    # from _ensure_blank; pop that too so answers never end on empty lines.
    if len(spans) >= 2 and spans[-1].text == "\n" and spans[-2].text == "\n":
        spans.pop()
    if spans and spans[-1].text == "\n" and spans[-1].style_token == "fg":
        spans.pop()
    if not spans:
        spans.append(Segment(text=""))
    return tuple(spans)


def visible_length(lines: list[str]) -> int:
    """Number of leading lines that may be painted mid-stream.

    A trailing run of table lines (``|``-prefixed) is withheld. One final
    empty element (the artifact of a source ending in ``\\n``) is ignored
    when locating the run; a *blank line* after the table (paragraph
    break) means the table is complete and paintable.
    """
    scan = len(lines)
    if scan and lines[-1] == "":
        scan -= 1
    cut = scan
    while cut > 0 and lines[cut - 1].lstrip().startswith("|"):
        cut -= 1
    if cut == scan:
        return len(lines)  # no trailing table run
    return cut


def _open_fence(source: str) -> str | None:
    """Return the active fence marker after all completed lines."""
    active: str | None = None
    for line in source.splitlines():
        stripped = line.strip()
        marker = (
            "```"
            if stripped.startswith("```")
            else "~~~"
            if stripped.startswith("~~~")
            else None
        )
        if marker is None:
            continue
        if active is None:
            active = marker
        elif marker == active:
            active = None
    return active


def streaming_spans(source: str) -> tuple[Segment, ...]:
    """Render completed streaming lines through the final answer pipeline.

    Only the trailing partial line remains plain. A trailing table run is
    held back until its paragraph break arrives, and a partial line inside
    an open fence uses the same indented teal treatment as the final answer.
    """
    lines = source.split("\n")
    cut = visible_length(lines)
    table_held = cut < len(lines)
    visible = "\n".join(lines[:cut]) if table_held else source
    if not visible:
        return ()

    if table_held:
        committed, partial = visible, ""
    elif "\n" in visible:
        committed, partial = visible.rsplit("\n", 1)
    else:
        committed, partial = "", visible

    spans: list[Segment] = list(answer_spans(committed)) if committed else []
    if committed and partial:
        spans.append(Segment(text="\n"))
    if partial:
        if _open_fence(committed) is not None:
            spans.append(Segment(text=f"  {partial}", style_token="teal"))
        else:
            spans.append(Segment(text=partial))
    return tuple(spans)


def lane_tail_markup(text: str) -> str:
    """Markup for a focused lane's tail: the last :data:`LANE_TAIL_LINES`
    non-blank lines, ``┆``-guttered, dim (DESIGN-SPEC §8). Pure function —
    unit-testable without a widget; content is escaped, never interpreted.
    """
    from textual.markup import escape

    lines = [line for line in text.split("\n") if line.strip()][-LANE_TAIL_LINES:]
    if not lines:
        return ""
    body = "\n".join(f"┆ {escape(line)}" for line in lines)
    return f"[$dim]{body}[/]"


class LiveTail(Static):
    """Streaming tail widget: accumulate deltas, throttle paints, consolidate.

    Contract with the app layer:

    - ``open_stream(block_type)`` on ``stream_block_start``;
    - ``feed(text)`` per ``stream_block_delta`` (repaints coalesce to
      ≤30Hz via a trailing timer — high-frequency deltas cost one paint);
    - ``consolidate(block_id)`` on ``stream_block_end`` → returns the
      durable Answer AND posts :class:`LiveTail.Consolidated` so wiring
      stays message-based.
    """

    DEFAULT_CSS = """
    LiveTail {
        height: auto;
    }
    """

    class Consolidated(Message):
        """The stream ended; ``answer`` is the durable block to append."""

        def __init__(self, answer: Answer) -> None:
            super().__init__()
            self.answer = answer

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._source_chunks: list[str] = []
        self._source_cache = ""
        self._block_type = "text"
        self._timer: Timer | None = None
        self._last_paint = 0.0
        self._paint_count = 0
        self._render_generation = 0
        self._render_pending: tuple[int, str, str] | None = None
        self._async_render_active = False
        self._lane_mode = False
        self._root_open = False

    @property
    def source(self) -> str:
        """The full accumulated raw text (holdback never applies here)."""
        if len(self._source_chunks) > 1 or (
            self._source_chunks and not self._source_cache
        ):
            self._source_cache = "".join(self._source_chunks)
            self._source_chunks = [self._source_cache] if self._source_cache else []
        return self._source_cache

    @property
    def block_type(self) -> str:
        return self._block_type

    @property
    def paint_count(self) -> int:
        """Paints performed so far (throttle tests observe this)."""
        return self._paint_count

    def open_stream(self, block_type: str = "text") -> None:
        """Reset for a new streaming block (``llm:stream_block_start``)."""
        self._lane_mode = False  # root always preempts the lane tail (D4)
        self._root_open = True
        self._cancel_timer()
        self._reset_source()
        self._invalidate_async_render()
        self._block_type = block_type
        self._last_paint = 0.0
        self._paint_now()

    def feed(self, text: str) -> None:
        """Accumulate one delta; schedule a throttled repaint."""
        if text:
            self._source_chunks.append(text)
            self._source_cache = ""
        if self._timer is not None:
            return  # a trailing paint is already scheduled
        now = monotonic()
        due = self._last_paint + THROTTLE_SECONDS
        if now >= due:
            self._paint_now()
        else:
            self._timer = self.set_timer(due - now, self._paint_now)

    def consolidate(self, block_id: str) -> Answer:
        """Close the stream: emit the durable Answer, clear the tail.

        The full source (including any held-back trailing table) becomes
        the Answer's spans. Also posts :class:`LiveTail.Consolidated`.
        Evidence refs are attached later by the app (they need tool
        correlation) — the block id is stable for that.
        """
        source = self.source.rstrip("\n")
        answer = Answer(id=block_id, spans=answer_spans(source))
        self._cancel_timer()
        self._root_open = False
        self._reset_source()
        self._invalidate_async_render()
        self._last_paint = 0.0
        self.update("")
        self.post_message(self.Consolidated(answer))
        return answer

    @property
    def lane_mode(self) -> bool:
        """True while the tail shows a focused lane's stream, not the root's."""
        return self._lane_mode

    def show_lane_tail(self, text: str) -> None:
        """Paint the focused lane's accumulated tail (dim, ``┆``-guttered).

        The root always preempts: refused while a root stream is open. The
        reducer owns accumulation and the ~0.05s throttle
        (``LANE_TAIL_NOTIFY_SECONDS``); this widget just paints the last
        :data:`LANE_TAIL_LINES` lines. Lane content is ephemeral — it is
        never consolidated into a transcript block.
        """
        if self._root_open:
            return
        self._lane_mode = True
        self.update(lane_tail_markup(text))

    def clear_lane_tail(self) -> None:
        """Drop the lane tail (root preemption / lane done / turn end)."""
        if not self._lane_mode:
            return
        self._lane_mode = False
        self.update("")

    def attach_evidence(
        self, answer: Answer, links: tuple[EvidenceLink, ...]
    ) -> Answer:
        """Convenience: the consolidated Answer with evidence refs attached."""
        return answer.model_copy(update={"evidence_refs": links})

    def visible_source(self) -> str:
        """The paintable portion of the source (trailing tables withheld)."""
        source = self.source
        lines = source.split("\n")
        cut = visible_length(lines)
        if cut >= len(lines):
            return source
        return "\n".join(lines[:cut])

    # -- painting ------------------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _reset_source(self) -> None:
        self._source_chunks.clear()
        self._source_cache = ""

    def _invalidate_async_render(self) -> None:
        self._render_generation += 1
        self._render_pending = None

    def _paint_now(self) -> None:
        self._timer = None
        self._last_paint = monotonic()
        source = self.source
        self._render_generation += 1
        generation = self._render_generation
        if len(source) < ASYNC_RENDER_THRESHOLD:
            self._render_pending = None
            self._paint_count += 1
            self.update(self._markup_for(source, self._block_type))
            return
        # Keep only the newest requested frame. The parser is pure and the
        # generation fence prevents a stale worker from repainting after a
        # stream closes or a newer delta arrives.
        self._render_pending = (generation, source, self._block_type)
        if not self._async_render_active:
            self._async_render_active = True
            self.run_worker(self._drain_async_renders(), exclusive=False)

    async def _drain_async_renders(self) -> None:
        try:
            while self._render_pending is not None:
                generation, source, block_type = self._render_pending
                self._render_pending = None
                markup = await asyncio.to_thread(self._markup_for, source, block_type)
                if generation == self._render_generation:
                    self._paint_count += 1
                    self.update(markup)
        finally:
            self._async_render_active = False

    def _markup(self) -> str:
        return self._markup_for(self.source, self._block_type)

    @staticmethod
    def _markup_for(source: str, block_type: str) -> str:
        lines = source.split("\n")
        cut = visible_length(lines)
        visible = source if cut >= len(lines) else "\n".join(lines[:cut])
        if not visible:
            return ""
        if block_type == "thinking":
            from textual.markup import escape

            return f"[italic $dim]{escape(visible)}[/]"
        return "".join(segment_markup(segment) for segment in streaming_spans(source))


__all__ = [
    "ASYNC_RENDER_THRESHOLD",
    "LANE_TAIL_LINES",
    "THROTTLE_SECONDS",
    "LiveTail",
    "answer_spans",
    "lane_tail_markup",
    "streaming_spans",
    "visible_length",
]
