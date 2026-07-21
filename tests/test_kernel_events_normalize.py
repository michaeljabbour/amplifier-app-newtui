"""Contract tests for kernel/events.py normalization.

Feeds raw hook payloads — including the variant shapes documented in
RESEARCH-BRIEF §2 — and asserts the typed UIEvents that come out.
"""

from __future__ import annotations

from amplifier_app_newtui.kernel.events import (
    AgentCompleted,
    AgentResumed,
    AgentSpawned,
    ApprovalRequired,
    CancelCompleted,
    CancelRequested,
    ContentBlockEnd,
    ContextCompacted,
    ExecutionEnd,
    ExecutionStart,
    Notification,
    OrchestratorComplete,
    PromptComplete,
    PromptSubmit,
    ProviderNotice,
    ProviderResponseUsage,
    SessionFork,
    SessionStart,
    StreamAborted,
    StreamBlockDelta,
    StreamBlockEnd,
    StreamBlockStart,
    ToolError,
    ToolPost,
    ToolPre,
    normalize,
)

SID = {"session_id": "sess-1", "parent_id": None}
ROOT = "root-session"


def test_stream_block_start() -> None:
    event = normalize(
        "llm:stream_block_start",
        {**SID, "request_id": "r1", "block_index": 0, "block_type": "text"},
    )
    assert isinstance(event, StreamBlockStart)
    assert event.request_id == "r1"
    assert event.session_id == "sess-1"
    assert event.event_id  # envelope minted


def test_delta_text_key_variants() -> None:
    """Delta text arrives under delta | text | content depending on provider."""
    for key in ("delta", "text", "content"):
        event = normalize(
            "llm:stream_block_delta",
            {**SID, "request_id": "r1", "block_index": 0, "sequence": 3, key: "chunk"},
        )
        assert isinstance(event, StreamBlockDelta)
        assert event.text == "chunk", key
        assert event.sequence == 3


def test_delta_prefers_delta_key_over_others() -> None:
    event = normalize("llm:stream_block_delta", {**SID, "delta": "right", "text": "wrong"})
    assert isinstance(event, StreamBlockDelta)
    assert event.text == "right"


def test_stream_end_and_abort() -> None:
    end = normalize("llm:stream_block_end", {**SID, "request_id": "r1", "block_index": 2})
    assert isinstance(end, StreamBlockEnd)
    assert end.block_index == 2
    aborted = normalize(
        "llm:stream_aborted",
        {**SID, "request_id": "r1", "error": {"type": "overloaded", "msg": "529"}},
    )
    assert isinstance(aborted, StreamAborted)
    assert aborted.error_type == "overloaded"
    assert aborted.error_message == "529"


def test_tool_pre_keyed_by_tool_call_id() -> None:
    event = normalize(
        "tool:pre",
        {
            **SID,
            "tool_name": "bash",
            "tool_call_id": "call-7",
            "tool_input": {"command": "pytest -q"},
            "parallel_group_id": "pg-1",
        },
    )
    assert isinstance(event, ToolPre)
    assert event.tool_call_id == "call-7"
    assert event.tool_input == {"command": "pytest -q"}
    assert event.parallel_group_id == "pg-1"


def test_tool_post_result_vs_tool_response_variants() -> None:
    """Result payload arrives under result | tool_response."""
    for key in ("result", "tool_response"):
        event = normalize(
            "tool:post",
            {**SID, "tool_name": "bash", "tool_call_id": "c1", key: {"output": "ok"}},
        )
        assert isinstance(event, ToolPost)
        assert event.result == {"output": "ok"}, key


def test_tool_post_non_mapping_result_preserved() -> None:
    event = normalize(
        "tool:post", {**SID, "tool_name": "bash", "tool_call_id": "c1", "result": "done"}
    )
    assert isinstance(event, ToolPost)
    assert event.result == {"value": "done"}


def test_tool_error() -> None:
    event = normalize(
        "tool:error",
        {
            **SID,
            "tool_name": "web_fetch",
            "tool_call_id": "c9",
            "error": {"type": "Timeout", "msg": "30s"},
        },
    )
    assert isinstance(event, ToolError)
    assert event.tool_call_id == "c9"
    assert event.error_type == "Timeout"


