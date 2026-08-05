"""Turn-level telemetry, outcomes, checkpoints and the session ledger.

Turn identity (ADR-0007 resolution 4): the app assigns ``turn_id`` at
``prompt:submit`` as the 1-indexed user-message position in the live
context (resume history base + recorded ledger turns — rewound
automatically when a confirmed fork trims the ledger, spec §9). Steers
never increment it (leftover steers are discarded at turn end); queued
messages DO. Every user prompt opens a :class:`Checkpoint` *before* the
turn executes. The completed turn rule is later stamped with that same
checkpoint id — rewind resolves checkpoints by id, never by string matching
rendered labels.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .formatting import format_tokens_k

MAX_VISIBLE_CHECKPOINTS = 100
"""Maximum recent pre-prompt checkpoints offered by the restore picker."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _format_elapsed(seconds: float) -> str:
    """Elapsed format used in telemetry suffixes/labels.

    Mockup: always raw integer seconds (``secs + "s"`` — working line,
    plan suffix and rule telemetry alike), so a 75-second turn reads
    ``75s``, never ``1m 15s``.
    """
    return f"{int(seconds)}s"


class TurnTelemetry(_FrozenModel):
    """Compact per-turn (or live) telemetry (DESIGN-SPEC §3/§11).

    - ``secs``: wall-clock seconds for the turn so far.
    - ``tokens_down``: output tokens received (the ``↓ X.Xk tok`` figure).
    - ``cached_pct``: percentage of input tokens served from cache.
    - ``cost``: dollars, computed from provider usage (kernel/cost.py).
    - ``estimated``: some usage could not be priced, so ``cost`` is a
      floor — the rendered $ figure gets a ``~`` prefix (never lie).
    """

    secs: float = Field(ge=0)
    tokens_down: int = Field(default=0, ge=0)
    cached_pct: int | None = Field(default=None, ge=0, le=100)
    cost: Decimal = Field(default=Decimal("0"), ge=0)
    estimated: bool = False

    def suffix(self) -> str:
        """Live plan-header suffix: ``(Ns · ↓ X.Xk tok)``."""
        parts = [_format_elapsed(self.secs), f"↓ {format_tokens_k(self.tokens_down)} tok"]
        return f"({' · '.join(parts)})"

    def label(self) -> str:
        """Turn-rule label prefix: ``<Ns> · <X.Xk> tok, <N>% cached · $<cost>``.

        ``~$`` when any of the turn's usage was unpriceable (the figure
        is a floor, not the real spend).
        """
        token_part = f"{format_tokens_k(self.tokens_down)} tok"
        if self.cached_pct is not None:
            token_part += f", {self.cached_pct}% cached"
        marker = "~" if self.estimated else ""
        return " · ".join((_format_elapsed(self.secs), token_part, f"{marker}${self.cost:.2f}"))


OutcomeKind = Literal["answer", "shipped", "interrupted", "plan_ready"]


class TurnOutcome(_FrozenModel):
    """What a completed turn produced (DESIGN-SPEC §3 turn-rule outcomes).

    Rendered outcome strings per kind:

    - ``answer``      → ``answer`` (dimmer label)
    - ``shipped``     → ``3 files · +142/−38 · tests ✔`` (dim label)
    - ``interrupted`` → ``· interrupted``
    - ``plan_ready``  → ``· plan ready``
    """

    kind: OutcomeKind
    files_changed: int = Field(default=0, ge=0)
    diffstat: str = ""
    """``+142/−38`` style diffstat captured from git; empty when not shipped."""
    tests_ok: bool | None = None
    """True/False when tests ran this turn; None when they did not."""

    @property
    def shipped(self) -> bool:
        return self.kind == "shipped"

    def outcome_label(self) -> str:
        """The outcome fragment of the turn-rule label."""
        if self.kind == "answer":
            return "answer"
        if self.kind == "interrupted":
            return "· interrupted"
        if self.kind == "plan_ready":
            return "· plan ready"
        parts = [f"{self.files_changed} file{'s' if self.files_changed != 1 else ''}"]
        if self.diffstat:
            parts.append(self.diffstat)
        if self.tests_ok is not None:
            parts.append("tests ✔" if self.tests_ok else "tests ✗")
        return " · ".join(parts)


