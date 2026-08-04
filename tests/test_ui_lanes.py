"""Tests for ui/lanes_panel.py — agent lanes strip (DESIGN-SPEC §8)."""

from __future__ import annotations

import unicodedata
from decimal import Decimal

import pytest
from textual.app import App, ComposeResult

from amplifier_app_tui.model.lanes import LaneRecord, LaneState, lane_labels
from amplifier_app_tui.ui.keymap import hint_label
from amplifier_app_tui.ui.lanes_panel import (
    EXPAND_HINT_TEXT,
    LANE_MOTION_INTERVAL_SECONDS,
    LANES_HEADER,
    LanesPanel,
    _elide,
    format_lane_lines,
    lane_elapsed,
)
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id


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
    # Booting reuses the running glyph (the §8 glyph set is closed).
    booting = _record("s4", "a", "booting", "booting", 5, "0").lane
    assert (booting.glyph, booting.color_token) == ("◐", "teal")


def test_lane_booting_row_ends_at_elapsed_clock() -> None:
    """A spawned-but-silent child renders ``booting · Ns`` with no zeroed
    tokens/cost cells (which read as a hung agent), while sibling rows
    keep the full telemetry format. Mirrored by the Rust suite
    (ui/lanes_panel.rs test of the same name)."""
    records = (
        _record("s1", "researcher", "booting", "booting", 5, "0"),
        _record("s2", "coder", "working", "migrating store", 124, "0.31", 48300),
    )
    lines = format_lane_lines(tuple(r.lane for r in records))
    assert lines == (
        "  ◐ researcher · booting         · 5s",
        "  ■ coder      · migrating store · 2m 04s · ↓ 48.3k tokens · $0.31",
    )
    # A queued-steer badge still lands after the booting clock.
    lines = format_lane_lines((records[0].lane,), queued_counts=(2,))
    assert lines == ("  ◐ researcher · booting · 5s · ▸ 2 queued",)


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
        from amplifier_app_tui.ui.lanes_panel import _LaneRow  # test-only

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

        from amplifier_app_tui.ui.lanes_panel import _LaneRow  # test-only

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

        from amplifier_app_tui.ui.lanes_panel import _LaneRow  # test-only

        row = panel.query_one(_LaneRow)
        updated = _record("s1", "researcher", "working", "reading README.md", 42, "0.10", 120000)
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
    lines = format_lane_lines(tuple(r.lane for r in records), labels=lane_labels(records))
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
    from amplifier_app_tui.ui.lanes_panel import _LaneRow, _LaneTail

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


# -- D5 AC3: boundary-safe truncation + explicit expand affordance -----------


_LONG_ACTIVITY = "recovering from bash tool invocation error while retrying"


@pytest.mark.parametrize("budget", list(range(6, 40)))
def test_elide_never_clips_mid_word(budget: int) -> None:
    """Across every plausible column budget, a truncated preview cuts on a
    word boundary \u2014 never mid-word \u2014 and always ends with one ellipsis."""
    result = _elide(_LONG_ACTIVITY, budget)
    if result == _LONG_ACTIVITY:
        return  # fit whole; nothing to check
    assert result.endswith("\u2026")
    prefix = result[:-1]  # strip the ellipsis
    assert len(result) <= budget
    if not prefix:
        return  # budget so tight only the ellipsis itself survives
    # The kept prefix must be a clean word-boundary slice of the original:
    # either the whole original up to some space, or (single unbreakable
    # token fallback) still a prefix of the original text.
    assert _LONG_ACTIVITY.startswith(prefix)
    next_char_index = len(prefix)
    at_boundary = next_char_index >= len(_LONG_ACTIVITY) or _LONG_ACTIVITY[next_char_index] == " "
    # Word-boundary cuts land exactly at a space; the single-long-token
    # fallback (tested separately below) is the only exception.
    assert at_boundary or " " not in _LONG_ACTIVITY[:next_char_index]


def test_elide_fits_within_budget_returns_unchanged() -> None:
    assert _elide("thinking", 20) == "thinking"
    assert _elide("thinking", len("thinking")) == "thinking"


