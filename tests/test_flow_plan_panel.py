"""Flow tests — ambient plan panel over the demo runtime
(docs/plans/2026-07-21-ambient-progress-design.md, Phase 1)."""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel.demo import BUILD_PROMPT
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.footer import footer_left_text

from .test_flow_helpers import SIZE, GatedDemoAdapter, blocks_of, seed_done, type_text, wait_for


@pytest.mark.asyncio
async def test_plan_panel_lights_up_mid_turn_and_collapses_when_done() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
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
async def test_plan_panel_stacks_and_remains_interactive_below_90_cols() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=(80, 18)) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: bool(app.plan_items))
        assert app.plan_panel.display
        assert app.query_one("#bottom-strip").has_class("plan-narrow")
        assert str(app.plan_panel.styles.width) == "100w"
        assert "Plan" not in footer_left_text(app.footer_bar.state)

        from amplifier_app_tui.model.blocks import TodoItem

        app.plan_panel.update_plan(
            tuple(
                TodoItem(
                    content=f"narrow task {i}",
                    status="in_progress" if i == 0 else "pending",
                )
                for i in range(20)
            )
        )
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.plan_panel.expanded
        assert app.plan_panel.max_scroll_y > 0
        assert app.composer.region.y + app.composer.region.height <= app.size.height
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        assert app.plan_panel.display


@pytest.mark.asyncio
async def test_expanded_plan_stays_bounded_and_composer_reachable_at_short_height() -> None:
    """S7 AC5, end-to-end: a long plan, expanded, at a short terminal --
    the panel bounds its own height and scrolls internally rather than
    pushing the composer off-screen."""
    from amplifier_app_tui.model.blocks import TodoItem
    from amplifier_app_tui.ui.plan_panel import plan_panel_max_height

    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=(100, 18)) as pilot:  # a short terminal
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: app.plan_panel.display)
        # A much longer plan than the demo script itself ever seeds.
        long_items = tuple(
            TodoItem(content=f"task {i}", status="in_progress" if i == 0 else "pending")
            for i in range(40)
        )
        app.plan_panel.update_plan(long_items)
        await pilot.pause()
        await pilot.click("#plan-overflow")
        await pilot.pause()
        assert app.plan_panel.expanded is True
        bound = plan_panel_max_height(app.size.height)
        assert app.plan_panel.size.height <= bound
        assert app.plan_panel.max_scroll_y > 0  # scrolls internally, not unbounded growth
        # The composer's full region still fits on screen: never pushed off
        # by the expanded panel.
        assert app.composer.region.y + app.composer.region.height <= app.size.height
        adapter.release()


# -- S7 gap 1: ctrl+h is a genuine keyboard-REACH path, not just an -------
# activate-once-focused affordance. These drive the real TuiApp + keymap
# dispatch (not the bare PlanPanel widget) so the assertions prove the chord
# focuses the actual control, toggles it, and provides a return path.


@pytest.mark.asyncio
async def test_ctrl_h_focuses_and_toggles_plan_overflow_without_prior_focus() -> None:
    from amplifier_app_tui.model.blocks import TodoItem

    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: app.plan_panel.display)
        long_items = tuple(
            TodoItem(content=f"task {i}", status="in_progress" if i == 0 else "pending")
            for i in range(10)
        )
        app.plan_panel.update_plan(long_items)
        await pilot.pause()
        assert app.plan_panel.expanded is False
        assert app.composer.has_focus_within  # composer owns focus, as always
        assert app.focused is not app.plan_panel.overflow_control  # never focused it

        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.plan_panel.expanded is True  # reached AND toggled in one press
        assert app.focused is app.plan_panel.overflow_control
        assert app.plan_panel.overflow_label.endswith("Show less")

        # Enter activates the selected control itself, proving the same
        # keyboard path named by AC1 rather than only a global-state bypass.
        await pilot.press("enter")
        await pilot.pause()
        assert app.plan_panel.expanded is False
        assert app.focused is app.plan_panel.overflow_control

        # Esc is the explicit return to normal typing.
        await pilot.press("escape")
        await pilot.pause()
        assert app.composer.has_focus_within
        await type_text(pilot, "hi")
        assert app.composer.text == "hi"
        adapter.release()


@pytest.mark.asyncio
async def test_ctrl_h_no_ops_with_a_notice_when_the_plan_panel_is_hidden() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        assert not app.plan_panel.display  # nothing seeded yet
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.notice_slot.current == "no plan panel to expand"
        assert app.composer.has_focus_within


@pytest.mark.asyncio
async def test_ctrl_h_no_ops_with_a_notice_when_nothing_is_hidden() -> None:
    from amplifier_app_tui.model.blocks import TodoItem

    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: app.plan_panel.display)
        # A short plan -- well within PLAN_MAX_ROWS, nothing to disclose.
        short_items = (
            TodoItem(content="only step", status="in_progress"),
            TodoItem(content="second step", status="pending"),
        )
        app.plan_panel.update_plan(short_items)
        await pilot.pause()
        assert app.plan_panel.overflow_control.display is False

        await pilot.press("ctrl+h")
        await pilot.pause()
        assert app.notice_slot.current == "plan · nothing hidden to expand"
        assert app.plan_panel.expanded is False
        adapter.release()


@pytest.mark.asyncio
async def test_ctrl_h_and_ctrl_n_stay_independent() -> None:
    """Requirement: the new chord must not remove/regress the existing
    ctrl+n drill-level cycling -- each keeps its own state untouched by
    the other, exactly as clicking/Enter already proved (module docstring
    composition rule)."""
    from amplifier_app_tui.model.blocks import TodoItem

    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: app.plan_panel.display)
        long_items = tuple(
            TodoItem(content=f"task {i}", status="in_progress" if i == 0 else "pending")
            for i in range(20)
        )
        app.plan_panel.update_plan(long_items)
        await pilot.pause()

        await pilot.press("ctrl+n")  # drill: default -> +2
        await pilot.pause()
        assert app.plan_panel.drill_extra == 2

        await pilot.press("ctrl+h")  # expand -- ctrl+n's own state untouched
        await pilot.pause()
        assert app.plan_panel.expanded is True
        assert app.plan_panel.drill_extra == 2

        await pilot.press("ctrl+h")  # collapse -- lands back on the drilled window
        await pilot.pause()
        assert app.plan_panel.expanded is False
        assert app.plan_panel.drill_extra == 2

        await pilot.press("ctrl+n")  # ctrl+n itself keeps working afterward
        await pilot.pause()
        assert app.plan_panel.drill_extra == 3
        adapter.release()
