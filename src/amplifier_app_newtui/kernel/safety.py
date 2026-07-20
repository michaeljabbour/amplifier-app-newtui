"""Two-axis safety resolution: approval policy and execution path policy.

Approval answers whether a tool call may proceed without a human decision.
Path policy independently answers where a recognizable action may operate.
Keeping both axes explicit prevents an allowlisted command from silently
bypassing configured directory boundaries and gives future OS sandboxes a
stable policy seam without claiming that one exists today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..model.trust import CapabilityClass, TrustDecision
from .directory_permissions import DirectoryPolicy

ExecutionPolicyDecision = Literal[
    "not-applicable",
    "within-policy",
    "outside-policy",
    "blocked",
]


@dataclass(frozen=True)
class SafetyResolution:
    """Independent approval and path-policy outcomes for one tool call."""

    approval: TrustDecision
    execution_policy: ExecutionPolicyDecision
    policy_reason: str = ""
    target: str = ""

    @property
    def blocked(self) -> bool:
        return self.execution_policy == "blocked"


def resolve_safety(
    approval: TrustDecision,
    *,
    action: str,
    target: str,
    directory_policy: DirectoryPolicy | None,
) -> SafetyResolution:
    """Resolve path policy without changing approval-policy precedence."""
    if directory_policy is None:
        return SafetyResolution(approval, "not-applicable", target=target)

    capability = approval.capability
    if capability == CapabilityClass.WRITE and target:
        allowed, reason = directory_policy.check_write(target)
        return SafetyResolution(
            approval,
            "within-policy" if allowed else "blocked",
            reason,
            target,
        )

    if capability == CapabilityClass.READ and target:
        return SafetyResolution(
            approval,
            "not-applicable",
            "reads are not constrained by allowed write directories",
            target,
        )

    if capability == CapabilityClass.EXEC:
        violation = directory_policy.shell_write_violation(action)
        if violation is None:
            return SafetyResolution(
                approval,
                "within-policy",
                "no recognizable shell write violates path policy",
                target,
            )
        path, reason = violation
        return SafetyResolution(approval, "blocked", reason, path)

    return SafetyResolution(approval, "not-applicable", target=target)


__all__ = ["ExecutionPolicyDecision", "SafetyResolution", "resolve_safety"]
