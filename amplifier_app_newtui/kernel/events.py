"""THE event contract: raw amplifier hook payloads → typed ``UIEvent``s.

All amplifier-core events are normalized at exactly this one boundary
(ADR-0007). Both channels are consumed and kept independent:

- **Channel A** (live deltas, ad-hoc provider events):
  ``llm:stream_block_start/delta/end``, ``llm:stream_aborted``.
- **Channel B** (durable records, orchestrator events): ``tool:pre/post/
  error``, ``content_block:start/end``, ``orchestrator:complete``.

Never reconstruct one channel from the other. Tool correlation is by
``tool_call_id`` only — never ``tool_name`` (parallel calls of the same
tool run concurrently).

This module is intentionally **pure**: dict in, pydantic model out. It
imports neither amplifier-core nor Textual, so the whole contract is
testable with nothing but pydantic installed. :func:`normalize` absorbs
the payload variance documented in RESEARCH-BRIEF §2:

- delta text under ``delta`` | ``text`` | ``content``;
- ``task:agent_spawned``/``task:agent_completed`` vs the legacy
  ``task:spawned``/``task:completed`` names;
- tool results under ``result`` vs ``tool_response``;
- provider usage flat or nested under ``usage``, with cache counters
  under ``cache_read_input_tokens``/``cache_read`` etc.

Every event carries the envelope ``{event_id, session_id, parent_id,
ts}``. ``session_id``/``parent_id`` come from the payload (stamped by
``hooks.set_default_fields``) and are the entire lane-routing key.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from itertools import count
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_event_counter = count(1)


def _mint_event_id() -> str:
    return f"ev{next(_event_counter)}"


class _Envelope(BaseModel):
    """Common envelope on every normalized event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=_mint_event_id)
    session_id: str = ""
    parent_id: str | None = None
    ts: float = Field(default_factory=time.time)


# --------------------------------------------------------------------------
# Channel A — live streaming deltas
# --------------------------------------------------------------------------


class StreamBlockStart(_Envelope):
    """A streaming content block opened (``llm:stream_block_start``)."""

    kind: Literal["stream_block_start"] = "stream_block_start"
    request_id: str = ""
    block_index: int = 0
    block_type: str = "text"
    name: str = ""


class StreamBlockDelta(_Envelope):
    """One incremental text/thinking chunk (``llm:stream_block_delta``).

    ``text`` is canonical regardless of which raw key (``delta`` /
    ``text`` / ``content``) the provider used.
    """

    kind: Literal["stream_block_delta"] = "stream_block_delta"
    request_id: str = ""
    block_index: int = 0
    block_type: str = "text"
    sequence: int = 0
    text: str = ""


class StreamBlockEnd(_Envelope):
    """A streaming block closed — consolidate the live tail now."""

    kind: Literal["stream_block_end"] = "stream_block_end"
    request_id: str = ""
    block_index: int = 0
    block_type: str = "text"


class StreamAborted(_Envelope):
    """The stream died mid-flight (``llm:stream_aborted``)."""

    kind: Literal["stream_aborted"] = "stream_aborted"
    request_id: str = ""
    error_type: str = ""
    error_message: str = ""


# --------------------------------------------------------------------------
# Channel B — durable tool / content records
# --------------------------------------------------------------------------


class ToolPre(_Envelope):
    """A tool call is about to run (``tool:pre``) — open the tool line."""

    kind: Literal["tool_pre"] = "tool_pre"
    tool_name: str = ""
    tool_call_id: str = ""
    tool_input: dict[str, Any] = Field(default_factory=dict)
    parallel_group_id: str | None = None


class ToolPost(_Envelope):
    """A tool call finished (``tool:post``) — finalize + expandable body.

    ``result`` is the normalized payload whether the raw event used
    ``result`` or ``tool_response``.
    """

    kind: Literal["tool_post"] = "tool_post"
    tool_name: str = ""
    tool_call_id: str = ""
    tool_input: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class ToolError(_Envelope):
    """A tool call failed (``tool:error``)."""

    kind: Literal["tool_error"] = "tool_error"
    tool_name: str = ""
    tool_call_id: str = ""
    error_type: str = ""
    error_message: str = ""


