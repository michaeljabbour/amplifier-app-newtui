"""Tests for kernel/rewind.py — checkpoint forking, confirm-then-trim.

The file-based fork tests run foundation's real ``fork_session`` against
tmp session directories (pure file I/O — offline, no API keys).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_newtui.kernel.rewind import (
    ForkOutcome,
    RewindController,
    RewindError,
)
from amplifier_app_newtui.model.turn import (
    OutcomeLedger,
    TurnOutcome,
    TurnTelemetry,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_ledger(turn_ids: list[int]) -> OutcomeLedger:
    ledger = OutcomeLedger()
    for i, turn_id in enumerate(turn_ids, start=1):
        ledger.record_turn(
            TurnTelemetry(secs=2.0, tokens_down=100, cost=Decimal("0.01")),
            TurnOutcome(kind="answer"),
            turn_id=turn_id,
            message_index=i * 2,
            label=f"turn {turn_id}",
        )
    return ledger


def make_session_dir(tmp_path: Path, turns: int = 3) -> Path:
    session_dir = tmp_path / "sessions" / "parent-session"
    session_dir.mkdir(parents=True)
    lines = []
    for n in range(1, turns + 1):
        lines.append(json.dumps({"role": "user", "content": f"turn {n}"}))
        lines.append(json.dumps({"role": "assistant", "content": f"answer {n}"}))
    (session_dir / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (session_dir / "metadata.json").write_text(
        json.dumps({"session_id": "parent-session", "bundle": "newtui", "model": "claude"}),
        encoding="utf-8",
    )
    return session_dir


# --------------------------------------------------------------------------
# Checkpoint picker
# --------------------------------------------------------------------------


def test_checkpoints_come_from_ledger() -> None:
    controller = RewindController(make_ledger([1, 2, 3]))
    ids = [cp.id for cp in controller.checkpoints]
    assert ids == ["t1", "t2", "t3"]
    assert controller.resolve("t2").turn_id == 2
    # resolve accepts the checkpoint object too
    checkpoint = controller.checkpoints[0]
    assert controller.resolve(checkpoint).id == "t1"


def test_resolve_unknown_checkpoint_raises() -> None:
    controller = RewindController(make_ledger([1]))
    with pytest.raises(RewindError, match="unknown checkpoint"):
        controller.resolve("t9")


# --------------------------------------------------------------------------
# File-based fork (real foundation fork_session)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_from_forks_and_trims_after_confirm(tmp_path: Path) -> None:
    session_dir = make_session_dir(tmp_path, turns=3)
    ledger = make_ledger([1, 2, 3])
    controller = RewindController(ledger, session_dir=session_dir)

    outcome = await controller.fork_from("t2")

    assert isinstance(outcome, ForkOutcome)
    assert outcome.checkpoint_id == "t2"
    assert outcome.forked_from_turn == 2
    assert outcome.message_count == 4  # 2 turns × (user + assistant)
    assert not outcome.in_memory

    # backend fork really happened
    assert outcome.session_dir is not None and outcome.session_dir.exists()
    forked_lines = (
        (outcome.session_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert len(forked_lines) == 4
    forked_metadata = json.loads(
        (outcome.session_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert forked_metadata["parent_id"] == "parent-session"
    assert forked_metadata["forked_from_turn"] == 2

    # confirm-then-trim: ledger trimmed only after the backend confirmed
    assert ledger.turn_count == 2
    assert [cp.id for cp in controller.checkpoints] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_fork_from_failure_leaves_ledger_untouched(tmp_path: Path) -> None:
    session_dir = make_session_dir(tmp_path, turns=2)
    ledger = make_ledger([1, 99])  # t2 points at a turn the store doesn't have
    controller = RewindController(ledger, session_dir=session_dir)

    with pytest.raises(RewindError, match="t2"):
        await controller.fork_from("t2")
    assert ledger.turn_count == 2  # NOTHING trimmed on failure


@pytest.mark.asyncio
async def test_fork_from_requires_session_dir() -> None:
    controller = RewindController(make_ledger([1]))
    with pytest.raises(RewindError, match="session_dir"):
        await controller.fork_from("t1")


@pytest.mark.asyncio
async def test_fork_from_orphaned_tools_completed(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "p"
    session_dir.mkdir(parents=True)
    messages = [
        {"role": "user", "content": "turn 1"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tc1", "name": "bash", "input": {"cmd": "ls"}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc1"}]},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "done"},
    ]
    (session_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(m) for m in messages) + "\n", encoding="utf-8"
    )
    (session_dir / "metadata.json").write_text(json.dumps({"session_id": "p"}), encoding="utf-8")
    ledger = make_ledger([1, 2])
    controller = RewindController(ledger, session_dir=session_dir)

    outcome = await controller.fork_from("t1")
    # slicing at turn 1 cuts before the tool_result; the fork must remain
    # provider-valid (handle_orphaned_tools="complete")
    forked = [
        json.loads(line)
        for line in (outcome.session_dir / "transcript.jsonl")  # type: ignore[union-attr]
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    tool_use_ids = {
        block["id"]
        for message in forked
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    tool_result_ids = {
        message["tool_call_id"]
        for message in forked
        if message.get("role") == "tool" and "tool_call_id" in message
    } | {
        block["tool_use_id"]
        for message in forked
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    assert tool_use_ids <= tool_result_ids  # no orphaned tool_use survives


# --------------------------------------------------------------------------
# In-memory fork (live context rewind)
# --------------------------------------------------------------------------


def live_messages(turns: int = 3) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for n in range(1, turns + 1):
        messages.append({"role": "user", "content": f"turn {n}"})
        messages.append({"role": "assistant", "content": f"answer {n}"})
    return messages


@pytest.mark.asyncio
async def test_fork_in_memory_sets_messages_then_trims() -> None:
    ledger = make_ledger([1, 2, 3])
    controller = RewindController(ledger)
    restored: list[list[dict[str, Any]]] = []

    async def set_messages(messages: list[dict[str, Any]]) -> None:
        restored.append(messages)

    outcome = await controller.fork_in_memory(
        "t1", messages=live_messages(3), set_messages=set_messages, parent_id="parent"
    )

    assert outcome.in_memory
    assert outcome.session_dir is None
    assert outcome.forked_from_turn == 1
    assert restored == [
        [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "answer 1"},
        ]
    ]
    assert ledger.turn_count == 1


@pytest.mark.asyncio
async def test_fork_in_memory_context_failure_leaves_ledger() -> None:
    ledger = make_ledger([1, 2])
    controller = RewindController(ledger)

    async def set_messages(messages: list[dict[str, Any]]) -> None:
        raise RuntimeError("context rejected restore")

    with pytest.raises(RewindError, match="context restore"):
        await controller.fork_in_memory("t1", messages=live_messages(2), set_messages=set_messages)
    assert ledger.turn_count == 2  # confirm-then-trim: no trim on failure


@pytest.mark.asyncio
async def test_real_runtime_fork_rewinds_live_context_confirm_then_trim() -> None:
    """RealRuntime.fork: in-memory fork + context.set_messages(), then trim."""
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    class FakeContext:
        def __init__(self) -> None:
            self.messages = live_messages(3)

        async def get_messages(self) -> list[dict[str, Any]]:
            return list(self.messages)

        async def set_messages(self, messages: list[dict[str, Any]]) -> None:
            self.messages = list(messages)

    class FakeCoordinator:
        def __init__(self, context: FakeContext) -> None:
            self._context = context

        def get(self, name: str) -> Any:
            return self._context if name == "context" else None

    class FakeInitialized:
        def __init__(self, context: FakeContext) -> None:
            self.session_id = "live-session"
            self.coordinator = FakeCoordinator(context)

    runtime = RealRuntime()
    context = FakeContext()
    ledger = make_ledger([1, 2, 3])

    with pytest.raises(RewindError, match="not completed"):
        await runtime.fork("t1", ledger)  # no session yet → nothing trimmed
    assert ledger.turn_count == 3

    runtime._initialized = FakeInitialized(context)  # type: ignore[assignment]
    outcome = await runtime.fork("t1", ledger)

    assert outcome.in_memory and outcome.forked_from_turn == 1
    # The live context really rewound: only turn 1 survives.
    assert context.messages == [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "answer 1"},
    ]
    # …and the ledger trimmed only after the context confirmed.
    assert [cp.id for cp in ledger.checkpoints] == ["t1"]


@pytest.mark.asyncio
async def test_real_runtime_fork_refuses_while_turn_executing() -> None:
    """s9 guard: ``context.set_messages()`` under a live provider loop
    corrupts turn numbering — RealRuntime.fork must refuse while a
    submit() turn is executing, leaving ledger and context untouched."""
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    class FakeContext:
        def __init__(self) -> None:
            self.messages = live_messages(3)

        async def get_messages(self) -> list[dict[str, Any]]:
            return list(self.messages)

        async def set_messages(self, messages: list[dict[str, Any]]) -> None:
            self.messages = list(messages)

    class FakeCoordinator:
        def __init__(self, context: FakeContext) -> None:
            self._context = context

        def get(self, name: str) -> Any:
            return self._context if name == "context" else None

    class FakeInitialized:
        def __init__(self, context: FakeContext) -> None:
            self.session_id = "live-session"
            self.coordinator = FakeCoordinator(context)

    runtime = RealRuntime()
    context = FakeContext()
    ledger = make_ledger([1, 2, 3])
    runtime._initialized = FakeInitialized(context)  # type: ignore[assignment]

    runtime._executing = True  # a submit() turn is live
    with pytest.raises(RewindError, match="turn still running"):
        await runtime.fork("t1", ledger)
    assert ledger.turn_count == 3  # confirm-then-trim: nothing trimmed
    assert len(context.messages) == 6  # live context untouched

    runtime._executing = False  # turn closed out → the fork proceeds
    outcome = await runtime.fork("t1", ledger)
    assert outcome.in_memory and outcome.forked_from_turn == 1
    assert [cp.id for cp in ledger.checkpoints] == ["t1"]


@pytest.mark.asyncio
async def test_injected_fork_fn_receives_contract_arguments(tmp_path: Path) -> None:
    """The fork seam passes exactly the ADR-0007 contract arguments."""
    calls: dict[str, Any] = {}

    class FakeResult:
        session_id = "forked-id"
        session_dir = tmp_path / "forked"
        forked_from_turn = 2
        message_count = 4

    def fake_fork(parent_dir: Path, *, turn: int, handle_orphaned_tools: str) -> FakeResult:
        calls.update(parent_dir=parent_dir, turn=turn, orphans=handle_orphaned_tools)
        return FakeResult()

    ledger = make_ledger([1, 2])
    controller = RewindController(ledger, session_dir=tmp_path / "parent", fork_fn=fake_fork)
    outcome = await controller.fork_from("t2")

    assert calls == {
        "parent_dir": tmp_path / "parent",
        "turn": 2,
        "orphans": "complete",
    }
    assert outcome.session_id == "forked-id"
    assert ledger.turn_count == 2  # t2 itself survives the trim
