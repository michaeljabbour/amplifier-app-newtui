"""Tests for the ambient plan panel (ui/plan_panel.py) — Phase 1 of
docs/plans/2026-07-21-ambient-progress-design.md (D1/D2)."""

from __future__ import annotations

from amplifier_app_newtui.model.blocks import TodoItem
from amplifier_app_newtui.ui.plan_panel import PLAN_MAX_ROWS, format_plan_lines
from amplifier_app_newtui.ui.segments import line_plain


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
    from amplifier_app_newtui.ui.plan_panel import plan_panel_width

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
    from amplifier_app_newtui.ui.plan_panel import PLAN_PANEL_WIDTH, plan_panel_width

    short_items = (
        TodoItem(content="scan provider docs", status="completed"),
        TodoItem(content="run store tests", status="pending"),
    )
    # Demo-length content at the snapshot width: unchanged 37 (goldens hold).
    assert plan_panel_width(short_items, 120) == PLAN_PANEL_WIDTH
    assert plan_panel_width((), 198) == PLAN_PANEL_WIDTH
