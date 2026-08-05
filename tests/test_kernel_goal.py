"""Thin ``/goal`` bridge tests over Amplifier's native state contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from amplifier_app_tui.kernel.goal import (
    GOAL_PROGRESS_EVENT,
    GoalCommandResult,
    clear_matching_goal,
    configure_goal,
    goal_action,
    parse_goal_max_turns,
    supports_native_goal,
)


class _Coordinator:
    def __init__(self, *, events: object = (GOAL_PROGRESS_EVENT,), fallback: bool = False) -> None:
        self.session_state: dict[str, Any] = {}
        self._events = events
        self._orchestrator = SimpleNamespace(execute=lambda _prompt: None)
        if fallback:
            self._orchestrator._GOAL_PROGRESS_SCHEMA_VERSION = 1

    def get_capability(self, name: str) -> object:
        return self._events if name == "observability.events" else None

    def get(self, name: str) -> object | None:
        return self._orchestrator if name == "orchestrator" else None


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("ship when tests pass", (None, "ship when tests pass")),
        ("--max-turns 5 ship when tests pass", (5, "ship when tests pass")),
        ("--max-turns 0 keep going", (None, "keep going")),
    ],
)
def test_parse_goal_max_turns(args: str, expected: tuple[int | None, str]) -> None:
    assert parse_goal_max_turns(args) == expected


@pytest.mark.parametrize(
    "args",
    ["--max-turns", "--max-turns nope condition", "--max-turns -1 condition"],
)
def test_parse_goal_max_turns_fails_loudly(args: str) -> None:
    with pytest.raises(ValueError):
        parse_goal_max_turns(args)


def test_goal_action_classifies_status_clear_and_set() -> None:
    assert goal_action("") == "status"
    assert goal_action(" STOP ") == "cleared"
    assert goal_action("a checkable condition") == "set"


def test_native_support_prefers_advertised_event_and_has_compatible_fallback() -> None:
    assert asyncio.run(supports_native_goal(_Coordinator()))
    assert asyncio.run(supports_native_goal(_Coordinator(events=(), fallback=True)))
    assert not asyncio.run(supports_native_goal(_Coordinator(events=())))


def test_configure_goal_writes_exact_native_state_and_snapshots_mentions_once() -> None:
    coordinator = _Coordinator()
    expanded: list[str] = []

    async def expand(text: str) -> str:
        expanded.append(text)
        return f"<context_file>proof</context_file>\n{text}"

    result = asyncio.run(
        configure_goal(
            coordinator,
            "--max-turns 4 all acceptance checks pass",
            expand_mentions=expand,
        )
    )

    assert result == GoalCommandResult(
        True,
        "set",
        "Goal set (max 4 turns).",
        raw_condition="all acceptance checks pass",
        condition="<context_file>proof</context_file>\nall acceptance checks pass",
        cap=4,
    )
    assert expanded == ["all acceptance checks pass"]
    assert coordinator.session_state["goal"] == {
        "condition": "<context_file>proof</context_file>\nall acceptance checks pass",
        "turns_used": 0,
        "last_reason": None,
        "cap": 4,
        "reasons": [],
        "continuations": 0,
        "no_tool_turns": 0,
        "escalated": False,
    }


def test_status_and_clear_read_the_same_native_state() -> None:
    coordinator = _Coordinator()
    coordinator.session_state["goal"] = {
        "condition": "tests pass",
        "turns_used": 2,
        "cap": 5,
        "continuations": 1,
        "last_reason": "one test remains",
        "reasons": ["two remain", "one test remains"],
    }

    status = asyncio.run(configure_goal(coordinator, "", expand_mentions=lambda text: text))
    assert status.ok and status.action == "status"
    assert "Goal: tests pass" in status.detail
    assert "Turns evaluated: 2/5" in status.detail

    cleared = asyncio.run(configure_goal(coordinator, "stop", expand_mentions=lambda text: text))
    assert cleared.ok and cleared.action == "cleared"
    assert coordinator.session_state["goal"] is None


def test_unsupported_orchestrator_and_expansion_failure_never_arm_goal() -> None:
    unsupported = _Coordinator(events=())
    result = asyncio.run(configure_goal(unsupported, "finish", expand_mentions=lambda text: text))
    assert not result.ok and "does not advertise" in result.detail
    assert "goal" not in unsupported.session_state

    supported = _Coordinator()

    async def fail(_text: str) -> str:
        raise RuntimeError("mention resolver failed")

    result = asyncio.run(configure_goal(supported, "finish", expand_mentions=fail))
    assert not result.ok and "mention resolver failed" in result.detail
    assert "goal" not in supported.session_state


def test_clear_matching_goal_only_rolls_back_unadmitted_matching_state() -> None:
    coordinator = _Coordinator()
    result = asyncio.run(configure_goal(coordinator, "finish", expand_mentions=lambda text: text))
    clear_matching_goal(coordinator, result)
    assert coordinator.session_state["goal"] is None

    coordinator.session_state["goal"] = {
        "condition": "finish",
        "cap": None,
        "turns_used": 1,
    }
    clear_matching_goal(coordinator, result)
    assert coordinator.session_state["goal"] is not None
