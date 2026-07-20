"""Composer ``@file`` autocomplete strip.

The composer retains keyboard focus; this controlled overlay only presents a
ranked workspace index and posts a path when a row is clicked. Arrow/accept
keys are routed by :class:`ui.composer.ComposerInput`, matching the command
palette's message-driven ownership without introducing filesystem work in UI.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from rich.style import Style
from rich.text import Text
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ..kernel.file_mentions import filter_file_mentions

MentionAction = Literal["filter", "clear", "move", "accept", "select"]


class FileMentionIntent(Message):
    """One composer/row intent, dispatched outside the app composition root."""

    def __init__(
        self,
        action: MentionAction,
        *,
        query: str = "",
        delta: int = 0,
        path: str = "",
    ) -> None:
        self.action = action
        self.query = query
        self.delta = delta
        self.path = path
        super().__init__()


class _MentionRow(Static):
    DEFAULT_CSS = """
    _MentionRow {
        width: 100%;
        height: 1;
        padding: 0 2;
        color: $dim;
    }
    _MentionRow.-selected { background: $bg-tab; }
    """

    def __init__(self, path: str, index: int) -> None:
        super().__init__(id=f"file-mention-row-{index}")
        self.path = path
        self.index = index

    def render(self) -> Text:
        tokens = self.app.theme_variables
        selected = self.has_class("-selected")
        text = Text("@", style=Style(color=tokens.get("green"), bold=True))
        text.append(
            self.path,
            style=Style(color=tokens.get("bright" if selected else "fg")),
        )
        return text

    def on_click(self) -> None:
        self.post_message(FileMentionIntent("select", path=self.path))


class FileMentionStrip(VerticalScroll):
    """Ranked workspace paths shown immediately above the composer."""

    DEFAULT_CSS = """
    FileMentionStrip {
        display: none;
        width: 100%;
        height: auto;
        max-height: 9;
        border-top: solid $rule;
        background: $bg-page;
        scrollbar-size-vertical: 1;
        scrollbar-color: $rule;
        scrollbar-background: $bg-page;
    }
    FileMentionStrip > .file-mention-hint {
        width: 100%;
        height: 1;
        padding: 0 2;
        color: $dimmer;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._paths: tuple[str, ...] = ()
        self._matches: tuple[str, ...] = ()
        self._selected = 0

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    @property
    def matches(self) -> tuple[str, ...]:
        return self._matches

    @property
    def selected_path(self) -> str | None:
        return self._matches[self._selected] if self._matches else None

    def set_files(self, paths: Sequence[str]) -> None:
        self._paths = tuple(paths)

    def apply_filter(self, query: str | None) -> None:
        self._matches = () if query is None else filter_file_mentions(self._paths, query)
        self._selected = 0
        self.display = bool(self._matches)
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        if not self._matches:
            return
        self._selected = max(0, min(len(self._matches) - 1, self._selected + delta))
        self._sync_selection()

    def _rebuild(self) -> None:
        if not self._matches:
            self.remove_children()
            return
        # DOM removal is asynchronous; serialize it before remounting so
        # rapid per-keystroke filters never collide on stable row ids.
        self.call_later(self._remount_rows)

    async def _remount_rows(self) -> None:
        await self.remove_children()
        if not self._matches:
            return
        await self.mount(
            Static(
                "@ file  ·  ↑↓ select  ·  enter insert  ·  esc close",
                classes="file-mention-hint",
            ),
            *(_MentionRow(path, index) for index, path in enumerate(self._matches)),
        )
        self._sync_selection()

    def _sync_selection(self) -> None:
        for row in self.query(_MentionRow):
            row.set_class(row.index == self._selected, "-selected")
        selected = self.query(_MentionRow).filter(f"#file-mention-row-{self._selected}")
        if selected:
            self.scroll_to_widget(selected.first(), animate=False)


def close_file_mentions(app: Any) -> None:
    """Close suggestions while leaving the composer and its text intact."""
    app.file_mentions.apply_filter(None)
    app.composer.mention_open = False


def handle_file_mention_intent(app: Any, message: FileMentionIntent) -> None:
    """Apply a mention intent; extracted to keep ``ui/app.py`` composition-only."""
    message.stop()
    if message.action == "filter":
        app.palette.apply_filter(None)
        app.file_mentions.apply_filter(message.query)
        app.composer.mention_open = app.file_mentions.is_open
    elif message.action == "move":
        app.file_mentions.move_selection(message.delta)
    elif message.action in ("accept", "select"):
        path = message.path or app.file_mentions.selected_path
        if path is not None:
            app.composer.apply_file_mention(path)
        close_file_mentions(app)
        app.composer.focus_input()
    else:
        close_file_mentions(app)


__all__ = [
    "FileMentionIntent",
    "FileMentionStrip",
    "close_file_mentions",
    "handle_file_mention_intent",
]
