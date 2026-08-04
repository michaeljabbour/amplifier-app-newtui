"""Title bar chrome (DESIGN-SPEC §2 item 1).

Centered title ``amplifier — <state> — <bundle> — <session-short>`` on
the ``bg-chrome`` background. The brand is always plain ``amplifier`` —
only the terminal command is amplifier-tui. While a turn is
running the title is prefixed with an orange spinner glyph cycling
``✳ ✦ ✧ ✦`` every ~260ms (Textual timer).

The ``<state>`` text is owned by the app: it reflects the current plan
step (lowercased) or ``ready`` / ``planning`` / ``brainstorming`` /
``✳ coordinating N agents`` — the title bar only displays it.

The ``<bundle>`` fragment is the ACTUALLY-RESOLVED bundle URI/path
(:attr:`~amplifier_app_tui.ui.runtime_adapter.RuntimeAdapter.bundle_uri`,
sourced from ``kernel/config.resolve_bundle_source`` via
``ResolvedConfig.bundle_uri``) — not just the short name it was
requested by — fitted to the live terminal width (see
:func:`_bundle_fit_budget`) rather than a fixed cap, so it reflows on
resize the way ``ui/footer.py``'s fit ladder does for its own segments.
"""

from __future__ import annotations

import unicodedata

from rich.cells import cell_len
from textual import events
from textual.content import Content
from textual.driver import Driver
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static

from ..model.blocks import GLYPH_SPINNER_FRAMES

TITLE_SEPARATOR = " — "
SPINNER_INTERVAL = 0.26
"""Seconds between spinner frames (~260ms per DESIGN-SPEC §2)."""

TERMINAL_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
"""Unmistakable terminal-window spinner; the in-app chrome keeps its stars."""

TERMINAL_TITLE_MAX_CHARS = 180
"""Keep macOS terminal tabs useful when a plan step has a long title."""

APP_TITLE_NAME = "amplifier"

TITLE_BUNDLE_MAX_CELLS = 40
"""Fallback bundle-fragment budget (cells) used only before the real
terminal width is known — a bare, unmounted :class:`TitleBar` (no App/
layout pass yet) has no live width to fit against, so :func:`_bundle_fit_budget`
falls back to this constant rather than showing an unbounded value.

Once mounted, the budget is VIEWPORT-AWARE (compliance 2026-08-02, item D4
gap 2): it is computed from the title row's actual rendered width — not
this fixed cap — mirroring the footer's fit-ladder idiom
(``ui/footer.py:_fit_drops``) applied to the title's one elastic fragment.
A wide terminal can show MORE of a long resolved bundle URI than this
constant used to allow; a narrow one shows less, down to dropping the
fragment entirely once even a truncated stub would be meaningless (see
:data:`_MIN_BUNDLE_CELLS`) — never wrapping the ``height: 1`` row onto the
composer docked below it (D4 AC4).
"""

_MIN_BUNDLE_CELLS = 4
"""Below this budget a truncated bundle stub (a couple of characters plus
an ellipsis) reads as noise, not identity — :func:`_bundle_fit_budget`
returns 0 instead, and the title drops the fragment entirely rather than
show it. Mirrors the footer ladder's "drop, don't garble" shape."""


def _truncate_bundle_label(bundle: str, max_cells: int = TITLE_BUNDLE_MAX_CELLS) -> str:
    """Cell-width-safe truncation with a single trailing ellipsis (D4 AC4).

    Mirrors the house truncation shape already used elsewhere for the same
    reason (``ui/transcript_render._clip``, ``ui/lanes_panel._elide``):
    never silently clip -- a value longer than *max_cells* always ends in
    exactly one ``\u2026`` so the title visibly promises more text exists.
    Cell-width (not code-point) aware, so a wide-glyph bundle name
    truncates at the same visual boundary a narrow-glyph one would.
    """
    if cell_len(bundle) <= max_cells:
        return bundle
    out = ""
    for character in bundle:
        if cell_len(out + character) > max_cells - 1:
            break
        out += character
    return out.rstrip() + "\u2026"