class ContentBlockStart(_Envelope):
    """Durable content block opened (``content_block:start``)."""

    kind: Literal["content_block_start"] = "content_block_start"
    block_type: str = "text"
    block_index: int = 0
    total_blocks: int = 0


class ContentBlockEnd(_Envelope):
    """Durable content block record (``content_block:end``) — the atomic,
    non-incremental source of truth for answer/thinking text."""

    kind: Literal["content_block_end"] = "content_block_end"
    block_type: str = "text"
    block_index: int = 0
    total_blocks: int = 0
    block: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)


class OrchestratorComplete(_Envelope):
    """The orchestrator loop ended (``orchestrator:complete``)."""

    kind: Literal["orchestrator_complete"] = "orchestrator_complete"
    orchestrator: str = ""
    turn_count: int = 0
    status: Literal["success", "cancelled", "incomplete"] = "success"


# --------------------------------------------------------------------------
# Turn / execution lifecycle
# --------------------------------------------------------------------------


class PromptSubmit(_Envelope):
    """A user prompt entered the engine (``prompt:submit``) — the turn
    boundary where the app stamps its monotonic turn_id."""

    kind: Literal["prompt_submit"] = "prompt_submit"
    prompt: str = ""


class PromptComplete(_Envelope):
    """The prompt's turn finished (``prompt:complete``)."""

    kind: Literal["prompt_complete"] = "prompt_complete"
    response: str = ""


class ExecutionStart(_Envelope):
    """Engine execution started (``execution:start``)."""

    kind: Literal["execution_start"] = "execution_start"


class ExecutionEnd(_Envelope):
    """Engine execution ended (``execution:end``)."""

    kind: Literal["execution_end"] = "execution_end"


# --------------------------------------------------------------------------
# Provider telemetry / notices
# --------------------------------------------------------------------------


class ProviderResponseUsage(_Envelope):
    """Token usage from one provider response (``provider:response``).

    Drives live token counting, cache %, and per-turn cost (kernel
    SessionStatus counters are NOT populated — the app computes cost from
    these numbers itself).
    """

    kind: Literal["provider_response_usage"] = "provider_response_usage"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    model: str = ""


class ProviderNotice(_Envelope):
    """Provider error/retry/throttle notice (footer transient)."""

    kind: Literal["provider_notice"] = "provider_notice"
    notice: Literal["error", "retry", "throttle"] = "error"
    message: str = ""


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------


class SessionStart(_Envelope):
    kind: Literal["session_start"] = "session_start"


class SessionEnd(_Envelope):
    kind: Literal["session_end"] = "session_end"


class SessionFork(_Envelope):
    """A session forked (rewind); ``source_session_id`` is the parent."""

    kind: Literal["session_fork"] = "session_fork"
    source_session_id: str = ""


class SessionResume(_Envelope):
    kind: Literal["session_resume"] = "session_resume"


# --------------------------------------------------------------------------
# Approvals / cancellation
# --------------------------------------------------------------------------


class ApprovalRequired(_Envelope):
    """An approval is being requested (``approval:required``).

    ``options`` always contains the verbatim strings ``Allow once`` /
    ``Allow always`` / ``Deny`` (Rust fail-closed string matching).
    """

    kind: Literal["approval_required"] = "approval_required"
    prompt: str = ""
    options: tuple[str, ...] = ()


class ApprovalGranted(_Envelope):
    kind: Literal["approval_granted"] = "approval_granted"
    prompt: str = ""
    choice: str = ""


