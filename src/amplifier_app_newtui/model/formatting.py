"""The one public home for token-count display formatting.

Two DISTINCT display contracts live here, pinned by different tests for
different surfaces. They are deliberately NOT merged -- each serves a
different part of the UI and renders the same count differently:

- :func:`format_tokens_k` -- fixed one-decimal thousands (``0.0k`` /
  ``3.2k`` / ``1200.0k``). The turn-telemetry / lanes / demo-mockup
  surface: always ``(tokens/1000).1f + "k"``, sub-1k counts included,
  never switches to ``m`` units.
- :func:`format_tokens_compact` -- compact human count (``742`` /
  ``4.1k`` / ``52k`` / ``1.2m``). The ``/context`` and ``/doctor``
  surface: bare integer under 1k, adaptive-decimal ``k``, ``m`` above a
  million.

Pure arithmetic -- no imports, no side effects -- so it sits cleanly at
the bottom of the ADR-0007 layering (imports neither Textual nor
amplifier-core) and every layer above can share it.
"""

from __future__ import annotations


def format_tokens_k(tokens: int) -> str:
    """Fixed one-decimal thousands: ``0.0k`` / ``3.2k`` / ``1200.0k``.

    The turn-telemetry surface (``TurnTelemetry`` suffix/label, the
    lanes-panel down-arrow ``X.Xk tokens`` figure, and the demo mockup's
    rule labels). Always ``(tokens/1000).toFixed(1) + "k"`` per the
    mockup -- sub-1k counts are shown (``0.0k`` at turn start) and it
    never switches to ``m`` units, so 1.2M tokens reads ``1200.0k``.
    """
    return f"{tokens / 1_000:.1f}k"


def format_tokens_compact(tokens: int) -> str:
    """Compact human count: ``742`` / ``4.1k`` / ``52k`` / ``1.2m``.

    The ``/context`` and ``/doctor`` surface. Bare integer below 1k;
    ``k`` above that with a decimal only when it adds information
    (``4.1k`` but ``8k``); ``m`` above a million.
    """
    if tokens < 1_000:
        return str(tokens)
    if tokens < 1_000_000:
        thousands = tokens / 1_000
        if thousands < 10 and round(thousands, 1) != round(thousands):
            return f"{thousands:.1f}k"
        return f"{round(thousands)}k"
    return f"{tokens / 1_000_000:.1f}m"


__all__ = ["format_tokens_compact", "format_tokens_k"]
