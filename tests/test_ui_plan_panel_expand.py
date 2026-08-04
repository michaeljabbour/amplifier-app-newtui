"""Widget-level tests for the plan panel's expand/collapse control (S7
compliance, 2026-08-02): "+N more" becomes a focusable, clickable control
that expands the hidden rows in place and flips to a reversible "Show
less" control. Pure-function coverage (the ``expanded`` branch of
``format_plan_body_and_control``) lives in ``test_ui_plan_panel.py``; this
file drives the actual mounted widget with a Textual Pilot, matching the
``LanesHost`` pattern in ``test_ui_lanes.py``.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_tui.model.blocks import TodoItem
from amplifier_app_tui.ui.plan_panel import (
    PLAN_MAX_ROWS,
    PLAN_PANEL_HEIGHT_FLOOR,
    PlanPanel,
    plan_panel_max_height,
)
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id


def _items(*statuses: str) -> tuple[TodoItem, ...]:
    return tuple(
        TodoItem(content=f"step {i}", status=status)  # type: ignore[arg-type]
        for i, status in enumerate(statuses)
    )


LONG_PLAN = _items("in_progress", *(["pending"] * 9))  # 10 items -> 5 hidden by default


class PlanHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield PlanPanel(id="plan-panel")


# -- AC1: a focusable control that expands via Enter, Space, or click ------


@pytest.mark.asyncio
async def test_overflow_control_is_focusable() -> None:
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()
        assert panel.overflow_control.can_focus
        assert panel.overflow_label == "  \u22ee +5 more"


@pytest.mark.asyncio
async def test_enter_on_the_focused_control_expands_the_list() -> None:
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()
        panel.overflow_control.focus()
        await pilot.press("enter")
        assert panel.expanded is True
        assert len(panel.plan_lines) == 1 + 10 + 1  # header + every item + control


@pytest.mark.asyncio
async def test_space_on_the_focused_control_also_expands() -> None:
    """AC1: Space is a full alternate to Enter, not a lesser one."""
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()
        panel.overflow_control.focus()
        await pilot.press("space")
        assert panel.expanded is True


@pytest.mark.asyncio
async def test_click_on_the_control_expands_and_a_second_click_collapses() -> None:
    """AC1 mouse parity: click alone (no keyboard) drives the same toggle."""
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()
        await pilot.click("#plan-overflow")
        assert panel.expanded is True
        await pilot.click("#plan-overflow")
        assert panel.expanded is False


# -- AC2: the label is a clear, reversible collapse action -----------------


@pytest.mark.asyncio
async def test_label_flips_to_show_less_then_back_to_more() -> None:
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()
        assert panel.overflow_label == "  \u22ee +5 more"
        panel.overflow_control.focus()
        await pilot.press("enter")
        assert panel.overflow_label == "  \u25be Show less"
        await pilot.press("enter")
        assert panel.overflow_label == "  \u22ee +5 more"


# -- AC3: the hidden count / control stay accurate through changes ---------


@pytest.mark.asyncio
async def test_hidden_count_updates_live_while_expanded() -> None:
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()
        panel.expand()
        await pilot.pause()
        assert panel.overflow_label == "  \u25be Show less"
        assert len(panel.plan_lines) == 1 + 10 + 1

        # The plan grows mid-turn (the todo tool replaces the whole list) --
        # expanded stays expanded (view state independent of the model) and
        # every new row shows immediately, not just the ones present when
        # the control was first activated.
        panel.update_plan(_items("in_progress", *(["pending"] * 14)))  # now 15 items
        await pilot.pause()
        assert panel.expanded is True
        assert len(panel.plan_lines) == 1 + 15 + 1

        # The plan then shrinks below the overflow threshold entirely (a
        # completion wave, or \u2014 were it to exist \u2014 a filter): nothing left
        # to hide, so the control disappears rather than showing a stale
        # "+N more"/"Show less" for rows that no longer exist.
        panel.update_plan(_items("in_progress", "pending"))
        await pilot.pause()
        assert panel.overflow_label == ""
        assert panel.plan_lines == ("Plan 0/2", "  \u25b6 step 0", "  \u25cb step 1")


@pytest.mark.asyncio
async def test_hidden_count_updates_on_completion_change_while_expanded() -> None:
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        panel.expand()
        await pilot.pause()
        assert panel.plan_lines[0] == "Plan 0/10"

        completed_one = (
            TodoItem(content="step 0", status="completed"),
            TodoItem(content="step 1", status="in_progress"),
            *(TodoItem(content=f"step {i}", status="pending") for i in range(2, 10)),
        )
        panel.update_plan(completed_one)
        await pilot.pause()
        assert panel.plan_lines[0] == "Plan 1/10"
        assert panel.expanded is True  # still expanded; completion alone never collapses it


# -- Composition with ctrl+n (cycle_drill) ----------------------------------


@pytest.mark.asyncio
async def test_expand_overrides_the_drill_window_while_active() -> None:
    """Brief's chosen composition: expand-all is a separate view state that
    overrides the row window while active."""
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        items = _items("in_progress", *(["pending"] * 19))  # 20 items
        panel.update_plan(items)
        panel.show_panel()
        await pilot.pause()
        panel.cycle_drill()  # default -> +2 (still far short of 20)
        assert panel.max_rows == PLAN_MAX_ROWS + 2
        panel.expand()
        await pilot.pause()
        # every item shows, not just the drilled 7-row window.
        assert len(panel.plan_lines) == 1 + 20 + 1


@pytest.mark.asyncio
async def test_collapsing_lands_back_on_the_current_drill_window_not_reset() -> None:
    """ctrl+n's own drill level is untouched by expand/collapse \u2014 the two
    disclosure mechanisms are independent state (module docstring)."""
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        items = _items("in_progress", *(["pending"] * 19))
        panel.update_plan(items)
        panel.show_panel()
        await pilot.pause()
        panel.cycle_drill()  # -> +2 rows (7 total)
        assert panel.max_rows == PLAN_MAX_ROWS + 2
        panel.expand()
        await pilot.pause()
        panel.collapse()
        await pilot.pause()
        # Collapsing returns to the drilled window, not the ctrl+n default.
        assert panel.max_rows == PLAN_MAX_ROWS + 2
        assert len(panel.plan_lines) == 1 + (PLAN_MAX_ROWS + 2) + 1

        # ctrl+n itself keeps working normally after an expand/collapse
        # round-trip (IMPORTANT: do not remove/break the existing mechanism).
        panel.cycle_drill()  # -> +3
        assert panel.max_rows == PLAN_MAX_ROWS + 3


# -- AC4: preserve selection/scroll as closely as possible ------------------


@pytest.mark.asyncio
async def test_focus_falls_back_to_the_panel_when_the_control_vanishes() -> None:
    """If a plan update makes the overflow control disappear (nothing left
    to disclose) while it holds focus, focus lands on the panel itself
    rather than a hidden, unreachable widget."""
    app = PlanHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()
        panel.overflow_control.focus()
        await pilot.press("enter")  # expand; control now focused + "Show less"
        assert app.focused is panel.overflow_control

        panel.update_plan(_items("in_progress", "pending"))  # shrinks to 2: no overflow
        await pilot.pause()
        assert panel.overflow_control.display is False
        assert app.focused is panel  # not stranded on the now-hidden control


@pytest.mark.asyncio
async def test_scroll_position_preserved_when_the_plan_updates_while_expanded() -> None:
    app = PlanHost()
    async with app.run_test(size=(40, 16)) as pilot:
        panel = app.query_one(PlanPanel)
        panel.styles.max_height = 6  # force a bounded, scrollable region
        big = _items("in_progress", *(["pending"] * 29))  # 30 items
        panel.update_plan(big)
        panel.show_panel()
        panel.expand()
        await pilot.pause()
        assert panel.max_scroll_y > 0  # really is overflowing/scrollable
        panel.scroll_to(y=4, animate=False)
        await pilot.pause()
        scrolled_to = panel.scroll_offset.y
        assert scrolled_to > 0

        # A plan update that keeps roughly the same shape (still expanded,
        # still overflowing the bounded region) should not yank the view
        # back to the top.
        still_big = _items("in_progress", *(["pending"] * 29))
        panel.update_plan(still_big)
        await pilot.pause()
        assert panel.scroll_offset.y == scrolled_to


# -- AC5: bounded scroll container at short viewports ------------------------


@pytest.mark.asyncio
async def test_expanded_panel_is_bounded_and_scrolls_instead_of_growing() -> None:
    app = PlanHost()
    async with app.run_test(size=(40, 20)) as pilot:
        panel = app.query_one(PlanPanel)
        bound = plan_panel_max_height(pilot.app.size.height)
        panel.styles.max_height = bound
        huge = _items("in_progress", *(["pending"] * 49))  # 50 items
        panel.update_plan(huge)
        panel.show_panel()
        panel.expand()
        await pilot.pause()
        # However many items, the panel's own rendered height never exceeds
        # the computed bound -- it scrolls its own content instead.
        assert panel.size.height <= bound
        assert panel.max_scroll_y > 0


# -- S7 gap 2: the control itself survives narrow widths / short heights ----
#
# The panel's HEIGHT is bounded (AC5 above), but that says nothing about
# whether the CONTROL stays present, focusable, and click/keyboard-
# activatable at the repo's standard widths, or at a short terminal height
# -- i.e. whether it gets clipped/dropped exactly when the disclosure
# matters most. These pin that it does not.

STANDARD_WIDTHS = (40, 80, 97, 120)
"""The repo's standard width matrix (docs/DEVELOPMENT.md golden files)."""


