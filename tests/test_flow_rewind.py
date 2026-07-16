"""Flow tests — DESIGN-SPEC §9: rewind & checkpoints.

End-to-end over DemoRuntime + Pilot: every turn rule records a
checkpoint; ctrl-r opens the picker on the newest checkpoint with
``‹ rewind › tN · $<cost> · <label>`` and ‹/› clamped navigation;
clicking a turn rule opens the picker at that checkpoint; Enter forks —
ledger and transcript trim to the checkpoint (confirm-then-trim).
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import footer_right_text
from amplifier_app_newtui.ui.rewind_strip import rewind_line

from .test_flow_helpers import SIZE, blocks_of, rules, seed_done, type_text, wait_for


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
async def test_fork_trims_transcript_and_ledger_to_checkpoint() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        assert blocks_of(app, "plan")  # the build turn left its plan block

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("left")  # select t1
        await pilot.press("enter")  # fork
        await pilot.pause()

        assert not app.rewind.display
        # Ledger trimmed: only t1 survives.
        assert [c.id for c in app.ledger.checkpoints] == ["t1"]
        # Transcript trimmed after the t1 rule (confirm-then-trim).
        last = app.transcript.blocks[-1]
        assert last.kind == "turn_rule" and last.checkpoint_id == "t1"
        assert not blocks_of(app, "plan")  # build-turn blocks are gone
        assert app.notice_slot.current == "forked from t1 · repo explainer · answer"
