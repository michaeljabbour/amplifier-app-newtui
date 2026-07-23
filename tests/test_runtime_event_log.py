"""ui-events.jsonl write-path filtering (issue #27, audit-2026-07).

The hottest path is ``RealRuntime._tap``: it runs on *every* normalized
UIEvent, including a per-token ``stream_block_delta``. Persisting each one
turned the append-only log into a per-token open/write/close with
unbounded growth, even though nothing that resume or cost re-seed reads is
a Channel A stream kind (they render from Channel B's durable records).

These tests pin the write-side contract: Channel A stream kinds are
skipped at write time, everything resume/cost re-seed reads still lands on
disk, and log growth is bounded by durable-event count, not token count.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from amplifier_app_newtui.kernel.cost import CostTracker, restore_session_cost
from amplifier_app_newtui.kernel.events import (
    ContentBlockEnd,
    ProviderResponseUsage,
    StreamAborted,
    StreamBlockDelta,
    StreamBlockEnd,
    StreamBlockStart,
    ToolPost,
)
from amplifier_app_newtui.kernel.persistence import SessionStore
from amplifier_app_newtui.kernel.runtime import RealRuntime


def _runtime_with_store(tmp_path: Path, session_id: str = "s1") -> RealRuntime:
    """A RealRuntime wired only far enough to exercise ``_tap``.

    ``_tap`` needs a ``SessionStore`` and a session identity; nothing else
    is touched for the write path, so we skip the full ``start()`` boot.
    """
    runtime = RealRuntime()
    runtime._store = SessionStore(base_dir=tmp_path)
    runtime._initialized = SimpleNamespace(session_id=session_id)  # type: ignore[assignment]
    return runtime


def _stream_burst(session_id: str, tokens: int) -> list[object]:
    """A full Channel A burst: start, N per-token deltas, end, an abort."""
    events: list[object] = [StreamBlockStart(session_id=session_id, request_id="r1")]
    events += [
        StreamBlockDelta(session_id=session_id, request_id="r1", sequence=i, text=f"tok{i}")
        for i in range(tokens)
    ]
    events.append(StreamBlockEnd(session_id=session_id, request_id="r1"))
    events.append(StreamAborted(session_id=session_id, request_id="r1"))
    return events


def test_tap_skips_channel_a_stream_kinds(tmp_path: Path) -> None:
    """Stream start/delta/end/aborted never reach the log; Channel B does."""
    runtime = _runtime_with_store(tmp_path)
    store = runtime._store
    assert store is not None

    for event in _stream_burst("s1", tokens=50):
        runtime._tap(event)  # type: ignore[arg-type]
    runtime._tap(ProviderResponseUsage(session_id="s1", input_tokens=12, output_tokens=7))
    runtime._tap(
        ContentBlockEnd(
            session_id="s1",
            block_index=0,
            total_blocks=1,
            block={"type": "text", "text": "done"},
        )
    )
    runtime._tap(ToolPost(session_id="s1", tool_name="write_file", tool_call_id="c1"))

    kinds = [record["kind"] for record in store.read_events("s1")]
    assert kinds == ["provider_response_usage", "content_block_end", "tool_post"]
    assert not any(kind.startswith("stream_") for kind in kinds)


def test_tap_log_growth_is_bounded_by_durable_events_not_tokens(tmp_path: Path) -> None:
    """A long streamed answer must not grow the log per token.

    Two turns stream wildly different token counts yet persist the same
    single durable record each — proving growth tracks Channel B, not the
    number of deltas (the audit's file-growth concern).
    """
    runtime = _runtime_with_store(tmp_path)
    store = runtime._store
    assert store is not None

    for event in _stream_burst("s1", tokens=200):
        runtime._tap(event)  # type: ignore[arg-type]
    runtime._tap(ContentBlockEnd(session_id="s1", block={"type": "text", "text": "a"}))
    after_short = len(store.events_path("s1").read_text(encoding="utf-8").splitlines())

    for event in _stream_burst("s1", tokens=5000):
        runtime._tap(event)  # type: ignore[arg-type]
    runtime._tap(ContentBlockEnd(session_id="s1", block={"type": "text", "text": "b"}))
    after_long = len(store.events_path("s1").read_text(encoding="utf-8").splitlines())

    # 25x the tokens, but exactly one more durable line — not 5000 more.
    assert after_short == 1
    assert after_long == 2


def test_tap_keeps_cost_reseed_source_on_disk(tmp_path: Path) -> None:
    """The honesty contract: usage the cost re-seed reads still persists.

    Deltas are dropped, but the ``provider_response_usage`` the resume cost
    re-seed replays must land — otherwise restored spend would silently
    reset to zero.
    """
    runtime = _runtime_with_store(tmp_path)
    store = runtime._store
    assert store is not None

    for event in _stream_burst("s1", tokens=100):
        runtime._tap(event)  # type: ignore[arg-type]
    runtime._tap(
        ProviderResponseUsage(session_id="s1", model="claude-sonnet-4", cost_usd=Decimal("0.42"))
    )

    tracker = CostTracker()
    restored = restore_session_cost(tracker, *store.events_read_paths("s1"))
    assert restored == Decimal("0.42")
    assert tracker.session_cost == Decimal("0.42")
