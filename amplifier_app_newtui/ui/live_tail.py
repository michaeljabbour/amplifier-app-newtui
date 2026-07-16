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

import re
from time import monotonic

from textual.markup import escape
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Static

from ..model.blocks import Answer, Segment
from ..model.evidence import EvidenceLink

THROTTLE_SECONDS = 1 / 30
"""Minimum interval between tail repaints (30Hz — inside the 30–60Hz budget)."""

_ANSWER_SPAN_RE = re.compile(
    r"(\*\*.+?\*\*|`[^`\n]+`|\[[^\]\n]+\]\((?:https?|file)://[^)\s]+\))", re.DOTALL
)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:https?|file)://[^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")


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


def answer_spans(source: str) -> tuple[Segment, ...]:
    """Raw model text → Answer segments (light markdown, theme tokens only).

    Inline: ``**…**`` bright bold, `` `…` `` teal code, links teal+dim —
    the selective emphasis DESIGN-SPEC §3 specifies. Real model output
    also carries block structure the mockup never had, rendered here so
    it doesn't leak raw (user report): ``#`` headings → bright bold,
    pipe tables → aligned columns with a dim rule, fenced code → teal
    indented block (fence lines dropped), ``- `` bullets → ``• ``.
    """
    spans: list[Segment] = []
    lines = source.split("\n")
    index = 0
    in_code = False
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
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
            index = _emit_table(spans, lines, index)
            continue
        if heading := _HEADING_RE.match(line):
            spans.append(Segment(text=heading.group(2), style_token="bright", bold=True))
            spans.append(Segment(text="\n"))
            index += 1
            continue
        if bullet := _BULLET_RE.match(line):
            spans.append(Segment(text=f"{bullet.group(1)}• ", style_token="dim"))
            spans.extend(_inline(bullet.group(2)))
            spans.append(Segment(text="\n"))
            index += 1
            continue
        spans.extend(_inline(line))
        spans.append(Segment(text="\n"))
        index += 1
    # The per-line loop appends one newline per source line; the final one
    # would fabricate a trailing blank line — drop it.
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
        self._source = ""
        self._block_type = "text"
        self._timer: Timer | None = None
        self._last_paint = 0.0
        self._paint_count = 0

    @property
    def source(self) -> str:
        """The full accumulated raw text (holdback never applies here)."""
        return self._source

    @property
    def block_type(self) -> str:
        return self._block_type

    @property
    def paint_count(self) -> int:
        """Paints performed so far (throttle tests observe this)."""
        return self._paint_count

    def open_stream(self, block_type: str = "text") -> None:
        """Reset for a new streaming block (``llm:stream_block_start``)."""
        self._cancel_timer()
        self._source = ""
        self._block_type = block_type
        self._last_paint = 0.0
        self._paint_now()

    def feed(self, text: str) -> None:
        """Accumulate one delta; schedule a throttled repaint."""
        self._source += text
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
        source = self._source.rstrip("\n")
        answer = Answer(id=block_id, spans=answer_spans(source))
        self._cancel_timer()
        self._source = ""
        self._last_paint = 0.0
        self.update("")
        self.post_message(self.Consolidated(answer))
        return answer

    def attach_evidence(
        self, answer: Answer, links: tuple[EvidenceLink, ...]
    ) -> Answer:
        """Convenience: the consolidated Answer with evidence refs attached."""
        return answer.model_copy(update={"evidence_refs": links})

    def visible_source(self) -> str:
        """The paintable portion of the source (trailing tables withheld)."""
        lines = self._source.split("\n")
        cut = visible_length(lines)
        if cut >= len(lines):
            return self._source
        return "\n".join(lines[:cut])

    # -- painting ------------------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _paint_now(self) -> None:
        self._timer = None
        self._last_paint = monotonic()
        self._paint_count += 1
        self.update(self._markup())

    def _markup(self) -> str:
        text = escape(self.visible_source())
        if not text:
            return ""
        if self._block_type == "thinking":
            return f"[italic $dim]{text}[/]"
        return f"[$fg]{text}[/]"


__all__ = ["THROTTLE_SECONDS", "LiveTail", "answer_spans", "visible_length"]
