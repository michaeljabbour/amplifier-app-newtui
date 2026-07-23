"""Event-sequence tests for kernel/demo.py — the five scripted demo turns.

Every test drives DemoRuntime with a no-op sleep, so the full virtual
choreography (9.3s build turn, 9.7s auto turn, …) plays with ZERO real
sleeps while still stamping exact virtual timestamps.
"""

from __future__ import annotations

import asyncio
from typing import Any

from amplifier_app_newtui.kernel.demo import (
    AGENTS_END_NOTICE,
    AGENTS_PLAN_STEPS,
    APPROVAL_OPTIONS,
    AUTO_BLOCK_REASON,
    AUTO_DEFER_NOTICE,
    AUTO_MODE_NOTICE,
    BRAINSTORM_IDEAS,
    BUILD_END_NOTICE,
    DEMO_LANES,
    DEMO_SESSION_ID,
    DEMO_TURN_BY_KEY,
    FORCE_PUSH_COMMAND,
    PLAN_END_NOTICE,
    PLAN_RECAP,
    PLAN_STEPS,
    PLAN_TITLE,
    PYTEST_APPROVAL_PROMPT,
    SEED_ANSWER,
    SEED_COMMANDS,
    STORE_COMMANDS,
    STORE_NARRATIONS,
    STORE_STEPS,
    DemoRuntime,
    tick_tokens,
)

TEXT = ["stream_block_start", "stream_block_delta", "stream_block_end", "content_block_end"]
PLAN = ["tool_pre", "tool_post"]
TODO = ["tool_pre", "tool_post"]
U = ["provider_response_usage"]


async def _instant(_: float) -> None:
    return None


def play(method: str, *, approver: Any = None) -> tuple[DemoRuntime, list[Any]]:
    """Run one scripted turn instantly and drain its events."""

    async def go() -> tuple[DemoRuntime, list[Any]]:
        runtime = DemoRuntime(sleep=_instant, approver=approver)
        await getattr(runtime, method)()
        events = []
        while not runtime.queue.empty():
            events.append(runtime.queue.get_nowait())
        return runtime, events

    return asyncio.run(go())


def kinds(events: list[Any]) -> list[str]:
    return [event.kind for event in events]


def texts(events: list[Any]) -> list[str]:
    """Durable text-block contents in order."""
    return [e.block["text"] for e in events if e.kind == "content_block_end"]


def usage_tokens(events: list[Any]) -> list[int]:
    return [e.output_tokens for e in events if e.kind == "provider_response_usage"]


# --------------------------------------------------------------------------
# Seed transcript
# --------------------------------------------------------------------------


def test_seed_sequence() -> None:
    _, events = play("run_seed")
    assert kinds(events) == (
        ["prompt_submit", "execution_start"]
        + TEXT
        + ["tool_pre", "tool_pre", "tool_post", "tool_post"]
        + TEXT
        + ["provider_response_usage", "orchestrator_complete", "execution_end", "prompt_complete"]
    )
    assert events[0].prompt == "explain what this repo is in simple terms"
    tool_pres = [e for e in events if e.kind == "tool_pre"]
    assert [t.tool_input["command"] for t in tool_pres] == list(SEED_COMMANDS)
    # Two parallel shell calls share one batch group (one dim line per batch).
    assert len({t.parallel_group_id for t in tool_pres}) == 1
    assert tool_pres[0].parallel_group_id
    assert texts(events)[-1] == SEED_ANSWER
    assert usage_tokens(events) == [83_900]
    assert events[-1].response == SEED_ANSWER


# --------------------------------------------------------------------------
# Build turn (runTurn(false), chat mode, approval allowed)
# --------------------------------------------------------------------------

