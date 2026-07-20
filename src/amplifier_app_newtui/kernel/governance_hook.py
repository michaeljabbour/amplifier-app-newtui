"""Governance ``tool:pre`` hook: model.trust decisions → HookResults.

The single place trust gating happens (ADR-0007 resolution 1). Registered
at high precedence (priority 1000 by default) so it runs before display
hooks. Maps :func:`amplifier_app_newtui.model.trust.resolve` onto the
kernel contract:

- ``allow`` → ``HookResult(action="continue")``
- ``ask``   → ``HookResult(action="ask_user", approval_*)`` with the
  verbatim ``Allow once / Allow always / Deny`` options; the structured
  :class:`~.approval.ApprovalDetail` is staged on the ApprovalBroker so it
  travels end-to-end without prompt-global smuggling.
- ``deny``  → ``HookResult(action="deny", reason=…)`` — deny-and-continue:
  the orchestrator synthesizes a "denied" tool result and the turn keeps
  going; the DenialLog counts it and escalation (3 consecutive / 20 total)
  raises a needs-you decision.

Auto mode (DESIGN-SPEC §4/§7): capabilities outside the static read/write
allowance come back ``classifier_gated`` and are settled by a
reasoning-blind classifier — it sees only user messages and proposed
actions, never assistant reasoning. Classifier deny (or any classifier
failure — fail closed) becomes a deferred needs-you decision while the run
continues. :class:`OfflineAutoClassifier` is the deterministic offline
fallback (ported from amplifier-app-cli ``authorization_stage.py``
``ReasoningBlindStageEvaluator``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path, PurePath
from typing import Any, Protocol

from amplifier_core import HookResult

from ..model.queues import NeedsYouQueue
from ..model.trust import (
    CapabilityClass,
    DenialLog,
    TrustDecision,
    resolve,
    resolve_capability,
)
from .approval import STANDARD_OPTIONS, ApprovalBroker, ApprovalDetail
from .directory_permissions import DirectoryPolicy

_MAX_USER_MESSAGES = 12
_MAX_MESSAGE_CHARS = 32_768
_MAX_ACTION_CHARS = 4_096

Verdict = tuple[bool, str]
"""(allowed, reason) — the classifier's binary, reasoning-blind verdict."""


class AutoClassifier(Protocol):
    """Reasoning-blind action classifier for auto mode."""

    async def classify(
        self,
        *,
        action: str,
        capability: CapabilityClass,
        target: str,
        user_messages: tuple[str, ...],
    ) -> Verdict: ...


class OfflineAutoClassifier:
    """Deterministic classifier (no provider, no network) — wide scope.

    Deny destructive shapes outright; defer outbound publishes (``git
    push``) unless they match an explicit user request; allow everything
    else — amplifier's natural wide trust scope in auto mode (user
    directive 2026-07-16). Sees ONLY user messages — never assistant
    reasoning (reasoning-blind by construction).
    """

    _BOUNDARY = re.compile(r"\bgit\s+push\b", re.IGNORECASE)

    _DESTRUCTIVE = re.compile(
        r"(?:\brm\s+-[^\n]*r[^\n]*f|\bgit\s+push\b[^\n]*(?:--force|-f\b)|"
        r"\bdrop\s+(?:database|table)\b|\bcurl\b[^\n]*\|\s*(?:sh|bash)\b)",
        re.IGNORECASE,
    )
    _WORDS = re.compile(r"[a-z0-9][a-z0-9._/-]{1,}", re.IGNORECASE)
    _STOP_WORDS = frozenset(
        {"and", "for", "from", "into", "main", "origin", "please", "the", "this", "that", "with"}
    )
    _VERBS: dict[CapabilityClass, tuple[str, ...]] = {
        CapabilityClass.READ: ("inspect", "list", "read", "show"),
        CapabilityClass.TEST: ("check", "run", "test", "verify"),
        CapabilityClass.WRITE: ("add", "change", "create", "edit", "write"),
        CapabilityClass.EXEC: ("check", "execute", "inspect", "run", "verify"),
        CapabilityClass.NET: ("browse", "download", "fetch", "look up", "search", "upload"),
        CapabilityClass.SPEND: ("agent", "delegate", "parallel", "research", "spawn"),
        CapabilityClass.OUTSIDE_PROJECT: ("change", "edit", "run", "write"),
    }
    _SEMANTIC_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("pytest", ("test", "verify")),
        ("git push", ("publish", "push", "ship")),
        ("git commit", ("commit", "save")),
        ("git status", ("inspect", "status")),
        ("git diff", ("diff", "review")),
    )

    async def classify(
        self,
        *,
        action: str,
        capability: CapabilityClass,
        target: str,
        user_messages: tuple[str, ...],
    ) -> Verdict:
        if self._DESTRUCTIVE.search(action):
            return (False, "action has destructive or irreversible form")
        if self._is_authorized(action, capability, target, user_messages):
            return (True, "action matches an explicit user request")
        if capability == CapabilityClass.OUTSIDE_PROJECT:
            return (False, "outside configured project boundary without explicit authorization")
        if self._BOUNDARY.search(action):
            return (False, "outbound push crosses the trust boundary unrequested")
        return (True, "within amplifier's wide trust scope")

    def _is_authorized(
        self,
        action: str,
        capability: CapabilityClass,
        target: str,
        user_messages: tuple[str, ...],
    ) -> bool:
        action_fold = action.casefold()
        action_words = self._significant_words(action_fold)
        verbs = self._VERBS.get(capability, ())
        clean_target = target.casefold().strip()
        for raw_message in reversed(user_messages[-_MAX_USER_MESSAGES:]):
            message = raw_message.casefold()
            has_verb = any(verb in message for verb in verbs) or self._semantic(
                action_fold, message
            )
            if not has_verb:
                continue
            if clean_target and clean_target in message:
                return True
            if action_words & self._significant_words(message):
                return True
            if capability == CapabilityClass.SPEND:
                return True
            if self._semantic(action_fold, message):
                return True
        return False

    def _semantic(self, action: str, message: str) -> bool:
        return any(
            command in action and any(term in message for term in terms)
            for command, terms in self._SEMANTIC_TERMS
        )

    def _significant_words(self, value: str) -> frozenset[str]:
        return frozenset(
            word
            for word in self._WORDS.findall(value)
            if word not in self._STOP_WORDS and len(word) > 2
        )


