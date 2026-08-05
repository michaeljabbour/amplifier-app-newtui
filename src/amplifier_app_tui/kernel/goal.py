"""Thin app bridge for Amplifier's native ``/goal`` orchestration.

The autonomous loop is deliberately *not* implemented here.  The mounted
``loop-streaming`` orchestrator owns evaluation, continuation, stall
detection, routing, cancellation, and ``orchestrator:goal_progress`` events.
This module only validates the app-level command and writes the native
``coordinator.session_state["goal"]`` contract before the ordinary session
execute path runs.

The command grammar and state shape track amplifier-app-cli's public
``/goal`` contract.  Keeping the bridge small matters: another frontend can
set the same state and receive the same native behavior without importing a
CLI renderer or carrying a second goal engine.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

GOAL_MAX_TURNS_FLAG = "--max-turns"
GOAL_CLEAR_ALIASES = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})
GOAL_PROGRESS_EVENT = "orchestrator:goal_progress"

GoalAction = Literal["status", "set", "cleared", "error"]


@dataclass(frozen=True)
class GoalCommandResult:
    """Normalized result of one app-level ``/goal`` command."""

    ok: bool
    action: GoalAction
    detail: str
    raw_condition: str = ""
    condition: str = ""
    cap: int | None = None


def parse_goal_max_turns(args: str) -> tuple[int | None, str]:
    """Return ``(cap, condition)`` for Amplifier's public ``/goal`` syntax.

    No flag (or an explicit zero) means unlimited.  A malformed flag fails
    loudly instead of silently turning an intended bounded run into an
    unlimited one.
    """

    stripped = args.strip()
    if not stripped.startswith(GOAL_MAX_TURNS_FLAG):
        return (None, stripped)

    rest = stripped[len(GOAL_MAX_TURNS_FLAG) :].strip()
    parts = rest.split(maxsplit=1)
    if not parts:
        raise ValueError(
            f"{GOAL_MAX_TURNS_FLAG} requires a non-negative integer value, "
            f"e.g. '/goal {GOAL_MAX_TURNS_FLAG} 5 <condition>' (0 means unlimited)."
        )
    value_text = parts[0]
    condition = parts[1].strip() if len(parts) > 1 else ""
    try:
        value = int(value_text)
    except ValueError:
        raise ValueError(
            f"Invalid {GOAL_MAX_TURNS_FLAG} value: {value_text!r} -- "
            "must be a non-negative integer (0 means unlimited)."
        ) from None
    if value < 0:
        raise ValueError(
            f"Invalid {GOAL_MAX_TURNS_FLAG} value: {value} -- "
            "must be a non-negative integer (0 means unlimited)."
        )
    return (None if value == 0 else value, condition)


def goal_action(args: str) -> GoalAction:
    """Classify *args* without touching coordinator state."""

    stripped = args.strip()
    if not stripped:
        return "status"
    if stripped.lower() in GOAL_CLEAR_ALIASES:
        return "cleared"
    return "set"


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _event_names(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _event_names(item)
    elif isinstance(value, Iterable):
        for item in value:
            yield from _event_names(item)


async def supports_native_goal(coordinator: Any) -> bool:
    """Whether the mounted orchestrator advertises the native goal event.

    ``loop-streaming`` publishes the event through Amplifier's observable
    contribution channel.  The attribute fallback supports older compatible
    builds that implemented the exact payload contract before contributor
    discovery was available; it still verifies the mounted orchestrator, not
    a TUI-local implementation.
    """

    names: set[str] = set()
    getter = getattr(coordinator, "get_capability", None)
    if callable(getter):
        try:
            names.update(_event_names(getter("observability.events") or ()))
        except Exception:  # noqa: BLE001 -- optional discovery surface
            pass
    collect = getattr(coordinator, "collect_contributions", None)
    if callable(collect):
        try:
            names.update(_event_names(await _maybe_await(collect("observability.events"))))
        except Exception:  # noqa: BLE001 -- optional discovery surface
            pass
    if GOAL_PROGRESS_EVENT in names:
        return True

    try:
        orchestrator = coordinator.get("orchestrator")
    except Exception:  # noqa: BLE001 -- duck-typed coordinator
        orchestrator = None
    return callable(getattr(orchestrator, "execute", None)) and hasattr(
        orchestrator, "_GOAL_PROGRESS_SCHEMA_VERSION"
    )


def _session_state(coordinator: Any) -> dict[str, Any] | None:
    state = getattr(coordinator, "session_state", None)
    return state if isinstance(state, dict) else None


def _status_result(state: dict[str, Any]) -> GoalCommandResult:
    goal = state.get("goal")
    if not isinstance(goal, Mapping):
        return GoalCommandResult(
            ok=True,
            action="status",
            detail="No goal active. Usage: /goal <condition>",
        )

    cap = goal.get("cap")
    turns = int(goal.get("turns_used") or 0)
    cap_value = cap if isinstance(cap, int) and cap > 0 else None
    turn_label = f"{turns}/{cap_value}" if cap_value else f"{turns} (unlimited)"
    continuations = int(goal.get("continuations") or 0)
    lines = [
        f"Goal: {goal.get('condition') or ''}",
        f"Turns evaluated: {turn_label}",
        f"Continuations (sent back to assistant): {continuations}",
        f"Last evaluator reason: {goal.get('last_reason') or '(none yet)'}",
    ]
    reasons = goal.get("reasons")
    if isinstance(reasons, list) and len(reasons) > 1:
        lines.append("Recent reasons:")
        lines.extend(f"  - {reason}" for reason in reasons[-3:])
    return GoalCommandResult(
        ok=True,
        action="status",
        detail="\n".join(lines),
        condition=str(goal.get("condition") or ""),
        cap=cap_value,
    )


async def configure_goal(
    coordinator: Any,
    args: str,
    *,
    expand_mentions: Callable[[str], str | Awaitable[str]],
) -> GoalCommandResult:
    """Inspect, clear, or configure the native goal state.

    Setting a goal does not execute a turn here.  The caller must send
    :attr:`GoalCommandResult.raw_condition` through the normal session turn
    path while passing :attr:`GoalCommandResult.condition` as the already
    expanded model prompt.  This preserves one @mention snapshot for both
    the evaluator and the first work turn.
    """

    state = _session_state(coordinator)
    if state is None:
        return GoalCommandResult(False, "error", "Goal unavailable: session state is missing.")

    stripped = args.strip()
    action = goal_action(stripped)
    if action == "status":
        return _status_result(state)
    if action == "cleared":
        active = state.get("goal")
        state["goal"] = None
        if isinstance(active, Mapping):
            return GoalCommandResult(
                True,
                "cleared",
                f"Goal cleared: {active.get('condition') or ''}",
            )
        return GoalCommandResult(True, "cleared", "No goal active.")

    if not await supports_native_goal(coordinator):
        return GoalCommandResult(
            False,
            "error",
            "Goal unavailable: the mounted orchestrator does not advertise Amplifier's "
            "native goal-progress contract.",
        )

    try:
        cap, raw_condition = parse_goal_max_turns(stripped)
    except ValueError as error:
        return GoalCommandResult(False, "error", f"Goal not set: {error}")
    if not raw_condition:
        return GoalCommandResult(
            False,
            "error",
            f"Goal not set: missing condition text after {GOAL_MAX_TURNS_FLAG} N. "
            f"Usage: /goal [{GOAL_MAX_TURNS_FLAG} N] <condition>",
        )

    try:
        expanded = str(await _maybe_await(expand_mentions(raw_condition)))
    except Exception as error:  # noqa: BLE001 -- mention failure must not arm a goal
        return GoalCommandResult(False, "error", f"Goal not set: {error}")

    state["goal"] = {
        "condition": expanded,
        "turns_used": 0,
        "last_reason": None,
        "cap": cap,
        "reasons": [],
        "continuations": 0,
        "no_tool_turns": 0,
        "escalated": False,
    }
    suffix = f"max {cap} turns" if cap else "unlimited turns"
    return GoalCommandResult(
        True,
        "set",
        f"Goal set ({suffix}).",
        raw_condition=raw_condition,
        condition=expanded,
        cap=cap,
    )


def clear_matching_goal(coordinator: Any, result: GoalCommandResult) -> None:
    """Roll back a just-configured goal if its first turn was not admitted."""

    state = _session_state(coordinator)
    if state is None:
        return
    current = state.get("goal")
    if not isinstance(current, Mapping):
        return
    if (
        current.get("condition") == result.condition
        and current.get("cap") == result.cap
        and int(current.get("turns_used") or 0) == 0
    ):
        state["goal"] = None


__all__ = [
    "GOAL_CLEAR_ALIASES",
    "GOAL_MAX_TURNS_FLAG",
    "GOAL_PROGRESS_EVENT",
    "GoalAction",
    "GoalCommandResult",
    "clear_matching_goal",
    "configure_goal",
    "goal_action",
    "parse_goal_max_turns",
    "supports_native_goal",
]