_BUILD_KINDS = (
    ["prompt_submit", "execution_start"]
    + PLAN
    + TODO  # plan seeded: all pending
    # step 0
    + PLAN
    + TODO
    + TEXT
    + U
    + ["tool_pre"]
    + U
    + ["tool_post"]
    + PLAN
    + TODO
    + U
    # step 1 — chat-mode pytest approval
    + PLAN
    + TODO
    + TEXT
    + U
    + ["approval_required", "approval_granted"]
    + ["tool_pre"]
    + U
    + ["tool_post"]
    + PLAN
    + TODO
    + U
    # step 2
    + PLAN
    + TODO
    + TEXT
    + U
    + ["tool_pre"]
    + U
    + ["tool_post"]
    + PLAN
    + TODO
    + U
    + TEXT  # answer
    + TEXT  # recap
    + ["orchestrator_complete", "execution_end", "prompt_complete", "notification"]
)


def test_build_turn_full_sequence() -> None:
    runtime, events = play("run_build_turn")
    assert kinds(events) == _BUILD_KINDS
    assert runtime.clock == 9.3  # 3 × (1300 + 1400 + 400) ms of virtual time
    assert events[-1].message == BUILD_END_NOTICE
    bash_cmds = [
        e.tool_input["command"] for e in events if e.kind == "tool_pre" and e.tool_name == "bash"
    ]
    assert bash_cmds == list(STORE_COMMANDS)
    assert texts(events)[:1] == [STORE_NARRATIONS[0]]


def test_build_turn_token_ticks_match_mockup_formula() -> None:
    _, events = play("run_build_turn")
    assert usage_tokens(events) == list(tick_tokens("build"))
    assert len(usage_tokens(events)) == 9  # one tick per virtual second
    assert all(380 <= t <= 639 for t in usage_tokens(events))


def test_build_turn_plan_progression() -> None:
    _, events = play("run_build_turn")
    plans = [e for e in events if e.kind == "tool_pre" and e.tool_name == "update_plan"]
    statuses = [tuple(s["status"] for s in p.tool_input["steps"]) for p in plans]
    assert statuses == [
        ("pending", "pending", "pending"),
        ("active", "pending", "pending"),
        ("done", "pending", "pending"),
        ("done", "active", "pending"),
        ("done", "done", "pending"),
        ("done", "done", "active"),
        ("done", "done", "done"),
    ]
    assert all(p.tool_input["title"] == "Refactor session store" for p in plans)
    assert plans[0].tool_input["read_only"] is False
    assert [s["step"] for s in plans[0].tool_input["steps"]] == list(STORE_STEPS)


def test_build_turn_todo_progression_mirrors_the_plan() -> None:
    _, events = play("run_build_turn")
    todos = [e for e in events if e.kind == "tool_pre" and e.tool_name == "todo"]
    statuses = [tuple(t["status"] for t in e.tool_input["todos"]) for e in todos]
    assert statuses == [
        ("pending", "pending", "pending"),
        ("in_progress", "pending", "pending"),
        ("completed", "pending", "pending"),
        ("completed", "in_progress", "pending"),
        ("completed", "completed", "pending"),
        ("completed", "completed", "in_progress"),
        ("completed", "completed", "completed"),
    ]
    assert all(e.tool_input["operation"] == "update" for e in todos)
    assert [t["content"] for t in todos[0].tool_input["todos"]] == list(STORE_STEPS)


def test_build_turn_approval_contract() -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []

    async def approver(prompt: str, options: tuple[str, ...]) -> str:
        seen.append((prompt, options))
        return "Allow always"

    _, events = play("run_build_turn", approver=approver)
    assert seen == [(PYTEST_APPROVAL_PROMPT, APPROVAL_OPTIONS)]
    required = next(e for e in events if e.kind == "approval_required")
    assert required.prompt == PYTEST_APPROVAL_PROMPT
    assert required.options == ("Allow once", "Allow always", "Deny")
    granted = next(e for e in events if e.kind == "approval_granted")
    assert granted.choice == "Allow always"


