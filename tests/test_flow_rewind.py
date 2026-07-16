"""Flow tests — DESIGN-SPEC §9: rewind & checkpoints.

End-to-end over DemoRuntime + Pilot: every turn rule records a
checkpoint; ctrl-r opens the picker on the newest checkpoint with
``‹ rewind › tN · $<cost> · <label>`` and ‹/› clamped navigation;
clicking a turn rule opens the picker at that checkpoint; Enter forks —
ledger and transcript trim to the checkpoint (confirm-then-trim).
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import BRAINSTORM_PROMPT
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import footer_right_text
from amplifier_app_newtui.ui.rewind_strip import rewind_line

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    rules,
    seed_done,
    type_text,
    wait_for,
)


async def _two_turns(pilot, app: NewTuiApp) -> None:
    """Seed (t1) + the build turn (t2, pytest approval allowed)."""
    await seed_done(pilot, app)
    await type_text(pilot, "hi")
    await pilot.press("enter")
    assert await wait_for(pilot, lambda: app.approval_bar is not None)
    await pilot.press("enter")  # Allow once
    assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)


@pytest.mark.asyncio
async def test_ctrl_r_opens_picker_on_newest_and_navigation_clamps() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        checkpoints = app.ledger.checkpoints
        assert [c.id for c in checkpoints] == ["t1", "t2"]
        # Checkpoints carry {id, label, cost-at-time}.
        assert checkpoints[0].label == "repo explainer · answer"
        assert checkpoints[1].label == "store refactor · shipped"

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.display
        # Newest selected by default; exact strip text.
        assert app.rewind.label_text == rewind_line(checkpoints[1])
        assert app.rewind.label_text.startswith("rewind › t2 · $")

        # ‹ / › navigate, clamped at both ends.
        await pilot.press("left")
        assert app.rewind.label_text == rewind_line(checkpoints[0])
        await pilot.press("left")
        assert app.rewind.label_text == rewind_line(checkpoints[0])  # clamped
        await pilot.press("right")
        assert app.rewind.label_text == rewind_line(checkpoints[1])
        await pilot.press("right")
        assert app.rewind.label_text == rewind_line(checkpoints[1])  # clamped

        # Esc closes the strip.
        await pilot.press("escape")
        await pilot.pause()
        assert not app.rewind.display
        assert app.footer_bar.state.context == "idle"
        assert footer_right_text(app.footer_bar.state) == (
            "/ commands · shift+tab mode · ctrl-t tasks"
        )


@pytest.mark.asyncio
async def test_clicking_turn_rule_opens_picker_at_that_checkpoint() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        first_rule = blocks_of(app, "turn_rule")[0]
        assert first_rule.checkpoint_id == "t1"
        widget = app.query_one(f"#block-{first_rule.id}")
        widget.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(f"#block-{first_rule.id}")
        await pilot.pause()
        assert app.rewind.display
        current = app.rewind.current
        assert current is not None and current.id == "t1"


@pytest.mark.asyncio
async def test_typing_passes_through_focused_rewind_strip_to_composer() -> None:
    """Mockup keydown (the composer input keeps focus while rewindOpen):
    printable keys typed while the strip holds focus are never swallowed —
    '/' opens the palette live-filtered and text lands in the composer (§5)."""
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.has_focus

        # '/led' reaches the composer and opens the palette live-filtered.
        await pilot.press("/", "l", "e", "d")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        assert app.composer.text == "/led"
        assert app.composer.has_focus_within
        assert app.rewind.display  # the strip stays open

        # Esc closes the palette first (ESC_CHAIN); reset the input.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: not app.palette.is_open)
        assert app.rewind.display
        app.composer.clear()

        # Refocus the strip and type plain text: it lands in the composer.
        app.rewind.focus()
        await pilot.pause()
        await pilot.press("h", "i")
        assert await wait_for(pilot, lambda: app.composer.text == "hi")
        assert app.composer.has_focus_within

        # ←→/enter still belong to the strip when it holds focus.
        app.rewind.focus()
        await pilot.pause()
        await pilot.press("left")
        assert app.rewind.label_text == rewind_line(app.ledger.checkpoints[0])


@pytest.mark.asyncio
async def test_checkpoint_cut_while_picker_open_is_navigable() -> None:
    """Mockup openRewind/rewindNext read the live this.checkpoints array:
    a checkpoint cut while the picker is open is immediately navigable
    with › — no reopen needed (§9)."""
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)  # t1 cut
        app.submit_prompt(BRAINSTORM_PROMPT)  # no approvals: strip keeps focus
        assert await wait_for(
            pilot, lambda: app.turn_active and blocks_of(app, "narration")
        )

        # Open the picker mid-turn: only t1 exists, › clamps on it.
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.display
        assert app.rewind.label_text == rewind_line(app.ledger.checkpoints[0])
        await pilot.press("right")
        assert app.rewind.label_text == rewind_line(app.ledger.checkpoints[0])

        # Let the turn finish: t2 is cut while the strip stays open.
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        checkpoints = app.ledger.checkpoints
        assert [c.id for c in checkpoints] == ["t1", "t2"]
        assert app.rewind.display
        # Cursor stays where it was (t1)…
        assert app.rewind.label_text == rewind_line(checkpoints[0])
        # …and › now reaches the freshly cut t2 without reopening.
        await pilot.press("right")
        assert app.rewind.label_text == rewind_line(checkpoints[1])


@pytest.mark.asyncio
async def test_fork_trims_transcript_and_ledger_to_checkpoint() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        assert blocks_of(app, "plan")  # the build turn left its plan block

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("left")  # select t1
        await pilot.press("enter")  # fork (backend confirms, then trims)
        assert await wait_for(
            pilot, lambda: [c.id for c in app.ledger.checkpoints] == ["t1"]
        )

        assert not app.rewind.display
        # Transcript trimmed after the t1 rule (confirm-then-trim).
        last = app.transcript.blocks[-1]
        assert last.kind == "turn_rule" and last.checkpoint_id == "t1"
        assert not blocks_of(app, "plan")  # build-turn blocks are gone
        assert app.notice_slot.current == "forked from t1 · repo explainer · answer"
