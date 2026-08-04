"""Footer status bar (DESIGN-SPEC §2 item 6).

Left segment: ``mode <mode>`` (mode color) ``· <trust> · <model> ·
<session-short> · $<cost>`` — segment text dim, the inline ``·``
separators dimmer (mockup: each is its own ``--dimmer`` span) — plus
the green ``▲`` yield glyph when the
last turn shipped and an orange ``· q1`` when a next-turn message is
queued; an optional orange, clickable ``N decisions waiting · ctrl-y``
badge preceded by a dimmer ``·`` separator.

Right segment: context-sensitive hints — the EXACT strings from
``keymap.FOOTER_HINTS``, except the running hint (composed live from
:func:`keymap.hint_label` so the advertised queue chord swaps to
``alt+enter`` on terminals without the kitty keyboard protocol) and the
``idle`` hint, which is conditionally empty (item D4 below): plain "" the
moment nothing state-tied is available to act on, but ``ctrl-r rewind``
once :attr:`FooterState.rewind_available` is True (S1 AC1 — "the footer
exposes the rewind shortcut in plain language when the action is
available" — reconciled post-merge with D4 AC2/AC3 below; see
``test_ui_footer.py`` for the pinned reconciliation).

Like the mockup's ``flex-wrap: wrap`` footer, when both segments do not
fit on one row the hints drop to their own full-width second row instead
of clipping; when the left segment plus the waiting badge still exceed
the width, the badge drops to its own row too (separator hidden) so the
``ctrl-y`` affordance stays fully readable.

All rendering is a pure function of :class:`FooterState` — the widget is
a dumb painter, which is what the tests assert against.

Structural seam (compliance 2026-08-02, item D2 — David Koleczek's UX
review, July 31 2026). The composer and this persistent status band used
to share the same ``$bg-chrome`` fill with nothing between them, reading
as one tight visual band. ``FooterBar`` now owns a permanent
``border-top: solid $rule`` hairline in ``DEFAULT_CSS`` — unconditional,
so it survives every footer state (idle, ``running``, ``-wrapped``,
``-badge-wrapped``) and every theme, and it is independent of whatever
currently occupies the composer slot above it (the composer itself, or
the :class:`~.approval_bar.ApprovalBar` that temporarily replaces it —
see ``app_support.mount_approval``). It is a real border, not simulated
with blank rows, so it reads as a boundary even with color off (a shape
cue, not a hue cue).

Item D4 (footer hint + bundle-metadata consolidation, same review) landed
INSIDE this seam rather than widening it — it removes content, it does
not add a row. The footer no longer paints a ``bundle`` part at all:
:class:`~.chrome.TitleBar` is now the ONE persistent place the active
bundle renders (AC1 — David preferred it kept at the top, "the footer is
already crowded"). ``FooterState`` accordingly carries no ``bundle``
field — one view model, one region, never two copies of the same fact to
keep in sync. The generic ``idle`` hint (history/newline/rewind/commands,
previously shown on literally every idle frame) also moved out, to
:data:`keymap.COMPOSER_PLACEHOLDER` and the new ``/keys`` command
(:func:`keymap.help_rows`) — see ``keymap.FOOTER_HINTS`` for the
reasoning. This box now carries only transient status, attention,
model/mode and immediately-available actions (AC3).

Post-merge compliance audit (Finding 1): removing ``ctrl-r rewind`` from
the generic idle hint accidentally broke item S1's own AC1, which
requires the footer to expose that shortcut in plain language once it is
genuinely available. :func:`footer_right_text` now restores exactly that
one chord — and only that one — the moment :attr:`FooterState.
rewind_available` says checkpoints actually exist; it is still absent the
rest of the time (a fresh session, or while the rewind picker itself is
already open), so this is a state-tied, immediately-available action
(AC3) rather than the always-on reminder row AC2 removed.
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
from ..model.formatting import format_tokens_compact
from ..model.modes import ModeId, effective_trust_str, get_mode
from ..model.native_modes import native_badge_text
from .keymap import FOOTER_HINTS, Context, hint_label

SEPARATOR = " · "

_SEGMENT_GAP = 2
"""Minimum cells between the left segment and the right hints before wrapping."""


class FooterState(BaseModel):
    """Everything the footer needs to paint, as one frozen value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode_id: ModeId = "chat"
    gated_auto: bool = False
    """Whether auto-mode gating is armed (``permissions.governance: gated``) —
    auto's posture string renders truthfully from this."""
    native_modes: tuple[str, ...] = ()
    """Active bundle-composed modes (``/mode <name>``), in activation order —
    the LAST is the primary (the one enforced upstream). Shown as a
    ``◆ <primary> +<others>`` badge next to the posture so activation is
    visible and sticky. A single active mode renders exactly as the old
    single-slot badge did (backward compatible)."""
    model: str = ""
    """Primary model id, already bare (``claude-fable-5``, no provider
    prefix) — its own dim part between the bundle and the session."""
    effort: str | None = None
    """Reasoning-effort tier (``none``…``xhigh``) shown as an ``effort <tier>``
    part just before the cost. ``None`` = unset/default — the segment is
    omitted entirely, so an untouched session keeps the lean footer (the
    ctrl+b cycle is what first surfaces it). Mirrors the backend's null-vs-"none"
    distinction: null hides the indicator, an explicit ``none`` shows it."""
    session_short: str = ""
    cost: Decimal = Field(default=Decimal("0"), ge=0)
    cost_estimated: bool = False
    """True when any usage this session was unpriceable → the total is a
    floor, rendered ``~$1.23`` (never lie in the footer)."""
    context_pct: int | None = None
    """Context occupancy as a whole-number % of the real window (the same
    ``compaction.max_tokens`` window ``/context`` meters against). ``None``
    before any usage, or when the window is unknown (donor parity:
    ``model.limit.context ? … : null`` — omit the %, never guess a
    denominator)."""
    context_tokens: int | None = None
    """Context tokens used — the last provider response’s occupancy
    (donor ``last.tokens.*`` sum). ``None`` before any usage; rendered
    alone (``12.4k ctx``) when the window is unknown so the readout is
    still honest without a percentage."""
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
    rewind_available: bool = False
    """At least one rewind checkpoint exists AND the rewind picker itself
    is not already open — the idle hint surfaces ``ctrl-r rewind`` only
    then (S1 AC1: "the footer exposes the rewind shortcut in plain
    language when the action is available"), never as an always-on
    reminder (D4 AC2/AC3)."""


