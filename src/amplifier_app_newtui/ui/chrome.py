"""Title bar chrome (DESIGN-SPEC §2 item 1).

Centered title ``amplifier-app-newtui — Amplifier — <state> — <bundle> —
<session-short>`` on the ``bg-chrome`` background. While a turn is
running the title is prefixed with an orange spinner glyph cycling
``✳ ✦ ✧ ✦`` every ~260ms (Textual timer).

The ``<state>`` text is owned by the app: it reflects the current plan
step (lowercased) or ``ready`` / ``planning`` / ``brainstorming`` /
``✳ coordinating N agents`` — the title bar only displays it.
"""

from __future__ import annotations

import unicodedata

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

APP_TITLE_NAME = "amplifier-app-newtui"
PRODUCT_NAME = "Amplifier"


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
    - ``bundle`` / ``session_short``: identity fragments (skipped when empty).
    - ``running``: True while a turn executes — starts the spinner timer.
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
    bundle: reactive[str] = reactive("")
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
        parts = [APP_TITLE_NAME, PRODUCT_NAME, self.state_text]
        if self.bundle:
            parts.append(self.bundle)
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

    def watch_bundle(self, _value: str) -> None:
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


__all__ = [
    "APP_TITLE_NAME",
    "PRODUCT_NAME",
    "SPINNER_INTERVAL",
    "TERMINAL_SPINNER_FRAMES",
    "TERMINAL_TITLE_MAX_CHARS",
    "TITLE_SEPARATOR",
    "TitleBar",
    "terminal_title_sequence",
    "write_terminal_title",
]