def test_build_turn_skips_approval_outside_chat_mode() -> None:
    """Spec §4 / mockup ``if (this.mode().id === "chat" && i === 1)``:
    the pytest approval is gated on the LIVE mode — build trust is
    ``auto read,test``, so pytest auto-runs with no ask."""

    async def go() -> list[Any]:
        runtime = DemoRuntime(sleep=_instant, mode_source=lambda: "build")
        await runtime.run_build_turn()
        events = []
        while not runtime.queue.empty():
            events.append(runtime.queue.get_nowait())
        return events

    events = asyncio.run(go())
    assert not [e for e in events if e.kind in ("approval_required", "approval_granted")]
    # pytest still runs (auto read,test) — all three commands execute.
    bash_cmds = [
        e.tool_input["command"] for e in events if e.kind == "tool_pre" and e.tool_name == "bash"
    ]
    assert bash_cmds == list(STORE_COMMANDS)


def test_build_turn_deny_path() -> None:
    async def deny(prompt: str, options: tuple[str, ...]) -> str:
        return "Deny"

    runtime, events = play("run_build_turn", approver=deny)
    expected = (
        ["prompt_submit", "execution_start"]
        + PLAN
        + TODO
        + PLAN
        + TODO
        + TEXT
        + U
        + ["tool_pre"]
        + U
        + ["tool_post"]
        + PLAN
        + TODO
        + U
        + PLAN
        + TODO
        + TEXT
        + U
        + ["approval_required", "approval_denied"]
        + PLAN
        + TODO
        + PLAN
        + TODO
        + TEXT
        + U
        + ["tool_pre"]
        + U
        + U
        + ["tool_post"]
        + PLAN
        + TODO
        + TEXT
        + TEXT
        + ["orchestrator_complete", "execution_end", "prompt_complete", "notification"]
    )
    assert kinds(events) == expected
    denied = next(e for e in events if e.kind == "approval_denied")
    assert denied.prompt == PYTEST_APPROVAL_PROMPT
    assert denied.reason == "denied by user"
    # The pytest command never runs; the denied step still completes.
    bash_cmds = [
        e.tool_input["command"] for e in events if e.kind == "tool_pre" and e.tool_name == "bash"
    ]
    assert bash_cmds == [STORE_COMMANDS[0], STORE_COMMANDS[2]]
    assert "(tests skipped by your denial)" in texts(events)[-2]
    # Deny path: 7 virtual seconds, first 7 formula draws.
    assert runtime.clock == 7.5
    assert usage_tokens(events) == list(tick_tokens("build", 7))


# --------------------------------------------------------------------------
# Auto turn (runTurn(true)): force-push block + deferred decision
# --------------------------------------------------------------------------

_AUTO_KINDS = (
    ["notification", "prompt_submit", "execution_start"]
    + PLAN
    + TODO
    + PLAN
    + TODO
    + TEXT
    + U
    + ["tool_pre"]
    + U
    + ["tool_post"]
    + PLAN
    + TODO
    + U
    + PLAN
    + TODO
    + TEXT
    + U
    + ["tool_pre"]
    + U
    + ["tool_post"]
    + PLAN
    + TODO
    + U
    + PLAN
    + TODO
    + TEXT
    + U
    + ["tool_pre"]
    + U
    + ["tool_post", "approval_denied"]
    + U
    + TEXT  # defer narration
    + ["notification"]  # decision deferred to needs-you
    + PLAN
    + TODO
    + TEXT
    + TEXT
    + ["orchestrator_complete", "execution_end", "prompt_complete"]  # no end notice
)


def test_auto_turn_full_sequence() -> None:
    runtime, events = play("run_auto_turn")
    assert kinds(events) == _AUTO_KINDS
    assert runtime.clock == 9.7
    assert events[0].message == AUTO_MODE_NOTICE
    assert events[0].source == "mode"
    # Mockup: the blocked turn ends with NO turn-end notice.
    assert events[-1].kind == "prompt_complete"


