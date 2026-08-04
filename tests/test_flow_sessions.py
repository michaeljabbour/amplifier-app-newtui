"""Flow tests -- S2 compliance: the interactive sessions picker.

End-to-end over DemoRuntime + Pilot: ``/sessions`` opens the picker strip
(never posts straight to the transcript any more); |up-down-arrow| moves the
highlight and Enter activates it (keyboard parity); clicking any row
activates it directly (mouse parity); activating a row posts the full-id
detail block and best-effort copies the full id; Esc closes the picker
ahead of the running-interrupt in the esc chain (matches the palette/
rewind precedent).
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.footer import footer_right_text
from amplifier_app_tui.ui.sessions_strip import _SessionRow

from .test_flow_helpers import SIZE, GatedDemoAdapter, blocks_of, seed_done, type_text, wait_for


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
            == "\u2191\u2193 select \u00b7 enter open \u00b7 esc close"
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
