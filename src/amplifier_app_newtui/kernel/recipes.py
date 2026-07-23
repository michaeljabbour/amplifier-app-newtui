"""RecipeApprovalBridge: tool-recipes approval gates → ApprovalBroker.

tool-recipes (amplifier-bundle-recipes, ``modules/tool-recipes``) does NOT
use the bridged ``approval:*`` path. Its native gate contract, verbatim
from the module source:

- **Pause**: the executor persists a pending approval into the recipe
  session state (``session.py set_pending_approval``), emits ONE hook
  event ``recipe:approval`` with payload ``{name, description,
  current_step, total_steps, steps, status: "waiting_approval", prompt,
  stage_name}`` (``executor.py _build_recipe_event_data``), then raises
  ``ApprovalGatePausedError`` — so the recipes TOOL CALL returns
  ``{"status": "paused_for_approval", "recipe", "session_id",
  "stage_name", "approval_prompt", ...}`` and execution stops.
- **Resume**: exclusively via further recipes tool operations —
  ``{"operation": "approve"|"deny", "session_id", "stage_name",
  "message"/"reason"}`` writes the decision into the on-disk session
  state, and ``{"operation": "resume", "session_id"}`` re-runs the
  executor from the checkpoint. There is no event, capability, or
  callback the module listens on.

This bridge adapts the TUI to that contract without wrapping or patching
the module: the ``recipe:approval`` event raises a ticket on the app's
one approval seam (:class:`~.approval.ApprovalBroker` → approval bar),
and the human's choice is routed back through the tool's own operations.

Native-contract boundaries (upstream findings, not worked around here):

- The ``recipe:approval`` payload carries no recipe session id — only the
  paused_for_approval TOOL RESULT does. The bridge recovers it through
  the tool's own ``approvals`` operation, matching on ``stage_name`` and
  preferring the NEWEST pending entry: a nested sub-recipe gate is
  mirrored onto each ancestor session under the same stage name, written
  child-first (``executor.py`` child-pause mirroring), so the newest
  entry is the outermost session — the one whose ``approve``/``resume``
  forward down the chain.
- An app-side ``resume`` runs outside any model turn: the model's
  context still ends at "paused_for_approval" and is not informed of the
  outcome (the module offers no injection path). The UI sees everything
  (the executor's own display messages + this bridge's notifications);
  the model rediscovers state through the tool (``list``/``approvals``)
  on its next turn.
- Gate timeouts belong to the module (``check_approval_timeout`` applies
  the recipe-declared default on resume). The broker ask therefore gets
  a practically-infinite presentation timeout: letting the bar time out
  to Deny would stomp a recipe-declared ``default: approve`` or a
  wait-forever gate with an app-invented denial.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from amplifier_core import HookResult

from .approval import STANDARD_OPTIONS, ApprovalBroker, ApprovalDetail, is_allow
from .events import Notification, UIEvent, recipe_approval_prompt

logger = logging.getLogger(__name__)

_ASK_TIMEOUT = 7 * 24 * 3600.0
"""Presentation timeout for the broker ticket (one week — effectively
"until answered"). See module docstring: the module owns gate timeouts."""

_IDLE_POLL_SECONDS = 0.2
"""Poll interval while waiting for the live turn to finish before an
app-side ``resume`` (a concurrent resume would double-run the executor
if the model — told "use 'approve' or 'deny' to continue" — acts too)."""


def _result_parts(result: Any) -> tuple[bool, dict[str, Any]]:
    """(success, output) from a ToolResult or a plain-dict test double."""
    if isinstance(result, Mapping):
        output = result.get("output")
        return bool(result.get("success")), dict(output) if isinstance(output, Mapping) else {}
    output = getattr(result, "output", None)
    return (
        bool(getattr(result, "success", False)),
        dict(output) if isinstance(output, Mapping) else {},
    )


class RecipeApprovalBridge:
    """One ``recipe:approval`` hook handler + the answer round-trip."""

    EVENTS = ("recipe:approval",)

    def __init__(
        self,
        *,
        broker: ApprovalBroker,
        tools: Callable[[], Mapping[str, Any] | None],
        emit: Callable[[UIEvent], None],
        is_executing: Callable[[], bool],
        ask_timeout: float = _ASK_TIMEOUT,
        idle_poll_seconds: float = _IDLE_POLL_SECONDS,
    ) -> None:
        self._broker = broker
        self._tools = tools
        self._emit = emit
        self._is_executing = is_executing
        self._ask_timeout = ask_timeout
        self._idle_poll_seconds = idle_poll_seconds
        self._tasks: set[asyncio.Task[None]] = set()
        """Strong refs: the settle task outlives the fast hook handler and
        a bare ``create_task`` result may be garbage-collected mid-flight."""

    # -- hook side (must return fast; the executor awaits ``hooks.emit``) -----

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        del event
        task = asyncio.create_task(self._settle(dict(data or {})))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return HookResult(action="continue")

    def register_hooks(self, hooks: Any, *, priority: int = 10) -> Callable[[], None]:
        unregister = hooks.register(
            "recipe:approval",
            self.handle_event,
            priority=priority,
            name="newtui-recipe-approval",
        )
        if not callable(unregister):
            return lambda: None

        def unregister_hook() -> None:
            unregister()

        return unregister_hook

    # -- the round-trip --------------------------------------------------------

    async def _settle(self, data: dict[str, Any]) -> None:
        recipe = str(data.get("name") or "recipe")
        stage = str(data.get("stage_name") or "")
        prompt = recipe_approval_prompt(data)
        try:
            choice = await self._ask(prompt, recipe, stage)
            await self._route(choice, recipe, stage)
        except asyncio.CancelledError:  # session teardown
            raise
        except Exception:  # noqa: BLE001 — a gate must never pause silently
            logger.warning("recipe approval round-trip failed", exc_info=True)
            self._notify(
                f"recipe '{recipe}' is paused at stage '{stage}' but the approval "
                "could not be routed — answer via the recipes tool (approve/deny + resume)",
                level="error",
            )

    async def _ask(self, prompt: str, recipe: str, stage: str) -> str:
        self._broker.stage_detail(
            prompt,
            ApprovalDetail(
                command=f"recipes · {recipe} · stage {stage}",
                rule="recipe approval gate",
                tool_name="recipes",
                tool_input={"recipe": recipe, "stage_name": stage},
            ),
        )
        # default="deny" only shapes the (practically unreachable) timeout
        # resolution; the module's own timeout/default machinery governs
        # the gate itself (module docstring, last boundary note).
        return await self._broker.request_approval(
            prompt, list(STANDARD_OPTIONS), timeout=self._ask_timeout, default="deny"
        )

    async def _route(self, choice: str, recipe: str, stage: str) -> None:
        tool = (self._tools() or {}).get("recipes")
        if tool is None:
            self._notify(
                f"recipe '{recipe}' stage '{stage}': answered but no recipes tool "
                "is mounted — cannot route the decision",
                level="error",
            )
            return
        session_id = await self._pending_session_id(tool, recipe, stage)
        if session_id is None:
            # Already settled elsewhere (tool-driven approve/deny, timeout
            # default applied on a resume) — nothing left to route.
            self._notify(
                f"recipe '{recipe}' stage '{stage}': no pending approval remains — "
                "the gate was already settled"
            )
            return

        if is_allow(choice):
            # "Allow always" has no persistence meaning for a one-shot
            # recipe gate; both allow choices approve this stage once.
            ok, output = _result_parts(
                await tool.execute(
                    {"operation": "approve", "session_id": session_id, "stage_name": stage}
                )
            )
            if not ok:
                self._notify(
                    f"recipe '{recipe}' stage '{stage}': approve failed — "
                    f"{output.get('message', 'see recipes tool')}",
                    level="error",
                )
                return
            await self._resume(tool, recipe, session_id)
        else:
            ok, output = _result_parts(
                await tool.execute(
                    {
                        "operation": "deny",
                        "session_id": session_id,
                        "stage_name": stage,
                        "reason": "Denied via approval bar",
                    }
                )
            )
            if ok:
                self._notify(f"recipe '{recipe}' stage '{stage}' denied — recipe stopped")
            else:
                self._notify(
                    f"recipe '{recipe}' stage '{stage}': deny failed — "
                    f"{output.get('message', 'see recipes tool')}",
                    level="error",
                )

    async def _pending_session_id(self, tool: Any, recipe: str, stage: str) -> str | None:
        """Recover the paused recipe session via the tool's own ``approvals``.

        Matching is by ``stage_name``, preferring the newest entry (see
        module docstring: nested gates mirror onto ancestors under the
        same stage name, written child-first, and only the outermost
        session's approve/resume forward down the whole chain). Ties on
        timestamp prefer a ``recipe_name`` match.
        """
        del recipe  # nested gates carry the CHILD's name; matching on it picks the wrong session
        ok, output = _result_parts(await tool.execute({"operation": "approvals"}))
        if not ok:
            return None
        pending = output.get("pending_approvals")
        if not isinstance(pending, list):
            return None
        matches = [
            entry
            for entry in pending
            if isinstance(entry, Mapping) and str(entry.get("stage_name") or "") == stage
        ]
        if not matches:
            return None
        matches.sort(key=lambda entry: str(entry.get("approval_requested_at") or ""), reverse=True)
        session_id = matches[0].get("session_id")
        return str(session_id) if session_id else None

    async def _resume(self, tool: Any, recipe: str, session_id: str) -> None:
        # The approval was written; resuming while the model's turn is
        # still live risks a double resume (the paused_for_approval tool
        # result tells the model to approve/resume too). Wait for idle —
        # the wait is bounded by the session's lifetime, and the approve
        # above is durable either way.
        while self._is_executing():
            await asyncio.sleep(self._idle_poll_seconds)
        ok, output = _result_parts(
            await tool.execute({"operation": "resume", "session_id": session_id})
        )
        if not ok:
            self._notify(
                f"recipe '{recipe}': resume failed — {output.get('message', 'see recipes tool')}",
                level="error",
            )
            return
        status = str(output.get("status") or "")
        if status == "paused_for_approval":
            # The next gate already re-emitted recipe:approval, raising a
            # fresh ticket — nothing more to do here.
            self._notify(
                f"recipe '{recipe}' resumed and paused at stage '{output.get('stage_name', '')}'"
            )
        else:
            self._notify(f"recipe '{recipe}' approved and resumed — status: {status or 'done'}")

    def _notify(self, message: str, *, level: str = "info") -> None:
        self._emit(Notification(message=message, level=level, source="recipes"))


__all__ = ["RecipeApprovalBridge"]
