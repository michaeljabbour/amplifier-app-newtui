"""Rewind picker overlay strip (DESIGN-SPEC §9, §2 overlay strips).

A bordered orange strip docked ABOVE the composer, opened by ctrl-r /
``/rewind`` / clicking a turn rule:

``‹ rewind › tN · $X.XX · <label> › [enter fork] [esc close]``

- ``‹`` / ``›`` (click or ``←``/``→``) navigate checkpoints, clamped at
  the ends (mockup ``Math.max/Math.min`` — no wrap-around).
- ``enter fork`` (chip, bright on bg-tab; Enter or click) posts
  :class:`RewindStrip.ForkRequested` with the current checkpoint id —
  the app performs the actual session fork (confirm-then-trim,
  ADR-0007) and only then trims the transcript.
- ``esc close`` (dimmer; Esc or click) posts :class:`RewindStrip.Closed`.

The strip hides itself after fork/close.
"""

from __future__ import annotations

from collections.abc import Sequence

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static

from ..model.blocks import GLYPH_REWIND_LEFT, GLYPH_REWIND_RIGHT
from ..model.turn import Checkpoint

FORK_HINT = "enter fork"
CLOSE_HINT = "esc close"


def rewind_label(checkpoint: Checkpoint) -> str:
    """``tN · $X.XX · <label>`` — the picker's checkpoint description."""
    return f"{checkpoint.id} · ${checkpoint.cost_at:.2f} · {checkpoint.label}"


def rewind_line(checkpoint: Checkpoint) -> str:
    """The strip's center text: ``rewind › tN · $X.XX · <label>``."""
    return f"rewind {GLYPH_REWIND_RIGHT} {rewind_label(checkpoint)}"


class RewindStrip(Horizontal):
    """The rewind picker strip (DESIGN-SPEC §9).

    Open with :meth:`show_checkpoints` (defaults to the newest
    checkpoint, or the clicked rule's). Posts:

    - :class:`ForkRequested` — Enter / ``enter fork`` chip click.
    - :class:`Closed` — Esc / ``esc close`` click.
    """

    can_focus = True

    DEFAULT_CSS = """
    RewindStrip {
        display: none;
        width: 100%;
        height: auto;
        border-top: solid $rule;
        padding: 0 2;
        color: $orange;
    }
    RewindStrip > Static {
        width: auto;
        height: 1;
        color: $orange;
        margin-right: 1;
    }
    RewindStrip #rewind-fork {
        color: $bright;
        background: $bg-tab;
        padding: 0 1;
    }
    RewindStrip #rewind-close {
        color: $dimmer;
    }
    """

    BINDINGS = [
        Binding("left", "prev", "‹ ›", show=False),
        Binding("right", "next", "‹ ›", show=False),
        Binding("enter", "fork", "enter fork", show=False),
        # No local escape binding: Esc must bubble to the app so it resolves
        # via keymap.ESC_CHAIN (spec §5 — lane-focus/palette close before
        # rewind even while this strip holds keyboard focus). The chain
        # calls ``action_close`` when the rewind step is reached.
    ]

    class ForkRequested(Message):
        """The user asked to fork from a checkpoint (Enter / chip click)."""

        def __init__(self, checkpoint_id: str) -> None:
            self.checkpoint_id = checkpoint_id
            super().__init__()

    class Closed(Message):
        """Esc pressed / ``esc close`` clicked."""

    class TypeThrough(Message):
        """A printable key pressed while the strip held focus.

        Mockup ground truth (document-level keydown, composer input keeps
        focus while ``rewindOpen``): typing is never swallowed by the
        rewind picker — the app forwards the character to the composer,
        so ``/`` opens the palette live-filtered and the text lands in
        the input (spec §5).
        """

        def __init__(self, character: str) -> None:
            self.character = character
            super().__init__()

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._checkpoints: tuple[Checkpoint, ...] = ()
        self._index = 0

    def compose(self):
        yield Static(GLYPH_REWIND_LEFT, id="rewind-prev")
        yield Static("", id="rewind-label")
        yield Static(GLYPH_REWIND_RIGHT, id="rewind-next")
        yield Static(FORK_HINT, id="rewind-fork")
        yield Static(CLOSE_HINT, id="rewind-close")

    # -- public API ----------------------------------------------------

    @property
    def checkpoints(self) -> tuple[Checkpoint, ...]:
        return self._checkpoints

    @property
    def index(self) -> int:
        return self._index

    @property
    def current(self) -> Checkpoint | None:
        if not self._checkpoints:
            return None
        return self._checkpoints[self._index]

    @property
    def label_text(self) -> str:
        """The exact center text currently displayed."""
        current = self.current
        return rewind_line(current) if current is not None else ""

    def show_checkpoints(
        self, checkpoints: Sequence[Checkpoint], index: int | None = None
    ) -> None:
        """Open the picker on *checkpoints* (newest selected by default).

        An empty checkpoint list keeps the strip hidden — the app shows
        the ``no rewind checkpoints yet`` notice instead.
        """
        self._checkpoints = tuple(checkpoints)
        if not self._checkpoints:
            self.display = False
            return
        last = len(self._checkpoints) - 1
        self._index = last if index is None else max(0, min(last, index))
        self._refresh_label()
        self.display = True
        self.focus()

    def sync_checkpoints(self, checkpoints: Sequence[Checkpoint]) -> None:
        """Refresh the open picker's list in place (mockup openRewind /
        rewindNext read the live ``this.checkpoints`` array — a checkpoint
        cut while the picker is open is immediately navigable with ›).

        The cursor position is preserved (clamped); focus is untouched.
        """
        if not self.display:
            return
        self._checkpoints = tuple(checkpoints)
        if not self._checkpoints:
            self.display = False
            return
        self._index = max(0, min(len(self._checkpoints) - 1, self._index))
        self._refresh_label()

    def nav(self, delta: int) -> None:
        """Move the checkpoint cursor by *delta*, clamped at both ends."""
        if not self._checkpoints:
            return
        self._index = max(0, min(len(self._checkpoints) - 1, self._index + delta))
        self._refresh_label()

    def fork(self) -> None:
        """Request the fork for the current checkpoint and close the strip."""
        current = self.current
        if current is None:
            return
        self.display = False
        self.post_message(self.ForkRequested(current.id))

    def close_strip(self) -> None:
        self.display = False
        self.post_message(self.Closed())

    # -- key actions ----------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        """Printable keys pass through to the composer (mockup: the
        composer keeps typing rights while ``rewindOpen``); ←→/enter stay
        with the strip via BINDINGS, esc bubbles to the app's ESC_CHAIN."""
        if event.is_printable and event.character:
            event.stop()
            event.prevent_default()
            self.post_message(self.TypeThrough(event.character))

    def action_prev(self) -> None:
        self.nav(-1)

    def action_next(self) -> None:
        self.nav(1)

    def action_fork(self) -> None:
        self.fork()

    def action_close(self) -> None:
        self.close_strip()

    # -- clicks ----------------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        widget = event.widget
        if widget is None or widget.id is None:
            return
        if widget.id == "rewind-prev":
            self.nav(-1)
        elif widget.id == "rewind-next":
            self.nav(1)
        elif widget.id == "rewind-fork":
            self.fork()
        elif widget.id == "rewind-close":
            self.close_strip()

    # -- internals -------------------------------------------------------

    def _refresh_label(self) -> None:
        if self.is_mounted:
            self.query_one("#rewind-label", Static).update(self.label_text)


__all__ = [
    "CLOSE_HINT",
    "FORK_HINT",
    "RewindStrip",
    "rewind_label",
    "rewind_line",
]
