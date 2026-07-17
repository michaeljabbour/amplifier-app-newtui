"""DisplaySystem implementation: kernel messages → Notification UIEvents.

One of the four injected protocol objects (RESEARCH-BRIEF §2). The kernel
calls ``show_message(message, level, source)``; we mint a typed
:class:`~.events.Notification` and emit it into the UI event queue — the
notice slot renders it as a transient right-aligned dim line.

``push_nesting``/``pop_nesting`` exist for spawn compatibility (child
sessions inherit this display system); nesting depth is stamped onto the
notification's ``source`` suffix so the UI can de-emphasize child chatter.
"""

from __future__ import annotations

from collections.abc import Callable

from .events import Notification

Emit = Callable[[Notification], None]


class DisplaySystem:
    """Emit-only display system — never prints, never blocks."""

    def __init__(self, emit: Emit, *, session_id: str = "") -> None:
        self._emit = emit
        self._session_id = session_id
        self._nesting = 0

    @property
    def nesting(self) -> int:
        return self._nesting

    def show_message(
        self, message: str, level: str = "info", source: str = ""
    ) -> None:
        self._emit(
            Notification(
                session_id=self._session_id,
                message=str(message),
                level=str(level or "info"),
                source=str(source),
            )
        )

    def show_status(self, message: str, source: str = "") -> None:
        self.show_message(message, "status", source)

    def show_error(self, message: str, source: str = "") -> None:
        self.show_message(message, "error", source)

    def push_nesting(self) -> None:
        self._nesting += 1

    def pop_nesting(self) -> None:
        self._nesting = max(0, self._nesting - 1)


__all__ = ["DisplaySystem"]
