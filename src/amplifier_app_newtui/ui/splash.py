"""Boot splash: the AMPLIFIER wordmark drawn over the empty transcript.

Module prepare can run for minutes on a cold cache; instead of a lone dim
line in an empty screen, the splash draws the wordmark with a left→right
scan (sweep), holds it with the shared shimmer band while foundation
reports install phases beneath it, and dissolves character-by-character
the moment the session banner is ready (``clear_boot_progress``).

Presentation-only, like ``ui/motion.py``: every frame is a pure function
of (art, frame) returning :class:`~amplifier_app_newtui.ui.segments.Line`
rows styled ONLY by DESIGN-SPEC §1 theme tokens — no colors here, and the
dissolve order comes from a fixed seed so frames stay deterministic.
"""

from __future__ import annotations

import random

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static

from ..model.blocks import GLYPH_SPINNER_FRAMES, Segment, StyleToken
from .motion import shimmer_band
from .segments import Line, to_rich_text

FRAME_SECONDS = 1 / 20
"""Splash frame cadence — smooth motion, trivially cheap repaints."""

SWEEP_COLS_PER_FRAME = 4
"""Scan-edge speed: the 55-col wordmark draws on in under a second."""

DISSOLVE_SPREAD_FRAMES = 6
"""Per-cell decay starts are spread over this many frames."""

DISSOLVE_DOT_FRAMES = 2
"""Frames a decaying cell lingers as ``·`` before clearing."""

DISSOLVE_SEED = 0x0A3D
"""Fixed seed: the dissolve order is decorative, determinism is load-bearing
(frames must be reproducible for tests and resumable repaints)."""

SPINNER_TICKS = 5
"""Splash frames per status-spinner glyph (~260ms, matching the title bar)."""

_EDGE_GLYPHS = "░▒▓"
_EDGE_WIDTH = 3

_WORDMARK_RAW = (
    r"    ___    __  _______  __    ________________________",
    r"   /   |  /  |/  / __ \/ /   /  _/ ____/  _/ ____/ __ \ ",
    r"  / /| | / /|_/ / /_/ / /    / // /_   / // __/ / /_/ /",
    r" / ___ |/ /  / / ____/ /____/ // __/ _/ // /___/ _, _/ ",
    r"/_/  |_/_/  /_/_/   /_____/___/_/   /___/_____/_/ |_|  ",
)

_FALLBACK_RAW = ("A M P L I F I E R",)


def _padded(art: tuple[str, ...]) -> tuple[str, ...]:
    width = max(len(line.rstrip()) for line in art)
    return tuple(line.rstrip().ljust(width) for line in art)


WORDMARK = _padded(_WORDMARK_RAW)
FALLBACK = _padded(_FALLBACK_RAW)

DecayGrid = tuple[tuple[int, ...], ...]

_Cell = tuple[str, StyleToken, bool]


def art_for(width: int, height: int) -> tuple[str, ...]:
    """The wordmark when it fits (art + status rows), else the plain row."""
    if width >= len(WORDMARK[0]) + 2 and height >= len(WORDMARK) + 4:
        return WORDMARK
    return FALLBACK


def _merged(cells: list[_Cell]) -> Line:
    """Adjacent same-styled cells collapse into one Segment."""
    segments: list[Segment] = []
    for text, token, bold in cells:
        last = segments[-1] if segments else None
        if last is not None and last.style_token == token and last.bold == bold:
            segments[-1] = last.model_copy(update={"text": last.text + text})
        else:
            segments.append(Segment(text=text, style_token=token, bold=bold))
    return tuple(segments)


def sweep_frame(art: tuple[str, ...], frame: int) -> tuple[Line, ...] | None:
    """Draw-on: revealed columns in orange behind a bright noise edge.

    Returns ``None`` once the scan has crossed the full width (sweep done).
    """
    width = len(art[0])
    reveal = frame * SWEEP_COLS_PER_FRAME
    if reveal >= width:
        return None
    lines: list[Line] = []
    for row, text in enumerate(art):
        cells: list[_Cell] = [(char, "orange", False) for char in text[:reveal]]
        for col in range(reveal, min(reveal + _EDGE_WIDTH, width)):
            glyph = _EDGE_GLYPHS[(row + col + frame) % len(_EDGE_GLYPHS)]
            cells.append((glyph, "bright", True))
        lines.append(_merged(cells))
    return tuple(lines)


def hold_frame(art: tuple[str, ...], frame: int) -> tuple[Line, ...]:
    """Idle: the full wordmark with the shared shimmer band drifting across.

    Plain text never changes (``line_plain`` equals the art), so selection
    and copy stay stable while packages install — same rule as motion.py.
    """
    band: dict[int, tuple[StyleToken, bool]] = {
        index: (token, bold) for index, token, bold in shimmer_band(len(art[0]), frame)
    }
    lines: list[Line] = []
    for text in art:
        cells: list[_Cell] = []
        for col, char in enumerate(text):
            token, bold = band.get(col, ("orange", False))
            if char == " ":
                token, bold = "orange", False  # spaces carry no visible style
            cells.append((char, token, bold))
        lines.append(_merged(cells))
    return tuple(lines)


