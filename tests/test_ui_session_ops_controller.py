"""SessionOpsController unit tests — no full Textual App required.

The controller (extracted from ``ui/app.py``, issue #31) drives the live
in-session ops (``/status /model /effort /compact /clear /tools /agents
/diff /skills /skill /mcp``) through the narrow ``SessionOpsHost``
protocol. These tests satisfy it with a plain in-memory fake host + fake
adapter — the same "no Textual involved" discipline the command tests use
with ``FakeCommandContext`` — so the extracted seam is provably testable
without booting the app.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from amplifier_app_tui.kernel.compaction import CompactionConfig
from amplifier_app_tui.kernel.goal import GoalCommandResult
from amplifier_app_tui.kernel.mcp_prompts import MCPPromptInfo
from amplifier_app_tui.kernel.session_ops import ModelListing, SkillInfo, StatusInfo
from amplifier_app_tui.model.blocks import BlockIdAllocator, TranscriptBlock
from amplifier_app_tui.ui.session_ops_controller import SessionOpsController


class _FakeAdapter:
    """The RuntimeAdapter surface the controller touches — in memory."""

    def __init__(self) -> None:
        self.bundle_name = "dev-bundle"
        self.bundle_uri = "file:///workspace/dev-bundle/bundle.md"
        self.session_short = "a1b2c3"
        self.compaction = CompactionConfig()
        self.calls: list[str] = []
        self.tools: tuple[str, ...] = ("read", "bash")
        self.agents: tuple[str, ...] = ("zen-architect",)
        self.skills: tuple[SkillInfo, ...] = (
            SkillInfo(name="cranky-old-sam", description="a reviewer", shortcut="cosam"),
        )
        self.models = ModelListing(provider="anthropic", current="m1", available=("m1", "m2"))
        self.status_info = StatusInfo(
            session_id="sess123456", provider="anthropic", model="m1", messages=3, tools=2
        )
        self.effort = "high"
        self.patch = "diff --git a/x b/x\n+added line\n-removed line\n"
        self.set_model_result: tuple[bool, str] = (True, "m2")
        self.set_effort_result: tuple[bool, str] = (True, "medium")
        self.compact_result: tuple[bool, str] = (True, "9 -> 1 messages")
        self.clear_result: tuple[bool, int] = (True, 4)
        self.goal_result = GoalCommandResult(True, "status", "No goal active.")
        self.compact_hangs = False
        self.clear_hangs = False
        self.interrupt_result = True
        self.load_skill_result: tuple[bool, str] = (True, "# skill body")
        self.mcp_server_summaries: dict[str, str] = {}
        self.mcp_add_result: tuple[bool, str] = (True, "mcp docs · connected live")
        self.mcp_reload_result: tuple[bool, str] = (True, "mcp docs · reloaded live")
        self.mcp_remove_result: tuple[bool, str] = (True, "mcp docs · disconnected live")
        self.mcp_prompt_catalog: tuple[MCPPromptInfo, ...] = (
            MCPPromptInfo("/github:triage", "github", "triage", "Triage one issue"),
        )
        self.mcp_prompt_result: tuple[bool, str] = (True, "[user]\nTriage #42")
        self.deferred: tuple[str, ...] = ("git+https://x/heavy@main",)
        self.load_bundle_result: tuple[bool, str] = (True, "loaded · heavy · 2 module(s) mounted")
        self.load_module_result: tuple[bool, str] = (
            True,
            "loaded · tool-extra · 1 module(s) mounted",
        )

    async def status(self) -> StatusInfo:
        self.calls.append("status")
        return self.status_info

    async def set_model(self, model: str) -> tuple[bool, str]:
        self.calls.append(f"set_model:{model}")
        return self.set_model_result

    async def list_models(self) -> ModelListing:
        self.calls.append("list_models")
        return self.models

    async def set_effort(self, level: str) -> tuple[bool, str]:
        self.calls.append(f"set_effort:{level}")
        return self.set_effort_result

    async def get_effort(self) -> str:
        self.calls.append("get_effort")
        return self.effort

    async def compact(self, focus: str) -> tuple[bool, str]:
        self.calls.append(f"compact:{focus}")
        if self.compact_hangs:
            await asyncio.Event().wait()
        return self.compact_result

    async def clear_context(self) -> tuple[bool, int]:
        self.calls.append("clear_context")
        if self.clear_hangs:
            await asyncio.Event().wait()
        return self.clear_result

    async def manage_goal(self, args: str) -> GoalCommandResult:
        self.calls.append(f"manage_goal:{args}")
        return self.goal_result

    async def interrupt(self) -> bool:
        self.calls.append("interrupt")
        return self.interrupt_result

    async def list_tools(self) -> tuple[str, ...]:
        self.calls.append("list_tools")
        return self.tools

    async def list_agents(self) -> tuple[str, ...]:
        self.calls.append("list_agents")
        return self.agents

    async def diff(self, staged: bool) -> str:
        self.calls.append(f"diff:{staged}")
        return self.patch

    async def list_skills(self) -> tuple[SkillInfo, ...]:
        self.calls.append("list_skills")
        return self.skills

    async def load_skill(self, name: str) -> tuple[bool, str]:
        self.calls.append(f"load_skill:{name}")
        return self.load_skill_result

    async def mcp_tools(self) -> tuple[str, ...]:
        self.calls.append("mcp_tools")
        return ()

    async def mcp_prompts(self) -> tuple[MCPPromptInfo, ...]:
        self.calls.append("mcp_prompts")
        return self.mcp_prompt_catalog

    async def execute_mcp_prompt(self, server: str, prompt: str, args: str) -> tuple[bool, str]:
        self.calls.append(f"execute_mcp_prompt:{server}:{prompt}:{args}")
        return self.mcp_prompt_result

    async def mcp_servers(self) -> dict[str, str]:
        self.calls.append("mcp_servers")
        return dict(self.mcp_server_summaries)

    async def add_mcp_server(
        self, name: str, command: str, args: tuple[str, ...]
    ) -> tuple[bool, str]:
        self.calls.append(f"add_mcp_server:{name}:{command}:{' '.join(args)}")
        return self.mcp_add_result

    async def reload_mcp_server(self, name: str) -> tuple[bool, str]:
        self.calls.append(f"reload_mcp_server:{name}")
        return self.mcp_reload_result

    async def remove_mcp_server(self, name: str) -> tuple[bool, str]:
        self.calls.append(f"remove_mcp_server:{name}")
        return self.mcp_remove_result

    async def deferred_bundles(self) -> tuple[str, ...]:
        self.calls.append("deferred_bundles")
        return self.deferred

    async def load_deferred_bundle(self, name: str) -> tuple[bool, str]:
        self.calls.append(f"load_deferred_bundle:{name}")
        return self.load_bundle_result

    async def load_module(self, module_id: str, source_hint: str = "") -> tuple[bool, str]:
        self.calls.append(f"load_module:{module_id}:{source_hint}")
        return self.load_module_result


class _FakeHost:
    """A SessionOpsHost that is emphatically NOT a Textual App."""

    def __init__(self, adapter: _FakeAdapter, *, splash_active: bool = False) -> None:
        self.adapter = adapter
        self.allocator = BlockIdAllocator()
        self.mode_id = "auto"
        self.session_cost = Decimal("1.50")
        self.splash_active = splash_active
        self.turn_active = False
        self.submit_pending = False
        self.context_restore_pending = False
        self.turn_idle_hangs = False
        self.blocks: list[TranscriptBlock] = []
        self.notices: list[str] = []
        self.status_refreshes = 0
        self.workers_run = 0
        self.effort_indicator: list[str | None] = []
        self.transcript_view_clears = 0
        self.turn_idle_waits = 0
        self.skill_refreshes: list[tuple[SkillInfo, ...]] = []
        self.mcp_prompt_refreshes: list[tuple[MCPPromptInfo, ...]] = []
        self.generated_prompts: list[str] = []

    def run_worker(self, work: Any, *, exclusive: bool = False) -> None:
        # The app schedules the async body on its loop; here we just run it
        # to completion so the assertions see the finished effect.
        self.workers_run += 1
        asyncio.run(work)

    def append_block(self, block: TranscriptBlock) -> None:
        self.blocks.append(block)

    def show_notice(self, text: str, duration: float | None = None) -> None:
        self.notices.append(text)

    def clear_transcript_view(self) -> None:
        self.transcript_view_clears += 1

    async def wait_for_turn_idle(self) -> None:
        self.turn_idle_waits += 1
        if self.turn_idle_hangs:
            await asyncio.Event().wait()
        self.turn_active = False

    def refresh_status(self) -> None:
        self.status_refreshes += 1

    def refresh_skill_commands(self, skills: tuple[SkillInfo, ...]) -> None:
        self.skill_refreshes.append(skills)

    def refresh_mcp_prompt_commands(self, prompts: tuple[MCPPromptInfo, ...]) -> None:
        self.mcp_prompt_refreshes.append(prompts)

    def submit_or_queue_generated_prompt(self, text: str) -> None:
        self.generated_prompts.append(text)

    def set_effort_indicator(self, level: str | None) -> None:
        self.effort_indicator.append(level)


def _text(block: TranscriptBlock) -> str:
    return "".join(seg.text for seg in block.spans)  # type: ignore[attr-defined]


@pytest.fixture
def host() -> _FakeHost:
    return _FakeHost(_FakeAdapter())


@pytest.fixture
def controller(host: _FakeHost) -> SessionOpsController:
    return SessionOpsController(host)


def test_controller_needs_no_textual_app(host: _FakeHost) -> None:
    from textual.app import App

    assert not isinstance(host, App)  # the whole point of the extraction
    SessionOpsController(host).show_tools()
    assert host.blocks  # it still worked


def test_show_tools_appends_roster(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.show_tools()
    assert host.adapter.calls == ["list_tools"]
    assert len(host.blocks) == 1
    body = _text(host.blocks[0])
    assert "Tools" in body and "read" in body and "bash" in body


def test_show_tools_empty(controller: SessionOpsController, host: _FakeHost) -> None:
    host.adapter.tools = ()
    controller.show_tools()
    assert "no tools mounted" in _text(host.blocks[0])


def test_show_agents_appends_roster(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.show_agents()
    assert host.adapter.calls == ["list_agents"]
    assert "Agents" in _text(host.blocks[0])
    assert "zen-architect" in _text(host.blocks[0])


def test_show_status_appends_block(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.show_status()
    assert host.adapter.calls == ["status"]
    body = _text(host.blocks[0])
    assert "Status" in body and "$1.50" in body
    # D4 gap 1: /status shows the FULL resolved bundle URI, not the short
    # name -- the two differ here specifically so this can't pass by accident.
    assert host.adapter.bundle_uri in body
    assert host.adapter.bundle_uri != host.adapter.bundle_name


def test_show_model_no_arg_lists(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.show_model("")
    assert host.adapter.calls == ["list_models"]
    assert "anthropic" in _text(host.blocks[0])


def test_show_model_arg_switches(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.show_model("m2")
    assert host.adapter.calls == ["set_model:m2"]
    assert host.status_refreshes == 1  # footer model field is adapter-derived
    assert host.notices == ["model · m2"]
    assert host.blocks == []


def test_apply_effort_shows_current(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.apply_effort("")
    assert host.adapter.calls == ["get_effort"]
    assert host.notices == ["effort · high · /effort <level> to set"]


def test_apply_effort_sets(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.apply_effort("medium")
    assert host.adapter.calls == ["set_effort:medium"]
    assert host.notices == ["effort · medium"]


def test_apply_effort_sets_updates_footer_indicator(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller.apply_effort("medium")
    # the successful set feeds the footer indicator its canonical tier
    assert host.effort_indicator == ["medium"]


def test_cycle_effort_advances_ring(controller: SessionOpsController, host: _FakeHost) -> None:
    host.adapter.effort = "high"  # next in the ring is xhigh
    host.adapter.set_effort_result = (True, "xhigh")
    controller.cycle_effort()
    assert host.adapter.calls == ["get_effort", "set_effort:xhigh"]
    assert host.effort_indicator == ["xhigh"]
    assert host.notices == ["effort · xhigh"]


def test_cycle_effort_from_unset_enters_ring_at_none(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.adapter.effort = None  # unset -> the ring's first tier is none
    host.adapter.set_effort_result = (True, "none")
    controller.cycle_effort()
    assert host.adapter.calls == ["get_effort", "set_effort:none"]
    assert host.effort_indicator == ["none"]
    assert host.notices == ["effort · none"]


def test_cycle_effort_guards_while_starting() -> None:
    starting = _FakeHost(_FakeAdapter(), splash_active=True)
    SessionOpsController(starting).cycle_effort()
    # the friendly "still starting" notice, no adapter traffic, no indicator
    assert starting.adapter.calls == []
    assert starting.effort_indicator == []
    assert starting.notices == ["session still starting · try again once the banner lands"]


def test_goal_status_renders_native_state(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.adapter.goal_result = GoalCommandResult(
        True,
        "status",
        "Goal: ship it\nTurns evaluated: 2 (unlimited)",
    )

    controller.manage_goal("")

    assert host.adapter.calls == ["manage_goal:"]
    assert "Native goal" in _text(host.blocks[0])
    assert "Goal: ship it" in _text(host.blocks[0])


def test_goal_invalid_cap_fails_before_runtime(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller.manage_goal("--max-turns nope ship it")

    assert host.adapter.calls == []
    assert "must be a non-negative integer" in host.notices[0]


def test_goal_set_uses_native_runtime_and_releases_admission_fence(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.adapter.goal_result = GoalCommandResult(
        True,
        "set",
        "Goal set (max 3 turns).",
        raw_condition="ship it",
        condition="ship it",
        cap=3,
    )

    controller.manage_goal("--max-turns 3 ship it")

    assert host.adapter.calls == ["manage_goal:--max-turns 3 ship it"]
    assert controller.context_operation_pending is False
    assert host.notices == ["goal starting · max 3 turns · /goal stop to clear"]


def test_goal_admitted_releases_only_pre_submit_fence(
    controller: SessionOpsController,
) -> None:
    controller._goal_pending = True
    assert controller.context_operation_pending

    controller.goal_admitted()

    assert controller.context_operation_pending is False


def test_compact_context_notice(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.compact_context("tests")
    assert host.adapter.calls == ["compact:tests"]
    assert host.notices == ["compacted · 9 -> 1 messages"]


def test_context_snapshot_claim_fences_clear_and_compact(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    assert controller.begin_context_snapshot()
    assert controller.context_snapshot_pending
    assert controller.context_operation_pending
    assert controller.context_operation_label == "session snapshot"

    controller.clear_context()
    controller.compact_context("tests")

    assert host.adapter.calls == []
    assert host.notices == [
        "session snapshot in progress · clear unavailable",
        "session snapshot in progress · compact unavailable",
    ]
    controller.finish_context_snapshot()
    assert not controller.context_operation_pending


@pytest.mark.parametrize("busy_state", ["submit", "turn", "restore"])
def test_context_snapshot_requires_idle_session(
    controller: SessionOpsController,
    host: _FakeHost,
    busy_state: str,
) -> None:
    if busy_state == "submit":
        host.submit_pending = True
    elif busy_state == "turn":
        host.turn_active = True
    else:
        host.context_restore_pending = True

    assert not controller.begin_context_snapshot()
    assert not controller.context_snapshot_pending
    assert host.notices == ["session snapshot requires an idle session"]


@pytest.mark.parametrize("busy_state", ["submit", "turn", "restore"])
def test_compact_context_requires_idle_session(
    controller: SessionOpsController,
    host: _FakeHost,
    busy_state: str,
) -> None:
    if busy_state == "submit":
        host.submit_pending = True
    elif busy_state == "turn":
        host.turn_active = True
    else:
        host.context_restore_pending = True

    controller.compact_context("tests")

    assert host.adapter.calls == []
    assert host.notices == ["compact requires an idle session"]


def test_compact_context_rejects_while_clear_is_pending(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller._clear_pending = True

    controller.compact_context("tests")

    assert host.adapter.calls == []
    assert host.notices == ["context clear in progress · compact unavailable"]


def test_compact_context_timeout_releases_its_fence(
    controller: SessionOpsController,
    host: _FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "amplifier_app_tui.ui.session_ops_controller.CONTEXT_MUTATION_TIMEOUT_S",
        0.01,
    )
    host.adapter.compact_hangs = True

    controller.compact_context("tests")

    assert host.adapter.calls == ["compact:tests"]
    assert controller.compact_pending is False
    assert host.notices == ["compact timed out · context state uncertain; retry or restart"]


def test_clear_context_notice(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.clear_context()
    assert host.adapter.calls == ["clear_context"]
    # D3: /clear resets BOTH the context (this notice) AND the rendered
    # view (clear_transcript_view), together, on a successful clear.
    assert host.notices == ["view cleared · 4 messages dropped"]
    assert host.transcript_view_clears == 1


def test_clear_context_failure_leaves_the_view_untouched(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    """D3: a failed/unavailable clear must never wipe the transcript for
    a no-op -- the view-only reset is gated on a confirmed context clear."""
    host.adapter.clear_result = (False, 0)
    controller.clear_context()
    assert host.notices == ["clear unavailable in this session"]
    assert host.transcript_view_clears == 0


def test_clear_context_interrupts_and_waits_while_turn_is_running(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.turn_active = True

    controller.clear_context()

    assert host.adapter.calls == ["interrupt", "clear_context"]
    assert host.turn_idle_waits == 1
    assert host.transcript_view_clears == 1
    assert host.notices == [
        "interrupting turn to clear context …",
        "view cleared · 4 messages dropped",
    ]


def test_clear_context_rejects_while_submit_awaits_admission(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.submit_pending = True

    controller.clear_context()

    assert host.adapter.calls == []
    assert host.transcript_view_clears == 0
    assert host.notices == ["clear requires an idle session · esc to interrupt, then retry"]


def test_clear_context_rejects_while_checkpoint_restore_owns_context(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.context_restore_pending = True

    controller.clear_context()

    assert host.adapter.calls == []
    assert host.transcript_view_clears == 0
    assert host.notices == ["checkpoint restore in progress · clear unavailable"]


def test_clear_context_rejects_while_compaction_owns_context(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller._compact_pending = True

    controller.clear_context()

    assert host.adapter.calls == []
    assert host.transcript_view_clears == 0
    assert host.notices == ["context compaction in progress · clear unavailable"]


def test_clear_context_timeout_releases_its_fence_without_clearing_view(
    controller: SessionOpsController,
    host: _FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "amplifier_app_tui.ui.session_ops_controller.CONTEXT_MUTATION_TIMEOUT_S",
        0.01,
    )
    host.adapter.clear_hangs = True

    controller.clear_context()

    assert host.adapter.calls == ["clear_context"]
    assert host.transcript_view_clears == 0
    assert controller.clear_pending is False
    assert host.notices == ["clear timed out · view kept; context state uncertain"]


def test_clear_context_leaves_state_when_active_turn_rejects_interrupt(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.turn_active = True
    host.adapter.interrupt_result = False

    controller.clear_context()

    assert host.adapter.calls == ["interrupt"]
    assert host.turn_idle_waits == 0
    assert host.transcript_view_clears == 0
    assert controller.clear_pending is False
    assert host.notices == [
        "interrupting turn to clear context …",
        "clear could not interrupt the active turn · retry",
    ]


def test_clear_context_times_out_if_interrupted_turn_never_closes(
    controller: SessionOpsController,
    host: _FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "amplifier_app_tui.ui.session_ops_controller.CLEAR_INTERRUPT_TIMEOUT_S",
        0.01,
    )
    host.turn_active = True
    host.turn_idle_hangs = True

    controller.clear_context()

    assert host.adapter.calls == ["interrupt"]
    assert host.turn_idle_waits == 1
    assert host.transcript_view_clears == 0
    assert controller.clear_pending is False
    assert host.notices == [
        "interrupting turn to clear context …",
        "clear timed out waiting for the active turn · context unchanged",
    ]


def test_repeated_clear_context_clears_the_view_every_time(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    """AC5 repeated clear: back-to-back ``/clear`` each reset the view."""
    controller.clear_context()
    controller.clear_context()
    assert host.transcript_view_clears == 2
    assert host.notices == ["view cleared · 4 messages dropped"] * 2


def test_show_diff_unstaged(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.show_diff("")
    assert host.adapter.calls == ["diff:False"]
    assert "added line" in _text(host.blocks[0])


def test_show_diff_staged_arg(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.show_diff("staged")
    assert host.adapter.calls == ["diff:True"]


def test_show_skills_roster(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.show_skills()
    assert host.adapter.calls == ["list_skills"]
    assert "Skills" in _text(host.blocks[0])


def test_load_skill_requires_name(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_skill("")
    assert host.adapter.calls == []  # never reached the coordinator
    assert host.workers_run == 0
    assert host.notices == ["usage: /skill <name> · /skills lists them"]


def test_load_skill_loads(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_skill("cranky-old-sam")
    assert host.adapter.calls == ["load_skill:cranky-old-sam"]
    assert "Skill loaded" in _text(host.blocks[0])
    assert host.notices == ["skill loaded · cranky-old-sam"]
    assert host.generated_prompts == [
        "Apply the active /cranky-old-sam skill now and complete its requested output."
    ]


def test_ops_starting_gates_the_coordinator() -> None:
    host = _FakeHost(_FakeAdapter(), splash_active=True)
    controller = SessionOpsController(host)
    controller.compact_context("x")
    assert host.adapter.calls == []  # gated before any worker ran
    assert host.workers_run == 0
    assert host.notices == ["session still starting · try again once the banner lands"]


def test_manage_mcp_add_usage(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.manage_mcp("add only-two")
    assert host.notices == ["usage: /mcp add <name> <command> [args…]"]
    assert host.blocks == []


def test_manage_mcp_list(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.manage_mcp("")
    assert host.adapter.calls == ["mcp_servers", "mcp_tools", "mcp_prompts"]
    assert host.mcp_prompt_refreshes == [host.adapter.mcp_prompt_catalog]
    assert "MCP" in _text(host.blocks[0])


def test_manage_mcp_add_connects_live_and_preserves_quoted_args(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller.manage_mcp('add docs "docs server" "two words" --stdio')
    assert host.adapter.calls == [
        "add_mcp_server:docs:docs server:two words --stdio",
        "mcp_prompts",
    ]
    assert host.mcp_prompt_refreshes == [host.adapter.mcp_prompt_catalog]
    assert host.notices == ["mcp docs · connected live"]


def test_manage_mcp_reload_and_remove_are_runtime_ops(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller.manage_mcp("reload docs")
    controller.manage_mcp("remove docs")
    assert host.adapter.calls == [
        "reload_mcp_server:docs",
        "mcp_prompts",
        "remove_mcp_server:docs",
        "mcp_prompts",
    ]
    assert host.mcp_prompt_refreshes == [host.adapter.mcp_prompt_catalog] * 2
    assert host.notices == ["mcp docs · reloaded live", "mcp docs · disconnected live"]


def test_native_mcp_prompt_submits_returned_messages(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller.run_mcp_prompt("github", "triage", "#42")

    assert host.adapter.calls == ["execute_mcp_prompt:github:triage:#42"]
    assert host.generated_prompts == ["[user]\nTriage #42"]


def test_native_mcp_prompt_failure_is_a_notice(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.adapter.mcp_prompt_result = (False, "Required MCP prompt arguments: issue")

    controller.run_mcp_prompt("github", "triage", "")

    assert host.generated_prompts == []
    assert host.notices == ["Required MCP prompt arguments: issue"]


# ---------------------------------------------------------------------------
# /bundle — deferred overlay listing + on-demand in-session load (fast boot)
# ---------------------------------------------------------------------------


def test_bundle_bare_lists_deferred(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_bundle("")
    assert host.adapter.calls == ["deferred_bundles"]
    body = _text(host.blocks[0])
    assert "Live-loadable bundles" in body and "heavy" in body


def test_bundle_list_when_none_deferred(controller: SessionOpsController, host: _FakeHost) -> None:
    host.adapter.deferred = ()
    controller.load_bundle("list")
    assert "none discovered" in _text(host.blocks[0])


def test_bundle_load_composes(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_bundle("load heavy")
    assert host.adapter.calls == ["load_deferred_bundle:heavy", "list_skills", "mcp_prompts"]
    assert host.status_refreshes == 1  # mounted tools/agents change the roster
    assert host.skill_refreshes == [host.adapter.skills]
    assert host.mcp_prompt_refreshes == [host.adapter.mcp_prompt_catalog]
    assert host.notices == ["bundle · loaded · heavy · 2 module(s) mounted"]


def test_bundle_load_shorthand(controller: SessionOpsController, host: _FakeHost) -> None:
    # `/bundle heavy` is shorthand for `/bundle load heavy`.
    controller.load_bundle("heavy")
    assert host.adapter.calls == ["load_deferred_bundle:heavy", "list_skills", "mcp_prompts"]


def test_bundle_load_preserves_quoted_local_path(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller.load_bundle('load "/tmp/my bundles/heavy.md"')
    assert host.adapter.calls == [
        "load_deferred_bundle:/tmp/my bundles/heavy.md",
        "list_skills",
        "mcp_prompts",
    ]


def test_bundle_load_missing_name(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_bundle("load")
    assert host.adapter.calls == []
    assert host.notices == ["usage: /bundle load <name-or-uri> · /bundle lists available targets"]


def test_bundle_load_failure_notices(controller: SessionOpsController, host: _FakeHost) -> None:
    host.adapter.load_bundle_result = (False, "'heavy' is not a deferred bundle · deferred: none")
    controller.load_bundle("load heavy")
    assert host.status_refreshes == 0  # nothing mounted
    assert host.notices == ["'heavy' is not a deferred bundle · deferred: none"]


def test_bundle_load_gated_while_starting() -> None:
    host = _FakeHost(_FakeAdapter(), splash_active=True)
    SessionOpsController(host).load_bundle("load heavy")
    assert host.adapter.calls == []
    assert host.notices == ["session still starting · try again once the banner lands"]


# ---------------------------------------------------------------------------
# /module — explicit additive provider/tool/hook loading
# ---------------------------------------------------------------------------


def test_module_load_with_source(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_module("load tool-extra git+https://example.test/tool@abc")
    assert host.adapter.calls == [
        "load_module:tool-extra:git+https://example.test/tool@abc",
        "list_skills",
        "mcp_prompts",
    ]
    assert host.status_refreshes == 1
    assert host.skill_refreshes == [host.adapter.skills]
    assert host.notices == ["module · loaded · tool-extra · 1 module(s) mounted"]


def test_module_load_shorthand(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_module("hook-redaction")
    assert host.adapter.calls == ["load_module:hook-redaction:", "list_skills", "mcp_prompts"]


def test_module_load_usage(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_module("load")
    assert host.adapter.calls == []
    assert host.notices == ["usage: /module load <provider-, tool-, or hook-module> [source-uri]"]


def test_module_load_preserves_quoted_local_source(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    controller.load_module('load tool-extra "/tmp/my modules/tool"')
    assert host.adapter.calls == [
        "load_module:tool-extra:/tmp/my modules/tool",
        "list_skills",
        "mcp_prompts",
    ]


def test_module_load_failure_does_not_refresh(
    controller: SessionOpsController, host: _FakeHost
) -> None:
    host.adapter.load_module_result = (False, "singleton modules attach next session")
    controller.load_module("load orchestrator-loop-streaming")
    assert host.status_refreshes == 0
    assert host.notices == ["singleton modules attach next session"]
