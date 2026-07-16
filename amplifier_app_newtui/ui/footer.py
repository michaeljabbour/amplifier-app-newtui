"""Footer status bar (DESIGN-SPEC §2 item 6).

Left segment: ``mode <mode>`` (mode color) ``· <trust> · <bundle> ·
<session-short> · $<cost>`` plus the green ``▲`` yield glyph when the
last turn shipped and ``· q1`` when a next-turn message is queued; an
optional orange, clickable ``N decisions waiting · ctrl-y`` badge.

Right segment: context-sensitive hints — the EXACT strings from
``keymap.FOOTER_HINTS``, except the running hint which is composed live
from :func:`keymap.hint_label` so the advertised queue chord swaps to
``alt+enter`` on terminals without the kitty keyboard protocol.

All rendering is a pure function of :class:`FooterState` — the widget is
a dumb painter, which is what the tests assert against.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from textual import events
from textual.containers import Horizontal
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from ..model.blocks import GLYPH_YIELD
from ..model.modes import ModeId, get_mode
from .keymap import FOOTER_HINTS, Context, hint_label

SEPARATOR = " · "


class FooterState(BaseModel):
    """Everything the footer needs to paint, as one frozen value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode_id: ModeId = "chat"
    bundle: str = ""
    session_short: str = ""
    cost: Decimal = Field(default=Decimal("0"), ge=0)
    shipped: bool = False
    """True when the last turn shipped → green ``▲`` yield glyph."""
    queued: int = Field(default=0, ge=0)
    """Queued next-turn messages → ``· qN`` marker."""
    waiting: int = Field(default=0, ge=0)
    """Deferred needs-you decisions → orange ``N decisions waiting · ctrl-y``."""
    context: Context = "idle"
    """Which hint set the right segment shows."""
    kitty_protocol: bool = True
    """Terminal probe result; False swaps shift+enter → alt+enter in hints."""


# -- pure text builders (exact strings; tests assert on these) ---------------


def footer_left_text(state: FooterState) -> str:
    """The full left segment as plain text."""
    mode = get_mode(state.mode_id)
    parts = [f"mode {mode.id}", mode.trust_str]
    if state.bundle:
        parts.append(state.bundle)
    if state.session_short:
        parts.append(state.session_short)
    cost_part = f"${state.cost:.2f}"
    if state.shipped:
        cost_part += f" {GLYPH_YIELD}"
    parts.append(cost_part)
    if state.queued:
        parts.append(f"q{state.queued}")
    return SEPARATOR.join(parts)


def footer_waiting_text(state: FooterState) -> str:
    """The waiting badge text; empty when nothing is deferred."""
    if not state.waiting:
        return ""
    plural = "s" if state.waiting != 1 else ""
    return f"{state.waiting} decision{plural} waiting · ctrl-y"


def footer_right_text(state: FooterState) -> str:
    """Context-sensitive hints (exact DESIGN-SPEC §2 strings)."""
    if state.context == "running":
        overrides = None if state.kitty_protocol else {"queue_message": "alt+enter"}
        queue_chord = hint_label("queue_message", overrides)
        return f"esc interrupt · enter steer · {queue_chord} queue"
    return FOOTER_HINTS.get(state.context, FOOTER_HINTS["idle"])


# -- widgets -------------------------------------------------------------------


class _WaitingBadge(Static):
    """The clickable orange decisions-waiting badge."""

    DEFAULT_CSS = """
    _WaitingBadge {
        width: auto;
        height: 1;
        color: $orange;
        padding: 0 1;
        display: none;
    }
    _WaitingBadge.-visible { display: block; }
    """

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(FooterBar.WaitingBadgeClicked())


class FooterBar(Horizontal):
    """The bottom chrome strip. Call :meth:`update_state` to repaint."""

    DEFAULT_CSS = """
    FooterBar {
        dock: bottom;
        width: 100%;
        height: 1;
        background: $bg-chrome;
        color: $dim;
        padding: 0 1;
    }
    FooterBar > #footer-left { width: auto; height: 1; }
    FooterBar > #footer-right {
        width: 1fr;
        height: 1;
        color: $dimmer;
        text-align: right;
    }
    """

    class WaitingBadgeClicked(Message):
        """The ``N decisions waiting · ctrl-y`` badge was clicked."""

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._state = FooterState()
        self._left = Static(id="footer-left")
        self._badge = _WaitingBadge()
        self._right = Static(id="footer-right")

    def compose(self):
        yield self._left
        yield self._badge
        yield self._right

    def on_mount(self) -> None:
        self._repaint()

    @property
    def state(self) -> FooterState:
        return self._state

    def update_state(self, state: FooterState) -> None:
        self._state = state
        self._repaint()

    def _repaint(self) -> None:
        state = self._state
        mode = get_mode(state.mode_id)

        # Left: "mode <id>" in mode color, rest dim, ▲ green.
        rest_parts: list[str] = [mode.trust_str]
        if state.bundle:
            rest_parts.append(state.bundle)
        if state.session_short:
            rest_parts.append(state.session_short)
        cost_part = f"${state.cost:.2f}"
        rest = SEPARATOR + SEPARATOR.join([*rest_parts, cost_part])
        markup = f"[${mode.color_token}]$mode_part[/]$rest"
        if state.shipped:
            markup += f" [$green]{GLYPH_YIELD}[/]"
        if state.queued:
            markup += f"{SEPARATOR}q{state.queued}"
        self._left.update(
            Content.from_markup(markup, mode_part=f"mode {mode.id}", rest=rest)
        )

        badge_text = footer_waiting_text(state)
        self._badge.set_class(bool(badge_text), "-visible")
        self._badge.update(Content.from_markup("$badge", badge=badge_text))

        self._right.update(
            Content.from_markup("$hints", hints=footer_right_text(state))
        )


__all__ = [
    "FooterBar",
    "FooterState",
    "footer_left_text",
    "footer_right_text",
    "footer_waiting_text",
]
