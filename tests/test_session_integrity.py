"""Resume-time repair for interrupted tool sequences."""

from __future__ import annotations

from amplifier_app_tui.kernel.session_integrity import complete_orphaned_tool_results


def test_completes_content_and_top_level_orphans_immediately_after_assistant() -> None:
    messages = [
        {"role": "user", "content": "work in parallel"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Starting."},
                {"type": "tool_call", "id": "call-a", "name": "bash", "input": {}},
                {"type": "tool_use", "id": "call-b", "name": "delegate", "input": {}},
            ],
            "tool_calls": [
                {
                    "id": "call-c",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {"id": "call-d", "tool": "todo", "arguments": {}},
            ],
        },
        {"role": "user", "content": "resume"},
    ]

    repaired, repairs = complete_orphaned_tool_results(messages)

    assert [(row.tool_call_id, row.tool_name) for row in repairs] == [
        ("call-c", "read_file"),
        ("call-d", "todo"),
        ("call-a", "bash"),
        ("call-b", "delegate"),
    ]
    assert [message["role"] for message in repaired] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "tool",
        "tool",
        "user",
    ]
    assert {message.get("tool_call_id") for message in repaired} >= {
        "call-a",
        "call-b",
        "call-c",
        "call-d",
    }
    assert all(
        "may have executed" in str(message.get("content"))
        for message in repaired
        if message.get("role") == "tool"
    )


def test_preserves_real_results_and_repairs_only_the_missing_parallel_call() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_call", "id": "done", "name": "bash", "input": {}},
                {"type": "tool_call", "id": "missing", "name": "delegate", "input": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "done", "name": "bash", "content": "ok"},
    ]

    repaired, repairs = complete_orphaned_tool_results(messages)

    assert [row.tool_call_id for row in repairs] == ["missing"]
    assert sum(message.get("tool_call_id") == "done" for message in repaired) == 1
    assert sum(message.get("tool_call_id") == "missing" for message in repaired) == 1


def test_tool_result_content_block_counts_as_real_and_repair_is_idempotent() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "existing", "name": "bash", "input": {}},
                {"type": "tool_use", "id": "orphan", "name": "delegate", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "existing", "content": "ok"}],
        },
    ]

    first, repairs = complete_orphaned_tool_results(messages)
    second, repeated = complete_orphaned_tool_results(first)

    assert [row.tool_call_id for row in repairs] == ["orphan"]
    assert repeated == ()
    assert second == first