def _bundle_fit_budget(width: int, state_text: str, session_short: str) -> int:
    """Cells available for the bundle fragment so the WHOLE title fits *width*.

    The viewport-aware replacement for the old fixed :data:`TITLE_BUNDLE_MAX_CELLS`
    cap (D4 gap 2): reserves exactly the cells the rest of the title needs
    (``amplifier``, its separators, ``state_text``, and ``session_short`` when
    present) and returns whatever is left over for the bundle fragment --
    mirroring the footer's fit-ladder idiom (``ui/footer.py:_fit_drops``)
    applied to the title's one elastic, user-supplied fragment instead of a
    set of droppable decorations.

    ``width <= 0`` (no real layout yet -- a bare, unmounted ``TitleBar``)
    falls back to :data:`TITLE_BUNDLE_MAX_CELLS` so pre-layout behavior is
    unchanged. Below :data:`_MIN_BUNDLE_CELLS` the budget collapses to 0 --
    drop the fragment rather than show a near-meaningless truncated stub.
    """
    if width <= 0:
        return TITLE_BUNDLE_MAX_CELLS
    reserved = cell_len(TITLE_SEPARATOR.join((APP_TITLE_NAME, state_text)))
    reserved += cell_len(TITLE_SEPARATOR)  # the separator introducing the bundle fragment
    if session_short:
        reserved += cell_len(TITLE_SEPARATOR) + cell_len(session_short)
    budget = width - reserved
    return budget if budget >= _MIN_BUNDLE_CELLS else 0


def terminal_title_sequence(title: str) -> str:
    """Build a safe OSC 0 sequence for a native terminal window/tab title.

    Bundle names and plan steps can come from runtime data, so control
    characters must never reach the OSC payload. Whitespace is collapsed and
    the result is bounded so a verbose step does not take over the tab bar.
    """

    without_controls = "".join(
        " " if unicodedata.category(character) == "Cc" else character for character in title
    )
    safe_title = " ".join(without_controls.split())[:TERMINAL_TITLE_MAX_CHARS]
    return f"\x1b]0;{safe_title}\x07"


def write_terminal_title(driver: Driver | None, title: str) -> bool:
    """Write ``title`` to native terminal chrome when a terminal is present."""

    if driver is None or driver.is_headless or driver.is_web:
        return False
    driver.write(terminal_title_sequence(title))
    driver.flush()
    return True


