"""Provider-valid transcript repair at the resume boundary.

A session can end after an assistant emitted tool calls but before their
results were durably recorded.  Anthropic rejects that transcript shape.  A
provider may patch one request in memory, but the repair disappears on the
next request unless the application owns and persists it.  Resume therefore
completes only genuinely orphaned calls before the restored context is mounted.

The synthetic result never claims whether execution happened.  It tells the
next model to inspect real state before retrying, which is the only safe answer
when interruption races an external side effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolResultRepair:
    """One interrupted tool call completed with an uncertainty placeholder."""

    tool_call_id: str
    tool_name: str


def _tool_calls(message: dict[str, Any]) -> tuple[ToolResultRepair, ...]:
    if message.get("role") != "assistant":
        return ()
    calls: list[ToolResultRepair] = []
    top_level = message.get("tool_calls")
    if isinstance(top_level, list):
        for raw in top_level:
            if not isinstance(raw, dict):
                continue
            call_id = str(raw.get("id") or "").strip()
            function = raw.get("function")
            function_name = function.get("name") if isinstance(function, dict) else ""
            name = str(
                raw.get("name") or raw.get("tool") or function_name or "unknown tool"
            ).strip()
            if call_id:
                calls.append(ToolResultRepair(call_id, name))

    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {
                "tool_call",
                "tool_use",
            }:
                continue
            call_id = str(block.get("id") or "").strip()
            name = str(block.get("name") or "unknown tool").strip()
            if call_id:
                calls.append(ToolResultRepair(call_id, name))
    return tuple(calls)


def _tool_result_ids(message: dict[str, Any]) -> set[str]:
    results: set[str] = set()
    if message.get("role") == "tool":
        call_id = str(message.get("tool_call_id") or "").strip()
        if call_id:
            results.add(call_id)
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or block.get("tool_call_id") or "").strip()
            if call_id:
                results.add(call_id)
    return results


def _synthetic_result(repair: ToolResultRepair) -> dict[str, Any]:
    return {
        "role": "tool",
        "content": (
            "[SYSTEM RECOVERY: This tool result was not durably recorded before "
            "the previous session ended. The tool may have executed. Inspect the "
            "actual external or disk state before retrying.]"
        ),
        "tool_call_id": repair.tool_call_id,
        "name": repair.tool_name,
    }


def complete_orphaned_tool_results(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[ToolResultRepair, ...]]:
    """Insert uncertainty-preserving results for every unmatched tool call.

    Existing results win even when they appear after the assistant message;
    this function never duplicates or replaces real output.  New placeholders
    are inserted immediately after the assistant message that made the calls,
    satisfying provider ordering requirements for parallel calls.
    """

    result_ids: set[str] = set()
    for message in messages:
        result_ids.update(_tool_result_ids(message))

    repaired: list[dict[str, Any]] = []
    repairs: list[ToolResultRepair] = []
    claimed: set[str] = set(result_ids)
    for message in messages:
        repaired.append(message)
        for call in _tool_calls(message):
            if call.tool_call_id in claimed:
                continue
            claimed.add(call.tool_call_id)
            repairs.append(call)
            repaired.append(_synthetic_result(call))
    return repaired, tuple(repairs)


__all__ = [
    "ToolResultRepair",
    "complete_orphaned_tool_results",
]
