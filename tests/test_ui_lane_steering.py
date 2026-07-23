"""Per-lane steering UI surface (issue #39).

Three layers: the ``▸ N queued`` lane-row badge (pure formatting), the
delivery echo landing in a lane's focus transcript (reducer routing), and
the end-to-end send path over the real app — a focused lane's mid-turn
Enter steers THAT delegate, shows a "queued for lane" chat line, a notice,
and increments the badge.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import Answer, SessionBanner
from amplifier_app_newtui.model.lanes import LaneRecord, LaneState
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.lanes_panel import format_lane_lines

from .test_flow_helpers import SIZE, seed_done, type_text, wait_for
from .test_ui_reducer_delegates import SID, _env, make_reducer


# -- badge (pure formatting) --------------------------------------------


def _lane(name: str, state: str = "running") -> LaneState:
    return LaneState.for_state(
        name=name, state=state, activity="working", elapsed=41, cost=Decimal("0.09")  # type: ignore[arg-type]
    )


def test_badge_appended_when_a_lane_has_queued_steers() -> None:
    lanes = (_lane("researcher"), _lane("coder"))
    lines = format_lane_lines(lanes, queued_counts=[1, 0])
    assert "▸ 1 queued" in lines[0]
    assert "queued" not in lines[1]  # no badge without a queue


def test_badge_absent_by_default() -> None:
    lines = format_lane_lines((_lane("researcher"),))
    assert "queued" not in lines[0]


def test_badge_pluralises_by_count() -> None:
    lines = format_lane_lines((_lane("researcher"),), queued_counts=[3])
    assert "▸ 3 queued" in lines[0]


# -- delivery echo (reducer routing) ------------------------------------


def _child_env(sub: str, ts: float) -> dict:
    return {"event_id": f"c{ts}", "session_id": sub, "parent_id": SID, "ts": ts}


def test_delivery_echo_lands_in_the_lanes_focus_transcript() -> None:
    reducer, _host = make_reducer()
    reducer.handle(ev.PromptSubmit(**_env(0.0), prompt="fan out"))
    reducer.handle(
        ev.AgentSpawned(
            **_env(1.0), agent="researcher", sub_session_id="s1", parent_session_id=SID
        )
    )
    # The runtime's _lane_steer_applied emits exactly this child-stamped
    # narration when it delivers a lane steer at the child's step boundary.
    reducer.handle(
        ev.ContentBlockEnd(
            **_child_env("s1", 2.0),
            block_type="text",
            block={"text": "Applying steer: focus on the tests", "demo_role": "narration"},
        )
    )
    blocks = reducer.lane_transcript("s1")
    assert blocks is not None
    prose = "\n".join(
        "".join(s.text for s in b.spans) for b in blocks if isinstance(b, Answer)
    )
    assert "Applying steer: focus on the tests" in prose


# -- end-to-end send path (real app over the demo runtime) --------------


def _register_running_lane(app: NewTuiApp, session_id: str, name: str) -> LaneRecord:
    return app.lanes.register(session_id, parent_id="root", name=name, state="running")


@pytest.mark.asyncio
async def test_focused_lane_steer_queues_badge_and_chat_line() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        _register_running_lane(app, "child-1", "researcher")
        # Focus the lane (bypass the panel path — the app state we exercise
        # is transcript.focused_lane) and simulate a live turn.
        await app.transcript.focus_lane(
            "child-1", [SessionBanner(id=app.allocator.next_id(), headline="")]
        )
        app.turn_active = True
        app.composer.running = True

        await type_text(pilot, "focus on the parser")
        await pilot.press("enter")
        assert await wait_for(
            pilot, lambda: app.adapter.lane_steering.queued_count("child-1") == 1
        )
        assert "researcher" in app.notice_slot.current
        # Badge is painted on the lane row.
        app.lanes_panel.show_panel(focus=False)
        assert await wait_for(
            pilot, lambda: any("▸ 1 queued" in line for line in app.lanes_panel.lane_lines)
        )
        # The "queued for lane" line is in the parent chat (restored on esc).
        await app.transcript.restore_main()
        chat = [
            "".join(s.text for s in b.spans)
            for b in app.transcript.blocks
            if isinstance(b, Answer)
        ]
        assert any("queued for lane researcher" in line for line in chat)


@pytest.mark.asyncio
async def test_root_steer_unaffected_when_no_lane_focused() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.turn_active = True
        app.composer.running = True

        await type_text(pilot, "steer the root")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: bool(app.adapter.steering.pending_steers))
        # No lane focused → root queue used, lane queues stay empty.
        assert app.adapter.lane_steering.total_pending == 0
        assert [b.kind for b in app.transcript.blocks if b.kind == "steer_echo"]


@pytest.mark.asyncio
async def test_done_lane_falls_back_to_root_steer() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.lanes.register("child-done", parent_id="root", name="tester", state="done")
        await app.transcript.focus_lane(
            "child-done", [SessionBanner(id=app.allocator.next_id(), headline="")]
        )
        app.turn_active = True
        app.composer.running = True

        await type_text(pilot, "too late")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: bool(app.adapter.steering.pending_steers))
        assert app.adapter.lane_steering.queued_count("child-done") == 0