def test_elide_prefers_word_boundary_over_raw_character_slice() -> None:
    """The regression case: the old raw ``text[:n]`` slice cut through
    ``bash`` (``recovering from ba\u2026``); the fix backs up to the space."""
    result = _elide("recovering from bash error", 20)
    assert result == "recovering from\u2026"
    assert "ba\u2026" not in result
    assert not any(word.endswith("\u2026") and word != "\u2026" for word in result.split(" ")[:-1])


def test_elide_single_unbreakable_token_falls_back_to_hard_cut() -> None:
    """No whitespace anywhere \u2014 there is no boundary to break on, so the
    grapheme-safe hard cut is the documented, deliberate exception."""
    token = "a" * 40
    result = _elide(token, 10)
    assert result == "a" * 9 + "\u2026"
    assert len(result) == 10


@pytest.mark.parametrize("budget", list(range(4, 20)))
def test_elide_grapheme_safe_hard_cut_never_splits_combining_mark(budget: int) -> None:
    """An unbreakable token (no whitespace to break on) whose naive
    code-point cut would land between a base letter and its combining
    accent instead backs up off the bare base — the accent is never
    separated from the character it belongs to."""
    # "e" + COMBINING ACUTE ACCENT, repeated — one token, no spaces, so
    # every OTHER code-point boundary is a would-be split point.
    token = "e\u0301" * 20
    result = _elide(token, budget)
    if result == token:
        return  # fits whole at this budget; nothing to check
    assert result.endswith("\u2026")
    body = result[:-1]  # strip the ellipsis
    if len(body) < len(token):
        next_char = token[len(body)]
        # The character immediately after the cut must not itself be a
        # combining mark — otherwise the last kept character is a bare
        # base that just lost its accent (a split grapheme cluster).
        assert not unicodedata.combining(next_char)


# A short first word + one long unbreakable blob: the word-boundary cut
# lands right after "processing" regardless of width, leaving plenty of
# slack for the trailing hint — unlike a natural multi-word sentence
# (tested above), where each extra word consumes the slack as it grows.
_BLOB_ACTIVITY = "processing " + ("x" * 80)


def _lane(name: str, activity: str, **overrides: object) -> LaneState:
    fields: dict[str, object] = {"elapsed": 41, "tokens": 100, "cost": Decimal("0.09")}
    fields.update(overrides)
    return LaneState.for_state(name=name, state="running", activity=activity, **fields)


def test_format_lane_lines_expand_hint_appears_when_truncated_and_fits() -> None:
    lines = format_lane_lines((_lane("researcher", _BLOB_ACTIVITY),), width=80)
    assert "…" in lines[0]
    assert EXPAND_HINT_TEXT in lines[0]
    assert EXPAND_HINT_TEXT == f"{hint_label('focus_lane')} to expand"
    assert len(lines[0]) <= 80


def test_format_lane_lines_expand_hint_absent_when_activity_fits() -> None:
    lines = format_lane_lines(_wide_lanes(), width=200)
    assert EXPAND_HINT_TEXT not in lines[0]
    assert EXPAND_HINT_TEXT not in lines[1]


def test_format_lane_lines_expand_hint_never_pushes_past_width() -> None:
    """Whether or not the hint fits, the row is never silently widened
    past its budget — the header's ``enter focus`` hint is the
    width-independent fallback (D5 AC3 design note). Exercised at the
    golden width matrix (40/80/97/120, docs/DEVELOPMENT.md) plus a couple
    of in-between values, over both a natural multi-word activity (where
    the hint frequently has no room) and the slack-leaving blob activity
    (where it usually does).
    """
    for activity in (_LONG_ACTIVITY, _BLOB_ACTIVITY):
        lane = _lane("researcher", activity)
        for width in (40, 45, 50, 58, 70, 80, 97, 120):
            lines = format_lane_lines((lane,), width=width)
            assert len(lines[0]) <= width
            assert "…" in lines[0] or activity in lines[0]


def test_format_lane_lines_expand_hint_sits_before_queued_badge() -> None:
    lines = format_lane_lines((_lane("researcher", _BLOB_ACTIVITY),), width=80, queued_counts=[2])
    assert EXPAND_HINT_TEXT in lines[0]
    assert "▸ 2 queued" in lines[0]
    assert lines[0].index(EXPAND_HINT_TEXT) < lines[0].index("▸ 2 queued")
