"""Governance gate for the one-shot ``tool invoke`` CLI (ADR-0007 resolution 1).

The interactive TUI gates every tool call on ``tool:pre`` through
:class:`~amplifier_app_newtui.kernel.governance_hook.GovernanceHook`, which can
*ask* the human at the approval bar. A scriptable ``amplifier-newtui tool
invoke`` runs in a one-shot, non-interactive context: there is no approval bar
to answer, so an ``ask`` outcome cannot be satisfied. Rather than silently drop
that gate (an ungated bypass around the very protections the app enforces
in-session), this module resolves the SAME trust posture the TUI would and
refuses anything that is not an outright ``allow`` -- fail-safe by construction.

Two postures, both reusing :mod:`amplifier_app_newtui.model.trust`:

- **safe (default):** ``build`` -- read/test auto-allow; write/net/spend/exec
  resolve to ``ask`` and are therefore refused (no one to ask).
- **write (``--yes``):** ``auto`` -- read/write/test auto-allow (writes still
  clear the directory write-boundary via :func:`~.safety.resolve_safety`);
  net/spend/exec stay classifier-gated and are refused.

The offline classifier is deliberately NOT consulted here: it authorizes
against ambient user intent (the run's prompt history), and a bare CLI invoke
has none, so a classifier-gated capability fails safe to a refusal instead of
being waved through. Out-of-project writes are blocked by the directory policy
exactly as they are in-session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..model.trust import TrustDecision, resolve, resolve_capability
from .directory_permissions import DirectoryPolicy
from .safety import resolve_safety

SAFE_POSTURE = "build"
"""Default one-shot posture: reads/tests run, mutations are refused."""

WRITE_POSTURE = "auto"
"""``--yes`` posture: also permits in-project writes (still boundary-checked)."""

_ACTION_KEYS = ("command", "cmd", "instruction", "query")
_TARGET_KEYS = ("path", "file_path", "directory", "cwd")


@dataclass(frozen=True)
class GateResult:
    """Whether a one-shot invocation may proceed, and why not when it may not."""

    allowed: bool
    capability: str
    reason: str


def _target(args: Mapping[str, Any]) -> str:
    for key in _TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _action(name: str, args: Mapping[str, Any], target: str) -> str:
    for key in _ACTION_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return f"{name} {target}".strip()


def gate_invocation(
    name: str,
    args: Mapping[str, Any],
    *,
    allow_writes: bool,
    directory_policy: DirectoryPolicy | None,
) -> GateResult:
    """Resolve *name*/*args* against the one-shot posture + directory policy.

    Returns an allow only for a static ``allow`` that is not classifier-gated
    and not blocked by the write-boundary; every other outcome (``ask``,
    ``deny``, classifier-gated, path-blocked) is a refusal carrying the
    capability and a human reason. Never raises.
    """
    posture = WRITE_POSTURE if allow_writes else SAFE_POSTURE
    tool_input: Mapping[str, Any] = args if isinstance(args, Mapping) else {}
    decision: TrustDecision = resolve(posture, name, tool_input)
    target = _target(tool_input)
    action = _action(name, tool_input, target)
    safety = resolve_safety(
        decision,
        action=action,
        target=target,
        directory_policy=directory_policy,
        resolve_capability=lambda capability: resolve_capability(posture, capability),
    )
    if safety.blocked:
        return GateResult(
            False, decision.capability.value, safety.policy_reason or "blocked by policy"
        )
    decision = safety.approval
    if decision.decision == "allow" and not decision.classifier_gated:
        return GateResult(True, decision.capability.value, decision.reason)
    reason = _refusal_reason(decision)
    return GateResult(False, decision.capability.value, reason)


def _refusal_reason(decision: TrustDecision) -> str:
    capability = decision.capability.value
    if decision.classifier_gated:
        return (
            f"{capability} needs a human decision the classifier cannot make "
            "non-interactively -- run it in the interactive TUI"
        )
    if decision.decision == "ask":
        return (
            f"{capability} requires approval that a one-shot CLI cannot request "
            "-- re-run with --yes for in-project writes, or use the interactive TUI"
        )
    return decision.reason or f"{capability} is not permitted from the CLI"


__all__ = ["SAFE_POSTURE", "WRITE_POSTURE", "GateResult", "gate_invocation"]
