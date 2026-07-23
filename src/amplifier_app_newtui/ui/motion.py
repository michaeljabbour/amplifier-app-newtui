"""Small, shared motion primitives for active TUI labels.

Motion here is presentation-only: it never changes the underlying text, so
snapshots, selection, and copy/paste remain deterministic.
"""

from __future__ import annotations

from ..model.blocks import StyleToken

SHIMMER_INTERVAL_SECONDS = 0.08
"""Soft-band cadence: quick enough to read as motion without busy redraws."""

SHIMMER_GAP_CELLS = 5
"""Quiet cells after a band crosses a label before it loops."""

_SHIMMER_BAND: tuple[tuple[int, StyleToken, bool], ...] = (
    (-2, "fg", False),
    (-1, "bright", False),
    (0, "bright", True),
    (1, "bright", False),
    (2, "fg", False),
)
"""A soft five-cell ``shadow -> light -> peak -> light -> shadow`` band."""


def shimmer_band(length: int, frame: int) -> tuple[tuple[int, StyleToken, bool], ...]:
    """Return visible ``(index, theme-token, bold)`` cells for one frame.

    Indices outside the label are clipped. During the quiet gap the result is
    empty, leaving callers' base styling untouched.
    """

    if length <= 0:
        return ()
    peak = frame % (length + SHIMMER_GAP_CELLS)
    if peak >= length:
        return ()
    return tuple(
        (index, token, bold)
        for offset, token, bold in _SHIMMER_BAND
        if 0 <= (index := peak + offset) < length
    )


__all__ = ["SHIMMER_GAP_CELLS", "SHIMMER_INTERVAL_SECONDS", "shimmer_band"]
