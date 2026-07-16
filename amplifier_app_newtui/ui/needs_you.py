"""Needs-you block rendering + focused-lane banner helpers (DESIGN-SPEC §7/§8).

The needs-you list renders transcript-block-style (it is printed into the
transcript flow on ctrl-y / footer-badge click, not a modal):

- Header (orange): ``· Needs you  N deferred decision``
- One numbered row per deferred decision: orange number + fg question +
  inline actionable chips like ``[yes · push to fork]`` (green on
  bg-tab). Clicking a chip posts :class:`NeedsYouList.DecisionTaken`;
  the app then logs the ``Applying decision: …`` narration and clears
  the footer badge.

Also provides the focused-lane banner line helper (spec §8): the bright
``focused: <name>`` prefix plus the dim
``· subagent of <parent> · own context window · results report back to
parent · esc back`` tail.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from ..model.blocks import NeedsYouBlock, NeedsYouChoice, NeedsYouEntry


def needs_you_header(count: int) -> str:
    """Header text: ``Needs you  N deferred decision`` (spec §7, verbatim)."""
    return f"Needs you  {count} deferred decision"


def needs_you_header_line(count: int) -> str:
    """The full header line including the leading ``· `` marker."""
    return f"· {needs_you_header(count)}"


def decision_number_text(number: int) -> str:
    """The orange row-number prefix: ``  1 `` (two-space indent, mockup)."""
    return f"  {number} "


def chip_text(choice: NeedsYouChoice) -> str:
    """Inline chip text: ``[<label>]`` e.g. ``[yes · push to fork]``."""
    return f"[{choice.label}]"


def applying_decision_line(detail: str) -> str:
    """Narration logged when a decision is acted on: ``Applying decision: …``."""
    return f"Applying decision: {detail}"


def focused_lane_banner_parts(name: str, parent_session: str) -> tuple[str, str]:
    """(bright bold prefix, dim tail) of the focused-lane banner (spec §8)."""
    return (
        f"focused: {name} ",
        f"· subagent of {parent_session} · own context window"
        " · results report back to parent · esc back",
    )


def focused_lane_banner(name: str, parent_session: str) -> str:
    """The full focused-lane banner line as plain text."""
    prefix, tail = focused_lane_banner_parts(name, parent_session)
    return prefix + tail


class _NeedsYouHeader(Static):
    """Orange ``· Needs you  N deferred decision`` header line."""

    DEFAULT_CSS = """
    _NeedsYouHeader {
        width: 100%;
        height: 1;
        color: $orange;
    }
    """

    def __init__(self, count: int) -> None:
        super().__init__(Text(needs_you_header_line(count)), id="needs-you-header")
        self.count = count


class _ChoiceChip(Static):
    """One actionable chip: ``[<label>]`` green on bg-tab, clickable."""

    DEFAULT_CSS = """
    _ChoiceChip {
        width: auto;
        height: 1;
        color: $green;
        background: $bg-tab;
        margin-left: 2;
    }
    """

    def __init__(self, entry: NeedsYouEntry, choice: NeedsYouChoice, index: int) -> None:
        super().__init__(
            Text(chip_text(choice)), id=f"chip-{entry.decision_id}-{index}"
        )
        self.entry = entry
        self.choice = choice

    def on_click(self) -> None:
        self.post_message(
            NeedsYouList.DecisionTaken(self.entry.decision_id, self.choice.answer)
        )


class _DecisionText(Static):
    """Orange number + fg question (+ dim reason) text of one decision row."""

    DEFAULT_CSS = """
    _DecisionText {
        width: auto;
        height: 1;
    }
    """

    def __init__(self, entry: NeedsYouEntry, number: int) -> None:
        super().__init__()
        self.entry = entry
        self.number = number

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text()
        text.append(
            decision_number_text(self.number), style=Style(color=tokens.get("orange"))
        )
        text.append(self.entry.question, style=Style(color=tokens.get("fg")))
        if self.entry.reason:
            text.append(f" · {self.entry.reason}", style=Style(color=tokens.get("dim")))
        return text


class _DecisionRow(Horizontal):
    """One numbered decision line with its inline chips."""

    DEFAULT_CSS = """
    _DecisionRow {
        width: 100%;
        height: auto;
    }
    """

    def __init__(self, entry: NeedsYouEntry, number: int) -> None:
        super().__init__(id=f"needs-you-row-{entry.decision_id}")
        self.entry = entry
        self.number = number

    def compose(self):
        yield _DecisionText(self.entry, self.number)
        for index, choice in enumerate(self.entry.choices):
            yield _ChoiceChip(self.entry, choice, index)


class NeedsYouList(Vertical):
    """Transcript-block-style needs-you list (DESIGN-SPEC §7).

    Feed it a :class:`NeedsYouBlock` via :meth:`update_block`. Chip
    clicks (or :meth:`take_decision` for keyboard paths) post
    :class:`DecisionTaken`; the app applies the answer, logs
    ``Applying decision: …`` and clears the footer badge.
    """

    DEFAULT_CSS = """
    NeedsYouList {
        width: 100%;
        height: auto;
    }
    """

    class DecisionTaken(Message):
        """The human acted on a deferred decision chip."""

        def __init__(self, item_id: str, choice: str) -> None:
            self.item_id = item_id
            self.choice = choice
            super().__init__()

    def __init__(self, block: NeedsYouBlock | None = None, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._block = block

    @property
    def block(self) -> NeedsYouBlock | None:
        return self._block

    @property
    def header_text(self) -> str:
        """The exact header line currently displayed (empty when no block)."""
        if self._block is None:
            return ""
        return needs_you_header_line(len(self._block.items))

    def on_mount(self) -> None:
        if self._block is not None:
            self._rebuild()

    def update_block(self, block: NeedsYouBlock) -> None:
        """Replace the rendered decision list."""
        self._block = block
        if self.is_mounted:
            self._rebuild()

    def take_decision(self, item_id: str, choice: str) -> None:
        """Programmatic chip activation (keyboard/number paths)."""
        self.post_message(self.DecisionTaken(item_id, choice))

    def _rebuild(self) -> None:
        # remove_children is asynchronous: await it before remounting so
        # rebuilt rows never collide with the ids of outgoing ones.
        self.call_later(self._remount_rows)

    async def _remount_rows(self) -> None:
        await self.remove_children()
        if self._block is None or not self._block.items:
            return
        rows: list[Static | Horizontal] = [_NeedsYouHeader(len(self._block.items))]
        rows.extend(
            _DecisionRow(entry, number)
            for number, entry in enumerate(self._block.items, start=1)
        )
        await self.mount(*rows)


__all__ = [
    "NeedsYouList",
    "applying_decision_line",
    "chip_text",
    "decision_number_text",
    "focused_lane_banner",
    "focused_lane_banner_parts",
    "needs_you_header",
    "needs_you_header_line",
]
