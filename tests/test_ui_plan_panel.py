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
