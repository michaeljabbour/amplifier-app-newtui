"""/context usage math: percentages, bar apportionment, labels."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.commands.context import (
    ContextUsage,
    build_context_block,
    format_tokens,
    usage_segments,
)


def test_format_tokens() -> None:
    assert format_tokens(742) == "742"
    assert format_tokens(4_100) == "4.1k"
    assert format_tokens(8_000) == "8k"
    assert format_tokens(52_000) == "52k"
    assert format_tokens(118_000) == "118k"
    assert format_tokens(200_000) == "200k"
    assert format_tokens(1_200_000) == "1.2m"


def test_usage_accounting() -> None:
    usage = ContextUsage(conversation=52_000, tools=18_000, memory=8_000)
    assert usage.used == 78_000
    assert usage.free == 122_000
    assert usage.used_pct == 39
    assert usage.window_label == "200k"
    assert usage.header_text() == "Context  39% of 200k"


def test_usage_rejects_overflow() -> None:
    with pytest.raises(ValueError):
        ContextUsage(conversation=150_000, tools=60_000, memory=0)


def test_segments_sum_to_bar_width_and_keep_order() -> None:
    usage = ContextUsage(conversation=52_000, tools=18_000, memory=8_000)
    segments = usage_segments(usage, bar_width=20)
    assert sum(cells for _, cells in segments) == 20
    assert [label.split()[0] for label, _ in segments] == [
        "conversation",
        "tools",
        "memory",
        "free",
    ]
    # Non-zero buckets never vanish from the bar.
    assert all(cells >= 1 for _, cells in segments)


def test_tiny_bucket_keeps_a_cell() -> None:
    usage = ContextUsage(conversation=180_000, tools=100, memory=100)
    segments = usage_segments(usage, bar_width=10)
    assert sum(cells for _, cells in segments) == 10
    by_label = {label.split()[0]: cells for label, cells in segments}
    assert by_label["tools"] >= 1
    assert by_label["memory"] >= 1


def test_empty_usage_is_all_free() -> None:
    usage = ContextUsage()
    segments = usage_segments(usage, bar_width=10)
    assert segments[-1] == ("free 200k", 10)
    assert usage.used_pct == 0


def test_build_context_block() -> None:
    usage = ContextUsage(conversation=52_000, tools=18_000, memory=8_000)
    block = build_context_block("b7", usage)
    assert block.id == "b7"
    assert block.kind == "context"
    assert block.used_pct == 39
    assert block.window_label == "200k"
    assert block.bar_width == 20
    assert sum(cells for _, cells in block.segments) == 20
    assert block.segments[0][0] == "conversation 52k"
    assert block.segments[-1][0] == "free 122k"
