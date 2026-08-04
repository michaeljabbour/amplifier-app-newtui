"""Evidence side panel: supporting detail for the focused claim.

Compliance item D7 (DESIGN-SPEC §10 extension). One docked panel, one
purpose — tool-call provenance for whichever evidence claim the user is
looking at (brief design note: "choose ONE primary side-panel purpose at
a time; avoid a permanently crowded dashboard"). Modeled on
``ui/lanes_panel.py``'s conventions: bright-title + dimmer-hint header,
``display: none`` by default, explicit show/hide methods driven by the
app — never a second independent focus surface. The panel is read-only
and never takes keyboard focus itself (``can_focus = False``): the
evidence block's own ←/→/enter/d/esc chords stay the single interactive
surface (brief: "the final answer references [evidence] without becoming
the container for every tool detail" — this panel is the supporting
detail, the block stays primary).

Content is entirely driven by :meth:`EvidencePanel.show_detail` /
:meth:`hide_panel` / :meth:`close` — computed lazily by the caller only
when a claim's detail is actually requested, never eagerly for every
claim up front (brief: "implement … lazy loading … before adding
multiple panel tabs").
"""

from __future__ import annotations

from collections.abc import Mapping

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from ..model.evidence import EvidenceDetail, format_evidence_timestamp

PANEL_TITLE = "Evidence detail"
PANEL_HINT = "· d to close"
PANEL_HEADER = f"{PANEL_TITLE}  {PANEL_HINT}"
"""Exact header text (bright title + dimmer hint), mirroring
``ui/lanes_panel.py``'s ``LANES_HEADER`` idiom."""

EVIDENCE_PANEL_WIDTH = 44
"""Fixed docked width (AC4). Generous enough to read a wrapped tool
summary line without crowding the transcript at the panel's own minimum
supported terminal width (``app_support.EVIDENCE_PANEL_MIN_WIDTH``)."""

_FALLBACK_ONLY_STATUSES = frozenset({"unavailable", "expired"})
"""Statuses with no provenance record to show — only the claim + an
explicit AC5 fallback message render."""


def _detail_text(detail: EvidenceDetail, tokens: Mapping[str, str]) -> Text:
    """Pure rendering of *detail* against theme *tokens* (testable without
    booting a Textual app — mirrors the transcript renderer's own
    pure-function-of-state philosophy, ``ui/transcript_render.py``)."""
    text = Text()
    # The claim itself always renders first, styled exactly like its row
    # in the evidence block (fg quote + dim arrow/ref) so the panel reads
    # as the SAME claim's detail, never a disconnected second surface.
    text.append(f'"{detail.claim_quote}"', style=Style(color=tokens.get("fg")))
    text.append("\n")
    text.append(f"→ {detail.tool_ref}", style=Style(color=tokens.get("dim")))
    text.append("\n\n")

    if detail.status in _FALLBACK_ONLY_STATUSES:
        text.append(detail.fallback, style=Style(color=tokens.get("orange")))
        return text

    # ready / oversized: a record resolved — identify it (AC2).
    text.append("tool   ", style=Style(color=tokens.get("dimmer")))
    text.append(detail.tool_name or "—", style=Style(color=tokens.get("teal")))
    if detail.input_summary:
        text.append("\n")
        text.append("input  ", style=Style(color=tokens.get("dimmer")))
        text.append(detail.input_summary, style=Style(color=tokens.get("fg")))
    text.append("\n")
    text.append("agent  ", style=Style(color=tokens.get("dimmer")))
    text.append(detail.agent or "—", style=Style(color=tokens.get("fg")))
    when = format_evidence_timestamp(detail.timestamp)
    if when:
        text.append("\n")
        text.append("when   ", style=Style(color=tokens.get("dimmer")))
        text.append(when, style=Style(color=tokens.get("fg")))
    text.append("\n\n")
    if detail.output:
        text.append(detail.output, style=Style(color=tokens.get("fg")))
    else:
        text.append("(no output recorded)", style=Style(color=tokens.get("dimmer")))
    if detail.status == "oversized":
        text.append("\n\n")
        text.append(detail.fallback, style=Style(color=tokens.get("orange")))
    return text


