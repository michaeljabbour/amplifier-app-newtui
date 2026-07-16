"""Inline approval bar (DESIGN-SPEC §2 item 4, §7).

Replaces the composer while an approval is pending: ``Approval required
·`` (orange bold) + the prompt + the options, selected option prefixed
``› `` and shown bright on ``bg-tab``; Deny is red while unselected.

Keyboard (the bar owns the keyboard while open — keymap ``approval``
context): left/up and right/down/tab cycle, Enter confirms, Esc = Deny.
Clicking an option confirms it directly. Resolution is emitted as
:class:`ApprovalBar.Resolved(ticket_id, choice)` — the app routes it
back to the kernel approval broker.
"""

from __future__ import annotations

from textual import events
from textual.containers import Horizontal
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

APPROVAL_LABEL = "Approval required ·"
DEFAULT_OPTIONS: tuple[str, ...] = ("Allow once", "Allow always", "Deny")
"""Verbatim Rust fail-closed option strings (ADR-0007 approvals)."""

SELECTED_PREFIX = "› "
DENY_OPTION = "Deny"

_PREV_KEYS = frozenset({"left", "up"})
_NEXT_KEYS = frozenset({"right", "down", "tab"})


class ApprovalOption(Static):
    """One clickable option chip."""

    DEFAULT_CSS = """
    ApprovalOption {
        width: auto;
        height: 1;
        color: $dim;
        padding: 0 1;
    }
    ApprovalOption.-deny { color: $red; }
    ApprovalOption.-selected {
        color: $bright;
        background: $bg-tab;
    }
    """

    def __init__(self, index: int, label: str) -> None:
        super().__init__("", markup=False)
        self.index = index
        self.label = label

    def paint(self, selected: bool) -> None:
        prefix = SELECTED_PREFIX if selected else ""
        self.update(f"{prefix}{self.label}")
        self.set_class(selected, "-selected")
        self.set_class(self.label == DENY_OPTION and not selected, "-deny")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(ApprovalBar.OptionClicked(self.index))


class ApprovalBar(Horizontal):
    """The approval strip. Focus it when shown; it owns the keyboard."""

    can_focus = True

    DEFAULT_CSS = """
    ApprovalBar {
        width: 100%;
        height: 1;
        background: $bg-chrome;
        padding: 0 1;
    }
    ApprovalBar > .approval-label {
        width: auto;
        height: 1;
        color: $orange;
        text-style: bold;
        padding: 0 1 0 0;
    }
    ApprovalBar > .approval-prompt {
        width: auto;
        height: 1;
        color: $fg;
        padding: 0 2 0 0;
    }
    """

    selected: reactive[int] = reactive(0)

    class Resolved(Message):
        """The user answered: *choice* is the verbatim option string."""

        def __init__(self, ticket_id: str, choice: str) -> None:
            self.ticket_id = ticket_id
            self.choice = choice
            super().__init__()

    class OptionClicked(Message):
        """Internal: an option chip was clicked (confirms that option)."""

        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    def __init__(
        self,
        ticket_id: str,
        prompt: str,
        options: tuple[str, ...] = DEFAULT_OPTIONS,
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        if not options:
            raise ValueError("ApprovalBar needs at least one option")
        super().__init__(id=id, classes=classes)
        self.ticket_id = ticket_id
        self.prompt = prompt
        self.options = options
        self._option_widgets = [
            ApprovalOption(index, label) for index, label in enumerate(options)
        ]

    def compose(self):
        yield Static(APPROVAL_LABEL, classes="approval-label")
        yield Static(
            Content.from_markup("$prompt", prompt=self.prompt),
            classes="approval-prompt",
        )
        yield from self._option_widgets

    def on_mount(self) -> None:
        self._paint_options()

    # -- rendered strings (tests assert on these) ----------------------------

    def option_texts(self) -> tuple[str, ...]:
        """Plain option strings as rendered (``› `` prefix on selected)."""
        return tuple(
            (SELECTED_PREFIX if index == self.selected else "") + label
            for index, label in enumerate(self.options)
        )

    # -- interaction ----------------------------------------------------------

    def watch_selected(self, _selected: int) -> None:
        self._paint_options()

    def on_key(self, event: events.Key) -> None:
        if event.key in _PREV_KEYS:
            event.stop()
            event.prevent_default()
            self.selected = (self.selected - 1) % len(self.options)
        elif event.key in _NEXT_KEYS:
            event.stop()
            event.prevent_default()
            self.selected = (self.selected + 1) % len(self.options)
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            self._resolve(self.options[self.selected])
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self._resolve(self._deny_choice())

    def on_approval_bar_option_clicked(self, message: OptionClicked) -> None:
        message.stop()
        self.selected = message.index
        self._resolve(self.options[message.index])

    # -- internals ---------------------------------------------------------------

    def _deny_choice(self) -> str:
        return DENY_OPTION if DENY_OPTION in self.options else self.options[-1]

    def _resolve(self, choice: str) -> None:
        self.post_message(self.Resolved(self.ticket_id, choice))

    def _paint_options(self) -> None:
        for widget in self._option_widgets:
            widget.paint(widget.index == self.selected)


__all__ = [
    "APPROVAL_LABEL",
    "ApprovalBar",
    "ApprovalOption",
    "DEFAULT_OPTIONS",
    "DENY_OPTION",
    "SELECTED_PREFIX",
]
