"""SurfaceHintInjector: one width-aware surface hint kept current in the root
session's context at each ``provider:request`` (issue #35).

It edits context directly and returns ``continue`` (the ClipboardImageInjector
mechanism), so it never collides with the steering bridge's persistent
``inject_context`` at the same boundary. Pure asyncio, no engine."""

from __future__ import annotations

from typing import Any

import pytest

from amplifier_app_newtui.kernel.surface_hint import (
    SURFACE_HINT_SOURCE,
    SurfaceHintInjector,
    surface_hint_text,
)
from amplifier_app_newtui.model.terminal import TerminalSurface

ROOT = "sess-root"


class FakeContext:
    """Minimal get/set message store mirroring the offline FakeContext."""

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self._messages = [dict(m) for m in (messages or [])]
        self.set_calls = 0

    async def get_messages(self) -> list[dict[str, Any]]:
        return [dict(m) for m in self._messages]

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self.set_calls += 1
        self._messages = [dict(m) for m in messages]


class FakeHooks:
    def __init__(self) -> None:
        self.registered: list[tuple[str, int, str]] = []
        self.unregistered: list[str] = []

    def register(self, event: str, handler: Any, *, priority: int = 0, name: str = "") -> Any:
        self.registered.append((event, priority, name))
        return lambda: self.unregistered.append(name)


def _hints(context: FakeContext) -> list[dict[str, Any]]:
    return [
        m
        for m in context._messages
        if isinstance(m.get("metadata"), dict)
        and m["metadata"].get("source") == SURFACE_HINT_SOURCE
    ]


def test_hint_text_carries_width_and_markdown_subset() -> None:
    text = surface_hint_text(97)
    assert "~97 cols" in text
    assert "no images" in text
    assert "tables \u22644 columns" in text
    assert "fenced code with language tags" in text


@pytest.mark.asyncio
async def test_injects_current_width_as_one_system_message() -> None:
    context = FakeContext([{"role": "system", "content": "system prompt"}])
    injector = SurfaceHintInjector(ROOT, TerminalSurface(120), context)

    result = await injector.handle_event("provider:request", {"session_id": ROOT})
    assert result.action == "continue"

    hints = _hints(context)
    assert len(hints) == 1
    assert hints[0]["role"] == "system"
    assert "~120 cols" in hints[0]["content"]
    # Placed right after the leading system prompt, before the dialogue.
    assert context._messages[0]["content"] == "system prompt"
    assert (
        context._messages[1] is hints[0] or context._messages[1]["content"] == hints[0]["content"]
    )


@pytest.mark.asyncio
async def test_hint_tracks_a_resize_in_place_without_duplicating() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}, {"role": "user", "content": "hi"}])
    surface = TerminalSurface(80)
    injector = SurfaceHintInjector(ROOT, surface, context)

    await injector.handle_event("provider:request", {"session_id": ROOT})
    assert "~80 cols" in _hints(context)[0]["content"]

    surface.set_cols(40)  # the user narrows the terminal mid-session
    await injector.handle_event("provider:request", {"session_id": ROOT})
    hints = _hints(context)
    assert len(hints) == 1  # updated in place, never a second hint
    assert "~40 cols" in hints[0]["content"]
    assert "~80 cols" not in hints[0]["content"]
    # The user prompt is untouched.
    assert any(m.get("content") == "hi" for m in context._messages)


@pytest.mark.asyncio
async def test_already_current_hint_is_not_rewritten() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = SurfaceHintInjector(ROOT, TerminalSurface(100), context)

    await injector.handle_event("provider:request", {"session_id": ROOT})
    writes_after_first = context.set_calls
    await injector.handle_event("provider:request", {"session_id": ROOT})
    # Width unchanged and hint present -> no redundant set_messages.
    assert context.set_calls == writes_after_first


@pytest.mark.asyncio
async def test_reinserts_hint_if_context_was_cleared() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = SurfaceHintInjector(ROOT, TerminalSurface(90), context)
    await injector.handle_event("provider:request", {"session_id": ROOT})
    assert len(_hints(context)) == 1

    # Simulate /clear or compaction dropping the managed message.
    context._messages = [{"role": "system", "content": "sp"}]
    await injector.handle_event("provider:request", {"session_id": ROOT})
    assert len(_hints(context)) == 1
    assert "~90 cols" in _hints(context)[0]["content"]


@pytest.mark.asyncio
async def test_child_session_is_left_alone() -> None:
    # Subagents render through the root's summary, not the terminal surface.
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = SurfaceHintInjector(ROOT, TerminalSurface(120), context)
    result = await injector.handle_event("provider:request", {"session_id": "sess-child_worker"})
    assert result.action == "continue"
    assert _hints(context) == []
    assert context.set_calls == 0


@pytest.mark.asyncio
async def test_missing_session_id_defaults_to_root_and_injects() -> None:
    context = FakeContext()
    injector = SurfaceHintInjector(ROOT, TerminalSurface(64), context)
    await injector.handle_event("provider:request", {})
    assert "~64 cols" in _hints(context)[0]["content"]


@pytest.mark.asyncio
async def test_non_provider_request_events_are_ignored() -> None:
    context = FakeContext([{"role": "system", "content": "sp"}])
    injector = SurfaceHintInjector(ROOT, TerminalSurface(), context)
    result = await injector.handle_event("tool:pre", {"session_id": ROOT})
    assert result.action == "continue"
    assert context.set_calls == 0


@pytest.mark.asyncio
async def test_context_without_set_messages_is_a_safe_noop() -> None:
    class ReadOnly:
        async def get_messages(self) -> list[dict[str, Any]]:
            return []

    injector = SurfaceHintInjector(ROOT, TerminalSurface(80), ReadOnly())
    result = await injector.handle_event("provider:request", {"session_id": ROOT})
    assert result.action == "continue"


def test_register_hooks_priority_and_name() -> None:
    hooks = FakeHooks()
    injector = SurfaceHintInjector(ROOT, TerminalSurface(), FakeContext())
    unregister = injector.register_hooks(hooks)
    assert hooks.registered == [("provider:request", 940, "newtui-surface-hint")]
    unregister()
    assert hooks.unregistered == ["newtui-surface-hint"]


def test_register_hooks_tolerates_non_callable_unregister() -> None:
    class NullHooks:
        def register(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    injector = SurfaceHintInjector(ROOT, TerminalSurface(), FakeContext())
    injector.register_hooks(NullHooks())()  # must hand back a no-op, never crash