def test_auto_turn_force_push_block() -> None:
    _, events = play("run_auto_turn")
    force_pre = next(
        e
        for e in events
        if e.kind == "tool_pre" and e.tool_input.get("command") == FORCE_PUSH_COMMAND
    )
    force_post = next(
        e for e in events if e.kind == "tool_post" and e.tool_call_id == force_pre.tool_call_id
    )
    assert force_post.result == {
        "status": "denied",
        "reason": AUTO_BLOCK_REASON,
        "continuation": "finding safer path",
    }
    denied = next(e for e in events if e.kind == "approval_denied")
    assert denied.prompt == FORCE_PUSH_COMMAND
    assert denied.reason == AUTO_BLOCK_REASON
    deferred = next(e for e in events if e.kind == "notification" and e.source == "needs_you")
    assert deferred.message == AUTO_DEFER_NOTICE
    assert deferred.level == "decision"
    assert usage_tokens(events) == list(tick_tokens("auto"))


# --------------------------------------------------------------------------
# Plan turn
# --------------------------------------------------------------------------


def test_plan_turn_sequence() -> None:
    runtime, events = play("run_plan_turn")
    assert kinds(events) == (
        ["notification", "prompt_submit", "execution_start"]
        + TEXT
        + PLAN
        + PLAN
        + PLAN
        + PLAN
        + TEXT
        + [
            "provider_response_usage",
            "orchestrator_complete",
            "execution_end",
            "prompt_complete",
            "notification",
        ]
    )
    assert runtime.clock == 3.6
    assert events[0].message == "mode plan · read-only"
    plans = [e for e in events if e.kind == "tool_pre"]
    # Head lands first, then steps stream in one at a time — all read-only.
    assert [len(p.tool_input["steps"]) for p in plans] == [0, 1, 2, 3]
    assert all(p.tool_input["read_only"] is True for p in plans)
    assert all(p.tool_input["title"] == PLAN_TITLE for p in plans)
    assert [s["step"] for s in plans[-1].tool_input["steps"]] == list(PLAN_STEPS)
    assert all(
        s["status"] == "pending" for p in plans for s in p.tool_input["steps"]
    )  # plan mode never executes
    assert texts(events)[-1] == PLAN_RECAP
    assert usage_tokens(events) == [9_400]
    assert events[-1].message == PLAN_END_NOTICE


# --------------------------------------------------------------------------
# Brainstorm turn
# --------------------------------------------------------------------------


def test_brainstorm_turn_sequence() -> None:
    runtime, events = play("run_brainstorm_turn")
    assert kinds(events) == (
        ["notification", "prompt_submit", "execution_start"]
        + TEXT
        + TEXT
        + TEXT
        + TEXT
        + TEXT
        + TEXT
        + ["provider_response_usage", "orchestrator_complete", "execution_end", "prompt_complete"]
    )
    assert runtime.clock == 3.0
    # No tools in brainstorm — spec §4 trust string is literal.
    assert not [e for e in events if e.kind in ("tool_pre", "tool_post")]
    assert texts(events)[1:5] == list(BRAINSTORM_IDEAS)
    roles = [e.block["demo_role"] for e in events if e.kind == "content_block_end"]
    assert roles == ["narration", "idea", "idea", "idea", "idea", "recap"]
    assert usage_tokens(events) == [4_100]


# --------------------------------------------------------------------------
# Multi-agent turn
# --------------------------------------------------------------------------


def _child_stream(deltas: int) -> list[str]:
    """One child-session Channel-A burst (lane live tail, spec §8): a full
    stream envelope but NO durable ``content_block_end`` — child prose never
    lands in the parent transcript (design doc D4)."""
    return ["stream_block_start"] + ["stream_block_delta"] * deltas + ["stream_block_end"]