# -- pure text builders (exact strings; tests assert on these) ---------------


def _context_part(state: FooterState) -> str:
    """The live context readout (donor: context tokens + % of the window).

    ``NN% ctx`` when a window/percentage is known; the compact token count
    alone (``12.4k ctx``) when the window is unknown (honest — omit the %,
    never a guessed denominator); empty before any usage exists.
    """
    if state.context_pct is not None:
        return f"{state.context_pct}% ctx"
    if state.context_tokens is not None:
        return f"{format_tokens_compact(state.context_tokens)} ctx"
    return ""


def _left_parts(
    state: FooterState,
    *,
    trust: bool = True,
    model: bool = True,
    session: bool = True,
) -> list[str]:
    """The left-segment parts, with decorative ones optionally dropped."""
    mode = get_mode(state.mode_id)
    parts = [f"mode {mode.id}"]
    badge = native_badge_text(state.native_modes)
    if badge:
        parts.append(badge)
    if trust:
        parts.append(effective_trust_str(mode, gated_auto=state.gated_auto))
    if model and state.model:
        parts.append(state.model)
    if session and state.session_short:
        parts.append(state.session_short)
    if state.effort is not None:
        parts.append(f"effort {state.effort}")
    ctx_part = _context_part(state)
    if ctx_part:
        parts.append(ctx_part)
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
    {"trust": False, "session": False, "model": False},
)
"""Decorations in drop order: trust posture (the mode chip keeps the id),
then session id, then the model — the model is the identity users
actually ask about, so it outlives the other decorations (story #4). The
bundle is no longer part of this ladder (item D4): it doesn't ride the
footer's left segment at all any more, so there is nothing left here to
drop. Mode, cost, queue and ``Plan n/m`` never drop — design D2's footer
fallback only works if the plan count survives."""


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
    """Context-sensitive hints (exact DESIGN-SPEC §2 strings).

    ``idle`` is the one hint composed live rather than read verbatim from
    :data:`FOOTER_HINTS`: it stays the empty string from that table unless
    :attr:`FooterState.rewind_available` says the ctrl-r chord genuinely
    has something to do right now, in which case it surfaces exactly that
    one chord — never the old always-on reminder row (S1 AC1 × D4 AC2/
    AC3; see the module docstring and ``test_ui_footer.py``).
    """
    if state.context == "running":
        overrides = None if state.kitty_protocol else {"queue_message": "alt+enter"}
        queue_chord = hint_label("queue_message", overrides)
        return f"esc interrupt · enter steer · {queue_chord} queue"
    if state.context == "idle" and state.rewind_available:
        return f"{hint_label('open_rewind')} rewind"
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
    """The bottom chrome strip. Call :meth:`update_state` to repaint.

    Owns the D2 composer/status seam: an unconditional ``border-top`` in
    ``DEFAULT_CSS`` below (see the module docstring) that never depends on
    ``FooterState`` or the ``-wrapped``/``-badge-wrapped`` classes.
    """

    DEFAULT_CSS = """
    FooterBar {
        dock: bottom;
        width: 100%;
        height: auto;
        background: $bg-chrome;
        color: $dim;
        padding: 0 1;
        border-top: solid $rule;
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
            rest_parts.append(effective_trust_str(mode, gated_auto=state.gated_auto))
        if drops.get("model", True) and state.model:
            rest_parts.append(state.model)
        if drops.get("session", True) and state.session_short:
            rest_parts.append(state.session_short)
        if state.effort is not None:
            rest_parts.append(f"effort {state.effort}")
        ctx_part = _context_part(state)
        if ctx_part:
            rest_parts.append(ctx_part)
        rest_parts.append(f"{'~' if state.cost_estimated else ''}${state.cost:.2f}")
        markup = f"[${mode.color_token}]$mode_part[/]"
        substitutions = {"mode_part": f"mode {mode.id}"}
        native_badge = native_badge_text(state.native_modes)
        if native_badge:
            markup += f"[$dimmer]{SEPARATOR}[/][$teal]$native_part[/]"
            substitutions["native_part"] = native_badge
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
    "_context_part",
    "footer_left_text",
    "footer_left_text_fit",
    "footer_right_text",
    "footer_waiting_text",
]
