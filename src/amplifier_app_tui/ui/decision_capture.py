"""Persistent bottom band for a free-text deferred-decision answer.

The needs-you block remains the durable transcript record.  Once the user
chooses ``type your own``, this small band sits immediately above the composer
so the input's temporary meaning cannot be missed: Enter answers the decision,
and Esc cancels the capture without interrupting a running turn.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

MAX_QUESTION_CELLS = 240


def compact_question(question: str) -> str:
    """One bounded, whitespace-normalized question for the bottom band."""
    clean = " ".join(question.split())
    if len(clean) <= MAX_QUESTION_CELLS:
        return clean
    return f"{clean[: MAX_QUESTION_CELLS - 1].rstrip()}…"


class DecisionCaptureStrip(Static):
    """Bottom-docked context band shown while the composer answers a decision."""

    DEFAULT_CSS = """
    DecisionCaptureStrip {
        display: none;
        width: 100%;
        height: auto;
        border-top: solid $orange;
        padding: 0 2;
        background: $bg-page;
        color: $fg;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__("", id=id)
        self._question = ""

    @property
    def question(self) -> str:
        return self._question

    def show_question(self, question: str) -> None:
        """Show *question* with non-color-only submit/cancel instructions."""
        self._question = compact_question(question)
        tokens = self.app.theme_variables
        text = Text()
        text.append("● Decision · ", style=Style(color=tokens.get("orange"), bold=True))
        text.append(self._question, style=Style(color=tokens.get("bright"), bold=True))
        text.append(
            "\n  Enter submits answer · Ctrl+J newline · Esc cancels",
            style=Style(color=tokens.get("dim")),
        )
        self.update(text)
        self.display = True

    def close(self) -> None:
        self._question = ""
        self.update("")
        self.display = False


__all__ = ["DecisionCaptureStrip", "MAX_QUESTION_CELLS", "compact_question"]