def test_agents_turn_sequence() -> None:
    runtime, events = play("run_agents_turn")
    assert kinds(events) == (
        ["notification", "prompt_submit", "execution_start"]
        + TEXT
        + TODO
        + ["agent_spawned"] * 3
        + _child_stream(2)  # researcher: 2 narration rows
        + _child_stream(2)  # coder: 2 narration rows
        + _child_stream(1)  # tester: 1 answer row
        + U
        + U
        + ["agent_completed"]
        + TODO  # tester at 2.6s
        + U
        + U
        + ["agent_completed"]
        + TODO  # researcher at 4.4s
        + U
        + U
        + ["agent_completed"]
        + TODO  # coder at 6.0s
        + TEXT
        + ["orchestrator_complete", "execution_end", "prompt_complete", "notification"]
    )
    # Child bursts travel on the lanes' own sessions, parented to the root.
    child_events = [e for e in events if e.session_id != DEMO_SESSION_ID]
    assert {e.session_id for e in child_events} == {lane.sub_session_id for lane in DEMO_LANES}
    assert all(e.parent_id == DEMO_SESSION_ID for e in child_events)
    assert {e.kind for e in child_events} == {
        "stream_block_start",
        "stream_block_delta",
        "stream_block_end",
    }
    assert runtime.clock == 6.0
    spawned = [e for e in events if e.kind == "agent_spawned"]
    assert [s.agent for s in spawned] == ["researcher", "coder", "tester"]
    assert all(s.parent_session_id == DEMO_SESSION_ID for s in spawned)
    assert {s.sub_session_id for s in spawned} == {lane.sub_session_id for lane in DEMO_LANES}
    completed = [e for e in events if e.kind == "agent_completed"]
    assert [(c.agent, c.ts) for c in completed] == [
        ("tester", 2.6),
        ("researcher", 4.4),
        ("coder", 6.0),
    ]
    assert all(c.success for c in completed)
    assert usage_tokens(events) == [900] * 6
    assert events[-1].message == AGENTS_END_NOTICE
    # The scripted plan progresses to all-completed: the ambient plan panel
    # (Phase 1) and the delegate summary's ``Plan 4/4`` fold (Phase 2) both
    # feed off these todo beats.
    todo_pres = [e for e in events if e.kind == "tool_pre" and e.tool_name == "todo"]
    assert len(todo_pres) == 4
    assert [t["content"] for t in todo_pres[0].tool_input["todos"]] == list(AGENTS_PLAN_STEPS)
    final = todo_pres[-1].tool_input["todos"]
    assert all(t["status"] == "completed" for t in final)


# --------------------------------------------------------------------------
# Whole-session run
# --------------------------------------------------------------------------


def test_run_all_lifecycle_and_determinism() -> None:
    sleeps: list[float] = []

    async def counting_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def go() -> tuple[DemoRuntime, list[Any]]:
        runtime = DemoRuntime(sleep=counting_sleep)
        await runtime.run_all()
        events = []
        while not runtime.queue.empty():
            events.append(runtime.queue.get_nowait())
        return runtime, events

    runtime, events = asyncio.run(go())
    assert events[0].kind == "session_start"
    assert events[-1].kind == "session_end"
    prompts = [e.prompt for e in events if e.kind == "prompt_submit"]
    assert prompts == [
        DEMO_TURN_BY_KEY[k].prompt
        for k in ("seed", "build", "auto", "plan", "brainstorm", "agents")
    ]
    # Deterministic envelope: unique monotonic ids, monotonic virtual ts.
    ids = [e.event_id for e in events]
    assert len(set(ids)) == len(ids)
    ts = [e.ts for e in events]
    assert ts == sorted(ts)
    # Total virtual time = 9.3 + 9.7 + 3.6 + 3.0 + 6.0 (seed is instant).
    assert runtime.clock == 31.6
    assert round(sum(sleeps), 6) == 31.6  # paced entirely through the injected sleep
    # Only the agents turn's child stream bursts leave the root session —
    # each parented to it (lane live tail, spec §8).
    lane_ids = {lane.sub_session_id for lane in DEMO_LANES}
    for event in events:
        if event.session_id == DEMO_SESSION_ID:
            continue
        assert event.session_id in lane_ids
        assert event.parent_id == DEMO_SESSION_ID
        assert event.kind in ("stream_block_start", "stream_block_delta", "stream_block_end")


def test_two_runs_emit_identical_streams() -> None:
    _, first = play("run_build_turn")
    _, second = play("run_build_turn")
    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]
