"""Checkpoint restore picker (DESIGN-SPEC §9, §2 overlay strips).

A bordered orange strip docked ABOVE the composer, opened by ctrl-r /
``/rewind`` / clicking a turn rule:

``‹ checkpoint · before turn N · <prompt> › [code + conversation] [enter restore]``

- ``‹`` / ``›`` (click or ``←``/``→``) navigate checkpoints, clamped at
  the ends (mockup ``Math.max/Math.min`` — no wrap-around).
- ``↑`` / ``↓`` select Restore both, Conversation only, or Code only.
- ``enter restore`` posts :class:`RewindStrip.ForkRequested` with the
  checkpoint id and selected scope. The legacy message name remains as a
  compatibility seam; the operation is a pre-prompt restore.
- ``esc close`` (dimmer; Esc or click) posts :class:`RewindStrip.Closed`.

The strip hides itself after fork/close.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static

from ..model.blocks import GLYPH_REWIND_LEFT, GLYPH_REWIND_RIGHT
from ..model.turn import Checkpoint

FORK_HINT = "enter restore"
CLOSE_HINT = "esc close"
RestoreScope = Literal["both", "conversation", "code"]
RESTORE_SCOPES: tuple[tuple[RestoreScope, str], ...] = (
    ("both", "code + conversation"),
    ("conversation", "conversation only"),
    ("code", "code only"),
)


def rewind_label(checkpoint: Checkpoint) -> str:
    """``turn N · $X.XX · <label>`` — the picker's checkpoint description.

    The turn is spelled out (``turn 3``, not the cryptic ``t3``) so the
    marker reads legibly (S5 discoverability: users could not tell what
    ``t3`` meant).
    """
    return f"before turn {checkpoint.turn_id} · ${checkpoint.cost_at:.2f} · {checkpoint.label}"


def rewind_line(checkpoint: Checkpoint) -> str:
    """The strip's center text: ``rewind · pick a turn · turn N · $X.XX · <label>``.

    The ``pick a turn`` phrase turns the strip into a self-explaining
    header: it names the feature (``rewind``), states the action, then
    shows the currently selected turn — flanked by the ‹ › nav glyphs and
    the ``enter fork`` / ``esc close`` chips the strip composes alongside.
    """
    return f"checkpoint · pick a prompt · {rewind_label(checkpoint)}"


class RewindStrip(Horizontal):
    """The rewind picker strip (DESIGN-SPEC §9).

    Open with :meth:`show_checkpoints` (defaults to the newest
    checkpoint, or the clicked rule's). Posts:

    - :class:`ForkRequested` — Enter / ``enter restore`` chip click.
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
        Binding("up", "scope_prev", "restore mode", show=False),
        Binding("down", "scope_next", "restore mode", show=False),
        Binding("enter", "fork", "enter restore", show=False),
        # No local escape binding: Esc must bubble to the app so it resolves
        # via keymap.ESC_CHAIN (spec §5 — lane-focus/palette close before
        # rewind even while this strip holds keyboard focus). The chain
        # calls ``action_close`` when the rewind step is reached.
    ]

    class ForkRequested(Message):
        """The user asked to restore a checkpoint (legacy message name)."""

        def __init__(self, checkpoint_id: str, scope: RestoreScope = "both") -> None:
            self.checkpoint_id = checkpoint_id
            self.scope = scope
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
        self._scope_index = 0

    def compose(self):
        yield Static(GLYPH_REWIND_LEFT, id="rewind-prev")
        yield Static("", id="rewind-label")
        yield Static(GLYPH_REWIND_RIGHT, id="rewind-next")
        yield Static("", id="rewind-scope")
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

    @property
    def scope(self) -> RestoreScope:
        return RESTORE_SCOPES[self._scope_index][0]

    @property
    def scope_text(self) -> str:
        return f"↑↓ {RESTORE_SCOPES[self._scope_index][1]}"

    def show_checkpoints(self, checkpoints: Sequence[Checkpoint], index: int | None = None) -> None:
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
        self._scope_index = 0
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

    def nav_scope(self, delta: int) -> None:
        """Move through restore modes, clamped at both ends."""
        self._scope_index = max(0, min(len(RESTORE_SCOPES) - 1, self._scope_index + delta))
        self._refresh_label()

    def fork(self) -> None:
        """Request the fork for the current checkpoint and close the strip."""
        current = self.current
        if current is None:
            return
        self.display = False
        self.post_message(self.ForkRequested(current.id, self.scope))

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

    def action_scope_prev(self) -> None:
        self.nav_scope(-1)

    def action_scope_next(self) -> None:
        self.nav_scope(1)

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
        elif widget.id == "rewind-scope":
            delta = 1 if self._scope_index < len(RESTORE_SCOPES) - 1 else -self._scope_index
            self.nav_scope(delta)
        elif widget.id == "rewind-fork":
            self.fork()
        elif widget.id == "rewind-close":
            self.close_strip()

    # -- internals -------------------------------------------------------

    def _refresh_label(self) -> None:
        if self.is_mounted:
            self.query_one("#rewind-label", Static).update(self.label_text)
            self.query_one("#rewind-scope", Static).update(self.scope_text)


__all__ = [
    "CLOSE_HINT",
    "FORK_HINT",
    "RESTORE_SCOPES",
    "RewindStrip",
    "RestoreScope",
    "rewind_label",
    "rewind_line",
]
