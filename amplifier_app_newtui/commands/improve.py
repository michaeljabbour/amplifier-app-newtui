"""``/improve`` — configuration proposals from the ledger + denial log.

Mines two evidence streams (DESIGN-SPEC §6):

- **Allowlist candidates** from repeated identical approvals: an action
  approved every single time it was asked (``N/N``) is a candidate for
  the auto allowlist — mockup: ``allowlist: uv run pytest approved 22/22
  times · add to auto``.
- **Trust-slot suggestions** from overridden denials: an action denied by
  policy but overridden by the human every time is a candidate for a
  wider trust boundary — mockup: ``trust slot: 3 denials on push-to-fork
  all overridden · add to trust boundary``.

Everything here is pure data-in/data-out. ``/improve`` **proposes and
never applies silently**: the output is an
:class:`~amplifier_app_newtui.model.blocks.ImproveBlock` of
:class:`~amplifier_app_newtui.model.blocks.ImproveProposal` rows; acting
on one is a separate, explicit user step handled elsewhere.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from ..model.blocks import ImproveBlock, ImproveProposal
from ..model.trust import DenialLog
from ..model.turn import OutcomeLedger

MIN_ALLOWLIST_APPROVALS = 3
"""An action must be approved at least this many times (all N/N) to be
proposed for the allowlist."""

MIN_OVERRIDDEN_DENIALS = 2
"""An action's denials must have been overridden at least this many times
(and every time) to earn a trust-slot suggestion."""


class ApprovalTally(BaseModel):
    """Approval history for one identical action.

    - ``action``: the normalized action text (e.g. ``uv run pytest``).
    - ``approved`` / ``asked``: approvals granted vs. approval prompts
      shown. ``approved == asked`` means the human said yes every time.
    - ``capability``: capability-class name (``read``, ``exec``, …) —
      lets /doctor single out repeated *read-only* approvals.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    approved: int = Field(default=0, ge=0)
    asked: int = Field(default=0, ge=0)
    capability: str = ""

    @property
    def always_approved(self) -> bool:
        return self.asked > 0 and self.approved == self.asked


class OverriddenDenial(BaseModel):
    """Denials of one action that the human later overrode.

    ``overridden == denied`` means every denial of this action was
    reversed by the human — the policy is fighting the user.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    denied: int = Field(ge=1)
    overridden: int = Field(ge=0)

    @property
    def all_overridden(self) -> bool:
        return self.overridden >= self.denied


class ApprovalJournal:
    """Session-scope recorder feeding /improve and /doctor.

    The approval broker calls :meth:`record_ask` on every approval
    prompt; the governance hook calls :meth:`record_override` whenever a
    policy denial is later reversed by the human (retro-answered
    needs-you decision or immediate re-allow).
    """

    def __init__(self) -> None:
        self._asked: Counter[str] = Counter()
        self._approved: Counter[str] = Counter()
        self._capability: dict[str, str] = {}
        self._overridden: Counter[str] = Counter()

    def record_ask(self, action: str, *, approved: bool, capability: str = "") -> None:
        clean = " ".join(str(action).split())
        if not clean:
            raise ValueError("approval action is required")
        self._asked[clean] += 1
        if approved:
            self._approved[clean] += 1
        if capability:
            self._capability[clean] = capability

    def record_override(self, action: str) -> None:
        clean = " ".join(str(action).split())
        if not clean:
            raise ValueError("override action is required")
        self._overridden[clean] += 1

    def tallies(self) -> tuple[ApprovalTally, ...]:
        return tuple(
            ApprovalTally(
                action=action,
                approved=self._approved[action],
                asked=self._asked[action],
                capability=self._capability.get(action, ""),
            )
            for action in self._asked
        )

    def overrides(self, denial_log: DenialLog | None = None) -> tuple[OverriddenDenial, ...]:
        """Overridden-denial rows, denial counts taken from *denial_log*
        when provided (the log is the authority on how often policy said
        no); actions with zero overrides are omitted."""
        denied_counts: Counter[str] = Counter()
        if denial_log is not None:
            for record in denial_log.records:
                denied_counts[record.action] += 1
        rows = []
        for action, overridden in sorted(self._overridden.items()):
            denied = max(denied_counts.get(action, 0), overridden)
            rows.append(
                OverriddenDenial(action=action, denied=denied, overridden=overridden)
            )
        return tuple(rows)


def allowlist_proposals(
    tallies: Iterable[ApprovalTally],
    *,
    min_approvals: int = MIN_ALLOWLIST_APPROVALS,
) -> tuple[ImproveProposal, ...]:
    """``N/N`` approval candidates: always approved, asked >= threshold."""
    proposals = []
    for tally in sorted(tallies, key=lambda t: (-t.asked, t.action)):
        if tally.always_approved and tally.asked >= min_approvals:
            proposals.append(
                ImproveProposal(
                    title=f"allowlist: {tally.action}",
                    rationale=(
                        f"approved {tally.approved}/{tally.asked} times · add to auto"
                    ),
                )
            )
    return tuple(proposals)


def trust_slot_proposals(
    overrides: Iterable[OverriddenDenial],
    *,
    min_overridden: int = MIN_OVERRIDDEN_DENIALS,
) -> tuple[ImproveProposal, ...]:
    """Trust-slot suggestions: every denial overridden, count >= threshold."""
    proposals = []
    for row in sorted(overrides, key=lambda o: (-o.overridden, o.action)):
        if row.all_overridden and row.overridden >= min_overridden:
            proposals.append(
                ImproveProposal(
                    title=f"trust slot: {row.action}",
                    rationale=(
                        f"{row.denied} denials on {row.action} all overridden "
                        "· add to trust boundary"
                    ),
                )
            )
    return tuple(proposals)


def improve_proposals(
    *,
    tallies: Iterable[ApprovalTally] = (),
    overrides: Iterable[OverriddenDenial] = (),
    ledger: OutcomeLedger | None = None,
    min_approvals: int = MIN_ALLOWLIST_APPROVALS,
    min_overridden: int = MIN_OVERRIDDEN_DENIALS,
) -> tuple[ImproveProposal, ...]:
    """All /improve proposals: allowlist candidates first, then trust slots.

    *ledger* is accepted for future spend/yield-driven proposals; today
    the two spec'd proposal kinds are approval- and denial-derived (we
    never invent proposals the evidence does not support).
    """
    del ledger  # reserved: spend-vs-yield proposals are not spec'd yet
    return allowlist_proposals(tallies, min_approvals=min_approvals) + (
        trust_slot_proposals(overrides, min_overridden=min_overridden)
    )


def build_improve_block(
    block_id: str, proposals: tuple[ImproveProposal, ...]
) -> ImproveBlock:
    """Assemble the ``/improve`` transcript block (proposals only — the
    header line ``Improve  from ledger + denial log · proposes, never
    applies silently`` is the renderer's)."""
    return ImproveBlock(id=block_id, proposals=proposals)


__all__ = [
    "ApprovalJournal",
    "ApprovalTally",
    "MIN_ALLOWLIST_APPROVALS",
    "MIN_OVERRIDDEN_DENIALS",
    "OverriddenDenial",
    "allowlist_proposals",
    "build_improve_block",
    "improve_proposals",
    "trust_slot_proposals",
]
