"""Tests for turn telemetry/ledger, steering/needs-you queues, and lanes."""

from __future__ import annotations

from decimal import Decimal

import pytest

from amplifier_app_newtui.model.lanes import LaneRegistry, LaneState
from amplifier_app_newtui.model.queues import (
    MAX_QUEUE_ITEMS,
    NeedsYouQueue,
    SteeringQueue,
)
from amplifier_app_newtui.model.turn import (
    OutcomeLedger,
    TurnOutcome,
    TurnTelemetry,
)

# --- telemetry formatting (DESIGN-SPEC §3) ----------------------------------


def test_telemetry_label_marks_unpriced_cost_with_tilde() -> None:
    """estimated=True → the $ figure is a floor (some usage was unpriceable)."""
    telemetry = TurnTelemetry(
        secs=24, tokens_down=3200, cached_pct=80, cost=Decimal("0.12"), estimated=True
    )
    assert telemetry.label() == "24s · 3.2k tok, 80% cached · ~$0.12"


def test_telemetry_suffix_and_label() -> None:
    telemetry = TurnTelemetry(secs=24, tokens_down=3200, cached_pct=80, cost=Decimal("0.12"))
    assert telemetry.suffix() == "(24s · ↓ 3.2k tok)"
    assert telemetry.label() == "24s · 3.2k tok, 80% cached · $0.12"


def test_telemetry_elapsed_stays_raw_seconds_past_a_minute() -> None:
    # Mockup renders `secs + "s"` everywhere — no m/h rollover (75s, not 1m 15s).
    telemetry = TurnTelemetry(secs=75, tokens_down=3200)
    assert telemetry.suffix() == "(75s · ↓ 3.2k tok)"
    long_turn = TurnTelemetry(secs=3725, tokens_down=3200, cost=Decimal("0.50"))
    assert long_turn.label() == "3725s · 3.2k tok · $0.50"


def test_telemetry_elapsed_is_integer_seconds_not_a_float() -> None:
    # Issue #34: the mockup shows `8s`, never `8.0s` — fractional wall-clock
    # seconds always render as a truncated integer in every telemetry surface.
    telemetry = TurnTelemetry(secs=8.7, tokens_down=3200, cached_pct=91, cost=Decimal("0.17"))
    assert telemetry.suffix() == "(8s · ↓ 3.2k tok)"
    assert telemetry.label() == "8s · 3.2k tok, 91% cached · $0.17"


def test_outcome_labels_match_spec_examples() -> None:
    assert TurnOutcome(kind="answer").outcome_label() == "answer"
    assert TurnOutcome(kind="interrupted").outcome_label() == "· interrupted"
    assert TurnOutcome(kind="plan_ready").outcome_label() == "· plan ready"
    shipped = TurnOutcome(kind="shipped", files_changed=3, diffstat="+142/−38", tests_ok=True)
    assert shipped.outcome_label() == "3 files · +142/−38 · tests ✔"
    assert shipped.shipped


# --- ledger + checkpoints (DESIGN-SPEC §9/§10) -------------------------------


def _telemetry(cost: str = "0.10") -> TurnTelemetry:
    return TurnTelemetry(secs=10, tokens_down=1000, cached_pct=50, cost=Decimal(cost))


def test_ledger_records_turns_and_aggregates() -> None:
    ledger = OutcomeLedger()
    ledger.record_turn(_telemetry("0.10"), TurnOutcome(kind="answer"), turn_id=1, message_index=2)
    ledger.record_turn(
        _telemetry("0.30"),
        TurnOutcome(kind="shipped", files_changed=1),
        turn_id=2,
        message_index=6,
        label="fix retry",
    )
    assert ledger.turn_count == 2
    assert ledger.spend == Decimal("0.40")
    assert ledger.shipped_count == 1
    assert ledger.answer_only_count == 1
    assert ledger.last_shipped is True
    assert [c.id for c in ledger.checkpoints] == ["t1", "t2"]
    assert ledger.checkpoints[1].cost_at == Decimal("0.40")
    assert ledger.checkpoint_by_id("t2").label == "fix retry"  # type: ignore[union-attr]


def test_ledger_trim_to_checkpoint_confirm_then_trim() -> None:
    ledger = OutcomeLedger()
    for turn_id in (1, 2, 3):
        ledger.record_turn(
            _telemetry(), TurnOutcome(kind="answer"), turn_id=turn_id, message_index=turn_id * 2
        )
    ledger.trim_to("t1")
    assert [c.id for c in ledger.checkpoints] == ["t1"]
    assert ledger.next_checkpoint_id() == "t2"
    with pytest.raises(KeyError):
        ledger.trim_to("t9")