class Checkpoint(_FrozenModel):
    """One pre-prompt rewind target (DESIGN-SPEC §9).

    - ``id``: ``t1``, ``t2``, … (stamped on the TurnRule block at emit).
    - ``turn_id``: 1-indexed final user-message position occupied by this
      turn (including persistent mid-turn injections).
    - ``restore_turn_id``: number of user-message turns that existed before
      this prompt. Zero restores the empty session-start context.
    - ``message_index``: transcript message index at the rule — the trim
      point the backend fork restores to.
    - ``cost_at``: cumulative session spend when the checkpoint was cut.
    - ``label``: original prompt shown/restored by the rewind picker.
    - ``workspace_id``: opaque id of the kernel-owned file checkpoint. It
      deliberately does not reuse display ``tN`` ids after a rewind.
    """

    id: str
    turn_id: int = Field(ge=0)
    restore_turn_id: int | None = Field(default=None, ge=0)
    message_index: int = Field(ge=0)
    cost_at: Decimal = Field(default=Decimal("0"), ge=0)
    label: str = ""
    workspace_id: str = ""

    @property
    def before_turn_id(self) -> int:
        """Conversation boundary immediately before this prompt.

        Legacy checkpoints lack ``restore_turn_id``; their natural boundary
        is one turn before the historical post-turn fork point.
        """
        if self.restore_turn_id is not None:
            return self.restore_turn_id
        return max(0, self.turn_id - 1)


class LedgerTurn(_FrozenModel):
    """One completed turn as the ledger records it."""

    turn_id: int
    telemetry: TurnTelemetry
    outcome: TurnOutcome
    checkpoint: Checkpoint