class _EvidencePanelHeader(Static):
    """``Evidence detail  · d to close`` — bright title + dimmer hint."""

    DEFAULT_CSS = """
    _EvidencePanelHeader {
        width: 100%;
        height: 1;
    }
    """

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text()
        text.append(PANEL_TITLE, style=Style(color=tokens.get("bright"), bold=True))
        text.append("  ")
        text.append(PANEL_HINT, style=Style(color=tokens.get("dimmer")))
        return text


class _EvidencePanelBody(VerticalScroll):
    """Scrollable detail content.

    The panel never takes keyboard focus (see module docstring), but its
    content is still bounded-not-clipped: output is capped at
    ``MAX_EVIDENCE_OUTPUT_CHARS`` (model/evidence.py) yet can still wrap
    past the docked panel's fixed height, so this stays mouse-scrollable
    rather than silently cutting text off below the fold.
    """

    DEFAULT_CSS = """
    _EvidencePanelBody {
        width: 100%;
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._content = Static("")

    def compose(self) -> ComposeResult:
        yield self._content

    def show(self, detail: EvidenceDetail) -> None:
        self._content.update(_detail_text(detail, self.app.theme_variables))
        self.scroll_home(animate=False)

    def clear(self) -> None:
        self._content.update("")


class EvidencePanel(Vertical):
    """The evidence detail side panel (D7 AC2/AC4/AC5).

    Docked beside the transcript (``ui/app.py``'s ``#transcript-split``).
    ``show_detail`` / ``hide_panel`` / ``close`` are the only mutators;
    :meth:`sync_width` applies the AC4 responsive collapse without
    discarding the shown detail (mirrors the plan panel's ladder,
    ``app_support.sync_plan_surfaces``, design D2).
    """

    can_focus = False

    DEFAULT_CSS = """
    EvidencePanel {
        display: none;
        width: 100%;
        height: 1fr;
        border-left: solid $rule;
        padding: 0 1;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._detail: EvidenceDetail | None = None
        self._body = _EvidencePanelBody()

    def compose(self) -> ComposeResult:
        yield _EvidencePanelHeader()
        yield self._body

    @property
    def detail(self) -> EvidenceDetail | None:
        """The currently-shown detail, or ``None`` when fully closed."""
        return self._detail

    @property
    def is_open(self) -> bool:
        """True once a claim's detail has been requested — independent of
        a momentary width-driven collapse (see :meth:`sync_width`), so
        widening the terminal back out can restore it."""
        return self._detail is not None

    def show_detail(self, detail: EvidenceDetail) -> None:
        """Render *detail* and make the panel visible.

        Lazy by construction: the caller computes ``EvidenceDetail`` only
        when a claim's detail is actually requested (never eagerly for
        every claim in the block).
        """
        self._detail = detail
        self._body.show(detail)
        self.display = True

    def hide_panel(self) -> None:
        """Width-driven collapse (AC4): hide WITHOUT discarding the shown
        detail, so a later widen can restore it (mirrors the plan panel's
        responsive ladder, design D2)."""
        self.display = False

    def close(self) -> None:
        """User-driven dismissal: discard the shown detail entirely."""
        self._detail = None
        self._body.clear()
        self.display = False

    def sync_width(self, width: int, *, min_width: int) -> None:
        """Apply the AC4 responsive collapse for the current terminal
        *width* without discarding the shown detail. A no-op while
        nothing is open."""
        if self._detail is None:
            return
        self.display = width >= min_width


__all__ = [
    "EVIDENCE_PANEL_WIDTH",
    "PANEL_HEADER",
    "PANEL_HINT",
    "PANEL_TITLE",
    "EvidencePanel",
]
