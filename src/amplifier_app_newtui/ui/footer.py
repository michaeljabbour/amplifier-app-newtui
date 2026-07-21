"""Footer status bar (DESIGN-SPEC §2 item 6).

Left segment: ``mode <mode>`` (mode color) ``· <trust> · <bundle> ·
<session-short> · $<cost>`` — segment text dim, the inline ``·``
separators dimmer (mockup: each is its own ``--dimmer`` span) — plus
the green ``▲`` yield glyph when the
last turn shipped and an orange ``· q1`` when a next-turn message is
queued; an optional orange, clickable ``N decisions waiting · ctrl-y``
badge preceded by a dimmer ``·`` separator.

Right segment: context-sensitive hints — the EXACT strings from
``keymap.FOOTER_HINTS``, except the running hint which is composed live
from :func:`keymap.hint_label` so the advertised queue chord swaps to
``alt+enter`` on terminals without the kitty keyboard protocol.

Like the mockup's ``flex-wrap: wrap`` footer, when both segments do not
fit on one row the hints drop to their own full-width second row instead
of clipping; when the left segment plus the waiting badge still exceed
the width, the badge drops to its own row too (separator hidden) so the
``ctrl-y`` affordance stays fully readable.

All rendering is a pure function of :class:`FooterState` — the widget is
a dumb painter, which is what the tests assert against.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from rich.cells import cell_len
from textual import events
from textual.containers import Horizontal
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from ..model.blocks import GLYPH_YIELD
from ..model.modes import ModeId, get_mode
from .keymap import FOOTER_HINTS, Context, hint_label

SEPARATOR = " · "

_SEGMENT_GAP = 2
"""Minimum cells between the left segment and the right hints before wrapping."""


class FooterState(BaseModel):
    """Everything the footer needs to paint, as one frozen value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode_id: ModeId = "chat"
    bundle: str = ""
    session_short: str = ""
    cost: Decimal = Field(default=Decimal("0"), ge=0)
    cost_estimated: bool = False
    """True when any usage this session was unpriceable → the total is a
    floor, rendered ``~$1.23`` (never lie in the footer)."""
    shipped: bool = False
    """True when the last turn shipped → green ``▲`` yield glyph."""
    queued: int = Field(default=0, ge=0)
    """Queued next-turn messages → orange ``· qN`` marker."""
    waiting: int = Field(default=0, ge=0)
    """Deferred needs-you decisions → orange ``N decisions waiting · ctrl-y``."""
    plan_done: int = Field(default=0, ge=0)
    plan_total: int = Field(default=0, ge=0)
    """Plan fallback count — non-zero only while the plan panel is hidden
    (narrow terminal); the footer then carries ``Plan N/M`` (design D2)."""
    context: Context = "idle"
    """Which hint set the right segment shows."""
    kitty_protocol: bool = True
    """Terminal probe result; False swaps shift+enter → alt+enter in hints."""


# -- pure text builders (exact strings; tests assert on these) ---------------


def _left_parts(
    state: FooterState, *, trust: bool = True, bundle: bool = True, session: bool = True
) -> list[str]:
    """The left-segment parts, with decorative ones optionally dropped."""
    mode = get_mode(state.mode_id)
    parts = [f"mode {mode.id}"]
    if trust:
        parts.append(mode.trust_str)
    if bundle and state.bundle:
        parts.append(state.bundle)
    if session and state.session_short:
        parts.append(state.session_short)
    cost_part = f"{'~' if state.cost_estimated else ''}${state.cost:.2f}"
    if state.shipped:
        cost_part += f" {GLYPH_YIELD}"
    parts.append(cost_part)
    if state.queued:
        parts.append(f"q{state.queued}")
    if state.plan_total:
        parts.append(f"Plan {state.plan_done}/{state.plan_total}")
    return parts


def footer_left_text(state: FooterState) -> str:
    """The full left segment as plain text."""
    return SEPARATOR.join(_left_parts(state))


_FIT_LADDER: tuple[dict[str, bool], ...] = (
    {"trust": False},
    {"trust": False, "session": False},
    {"trust": False, "session": False, "bundle": False},
)
"""Decorations in drop order: trust posture (the mode chip keeps the id),
then session id, then bundle. Mode, cost, queue and ``Plan n/m`` never drop
— design D2's footer fallback only works if the plan count survives."""


def _fit_drops(state: FooterState, width: int) -> dict[str, bool]:
    """The mildest ladder step whose left text fits *width* cells."""
    if width <= 0 or cell_len(footer_left_text(state)) <= width:
        return {}
    for drops in _FIT_LADDER:
        if cell_len(SEPARATOR.join(_left_parts(state, **drops))) <= width:
            return drops
    return dict(_FIT_LADDER[-1])


def footer_left_text_fit(state: FooterState, width: int) -> str:
    """The left segment, decorations dropped until it fits *width* cells.

    Found live in forge at 80 cols: the full segment overflowed and the
    terminal clipped ``Plan n/m`` — the one part the narrow-width ladder
    exists to show. ``width <= 0`` (pre-layout) returns the full string.
    """
    return SEPARATOR.join(_left_parts(state, **_fit_drops(state, width)))


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