def test_content_block_end_carries_block_and_usage() -> None:
    event = normalize(
        "content_block:end",
        {
            **SID,
            "block_type": "text",
            "block_index": 1,
            "total_blocks": 2,
            "block": {"text": "final answer"},
            "usage": {"output_tokens": 42},
        },
    )
    assert isinstance(event, ContentBlockEnd)
    assert event.block == {"text": "final answer"}
    assert event.usage == {"output_tokens": 42}


def test_content_block_end_derives_type_from_inner_block() -> None:
    for block_type in ("thinking", "tool_call"):
        event = normalize(
            "content_block:end",
            {**SID, "block": {"type": block_type}, "block_index": 0, "total_blocks": 1},
        )
        assert isinstance(event, ContentBlockEnd)
        assert event.block_type == block_type


def test_orchestrator_complete_status_validation() -> None:
    event = normalize(
        "orchestrator:complete",
        {**SID, "orchestrator": "loop-streaming", "turn_count": 4, "status": "cancelled"},
    )
    assert isinstance(event, OrchestratorComplete)
    assert event.status == "cancelled"
    weird = normalize("orchestrator:complete", {**SID, "status": "exploded"})
    assert isinstance(weird, OrchestratorComplete)
    assert weird.status == "incomplete"  # unknown statuses degrade, never crash


def test_turn_lifecycle_events() -> None:
    assert isinstance(normalize("prompt:submit", {**SID, "prompt": "hi"}), PromptSubmit)
    assert isinstance(normalize("prompt:complete", {**SID}), PromptComplete)
    assert isinstance(normalize("execution:start", {**SID}), ExecutionStart)
    assert isinstance(normalize("execution:end", {**SID}), ExecutionEnd)


def test_provider_usage_nested_and_flat() -> None:
    nested = normalize(
        "provider:response",
        {
            **SID,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 250,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 100,
            },
        },
    )
    assert isinstance(nested, ProviderResponseUsage)
    assert (nested.input_tokens, nested.output_tokens) == (1000, 250)
    assert (nested.cache_read, nested.cache_write) == (800, 100)

    flat = normalize(
        "provider:response",
        {**SID, "input_tokens": 10, "output_tokens": 5, "cache_read": 3, "cache_write": 1},
    )
    assert isinstance(flat, ProviderResponseUsage)
    assert (flat.cache_read, flat.cache_write) == (3, 1)


def test_provider_notices() -> None:
    for name, kind in (
        ("provider:error", "error"),
        ("provider:retry", "retry"),
        ("provider:throttle", "throttle"),
    ):
        event = normalize(name, {**SID, "message": "boom"})
        assert isinstance(event, ProviderNotice)
        assert event.notice == kind
        assert event.message == "boom"


def test_session_events_and_envelope_routing() -> None:
    start = normalize("session:start", {"session_id": "child-1", "parent_id": "sess-1"})
    assert isinstance(start, SessionStart)
    assert start.parent_id == "sess-1"
    fork = normalize("session:fork", {**SID, "source_session_id": "sess-0"})
    assert isinstance(fork, SessionFork)
    assert fork.source_session_id == "sess-0"


def test_approval_required_options_verbatim() -> None:
    event = normalize(
        "approval:required",
        {**SID, "prompt": "Run git push?", "options": ["Allow once", "Allow always", "Deny"]},
    )
    assert isinstance(event, ApprovalRequired)
    assert event.options == ("Allow once", "Allow always", "Deny")


def test_cancel_events() -> None:
    assert isinstance(normalize("cancel:requested", {**SID}), CancelRequested)
    assert isinstance(normalize("cancel:completed", {**SID}), CancelCompleted)


