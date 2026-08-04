"""Pure model tests for lane state (D5 AC1: lane-level attention state).

``LaneStateName``/``TERMINAL_LANE_STATES``/``LaneRegistry`` live in
``model/lanes.py`` with no Textual/kernel imports (layering: ``ui`` ->
``model`` -> ``kernel``) \u2014 these tests drive the registry directly, without
a reducer or widget in the loop.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.model.lanes import (
    TERMINAL_LANE_STATES,
    LaneRegistry,
    LaneState,
)


# -- state/glyph table (D5 AC1) -----------------------------------------------


def test_terminal_states_are_exactly_done_error_cancelled() -> None:
    assert TERMINAL_LANE_STATES == frozenset({"done", "error", "cancelled"})


@pytest.mark.parametrize(
    ("state", "glyph", "token"),
    [
        ("booting", "\u25d0", "teal"),
        ("running", "\u25d0", "teal"),
        ("working", "\u25a0", "fg"),
        ("attention", "!", "orange"),
        ("done", "\u2714", "dim"),
        ("error", "\u2716", "red"),
        ("cancelled", "\u2298", "red"),
    ],
)
def test_state_glyph_and_color_table(state: str, glyph: str, token: str) -> None:
    lane = LaneState.for_state(name="a", state=state)  # type: ignore[arg-type]
    assert (lane.glyph, lane.color_token) == (glyph, token)


def test_error_and_cancelled_glyphs_match_delegate_summary_glyphs() -> None:
    """Cross-surface consistency (D5 AC1): the lanes panel and the post-turn
    delegate-summary block must use the SAME glyph for the SAME outcome."""
    from amplifier_app_tui.ui.transcript_render import _DELEGATE_GLYPHS

    error_lane = LaneState.for_state(name="a", state="error")
    cancelled_lane = LaneState.for_state(name="a", state="cancelled")
    assert error_lane.glyph == _DELEGATE_GLYPHS["error"][0]
    assert cancelled_lane.glyph == _DELEGATE_GLYPHS["cancelled"][0]


# -- LaneRegistry.complete() (D5 AC1: settle to done/error/cancelled) --------


def test_complete_defaults_to_done_unchanged_behavior() -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.complete("a", result="tests \u2714")
    record = reg.get("a")
    assert record is not None
    assert record.lane.state == "done"
    assert record.lane.activity == "done \u00b7 tests \u2714"


def test_complete_with_error_state_reads_as_failed_not_done() -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.complete("a", result="migration blew up", state="error")
    record = reg.get("a")
    assert record is not None
    assert record.lane.state == "error"
    # Human-facing text says "failed", never the internal state word "error"
    # doubled with a redundant "done" (the bug the reviewer flagged).
    assert record.lane.activity == "failed \u00b7 migration blew up"


def test_complete_with_error_state_and_no_result_is_bare_failed() -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.complete("a", state="error")
    assert reg.get("a").lane.activity == "failed"


def test_complete_with_cancelled_state() -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.complete("a", state="cancelled")
    record = reg.get("a")
    assert record is not None
    assert record.lane.state == "cancelled"
    assert record.lane.activity == "cancelled"


# -- terminal-state treatment: active/advance/tail/reopen --------------------


@pytest.mark.parametrize("state", ["done", "error", "cancelled"])
def test_terminal_lanes_are_never_active(state: str) -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.complete("a", state=state)  # type: ignore[arg-type]
    assert reg.active == ()
    assert reg.active_count == 0


def test_attention_lane_still_counts_as_active() -> None:
    """Attention is NOT terminal \u2014 the agent is still working; only its
    presentation differs (D5 AC1)."""
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.update("a", state="attention", activity="recovering from bash error")
    assert reg.active_count == 1
    assert reg.get("a").lane.state == "attention"


@pytest.mark.parametrize("state", ["done", "error", "cancelled"])
def test_advance_freezes_every_terminal_state(state: str) -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=100.0)
    reg.complete("a", state=state)  # type: ignore[arg-type]
    reg.advance(200.0)
    assert reg.get("a").lane.elapsed == 0.0  # frozen, not just "done" specifically


@pytest.mark.parametrize("from_state", ["done", "error", "cancelled"])
def test_reopen_resets_from_any_terminal_state_not_just_done(from_state: str) -> None:
    """A replayed demo turn reusing a sub-session id must reset live
    regardless of WHICH terminal outcome the prior run ended in \u2014 an
    errored or cancelled lane must not get stuck showing a stale \u2716/\u2298
    forever once the same id spawns again."""
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.complete("a", state=from_state)  # type: ignore[arg-type]
    fresh = reg.register("a", parent_id=None, name="coder", state="running", reopen=True, now=50.0)
    assert fresh.lane.state == "running"
    assert reg.get("a").lane.state == "running"


@pytest.mark.parametrize("state", ["done", "error", "cancelled"])
def test_tail_lane_skips_every_terminal_state(state: str) -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.complete("a", state=state)  # type: ignore[arg-type]
    assert reg.tail_lane is None


def test_note_stream_activity_ignores_terminal_lanes() -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder", now=1.0)
    reg.register("b", parent_id=None, name="tester", now=1.0)
    reg.complete("a", state="error")
    reg.note_stream_activity("a")  # dropped: lane is terminal
    reg.note_stream_activity("b")
    reg.cycle_tail_focus()  # advances past whatever _tail_recent holds
    # "a" never became tail-eligible via streaming activity.
    assert reg.tail_lane is not None and reg.tail_lane.session_id == "b"
