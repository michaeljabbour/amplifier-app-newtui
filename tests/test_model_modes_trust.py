"""Tests for mode profiles/cycling and trust resolution."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.model.modes import (
    DEFAULT_MODE,
    MODE_CYCLE,
    MODE_PROFILES,
    cycle_mode,
    get_mode,
)
from amplifier_app_newtui.model.trust import (
    CapabilityClass,
    DenialLog,
    classify_tool,
    resolve,
)

# --- modes (DESIGN-SPEC §4 table, verbatim) --------------------------------


def test_mode_table_matches_spec_exactly() -> None:
    expected = {
        "chat": ("dim", "ask all · auto read"),
        "plan": ("blue", "read-only"),
        "brainstorm": ("teal", "no tools"),
        "build": ("green", "auto read,test · ask write,net,spend"),
        "auto": ("orange", "auto read,write · classifier-gated"),
    }
    assert set(MODE_PROFILES) == set(expected)
    for mode_id, (color, trust) in expected.items():
        profile = MODE_PROFILES[mode_id]
        assert profile.color_token == color, mode_id
        assert profile.trust_str == trust, mode_id


def test_chat_composer_edge_uses_rule_token() -> None:
    assert MODE_PROFILES["chat"].accent == "rule"
    for mode_id in ("plan", "brainstorm", "build", "auto"):
        assert MODE_PROFILES[mode_id].accent == MODE_PROFILES[mode_id].color_token


def test_mode_change_notice_format() -> None:
    assert MODE_PROFILES["plan"].notice() == "mode plan · read-only"


def test_cycle_visits_all_five_modes_and_wraps() -> None:
    seen = []
    current: str | None = DEFAULT_MODE
    for _ in range(len(MODE_CYCLE)):
        seen.append(current)
        current = cycle_mode(current).id
    assert sorted(seen) == sorted(MODE_CYCLE)
    assert current == DEFAULT_MODE  # full wrap


def test_cycle_backwards() -> None:
    assert cycle_mode("chat", -1).id == MODE_CYCLE[-1]


def test_unknown_mode_falls_back_to_chat() -> None:
    assert get_mode("bogus").id == "chat"
    assert get_mode(None).id == "chat"


# --- trust resolution (DESIGN-SPEC §4 gating) -------------------------------


def test_plan_is_read_only() -> None:
    assert resolve("plan", "read_file").decision == "allow"
    assert resolve("plan", "grep").decision == "allow"
    for tool in ("write_file", "bash", "web_fetch", "task", "run_tests"):
        assert resolve("plan", tool).decision == "deny", tool


def test_brainstorm_has_no_tools() -> None:
    for tool in ("read_file", "write_file", "bash", "web_fetch", "task"):
        assert resolve("brainstorm", tool).decision == "deny", tool


def test_chat_asks_everything_except_reads() -> None:
    assert resolve("chat", "read_file").decision == "allow"
    for tool in ("write_file", "bash", "web_fetch", "task", "run_tests"):
        assert resolve("chat", tool).decision == "ask", tool


def test_build_auto_read_test_ask_write_net_spend() -> None:
    assert resolve("build", "read_file").decision == "allow"
    assert resolve("build", "run_tests").decision == "allow"
    for tool in ("write_file", "web_fetch", "task"):
        assert resolve("build", tool).decision == "ask", tool


def test_build_exec_test_command_is_auto() -> None:
    """A shell call running the test suite classifies as TEST → auto in build."""
    decision = resolve("build", "bash", {"command": "pytest -q"})
    assert decision.capability == CapabilityClass.TEST
    assert decision.decision == "allow"
    # A non-test shell command stays exec → ask.
    assert resolve("build", "bash", {"command": "rm -rf build"}).decision == "ask"


def test_auto_mode_static_allows_and_classifier_gates() -> None:
    assert resolve("auto", "read_file").decision == "allow"
    assert resolve("auto", "write_file").decision == "allow"
    for tool in ("bash", "web_fetch", "task"):
        decision = resolve("auto", tool)
        assert decision.decision == "ask", tool
        assert decision.classifier_gated, tool
    assert not resolve("auto", "read_file").classifier_gated


def test_unknown_mode_uses_chat_posture() -> None:
    assert resolve("bogus", "read_file").decision == "allow"
    assert resolve("bogus", "write_file").decision == "ask"


def test_unknown_tool_fails_safe_as_exec() -> None:
    assert classify_tool("mystery_widget") == CapabilityClass.EXEC
    assert resolve("build", "mystery_widget").decision == "ask"


def test_classify_tool_table_and_heuristics() -> None:
    assert classify_tool("read_file") == CapabilityClass.READ
    assert classify_tool("web_fetch") == CapabilityClass.NET
    assert classify_tool("spawn_agent") == CapabilityClass.SPEND
    assert classify_tool("fancy_file_search") == CapabilityClass.READ  # heuristic


# --- denial log escalation (3 consecutive / 20 total) ------------------------


def test_denial_log_escalates_at_three_consecutive() -> None:
    log = DenialLog()
    records = [
        log.record_denial(capability=CapabilityClass.EXEC, action=f"cmd{i}", reason="blocked")
        for i in range(3)
    ]
    assert not records[0].escalation_due
    assert not records[1].escalation_due
    assert records[2].escalation_due
    assert records[2].escalation_reasons == ("3 consecutive denials",)


def test_non_denial_resets_consecutive_streak() -> None:
    log = DenialLog()
    for i in range(2):
        log.record_denial(capability=CapabilityClass.EXEC, action=f"a{i}", reason="r")
    log.record_non_denial()
    record = log.record_denial(capability=CapabilityClass.EXEC, action="b", reason="r")
    assert record.consecutive_count == 1
    assert not record.escalation_due


def test_denial_log_escalates_at_twenty_total() -> None:
    log = DenialLog()
    escalations: list[tuple[str, ...]] = []
    for i in range(20):
        log.record_non_denial()  # keep the consecutive streak at 1
        record = log.record_denial(
            capability=CapabilityClass.NET, action=f"fetch{i}", reason="r"
        )
        escalations.append(record.escalation_reasons)
    assert escalations[-1] == ("20 total denials",)
    assert all(not reasons for reasons in escalations[:-1])


def test_denial_log_requires_reason() -> None:
    log = DenialLog()
    with pytest.raises(ValueError):
        log.record_denial(capability=CapabilityClass.EXEC, action="x", reason="   ")