def test_agent_spawned_canonical_and_legacy_names() -> None:
    """task:agent_* is canonical; legacy task:* names normalize identically."""
    payload = {
        **SID,
        "agent": "test-writer",
        "sub_session_id": "sess-1-abc_test-writer",
        "parent_session_id": "sess-1",
    }
    for name in ("task:agent_spawned", "task:spawned"):
        event = normalize(name, payload)
        assert isinstance(event, AgentSpawned), name
        assert event.agent == "test-writer"
        assert event.sub_session_id == "sess-1-abc_test-writer"


def test_agent_completed_success_default_true() -> None:
    for name in ("task:agent_completed", "task:completed"):
        event = normalize(name, {**SID, "agent": "a", "sub_session_id": "s"})
        assert isinstance(event, AgentCompleted), name
        assert event.success is True
    failed = normalize("task:agent_completed", {**SID, "agent": "a", "success": False})
    assert isinstance(failed, AgentCompleted)
    assert failed.success is False


def test_notification() -> None:
    event = normalize("user:notification", {**SID, "message": "saved", "level": "info"})
    assert isinstance(event, Notification)
    assert event.message == "saved"


def test_context_compaction_stats_are_normalized() -> None:
    event = normalize(
        "context:compaction",
        {
            **SID,
            "before_tokens": 120_000,
            "after_tokens": 60_000,
            "before_messages": 42,
            "after_messages": 23,
            "strategy_level": 3,
        },
    )
    assert isinstance(event, ContextCompacted)
    assert event.before_tokens == 120_000
    assert event.after_tokens == 60_000
    assert event.strategy_level == 3


def test_unknown_events_return_none() -> None:
    assert normalize("context:pre_compact_unknown_thing", {**SID}) is None
    assert normalize("totally:made_up", {}) is None


def test_missing_payload_never_crashes() -> None:
    """Payload drift degrades to defaults rather than raising."""
    for name in (
        "llm:stream_block_delta",
        "tool:pre",
        "tool:post",
        "provider:response",
        "approval:required",
        "task:agent_spawned",
    ):
        event = normalize(name, None)
        assert event is not None, name


def test_delegate_agent_lifecycle_aliases() -> None:
    from amplifier_app_newtui.kernel.queue_bridge import QueueBridge

    assert "delegate:agent_spawned" in QueueBridge.EVENTS
    assert "delegate:agent_completed" in QueueBridge.EVENTS

    spawned = normalize(
        "delegate:agent_spawned",
        {
            **SID,
            "agent": "reviewer",
            "sub_session_id": "sess-1-reviewer",
            "parent_session_id": "sess-1",
        },
    )
    assert isinstance(spawned, AgentSpawned)
    assert spawned.agent == "reviewer"
    assert spawned.sub_session_id == "sess-1-reviewer"

    completed = normalize(
        "delegate:agent_completed",
        {
            **SID,
            "agent": "reviewer",
            "sub_session_id": "sess-1-reviewer",
            "parent_session_id": "sess-1",
            "success": True,
            "result": "review complete",
        },
    )
    assert isinstance(completed, AgentCompleted)
    assert completed.success
    assert completed.result == "review complete"


def test_normalize_delegate_agent_resumed() -> None:
    """Resume reopens a lane without changing parent session."""
    raw = {
        "session_id": "kid-1_worker",  # child session
        "parent_session_id": ROOT,
    }
    result = normalize("delegate:agent_resumed", raw)
    assert isinstance(result, AgentResumed)
    assert result.kind == "agent_resumed"
    assert result.session_id == "kid-1_worker"


def test_normalize_delegate_agent_cancelled() -> None:
    """Cancellation is a terminal event with explicit state."""
    raw = {
        "session_id": ROOT,
        "agent": "worker",
        "sub_session_id": "kid-1_worker",
        "parent_session_id": ROOT,
    }
    result = normalize("delegate:agent_cancelled", raw)
    assert isinstance(result, AgentCompleted)
    assert result.kind == "agent_completed"  # normalized to agent_completed
    assert result.session_id == ROOT
    assert result.result == "cancelled"
    assert result.success is False