class ApprovalDenied(_Envelope):
    """An approval was denied (``approval:denied``).

    ``command`` is the blocked thing for the ⊘ line (falls back to
    ``prompt``); ``continuation`` is the deny-and-continue note
    (DESIGN-SPEC §7: ``continuing without <thing>``).
    """

    kind: Literal["approval_denied"] = "approval_denied"
    prompt: str = ""
    reason: str = ""
    command: str = ""
    continuation: str = ""


class CancelRequested(_Envelope):
    """Interrupt requested (``cancel:requested``) — esc while running."""

    kind: Literal["cancel_requested"] = "cancel_requested"


class CancelCompleted(_Envelope):
    """Interrupt landed at a step boundary (``cancel:completed``)."""

    kind: Literal["cancel_completed"] = "cancel_completed"


# --------------------------------------------------------------------------
# Subagents / notifications
# --------------------------------------------------------------------------


class AgentSpawned(_Envelope):
    """A subagent lane opened (``task:agent_spawned`` / ``task:spawned``)."""

    kind: Literal["agent_spawned"] = "agent_spawned"
    agent: str = ""
    sub_session_id: str = ""
    parent_session_id: str = ""


class AgentCompleted(_Envelope):
    """A subagent finished (``task:agent_completed`` / ``task:completed``)."""

    kind: Literal["agent_completed"] = "agent_completed"
    agent: str = ""
    sub_session_id: str = ""
    parent_session_id: str = ""
    success: bool = True


class Notification(_Envelope):
    """User-facing notice (``user:notification``) → transient notice slot."""

    kind: Literal["notification"] = "notification"
    message: str = ""
    level: str = "info"
    source: str = ""


