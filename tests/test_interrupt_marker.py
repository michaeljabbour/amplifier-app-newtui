"""Model-visible context marker for an accepted Esc interrupt."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from amplifier_app_newtui.kernel.git_yield import GitDiffSnapshot
from amplifier_app_newtui.kernel.runtime import (
    TURN_ABORTED_MARKER,
    RealRuntime,
    restored_history,
)


@pytest.mark.asyncio
async def test_interrupt_appends_marker_before_end_of_turn_save() -> None:
    started = asyncio.Event()
    released = asyncio.Event()

    class Context:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = []

        async def add_message(self, message: dict[str, str]) -> None:
            self.messages.append(message)

    context = Context()

    class Cancellation:
        def request_graceful(self) -> None:
            released.set()

    class Coordinator:
        cancellation = Cancellation()

        def get(self, capability: str):  # noqa: ANN201 - focused fake
            return context if capability == "context" else None

    class Session:
        async def execute(self, prompt: str) -> str:
            del prompt
            started.set()
            await released.wait()
            return ""

    class Saver:
        def __init__(self) -> None:
            self.saved_messages: list[dict[str, str]] = []

        async def maybe_save(self) -> bool:
            self.saved_messages = list(context.messages)
            return True

    runtime = RealRuntime()
    runtime._initialized = SimpleNamespace(
        session_id="session-id", coordinator=Coordinator(), session=Session()
    )
    runtime._saver = Saver()

    async def no_diff() -> GitDiffSnapshot:
        return GitDiffSnapshot(False)

    runtime._capture_diff = no_diff  # type: ignore[method-assign]

    turn = asyncio.create_task(runtime.submit("start a long task"))
    await started.wait()
    assert await runtime.interrupt()
    assert await turn == ""

    marker = {"role": "assistant", "content": TURN_ABORTED_MARKER}
    assert context.messages == [marker]
    assert runtime._saver.saved_messages == [marker]
    assert restored_history(context.messages) == ()