@pytest.mark.asyncio
@pytest.mark.parametrize("width", STANDARD_WIDTHS)
async def test_overflow_control_stays_present_focusable_and_clickable_at_every_width(
    width: int,
) -> None:
    """Gap 2: at every standard width, the control is never clipped or
    dropped -- it stays displayed and focusable, and both click and
    keyboard (Enter) still toggle it."""
    app = PlanHost()
    async with app.run_test(size=(width, 24)) as pilot:
        panel = app.query_one(PlanPanel)
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()
        assert panel.overflow_control.display is True
        assert panel.overflow_control.can_focus is True

        await pilot.click("#plan-overflow")
        await pilot.pause()
        assert panel.expanded is True

        panel.overflow_control.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert panel.expanded is False


@pytest.mark.asyncio
async def test_overflow_control_stays_reachable_at_the_height_floor() -> None:
    """Gap 2: a short terminal forces the bounded/scrolling case (AC5) --
    the collapsed control (header + PLAN_MAX_ROWS rows + control) always
    fits within even :data:`PLAN_PANEL_HEIGHT_FLOOR`, so it is never
    itself scrolled out of reach before a keyboard-only user gets to press
    anything."""
    app = PlanHost()
    async with app.run_test(size=(40, PLAN_PANEL_HEIGHT_FLOOR + 2)) as pilot:
        panel = app.query_one(PlanPanel)
        bound = plan_panel_max_height(pilot.app.size.height)
        assert bound == PLAN_PANEL_HEIGHT_FLOOR  # exercising the floor itself
        panel.styles.max_height = bound
        panel.update_plan(LONG_PLAN)
        panel.show_panel()
        await pilot.pause()

        assert panel.overflow_control.display is True
        assert panel.overflow_control.can_focus is True
        assert panel.max_scroll_y == 0  # the collapsed view never needs to scroll

        # Both pre-existing paths still work at the floor height.
        await pilot.click("#plan-overflow")
        await pilot.pause()
        assert panel.expanded is True
        panel.overflow_control.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert panel.expanded is False


@pytest.mark.asyncio
async def test_overflow_control_remains_focusable_after_expansion_scrolls_it_off_view() -> None:
    """Gap 2, the harder case: once expanded at a short height, the (now
    much longer) list can scroll the "Show less" control below the
    visible viewport -- but Textual's default ``focus(scroll_visible=True)``
    still finds and can activate it programmatically (the path a
    keyboard-only user drives), so it never becomes a dead, unreachable
    widget just because it is not currently painted on screen."""
    app = PlanHost()
    async with app.run_test(size=(40, 10)) as pilot:
        panel = app.query_one(PlanPanel)
        bound = plan_panel_max_height(pilot.app.size.height)
        panel.styles.max_height = bound
        panel.update_plan(_items("in_progress", *(["pending"] * 39)))  # 40 items
        panel.show_panel()
        panel.expand()
        await pilot.pause()
        assert panel.max_scroll_y > 0  # really did overflow the bound

        assert panel.overflow_control.display is True
        panel.overflow_control.focus()
        await pilot.pause()
        assert app.focused is panel.overflow_control  # reached, not stranded
        await pilot.press("enter")
        await pilot.pause()
        assert panel.expanded is False  # still activatable, on screen or not
