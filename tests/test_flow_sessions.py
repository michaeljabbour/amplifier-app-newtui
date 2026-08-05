"""Flow tests -- S2 compliance: the interactive sessions picker.

End-to-end over DemoRuntime + Pilot: ``/sessions`` opens the picker strip
(never posts straight to the transcript any more); |up-down-arrow| moves the
highlight and Enter opens detail (keyboard parity); ``r`` completes a
typed shutdown-and-resume handoff for the highlighted row; clicking any
row activates it directly (mouse parity); activating a row posts the full-id
detail block and best-effort copies the full id; Esc closes the picker
ahead of the running-interrupt in the esc chain (matches the palette/
rewind precedent).
"""

from __future__ import annotations

import asyncio

import pytest

from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.footer import footer_right_text
from amplifier_app_tui.kernel.session_manager import SessionSummary
from amplifier_app_tui.ui.sessions_strip import ResumeSessionRequest, _SessionRow

from .test_flow_helpers import SIZE, GatedDemoAdapter, blocks_of, seed_done, type_text, wait_for


@pytest.mark.parametrize("snapshot_kind", ["branch", "fork"])
@pytest.mark.asyncio
async def test_session_snapshot_fences_clear_and_keeps_the_next_prompt(
    snapshot_kind: str,
) -> None:
    """Branch/fork reads one idle context while later input waits visibly."""
    started = asyncio.Event()
    release = asyncio.Event()
    clear_called = False

    async def _delayed_snapshot(_value: str) -> tuple[bool, str]:
        started.set()
        await release.wait()
        return (True, "a1b2c3d4e5f6")

    async def _unexpected_clear() -> tuple[bool, int]:
        nonlocal clear_called
        clear_called = True
        raise AssertionError("clear crossed the snapshot fence")

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.adapter.clear_context = _unexpected_clear
    if snapshot_kind == "branch":
        app.adapter.branch_session = _delayed_snapshot
    else:
        app.adapter.fork_with_directive = _delayed_snapshot

    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        if snapshot_kind == "branch":
            app.branch_session("test-snapshot")
        else:
            app.fork_session("continue safely")
        assert await wait_for(pilot, started.is_set)
        assert app.session_ops.context_snapshot_pending

        await type_text(pilot, "send after snapshot")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.composer.text == "send after snapshot")
        assert app.notice_slot.current == "session snapshot in progress · message kept"

        app.session_ops.clear_context()
        await pilot.pause()
        assert not clear_called
        assert app.notice_slot.current == "session snapshot in progress · clear unavailable"

        release.set()
        assert await wait_for(pilot, lambda: not app.session_ops.context_snapshot_pending)
        await pilot.press("enter")
        assert await wait_for(
            pilot,
            lambda: (
                app.ledger.checkpoints[-1].label == "send after snapshot" and not app.turn_active
            ),
        )


@pytest.mark.asyncio
async def test_slash_sessions_opens_the_picker_not_a_transcript_post() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "/sessions")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.sessions_strip.is_open)
        rows = list(app.sessions_strip.query(_SessionRow))
        assert len(rows) == 2
        # The live demo session (DEMO_SESSION_ID) is the current-marked row.
        assert app.sessions_strip.selected_summary is not None
        # Opening the picker posts NOTHING new to the transcript -- it
        # replaced the old plain roster post (S2 gap 2). The seed turn's
        # own answer block is expected to already be there.
        assert len(blocks_of(app, "answer")) == 1
        # Footer hints swap to the sessions picker set.
        assert app.footer_bar.state.context == "sessions"
        assert (
            footer_right_text(app.footer_bar.state)
            == "\u2191\u2193 select \u00b7 enter open \u00b7 r resume \u00b7 esc close"
        )


