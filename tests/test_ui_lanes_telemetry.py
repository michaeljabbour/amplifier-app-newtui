"""Per-agent lane telemetry: live elapsed clock + per-lane token routing.

Claude-Code-style agent panel — every running lane's ``elapsed`` ticks on
the app heartbeat (:meth:`LaneRegistry.advance`) and per-response usage
stamped with a child session id is routed to that lane's token/cost
counters (:meth:`TranscriptReducer._usage`), never to the root turn's lane
(the root is never a registered lane).
"""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.lanes import LaneRegistry

from .test_ui_reducer_outcomes import FakeHost, make_reducer


class CountingHost(FakeHost):
    """FakeHost that counts ``lanes_changed`` fan-outs."""

    def __init__(self, mode_id: str = "chat") -> None:
        super().__init__(mode_id)
        self.lanes_changed_calls = 0

    def lanes_changed(self) -> None:
        self.lanes_changed_calls += 1


# -- LaneRegistry.advance -------------------------------------------------


def test_advance_ticks_running_lanes_and_freezes_done() -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="researcher", now=100.0)
    reg.register("b", parent_id=None, name="coder", now=100.0)
    reg.complete("b", result="tests ✔")  # done lanes are frozen

    changed = reg.advance(110.0)
    assert changed is True
    assert reg.get("a").lane.elapsed == 10.0  # running lane ticked
    assert reg.get("b").lane.elapsed == 0.0  # done lane left alone
    assert reg.get("b").lane.state == "done"

    # Idempotent: advancing to the same wall time changes nothing.
    assert reg.advance(110.0) is False


def test_advance_ignores_lanes_without_started_at() -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="a")  # no now → started_at 0.0
    assert reg.advance(500.0) is False
    assert reg.get("a").lane.elapsed == 0.0


# -- LaneRegistry.update(tokens=) -----------------------------------------


def test_update_sets_lane_tokens() -> None:
    reg = LaneRegistry()
    reg.register("a", parent_id=None, name="coder")
    assert reg.get("a").lane.tokens == 0
    reg.update("a", tokens=1234)
    assert reg.get("a").lane.tokens == 1234
    # tokens=None on a later update keeps the existing count.
    reg.update("a", activity="still going")
    assert reg.get("a").lane.tokens == 1234


# -- reducer._usage per-lane routing --------------------------------------


def test_usage_routes_child_tokens_to_lane_but_not_root() -> None:
    reducer, host = make_reducer("auto")
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="fan out", ts=1.0))
    reducer.lanes.register("child", parent_id="root", name="coder", now=1.0)

    # Usage stamped with the child session lands on the child lane AND the
    # session/turn totals.
    reducer.handle(
        ev.ProviderResponseUsage(
            session_id="child", output_tokens=1000, cost_usd=Decimal("0.20"), ts=2.0
        )
    )
    child = reducer.lanes.get("child")
    assert child is not None
    assert child.lane.tokens == 1000
    assert child.lane.cost == Decimal("0.20")
    assert reducer.total_tokens == 1000
    assert reducer._turn is not None and reducer._turn.tokens == 1000

    # Usage stamped with the ROOT session touches no lane (root is never a
    # registered lane) but still increments the session/turn totals.
    reducer.handle(ev.ProviderResponseUsage(session_id="root", output_tokens=500, ts=3.0))
    assert reducer.lanes.get("child").lane.tokens == 1000  # unchanged
    assert reducer.total_tokens == 1500
    assert reducer._turn.tokens == 1500


def test_usage_without_cost_usd_falls_back_to_estimate() -> None:
    reducer, _host = make_reducer("auto")
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="go", ts=1.0))
    reducer.lanes.register("child", parent_id="root", name="coder", now=1.0)
    reducer.handle(
        ev.ProviderResponseUsage(
            session_id="child", input_tokens=100, output_tokens=2000, model="fake", ts=2.0
        )
    )
    lane = reducer.lanes.get("child")
    assert lane is not None
    assert lane.lane.tokens == 2000  # tokens are the requirement
    assert lane.lane.cost >= Decimal("0")  # cost best-effort (0 when unpriceable)


def test_child_session_start_reconciles_redacted_spawn_id_for_live_usage() -> None:
    """Foundation may redact the spawn id but expose the real id on session:start."""
    reducer, _host = make_reducer("auto")
    root = "root-session"
    redacted = "[REDACTED:PII]-a7b97feb6f684d29_foundation-explorer"
    actual = "0000000000000000-a7b97feb6f684d29_foundation-explorer"
    reducer.handle(ev.PromptSubmit(session_id=root, prompt="fan out", ts=1.0))
    reducer.handle(
        ev.AgentSpawned(
            session_id=root,
            parent_session_id=root,
            sub_session_id=redacted,
            agent="foundation:explorer",
            ts=2.0,
        )
    )
    reducer.handle(ev.SessionStart(session_id=actual, parent_id=root, ts=2.1))
    reducer.handle(
        ev.ProviderResponseUsage(
            session_id=actual,
            output_tokens=9904,
            cost_usd=Decimal("1.9752735"),
            ts=3.0,
        )
    )

    lane = reducer.lanes.get(actual)
    assert lane is not None
    assert lane.session_id == actual  # lane focus now has a usable session id
    assert lane.lane.tokens == 9904
    assert lane.lane.cost == Decimal("1.9752735")
    assert reducer.lanes.get(redacted) == lane  # completion's redacted id remains an alias

    reducer.handle(
        ev.AgentCompleted(
            session_id=root,
            parent_session_id=root,
            sub_session_id=redacted,
            agent="foundation:explorer",
            success=True,
            ts=4.0,
        )
    )
    assert reducer.lanes.get(actual).lane.state == "done"


