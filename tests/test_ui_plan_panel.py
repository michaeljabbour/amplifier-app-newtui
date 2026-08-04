"""Tests for the ambient plan panel (ui/plan_panel.py) — Phase 1 of
docs/plans/2026-07-21-ambient-progress-design.md (D1/D2)."""

from __future__ import annotations

from amplifier_app_tui.model.blocks import TodoItem
from amplifier_app_tui.ui.plan_panel import PLAN_MAX_ROWS, format_plan_lines
from amplifier_app_tui.ui.segments import line_plain


def _items(*statuses: str) -> tuple[TodoItem, ...]:
    return tuple(
        TodoItem(content=f"step {i}", status=status)  # type: ignore[arg-type]
        for i, status in enumerate(statuses)
    )


def plains(items: tuple[TodoItem, ...]) -> tuple[str, ...]:
    return tuple(line_plain(line) for line in format_plan_lines(items))


def test_no_items_renders_nothing() -> None:
    assert format_plan_lines(()) == ()


def test_header_counts_and_glyph_rows() -> None:
    items = _items("completed", "in_progress", "pending", "pending")
    assert plains(items) == (
        "Plan 1/4",
        "  ✔ step 0",
        "  ▶ step 1",
        "  ○ step 2",
        "  ○ step 3",
    )


def test_all_complete_collapses_to_header_only() -> None:
    items = _items("completed", "completed", "completed")
    assert plains(items) == ("Plan 3/3",)


def test_overflow_windows_around_active_item_with_more_marker() -> None:
    # 8 items, active at index 4 → window starts one above the active row.
    items = _items(
        "completed",
        "completed",
        "pending",
        "pending",
        "in_progress",
        "pending",
        "pending",
        "pending",
    )
    assert PLAN_MAX_ROWS == 5
    assert plains(items) == (
        "Plan 2/8",
        "  ○ step 3",
        "  ▶ step 4",
        "  ○ step 5",
        "  ○ step 6",
        "  ○ step 7",
        "  ⋮ +3 more",
    )


def test_overflow_with_no_active_item_shows_first_rows() -> None:
    items = _items("pending", "pending", "pending", "pending", "pending", "pending")
    lines = plains(items)
    assert lines[0] == "Plan 0/6"
    assert lines[1] == "  ○ step 0"
    assert lines[-1] == "  ⋮ +1 more"
    assert len(lines) == 1 + PLAN_MAX_ROWS + 1  # header + rows + marker


# -- responsive width (found live: 198-col real fan-out, wrapping plan items) --


def test_plan_panel_width_grows_to_fit_long_items_capped_at_a_third() -> None:
    """At 198 cols the fixed 37-col panel wrapped real plan items while the
    lanes half sat mostly empty — the panel should fit its content, capped
    at a third of the strip so the lanes stay dominant."""
    from amplifier_app_tui.ui.plan_panel import plan_panel_width

    long_items = (
        TodoItem(content="Fan out parallel agents to survey repo state", status="in_progress"),
        TodoItem(content="Synthesize findings into recommended next steps", status="pending"),
    )
    width = plan_panel_width(long_items, 198)
    # widest row (4-char glyph prefix + content) + 4 cells panel padding
    assert width == 4 + len(long_items[1].content) + 4
    assert width <= 198 // 3
    # Very long content still respects the one-third cap.
    huge = (TodoItem(content="x" * 200, status="pending"),)
    assert plan_panel_width(huge, 198) == 198 // 3


def test_plan_panel_width_never_shrinks_below_the_mockup_37() -> None:
    from amplifier_app_tui.ui.plan_panel import PLAN_PANEL_WIDTH, plan_panel_width

    short_items = (
        TodoItem(content="scan provider docs", status="completed"),
        TodoItem(content="run store tests", status="pending"),
    )
    # Demo-length content at the snapshot width: unchanged 37 (goldens hold).
    assert plan_panel_width(short_items, 120) == PLAN_PANEL_WIDTH
    assert plan_panel_width((), 198) == PLAN_PANEL_WIDTH


# -- S7: expand/collapse the hidden rows (compliance 2026-08-02) ------------
#
# The "+N more" line used to be the end of the story (a plain Static
# segment). These pin the pure ``expanded`` branch of
# ``format_plan_body_and_control`` / ``format_plan_lines``; widget-level
# focus/click/keyboard behavior is covered in test_ui_plan_panel_expand.py.


def test_expanded_shows_every_item_and_a_show_less_control() -> None:
    from amplifier_app_tui.ui.plan_panel import format_plan_body_and_control

    items = _items("in_progress", *(["pending"] * 9))  # 10 items, well past PLAN_MAX_ROWS (5)
    body, control = format_plan_body_and_control(items, expanded=True)
    # header + all 10 rows, nothing dropped.
    assert len(body) == 1 + 10
    assert control is not None
    from amplifier_app_tui.ui.segments import line_plain

    assert line_plain(control) == "  \u25be Show less"
    expanded_lines = tuple(line_plain(line) for line in format_plan_lines(items, expanded=True))
    assert expanded_lines[0] == "Plan 0/10"
    assert expanded_lines[1] == "  \u25b6 step 0"  # the in-progress item, unwindowed
    assert expanded_lines[2:-1] == tuple(f"  \u25cb step {i}" for i in range(1, 10))
    assert expanded_lines[-1] == "  \u25be Show less"
    assert len(expanded_lines) == 1 + 10 + 1


