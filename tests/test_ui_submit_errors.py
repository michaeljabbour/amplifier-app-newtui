"""A raised turn must not tear down the TUI (regression for #21).

``submit_prompt`` schedules ``adapter.submit`` on a Textual worker, which
defaults to ``exit_on_error=True`` — an exception from ``submit`` (provider auth
expiry, network drop mid-turn) used to crash the whole app. The fix wraps the
call so the error surfaces as a notice and the session stays live.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui import app_support
from amplifier_app_tui.ui.runtime_adapter import RuntimeAdapter


class _RaisingAdapter(RuntimeAdapter):
    """Boots clean (instant ready), then fails the turn like a dropped provider."""

    async def submit(self, _text: str, _attachments: tuple[Any, ...] = ()) -> None:
        raise RuntimeError("provider auth expired")


async def _wait_for(pilot, predicate, *, tries: int = 120) -> bool:  # noqa: ANN001
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


def test_checkpoint_draft_cache_filters_stale_ids_and_evicts_oldest_by_bytes(
    monkeypatch,
) -> None:
    from amplifier_app_tui.model.turn import TurnOutcome, TurnTelemetry
    from amplifier_app_tui.ui import app as app_module
    from amplifier_app_tui.ui.composer import ComposerDraft

    app = TuiApp(RuntimeAdapter())
    for turn_id in (1, 2):
        app.ledger.record_turn(
            TurnTelemetry(secs=1, tokens_down=1, cost=Decimal("0")),
            TurnOutcome(kind="answer"),
            turn_id=turn_id,
            message_index=turn_id,
            restore_turn_id=turn_id - 1,
        )

    def draft(payload: str) -> ComposerDraft:
        return ComposerDraft(
            text="[Pasted #1 · 20 lines]",
            pastes={"[Pasted #1 · 20 lines]": payload},
            paste_seq=1,
            attachments=[],
            image_seq=0,
        )

    orphan = draft("orphan")
    oldest = draft("123456")
    newest = draft("abcdef")
    app._checkpoint_drafts = {"t0": orphan, "t1": oldest, "t2": newest}
    monkeypatch.setattr(app_module, "MAX_CHECKPOINT_DRAFT_BYTES", newest.sidecar_bytes)

    app.reconcile_checkpoint_drafts()

    assert app._checkpoint_drafts == {"t2": newest}
    assert sum(item.sidecar_bytes for item in app._checkpoint_drafts.values()) <= (
        app_module.MAX_CHECKPOINT_DRAFT_BYTES
    )


@pytest.mark.asyncio
async def test_turn_exception_shows_notice_and_keeps_app_alive() -> None:
    adapter = _RaisingAdapter()
    app = TuiApp(adapter)

    notices: list[str] = []
    async with app.run_test(size=(110, 40)) as pilot:
        # Boot is instant (base start() calls ready immediately); no splash guard.
        assert await _wait_for(pilot, lambda: app._splash is None)

        real_show_notice = app.show_notice
        app.show_notice = lambda text, duration=None: (  # type: ignore[method-assign]
            notices.append(text),
            real_show_notice(text, duration),
        )[-1]

        app.submit_prompt("hello")

        # The failing turn's notice lands and the app is still running — with the
        # old code the worker's re-raise (exit_on_error=True) would have stopped
        # the app and this wait would time out instead.
        assert await _wait_for(pilot, lambda: any("turn failed" in n for n in notices))
        assert any("provider auth expired" in n for n in notices)
        assert app.is_running

        # Still interactive: a second submit is accepted, not a dead app.
        app.submit_prompt("again")
        await pilot.pause(0.1)
        assert app.is_running


@pytest.mark.asyncio
async def test_cancelled_turn_is_not_reported_as_failure() -> None:
    """A real shutdown mid-turn (CancelledError is BaseException) must not be
    misread as a turn failure — only Exception is caught."""

    class _CancelAdapter(RuntimeAdapter):
        async def submit(self, _text: str, _attachments: tuple[Any, ...] = ()) -> None:
            raise asyncio.CancelledError

    adapter = _CancelAdapter()
    app = TuiApp(adapter)
    notices: list[str] = []
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: app._splash is None)
        real = app.show_notice
        app.show_notice = lambda text, duration=None: (  # type: ignore[method-assign]
            notices.append(text),
            real(text, duration),
        )[-1]
        app.submit_prompt("hello")
        await pilot.pause(0.2)
        assert not any("turn failed" in n for n in notices)


@pytest.mark.asyncio
async def test_checkpoint_preflight_failure_restores_exact_prompt_and_image() -> None:
    from amplifier_app_tui.kernel.checkpoints import WorkspaceCheckpointUnavailableError
    from amplifier_app_tui.kernel.clipboard import ImageAttachment

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    image = ImageAttachment(png, "image/png")

    class _CheckpointFailureAdapter(RuntimeAdapter):
        async def submit(self, _text: str, _attachments: tuple[Any, ...] = ()) -> None:
            raise WorkspaceCheckpointUnavailableError(
                "workspace checkpoint could not be created; message was not sent"
            )

    app = TuiApp(_CheckpointFailureAdapter())
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: app._splash is None)
        app.submit_prompt("inspect [Image #1]", (image,))

        assert await _wait_for(
            pilot,
            lambda: app.composer.text == "inspect [Image #1]",
        )
        assert app.composer._staged_attachments(app.composer.text) == (image,)
        assert "message was not sent" in app.notice_slot.current
        assert not app.ledger.turns


@pytest.mark.asyncio
async def test_queue_drain_preflight_rejection_requeues_exact_rich_capsule() -> None:
    """A rejected auto-drain must not flatten or lose the consumed item."""
    from amplifier_app_tui.kernel.checkpoints import WorkspaceCheckpointUnavailableError
    from amplifier_app_tui.kernel.clipboard import ImageAttachment

    image = ImageAttachment(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "image/png")

    class _QueuedCheckpointFailureAdapter(RuntimeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def submit_queued(
            self,
            _text: str,
            _attachments: tuple[Any, ...] = (),
        ) -> None:
            self.calls += 1
            raise WorkspaceCheckpointUnavailableError(
                "workspace checkpoint could not be created; message was not sent"
            )

    adapter = _QueuedCheckpointFailureAdapter()
    app = TuiApp(adapter)
    payload = "\n".join(f"pasted row {index}" for index in range(20))
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: app._splash is None)
        stub = app.composer.register_paste(payload)
        assert stub is not None
        app.composer.insert_text(f"inspect {stub} ")
        app.composer.add_image(image)
        visible = app.composer.text
        draft = app.composer._snapshot_draft()
        expanded = app.composer._expand(visible).strip()
        attachments = app.composer._staged_attachments(visible)
        queued = adapter.steering.enqueue(
            expanded,
            kind="next_turn",
            attachments=attachments,
            draft=draft,
        )
        app.queued_strip.show_queued(expanded)
        app.composer.clear()

        app_support.finish_turn_queues(app)

        assert await _wait_for(pilot, lambda: bool(adapter.steering.pending_next_turn))
        restored = adapter.steering.pending_next_turn[0]
        assert restored is queued
        assert restored.attachments == (image,)
        assert restored.draft is draft
        assert adapter.calls == 1
        assert app.is_running
        assert app.queued_strip.queued == expanded
        assert "message was not sent" in app.notice_slot.current
        assert "queued message kept" in app.notice_slot.current

        # The same capsule is still recallable with its compact paste stub and
        # binary image sidecar, proving that the failure path never flattened it.
        app.action_recall_queued()
        await pilot.pause()
        assert app.composer.text == visible
        assert payload in app.composer._expand(app.composer.text)
        assert app.composer._staged_attachments(app.composer.text) == (image,)


@pytest.mark.asyncio
async def test_queue_drain_provider_exception_is_contained() -> None:
    class _QueuedProviderFailureAdapter(RuntimeAdapter):
        async def submit_queued(
            self,
            text: str,
            _attachments: tuple[Any, ...] = (),
        ) -> None:
            # Model a failure *after* runtime admission. PromptSubmit is the
            # app's acknowledgement boundary; pre-admission exceptions are
            # intentionally requeued instead of reported as consumed turns.
            from amplifier_app_tui.kernel import events as ev

            self.app.reducer.handle(ev.PromptSubmit(session_id="root-session", prompt=text, ts=1.0))
            self.app.reducer.handle(
                ev.PromptComplete(session_id="root-session", response="", ts=2.0)
            )
            raise RuntimeError("provider auth expired")

    adapter = _QueuedProviderFailureAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: app._splash is None)
        adapter.steering.enqueue("run next", kind="next_turn")

        app_support.finish_turn_queues(app)

        assert await _wait_for(pilot, lambda: "queued turn failed" in app.notice_slot.current)
        assert "provider auth expired" in app.notice_slot.current
        assert app.is_running


@pytest.mark.asyncio
async def test_manual_submit_cannot_race_queue_admission_or_poison_rich_checkpoint() -> None:
    """Only one prompt may occupy the pre-PromptSubmit admission window.

    Turn-end queue drain consumes its capsule before the worker runs. A manual
    send during that tiny window must stay in the composer, while a later
    runtime rejection must put the original queue object back and remove its
    predicted tN rich-draft mapping.
    """
    from amplifier_app_tui.kernel.clipboard import ImageAttachment

    class _BlockedQueuedAdapter(RuntimeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def submit_queued(
            self,
            _text: str,
            _attachments: tuple[Any, ...] = (),
        ) -> None:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            raise RuntimeError("another turn is already running")

    def image(seed: int) -> ImageAttachment:
        return ImageAttachment(b"\x89PNG\r\n\x1a\n" + bytes([seed]) * 40, "image/png")

    adapter = _BlockedQueuedAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: app._splash is None)

        queued_payload = "\n".join(f"queued row {index}" for index in range(20))
        queued_stub = app.composer.register_paste(queued_payload)
        assert queued_stub is not None
        app.composer.insert_text(f"queue {queued_stub} ")
        app.composer.add_image(image(1))
        queued_visible = app.composer.text
        queued_draft = app.composer._snapshot_draft()
        queued = adapter.steering.enqueue(
            app.composer._expand(queued_visible).strip(),
            kind="next_turn",
            attachments=app.composer._staged_attachments(queued_visible),
            draft=queued_draft,
        )
        app.queued_strip.show_queued(queued.text)
        app.composer.clear()

        app_support.finish_turn_queues(app)
        assert await _wait_for(pilot, adapter.started.is_set)
        assert app._submit_accepting is True
        assert adapter.calls == 1
        assert list(app._checkpoint_drafts) == ["t1"]

        manual_payload = "\n".join(f"manual row {index}" for index in range(20))
        manual_stub = app.composer.register_paste(manual_payload)
        assert manual_stub is not None
        app.composer.insert_text(f"manual {manual_stub} ")
        manual_image = image(2)
        app.composer.add_image(manual_image)
        manual_visible = app.composer.text
        manual_draft = app.composer._snapshot_draft()
        manual_expanded = app.composer._expand(manual_visible).strip()
        manual_attachments = app.composer._staged_attachments(manual_visible)
        app.composer.clear()  # Composer.handle_enter clears before app delivery.

        app.submit_prompt(manual_expanded, manual_attachments, manual_draft)
        await pilot.pause()

        # The second submit never reached the adapter or claimed the same t1.
        assert adapter.calls == 1
        assert list(app._checkpoint_drafts) == ["t1"]
        assert app.composer.text == manual_visible
        assert manual_payload in app.composer._expand(app.composer.text)
        assert app.composer._staged_attachments(app.composer.text) == (manual_image,)
        assert "message kept" in app.notice_slot.current

        adapter.release.set()
        assert await _wait_for(pilot, lambda: bool(adapter.steering.pending_next_turn))
        assert adapter.steering.pending_next_turn[0] is queued
        assert app._checkpoint_drafts == {}
        assert app._submit_accepting is False
        assert app.composer.text == manual_visible
        assert "queued message kept" in app.notice_slot.current


@pytest.mark.asyncio
async def test_completed_submit_worker_cannot_clear_successor_queue_admission() -> None:
    """An older worker's finally must not release a newer worker's fence."""

    class _OverlappingHandoffAdapter(RuntimeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.first_closed = asyncio.Event()
            self.first_release = asyncio.Event()
            self.queued_started = asyncio.Event()
            self.queued_release = asyncio.Event()
            self.submit_calls = 0
            self.queued_calls = 0

        async def submit(self, text: str, _attachments: tuple[Any, ...] = ()) -> None:
            from amplifier_app_tui.kernel import events as ev

            self.submit_calls += 1
            self.app.reducer.handle(ev.PromptSubmit(session_id="root-session", prompt=text, ts=1.0))
            self.app.reducer.handle(
                ev.PromptComplete(session_id="root-session", response="done", ts=2.0)
            )
            self.first_closed.set()
            # Keep the original submit coroutine alive after turn_finished;
            # a real cross-thread future can resume in this same order.
            await self.first_release.wait()

        async def submit_queued(
            self,
            _text: str,
            _attachments: tuple[Any, ...] = (),
        ) -> None:
            self.queued_calls += 1
            self.queued_started.set()
            await self.queued_release.wait()

    adapter = _OverlappingHandoffAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: app._splash is None)

        app.submit_prompt("first")
        assert await _wait_for(pilot, adapter.first_closed.is_set)
        assert not app.turn_active

        adapter.steering.enqueue("queued successor", kind="next_turn")
        app.drain_turn_queues()
        assert await _wait_for(pilot, adapter.queued_started.is_set)
        successor_admission = app._submit_admission
        assert successor_admission is not None
        assert app._submit_accepting is True

        adapter.first_release.set()
        await pilot.pause(0.1)

        assert app._submit_admission is successor_admission
        assert app._submit_accepting is True

        app.submit_prompt("manual during successor admission")
        await pilot.pause()
        assert adapter.submit_calls == 1
        assert adapter.queued_calls == 1
        assert app.composer.text == "manual during successor admission"
        assert "message kept" in app.notice_slot.current

        adapter.queued_release.set()
        assert await _wait_for(pilot, lambda: app._submit_admission is None)
        assert app._submit_accepting is False
