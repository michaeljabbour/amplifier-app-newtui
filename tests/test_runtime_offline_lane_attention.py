"""Production-path tests for D5 AC1 (lane-level attention state).

Existing lane-state coverage (``tests/test_ui_reducer_delegates.py`` /
``tests/test_model_lanes.py``) drives the reducer with hand-built
``ev.AgentCompleted``/``ev.ToolError`` objects -- solid unit coverage, but
it never proves the REAL production event pipeline (foundation hooks ->
``kernel.runtime.RealRuntime`` normalization -> ``UIEvent``) produces the
same shapes. This module reuses ``tests/test_runtime_offline.py``'s
established fake-module-bundle harness (a REAL ``RealRuntime`` against
fake provider/context/tool/orchestrator modules, no network, no API keys)
and extends it two ways:

1. A REAL delegate spawn + a REAL FAILING completion (``delegate:agent_
   completed`` with ``success: False``), draining the genuine normalized
   events and feeding them through an ACTUAL ``TranscriptReducer`` --
   proving the full chain (not just the kernel-normalization half that
   ``test_offline_spawn_child_telemetry_reaches_the_queue`` already
   covers for the success path).
2. A REAL child-session tool failure. The fake orchestrator's own
   ``write_file`` tool always succeeds (it is shared, session-scoped
   fixture code other tests depend on byte-for-byte), so the child's own
   ``tool:error`` is hand-emitted -- but through the SAME real hooks bus
   the fake orchestrator itself uses, with an explicit ``session_id``
   matching what a genuine child-session tool failure would carry
   (confirmed empirically: ``kernel/events.py``'s ``normalize()`` reads
   ``session_id`` straight off the raw payload dict, the same field a
   real child-scoped hook emission would carry \u2014 this is not a
   different code path from the real one, just a hand-supplied payload
   on it, exactly like this file's sibling already does for
   ``delegate:agent_spawned``/``delegate:agent_completed``).

What this does NOT do (disclosed, not faked): boot the full THREADED
``RealRuntimeAdapter`` (its own thread + event loop) against this fake
bundle. No existing test does that either -- ``test_runtime_adapter_real.py``
monkeypatches ``RealRuntime`` itself away in favor of a recording fake to
test the thread/marshalling machinery in isolation, and that machinery
(``_AppLoopQueue.put_nowait`` -> ``call_soon_threadsafe``) carries no
lane-state logic of its own (already covered by that file's
``test_app_loop_queue_hops_threads``). What IS new here for the adapter
seam specifically: :func:`test_lane_seed_reads_the_real_agent_brief_off_a_real_runtime`
wires ``RealRuntimeAdapter._runtime`` directly to a genuinely-real,
offline-booted ``RealRuntime`` (skipping only the thread-boot plumbing,
not the runtime) to prove ``lane_seed()`` reads a REAL delegate brief
rather than a stand-in double's.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import BlockIdAllocator
from amplifier_app_tui.model.lanes import LaneRegistry
from amplifier_app_tui.model.turn import OutcomeLedger
from amplifier_app_tui.ui.reducer import TranscriptReducer
from amplifier_app_tui.ui.runtime_adapter import RealRuntimeAdapter

from .test_runtime_offline import _drain_kinds, _started_runtime, offline_env, offline_workspace  # noqa: F401

_SUB_ID_SUFFIX = "-deadbeefcafef00d_scout"


class _FakeHost:
    """Minimal ReducerHost: records nothing but what these tests inspect."""

    mode_id = "auto"

    def append_block(self, block) -> None:
        pass

    def replace_block(self, block) -> None:
        pass

    def remove_block(self, block_id: str) -> None:
        pass

    def show_notice(self, text: str) -> None:
        pass

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        pass

    def turn_started(self) -> None:
        pass

    def turn_finished(self) -> None:
        pass

    def lanes_changed(self) -> None:
        pass

    def plan_changed(self, items) -> None:
        pass

    def approval_opened(self, prompt: str, options) -> None:
        pass

    def decision_deferred(self, message: str, decision_id: str = "") -> None:
        pass

    def attention_error(self, detail: str, *, occasion: str) -> None:
        pass

    def stream_opened(self, block_type: str) -> None:
        pass

    def stream_delta(self, text: str) -> None:
        pass

    def stream_closed(self) -> None:
        pass

    def lane_tail_updated(self, text: str) -> None:
        pass

    def lane_tail_cleared(self) -> None:
        pass


def _real_reducer() -> TranscriptReducer:
    return TranscriptReducer(
        _FakeHost(), allocator=BlockIdAllocator(), ledger=OutcomeLedger(), lanes=LaneRegistry()
    )


async def _real_spawn(runtime, *, agent: str, sub_id: str, instruction: str):
    """Drive a REAL ``session.spawn`` fan-out (mirrors
    ``test_offline_spawn_child_telemetry_reaches_the_queue`` verbatim --
    the ground-truth kwargs tool-delegate passes)."""
    initialized = runtime._initialized
    assert initialized is not None
    root_id = initialized.session_id
    spawn = initialized.coordinator.get_capability("session.spawn")
    assert spawn is not None
    hooks = initialized.coordinator.hooks
    await hooks.emit(
        "delegate:agent_spawned",
        {
            "agent": agent,
            "sub_session_id": sub_id,
            "parent_session_id": root_id,
            "context_depth": "recent",
            "context_scope": "conversation",
            "tool_call_id": "call-7",
            "parallel_group_id": None,
            "model_role": None,
            "provider_preferences": None,
        },
    )
    result = await spawn(
        agent_name=agent,
        instruction=instruction,
        parent_session=initialized.session,
        agent_configs={},
        sub_session_id=sub_id,
        tool_inheritance={"exclude_tools": ["tool-delegate"]},
        hook_inheritance={},
        orchestrator_config={"usage_on_block_end": True},
        provider_preferences=None,
        self_delegation_depth=0,
        session_metadata={"agent_name": agent, "tool_call_id": "call-7"},
    )
    return root_id, result


async def _drive_reducer_through_spawn(reducer: TranscriptReducer, runtime, sub_id: str) -> None:
    """Replay every drained real event onto a real reducer (root turn
    already started), exactly as ``ui/app.py`` does off the live queue."""
    for event in _drain_kinds(runtime):
        reducer.handle(event)


@pytest.mark.asyncio
async def test_offline_agent_completed_failure_settles_lane_error_via_real_reducer(
    offline_env,  # noqa: F811 -- pytest fixture param, shadowing the re-export import above
) -> None:
    """A REAL delegate spawn + a REAL FAILING completion, fed through an
    ACTUAL TranscriptReducer, must settle the lane to "error" -- not the
    old fold-into-"done" behavior (D5 AC1), end to end through the
    production kernel path (not a hand-built ``ev.AgentCompleted``)."""
    runtime = await _started_runtime(offline_env["project"], mode="auto")
    reducer = _real_reducer()
    try:
        sub_id = f"root{_SUB_ID_SUFFIX}"
        reducer.handle(ev.PromptSubmit(session_id=runtime.session_id, prompt="fan out", ts=0.0))
        root_id, result = await _real_spawn(
            runtime, agent="scout", sub_id=sub_id, instruction="please write hello.txt with hi"
        )
        await _drive_reducer_through_spawn(reducer, runtime, sub_id)
        lane = reducer.lanes.get(sub_id)
        assert lane is not None and lane.lane.state in ("booting", "running", "working")

        initialized = runtime._initialized
        await initialized.coordinator.hooks.emit(
            "delegate:agent_completed",
            {
                "agent": "scout",
                "sub_session_id": sub_id,
                "parent_session_id": root_id,
                "success": False,
                "tool_call_id": "call-7",
                "parallel_group_id": None,
            },
        )
        for event in _drain_kinds(runtime):
            reducer.handle(event)

        lane = reducer.lanes.get(sub_id)
        assert lane is not None
        assert lane.lane.state == "error"  # the D5 AC1 gap: never folds into "done"
        assert reducer.lanes.active_count == 0
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_offline_child_tool_failure_settles_lane_attention_via_real_reducer(
    offline_env,  # noqa: F811 -- pytest fixture param, shadowing the re-export import above
) -> None:
    """A REAL child-session tool failure (emitted on the SAME real hooks
    bus the fake orchestrator itself uses, session_id-stamped exactly as
    a genuine child-scoped hook would be) must settle the still-running
    lane to "attention" -- the missing live state -- through the real
    kernel normalization boundary and an ACTUAL TranscriptReducer."""
    runtime = await _started_runtime(offline_env["project"], mode="auto")
    reducer = _real_reducer()
    try:
        sub_id = f"root{_SUB_ID_SUFFIX}"
        reducer.handle(ev.PromptSubmit(session_id=runtime.session_id, prompt="fan out", ts=0.0))
        root_id, _result = await _real_spawn(
            runtime, agent="debugger", sub_id=sub_id, instruction="fix the failing test"
        )
        await _drive_reducer_through_spawn(reducer, runtime, sub_id)
        assert reducer.lanes.get(sub_id) is not None

        initialized = runtime._initialized
        await initialized.coordinator.hooks.emit(
            "tool:error",
            {
                "session_id": sub_id,
                "parent_session_id": root_id,
                "tool_name": "bash",
                "tool_call_id": "call-x",
                "error_type": "runtime_error",
                "error_message": "disk full",
            },
        )
        for event in _drain_kinds(runtime):
            if event.kind == "tool_error":
                reducer.handle(event)

        lane = reducer.lanes.get(sub_id)
        assert lane is not None
        assert lane.lane.state == "attention"
        assert lane.lane.activity == "recovering from bash error"
        assert reducer.lanes.active_count == 1  # attention is NOT terminal
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_lane_seed_reads_the_real_agent_brief_off_a_real_runtime(
    offline_env,  # noqa: F811 -- pytest fixture param, shadowing the re-export import above
) -> None:
    """``RealRuntimeAdapter.lane_seed`` wired to a genuinely-real (offline)
    ``RealRuntime`` -- not the ``FakeRealRuntime`` double
    ``tests/test_runtime_adapter_real.py`` uses to test the THREAD seam.
    Skips only the background-thread boot plumbing (pure marshalling,
    separately covered), not the runtime itself."""
    runtime = await _started_runtime(offline_env["project"], mode="auto")
    try:
        adapter = RealRuntimeAdapter()
        adapter._runtime = runtime  # the real seam lane_seed() reads; no thread needed

        assert adapter.lane_seed("scout") is None  # nothing spawned yet -- no brief recorded

        sub_id = f"root{_SUB_ID_SUFFIX}"
        await _real_spawn(
            runtime, agent="scout", sub_id=sub_id, instruction="scan the provider docs"
        )

        seed = adapter.lane_seed("scout")
        assert seed is not None
        assert seed.activity == "scan the provider docs"
        assert seed.state == "running"  # LaneSeed's own default -- booting is derived downstream
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_publish_attention_emits_attention_recorded_on_the_real_hooks_bus(
    offline_env,  # noqa: F811 -- pytest fixture param, shadowing the re-export import above
) -> None:
    """B7 gap 2: RealRuntime.publish_attention actually emits the
    record-derived, event-id-carrying payload on the REAL hooks bus (not a
    hand-waved claim) -- proven the same way this file already proves
    delegate/tool-error events: register a real listener on the real
    coordinator, drive the method, observe what actually arrives."""
    runtime = await _started_runtime(offline_env["project"], mode="auto")
    received: list[dict] = []
    try:
        hooks = runtime._initialized.coordinator.hooks
        hooks.register(
            "attention:recorded",
            lambda _event, data: received.append(dict(data)) or None,
            priority=500,
            name="test-attention-recorded-listener",
        )
        payload = {
            "event_id": "sess-1:error:occ-1",
            "session_id": "sess-1",
            "reason": "error",
            "created_at": 123.0,
            "title": "Amplifier",
            "body": "The session hit an error",
        }
        await runtime.publish_attention(payload)

        # The hooks bus enriches every emission with its own envelope
        # fields (parent_id, timestamp) -- assert OUR payload arrived
        # intact as a subset, not byte-exact equality with the envelope.
        assert len(received) == 1
        assert payload.items() <= received[0].items()
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_publish_attention_before_start_is_a_safe_no_op() -> None:
    """Never raises even with no live session -- a destination problem must
    never block or crash the session."""
    from amplifier_app_tui.kernel.runtime import RealRuntime

    runtime = RealRuntime(bundle=None, resume_id=None, provider_override=None, model_override=None)
    await runtime.publish_attention({"event_id": "x"})  # must not raise
    assert runtime.session_dir() is None
