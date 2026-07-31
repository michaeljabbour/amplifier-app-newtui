"""Governance-map registration for the Code Mode ``execute`` tool."""

from __future__ import annotations

from amplifier_app_tui.model.trust import (
    CapabilityClass,
    classify_tool,
    resolve,
    tool_capability_map,
)


def test_execute_is_registered_as_exec_in_the_governance_map() -> None:
    assert tool_capability_map()["execute"] is CapabilityClass.EXEC
    assert classify_tool("execute") is CapabilityClass.EXEC


def test_execute_asks_in_interactive_modes() -> None:
    # A code-mode program can call any tool, so it is gated like exec.
    assert resolve("chat", "execute").decision == "ask"
    build = resolve("build", "execute")
    assert build.decision == "ask"
    assert build.capability is CapabilityClass.EXEC


def test_execute_is_classifier_gated_in_auto() -> None:
    decision = resolve("auto", "execute")
    assert decision.decision == "ask"
    assert decision.classifier_gated is True


def test_execute_is_denied_in_read_only_modes() -> None:
    assert resolve("plan", "execute").decision == "deny"
    assert resolve("brainstorm", "execute").decision == "deny"