def test_normalize_delegate_error() -> None:
    """Errors become agent_completed with error result."""
    raw = {
        "session_id": ROOT,
        "agent": "worker",
        "sub_session_id": "kid-1_worker",
        "parent_session_id": ROOT,
        "error": "boom",
    }
    result = normalize("delegate:error", raw)
    assert isinstance(result, AgentCompleted)
    assert result.kind == "agent_completed"
    assert result.result == "error"
    assert result.success is False


def test_event_ids_are_unique() -> None:
    a = normalize("execution:start", {**SID})
    b = normalize("execution:start", {**SID})
    assert a is not None and b is not None
    assert a.event_id != b.event_id


def test_events_json_roundtrip() -> None:
    """Normalized events survive events.jsonl round-trips."""
    event = normalize(
        "tool:post",
        {**SID, "tool_name": "bash", "tool_call_id": "c1", "result": {"output": "ok"}},
    )
    assert isinstance(event, ToolPost)
    restored = ToolPost.model_validate_json(event.model_dump_json())
    assert restored == event


class TestUsageFromContentBlockEnd:
    """Real runtime: usage rides on content_block:end (no provider:response)."""

    def test_synthesizes_usage_with_provider_cost(self) -> None:
        from decimal import Decimal

        from amplifier_app_newtui.kernel.cost import cost_of
        from amplifier_app_newtui.kernel.events import (
            normalize,
            usage_from_content_block_end,
        )

        block_end = normalize(
            "content_block:end",
            {
                "block_type": "text",
                "block_index": 0,
                "total_blocks": 1,
                "block": {"text": "OK", "type": "text"},
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 4,
                    "cache_read_tokens": None,
                    "cache_creation_input_tokens": 88471,
                    "cost_usd": "1.1061075",
                },
            },
        )
        usage = usage_from_content_block_end(block_end)
        assert usage is not None
        assert usage.input_tokens == 2
        assert usage.output_tokens == 4
        assert usage.cache_write == 88471
        assert usage.cost_usd == Decimal("1.1061075")
        # Provider-reported cost is authoritative over the table estimate.
        assert cost_of(usage) == Decimal("1.1061075")

    def test_no_usage_payload_returns_none(self) -> None:
        from amplifier_app_newtui.kernel.events import (
            normalize,
            usage_from_content_block_end,
        )

        block_end = normalize(
            "content_block:end",
            {"block_type": "text", "block": {"text": "hi", "type": "text"}},
        )
        assert usage_from_content_block_end(block_end) is None

    def test_bridge_emits_usage_before_block_end(self) -> None:
        import asyncio

        from amplifier_app_newtui.kernel.queue_bridge import QueueBridge

        async def run() -> list[str]:
            queue: asyncio.Queue = asyncio.Queue()
            bridge = QueueBridge(queue)
            await bridge.handle_event(
                "content_block:end",
                {
                    "block_type": "text",
                    "block": {"text": "OK", "type": "text"},
                    "usage": {"input_tokens": 2, "output_tokens": 4, "cost_usd": "0.5"},
                },
            )
            kinds = []
            while not queue.empty():
                kinds.append(queue.get_nowait().kind)
            return kinds

        assert asyncio.run(run()) == ["provider_response_usage", "content_block_end"]

    def test_bridge_emits_usage_once_for_multi_block_response(self) -> None:
        import asyncio

        from amplifier_app_newtui.kernel.queue_bridge import QueueBridge

        async def run() -> list[str]:
            queue: asyncio.Queue = asyncio.Queue()
            bridge = QueueBridge(queue)
            for index, block in enumerate(
                (
                    {"type": "thinking", "thinking": "considering"},
                    {"type": "text", "text": "Working on it."},
                    {"type": "tool_call", "name": "bash"},
                )
            ):
                await bridge.handle_event(
                    "content_block:end",
                    {
                        "block_index": index,
                        "total_blocks": 3,
                        "block": block,
                        "usage": {"input_tokens": 2, "output_tokens": 4},
                    },
                )
            kinds = []
            while not queue.empty():
                kinds.append(queue.get_nowait().kind)
            return kinds

        assert asyncio.run(run()) == [
            "content_block_end",
            "content_block_end",
            "provider_response_usage",
            "content_block_end",
        ]