def decay_grid(art: tuple[str, ...], seed: int = DISSOLVE_SEED) -> DecayGrid:
    """Per-cell dissolve start frames (fixed seed → deterministic order)."""
    rng = random.Random(seed)
    return tuple(tuple(rng.randint(0, DISSOLVE_SPREAD_FRAMES) for _ in line) for line in art)


def dissolve_frame(art: tuple[str, ...], grid: DecayGrid, frame: int) -> tuple[Line, ...] | None:
    """Melt-out: each cell decays ``char → · → space`` on its own schedule.

    Returns ``None`` once every cell has cleared (remove the widget).
    """
    if frame > DISSOLVE_SPREAD_FRAMES + DISSOLVE_DOT_FRAMES:
        return None
    lines: list[Line] = []
    for row, text in enumerate(art):
        cells: list[_Cell] = []
        for col, char in enumerate(text):
            age = frame - grid[row][col]
            if char == " " or age > DISSOLVE_DOT_FRAMES:
                cells.append((" ", "dimmer", False))
            elif age < 0:
                cells.append((char, "orange", False))
            else:
                cells.append(("·", "dimmer", False))
        lines.append(_merged(cells))
    return tuple(lines)


def status_line(art_width: int, status: str, spinner_glyph: str) -> Line:
    """The boot-phase line, hand-centered under the wordmark.

    Centering is done with pad segments (not text-align) so the status row
    and the art rows share one coordinate system whatever Align does.
    """
    text = status if len(status) <= art_width - 2 else status[: art_width - 3] + "…"
    pad = max(0, (art_width - len(text) - 2) // 2)
    return (
        Segment(text=" " * pad + spinner_glyph + " ", style_token="orange"),
        Segment(text=text, style_token="dim"),
    )


class BootSplash(Static):
    """The splash overlay; owned by the app for the boot window only.

    Lifecycle: mounted on the first ``boot_progress`` call, fed phase text
    via :meth:`set_status`, and dismissed by ``clear_boot_progress`` —
    dissolving on a normal ready, instantly on boot failure (the error
    text must not sit under a melting wordmark).
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 — Textual API
        super().__init__("", id=id)
        self._status = ""
        self._phase: str = "sweep"
        self._frame = 0
        self._tick = 0
        self._art: tuple[str, ...] | None = None
        self._grid: DecayGrid | None = None
        self._lines: tuple[Line, ...] = ()
        self._timer: Timer | None = None
        self._dismissed = False

    def on_mount(self) -> None:
        if self._dismissed and self._phase != "dissolve":
            # Dismissed while the mount message was still queued (instant
            # boot, or failure right after the first phase): never linger.
            self.remove()
            return
        self._timer = self.set_interval(FRAME_SECONDS, self._advance)

    def set_status(self, text: str) -> None:
        self._status = text
        if self._lines:
            self._paint()

    def dismiss_splash(self, *, immediate: bool = False) -> None:
        """Start the dissolve; ``immediate`` skips straight to removal."""
        if self._dismissed and not immediate:
            return
        self._dismissed = True
        if immediate or self._art is None:
            if self._timer is not None:
                self._timer.stop()
            if self.is_mounted:  # else on_mount sees _dismissed and removes
                self.remove()
            return
        if self._phase != "dissolve":
            self._grid = decay_grid(self._art)
            self._phase = "dissolve"
            self._frame = 0

    def _advance(self) -> None:
        self._tick += 1
        if self._art is None:
            if self.size.width <= 0:  # not laid out yet
                return
            self._art = art_for(self.size.width, self.size.height)
        frame: tuple[Line, ...] | None
        if self._phase == "sweep":
            frame = sweep_frame(self._art, self._frame)
            if frame is None:
                self._phase = "hold"
                self._frame = 0
                frame = hold_frame(self._art, 0)
        elif self._phase == "hold":
            frame = hold_frame(self._art, self._frame)
        else:
            assert self._grid is not None
            frame = dissolve_frame(self._art, self._grid, self._frame)
            if frame is None:
                if self._timer is not None:
                    self._timer.stop()
                self.remove()
                return
        self._frame += 1
        self._lines = frame
        self._paint()

    def _paint(self) -> None:
        rows: list[Line] = list(self._lines)
        if self._status and self._phase != "dissolve" and self._art is not None:
            glyph = GLYPH_SPINNER_FRAMES[(self._tick // SPINNER_TICKS) % len(GLYPH_SPINNER_FRAMES)]
            rows.append(())
            rows.append(status_line(len(self._art[0]), self._status, glyph))
        # Rich Text, not content markup: the wordmark is full of backslashes,
        # and a style split landing right after one (the shimmer band moves
        # every frame) makes markup swallow its own close tag — a literal
        # ``[/]`` painted on screen. to_rich_text renders glyphs verbatim.
        variables = self.app.theme_variables
        self.update(Text("\n").join(to_rich_text(row, variables) for row in rows))


__all__ = [
    "FALLBACK",
    "WORDMARK",
    "BootSplash",
    "art_for",
    "decay_grid",
    "dissolve_frame",
    "hold_frame",
    "status_line",
    "sweep_frame",
]