def test_ledger_cache_hit_is_token_weighted() -> None:
    ledger = OutcomeLedger()
    ledger.record_turn(
        TurnTelemetry(secs=1, tokens_down=1000, cached_pct=100, cost=Decimal("0")),
        TurnOutcome(kind="answer"),
        turn_id=1,
        message_index=1,
    )
    ledger.record_turn(
        TurnTelemetry(secs=1, tokens_down=3000, cached_pct=0, cost=Decimal("0")),
        TurnOutcome(kind="answer"),
        turn_id=2,
        message_index=2,
    )
    assert ledger.cache_hit_pct == 25


# --- steering queue (bounded 32/32KB) ----------------------------------------


def test_steering_queue_steer_vs_next_turn() -> None:
    queue = SteeringQueue()
    queue.enqueue("focus on tests", kind="steer")
    queue.enqueue("then update docs", kind="next_turn")
    assert len(queue.pending_steers) == 1
    assert len(queue.pending_next_turn) == 1
    steer = queue.consume_next_steer()
    assert steer is not None and steer.text == "focus on tests"
    assert queue.consume_next_steer() is None
    follow_up = queue.consume_next_turn_message()
    assert follow_up is not None and follow_up.text == "then update docs"


def test_next_turn_slot_replaces_on_second_enqueue() -> None:
    # Mockup single slot (``this.queued = text``): a second next-turn
    # message replaces the first — the footer badge is only ever q1.
    queue = SteeringQueue()
    queue.enqueue("first follow-up", kind="next_turn")
    queue.enqueue("second follow-up", kind="next_turn")
    assert len(queue.pending_next_turn) == 1
    assert queue.pending_next_turn[0].text == "second follow-up"
    picked = queue.consume_next_turn_message()
    assert picked is not None and picked.text == "second follow-up"
    assert queue.consume_next_turn_message() is None


def test_steering_queue_bounds() -> None:
    queue = SteeringQueue()
    for i in range(MAX_QUEUE_ITEMS):
        queue.enqueue(f"steer {i}")
    with pytest.raises(ValueError, match="limit"):
        queue.enqueue("one too many")
    assert len(queue.pending) == MAX_QUEUE_ITEMS  # queue left intact


def test_steering_queue_truncates_oversized_text() -> None:
    queue = SteeringQueue()
    message = queue.enqueue("x" * 40_000)
    assert len(message.text) == 32_768


def test_drain_steers_removes_leftovers_for_discard() -> None:
    queue = SteeringQueue()
    queue.enqueue("a", kind="steer")
    queue.enqueue("b", kind="next_turn")
    leftover = queue.drain_steers()
    assert [m.text for m in leftover] == ["a"]
    assert [m.text for m in queue.pending] == ["b"]


def test_steering_queue_rejects_empty() -> None:
    with pytest.raises(ValueError):
        SteeringQueue().enqueue("   ")


# --- needs-you queue (DESIGN-SPEC §7) ----------------------------------------


def test_needs_you_lifecycle() -> None:
    queue = NeedsYouQueue()
    item = queue.defer("push to fork?", "no push permission", choices=("yes · push to fork",))
    assert queue.pending_count == 1
    answered = queue.answer(item.decision_id, "yes")
    assert answered.status == "answered"
    assert queue.pending_count == 0
    consumed = queue.consume_answered()
    assert [c.decision_id for c in consumed] == [item.decision_id]
    assert consumed[0].status == "consumed"


def test_needs_you_cannot_answer_twice() -> None:
    queue = NeedsYouQueue()
    item = queue.defer("q?", "r")
    queue.answer(item.decision_id, "yes")
    with pytest.raises(ValueError):
        queue.answer(item.decision_id, "no")


def test_needs_you_listener_fires() -> None:
    queue = NeedsYouQueue()
    calls: list[int] = []
    remove = queue.add_listener(lambda: calls.append(1))
    queue.defer("q?", "r")
    assert calls
    remove()
    queue.defer("q2?", "r")
    assert len(calls) == 1


# --- lanes (DESIGN-SPEC §8) ---------------------------------------------------


def test_lane_state_glyphs_per_spec() -> None:
    running = LaneState.for_state(name="a", state="running")
    working = LaneState.for_state(name="a", state="working")
    done = LaneState.for_state(name="a", state="done")
    assert (running.glyph, running.color_token) == ("◐", "teal")
    assert (working.glyph, working.color_token) == ("■", "fg")
    assert (done.glyph, done.color_token) == ("✔", "dim")