def test_expanded_with_nothing_hidden_has_no_control() -> None:
    """Fewer items than the row window: expanded and collapsed render
    identically, and neither carries a control (nothing to disclose)."""
    from amplifier_app_tui.ui.plan_panel import format_plan_body_and_control

    items = _items("in_progress", "pending", "pending")  # 3 items, PLAN_MAX_ROWS=5
    collapsed_body, collapsed_control = format_plan_body_and_control(items, expanded=False)
    expanded_body, expanded_control = format_plan_body_and_control(items, expanded=True)
    assert collapsed_control is None
    assert expanded_control is None
    assert collapsed_body == expanded_body


def test_expanded_overrides_the_drill_row_window() -> None:
    """S7 composition rule: while expanded, ALL items show regardless of
    the ctrl+n drill window \u2014 expand-all overrides the row window rather
    than replacing it (the window is still what a later collapse lands on)."""
    from amplifier_app_tui.ui.plan_panel import PLAN_MAX_ROWS, format_plan_body_and_control

    items = _items("in_progress", *(["pending"] * 19))  # 20 items
    # Even at a WIDENED (but still short of total) drilled window, expanded
    # shows every item, not just the drilled window.
    body, control = format_plan_body_and_control(items, max_rows=PLAN_MAX_ROWS + 3, expanded=True)
    assert len(body) == 1 + 20
    assert control is not None  # 20 > (5+3): still something to collapse back to


def test_hidden_count_recomputes_fresh_after_the_list_shrinks() -> None:
    """AC3: the hidden count is never a stale, cached number \u2014 both branches
    are pure functions of *whatever items are passed*, so a plan update, a
    completion change, or (were it to exist) filtering the list all recompute
    it fresh. Simulated here as separate calls with a shrinking item set,
    matching how ``PlanPanel.update_plan`` replaces ``_items`` wholesale."""
    from amplifier_app_tui.ui.plan_panel import format_plan_body_and_control

    big = _items("in_progress", *(["pending"] * 9))  # 10 items \u2192 5 hidden
    _, control_big = format_plan_body_and_control(big, expanded=True)
    assert control_big is not None

    # The list is replaced (update/filter) with far fewer items that now fit
    # inside the default window entirely \u2014 nothing left to hide.
    small = _items("in_progress", "pending")
    body_small, control_small = format_plan_body_and_control(small, expanded=True)
    assert control_small is None
    assert len(body_small) == 1 + 2

    # And back up again (e.g. a filter cleared): the count tracks the
    # CURRENT list, not anything left over from the "big" call above.
    medium = _items("in_progress", *(["pending"] * 6))  # 7 items \u2192 2 hidden
    _, control_medium = format_plan_body_and_control(medium, expanded=False)
    assert control_medium is not None
    from amplifier_app_tui.ui.segments import line_plain

    assert line_plain(control_medium) == "  \u22ee +2 more"


def test_hidden_count_recomputes_after_a_completion_change() -> None:
    """AC3: marking the active item done (shifting which item is "active"
    and shrinking the effective backlog) is picked up on the very next call
    \u2014 no separate invalidation step exists to forget."""
    from amplifier_app_tui.ui.plan_panel import format_plan_body_and_control

    before = _items("in_progress", *(["pending"] * 9))
    _, control_before = format_plan_body_and_control(before, expanded=False)
    from amplifier_app_tui.ui.segments import line_plain

    assert line_plain(control_before) == "  \u22ee +5 more"

    # step 0 completes, step 1 becomes active \u2014 same 10 items, new shape.
    after = (
        TodoItem(content="step 0", status="completed"),
        TodoItem(content="step 1", status="in_progress"),
        *(TodoItem(content=f"step {i}", status="pending") for i in range(2, 10)),
    )
    _, control_after = format_plan_body_and_control(after, expanded=False)
    assert line_plain(control_after) == "  \u22ee +5 more"  # still 10 items, still 5 hidden
    body_after, _ = format_plan_body_and_control(after, expanded=False)
    assert line_plain(body_after[0]) == "Plan 1/10"


def test_plan_panel_max_height_floors_and_halves_the_screen() -> None:
    from amplifier_app_tui.ui.plan_panel import PLAN_PANEL_HEIGHT_FLOOR, plan_panel_max_height

    assert plan_panel_max_height(50) == 25
    assert plan_panel_max_height(24) == 12
    # A very short terminal still gets the floor, not a near-zero cap.
    assert plan_panel_max_height(10) == PLAN_PANEL_HEIGHT_FLOOR == 8
    assert plan_panel_max_height(0) == PLAN_PANEL_HEIGHT_FLOOR


# -- S7 gap 1: the ctrl+h reach/toggle chord's own notice -------------------


def test_plan_overflow_notice_names_expanded_and_collapsed() -> None:
    """Mirrors plan_drill_notice's shape for the sibling ctrl+n chord, so
    both plan-panel keyboard actions confirm themselves the same way."""
    from amplifier_app_tui.ui.plan_panel import plan_overflow_notice

    assert plan_overflow_notice(True) == "plan · expanded"
    assert plan_overflow_notice(False) == "plan · collapsed"
