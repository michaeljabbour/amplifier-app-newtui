"""Trust resolution: mode → capability → allow/ask/deny (DESIGN-SPEC §4).

Semantics ported from amplifier-app-cli ``ui/governance.py`` +
``interaction_state.py``, collapsed to the five spec modes:

- **plan** — read-only: reads auto-allow, everything else denied.
- **brainstorm** — no tools: everything denied.
- **chat** — ask everything except reads.
- **build** — auto read/test; ask write/net/spend (and exec, which may
  touch any of those).
- **auto** — auto read/write; other capabilities are *classifier-gated*:
  :func:`resolve` returns ``ask`` with ``classifier_gated=True`` and the
  kernel governance hook routes those through the reasoning-blind
  classifier (deny-and-continue on denial, DESIGN-SPEC §7).

Deny is never a halt: the governance hook converts a ``deny`` decision
into a synthesized tool result + a Blocked transcript block, and the turn
continues (deny-and-continue). :class:`DenialLog` counts denials and
flags escalation at 3 consecutive / 20 total, feeding the needs-you queue.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import Enum
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CapabilityClass(str, Enum):
    """The capability a tool call exercises — the unit trust is granted in."""

    READ = "read"
    WRITE = "write"
    NET = "net"
    TEST = "test"
    SPEND = "spend"
    EXEC = "exec"


Decision = Literal["allow", "ask", "deny"]


class TrustDecision(BaseModel):
    """The outcome of resolving one tool call against the active mode.

    - ``decision``: allow (run silently), ask (approval bar), deny
      (deny-and-continue with a Blocked block).
    - ``capability``: the classified capability the decision applied to.
    - ``reason``: short human explanation (surfaces in notices/blocks).
    - ``classifier_gated``: True only in auto mode for capabilities the
      static table cannot settle — the kernel must run the two-stage
      classifier before acting on ``decision`` (which is the fail-closed
      fallback if the classifier is unavailable).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Decision
    capability: CapabilityClass
    reason: str
    classifier_gated: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


# Explicit tool-name -> capability table (declared config first; the
# substring heuristic below is only a fallback — RESEARCH-BRIEF risk 10).
_TOOL_CAPABILITIES: dict[str, CapabilityClass] = {
    "read_file": CapabilityClass.READ,
    "list_files": CapabilityClass.READ,
    "glob": CapabilityClass.READ,
    "grep": CapabilityClass.READ,
    "search": CapabilityClass.READ,
    "write_file": CapabilityClass.WRITE,
    "edit_file": CapabilityClass.WRITE,
    "apply_patch": CapabilityClass.WRITE,
    "create_file": CapabilityClass.WRITE,
    "delete_file": CapabilityClass.WRITE,
    "web_fetch": CapabilityClass.NET,
    "web_search": CapabilityClass.NET,
    "http_request": CapabilityClass.NET,
    "run_tests": CapabilityClass.TEST,
    "task": CapabilityClass.SPEND,
    "spawn_agent": CapabilityClass.SPEND,
    "bash": CapabilityClass.EXEC,
    "shell": CapabilityClass.EXEC,
    "exec": CapabilityClass.EXEC,
    "exec_command": CapabilityClass.EXEC,
}

_READ_HINTS = ("read", "list", "glob", "grep", "search", "find", "cat", "view", "load")
_WRITE_HINTS = ("write", "edit", "patch", "create", "delete", "move", "rename", "mkdir")
_NET_HINTS = ("web", "http", "fetch", "url", "download", "browse")
_TEST_HINTS = ("test", "pytest", "check")
_SPEND_HINTS = ("task", "agent", "spawn", "delegate")
_EXEC_HINTS = ("bash", "shell", "exec", "command", "run")

# Shell command prefixes that make an exec call effectively a test run.
_TEST_COMMAND_MARKERS = (
    "pytest",
    "uv run pytest",
    "python -m pytest",
    "npm test",
    "npm run test",
    "cargo test",
    "go test",
    "make test",
)


def classify_tool(tool_name: str, tool_input: Mapping[str, object] | None = None) -> CapabilityClass:
    """Classify one tool call into a :class:`CapabilityClass`.

    Order: explicit table → exec-command test sniffing → name-substring
    heuristic → EXEC (the most restrictive default: exec asks in every
    non-auto mode, so misclassification fails safe).
    """
    name = tool_name.strip().casefold()
    capability = _TOOL_CAPABILITIES.get(name)
    if capability is None:
        for hints, hinted in (
            (_NET_HINTS, CapabilityClass.NET),
            (_TEST_HINTS, CapabilityClass.TEST),
            (_SPEND_HINTS, CapabilityClass.SPEND),
            (_WRITE_HINTS, CapabilityClass.WRITE),
            (_READ_HINTS, CapabilityClass.READ),
            (_EXEC_HINTS, CapabilityClass.EXEC),
        ):
            if any(hint in name for hint in hints):
                capability = hinted
                break
        else:
            capability = CapabilityClass.EXEC
    if capability == CapabilityClass.EXEC and tool_input:
        command = str(tool_input.get("command", "") or tool_input.get("cmd", "")).strip()
        if command and any(
            command == marker or command.startswith(f"{marker} ")
            for marker in _TEST_COMMAND_MARKERS
        ):
            return CapabilityClass.TEST
    return capability


