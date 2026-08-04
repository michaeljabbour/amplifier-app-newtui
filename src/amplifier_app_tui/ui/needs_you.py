"""Needs-you block rendering + focused-lane banner helpers (DESIGN-SPEC §7/§8).

The needs-you list renders transcript-block-style (it is printed into the
transcript flow on ctrl-y / footer-badge click, not a modal):

- Header (orange): ``· Needs you  N deferred decision``
- One numbered row per deferred decision: orange number + fg question +
  inline actionable chips like ``[yes · push to fork]`` (green on
  bg-tab), with the governance escalation reason (or the question tool's
  header) as its own dim ``    why · …`` line beneath. Clicking a chip posts
  :class:`NeedsYouList.DecisionTaken`; the app then logs the
  ``Applying decision: …`` narration and clears the footer badge.

Question-tool answering (this slice): a decision may carry the donor
``question`` tool's richer shape -- per-option ``description`` help, a
``multiple`` multi-select flag, and a ``custom`` free-text affordance. Those
render as dim description lines under the row, checkbox chips that TOGGLE a
selection (finalized by a ``[submit]`` chip, comma-joining the picks), and a
``[+ type your own]`` chip that asks the app to capture a free-text answer.
When the block is opened for answering (ctrl-y) it takes the keyboard, so
number keys 1..9 pick/toggle an option and Enter submits a multi-select --
the terminal-native echo of the donor's number-key + enter flow.

Also provides the focused-lane banner line helper (spec §8): the bright
``focused: <name>`` prefix plus the dim
``· subagent of <parent> · own context window · results report back to
parent · esc back`` tail.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from ..model.blocks import (
    GLYPH_CHECKBOX_CHECKED,
    GLYPH_CHECKBOX_EMPTY,
    NeedsYouBlock,
    NeedsYouChoice,
    NeedsYouEntry,
)


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


def multi_chip_text(choice: NeedsYouChoice, *, selected: bool) -> str:
    """Multi-select chip: ``[✓ label]`` when picked, ``[☐ label]`` when not
    (donor question.tsx checkbox affordance)."""
    box = GLYPH_CHECKBOX_CHECKED if selected else GLYPH_CHECKBOX_EMPTY
    return f"[{box} {choice.label}]"


def select_all_hint() -> str:
    """Donor multi-select prompt suffix."""
    return " (select all that apply)"


def submit_chip_text(count: int) -> str:
    """The multi-select finalize chip: ``[submit N]`` (or ``[submit]`` at 0)."""
    return f"[submit {count}]" if count else "[submit]"


def custom_chip_text() -> str:
    """The free-text affordance chip (donor "Type your own answer")."""
    return "[+ type your own]"


def option_description_line(choice: NeedsYouChoice) -> str:
    """One dim per-option help line: ``      <label> · <description>``."""
    return f"      {choice.label} · {choice.description}"


def applying_decision_line(detail: str) -> str:
    """Narration logged when a decision is acted on: ``Applying decision: …``."""
    return f"Applying decision: {detail}"


def decision_why_line(reason: str) -> str:
    """The dim ``    why · <reason>`` line under a decision row.

    The governance escalation reason -- or the question tool's short header --
    gets its own line (deferred-decision UX) instead of being inlined into the
    question row.
    """
    return f"    why · {reason}"


def focused_lane_banner_parts(name: str, parent_session: str, turn: int = 0) -> tuple[str, str]:
    """(bright bold prefix, dim tail) of the focused-lane banner (spec §8).

    ``turn`` -- the 1-indexed turn that spawned this lane (D6 AC4: every
    visible stream states its producing agent AND its turn) -- rides
    between the parent-session clause and the context-window clause when
    known (``<= 0`` omits it cleanly rather than printing ``turn 0``).
    """
    turn_clause = f" · turn {turn}" if turn > 0 else ""
    return (
        f"focused: {name} ",
        f"· subagent of {parent_session}{turn_clause} · own context window"
        " · results report back to parent · esc back",
    )


def focused_lane_banner(name: str, parent_session: str, turn: int = 0) -> str:
    """The full focused-lane banner line as plain text."""
    prefix, tail = focused_lane_banner_parts(name, parent_session, turn)
    return prefix + tail


def multi_answer(labels: tuple[str, ...]) -> str:
    """The submitted multi-select answer: labels comma-joined (matches the
    backend ``question`` tool's ``format_output`` for multi-select)."""
    return ", ".join(labels)


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
    """One actionable chip: ``[<label>]`` green on bg-tab, clickable.

    For a ``multiple`` decision the chip is a checkbox that TOGGLES the choice
    into the row's selection instead of answering immediately.
    """

    DEFAULT_CSS = """
    _ChoiceChip {
        width: auto;
        height: 1;
        color: $green;
        background: $bg-tab;
        margin-left: 2;
    }
    """

    def __init__(
        self, entry: NeedsYouEntry, choice: NeedsYouChoice, index: int, *, selected: bool
    ) -> None:
        text = multi_chip_text(choice, selected=selected) if entry.multiple else chip_text(choice)
        super().__init__(Text(text), id=f"chip-{entry.decision_id}-{index}")
        self.entry = entry
        self.choice = choice
        self.index = index

    def on_click(self, event: events.Click) -> None:
        event.stop()  # the row would otherwise re-fire its first choice
        if self.entry.multiple:
            self.post_message(NeedsYouList.ChoiceToggled(self.entry.decision_id, self.index))
        else:
            self.post_message(
                NeedsYouList.DecisionTaken(self.entry.decision_id, self.choice.answer)
            )


class _SubmitChip(Static):
    """The ``[submit N]`` chip finalizing a multi-select decision."""

    DEFAULT_CSS = """
    _SubmitChip {
        width: auto;
        height: 1;
        color: $orange;
        background: $bg-tab;
        margin-left: 2;
    }
    """

    def __init__(self, entry: NeedsYouEntry, count: int) -> None:
        super().__init__(Text(submit_chip_text(count)), id=f"submit-{entry.decision_id}")
        self.entry = entry

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(NeedsYouList.SubmitRequested(self.entry.decision_id))


class _CustomChip(Static):
    """The ``[+ type your own]`` free-text affordance chip (donor custom)."""

    DEFAULT_CSS = """
    _CustomChip {
        width: auto;
        height: 1;
        color: $teal;
        background: $bg-tab;
        margin-left: 2;
    }
    """

    def __init__(self, entry: NeedsYouEntry) -> None:
        super().__init__(Text(custom_chip_text()), id=f"custom-{entry.decision_id}")
        self.entry = entry

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(NeedsYouList.CustomAnswerRequested(self.entry.decision_id))


class _DecisionText(Static):
    """Orange number + fg question text of one decision row (the reason
    renders as its own :class:`_DecisionWhy` line beneath)."""

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
        text.append(decision_number_text(self.number), style=Style(color=tokens.get("orange")))
        question = self.entry.question
        highlight = self.entry.highlight
        if highlight and highlight in question:
            before, _, after = question.partition(highlight)
            if before:
                text.append(before, style=Style(color=tokens.get("fg")))
            text.append(highlight, style=Style(color=tokens.get("teal")))
            if after:
                text.append(after, style=Style(color=tokens.get("fg")))
        else:
            text.append(question, style=Style(color=tokens.get("fg")))
        if self.entry.multiple:
            text.append(select_all_hint(), style=Style(color=tokens.get("dim")))
        return text


class _DescriptionLine(Static):
    """Dim per-option help line beneath a decision row (donor opt.description)."""

    DEFAULT_CSS = """
    _DescriptionLine {
        width: 100%;
        height: auto;
        color: $dim;
    }
    """

    def __init__(self, entry: NeedsYouEntry, choice: NeedsYouChoice, index: int) -> None:
        super().__init__(
            Text(option_description_line(choice)), id=f"desc-{entry.decision_id}-{index}"
        )


class _DecisionWhy(Static):
    """Dim ``    why · <reason>`` line under one decision row.

    Presentation only — not a click target (like the header)."""

    DEFAULT_CSS = """
    _DecisionWhy {
        width: 100%;
        height: 1;
        color: $dim;
    }
    """

    def __init__(self, entry: NeedsYouEntry) -> None:
        super().__init__(Text(decision_why_line(entry.reason)), id=f"why-{entry.decision_id}")
        self.entry = entry


class _DecisionRow(Horizontal):
    """One numbered decision line with its inline chips."""

    DEFAULT_CSS = """
    _DecisionRow {
        width: 100%;
        height: auto;
    }
    _DecisionRow.-wrapped { layout: vertical; }
    _DecisionRow.-wrapped _DecisionText { width: 100%; height: auto; }
    """

    def __init__(self, entry: NeedsYouEntry, number: int, selected: frozenset[int]) -> None:
        super().__init__(id=f"needs-you-row-{entry.decision_id}")
        self.entry = entry
        self.number = number
        self._selected = selected

    def compose(self):
        yield _DecisionText(self.entry, self.number)
        for index, choice in enumerate(self.entry.choices):
            yield _ChoiceChip(self.entry, choice, index, selected=index in self._selected)
        if self.entry.custom:
            yield _CustomChip(self.entry)
        if self.entry.multiple:
            yield _SubmitChip(self.entry, len(self._selected))

    def on_resize(self, event: events.Resize) -> None:
        self._update_wrap()

    def _update_wrap(self) -> None:
        """Drop the chips onto their own rows when one row can't fit all."""
        width = self.container_size.width
        if width <= 0:
            return
        needed = cell_len(decision_number_text(self.number)) + cell_len(self.entry.question)
        if self.entry.multiple:
            needed += cell_len(select_all_hint())
        for index, choice in enumerate(self.entry.choices):
            text = (
                multi_chip_text(choice, selected=index in self._selected)
                if self.entry.multiple
                else chip_text(choice)
            )
            needed += cell_len(text) + 2  # each chip carries ``margin-left: 2``
        if self.entry.custom:
            needed += cell_len(custom_chip_text()) + 2
        if self.entry.multiple:
            needed += cell_len(submit_chip_text(len(self._selected))) + 2
        self.set_class(needed > width, "-wrapped")

    def on_click(self, event: events.Click) -> None:
        # Mockup showNeedsYou: clicking anywhere on the row acts on THIS
        # decision. For a single-select it answers the first choice; for a
        # multi-select a bare row click is ambiguous, so it is a no-op (the
        # chips/submit own the interaction).
        event.stop()
        if not self.entry.multiple and self.entry.choices:
            self.post_message(
                NeedsYouList.DecisionTaken(self.entry.decision_id, self.entry.choices[0].answer)
            )


class NeedsYouList(Vertical):
    """Transcript-block-style needs-you list (DESIGN-SPEC §7).

    Feed it a :class:`NeedsYouBlock` via :meth:`update_block`. Chip
    clicks (or :meth:`take_decision` for keyboard paths) post
    :class:`DecisionTaken`; the app applies the answer, logs
    ``Applying decision: …`` and clears the footer badge.

    Multi-select decisions accumulate a per-decision selection that a
    ``[submit]`` chip (or Enter, when focused) finalizes into one comma-joined
    answer; ``custom`` decisions offer a free-text chip that posts
    :class:`CustomAnswerRequested`.
    """

    DEFAULT_CSS = """
    NeedsYouList {
        width: 100%;
        height: auto;
    }
    """

    can_focus = True
    """Taking the keyboard (on the deliberate ctrl-y "review decisions" open)
    lets number keys answer/toggle -- the terminal-native donor number-key
    flow. Like the evidence block, this is an intentional keyboard grab, not a
    stray transcript click."""

    class DecisionTaken(Message):
        """The human acted on a deferred decision (chip / row / submit)."""

        def __init__(self, item_id: str, choice: str) -> None:
            self.item_id = item_id
            self.choice = choice
            super().__init__()

    class ChoiceToggled(Message):
        """A multi-select checkbox was toggled (not yet submitted)."""

        def __init__(self, item_id: str, index: int) -> None:
            self.item_id = item_id
            self.index = index
            super().__init__()

    class SubmitRequested(Message):
        """The multi-select ``[submit]`` chip was activated."""

        def __init__(self, item_id: str) -> None:
            self.item_id = item_id
            super().__init__()

    class CustomAnswerRequested(Message):
        """The ``[+ type your own]`` free-text affordance was activated."""

        def __init__(self, item_id: str) -> None:
            self.item_id = item_id
            super().__init__()

    def __init__(self, block: NeedsYouBlock | None = None, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._block = block
        self._selected: dict[str, set[int]] = {}

    @property
    def block(self) -> NeedsYouBlock | None:
        return self._block

    @property
    def header_text(self) -> str:
        """The exact header line currently displayed (empty when no block)."""
        if self._block is None:
            return ""
        return needs_you_header_line(len(self._block.items))

    def selected_labels(self, item_id: str) -> tuple[str, ...]:
        """The labels currently toggled on for *item_id*, in option order."""
        entry = self._entry(item_id)
        if entry is None:
            return ()
        chosen = self._selected.get(item_id, set())
        return tuple(choice.label for index, choice in enumerate(entry.choices) if index in chosen)

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

    def on_needs_you_list_choice_toggled(self, message: NeedsYouList.ChoiceToggled) -> None:
        message.stop()  # handled here; do not leak the intermediate toggle to the app
        self.toggle_choice(message.item_id, message.index)

    def on_needs_you_list_submit_requested(self, message: NeedsYouList.SubmitRequested) -> None:
        message.stop()
        self.submit_selection(message.item_id)

    def toggle_choice(self, item_id: str, index: int) -> None:
        """Flip a multi-select choice in/out of the pending selection."""
        entry = self._entry(item_id)
        if entry is None or not entry.multiple or not (0 <= index < len(entry.choices)):
            return
        chosen = self._selected.setdefault(item_id, set())
        if index in chosen:
            chosen.discard(index)
        else:
            chosen.add(index)
        self._rebuild()

    def submit_selection(self, item_id: str) -> None:
        """Finalize a multi-select decision as one comma-joined answer."""
        labels = self.selected_labels(item_id)
        if labels:
            self.post_message(self.DecisionTaken(item_id, multi_answer(labels)))

    def on_key(self, event: events.Key) -> None:
        """Keyboard answering for the FIRST (topmost) pending decision -- the
        common single-question case. Number keys pick/toggle an option; Enter
        submits a multi-select. Only fires while this block holds the keyboard."""
        if self._block is None or not self._block.items:
            return
        entry = self._block.items[0]
        if event.key == "enter":
            if entry.multiple:
                event.stop()
                self.submit_selection(entry.decision_id)
            return
        if event.key.isdigit() and event.key != "0":
            index = int(event.key) - 1
            if 0 <= index < len(entry.choices):
                event.stop()
                if entry.multiple:
                    self.toggle_choice(entry.decision_id, index)
                else:
                    self.take_decision(entry.decision_id, entry.choices[index].answer)
            elif index == len(entry.choices) and entry.custom:
                event.stop()
                self.post_message(self.CustomAnswerRequested(entry.decision_id))

    def _entry(self, item_id: str) -> NeedsYouEntry | None:
        if self._block is None:
            return None
        for entry in self._block.items:
            if entry.decision_id == item_id:
                return entry
        return None

    def _rebuild(self) -> None:
        # remove_children is asynchronous: await it before remounting so
        # rebuilt rows never collide with the ids of outgoing ones.
        self.call_later(self._remount_rows)

    async def _remount_rows(self) -> None:
        await self.remove_children()
        if self._block is None or not self._block.items:
            return
        rows: list[Static | Horizontal] = [_NeedsYouHeader(len(self._block.items))]
        for number, entry in enumerate(self._block.items, start=1):
            selected = frozenset(self._selected.get(entry.decision_id, set()))
            rows.append(_DecisionRow(entry, number, selected))
            for index, choice in enumerate(entry.choices):
                if choice.description:
                    rows.append(_DescriptionLine(entry, choice, index))
            if entry.reason:
                rows.append(_DecisionWhy(entry))
        await self.mount(*rows)


__all__ = [
    "NeedsYouList",
    "applying_decision_line",
    "chip_text",
    "custom_chip_text",
    "decision_number_text",
    "decision_why_line",
    "focused_lane_banner",
    "focused_lane_banner_parts",
    "multi_answer",
    "multi_chip_text",
    "needs_you_header",
    "needs_you_header_line",
    "option_description_line",
    "select_all_hint",
    "submit_chip_text",
]
