"""Turn-level telemetry, outcomes, checkpoints and the session ledger.

Turn identity (ADR-0007 resolution 4): the app assigns ``turn_id`` at
``prompt:submit`` as the 1-indexed user-message position in the live
context (resume history base + recorded ledger turns — rewound
automatically when a confirmed fork trims the ledger, spec §9). Steers
never increment it (leftover steers are discarded at turn end); queued
messages DO. Every turn rule records a :class:`Checkpoint`
stamped onto the TurnRule block at emit time — rewind resolves
checkpoints by id, never by string matching rendered labels.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _format_elapsed(seconds: float) -> str:
    """Elapsed format used in telemetry suffixes/labels.

    Mockup: always raw integer seconds (``secs + "s"`` — working line,
    plan suffix and rule telemetry alike), so a 75-second turn reads
    ``75s``, never ``1m 15s``.
    """
    return f"{int(seconds)}s"


def _format_tokens(tokens: int) -> str:
    """``0.0k`` / ``3.2k`` / ``1200.0k`` token formatting per the mockup.

    Mockup always renders ``(toks/1000).toFixed(1) + "k"`` — sub-1k
    counts included (``↓ 0.0k tok`` at turn start, ``0.6k`` at 608)
    and never switches to m-units, so 1.2M tokens reads ``1200.0k``.
    """
    return f"{tokens / 1_000:.1f}k"


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
        parts = [_format_elapsed(self.secs), f"↓ {_format_tokens(self.tokens_down)} tok"]
        return f"({' · '.join(parts)})"

    def label(self) -> str:
        """Turn-rule label prefix: ``<Ns> · <X.Xk> tok, <N>% cached · $<cost>``.

        ``~$`` when any of the turn's usage was unpriceable (the figure
        is a floor, not the real spend).
        """
        token_part = f"{_format_tokens(self.tokens_down)} tok"
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
    """One rewind target recorded at every turn rule (DESIGN-SPEC §9).

    - ``id``: ``t1``, ``t2``, … (stamped on the TurnRule block at emit).
    - ``turn_id``: 1-indexed user-message turn in the live context (the
      fork point foundation's ``fork_session[_in_memory]`` slices at).
    - ``message_index``: transcript message index at the rule — the trim
      point the backend fork restores to.
    - ``cost_at``: cumulative session spend when the checkpoint was cut.
    - ``label``: human description shown in the rewind picker.
    """

    id: str
    turn_id: int = Field(ge=0)
    message_index: int = Field(ge=0)
    cost_at: Decimal = Field(default=Decimal("0"), ge=0)
    label: str = ""


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
        return tuple(turn.checkpoint for turn in self._turns)

    def next_checkpoint_id(self) -> str:
        return f"t{len(self._turns) + 1}"

    def record_turn(
        self,
        telemetry: TurnTelemetry,
        outcome: TurnOutcome,
        *,
        turn_id: int,
        message_index: int,
        label: str = "",
        cost_at: Decimal | None = None,
    ) -> LedgerTurn:
        """Record a completed turn, cutting its checkpoint at the same time.

        ``cost_at`` is the cumulative SESSION cost at the rule (mockup
        ``cp.cost = this.cost`` — the footer $ at that moment, including
        any pre-session baseline). Falls back to recorded-turn spend when
        the caller has no session baseline.
        """
        checkpoint = Checkpoint(
            id=self.next_checkpoint_id(),
            turn_id=turn_id,
            message_index=message_index,
            cost_at=self.spend + telemetry.cost if cost_at is None else cost_at,
            label=label,
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

    def trim_to(self, checkpoint_id: str) -> None:
        """Drop ledger turns after *checkpoint_id* (post-fork, confirm-then-trim).

        Called only after the backend confirms the session fork
        (ADR-0007 rewind contract). The checkpoint's own turn survives.
        """
        for index, turn in enumerate(self._turns):
            if turn.checkpoint.id == checkpoint_id:
                del self._turns[index + 1 :]
                return
        raise KeyError(f"unknown checkpoint: {checkpoint_id}")


__all__ = [
    "Checkpoint",
    "LedgerTurn",
    "OutcomeKind",
    "OutcomeLedger",
    "TurnOutcome",
    "TurnTelemetry",
]
