"""Composer frecency-recall autosuggestion strip.

A fish/zsh-style *ghost* line shown immediately above the composer: while the
draft is a plain prefix, it surfaces the single best frecency-ranked prior
prompt that completes it (``ui/composer.py`` computes it from the in-memory
ring via ``kernel.frecency.suggest_completion``; ADR-0007 lets ``ui/`` read the
app's own ``kernel/``). Tab (or Right at end-of-buffer) accepts it.

Deliberately distinct from the chronological up-ring, which is left untouched:
the ghost never captures an arrow, and it appears only while typing -- no chord
opens it. The rendered text is a pure function so it is golden-testable without
a live Textual screen.
"""

from __future__ import annotations

from textual.widgets import Static

RECALL_LABEL = "history recall:"
"""Leading label on the ghost line -- pins the suggestion to THIS surface so a
transcript echo of the same prompt can never be mistaken for the recall ghost."""


def render_recall_line(suggestion: str) -> str:
    """The exact one-line ghost text for *suggestion* (pure; golden-tested)."""
    return f"{RECALL_LABEL} {suggestion}  \u00b7  tab accepts"


class HistoryRecallStrip(Static):
    """One-line recall ghost above the composer; hidden when nothing completes."""

    DEFAULT_CSS = """
    HistoryRecallStrip {
        display: none;
        width: 100%;
        height: 1;
        padding: 0 2;
        color: $dimmer;
        background: $bg-page;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__("", markup=False, id=id)
        self._suggestion: str | None = None

    @property
    def suggestion(self) -> str | None:
        return self._suggestion

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    def show(self, suggestion: str | None) -> None:
        """Render *suggestion* as the ghost, or hide the strip when ``None``."""
        self._suggestion = suggestion
        if suggestion:
            self.update(render_recall_line(suggestion))
            self.display = True
        else:
            self.update("")
            self.display = False


__all__ = ["HistoryRecallStrip", "RECALL_LABEL", "render_recall_line"]