class GovernanceHook:
    """The app's trust gate on ``tool:pre`` (+ ``prompt:submit`` evidence).

    ``mode`` is a live callable so mode changes apply instantly with no
    session teardown. Deny is never a halt (deny-and-continue).
    """

    EVENTS = ("prompt:submit", "tool:pre")

    def __init__(
        self,
        root_session_id: str,
        *,
        mode: Callable[[], str],
        denial_log: DenialLog,
        broker: ApprovalBroker | None = None,
        needs_you: NeedsYouQueue | None = None,
        classifier: AutoClassifier | None = None,
        on_blocked: Callable[[str, str], None] | None = None,
        directory_policy: DirectoryPolicy | None = None,
        permission_resolver: Callable[
            [str, Mapping[str, object] | None], TrustDecision
        ]
        | None = None,
        capability_resolver: Callable[[CapabilityClass], TrustDecision] | None = None,
    ) -> None:
        self._root_session_id = root_session_id
        self._mode = mode
        self._denial_log = denial_log
        self._broker = broker
        self._needs_you = needs_you
        self._classifier: AutoClassifier = classifier or OfflineAutoClassifier()
        self._on_blocked = on_blocked
        self._directory_policy = directory_policy
        self._permission_resolver = permission_resolver
        self._capability_resolver = capability_resolver
        self._user_messages: list[str] = []

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        if event == "prompt:submit":
            self._observe_prompt(data)
            return HookResult(action="continue")
        if event != "tool:pre":
            return HookResult(action="continue")
        return await self._govern_tool(data)

    def register_hooks(
        self, hooks: Any, *, priority: int = 1_000
    ) -> Callable[[], None]:
        unregister_callbacks: list[Callable[..., object]] = []
        for event in self.EVENTS:
            unregister = hooks.register(
                event,
                self.handle_event,
                priority=priority,
                name=f"newtui-governance-{event.replace(':', '-')}",
            )
            if callable(unregister):
                unregister_callbacks.append(unregister)

        def unregister_all() -> None:
            for unregister in reversed(unregister_callbacks):
                unregister()

        return unregister_all

    # -- internals -----------------------------------------------------------

    def _observe_prompt(self, data: Mapping[str, Any]) -> None:
        session_id = str(data.get("session_id") or self._root_session_id)
        prompt = data.get("prompt")
        if session_id != self._root_session_id or not isinstance(prompt, str):
            return
        if not prompt.strip():
            return
        self._user_messages.append(prompt[:_MAX_MESSAGE_CHARS])
        if len(self._user_messages) > _MAX_USER_MESSAGES:
            del self._user_messages[: len(self._user_messages) - _MAX_USER_MESSAGES]

    async def _govern_tool(self, data: Mapping[str, Any]) -> HookResult:
        tool_name = _line(data.get("tool_name") or data.get("tool") or "tool")
        tool_input = _mapping(data.get("tool_input") or data.get("input"))
        action = _action_text(tool_name, tool_input)
        target = _target(tool_input)
        decision = (
            self._permission_resolver(tool_name, tool_input)
            if self._permission_resolver is not None
            else resolve(self._mode(), tool_name, tool_input)
        )

        policy = self._directory_policy
        if policy is not None and decision.capability == CapabilityClass.WRITE and target:
            allowed, reason = policy.check_write(target)
            if not allowed:
                return self._deny(CapabilityClass.OUTSIDE_PROJECT, action, reason)
        if (
            policy is not None
            and decision.capability == CapabilityClass.READ
            and target
            and not policy.within_allowed(target)
        ):
            decision = self._resolve_capability(CapabilityClass.OUTSIDE_PROJECT)
        if policy is not None and decision.capability == CapabilityClass.EXEC:
            outside = policy.shell_outside_target(action)
            if outside is not None:
                target, reason = outside
                if "denied directories" in reason:
                    return self._deny(CapabilityClass.OUTSIDE_PROJECT, action, reason)
                decision = self._resolve_capability(CapabilityClass.OUTSIDE_PROJECT)

        if decision.classifier_gated:
            return await self._classify(decision, action, target)
        if decision.decision == "allow":
            self._denial_log.record_non_denial()
            return HookResult(action="continue")
        if decision.decision == "ask":
            return self._ask(decision, tool_name, tool_input, action, target)
        return self._deny(decision.capability, action, decision.reason)

    def _resolve_capability(self, capability: CapabilityClass) -> TrustDecision:
        if self._capability_resolver is not None:
            return self._capability_resolver(capability)
        return resolve_capability(self._mode(), capability)

    async def _classify(
        self, decision: TrustDecision, action: str, target: str
    ) -> HookResult:
        try:
            allowed, reason = await self._classifier.classify(
                action=action,
                capability=decision.capability,
                target=target,
                user_messages=tuple(self._user_messages),
            )
        except Exception as error:  # fail closed — a broken classifier denies
            allowed, reason = False, f"classifier failed closed · {error}"
        if allowed:
            self._denial_log.record_non_denial()
            return HookResult(action="continue")
        # Auto-mode trust boundary: deny-and-continue AND park a deferred
        # decision (DESIGN-SPEC §7 — footer "N decisions waiting · ctrl-y").
        if self._needs_you is not None:
            try:
                self._needs_you.defer(
                    f"Allow {action}?",
                    reason,
                    choices=STANDARD_OPTIONS,
                    action=action,
                )
            except ValueError:
                pass
        return self._deny(decision.capability, action, reason)

    def _ask(
        self,
        decision: TrustDecision,
        tool_name: str,
        tool_input: Mapping[str, Any],
        action: str,
        target: str,
    ) -> HookResult:
        prompt = f"Allow {action}?"
        if self._broker is not None:
            self._broker.stage_detail(
                prompt,
                ApprovalDetail(
                    command=action,
                    cwd=target,
                    rule=decision.reason,
                    capability=decision.capability.value,
                    tool_name=tool_name,
                    tool_input=dict(tool_input),
                ),
            )
        return HookResult(
            action="ask_user",
            approval_prompt=prompt,
            approval_options=list(STANDARD_OPTIONS),
            approval_default="deny",
            reason=decision.reason,
        )

    def _deny(
        self, capability: CapabilityClass, action: str, reason: str
    ) -> HookResult:
        record = self._denial_log.record_denial(
            capability=capability, action=action, reason=reason
        )
        if record.escalation_due and self._needs_you is not None:
            try:
                self._needs_you.defer(
                    "Review the run's denial pattern?",
                    " · ".join(record.escalation_reasons),
                    choices=("keep going", "change mode", "stop"),
                )
            except ValueError:
                pass
        if self._on_blocked is not None:
            self._on_blocked(action, reason)
        return HookResult(
            action="deny",
            reason=f"Denied by trust policy: {reason}. Continue without {action}.",
            user_message=f"blocked · {action}",
            user_message_level="warning",
            suppress_output=True,
        )


def _action_text(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    """Human-readable action for prompts/denials/needs-you questions.

    Commands and instructions are self-describing; bare paths are NOT —
    "Allow /Users/…/test_commands_export.py?" told the supervisor
    nothing about WHAT would happen (found live). Path-derived actions
    carry the tool verb and relativize under the working directory.
    """
    for key in ("command", "cmd", "instruction", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _line(value)[:_MAX_ACTION_CHARS]
    for key in ("path", "file_path", "directory"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            path = _line(value)
            try:
                path = str(PurePath(path).relative_to(Path.cwd()))
            except ValueError:
                pass  # outside the project — keep it absolute (that IS the signal)
            return f"{tool_name} · {path}"[:_MAX_ACTION_CHARS]
    return tool_name


def _target(tool_input: Mapping[str, Any]) -> str:
    for key in ("path", "file_path", "directory", "cwd"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_ACTION_CHARS]
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _line(value: Any) -> str:
    return " ".join(str(value or "").split())


__all__ = [
    "AutoClassifier",
    "GovernanceHook",
    "OfflineAutoClassifier",
    "Verdict",
]