class OutcomeLedger:
    """Session-scope outcome accounting (DESIGN-SPEC §10).

    Backs ``/ledger``: ``N turns · $X.XX · N shipped · N answer-only ·
    cache hit NN%``, the footer ``▲`` yield glyph (last turn shipped) and
    the rewind picker's checkpoint list. Mutable by design — one instance
    per session, fed by the turn lifecycle.
    """

    def __init__(self) -> None:
        self._turns: list[LedgerTurn] = []
        self._pending: Checkpoint | None = None

    @property
    def turns(self) -> tuple[LedgerTurn, ...]:
        return tuple(self._turns)

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    @property
    def spend(self) -> Decimal:
        """Total session cost across recorded turns."""
        return sum((turn.telemetry.cost for turn in self._turns), Decimal("0"))

    @property
    def shipped_count(self) -> int:
        return sum(1 for turn in self._turns if turn.outcome.shipped)

    @property
    def answer_only_count(self) -> int:
        """Mockup cmdLedger math: every non-shipped turn is answer-only.

        ``turns − shipped`` so the ledger line always sums
        (plan-ready and interrupted turns count as answer-only).
        """
        return self.turn_count - self.shipped_count

    @property
    def cache_hit_pct(self) -> int:
        """Token-weighted aggregate cache-hit percentage across turns."""
        weighted = 0.0
        total = 0
        for turn in self._turns:
            if turn.telemetry.cached_pct is None:
                continue
            weighted += turn.telemetry.cached_pct * turn.telemetry.tokens_down
            total += turn.telemetry.tokens_down
        return round(weighted / total) if total else 0

    @property
    def last_shipped(self) -> bool:
        """True when the most recent turn shipped (footer ``▲`` yield glyph)."""
        return bool(self._turns) and self._turns[-1].outcome.shipped

    @property
    def checkpoints(self) -> tuple[Checkpoint, ...]:
        # The durable workspace store retains the same 100-item window. Keep
        # the UI/model list aligned so an old turn rule cannot advertise a
        # file checkpoint whose private manifest has already expired. The
        # complete turn ledger remains intact for telemetry and replay math.
        completed_limit = MAX_VISIBLE_CHECKPOINTS - (1 if self._pending is not None else 0)
        completed = tuple(turn.checkpoint for turn in self._turns[-completed_limit:])
        return (*completed, self._pending) if self._pending is not None else completed

    def next_checkpoint_id(self) -> str:
        return f"t{len(self._turns) + 1}"

    def begin_turn(
        self,
        *,
        turn_id: int,
        restore_turn_id: int,
        message_index: int,
        label: str,
        cost_at: Decimal,
        workspace_id: str = "",
    ) -> Checkpoint:
        """Cut and expose a checkpoint before its prompt starts running.

        A pending checkpoint makes even the first in-flight turn undoable.
        :meth:`record_turn` consumes the same object after close-out, retaining
        the id selected by an interrupt-then-restore flow.
        """
        if self._pending is not None:
            raise RuntimeError(f"checkpoint {self._pending.id} is still pending")
        self._pending = Checkpoint(
            id=self.next_checkpoint_id(),
            turn_id=turn_id,
            restore_turn_id=restore_turn_id,
            message_index=message_index,
            cost_at=cost_at,
            label=label,
            workspace_id=workspace_id,
        )
        return self._pending

    def record_turn(
        self,
        telemetry: TurnTelemetry,
        outcome: TurnOutcome,
        *,
        turn_id: int,
        message_index: int,
        label: str = "",
        cost_at: Decimal | None = None,
        restore_turn_id: int | None = None,
        workspace_id: str = "",
    ) -> LedgerTurn:
        """Record a completed turn, finalizing its pre-prompt checkpoint.

        ``cost_at`` is the cumulative SESSION cost at the rule (mockup
        ``cp.cost = this.cost`` — the footer $ at that moment, including
        any pre-session baseline). Falls back to recorded-turn spend when
        the caller has no session baseline.
        """
        pending = self._pending
        if pending is not None:
            checkpoint = pending.model_copy(
                update={
                    "turn_id": turn_id,
                    "message_index": message_index,
                    "label": label or pending.label,
                    "workspace_id": workspace_id or pending.workspace_id,
                }
            )
            self._pending = None
        else:
            checkpoint = Checkpoint(
                id=self.next_checkpoint_id(),
                turn_id=turn_id,
                restore_turn_id=restore_turn_id,
                message_index=message_index,
                cost_at=self.spend + telemetry.cost if cost_at is None else cost_at,
                label=label,
                workspace_id=workspace_id,
            )
        turn = LedgerTurn(
            turn_id=turn_id, telemetry=telemetry, outcome=outcome, checkpoint=checkpoint
        )
        self._turns.append(turn)
        return turn

    def checkpoint_by_id(self, checkpoint_id: str) -> Checkpoint | None:
        for turn in self._turns:
            if turn.checkpoint.id == checkpoint_id:
                return turn.checkpoint
        if self._pending is not None and self._pending.id == checkpoint_id:
            return self._pending
        return None

    def clear(self) -> None:
        """Drop every recorded turn (resume-replay degrade path, spec §9).

        Used when a replayed event log disagrees with the restored
        transcript (foreign/truncated log, post-rewind ghost turns): the
        replayed checkpoints would slice the live context at the wrong
        turns, so they are discarded and new checkpoints fall back to the
        transcript-derived ``turn_base`` offset.
        """
        self._turns.clear()
        self._pending = None

    def trim_to(self, checkpoint_id: str) -> None:
        """Drop ledger turns after *checkpoint_id* (post-fork, confirm-then-trim).

        Called only after the backend confirms the session fork
        (ADR-0007 rewind contract). The checkpoint's own turn survives.
        """
        for index, turn in enumerate(self._turns):
            if turn.checkpoint.id == checkpoint_id:
                del self._turns[index + 1 :]
                self._pending = None
                return
        if self._pending is not None and self._pending.id == checkpoint_id:
            return
        raise KeyError(f"unknown checkpoint: {checkpoint_id}")

    def trim_before(self, checkpoint_id: str) -> None:
        """Drop the selected pre-prompt checkpoint and everything after it."""
        for index, turn in enumerate(self._turns):
            if turn.checkpoint.id == checkpoint_id:
                del self._turns[index:]
                self._pending = None
                return
        if self._pending is not None and self._pending.id == checkpoint_id:
            self._pending = None
            return
        raise KeyError(f"unknown checkpoint: {checkpoint_id}")


__all__ = [
    "Checkpoint",
    "LedgerTurn",
    "MAX_VISIBLE_CHECKPOINTS",
    "OutcomeKind",
    "OutcomeLedger",
    "TurnOutcome",
    "TurnTelemetry",
]