UIEvent = Annotated[
    StreamBlockStart
    | StreamBlockDelta
    | StreamBlockEnd
    | StreamAborted
    | ToolPre
    | ToolPost
    | ToolError
    | ContentBlockStart
    | ContentBlockEnd
    | OrchestratorComplete
    | PromptSubmit
    | PromptComplete
    | ExecutionStart
    | ExecutionEnd
    | ProviderResponseUsage
    | ProviderNotice
    | SessionStart
    | SessionEnd
    | SessionFork
    | SessionResume
    | ApprovalRequired
    | ApprovalGranted
    | ApprovalDenied
    | CancelRequested
    | CancelCompleted
    | AgentSpawned
    | AgentCompleted
    | Notification,
    Field(discriminator="kind"),
]
"""Discriminated union of every normalized UI event (on ``kind``)."""


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def _str(data: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return str(value)
    return default


def _int(data: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def _dict(data: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        if value is not None:
            # Non-mapping results (bare strings, model dumps as str) are
            # preserved rather than dropped.
            return {"value": value}
    return {}


def _error_fields(data: Mapping[str, Any]) -> tuple[str, str]:
    """Extract (type, message) from ``error`` dicts or flat keys."""
    error = data.get("error")
    if isinstance(error, Mapping):
        return (
            _str(error, "type", "error_type"),
            _str(error, "msg", "message", "error_message"),
        )
    if isinstance(error, str):
        return ("", error)
    return (_str(data, "error_type"), _str(data, "error_message", "msg", "message"))


def _envelope(data: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the common envelope fields from a raw payload."""
    fields: dict[str, Any] = {
        "session_id": _str(data, "session_id"),
        "parent_id": data.get("parent_id") or None,
    }
    event_id = _str(data, "event_id")
    if event_id:
        fields["event_id"] = event_id
    ts = data.get("ts", data.get("timestamp"))
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        fields["ts"] = float(ts)
    return fields


def _usage_source(data: Mapping[str, Any]) -> Mapping[str, Any]:
    usage = data.get("usage")
    return usage if isinstance(usage, Mapping) else data


_ORCH_STATUSES = frozenset({"success", "cancelled", "incomplete"})
_NOTICE_KINDS: dict[str, str] = {
    "provider:error": "error",
    "provider:retry": "retry",
    "provider:throttle": "throttle",
}


def normalize(event_name: str, data: Mapping[str, Any] | None) -> UIEvent | None:
    """Normalize one raw hook payload into a typed :class:`UIEvent`.

    Returns ``None`` for event names the UI does not consume — callers
    drop those silently. Never raises on missing payload keys: unknown
    shapes degrade to defaulted fields, because a rendering pipeline must
    not crash on provider payload drift.
    """
    payload: Mapping[str, Any] = data or {}
    env = _envelope(payload)

    match event_name:
        # -- Channel A -----------------------------------------------------
        case "llm:stream_block_start":
            return StreamBlockStart(
                **env,
                request_id=_str(payload, "request_id"),
                block_index=_int(payload, "block_index", "index"),
                block_type=_str(payload, "block_type", default="text"),
                name=_str(payload, "name"),
            )
        case "llm:stream_block_delta":
            return StreamBlockDelta(
                **env,
                request_id=_str(payload, "request_id"),
                block_index=_int(payload, "block_index", "index"),
                block_type=_str(payload, "block_type", default="text"),
                sequence=_int(payload, "sequence", "seq"),
                # Payload variance: delta | text | content (RESEARCH-BRIEF §2).
                text=_str(payload, "delta", "text", "content"),
            )
        case "llm:stream_block_end":
            return StreamBlockEnd(
                **env,
                request_id=_str(payload, "request_id"),
                block_index=_int(payload, "block_index", "index"),
                block_type=_str(payload, "block_type", default="text"),
            )
        case "llm:stream_aborted":
            error_type, error_message = _error_fields(payload)
            return StreamAborted(
                **env,
                request_id=_str(payload, "request_id"),
                error_type=error_type,
                error_message=error_message,
            )
        # -- Channel B -----------------------------------------------------
        case "tool:pre":
            return ToolPre(
                **env,
                tool_name=_str(payload, "tool_name", "name"),
                tool_call_id=_str(payload, "tool_call_id", "tool_use_id", "id"),
                tool_input=_dict(payload, "tool_input", "input"),
                parallel_group_id=payload.get("parallel_group_id") or None,
            )
        case "tool:post":
            return ToolPost(
                **env,
                tool_name=_str(payload, "tool_name", "name"),
                tool_call_id=_str(payload, "tool_call_id", "tool_use_id", "id"),
                tool_input=_dict(payload, "tool_input", "input"),
                # Payload variance: result | tool_response (RESEARCH-BRIEF §2).
                result=_dict(payload, "result", "tool_response", "response"),
            )
        case "tool:error":
            error_type, error_message = _error_fields(payload)
            return ToolError(
                **env,
                tool_name=_str(payload, "tool_name", "name"),
                tool_call_id=_str(payload, "tool_call_id", "tool_use_id", "id"),
                error_type=error_type,
                error_message=error_message,
            )
        case "content_block:start":
            return ContentBlockStart(
                **env,
                block_type=_str(payload, "block_type", default="text"),
                block_index=_int(payload, "block_index", "index"),
                total_blocks=_int(payload, "total_blocks"),
            )
        case "content_block:end":
            return ContentBlockEnd(
                **env,
                block_type=_str(payload, "block_type", default="text"),
                block_index=_int(payload, "block_index", "index"),
                total_blocks=_int(payload, "total_blocks"),
                block=_dict(payload, "block"),
                usage=_dict(payload, "usage"),
            )
        case "orchestrator:complete":
            status = _str(payload, "status", default="success")
            return OrchestratorComplete(
                **env,
                orchestrator=_str(payload, "orchestrator"),
                turn_count=_int(payload, "turn_count"),
                status=status if status in _ORCH_STATUSES else "incomplete",  # type: ignore[arg-type]
            )
        # -- Turn lifecycle --------------------------------------------------
        case "prompt:submit":
            return PromptSubmit(**env, prompt=_str(payload, "prompt", "text"))
        case "prompt:complete":
            return PromptComplete(**env, response=_str(payload, "response"))
        case "execution:start":
            return ExecutionStart(**env)
        case "execution:end":
            return ExecutionEnd(**env)
        # -- Provider ----------------------------------------------------------
        case "provider:response":
            usage = _usage_source(payload)
            return ProviderResponseUsage(
                **env,
                input_tokens=_int(usage, "input_tokens", "prompt_tokens"),
                output_tokens=_int(usage, "output_tokens", "completion_tokens"),
                cache_read=_int(
                    usage, "cache_read", "cache_read_input_tokens", "cache_read_tokens"
                ),
                cache_write=_int(
                    usage,
                    "cache_write",
                    "cache_creation_input_tokens",
                    "cache_write_tokens",
                ),
                model=_str(payload, "model"),
            )
        case "provider:error" | "provider:retry" | "provider:throttle":
            _, message = _error_fields(payload)
            return ProviderNotice(
                **env,
                notice=_NOTICE_KINDS[event_name],  # type: ignore[arg-type]
                message=message or _str(payload, "message", "reason"),
            )
        # -- Session lifecycle -------------------------------------------------
        case "session:start":
            return SessionStart(**env)
        case "session:end":
            return SessionEnd(**env)
        case "session:fork":
            return SessionFork(
                **env,
                source_session_id=_str(payload, "source_session_id", "parent_session_id"),
            )
        case "session:resume":
            return SessionResume(**env)
        # -- Approvals / cancel --------------------------------------------------
        case "approval:required":
            raw_options = payload.get("options")
            options = (
                tuple(str(option) for option in raw_options)
                if isinstance(raw_options, (list, tuple))
                else ()
            )
            return ApprovalRequired(
                **env, prompt=_str(payload, "prompt", "message"), options=options
            )
        case "approval:granted":
            return ApprovalGranted(
                **env,
                prompt=_str(payload, "prompt", "message"),
                choice=_str(payload, "choice", "option", "response"),
            )
        case "approval:denied":
            return ApprovalDenied(
                **env,
                prompt=_str(payload, "prompt", "message"),
                reason=_str(payload, "reason"),
                command=_str(payload, "command"),
                continuation=_str(payload, "continuation"),
            )
        case "cancel:requested":
            return CancelRequested(**env)
        case "cancel:completed":
            return CancelCompleted(**env)
        # -- Subagents (task:agent_* canonical; task:* legacy) --------------------
        case "task:agent_spawned" | "task:spawned":
            return AgentSpawned(
                **env,
                agent=_str(payload, "agent", "agent_name", "name"),
                sub_session_id=_str(payload, "sub_session_id", "child_session_id"),
                parent_session_id=_str(payload, "parent_session_id"),
            )
        case "task:agent_completed" | "task:completed":
            success = payload.get("success")
            return AgentCompleted(
                **env,
                agent=_str(payload, "agent", "agent_name", "name"),
                sub_session_id=_str(payload, "sub_session_id", "child_session_id"),
                parent_session_id=_str(payload, "parent_session_id"),
                success=True if success is None else bool(success),
            )
        case "user:notification":
            return Notification(
                **env,
                message=_str(payload, "message", "text"),
                level=_str(payload, "level", default="info"),
                source=_str(payload, "source"),
            )
        case _:
            return None


__all__ = [
    "AgentCompleted",
    "AgentSpawned",
    "ApprovalDenied",
    "ApprovalGranted",
    "ApprovalRequired",
    "CancelCompleted",
    "CancelRequested",
    "ContentBlockEnd",
    "ContentBlockStart",
    "ExecutionEnd",
    "ExecutionStart",
    "Notification",
    "OrchestratorComplete",
    "PromptComplete",
    "PromptSubmit",
    "ProviderNotice",
    "ProviderResponseUsage",
    "SessionEnd",
    "SessionFork",
    "SessionResume",
    "SessionStart",
    "StreamAborted",
    "StreamBlockDelta",
    "StreamBlockEnd",
    "StreamBlockStart",
    "ToolError",
    "ToolPost",
    "ToolPre",
    "UIEvent",
    "normalize",
]
