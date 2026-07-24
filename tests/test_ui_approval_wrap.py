"""Wrapped (multi/long-option) approval bar stacks options full-width, no clipping (#122)."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.approval_bar import ApprovalOption
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter

LONG_OPTIONS = (
    "Refactor the module first, then add tests",
    "Add tests first, then refactor",
    "Do both in parallel with a subagent",
    "Skip it and document the risk instead",
)


@pytest.mark.asyncio
async def test_wrapped_options_stack_fullwidth_and_stay_on_screen() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=(80, 24)) as pilot:
        app.present_approval("t1", "Which approach should I take?", LONG_OPTIONS)
        await pilot.pause()
        await pilot.pause()
        bar = app.approval_bar
        assert bar is not None and bar.has_class("-wrapped")
        chips = sorted(bar.query(ApprovalOption), key=lambda c: c.index)
        # Each option on its own row (distinct y), all within the terminal width.
        ys = [c.region.y for c in chips]
        assert len(set(ys)) == len(chips), f"options not stacked on distinct rows: {ys}"
        assert all(0 <= c.region.x and c.region.right <= app.size.width for c in chips), (
            f"an option is clipped off-screen: {[(c.index, c.region) for c in chips]}"
        )
        # The bar sits entirely above the footer (auto height reserved).
        assert bar.region.bottom <= app.footer_bar.region.y
        # Selection is visible and moves with arrows.
        assert chips[0].has_class("-selected")
        await pilot.press("down")
        assert bar.selected == 1
        assert sorted(bar.query(ApprovalOption), key=lambda c: c.index)[1].has_class("-selected")


@pytest.mark.asyncio
async def test_few_short_options_stay_on_one_row() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=(140, 24)) as pilot:
        app.present_approval("t1", "ok?", ("Allow once", "Allow always", "Deny"))
        await pilot.pause()
        await pilot.pause()
        bar = app.approval_bar
        assert bar is not None and not bar.has_class("-wrapped")
        chips = list(bar.query(ApprovalOption))
        assert len({c.region.y for c in chips}) == 1  # all on one row, unchanged
