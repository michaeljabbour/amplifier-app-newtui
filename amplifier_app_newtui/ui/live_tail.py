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

_ANSWER_SPAN_RE = re.compile(r"(\*\*.+?\*\*|`[^`\n]+`)", re.DOTALL)


def answer_spans(source: str) -> tuple[Segment, ...]:
    """Split raw streamed text into Answer segments.

    ``**…**`` → bright bold, `` `…` `` → teal inline code, everything else
    fg — the selective emphasis DESIGN-SPEC §3 specifies for final answers.
    """
    spans: list[Segment] = []
    position = 0
    for match in _ANSWER_SPAN_RE.finditer(source):
        if match.start() > position:
            spans.append(Segment(text=source[position : match.start()]))
        token = match.group(0)
        if token.startswith("**"):
            spans.append(Segment(text=token[2:-2], style_token="bright", bold=True))
        else:
            spans.append(Segment(text=token[1:-1], style_token="teal"))
        position = match.end()
    if position < len(source):
        spans.append(Segment(text=source[position:]))
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
