"""Tests for kernel/rewind.py — checkpoint forking, confirm-then-trim.

The file-based fork tests run foundation's real ``fork_session`` against
tmp session directories (pure file I/O — offline, no API keys).
"""

from __future__ import annotations

import json
import base64
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_tui.kernel.rewind import (
    ForkOutcome,
    RewindController,
    RewindError,
)
from amplifier_app_tui.model.turn import (
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
        json.dumps({"session_id": "parent-session", "bundle": "tui", "model": "claude"}),
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
async def test_restore_before_prompt_uses_prior_turn_and_drops_selected_checkpoint() -> None:
    ledger = OutcomeLedger()
    for turn_id in (1, 2, 3):
        ledger.record_turn(
            TurnTelemetry(secs=1, tokens_down=1),
            TurnOutcome(kind="answer"),
            turn_id=turn_id,
            restore_turn_id=turn_id - 1,
            message_index=turn_id,
            label=f"prompt {turn_id}",
        )
    restored: list[dict[str, Any]] = []

    async def set_messages(messages: list[dict[str, Any]]) -> None:
        restored[:] = messages

    outcome = await RewindController(ledger).restore_before_in_memory(
        "t2", messages=live_messages(3), set_messages=set_messages, parent_id="parent"
    )

    assert outcome.forked_from_turn == 1
    assert restored == live_messages(1)
    assert [checkpoint.id for checkpoint in ledger.checkpoints] == ["t1"]


@pytest.mark.asyncio
async def test_restore_before_first_prompt_commits_empty_context() -> None:
    ledger = OutcomeLedger()
    ledger.record_turn(
        TurnTelemetry(secs=1, tokens_down=1),
        TurnOutcome(kind="answer"),
        turn_id=1,
        restore_turn_id=0,
        message_index=1,
        label="first prompt",
    )
    restored: list[list[dict[str, Any]]] = []

    async def set_messages(messages: list[dict[str, Any]]) -> None:
        restored.append(messages)

    outcome = await RewindController(ledger).restore_before_in_memory(
        "t1", messages=live_messages(1), set_messages=set_messages
    )

    assert outcome.forked_from_turn == 0
    assert outcome.message_count == 0
    assert restored == [[]]
    assert ledger.checkpoints == ()


@pytest.mark.asyncio
async def test_restore_before_context_failure_leaves_selected_and_later_turns() -> None:
    ledger = make_ledger([1, 2])

    async def reject(messages: list[dict[str, Any]]) -> None:
        raise RuntimeError("nope")

    with pytest.raises(RewindError, match="context restore"):
        await RewindController(ledger).restore_before_in_memory(
            "t1", messages=live_messages(2), set_messages=reject
        )
    assert [checkpoint.id for checkpoint in ledger.checkpoints] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_real_runtime_fork_rewinds_live_context_confirm_then_trim() -> None:
    """RealRuntime.fork: in-memory fork + context.set_messages(), then trim."""
    from amplifier_app_tui.kernel.runtime import RealRuntime

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
    from amplifier_app_tui.kernel.runtime import RealRuntime

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
async def test_real_runtime_restore_both_applies_conversation_then_code_before_trim() -> None:
    """Combined restore keeps the ledger until both state surfaces commit."""
    from amplifier_app_tui.kernel.checkpoints import WorkspaceRestoreOutcome
    from amplifier_app_tui.kernel.runtime import RealRuntime

    order: list[str] = []

    class OrderedContext:
        def __init__(self) -> None:
            self.messages = live_messages(2)

        async def get_messages(self) -> list[dict[str, Any]]:
            return list(self.messages)

        async def set_messages(self, messages: list[dict[str, Any]]) -> None:
            order.append("conversation")
            self.messages = list(messages)

    class FakeCheckpointStore:
        def restore(
            self,
            checkpoint_id: str,
            *,
            include_target: bool = True,
            retain_target: bool = False,
        ):
            assert checkpoint_id == "workspace-2"
            assert include_target is True
            assert retain_target is False
            order.append("code")
            return WorkspaceRestoreOutcome(checkpoint_id, restored_paths=("src/app.py",))

    class Coordinator:
        def __init__(self, context: OrderedContext) -> None:
            self._context = context

        def get(self, name: str) -> Any:
            return self._context if name == "context" else None

    class Initialized:
        def __init__(self, context: OrderedContext) -> None:
            self.session_id = "live-session"
            self.coordinator = Coordinator(context)

    context = OrderedContext()
    runtime = RealRuntime()
    runtime._initialized = Initialized(context)  # type: ignore[assignment]
    runtime._checkpoint_store = FakeCheckpointStore()
    ledger = OutcomeLedger()
    ledger.record_turn(
        TurnTelemetry(secs=1, tokens_down=1),
        TurnOutcome(kind="answer"),
        turn_id=1,
        restore_turn_id=0,
        message_index=1,
        label="first",
        workspace_id="workspace-1",
    )
    ledger.record_turn(
        TurnTelemetry(secs=1, tokens_down=1),
        TurnOutcome(kind="shipped", files_changed=1),
        turn_id=2,
        restore_turn_id=1,
        message_index=2,
        label="second",
        workspace_id="workspace-2",
    )

    outcome = await runtime.restore_checkpoint("t2", ledger, scope="both")

    assert order == ["conversation", "code"]
    assert context.messages == live_messages(1)
    assert [checkpoint.id for checkpoint in ledger.checkpoints] == ["t1"]
    assert outcome.summary == "restored 1 file · conversation before turn 2"
    assert outcome.code_status == "restored"
    assert outcome.conversation_restored is True
    assert outcome.partial is False


@pytest.mark.asyncio
async def test_combined_code_failure_rolls_conversation_back_and_keeps_ledger() -> None:
    from amplifier_app_tui.kernel.runtime import RealRuntime

    order: list[str] = []

    class RollbackContext:
        def __init__(self) -> None:
            self.messages = live_messages(2)

        async def get_messages(self) -> list[dict[str, Any]]:
            return list(self.messages)

        async def set_messages(self, messages: list[dict[str, Any]]) -> None:
            order.append("conversation")
            self.messages = list(messages)

    class FailingCheckpointStore:
        def restore(
            self,
            checkpoint_id: str,
            *,
            include_target: bool = True,
            retain_target: bool = False,
        ) -> None:
            del checkpoint_id, include_target, retain_target
            order.append("code")
            raise OSError("checkpoint disk unavailable")

    class Coordinator:
        def __init__(self, context: RollbackContext) -> None:
            self._context = context

        def get(self, name: str) -> Any:
            return self._context if name == "context" else None

    class Initialized:
        def __init__(self, context: RollbackContext) -> None:
            self.session_id = "live-session"
            self.coordinator = Coordinator(context)

    context = RollbackContext()
    original = list(context.messages)
    runtime = RealRuntime()
    runtime._initialized = Initialized(context)  # type: ignore[assignment]
    runtime._checkpoint_store = FailingCheckpointStore()
    ledger = OutcomeLedger()
    for turn_id in (1, 2):
        ledger.record_turn(
            TurnTelemetry(secs=1, tokens_down=1),
            TurnOutcome(kind="answer"),
            turn_id=turn_id,
            restore_turn_id=turn_id - 1,
            message_index=turn_id,
            workspace_id=f"workspace-{turn_id}",
        )

    with pytest.raises(RewindError, match="code restore failed"):
        await runtime.restore_checkpoint("t2", ledger, scope="both")

    assert order == ["conversation", "code", "conversation"]
    assert context.messages == original
    assert [checkpoint.id for checkpoint in ledger.checkpoints] == ["t1", "t2"]
    assert runtime._restoring_checkpoint is False


@pytest.mark.asyncio
async def test_combined_partial_code_restore_keeps_checkpoint_retryable() -> None:
    """A partial default restore must not trim away its own retry target."""
    from amplifier_app_tui.kernel.checkpoints import WorkspaceRestoreOutcome
    from amplifier_app_tui.kernel.runtime import RealRuntime

    class RetryContext:
        def __init__(self) -> None:
            self.messages = live_messages(2)

        async def get_messages(self) -> list[dict[str, Any]]:
            return list(self.messages)

        async def set_messages(self, messages: list[dict[str, Any]]) -> None:
            self.messages = list(messages)

    class RetryCheckpointStore:
        def __init__(self) -> None:
            self.calls = 0

        def restore(
            self,
            checkpoint_id: str,
            *,
            include_target: bool = True,
            retain_target: bool = False,
        ):
            assert checkpoint_id == "workspace-2"
            assert include_target is True
            assert retain_target is False
            self.calls += 1
            if self.calls == 1:
                return WorkspaceRestoreOutcome(
                    checkpoint_id,
                    restored_paths=("src/done.py",),
                    skipped_paths=("src/conflict.py",),
                    warnings=("src/conflict.py: changed since checkpoint",),
                )
            return WorkspaceRestoreOutcome(
                checkpoint_id,
                restored_paths=("src/conflict.py",),
            )

    class Coordinator:
        def __init__(self, context: RetryContext) -> None:
            self._context = context

        def get(self, name: str) -> Any:
            return self._context if name == "context" else None

    class Initialized:
        def __init__(self, context: RetryContext) -> None:
            self.session_id = "live-session"
            self.coordinator = Coordinator(context)

    context = RetryContext()
    original = list(context.messages)
    checkpoint_store = RetryCheckpointStore()
    runtime = RealRuntime()
    runtime._initialized = Initialized(context)  # type: ignore[assignment]
    runtime._checkpoint_store = checkpoint_store
    ledger = OutcomeLedger()
    for turn_id in (1, 2):
        ledger.record_turn(
            TurnTelemetry(secs=1, tokens_down=1),
            TurnOutcome(kind="answer"),
            turn_id=turn_id,
            restore_turn_id=turn_id - 1,
            message_index=turn_id,
            workspace_id=f"workspace-{turn_id}",
        )

    first = await runtime.restore_checkpoint("t2", ledger, scope="both")

    assert first.partial is True
    assert first.code_status == "partial"
    assert first.conversation_restored is False
    assert "conversation kept for retry" in first.summary
    assert context.messages == original
    assert [checkpoint.id for checkpoint in ledger.checkpoints] == ["t1", "t2"]

    second = await runtime.restore_checkpoint("t2", ledger, scope="both")

    assert second.partial is False
    assert second.conversation_restored is True
    assert context.messages == live_messages(1)
    assert [checkpoint.id for checkpoint in ledger.checkpoints] == ["t1"]


@pytest.mark.asyncio
async def test_conversation_restore_recovers_selected_prompt_image() -> None:
    from amplifier_app_tui.kernel.runtime import RealRuntime

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    encoded = base64.b64encode(png).decode("ascii")
    selected = {
        "role": "user",
        "content": [
            {"type": "text", "text": "inspect [Image #1]"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": encoded,
                },
            },
        ],
    }

    class Context:
        def __init__(self) -> None:
            self.messages = [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "one"},
                selected,
                {"role": "assistant", "content": "two"},
            ]

        async def get_messages(self) -> list[dict[str, Any]]:
            return list(self.messages)

        async def set_messages(self, messages: list[dict[str, Any]]) -> None:
            self.messages = list(messages)

    class Coordinator:
        def __init__(self, context: Context) -> None:
            self.context = context

        def get(self, name: str) -> Any:
            return self.context if name == "context" else None

    class Initialized:
        def __init__(self, context: Context) -> None:
            self.session_id = "image-session"
            self.coordinator = Coordinator(context)

    ledger = OutcomeLedger()
    for turn_id, label in ((1, "first"), (2, "inspect [Image #1]")):
        ledger.record_turn(
            TurnTelemetry(secs=1, tokens_down=1),
            TurnOutcome(kind="answer"),
            turn_id=turn_id,
            restore_turn_id=turn_id - 1,
            message_index=turn_id * 2,
            label=label,
        )
    context = Context()
    runtime = RealRuntime()
    runtime._initialized = Initialized(context)  # type: ignore[assignment]

    outcome = await runtime.restore_checkpoint("t2", ledger, scope="conversation")

    assert context.messages == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
    ]
    assert len(outcome.prompt_attachments) == 1
    assert outcome.prompt_attachments[0].data == png
    assert outcome.prompt_attachments[0].media_type == "image/png"
    assert [item.id for item in ledger.checkpoints] == ["t1"]


