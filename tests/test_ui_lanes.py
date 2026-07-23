"""Tests for ui/lanes_panel.py — agent lanes strip (DESIGN-SPEC §8)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.model.lanes import LaneRecord, LaneState, lane_labels
from amplifier_app_newtui.ui.lanes_panel import (
    LANE_MOTION_INTERVAL_SECONDS,
    LANES_HEADER,
    LanesPanel,
    format_lane_lines,
    lane_elapsed,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id


def _record(
    session_id: str,
    name: str,
    state: str,
    activity: str,
    elapsed: float,
    cost: str,
    tokens: int = 0,
) -> LaneRecord:
    return LaneRecord(
        session_id=session_id,
        parent_id="root",
        lane=LaneState.for_state(
            name=name,
            state=state,  # type: ignore[arg-type]
            activity=activity,
            elapsed=elapsed,
            tokens=tokens,
            cost=Decimal(cost),
        ),
    )


# The mockup's three demo lanes, verbatim.
RECORDS = (
    _record("s1", "researcher", "running", "scanning provider docs", 41, "0.09", 100100),
    _record("s2", "coder", "working", "migrating store", 124, "0.31", 48300),
    _record("s3", "tester", "done", "done · tests ✔", 55, "0.07", 3200),
)


class LanesHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.focused_lanes: list[tuple[str, str]] = []
        self.closed = 0

    def compose(self) -> ComposeResult:
        yield LanesPanel()

    def on_lanes_panel_focus_lane(self, message: LanesPanel.FocusLane) -> None:
        self.focused_lanes.append((message.name, message.session_id))

    def on_lanes_panel_closed(self, message: LanesPanel.Closed) -> None:
        self.closed += 1


# -- pure formatting -----------------------------------------------------


def test_header_exact_string() -> None:
    assert LANES_HEADER == "Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close"


def test_lane_elapsed_format() -> None:
    assert lane_elapsed(41) == "41s"
    assert lane_elapsed(55) == "55s"
    assert lane_elapsed(124) == "2m 04s"
    assert lane_elapsed(348) == "5m 48s"
    assert lane_elapsed(0) == "0s"


def test_lane_lines_align_exactly_like_mockup() -> None:
    lines = format_lane_lines(tuple(r.lane for r in RECORDS))
    assert lines == (
        "  ◐ researcher · scanning provider docs · 41s    · ↓ 100.1k tokens · $0.09",
        "  ■ coder      · migrating store        · 2m 04s · ↓ 48.3k tokens  · $0.31",
        "  ✔ tester     · done · tests ✔         · 55s    · ↓ 3.2k tokens   · $0.07",
    )


def test_lane_glyphs_and_colors_per_state() -> None:
    running, working, done = (r.lane for r in RECORDS)
    assert (running.glyph, running.color_token) == ("◐", "teal")
    assert (working.glyph, working.color_token) == ("■", "fg")
    assert (done.glyph, done.color_token) == ("✔", "dim")


def test_empty_lanes_format_to_nothing() -> None:
    assert format_lane_lines(()) == ()


# -- widget behavior ----------------------------------------------------


@pytest.mark.asyncio
async def test_panel_lists_aligned_lanes_and_selects_first() -> None:
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        assert panel.display
        assert panel.lane_lines == format_lane_lines(tuple(r.lane for r in RECORDS))
        assert panel.selected_record is RECORDS[0]
        from amplifier_app_newtui.ui.lanes_panel import _LaneRow  # test-only

        rows = list(panel.query(_LaneRow))
        assert [r.line for r in rows] == list(panel.lane_lines)
        assert rows[0].has_class("-selected")


@pytest.mark.asyncio
async def test_active_lane_labels_shimmer_and_stop_when_all_done() -> None:
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS[:1])
        panel.show_panel()
        await pilot.pause()
        assert panel._motion_timer is not None
        start = panel._motion_frame
        await pilot.pause(LANE_MOTION_INTERVAL_SECONDS + 0.08)
        assert panel._motion_frame > start

        from amplifier_app_newtui.ui.lanes_panel import _LaneRow  # test-only

        row = panel.query_one(_LaneRow)
        assert any(span.style.bold for span in row.render().spans)

        panel.update_lanes((RECORDS[2],))
        await pilot.pause()
        assert panel._motion_timer is None


@pytest.mark.asyncio
async def test_live_telemetry_patches_rows_without_remounting_motion() -> None:
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS[:1])
        panel.show_panel()
        await pilot.pause()

        from amplifier_app_newtui.ui.lanes_panel import _LaneRow  # test-only

        row = panel.query_one(_LaneRow)
        updated = _record(
            "s1", "researcher", "working", "reading README.md", 42, "0.10", 120000
        )
        panel.update_lanes((updated,))
        await pilot.pause()
        assert panel.query_one(_LaneRow) is row
        assert "reading README.md" in row.line


@pytest.mark.asyncio
async def test_arrows_move_selection_and_enter_focuses_lane() -> None:
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        await pilot.press("down")
        assert panel.selected_record is RECORDS[1]
        await pilot.press("down", "down", "down")  # clamped at the end
        assert panel.selected_record is RECORDS[2]
        await pilot.press("up", "up")
        assert panel.selected_record is RECORDS[0]
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.focused_lanes == [("coder", "s2")]


@pytest.mark.asyncio
async def test_click_focuses_that_lane() -> None:
    app = LanesHost()
    async with app.run_test(size=(100, 40)) as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        await pilot.click("#lane-row-2")
        await pilot.pause()
        assert app.focused_lanes == [("tester", "s3")]


@pytest.mark.asyncio
async def test_close_action_hides_and_posts_closed() -> None:
    # Esc is resolved by the app via keymap.ESC_CHAIN (spec §5) — the panel
    # has no local escape binding; the chain invokes ``action_close``.
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        panel.action_close()
        await pilot.pause()
        assert app.closed == 1
        assert not panel.display


@pytest.mark.asyncio
async def test_set_focused_snaps_highlight() -> None:
    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS)
        panel.show_panel()
        await pilot.pause()
        panel.set_focused("tester")
        await pilot.pause()
        assert panel.selected_record is RECORDS[2]


def test_format_lane_lines_marks_the_tailed_lane_and_keeps_alignment() -> None:
    lanes = (
        LaneState.for_state(name="researcher", state="running", activity="scanning docs"),
        LaneState.for_state(name="coder", state="working", activity="migrating store"),
    )
    lines = format_lane_lines(lanes, tailed_index=1)
    assert "coder ▸" in lines[1]
    assert "▸" not in lines[0]
    # The name column still pads to the widest entry (marker included):
    assert lines[0].index(" · ") == lines[1].index(" · ")
    # No marker → identical to today's output shape.
    assert "▸" not in "".join(format_lane_lines(lanes))


# -- width budget (review finding: rows clipped their telemetry) ---------------


def _wide_lanes() -> tuple[LaneState, ...]:
    return (
        LaneState.for_state(
            name="foundation:zen-architect",
            state="running",
            activity="Exploring the codebase for relevant files",
            elapsed=348,
            tokens=128_000,
            cost=Decimal("12.34"),
        ),
        LaneState.for_state(
            name="foundation:git-ops",
            state="running",
            activity="running",
            elapsed=19,
            tokens=0,
            cost=Decimal("0"),
        ),
    )


def test_format_lane_lines_elides_activity_to_fit_width() -> None:
    """The row is height-1: anything past the width is CROPPED, and the
    dropped part was the telemetry (elapsed/tokens/cost) — the panel's
    whole point. The activity column is the elastic one."""
    lines = format_lane_lines(_wide_lanes(), width=80)
    assert all(len(line) <= 80 for line in lines)
    assert "…" in lines[0]  # activity elided
    assert "5m 48s" in lines[0] and "↓ 128.0k tokens" in lines[0] and "$12.34" in lines[0]
    assert lines[0].index(" · ") == lines[1].index(" · ")  # alignment holds


def test_format_lane_lines_drops_tokens_before_the_essentials() -> None:
    lines = format_lane_lines(_wide_lanes(), width=58)
    assert all(len(line) <= 58 for line in lines)
    assert "tokens" not in lines[0]  # tokens column dropped whole
    assert "foundation:zen-architect" in lines[0]
    assert "5m 48s" in lines[0] and "$12.34" in lines[0]  # essentials kept


def test_format_lane_lines_without_width_is_unchanged() -> None:
    wide = format_lane_lines(_wide_lanes())
    assert "Exploring the codebase for relevant files" in wide[0]
    assert wide == format_lane_lines(_wide_lanes(), width=None)


# -- same-named-agent lane aliasing (runtime parity) --------------------------


def test_lane_labels_leave_unique_names_untouched() -> None:
    labels = lane_labels(RECORDS)
    assert labels == ("researcher", "coder", "tester")


def test_lane_labels_disambiguate_same_named_agents() -> None:
    """Two delegates of the same agent get a short session-id tag so their
    lane rows stop reading identically (the whole point of the panel)."""
    records = (
        _record("sub-aaaa", "test-writer", "running", "writing tests", 10, "0.05"),
        _record("sub-bbbb", "test-writer", "working", "writing tests", 20, "0.06"),
        _record("s3", "reviewer", "done", "done \u00b7 ok", 5, "0.01"),
    )
    assert lane_labels(records) == ("test-writer #aaaa", "test-writer #bbbb", "reviewer")


def test_lane_labels_tail_collision_falls_back_to_ordinal() -> None:
    """Two ids sharing the last four usable chars can't disambiguate by tag,
    so the group falls back to a stable 1-based ordinal (deterministic)."""
    records = (
        _record("x-9999", "worker", "running", "a", 1, "0.01"),
        _record("y-9999", "worker", "running", "b", 2, "0.01"),
    )
    assert lane_labels(records) == ("worker #9999", "worker #2")


def test_lane_labels_ignore_blank_names() -> None:
    records = (
        _record("s1", "", "running", "a", 1, "0.01"),
        _record("s2", "", "running", "b", 2, "0.01"),
    )
    assert lane_labels(records) == ("", "")


def test_format_lane_lines_disambiguates_same_named_lanes() -> None:
    """Golden: the aliased labels flow into the aligned rows and the ``\u00b7``
    separator columns still line up exactly."""
    records = (
        _record("sub-aaaa", "test-writer", "running", "writing tests", 10, "0.05", 1000),
        _record("sub-bbbb", "test-writer", "working", "writing tests", 20, "0.06", 2000),
        _record("s3", "reviewer", "done", "done \u00b7 ok", 5, "0.01", 300),
    )
    lines = format_lane_lines(
        tuple(r.lane for r in records), labels=lane_labels(records)
    )
    assert lines == (
        "  \u25d0 test-writer #aaaa \u00b7 writing tests \u00b7 10s \u00b7 \u2193 1.0k tokens \u00b7 $0.05",
        "  \u25a0 test-writer #bbbb \u00b7 writing tests \u00b7 20s \u00b7 \u2193 2.0k tokens \u00b7 $0.06",
        "  \u2714 reviewer          \u00b7 done \u00b7 ok     \u00b7 5s  \u00b7 \u2193 0.3k tokens \u00b7 $0.01",
    )
    # Alignment holds across the disambiguated (wider) name column.
    assert lines[0].index(" \u00b7 ") == lines[1].index(" \u00b7 ") == lines[2].index(" \u00b7 ")


@pytest.mark.asyncio
async def test_panel_disambiguates_same_named_lanes() -> None:
    records = (
        _record("sub-aaaa", "test-writer", "running", "writing tests", 10, "0.05", 1000),
        _record("sub-bbbb", "test-writer", "working", "writing tests", 20, "0.06", 2000),
    )
    app = LanesHost()
    async with app.run_test(size=(100, 40)) as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(records)
        panel.show_panel()
        await pilot.pause()
        joined = "\n".join(panel.lane_lines)
        assert "test-writer #aaaa" in joined
        assert "test-writer #bbbb" in joined
        # Focus routing still carries the raw agent name (session id disambiguates).
        await pilot.click("#lane-row-1")
        await pilot.pause()
        assert app.focused_lanes == [("test-writer", "sub-bbbb")]


@pytest.mark.asyncio
async def test_lane_tail_mounts_under_focused_row_then_drops(monkeypatch) -> None:
    """Issue #90: the focused lane's live tail renders directly under that
    lane's row (co-located with its agent), and drops on focus change / clear."""
    monkeypatch.setenv("TERM", "xterm-256color")
    from amplifier_app_newtui.ui.lanes_panel import _LaneRow, _LaneTail

    app = LanesHost()
    async with app.run_test() as pilot:
        panel = app.query_one(LanesPanel)
        panel.update_lanes(RECORDS, tailed_session_id="s2")  # coder focused
        panel.show_panel()
        await pilot.pause()

        panel.show_lane_tail("scanning the queue bridge\nfeeding the lanes\nnext: trackers")
        await pilot.pause()
        assert panel.has_lane_tail

        # The tail widget sits immediately after the focused (s2 = coder) row.
        kids = list(panel.children)
        tail = app.query_one(_LaneTail)
        coder_row = next(r for r in panel.query(_LaneRow) if r.record.session_id == "s2")
        assert kids.index(tail) == kids.index(coder_row) + 1

        # Cycling focus drops it (the reducer re-feeds for the newly focused lane).
        panel.update_lanes(RECORDS, tailed_session_id="s1")
        await pilot.pause()
        assert not panel.has_lane_tail

        # Explicit clear (turn end) drops it too.
        panel.show_lane_tail("x")
        await pilot.pause()
        assert panel.has_lane_tail
        panel.clear_lane_tail()
        await pilot.pause()
        assert not panel.has_lane_tail