# Per-mode static policy: capability -> decision. Missing key = "ask"
# (never silently widen an incomplete table — governance.py invariant).
_ALL_DENY: dict[CapabilityClass, Decision] = {cap: "deny" for cap in CapabilityClass}

_MODE_POLICY: dict[str, dict[CapabilityClass, Decision]] = {
    "chat": {CapabilityClass.READ: "allow"},
    "plan": {**_ALL_DENY, CapabilityClass.READ: "allow"},
    "brainstorm": dict(_ALL_DENY),
    "build": {
        CapabilityClass.READ: "allow",
        CapabilityClass.TEST: "allow",
        CapabilityClass.WRITE: "ask",
        CapabilityClass.NET: "ask",
        CapabilityClass.SPEND: "ask",
        CapabilityClass.EXEC: "ask",
    },
}

# Auto is a strict superset of build's auto set (read+test) plus write —
# amplifier's natural wide scope. NET/SPEND/EXEC stay classifier-gated.
_AUTO_STATIC_ALLOW = frozenset(
    {CapabilityClass.READ, CapabilityClass.WRITE, CapabilityClass.TEST}
)


def resolve(
    mode: str,
    tool_name: str,
    tool_input: Mapping[str, object] | None = None,
) -> TrustDecision:
    """Resolve one tool call against a mode's trust posture.

    Unknown modes resolve with chat's posture (the safest interactive
    default: ask everything but reads). In auto mode, capabilities outside
    the static read/write allowance come back ``ask`` +
    ``classifier_gated=True`` — the caller must run the classifier and
    treat this decision as the fail-closed fallback.
    """
    capability = classify_tool(tool_name, tool_input)
    if mode == "auto":
        if capability in _AUTO_STATIC_ALLOW:
            return TrustDecision(
                decision="allow",
                capability=capability,
                reason=f"auto {capability.value} · bypasses classification",
            )
        return TrustDecision(
            decision="ask",
            capability=capability,
            reason=f"{capability.value} has real downside · classifier-gated",
            classifier_gated=True,
        )
    policy = _MODE_POLICY.get(mode, _MODE_POLICY["chat"])
    decision = policy.get(capability, "ask")
    reason = {
        "allow": f"auto {capability.value}",
        "ask": f"ask {capability.value}",
        "deny": f"blocked {capability.value} · {mode} mode",
    }[decision]
    return TrustDecision(decision=decision, capability=capability, reason=reason)


class DenialRecord(BaseModel):
    """One recorded denial with its escalation bookkeeping."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    denial_id: str
    capability: CapabilityClass
    action: str
    reason: str
    created_at: float
    consecutive_count: int
    total_count: int
    escalation_reasons: tuple[str, ...] = ()

    @property
    def escalation_due(self) -> bool:
        """True when this denial crossed an escalation threshold — the
        governance hook must surface a needs-you decision."""
        return bool(self.escalation_reasons)


class DenialLog:
    """Deny-and-continue accounting with escalation thresholds.

    Ported from amplifier-app-cli ``ui/governance.py``: 3 consecutive
    denials or 20 total denials trigger escalation (a needs-you question
    asking the human to review the pattern). ``record_non_denial`` resets
    the consecutive counter on any allowed/asked action.
    """

    _MAX_RETAINED = 1_000

    def __init__(
        self,
        *,
        consecutive_threshold: int = 3,
        total_threshold: int = 20,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if consecutive_threshold < 1 or total_threshold < 1:
            raise ValueError("denial thresholds must be positive")
        self._consecutive_threshold = consecutive_threshold
        self._total_threshold = total_threshold
        self._clock = clock
        self._records: list[DenialRecord] = []
        self._consecutive = 0
        self._total = 0

    @property
    def records(self) -> tuple[DenialRecord, ...]:
        return tuple(self._records)

    @property
    def consecutive_count(self) -> int:
        return self._consecutive

    @property
    def total_count(self) -> int:
        return self._total

    def record_denial(
        self, *, capability: CapabilityClass, action: str, reason: str
    ) -> DenialRecord:
        """Record one denial; the returned record says whether to escalate."""
        clean_reason = " ".join(str(reason).split())
        if not clean_reason:
            raise ValueError("denial reason is required")
        self._consecutive += 1
        self._total += 1
        triggers: list[str] = []
        if self._consecutive == self._consecutive_threshold:
            triggers.append(f"{self._consecutive_threshold} consecutive denials")
        if self._total == self._total_threshold:
            triggers.append(f"{self._total_threshold} total denials")
        record = DenialRecord(
            denial_id=f"denial-{self._total}",
            capability=capability,
            action=" ".join(str(action).split()),
            reason=clean_reason,
            created_at=self._clock(),
            consecutive_count=self._consecutive,
            total_count=self._total,
            escalation_reasons=tuple(triggers),
        )
        self._records.append(record)
        if len(self._records) > self._MAX_RETAINED:
            del self._records[: len(self._records) - self._MAX_RETAINED]
        return record

    def record_non_denial(self) -> None:
        """Reset the consecutive-denial streak (any allow/ask outcome)."""
        self._consecutive = 0


__all__ = [
    "CapabilityClass",
    "Decision",
    "DenialLog",
    "DenialRecord",
    "TrustDecision",
    "classify_tool",
    "resolve",
]
