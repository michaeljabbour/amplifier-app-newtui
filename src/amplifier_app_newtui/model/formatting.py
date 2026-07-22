"""Shared token-count formatting for the pure model layer.

One public home for token formatting so every layer imports a *public*
helper instead of reaching across the layer boundary into a private one
(ADR-0007 layering; fixes the ``ui/lanes_panel`` -> ``model/turn._format_tokens``
encapsulation leak and the independent ``commands/context`` copy).

Two DISTINCT formatters live here on purpose - they render different mockup
surfaces and are **not** interchangeable, so this de-duplicates *ownership*,
not behavior:

- :func:`format_tokens_k` - always ``X.Xk`` (turn telemetry / lanes panel:
  ``\u2193 0.0k tok`` at turn start, ``1200.0k`` for 1.2M - never m-units).
- :func:`format_tokens` - adaptive ``742`` / ``4.1k`` / ``52k`` / ``1.2m``
  (the ``/context`` line, which switches units as the count grows).
"""

from __future__ import annotations


def format_tokens_k(tokens: int) -> str:
    """``0.0k`` / ``3.2k`` / ``1200.0k`` token formatting per the mockup.

    Mockup always renders ``(toks/1000).toFixed(1) + "k"`` - sub-1k
    counts included (``\u2193 0.0k tok`` at turn start, ``0.6k`` at 608)
    and never switches to m-units, so 1.2M tokens reads ``1200.0k``.
    """
    return f"{tokens / 1_000:.1f}k"


def format_tokens(tokens: int) -> str:
    """``742`` / ``4.1k`` / ``52k`` / ``1.2m`` - mockup token formatting."""
    if tokens < 1_000:
        return str(tokens)
    if tokens < 1_000_000:
        thousands = tokens / 1_000
        if thousands < 10 and round(thousands, 1) != round(thousands):
            return f"{thousands:.1f}k"
        return f"{round(thousands)}k"
    return f"{tokens / 1_000_000:.1f}m"


__all__ = ["format_tokens", "format_tokens_k"]
