"""Shared terminal surface geometry (current width in columns).

The full-screen TUI's rendering contract is *width-aware*: the kernel
injects a per-request surface hint (issue #35 / docs/BACKLOG.md section 2)
telling the model how many columns it has and which Markdown subset renders
cleanly. Width is owned by the UI (Textual resize events, app loop) but
consumed in the kernel (a ``provider:request`` hook, runtime thread), so the
value lives here in ``model/`` -- the one layer both may touch (ADR-0007
layering: ``ui/`` -> ``model/`` -> ``kernel/``, and ``model/`` imports
neither Textual nor amplifier-core).

Reads and writes cross the app/runtime thread boundary, so a plain lock
keeps them honest; the stored value is always clamped to a sane column
range so a transient 0-width report during boot never leaks into the hint.
"""

from __future__ import annotations

import threading

DEFAULT_TERMINAL_COLS = 80
"""Assumed width before the UI reports a real size (VT100 default)."""

MIN_TERMINAL_COLS = 20
MAX_TERMINAL_COLS = 1000
"""Clamp bounds: guards against 0/negative boot reports and absurd values."""


class TerminalSurface:
    """Thread-safe holder for the current terminal width in columns.

    The UI updates it on resize (:meth:`set_cols`, app loop); the kernel's
    surface-hint hook reads :attr:`cols` at ``provider:request`` (runtime
    thread). A resize is therefore reflected on the next turn's request.
    """

    def __init__(self, cols: int = DEFAULT_TERMINAL_COLS) -> None:
        self._lock = threading.Lock()
        self._cols = _clamp(cols)

    @property
    def cols(self) -> int:
        """The current terminal width, clamped to the supported range."""
        with self._lock:
            return self._cols

    def set_cols(self, cols: int) -> None:
        """Record a new terminal width (out-of-range/junk values are clamped)."""
        clamped = _clamp(cols)
        with self._lock:
            self._cols = clamped


def _clamp(cols: object) -> int:
    try:
        value = int(cols)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TERMINAL_COLS
    if value < MIN_TERMINAL_COLS:
        return MIN_TERMINAL_COLS
    if value > MAX_TERMINAL_COLS:
        return MAX_TERMINAL_COLS
    return value


__all__ = [
    "DEFAULT_TERMINAL_COLS",
    "MAX_TERMINAL_COLS",
    "MIN_TERMINAL_COLS",
    "TerminalSurface",
]