class _WaitingBadgeSeparator(Static):
    """The dimmer ``·`` between the left segment and the waiting badge."""

    DEFAULT_CSS = """
    _WaitingBadgeSeparator {
        width: auto;
        height: 1;
        color: $dimmer;
        padding: 0 1;
        display: none;
    }
    _WaitingBadgeSeparator.-visible { display: block; }
    """


class _WaitingBadge(Static):
    """The clickable orange decisions-waiting badge."""

    DEFAULT_CSS = """
    _WaitingBadge {
        width: auto;
        height: 1;
        color: $orange;
        padding: 0 1 0 0;
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
        height: auto;
        background: $bg-chrome;
        color: $dim;
        padding: 0 1;
    }
    FooterBar > #footer-left-group { width: auto; height: 1; }
    #footer-left-group > #footer-left { width: auto; height: 1; }
    FooterBar > #footer-right {
        width: 1fr;
        height: 1;
        color: $dimmer;
        text-align: right;
    }
    FooterBar.-wrapped { layout: vertical; }
    FooterBar.-wrapped > #footer-right { width: 100%; }
    FooterBar.-badge-wrapped > #footer-left-group {
        layout: vertical;
        height: auto;
    }
    FooterBar.-badge-wrapped _WaitingBadgeSeparator { display: none; }
    """

    class WaitingBadgeClicked(Message):
        """The ``N decisions waiting · ctrl-y`` badge was clicked."""

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._state = FooterState()
        self._left = Static(id="footer-left")
        self._badge_sep = _WaitingBadgeSeparator("·")
        self._badge = _WaitingBadge()
        self._right = Static(id="footer-right")

    def compose(self):
        with Horizontal(id="footer-left-group"):
            yield self._left
            yield self._badge_sep
            yield self._badge
        yield self._right

    def on_mount(self) -> None:
        self._repaint()

    def on_resize(self, event: events.Resize) -> None:
        self._repaint()  # width changed: decorations may (re)appear or drop

    @property
    def state(self) -> FooterState:
        return self._state

    def update_state(self, state: FooterState) -> None:
        self._state = state
        self._repaint()

    def _update_wrap(self) -> None:
        """Drop the hints onto their own row when one row can't fit both.

        Mirrors the mockup footer's ``flex-wrap: wrap`` — segments stay
        fully readable instead of the right hints clipping off-screen.
        """
        width = self.container_size.width
        if width <= 0:
            return
        state = self._state
        group_needed = cell_len(footer_left_text_fit(state, width))
        badge_text = footer_waiting_text(state)
        if badge_text:
            # dimmer "·" separator (padding 0 1) + badge (padding-right 1)
            group_needed += 3 + cell_len(badge_text) + 1
        needed = group_needed + _SEGMENT_GAP + cell_len(footer_right_text(state))
        self.set_class(needed > width, "-wrapped")
        self.set_class(bool(badge_text) and group_needed > width, "-badge-wrapped")

    def _repaint(self) -> None:
        state = self._state
        mode = get_mode(state.mode_id)

        # Left: "mode <id>" in mode color, segments dim with dimmer "·"
        # separators (mockup: each inline "·" is its own --dimmer span),
        # ▲ green, · qN orange.
        drops = _fit_drops(state, self.container_size.width)
        rest_parts: list[str] = []
        if drops.get("trust", True):
            rest_parts.append(mode.trust_str)
        if drops.get("bundle", True) and state.bundle:
            rest_parts.append(state.bundle)
        if drops.get("session", True) and state.session_short:
            rest_parts.append(state.session_short)
        rest_parts.append(f"{'~' if state.cost_estimated else ''}${state.cost:.2f}")
        markup = f"[${mode.color_token}]$mode_part[/]"
        substitutions = {"mode_part": f"mode {mode.id}"}
        for index, part in enumerate(rest_parts):
            key = f"part{index}"
            markup += f"[$dimmer]{SEPARATOR}[/]${key}"
            substitutions[key] = part
        if state.shipped:
            markup += f" [$green]{GLYPH_YIELD}[/]"
        if state.queued:
            markup += f"[$orange]{SEPARATOR}q{state.queued}[/]"
        if state.plan_total:
            markup += f"[$dimmer]{SEPARATOR}[/][$dim]$plan_part[/]"
            substitutions["plan_part"] = f"Plan {state.plan_done}/{state.plan_total}"
        self._left.update(Content.from_markup(markup, **substitutions))

        badge_text = footer_waiting_text(state)
        self._badge_sep.set_class(bool(badge_text), "-visible")
        self._badge.set_class(bool(badge_text), "-visible")
        self._badge.update(Content.from_markup("$badge", badge=badge_text))

        self._right.update(Content.from_markup("$hints", hints=footer_right_text(state)))
        self._update_wrap()


__all__ = [
    "FooterBar",
    "FooterState",
    "footer_left_text",
    "footer_left_text_fit",
    "footer_right_text",
    "footer_waiting_text",
]
