"""Contract tests for kernel/events.py normalization.

Feeds raw hook payloads — including the variant shapes documented in
RESEARCH-BRIEF §2 — and asserts the typed UIEvents that come out.
"""

from __future__ import annotations

from amplifier_app_newtui.kernel.events import (
    AgentCompleted,
    AgentSpawned,
    ApprovalRequired,
    CancelCompleted,
    CancelRequested,
    ContentBlockEnd,
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
    event = normalize(
        "llm:stream_block_delta", {**SID, "delta": "right", "text": "wrong"}
    )
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
        {**SID, "tool_name": "web_fetch", "tool_call_id": "c9", "error": {"type": "Timeout", "msg": "30s"}},
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
