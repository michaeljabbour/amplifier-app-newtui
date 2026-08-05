"""Rewind: checkpoint-addressed session forking, confirm-then-trim.

DESIGN-SPEC §9 / ADR-0007 §Rewind:

- Checkpoints come from the model layer's ``OutcomeLedger`` — one per
  turn rule, stamped onto the TurnRule block at emit time. This module
  addresses them strictly by id, never by rendered-label matching.
- Forking uses foundation's ``fork_session(parent_dir, turn=N,
  handle_orphaned_tools="complete")`` for stored sessions and
  ``fork_session_in_memory`` + ``context.set_messages()`` for the live
  context.
- **Confirm-then-trim** (the codex backtrack state machine): the ledger
  (and therefore the UI transcript) is trimmed only AFTER the backend
  confirms the fork. A failed fork leaves everything untouched.

The ledger/checkpoint types are consumed through structural protocols so
this kernel module never imports the model layer at runtime.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from .clipboard import ImageAttachment

logger = logging.getLogger(__name__)


class RewindError(RuntimeError):
    """The fork could not be performed; nothing was trimmed."""


class RewindRecoveryPendingError(RewindError):
    """A durable restore intent must reconcile before another user turn."""


@runtime_checkable
class CheckpointLike(Protocol):
    """Structural view of ``model.turn.Checkpoint``."""

    @property
    def id(self) -> str: ...
    @property
    def turn_id(self) -> int: ...
    @property
    def before_turn_id(self) -> int: ...
    @property
    def cost_at(self) -> Decimal: ...
    @property
    def label(self) -> str: ...
    @property
    def workspace_id(self) -> str: ...


@runtime_checkable
class LedgerLike(Protocol):
    """Structural view of ``model.turn.OutcomeLedger``."""

    @property
    def checkpoints(self) -> tuple[Any, ...]: ...
    def checkpoint_by_id(self, checkpoint_id: str) -> Any | None: ...
    def trim_to(self, checkpoint_id: str) -> None: ...
    def trim_before(self, checkpoint_id: str) -> None: ...


class ForkOutcome(BaseModel):
    """Confirmed result of a rewind fork (backend already succeeded)."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    checkpoint_id: str
    session_id: str
    session_dir: Path | None = None
    forked_from_turn: int
    message_count: int
    in_memory: bool = False


@dataclass(frozen=True, slots=True)
class PreparedRestore:
    """A validated, non-mutating conversation slice ready to commit."""

    checkpoint_id: str
    session_id: str
    boundary: int
    messages: tuple[dict[str, Any], ...]


CodeRestoreStatus = Literal[
    "not_requested",
    "restored",
    "unchanged",
    "partial",
    "already_restored",
    "unavailable",
]