@pytest.mark.asyncio
async def test_arrow_keys_and_enter_open_full_id_detail_keyboard_parity() -> None:
    """Keyboard parity (S2 gap 2) + full-id detail/copy (S2 gap 1)."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    copied: list[str] = []
    app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "/sessions")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.sessions_strip.is_open)

        await pilot.press("down")
        await pilot.pause()
        assert app.sessions_strip.selected_summary is not None
        assert app.sessions_strip.selected_summary.session_id == "b1f4c209aa"

        await pilot.press("enter")
        await pilot.pause()
        assert not app.sessions_strip.is_open  # activating closes the picker
        assert copied == ["b1f4c209aa"]  # best-effort clipboard copy fired

        answers = blocks_of(app, "answer")
        assert answers
        detail_text = "".join(seg.text for seg in answers[-1].spans)
        assert "b1f4c209aa" in detail_text  # the FULL id, unambiguous
        assert "backend api sweep" in detail_text


@pytest.mark.asyncio
async def test_arrow_keys_and_r_request_actual_selected_session_resume() -> None:
    """Samuel S2 AC4: keyboard selection plus ``r`` reaches the app's real
    typed resume handoff; it does not merely append a command to scrollback."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    copied: list[str] = []
    app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "/sessions")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.sessions_strip.is_open)

        await pilot.press("down")
        await pilot.press("r")
        await pilot.pause()

    assert app.return_value == ResumeSessionRequest("b1f4c209aa")
    assert copied == ["amplifier-tui resume b1f4c209"]
    # Resume is an app result, not a misleading transcript-only success.
    assert len(blocks_of(app, "answer")) == 1


@pytest.mark.asyncio
async def test_r_refuses_unresumable_row_without_exiting(monkeypatch) -> None:
    """Damaged rows remain explicit and never turn the alternate screen
    into a deeper boot failure. ``transcript_lost`` is resumable elsewhere;
    ``indexing`` is not (same contract as resolve_for_resume)."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    exits: list[ResumeSessionRequest | None] = []

    def capture_exit(result: ResumeSessionRequest | None = None, **_kwargs: object) -> None:
        exits.append(result)

    app.exit = capture_exit  # type: ignore[method-assign]
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        async def _damaged() -> tuple[SessionSummary, ...]:
            return (SessionSummary(session_id="deadbeef1234", state="indexing"),)

        monkeypatch.setattr(app.adapter, "session_summaries", _damaged)
        await type_text(pilot, "/sessions")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.sessions_strip.is_open)
        await pilot.press("r")
        await pilot.pause()

        assert exits == []
        assert "cannot resume · session is indexing" in app.notice_slot.current


@pytest.mark.asyncio
async def test_click_any_row_activates_it_mouse_parity() -> None:
    """Mouse parity (S2 gap 2): a row click activates immediately -- no
    separate select-then-activate step, mirroring the command palette."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "/sessions")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.sessions_strip.is_open)

        rows = list(app.sessions_strip.query(_SessionRow))
        target_id = f"#{rows[1].id}"
        await pilot.click(target_id)
        await pilot.pause()
        assert not app.sessions_strip.is_open
        answers = blocks_of(app, "answer")
        detail_text = "".join(seg.text for seg in answers[-1].spans)
        assert "b1f4c209aa" in detail_text


@pytest.mark.asyncio
async def test_esc_closes_sessions_picker_before_interrupting_running_turn() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "hi")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.turn_active)

        await type_text(pilot, "/sessions")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.sessions_strip.is_open)

        await pilot.press("escape")
        await pilot.pause()
        assert not app.sessions_strip.is_open
        assert app.turn_active  # the running turn was NOT interrupted

        await pilot.press("escape")
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)


@pytest.mark.asyncio
async def test_no_stored_sessions_shows_notice_not_an_empty_picker(monkeypatch) -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        async def _empty() -> tuple:
            return ()

        monkeypatch.setattr(app.adapter, "session_summaries", _empty)
        await type_text(pilot, "/sessions")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.sessions_strip.is_open
        assert await wait_for(pilot, lambda: "no stored sessions" in app.notice_slot.current)