class TitleBar(Static):
    """The top chrome strip.

    State API (all reactives; the app sets them, the bar repaints):

    - ``state_text``: the ``<state>`` fragment (``ready``, a plan step, …).
    - ``bundle_uri`` / ``session_short``: identity fragments (skipped when empty).
    - ``running``: True while a turn executes — starts the spinner timer.

    ``bundle_uri`` is also the ONE persistent place the active bundle renders
    anywhere in the UI (compliance 2026-08-02, item D4 — David Koleczek's
    UX review, July 31 2026, preferred it kept here at the top since "the
    footer is already crowded"). It carries the ACTUALLY-RESOLVED bundle
    URI/path (``RuntimeAdapter.bundle_uri``), not just the short name a
    bundle was requested by, so the "full active bundle path" claim (AC1)
    is literally true; ``_plain_title`` fits it to the live terminal width
    (:func:`_bundle_fit_budget`) rather than showing it verbatim.
    ``ui/footer.py`` used to paint a second, always-identical copy in its
    left segment; that duplication is gone — see the footer module's
    docstring for the consolidation.
    """

    DEFAULT_CSS = """
    TitleBar {
        dock: top;
        width: 100%;
        height: 1;
        background: $bg-chrome;
        color: $title-fg;
        text-style: bold;
        text-align: center;
    }
    """

    state_text: reactive[str] = reactive("ready")
    bundle_uri: reactive[str] = reactive("")
    session_short: reactive[str] = reactive("")
    running: reactive[bool] = reactive(False)

    class TitleChanged(Message):
        """The rendered title changed, including an active spinner frame."""

        def __init__(self, title: str, terminal_title: str) -> None:
            self.title = title
            self.terminal_title = terminal_title
            super().__init__()

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._frame_index = 0
        self._spinner_timer: Timer | None = None
        self._last_emitted_title = ""

    # -- text assembly -----------------------------------------------------

    @property
    def spinner_glyph(self) -> str:
        """The current spinner frame (``✳``/``✦``/``✧``/``✦``)."""
        return GLYPH_SPINNER_FRAMES[self._frame_index % len(GLYPH_SPINNER_FRAMES)]

    @property
    def terminal_spinner_glyph(self) -> str:
        """The current high-motion braille frame for native terminal chrome."""

        return TERMINAL_SPINNER_FRAMES[self._frame_index % len(TERMINAL_SPINNER_FRAMES)]

    def title_text(self) -> str:
        """Plain rendered title, spinner prefix included while running."""
        title = self._plain_title()
        if self.running:
            return f"{self.spinner_glyph} {title}"
        return title

    def terminal_title_text(self) -> str:
        """Native terminal title with a visibly rotating braille spinner."""

        title = self._plain_title()
        if self.running:
            return f"{self.terminal_spinner_glyph} {title}"
        return title

    # -- painting ----------------------------------------------------------

    def _repaint(self) -> None:
        title = self.title_text()
        terminal_title = self.terminal_title_text()
        if self.running:
            # Substitution kwargs insert values literally (no markup parse).
            self.update(
                Content.from_markup(
                    "[bold $orange]$glyph[/] $title",
                    glyph=self.spinner_glyph,
                    title=self._plain_title(),
                )
            )
        else:
            self.update(Content.from_markup("$title", title=title))
        if self.is_mounted and terminal_title != self._last_emitted_title:
            self._last_emitted_title = terminal_title
            self.post_message(self.TitleChanged(title, terminal_title))

    def _plain_title(self) -> str:
        parts = [APP_TITLE_NAME, self.state_text]
        if self.bundle_uri:
            budget = _bundle_fit_budget(
                self.container_size.width, self.state_text, self.session_short
            )
            if budget > 0:
                parts.append(_truncate_bundle_label(self.bundle_uri, max_cells=budget))
        if self.session_short:
            parts.append(self.session_short)
        return TITLE_SEPARATOR.join(parts)

    def advance_spinner(self) -> None:
        """Step to the next spinner frame and repaint (timer callback)."""
        self._frame_index += 1
        self._repaint()

    # -- reactive watchers ---------------------------------------------------

    def watch_running(self, running: bool) -> None:
        if running:
            self._frame_index = 0
            if self._spinner_timer is None and self.is_running:
                self._spinner_timer = self.set_interval(SPINNER_INTERVAL, self.advance_spinner)
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._frame_index = 0
        self._repaint()

    def watch_state_text(self, _value: str) -> None:
        self._repaint()

    def watch_bundle_uri(self, _value: str) -> None:
        self._repaint()

    def watch_session_short(self, _value: str) -> None:
        self._repaint()

    def on_mount(self) -> None:
        # If running was set before mount, the timer could not start yet.
        if self.running and self._spinner_timer is None:
            self._spinner_timer = self.set_interval(SPINNER_INTERVAL, self.advance_spinner)
        self._repaint()

    def on_unmount(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def on_resize(self, event: events.Resize) -> None:
        # Viewport-aware bundle fitting (D4 gap 2): width changed, so the
        # bundle fragment's budget may have grown or shrunk -- mirrors
        # FooterBar.on_resize's identical "width changed, repaint" shape.
        del event
        self._repaint()


__all__ = [
    "APP_TITLE_NAME",
    "SPINNER_INTERVAL",
    "TITLE_BUNDLE_MAX_CELLS",
    "TERMINAL_SPINNER_FRAMES",
    "TERMINAL_TITLE_MAX_CHARS",
    "TITLE_SEPARATOR",
    "TitleBar",
    "terminal_title_sequence",
    "write_terminal_title",
]
