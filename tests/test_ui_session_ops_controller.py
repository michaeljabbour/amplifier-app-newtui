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
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_tui.kernel.compaction import CompactionConfig
from amplifier_app_tui.kernel.session_ops import ModelListing, SkillInfo, StatusInfo
from amplifier_app_tui.model.blocks import BlockIdAllocator, TranscriptBlock
from amplifier_app_tui.ui.session_ops_controller import SessionOpsController


class _FakeAdapter:
    """The RuntimeAdapter surface the controller touches — in memory."""

    def __init__(self) -> None:
        self.bundle_name = "dev-bundle"
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
        self.load_skill_result: tuple[bool, str] = (True, "# skill body")
        self.deferred: tuple[str, ...] = ("git+https://x/heavy@main",)
        self.load_bundle_result: tuple[bool, str] = (True, "loaded · heavy · 2 module(s) mounted")

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
        return self.compact_result

    async def clear_context(self) -> tuple[bool, int]:
        self.calls.append("clear_context")
        return self.clear_result

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

    async def deferred_bundles(self) -> tuple[str, ...]:
        self.calls.append("deferred_bundles")
        return self.deferred

    async def load_deferred_bundle(self, name: str) -> tuple[bool, str]:
        self.calls.append(f"load_deferred_bundle:{name}")
        return self.load_bundle_result


class _FakeHost:
    """A SessionOpsHost that is emphatically NOT a Textual App."""

    def __init__(self, adapter: _FakeAdapter, *, splash_active: bool = False) -> None:
        self.adapter = adapter
        self.allocator = BlockIdAllocator()
        self.mode_id = "auto"
        self.session_cost = Decimal("1.50")
        self.splash_active = splash_active
        self.blocks: list[TranscriptBlock] = []
        self.notices: list[str] = []
        self.status_refreshes = 0
        self.workers_run = 0
        self.effort_indicator: list[str | None] = []

    def run_worker(self, work: Any, *, exclusive: bool = False) -> None:
        # The app schedules the async body on its loop; here we just run it
        # to completion so the assertions see the finished effect.
        self.workers_run += 1
        asyncio.run(work)

    def append_block(self, block: TranscriptBlock) -> None:
        self.blocks.append(block)

    def show_notice(self, text: str, duration: float | None = None) -> None:
        self.notices.append(text)

    def refresh_status(self) -> None:
        self.status_refreshes += 1

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
    assert "Status" in body and "dev-bundle" in body and "$1.50" in body


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


def test_compact_context_notice(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.compact_context("tests")
    assert host.adapter.calls == ["compact:tests"]
    assert host.notices == ["compacted · 9 -> 1 messages"]


def test_clear_context_notice(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.clear_context()
    assert host.adapter.calls == ["clear_context"]
    assert host.notices == ["context cleared · 4 messages dropped"]


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


def test_manage_mcp_list(controller: SessionOpsController, host: _FakeHost, monkeypatch) -> None:
    from amplifier_app_tui.kernel import mcp_config

    monkeypatch.setattr(mcp_config, "mcp_config_path", lambda: Path("/tmp/none.json"))
    monkeypatch.setattr(mcp_config, "read_servers", lambda path: {})
    controller.manage_mcp("")
    assert "mcp_tools" in host.adapter.calls
    assert "MCP" in _text(host.blocks[0])


# ---------------------------------------------------------------------------
# /bundle — deferred overlay listing + on-demand in-session load (fast boot)
# ---------------------------------------------------------------------------


def test_bundle_bare_lists_deferred(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_bundle("")
    assert host.adapter.calls == ["deferred_bundles"]
    body = _text(host.blocks[0])
    assert "Deferred overlays" in body and "heavy" in body


def test_bundle_list_when_none_deferred(controller: SessionOpsController, host: _FakeHost) -> None:
    host.adapter.deferred = ()
    controller.load_bundle("list")
    assert "none deferred" in _text(host.blocks[0])


def test_bundle_load_composes(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_bundle("load heavy")
    assert host.adapter.calls == ["load_deferred_bundle:heavy"]
    assert host.status_refreshes == 1  # mounted tools/agents change the roster
    assert host.notices == ["bundle · loaded · heavy · 2 module(s) mounted"]


def test_bundle_load_shorthand(controller: SessionOpsController, host: _FakeHost) -> None:
    # `/bundle heavy` is shorthand for `/bundle load heavy`.
    controller.load_bundle("heavy")
    assert host.adapter.calls == ["load_deferred_bundle:heavy"]


def test_bundle_load_missing_name(controller: SessionOpsController, host: _FakeHost) -> None:
    controller.load_bundle("load")
    assert host.adapter.calls == []
    assert host.notices == ["usage: /bundle load <name> · /bundle lists deferred"]


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