@pytest.mark.asyncio
async def test_pending_rewind_recovery_reloads_live_context_before_send() -> None:
    from amplifier_app_tui.kernel.runtime import RealRuntime

    restored = live_messages(1)

    class Store:
        def reconcile_rewind_intent(self, session_id: str) -> bool:
            assert session_id == "recover-session"
            return True

        def load(self, session_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            assert session_id == "recover-session"
            return list(restored), {}

    class Context:
        def __init__(self) -> None:
            self.messages = live_messages(3)

        async def set_messages(self, messages: list[dict[str, Any]]) -> None:
            self.messages = list(messages)

    class Coordinator:
        def __init__(self, context: Context) -> None:
            self.context = context

        def get(self, name: str) -> Any:
            return self.context if name == "context" else None

    class Initialized:
        def __init__(self, context: Context) -> None:
            self.session_id = "recover-session"
            self.coordinator = Coordinator(context)

    class Saver:
        def __init__(self) -> None:
            self.count = -1

        def mark_saved_message_count(self, count: int) -> None:
            self.count = count

    runtime = RealRuntime()
    context = Context()
    saver = Saver()
    runtime._initialized = Initialized(context)  # type: ignore[assignment]
    runtime._store = Store()  # type: ignore[assignment]
    runtime._saver = saver  # type: ignore[assignment]
    runtime._rewind_recovery_pending = True

    await runtime._retry_rewind_recovery()

    assert context.messages == restored
    assert saver.count == len(restored)
    assert runtime._rewind_recovery_pending is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "conversation_restored", "code_status", "partial"),
    [
        ("retired", True, "already_restored", False),
        ("expired", False, "unavailable", True),
    ],
)
async def test_combined_restore_honors_workspace_checkpoint_status(
    status: str,
    conversation_restored: bool,
    code_status: str,
    partial: bool,
) -> None:
    from amplifier_app_tui.kernel.runtime import RealRuntime

    class Context:
        def __init__(self) -> None:
            self.messages = live_messages(2)

        async def get_messages(self) -> list[dict[str, Any]]:
            return list(self.messages)

        async def set_messages(self, messages: list[dict[str, Any]]) -> None:
            self.messages = list(messages)

    class Coordinator:
        def __init__(self, context: Context) -> None:
            self.context = context

        def get(self, name: str) -> Any:
            return self.context if name == "context" else None

    class Initialized:
        def __init__(self, context: Context) -> None:
            self.session_id = "status-session"
            self.coordinator = Coordinator(context)

    class Checkpoints:
        def checkpoint_status(self, checkpoint_id: str) -> str:
            assert checkpoint_id == "workspace-2"
            return status

        def restore(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("status seam should avoid a file restore")

    context = Context()
    original = list(context.messages)
    runtime = RealRuntime()
    runtime._initialized = Initialized(context)  # type: ignore[assignment]
    runtime._checkpoint_store = Checkpoints()
    ledger = OutcomeLedger()
    for turn_id in (1, 2):
        ledger.record_turn(
            TurnTelemetry(secs=1, tokens_down=1),
            TurnOutcome(kind="answer"),
            turn_id=turn_id,
            restore_turn_id=turn_id - 1,
            message_index=turn_id,
            workspace_id=f"workspace-{turn_id}",
        )

    outcome = await runtime.restore_checkpoint("t2", ledger, scope="both")

    assert outcome.conversation_restored is conversation_restored
    assert outcome.code_status == code_status
    assert outcome.partial is partial
    assert context.messages == (live_messages(1) if conversation_restored else original)
    assert [item.id for item in ledger.checkpoints] == (
        ["t1"] if conversation_restored else ["t1", "t2"]
    )


@pytest.mark.asyncio
async def test_new_submit_is_refused_while_checkpoint_restore_owns_state() -> None:
    from amplifier_app_tui.kernel.runtime import RealRuntime

    runtime = RealRuntime()
    runtime._restoring_checkpoint = True
    with pytest.raises(RuntimeError, match="checkpoint restore in progress"):
        await runtime.submit("do not race")


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