class CheckpointRestoreOutcome(BaseModel):
    """Structured result shared by kernel, adapter, and app-loop commit."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    scope: Literal["both", "conversation", "code"]
    summary: str
    conversation_restored: bool = False
    code_status: CodeRestoreStatus = "not_requested"
    partial: bool = False
    prompt_attachments: tuple[ImageAttachment, ...] = ()


class RestoreLedgerSnapshot:
    """Immutable app-loop checkpoint data safe to marshal to runtime thread.

    ``trim_before`` confirms only this snapshot. The real UI ledger is trimmed
    on the app loop after :class:`CheckpointRestoreOutcome` returns.
    """

    def __init__(
        self,
        checkpoint: CheckpointLike,
        kept_turns_before: int,
        visible_workspace_ids_before: tuple[str, ...] = (),
    ) -> None:
        self._checkpoint = checkpoint
        self.kept_turns_before = kept_turns_before
        self.visible_workspace_ids_before = visible_workspace_ids_before
        self.confirmed = False

    @property
    def checkpoints(self) -> tuple[CheckpointLike, ...]:
        return (self._checkpoint,)

    @property
    def turns(self) -> tuple[Any, ...]:
        return ()

    def checkpoint_by_id(self, checkpoint_id: str) -> CheckpointLike | None:
        return self._checkpoint if self._checkpoint.id == checkpoint_id else None

    def trim_to(self, checkpoint_id: str) -> None:
        if self.checkpoint_by_id(checkpoint_id) is None:
            raise KeyError(f"unknown checkpoint: {checkpoint_id}")
        self.confirmed = True

    def trim_before(self, checkpoint_id: str) -> None:
        self.trim_to(checkpoint_id)


class RewindController:
    """Checkpoint picker + confirm-then-trim forking for one session.

    Args:
        ledger: The session's ``OutcomeLedger`` (checkpoint source and
            trim target).
        session_dir: The stored session directory
            (``~/.amplifier/projects/<slug>/sessions/<id>``) — required
            for :meth:`fork_from`.
        fork_fn / fork_in_memory_fn: Injection seams for tests; default
            to foundation's ``fork_session`` / ``fork_session_in_memory``
            (imported lazily).
    """

    def __init__(
        self,
        ledger: LedgerLike,
        *,
        session_dir: Path | None = None,
        fork_fn: Callable[..., Any] | None = None,
        fork_in_memory_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.ledger = ledger
        self.session_dir = session_dir
        self._fork_fn = fork_fn
        self._fork_in_memory_fn = fork_in_memory_fn

    # -- checkpoint picker ----------------------------------------------------

    @property
    def checkpoints(self) -> tuple[CheckpointLike, ...]:
        """All rewind targets, oldest first (one per recorded turn rule)."""
        return tuple(self.ledger.checkpoints)

    def resolve(self, checkpoint: CheckpointLike | str) -> CheckpointLike:
        """Resolve a checkpoint object or id to a ledger-known checkpoint."""
        checkpoint_id = checkpoint if isinstance(checkpoint, str) else checkpoint.id
        found = self.ledger.checkpoint_by_id(checkpoint_id)
        if found is None:
            raise RewindError(f"unknown checkpoint: {checkpoint_id}")
        return found

    # -- forking (confirm-then-trim) -------------------------------------------

    async def fork_from(self, checkpoint: CheckpointLike | str) -> ForkOutcome:
        """Fork the stored session at *checkpoint*; trim only on success.

        Runs foundation's file-based ``fork_session`` in a worker thread
        (it is synchronous file I/O). Orphaned ``tool_use`` blocks at the
        cut point are completed with synthetic error results
        (``handle_orphaned_tools="complete"``) so the forked transcript
        is always provider-valid.
        """
        target = self.resolve(checkpoint)
        if self.session_dir is None:
            raise RewindError("fork_from requires session_dir (stored session)")
        if target.turn_id < 1:
            raise RewindError(f"checkpoint {target.id} has no forkable turn")

        fork_fn = self._fork_fn
        if fork_fn is None:
            from amplifier_foundation.session.fork import fork_session

            fork_fn = fork_session

        try:
            result = await asyncio.to_thread(
                fork_fn,
                self.session_dir,
                turn=target.turn_id,
                handle_orphaned_tools="complete",
            )
        except (OSError, ValueError, FileNotFoundError) as exc:
            raise RewindError(f"fork at {target.id} failed: {exc}") from exc

        # Backend confirmed — NOW trim the ledger (confirm-then-trim).
        self.ledger.trim_to(target.id)

        return ForkOutcome(
            checkpoint_id=target.id,
            session_id=result.session_id,
            session_dir=result.session_dir,
            forked_from_turn=result.forked_from_turn,
            message_count=result.message_count,
            in_memory=False,
        )

    async def fork_in_memory(
        self,
        checkpoint: CheckpointLike | str,
        *,
        messages: Sequence[dict[str, Any]],
        set_messages: Callable[[list[dict[str, Any]]], Awaitable[None]],
        parent_id: str | None = None,
    ) -> ForkOutcome:
        """Rewind the live context in place; trim only after it applies.

        Slices *messages* at the checkpoint's turn via foundation's
        ``fork_session_in_memory`` then commits them with
        ``context.set_messages`` (pass the bound method as
        *set_messages*). The ledger is trimmed only after the context
        accepted the restored messages.
        """
        target = self.resolve(checkpoint)
        if target.turn_id < 1:
            raise RewindError(f"checkpoint {target.id} has no forkable turn")

        fork_fn = self._fork_in_memory_fn
        if fork_fn is None:
            from amplifier_foundation.session.fork import fork_session_in_memory

            fork_fn = fork_session_in_memory

        try:
            result = fork_fn(
                list(messages),
                turn=target.turn_id,
                parent_id=parent_id,
                handle_orphaned_tools="complete",
            )
        except ValueError as exc:
            raise RewindError(f"in-memory fork at {target.id} failed: {exc}") from exc

        try:
            await set_messages(result.messages or [])
        except Exception as exc:  # noqa: BLE001 — context rejected the restore
            raise RewindError(f"context restore at {target.id} failed: {exc}") from exc

        # Context confirmed — NOW trim the ledger.
        self.ledger.trim_to(target.id)

        return ForkOutcome(
            checkpoint_id=target.id,
            session_id=result.session_id,
            session_dir=None,
            forked_from_turn=result.forked_from_turn,
            message_count=result.message_count,
            in_memory=True,
        )

    def prepare_restore_before_in_memory(
        self,
        checkpoint: CheckpointLike | str,
        *,
        messages: Sequence[dict[str, Any]],
        parent_id: str | None = None,
    ) -> PreparedRestore:
        """Validate and slice conversation state without mutating it.

        Foundation's fork primitive is reused for every non-empty boundary.
        Its public slicing contract is 1-indexed, so session start (before the
        first prompt) is the one TUI-owned special case.
        """
        target = self.resolve(checkpoint)
        boundary = target.before_turn_id
        if boundary < 0:
            raise RewindError(f"checkpoint {target.id} has an invalid boundary")

        session_id = str(uuid.uuid4())
        restored: list[dict[str, Any]]
        if boundary == 0:
            restored = []
        else:
            fork_fn = self._fork_in_memory_fn
            if fork_fn is None:
                from amplifier_foundation.session.fork import fork_session_in_memory

                fork_fn = fork_session_in_memory
            try:
                result = fork_fn(
                    list(messages),
                    turn=boundary,
                    parent_id=parent_id,
                    handle_orphaned_tools="complete",
                )
            except ValueError as exc:
                raise RewindError(f"in-memory restore at {target.id} failed: {exc}") from exc
            restored = list(result.messages or [])
            session_id = result.session_id

        return PreparedRestore(
            checkpoint_id=target.id,
            session_id=session_id,
            boundary=boundary,
            messages=tuple(dict(message) for message in restored),
        )

    def _validate_prepared_restore(self, plan: PreparedRestore) -> CheckpointLike:
        """Resolve *plan* again before either half of the commit."""
        target = self.resolve(plan.checkpoint_id)
        if target.before_turn_id != plan.boundary:
            raise RewindError(f"checkpoint {target.id} changed while preparing restore")
        return target

    async def apply_prepared_restore(
        self,
        plan: PreparedRestore,
        *,
        set_messages: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> None:
        """Apply prepared messages without trimming the ledger yet."""
        target = self._validate_prepared_restore(plan)
        try:
            await set_messages([dict(message) for message in plan.messages])
        except Exception as exc:  # noqa: BLE001 — context rejected the restore
            raise RewindError(f"context restore at {target.id} failed: {exc}") from exc

    def confirm_prepared_restore(self, plan: PreparedRestore) -> ForkOutcome:
        """Trim the ledger after every selected surface has committed."""
        target = self._validate_prepared_restore(plan)
        self.ledger.trim_before(target.id)
        return ForkOutcome(
            checkpoint_id=target.id,
            session_id=plan.session_id,
            session_dir=None,
            forked_from_turn=plan.boundary,
            message_count=len(plan.messages),
            in_memory=True,
        )

    async def commit_prepared_restore(
        self,
        plan: PreparedRestore,
        *,
        set_messages: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> ForkOutcome:
        """Commit a prepared slice, then trim the ledger on confirmation."""
        await self.apply_prepared_restore(plan, set_messages=set_messages)
        return self.confirm_prepared_restore(plan)

    async def restore_before_in_memory(
        self,
        checkpoint: CheckpointLike | str,
        *,
        messages: Sequence[dict[str, Any]],
        set_messages: Callable[[list[dict[str, Any]]], Awaitable[None]],
        parent_id: str | None = None,
    ) -> ForkOutcome:
        """Restore the live conversation to immediately before a prompt.

        The ledger drops the selected pre-prompt checkpoint and every later
        turn only after the context accepts the prepared replacement.
        """
        plan = self.prepare_restore_before_in_memory(
            checkpoint,
            messages=messages,
            parent_id=parent_id,
        )
        return await self.commit_prepared_restore(plan, set_messages=set_messages)


__all__ = [
    "CheckpointRestoreOutcome",
    "CheckpointLike",
    "CodeRestoreStatus",
    "ForkOutcome",
    "PreparedRestore",
    "LedgerLike",
    "RewindController",
    "RewindError",
    "RewindRecoveryPendingError",
    "RestoreLedgerSnapshot",
]
