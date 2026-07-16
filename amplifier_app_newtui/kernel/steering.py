"""StepBoundaryBridge: the ONE steering path (ADR-0007 §Steering).

Consumes exactly one queued steer per ``provider:request`` on the root
session and injects it as a user-role context message::

    HookResult(action="inject_context", context_injection_role="user")

Registered at priority ~950 so it runs just before the provider call.
Answered needs-you decisions ride the same boundary (the mockup's
"Applying decision: …" flow). Leftover steers at turn end are NOT this
module's job: the app drains them via ``SteeringQueue.drain_steers()``
and rolls them forward as a follow-up turn with a visible notice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from amplifier_core import HookResult

from ..model.queues import NeedsYouItem, NeedsYouQueue, QueuedMessage, SteeringQueue


class StepBoundaryBridge:
    """Drain one steer (+ any answered deferred decisions) per step."""

    EVENTS = ("provider:request",)

    def __init__(
        self,
        root_session_id: str,
        steering: SteeringQueue,
        *,
        needs_you: NeedsYouQueue | None = None,
        on_applied: Callable[[QueuedMessage], None] | None = None,
        on_answers: Callable[[tuple[NeedsYouItem, ...]], None] | None = None,
    ) -> None:
        self._root_session_id = root_session_id
        self._steering = steering
        self._needs_you = needs_you
        self._on_applied = on_applied
        self._on_answers = on_answers

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        if event != "provider:request":
            return HookResult(action="continue")
        session_id = str(data.get("session_id") or self._root_session_id)
        if session_id != self._root_session_id:
            return HookResult(action="continue")
        steer = self._steering.consume_next_steer()
        answers = self._needs_you.consume_answered() if self._needs_you else ()
        if steer is None and not answers:
            return HookResult(action="continue")
        if steer is not None and self._on_applied is not None:
            self._on_applied(steer)
        if answers and self._on_answers is not None:
            self._on_answers(answers)
        injections: list[str] = []
        if steer is not None:
            injections.append(
                "User steering received during this turn. Apply it at this safe "
                f"step boundary:\n{steer.text}"
            )
        if answers:
            answer_lines = [
                f"{item.decision_id}: {item.question}\nAnswer: {item.answer}"
                for item in answers
            ]
            injections.append(
                "The user answered deferred decisions. Apply these answers to "
                "dependent work:\n" + "\n".join(answer_lines)
            )
        return HookResult(
            action="inject_context",
            context_injection="\n\n".join(injections),
            context_injection_role="user",
            ephemeral=False,
            suppress_output=True,
        )

    def register_hooks(self, hooks: Any, *, priority: int = 950) -> Callable[[], None]:
        unregister = hooks.register(
            "provider:request",
            self.handle_event,
            priority=priority,
            name="newtui-step-boundary-steering",
        )
        return unregister if callable(unregister) else lambda: None


__all__ = ["StepBoundaryBridge"]
