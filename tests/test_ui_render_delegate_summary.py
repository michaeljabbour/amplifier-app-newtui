"""DelegateSummaryBlock renderer: pure (block, width) → lines (ambient-progress D5)."""

from __future__ import annotations

from amplifier_app_newtui.model.blocks import (
    DelegateEntry,
    DelegateSummaryBlock,
    TodoItem,
)
from amplifier_app_newtui.ui.segments import lines_plain
from amplifier_app_newtui.ui.transcript import render_block


def _plain(block: DelegateSummaryBlock, width: int = 97) -> list[str]:
    return [lines_plain([line]) for line in render_block(block, width)]


DONE_ENTRIES = (
    DelegateEntry(agent="researcher", state="done", elapsed_s=4.4, snippet="3 findings"),
    DelegateEntry(agent="coder", state="done", elapsed_s=6.0, snippet="2 files"),
    DelegateEntry(agent="tester", state="done", elapsed_s=2.6, snippet="tests ✔"),
)

PLAN = (
    TodoItem(content="scan provider docs", status="completed"),
    TodoItem(content="migrate session store", status="completed"),
    TodoItem(content="run store tests", status="completed"),
    TodoItem(content="synthesize findings", status="completed"),
)


def test_running_header_is_single_line_no_chevron() -> None:
    block = DelegateSummaryBlock(
        id="b1",
        entries=(
            DelegateEntry(agent="researcher"),
            DelegateEntry(agent="coder"),
            DelegateEntry(agent="tester"),
        ),
    )
    lines = _plain(block)
    assert lines == ["● 3 delegates running…"]


def test_single_running_delegate_is_singular() -> None:
    block = DelegateSummaryBlock(id="b1", entries=(DelegateEntry(agent="coder"),))
    assert _plain(block) == ["● 1 delegate running…"]


def test_collapsed_final_header_exact() -> None:
    block = DelegateSummaryBlock(id="b1", entries=DONE_ENTRIES, plan_final=PLAN, duration_s=102.0)
    assert _plain(block) == ["● Used 3 delegates · Plan 4/4 · 1m 42s ▸"]


def test_collapsed_header_omits_plan_when_none() -> None:
    block = DelegateSummaryBlock(id="b1", entries=DONE_ENTRIES, duration_s=42.0)
    assert _plain(block) == ["● Used 3 delegates · 42s ▸"]


def test_expanded_rows_and_plan_line() -> None:
    block = DelegateSummaryBlock(
        id="b1", entries=DONE_ENTRIES, plan_final=PLAN, duration_s=102.0, expanded=True
    )
    lines = _plain(block)
    assert lines[0] == "● Used 3 delegates · Plan 4/4 · 1m 42s ▾"
    assert lines[1].startswith("    ├─ ✔ researcher")
    assert '4s · "3 findings"' in lines[1]
    assert lines[3].startswith("    └─ ✔ tester")  # last row gets the corner glyph
    assert lines[4].startswith("    Plan  ")
    assert "✔ scan provider docs" in lines[4]
    assert len(lines) == 5


def test_error_and_cancelled_glyphs() -> None:
    block = DelegateSummaryBlock(
        id="b1",
        entries=(
            DelegateEntry(agent="coder", state="error", elapsed_s=3.0, snippet="failed"),
            DelegateEntry(agent="tester", state="cancelled", elapsed_s=1.0),
        ),
        duration_s=3.0,
        expanded=True,
    )
    lines = _plain(block)
    assert "✖ coder" in lines[1]
    assert "⊘ tester" in lines[2]


def test_expanded_running_row_shows_running() -> None:
    block = DelegateSummaryBlock(
        id="b1",
        entries=(DelegateEntry(agent="coder"),),
        expanded=True,
    )
    lines = _plain(block)
    assert lines[0] == "● 1 delegate running…"
    assert lines[1] == "    └─ ◐ coder  running"


def test_snippet_truncated_to_width() -> None:
    long = DelegateEntry(agent="a", state="done", elapsed_s=1.0, snippet="x" * 200)
    block = DelegateSummaryBlock(id="b1", entries=(long,), duration_s=1.0, expanded=True)
    row = _plain(block, width=40)[1]
    assert len(row) <= 40
    assert row.endswith('…"')
