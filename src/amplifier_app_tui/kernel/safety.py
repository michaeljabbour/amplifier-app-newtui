"""Two-axis safety resolution: approval policy and execution path policy.

Approval answers whether a tool call may proceed without a human decision.
Path policy independently answers where a recognizable action may operate.
Keeping both axes explicit prevents an allowlisted command from silently
bypassing configured directory boundaries and gives future OS sandboxes a
stable policy seam without claiming that one exists today.
"""

from __future__ import annotations

from collections.abc import Callable
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
    resolve_capability: Callable[[CapabilityClass], TrustDecision],
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
        if directory_policy.within_allowed(target):
            return SafetyResolution(approval, "within-policy", "within allowed directories", target)
        allowed, reason = directory_policy.check_read(target)
        if not allowed:
            return SafetyResolution(approval, "blocked", reason, target)
        # Reads roam anywhere outside denied directories (within reason) —
        # matching amplifier-app-cli's permissive read defaults. The
        # outside-project gate applies to writes and write-shaped shell.
        return SafetyResolution(approval, "within-policy", reason, target)

    if capability == CapabilityClass.EXEC:
        outside = directory_policy.shell_outside_target(action)
        if outside is None:
            return SafetyResolution(
                approval,
                "within-policy",
                "no outside or protected path detected",
                target,
            )
        path, reason = outside
        if reason.startswith(("path is protected", "path is within denied")):
            return SafetyResolution(approval, "blocked", reason, path)
        return SafetyResolution(
            resolve_capability(CapabilityClass.OUTSIDE_PROJECT),
            "outside-policy",
            reason,
            path,
        )

    return SafetyResolution(approval, "not-applicable", target=target)


__all__ = ["ExecutionPolicyDecision", "SafetyResolution", "resolve_safety"]
