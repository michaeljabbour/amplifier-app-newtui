"""Flow tests — ambient plan panel over the demo runtime
(docs/plans/2026-07-21-ambient-progress-design.md, Phase 1)."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import BUILD_PROMPT
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.footer import footer_left_text

from .test_flow_helpers import SIZE, GatedDemoAdapter, blocks_of, seed_done, wait_for


@pytest.mark.asyncio
async def test_plan_panel_lights_up_mid_turn_and_collapses_when_done() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        # parks at the first virtual wait: plan seeded + step 0 in progress
        assert await wait_for(
            pilot,
            lambda: (
                app.plan_panel.display
                and any(line.startswith("  ▶ ") for line in app.plan_panel.plan_lines)
            ),
        )
        assert app.plan_panel.plan_lines[0] == "Plan 0/3"
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        # all steps complete → collapsed to the header, still visible
        assert app.plan_panel.display
        assert app.plan_panel.plan_lines == ("Plan 3/3",)
        # D2: panel visible → the footer never shows the count twice
        assert "Plan" not in footer_left_text(app.footer_bar.state)
        # D3: the transcript never gets a live todo block
        assert blocks_of(app, "todo") == []


@pytest.mark.asyncio
async def test_plan_panel_hides_below_90_cols() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=(80, 40)) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: bool(app.plan_items))
        assert not app.plan_panel.display  # ladder: count-only below 90 cols
        assert "Plan 0/3" in footer_left_text(app.footer_bar.state)
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        assert not app.plan_panel.display
        assert "Plan 3/3" in footer_left_text(app.footer_bar.state)
