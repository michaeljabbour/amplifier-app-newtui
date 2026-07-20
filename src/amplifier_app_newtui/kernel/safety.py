"""Two-axis safety resolution: approval policy and execution confinement.

Approval answers whether a tool call may proceed without a human decision.
Confinement independently answers where the action may operate. Keeping both
axes explicit prevents an allowlisted command from silently bypassing the
workspace boundary and gives future OS sandboxes a stable policy seam.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..model.trust import CapabilityClass, TrustDecision
from .directory_permissions import DirectoryPolicy

ExecutionDecision = Literal[
    "not-applicable",
    "workspace-confined",
    "outside-boundary",
    "blocked",
]


@dataclass(frozen=True)
class SafetyResolution:
    """Independent approval and execution outcomes for one tool call."""

    approval: TrustDecision
    execution: ExecutionDecision
    execution_reason: str = ""
    target: str = ""

    @property
    def blocked(self) -> bool:
        return self.execution == "blocked"


def resolve_safety(
    approval: TrustDecision,
    *,
    action: str,
    target: str,
    directory_policy: DirectoryPolicy | None,
    resolve_capability: Callable[[CapabilityClass], TrustDecision],
) -> SafetyResolution:
    """Resolve confinement without changing approval-policy precedence."""
    if directory_policy is None:
        return SafetyResolution(approval, "not-applicable", target=target)

    capability = approval.capability
    if capability == CapabilityClass.WRITE and target:
        allowed, reason = directory_policy.check_write(target)
        return SafetyResolution(
            approval,
            "workspace-confined" if allowed else "blocked",
            reason,
            target,
        )

    if capability == CapabilityClass.READ and target:
        if directory_policy.within_allowed(target):
            return SafetyResolution(
                approval, "workspace-confined", "within allowed directories", target
            )
        return SafetyResolution(
            resolve_capability(CapabilityClass.OUTSIDE_PROJECT),
            "outside-boundary",
            "read target is outside allowed directories",
            target,
        )

    if capability == CapabilityClass.EXEC:
        outside = directory_policy.shell_outside_target(action)
        if outside is None:
            return SafetyResolution(
                approval,
                "workspace-confined",
                "no outside or protected path detected",
                target,
            )
        path, reason = outside
        if reason.startswith(("path is protected", "path is within denied")):
            return SafetyResolution(approval, "blocked", reason, path)
        return SafetyResolution(
            resolve_capability(CapabilityClass.OUTSIDE_PROJECT),
            "outside-boundary",
            reason,
            path,
        )

    return SafetyResolution(approval, "not-applicable", target=target)


__all__ = ["ExecutionDecision", "SafetyResolution", "resolve_safety"]
