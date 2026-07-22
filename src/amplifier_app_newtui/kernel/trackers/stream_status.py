"""Channel-A stream tracker: live text/thinking tail state.

Hook-tracker pattern (EVENTS tuple, ``async handle_event -> HookResult``,
``register_hooks -> unregister``, ``add_listener``). Pure state — the app
wires listeners to Textual message posting.

Consumes the ad-hoc provider streaming events (``llm:stream_block_*``)
through :func:`kernel.events.normalize`, so provider payload variance
(``delta`` | ``text`` | ``content`` keys) is absorbed at the one boundary.
Root session only: child streams stay dark by design (lanes summarize
them). Blocks are keyed ``(request_id, block_index)``; non-text/thinking
block types (and thinking when ``show_thinking`` is off) are hidden.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from time import monotonic
from typing import Any

from amplifier_core import HookResult

from ..events import (
    StreamBlockDelta,
    StreamBlockEnd,
    StreamBlockStart,
    normalize,
)

logger = logging.getLogger(__name__)

_MAX_ACTIVE_BLOCKS = 8
_MAX_STREAM_CHARS = 16_384
_DELTA_NOTIFY_SECONDS = 0.05
_VISIBLE_KINDS = frozenset({"text", "thinking", "reasoning"})

Listener = Callable[[], None]
_BlockKey = tuple[str, int]


class StreamStatusTracker:
    """Track the active root-session stream without touching the terminal."""

    EVENTS = (
        "llm:stream_block_start",
        "llm:stream_block_delta",
        "llm:stream_block_end",
        "llm:stream_aborted",
        "provider:error",
        "provider:retry",
        "orchestrator:complete",
        "execution:end",
        "prompt:submit",
    )

    _RESET_EVENTS = frozenset(
        {
            "llm:stream_aborted",
            "provider:error",
            "provider:retry",
            "orchestrator:complete",
            "execution:end",
            "prompt:submit",
        }
    )

    def __init__(
        self,
        root_session_id: str,
        *,
        show_thinking: bool = False,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.root_session_id = root_session_id
        self.show_thinking = show_thinking
        self._clock = clock
        # key -> (kind, accumulated text, monotonic sequence)
        self._blocks: dict[_BlockKey, tuple[str, str, int]] = {}
        self._hidden: set[_BlockKey] = set()
        self._listeners: list[Listener] = []
        self._sequence = 0
        self._last_delta_notify = 0.0

    # -- state ---------------------------------------------------------------

    @property
    def preview(self) -> tuple[str, str] | None:
        """(kind, text) of the most recently touched visible block."""
        if not self._blocks:
            return None
        kind, text, _ = max(self._blocks.values(), key=lambda block: block[2])
        return (kind, text)

    @property
    def active_block_count(self) -> int:
        return len(self._blocks)

    @property
    def estimated_tokens(self) -> int:
        """Rough live token count before provider usage arrives (~4 chars/tok)."""
        characters = sum(len(text) for kind, text, _ in self._blocks.values() if kind == "text")
        return max(0, (characters + 3) // 4)

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # -- hook plumbing ---------------------------------------------------------

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        self.consume(event, data)
        return HookResult(action="continue")

    def register_hooks(self, hooks: Any, *, priority: int = 60) -> Callable[[], None]:
        unregister_callbacks: list[Callable[..., object]] = []
        for event in self.EVENTS:
            unregister = hooks.register(
                event,
                self.handle_event,
                priority=priority,
                name=f"newtui-stream-status-{event.replace(':', '-')}",
            )
            if callable(unregister):
                unregister_callbacks.append(unregister)

        def unregister_all() -> None:
            for unregister in reversed(unregister_callbacks):
                unregister()

        return unregister_all

    # -- consumption -----------------------------------------------------------

    def consume(self, event: str, data: dict[str, Any]) -> None:
        session_id = str((data or {}).get("session_id") or self.root_session_id)
        if session_id != self.root_session_id:
            return
        if event in self._RESET_EVENTS:
            self._blocks.clear()
            self._hidden.clear()
            self._notify()
            return
        normalized = normalize(event, data or {})
        if isinstance(normalized, StreamBlockStart):
            self._on_start(normalized)
        elif isinstance(normalized, StreamBlockDelta):
            self._on_delta(normalized)
        elif isinstance(normalized, StreamBlockEnd):
            self._on_end(normalized)

    def _on_start(self, event: StreamBlockStart) -> None:
        key = (event.request_id, event.block_index)
        self._hidden.discard(key)
        if not self._visible(event.block_type):
            self._hide(key)
            return
        self._store(key, event.block_type, "")
        self._notify()

    def _on_delta(self, event: StreamBlockDelta) -> None:
        key = (event.request_id, event.block_index)
        if key in self._hidden:
            return
        current_kind, current_text, _ = self._blocks.get(key, (event.block_type, "", 0))
        kind = event.block_type or current_kind
        if not self._visible(kind):
            self._blocks.pop(key, None)
            self._hide(key)
            return
        text = (current_text + event.text)[-_MAX_STREAM_CHARS:]
        self._store(key, kind, text)
        now = self._clock()
        if now - self._last_delta_notify < _DELTA_NOTIFY_SECONDS:
            return
        self._last_delta_notify = now
        self._notify()

    def _on_end(self, event: StreamBlockEnd) -> None:
        key = (event.request_id, event.block_index)
        self._blocks.pop(key, None)
        self._hidden.discard(key)
        self._notify()

    # -- helpers ----------------------------------------------------------------

    def _visible(self, kind: str) -> bool:
        if kind not in _VISIBLE_KINDS:
            return False
        if kind in {"thinking", "reasoning"} and not self.show_thinking:
            return False
        return True

    def _hide(self, key: _BlockKey) -> None:
        if len(self._hidden) >= _MAX_ACTIVE_BLOCKS:
            self._hidden.pop()
        self._hidden.add(key)

    def _store(self, key: _BlockKey, kind: str, text: str) -> None:
        if key not in self._blocks and len(self._blocks) >= _MAX_ACTIVE_BLOCKS:
            oldest = min(self._blocks, key=lambda item: self._blocks[item][2])
            self._blocks.pop(oldest)
        self._sequence += 1
        self._blocks[key] = (kind, text, self._sequence)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 — a bad listener must not break notify
                logger.debug("Stream status listener failed", exc_info=True)


__all__ = ["StreamStatusTracker"]
