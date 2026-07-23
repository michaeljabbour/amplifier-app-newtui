"""Width-aware surface hint injected at ``provider:request`` (issue #35).

docs/BACKLOG.md section 2: the packaged bundle carries a *static* terminal
response contract, but nothing tells the model how WIDE the surface
currently is -- and a project/user bundle override can silently drop the
static contract entirely. This app-level hook injects a per-request,
width-aware surface hint for every active bundle, so pathological output
(wide tables, deep nesting) is prevented rather than rendered badly.

Mechanism mirrors :class:`~amplifier_app_newtui.kernel.clipboard.
ClipboardImageInjector`, NOT the steering bridge: it edits the root
session's context messages directly and returns ``continue``, instead of
returning ``inject_context``. That is deliberate -- the Rust hook registry
merges every ``provider:request`` ``inject_context`` result into ONE
message governed by a single ``ephemeral`` flag, so a second
``inject_context`` hook here would flip the steering bridge's *persistent*
steer to ephemeral and break rewind's turn accounting. Direct context
editing side-steps that collision entirely (the clipboard injector coexists
with steering for the same reason).

It keeps exactly ONE hint present: a single ``system`` message tagged with
:data:`SURFACE_HINT_SOURCE` in its metadata, refreshed in place whenever the
width changes (a resize therefore lands on the next turn's request) and
re-inserted if a ``/clear`` or compaction dropped it. Root session only --
subagents render through the root's summary, not the terminal. Because it is
app-level, it survives any bundle override.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from amplifier_core import HookResult

from ..model.terminal import TerminalSurface

SURFACE_HINT_SOURCE = "newtui-surface-hint"
"""Metadata marker identifying the single managed surface-hint message."""


def surface_hint_text(cols: int) -> str:
    """The width-aware surface-hint line for a *cols*-wide terminal."""
    return (
        f"terminal, ~{cols} cols; markdown subset: no images, "
        "tables \u22644 columns, prefer fenced code with language tags, "
        "short paragraphs."
    )


def _is_hint(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata")
    return isinstance(metadata, dict) and metadata.get("source") == SURFACE_HINT_SOURCE


def _hint_message(cols: int) -> dict[str, Any]:
    return {
        "role": "system",
        "content": surface_hint_text(cols),
        "metadata": {"source": SURFACE_HINT_SOURCE},
    }


class SurfaceHintInjector:
    """Keep one width-aware surface hint in the root session's context.

    Registered on ``provider:request`` (root only). ``prepare``-free: it
    reads the live width from the shared
    :class:`~amplifier_app_newtui.model.terminal.TerminalSurface` on every
    root request and reconciles the context to hold exactly one current hint.
    """

    EVENTS = ("provider:request",)

    def __init__(self, root_session_id: str, surface: TerminalSurface, context: Any) -> None:
        self._root_session_id = root_session_id
        self._surface = surface
        self._context = context

    def _can_edit(self) -> bool:
        return all(hasattr(self._context, m) for m in ("get_messages", "set_messages"))

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        if event != "provider:request" or not self._can_edit():
            return HookResult(action="continue")
        session_id = str(data.get("session_id") or self._root_session_id)
        if session_id != self._root_session_id:
            # Subagents render through the root's summary, not the terminal.
            return HookResult(action="continue")
        desired = surface_hint_text(self._surface.cols)
        messages = list(await self._context.get_messages())
        for index, message in enumerate(messages):
            if not _is_hint(message):
                continue
            if message.get("content") == desired:
                return HookResult(action="continue")  # already current: no write
            messages[index] = _hint_message(self._surface.cols)
            await self._context.set_messages(messages)
            return HookResult(action="continue")
        # No hint present (fresh turn, or dropped by /clear or compaction):
        # insert it right after the leading system block, before the dialogue.
        insert_at = 0
        while insert_at < len(messages) and messages[insert_at].get("role") == "system":
            insert_at += 1
        messages.insert(insert_at, _hint_message(self._surface.cols))
        await self._context.set_messages(messages)
        return HookResult(action="continue")

    def register_hooks(self, hooks: Any, *, priority: int = 940) -> Callable[[], None]:
        unregister = hooks.register(
            "provider:request",
            self.handle_event,
            priority=priority,
            name="newtui-surface-hint",
        )
        if not callable(unregister):
            return lambda: None

        def unregister_hook() -> None:
            unregister()

        return unregister_hook


__all__ = ["SURFACE_HINT_SOURCE", "SurfaceHintInjector", "surface_hint_text"]