def test_lane_registry_routing_and_completion() -> None:
    registry = LaneRegistry()
    registry.register("root", parent_id=None, name="main")
    registry.register("root-abc_tester", parent_id="root", name="tester", activity="writing tests")
    assert registry.active_count == 2
    record = registry.get("root-abc_tester")
    assert record is not None and record.depth == 2
    updated = registry.update("root-abc_tester", cost=Decimal("0.05"), elapsed=12.0)
    assert updated is not None and updated.lane.cost == Decimal("0.05")
    done = registry.complete("root-abc_tester", result="34 tests passing")
    assert done is not None and done.lane.state == "done"
    assert done.lane.activity == "done · 34 tests passing"
    assert registry.active_count == 1


def test_lane_registry_tolerates_child_before_parent() -> None:
    """session:start can race task:agent_spawned — depth is retro-patched."""
    registry = LaneRegistry()
    registry.register("child", parent_id="parent", name="early-bird")
    assert registry.get("child").depth == 1  # type: ignore[union-attr]
    registry.register("parent", parent_id=None, name="parent")
    assert registry.get("child").depth == 2  # type: ignore[union-attr]


def test_lane_registry_register_is_idempotent() -> None:
    registry = LaneRegistry()
    first = registry.register("s1", parent_id=None, name="a")
    second = registry.register("s1", parent_id=None, name="renamed")
    assert first == second
    assert len(registry.lanes) == 1
    # A done lane stays done by default (a completion that raced ahead of
    # its spawn must not be re-opened by the late spawn event).
    registry.complete("s1", result="ok")
    third = registry.register("s1", parent_id=None, name="a")
    assert third.lane.state == "done"


def test_lane_registry_reopen_resets_done_lane() -> None:
    """A replayed demo turn reuses sub-session ids: reopen=True resets the
    finished lane to a fresh spawned state so the panel shows live glyphs."""
    registry = LaneRegistry()
    registry.register("s1", parent_id=None, name="researcher")
    registry.update("s1", elapsed=30.0, cost=Decimal("0.12"))
    registry.complete("s1", result="3 findings")
    reopened = registry.register(
        "s1", parent_id=None, name="researcher", activity="running", reopen=True
    )
    assert reopened.lane.state == "running"
    assert (reopened.lane.glyph, reopened.lane.color_token) == ("◐", "teal")
    assert reopened.lane.activity == "running"
    assert reopened.lane.elapsed == 0.0
    assert reopened.lane.cost == Decimal("0")
    assert len(registry.lanes) == 1
    assert registry.active_count == 1


def test_lane_update_unknown_session_is_dropped() -> None:
    assert LaneRegistry().update("ghost", activity="x") is None


# -- lane tail focus (DESIGN-SPEC §8: live tail) -------------------------------


def test_tail_lane_defaults_to_first_running_then_most_recent_stream() -> None:
    lanes = LaneRegistry()
    assert lanes.tail_lane is None
    lanes.register("s1", parent_id="root", name="researcher")
    lanes.register("s2", parent_id="root", name="coder")
    tailed = lanes.tail_lane
    assert tailed is not None and tailed.session_id == "s1"  # fallback: first running
    lanes.note_stream_activity("s2")
    tailed = lanes.tail_lane
    assert tailed is not None and tailed.session_id == "s2"  # most recent stream wins


def test_cycle_tail_focus_pins_and_falls_back_when_lane_completes() -> None:
    lanes = LaneRegistry()
    lanes.register("s1", parent_id="root", name="researcher")
    lanes.register("s2", parent_id="root", name="coder")
    lanes.note_stream_activity("s2")
    pinned = lanes.cycle_tail_focus()  # from s2 → next running lane: s1
    assert pinned is not None and pinned.session_id == "s1"
    lanes.note_stream_activity("s2")  # recent changes, but the pin holds
    tailed = lanes.tail_lane
    assert tailed is not None and tailed.session_id == "s1"
    lanes.complete("s1")  # pinned lane done → falls back to most recent
    tailed = lanes.tail_lane
    assert tailed is not None and tailed.session_id == "s2"
    lanes.complete("s2")
    assert lanes.tail_lane is None
    assert lanes.cycle_tail_focus() is None


def test_note_stream_activity_ignores_done_and_unknown_lanes() -> None:
    lanes = LaneRegistry()
    lanes.register("s1", parent_id="root", name="researcher")
    lanes.note_stream_activity("never-registered")  # dropped, not fatal
    lanes.complete("s1")
    lanes.note_stream_activity("s1")  # done lanes never become the tail
    assert lanes.tail_lane is None