def test_redacted_lane_reconciliation_tolerates_session_start_race() -> None:
    reducer, _host = make_reducer("auto")
    root = "root-session"
    redacted = "[REDACTED:PII]-abcdef1234567890_foundation-explorer"
    actual = "0000000000000000-abcdef1234567890_foundation-explorer"
    reducer.handle(ev.PromptSubmit(session_id=root, prompt="fan out", ts=1.0))
    reducer.handle(ev.SessionStart(session_id=actual, parent_id=root, ts=1.5))
    reducer.handle(
        ev.AgentSpawned(
            session_id=root,
            parent_session_id=root,
            sub_session_id=redacted,
            agent="foundation:explorer",
            ts=2.0,
        )
    )
    lane = reducer.lanes.get(actual)
    assert lane is not None and lane.session_id == actual


def test_live_session_cost_moves_before_turn_close() -> None:
    reducer, _host = make_reducer("auto")
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="go", ts=1.0))
    reducer.handle(
        ev.ProviderResponseUsage(
            session_id="root",
            output_tokens=500,
            cost_usd=Decimal("0.75"),
            ts=2.0,
        )
    )

    assert reducer.session_cost == Decimal("0")  # checkpoint total commits at close
    assert reducer.live_session_cost == Decimal("0.75")
    assert reducer.live_cost_estimated is False


# -- reducer.tick advances lanes ------------------------------------------


def test_tick_advances_lanes_and_fires_lanes_changed() -> None:
    host = CountingHost("auto")
    from amplifier_app_newtui.model.blocks import BlockIdAllocator
    from amplifier_app_newtui.model.turn import OutcomeLedger
    from amplifier_app_newtui.ui.reducer import TranscriptReducer

    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="fan out", ts=100.0))
    reducer.lanes.register("child", parent_id="root", name="coder", now=100.0)
    before = host.lanes_changed_calls

    reducer.tick(105.0)
    assert reducer.lanes.get("child").lane.elapsed == 5.0
    assert host.lanes_changed_calls > before


def test_child_events_stream_compact_activity_into_lane_and_tree() -> None:
    reducer, host = make_reducer("auto")
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="fan out", ts=1.0))
    reducer.handle(
        ev.AgentSpawned(
            session_id="root",
            parent_session_id="root",
            sub_session_id="child",
            agent="foundation:explorer",
            ts=2.0,
        )
    )

    reducer.handle(
        ev.ToolPre(
            session_id="child",
            tool_call_id="read-1",
            tool_name="read_file",
            tool_input={"file_path": "/repo/README.md"},
            ts=3.0,
        )
    )
    lane = reducer.lanes.get("child")
    assert lane is not None
    assert lane.lane.state == "working"
    assert lane.lane.activity == "reading README.md"
    # The in-transcript agent-tree activity ticker is retired
    # (ambient-progress D5) — live child activity now lives only on the
    # lane (asserted above); the LanesPanel is the activity surface.
    assert not [block for block in host.blocks if block.kind == "answer"]

    reducer.handle(
        ev.ToolPre(
            session_id="child",
            tool_call_id="edit-1",
            tool_name="edit_file",
            tool_input={
                "file_path": "/repo/src/app.py",
                "old_string": "old_value = 1",
                "new_string": "new_value = 2",
            },
            ts=3.5,
        )
    )
    reducer.handle(
        ev.ToolPost(
            session_id="child",
            tool_call_id="edit-1",
            tool_name="edit_file",
            result={"success": True},
            ts=3.6,
        )
    )
    changes = next(block for block in host.blocks if block.kind == "tool_line")
    assert changes.summary == "Changed 1 file"
    assert changes.body_style == "diff"
    assert "foundation:explorer · edit file · /repo/src/app.py" in changes.body
    assert "-old_value = 1" in changes.body
    assert "+new_value = 2" in changes.body

    reducer.handle(
        ev.ToolPre(
            session_id="child",
            tool_call_id="patch-1",
            tool_name="apply_patch",
            tool_input={
                "patch": "\n".join(
                    (
                        "*** Begin Patch",
                        "*** Update File: src/one.py",
                        "-old",
                        "+new",
                        "*** Add File: src/two.py",
                        "+created",
                        "*** End Patch",
                    )
                )
            },
            ts=3.7,
        )
    )
    reducer.handle(
        ev.ToolPost(
            session_id="child",
            tool_call_id="patch-1",
            tool_name="apply_patch",
            result={"success": True},
            ts=3.8,
        )
    )
    changes = next(block for block in host.blocks if block.kind == "tool_line")
    assert changes.summary == "Changed 3 files"
    assert "foundation:explorer · apply patch · src/one.py, src/two.py" in changes.body

    reducer.handle(
        ev.StreamBlockStart(session_id="child", block_type="text", request_id="r1", ts=4.0)
    )
    lane = reducer.lanes.get("child")
    assert lane is not None
    assert lane.lane.state == "running"
    assert lane.lane.activity == "writing response"
    assert sum(block.kind == "tool_line" for block in host.blocks) == 1
