"""tool-recipes approval-gate bridge (kernel/recipes.py).

The native contract being faked here is verbatim from
amplifier-bundle-recipes ``modules/tool-recipes``:

- gate pause: ``hooks.emit("recipe:approval", {name, description,
  current_step, total_steps, steps, status: "waiting_approval", prompt,
  stage_name})`` then the recipes TOOL CALL returns ``{"status":
  "paused_for_approval", "recipe", "session_id", "stage_name",
  "approval_prompt", ...}`` (executor.py / __init__.py);
- resume: exclusively via further tool operations — ``approvals`` (list
  pending, incl. ``session_id``/``approval_requested_at``), ``approve`` /
  ``deny`` (write the decision), ``resume`` (re-run the executor).

Unit tests drive :class:`RecipeApprovalBridge` against a fake recipes
tool speaking exactly that contract; the offline test runs the whole
round-trip through a REAL foundation lifecycle (fake modules via a
``file://`` bundle, no network — test_runtime_offline.py style).
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_newtui.kernel.approval import (
    ALLOW_ONCE,
    DENY,
    STANDARD_OPTIONS,
    ApprovalBroker,
)
from amplifier_app_newtui.kernel.events import normalize, recipe_approval_prompt
from amplifier_app_newtui.kernel.recipes import RecipeApprovalBridge
from amplifier_app_newtui.kernel.runtime import RealRuntime

GATE_PAYLOAD: dict[str, Any] = {
    "name": "demo-flow",
    "description": "two-stage demo recipe",
    "current_step": 0,
    "total_steps": 2,
    "steps": [
        {"id": "plan", "status": "waiting_approval", "is_approval_gate": True},
        {"id": "ship", "status": "pending"},
    ],
    "status": "waiting_approval",
    "prompt": "Ship the plan?",
    "stage_name": "plan",
}
"""Verbatim ``recipe:approval`` payload shape (executor.py
``_build_recipe_event_data`` with the gate's prompt/stage extras)."""


class FakeRecipesTool:
    """Speaks the recipes tool's operation contract; records every call."""

    name = "recipes"

    def __init__(self, pending: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = list(
            pending
            if pending is not None
            else [
                {
                    "session_id": "recipe_20260721_100000_a1b2",
                    "recipe_name": "demo-flow",
                    "stage_name": "plan",
                    "approval_prompt": "Ship the plan?",
                    "approval_timeout": 0,
                    "approval_requested_at": "2026-07-21T10:00:00",
                    "approval_default": "deny",
                }
            ]
        )

    async def execute(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(tool_input))
        operation = tool_input.get("operation")
        if operation == "approvals":
            return {
                "success": True,
                "output": {"pending_approvals": list(self.pending), "count": len(self.pending)},
            }
        if operation == "approve":
            # The real module keeps the pending entry until resume clears it.
            return {"success": True, "output": {"status": "approved"}}
        if operation == "deny":
            self.pending = [
                p for p in self.pending if p["session_id"] != tool_input.get("session_id")
            ]
            return {"success": True, "output": {"status": "denied"}}
        if operation == "resume":
            self.pending = [
                p for p in self.pending if p["session_id"] != tool_input.get("session_id")
            ]
            return {
                "success": True,
                "output": {
                    "status": "completed",
                    "recipe": "demo-flow",
                    "session_id": tool_input.get("session_id"),
                    "summary": "done",
                },
            }
        raise AssertionError(f"unexpected operation: {operation}")

    def ops(self) -> list[str]:
        return [str(call.get("operation")) for call in self.calls]


def _bridge(
    tool: FakeRecipesTool,
    events: list,
    *,
    executing: Any = None,
) -> tuple[RecipeApprovalBridge, ApprovalBroker]:
    broker = ApprovalBroker()
    bridge = RecipeApprovalBridge(
        broker=broker,
        tools=lambda: {"recipes": tool},
        emit=events.append,
        is_executing=executing if executing is not None else (lambda: False),
        idle_poll_seconds=0.01,
    )
    return bridge, broker


async def _wait_head(broker: ApprovalBroker):
    for _ in range(500):
        if broker.head is not None:
            return broker.head
        await asyncio.sleep(0.01)
    raise AssertionError("no approval ticket appeared")


async def _settled(bridge: RecipeApprovalBridge) -> None:
    while bridge._tasks:
        await asyncio.gather(*tuple(bridge._tasks))


# --------------------------------------------------------------------------
# Normalization (the queue-bridge record path)
# --------------------------------------------------------------------------


def test_normalize_recipe_approval_to_approval_required() -> None:
    event = normalize("recipe:approval", {**GATE_PAYLOAD, "session_id": "amp-sess-1"})
    assert event is not None
    assert event.kind == "approval_required"
    assert event.session_id == "amp-sess-1"
    assert event.prompt == "Recipe 'demo-flow' · stage 'plan' — Ship the plan?"
    # No options ride the native payload; the record states the verbatim
    # fail-closed triple the broker presents.
    assert event.options == STANDARD_OPTIONS


def test_recipe_approval_prompt_defaults_without_prompt_or_name() -> None:
    assert (
        recipe_approval_prompt({"stage_name": "plan"})
        == "Recipe 'recipe' · stage 'plan' — Approve completion of stage 'plan'?"
    )
    assert recipe_approval_prompt({}) == "Recipe 'recipe' — Approve to continue?"


# --------------------------------------------------------------------------
# Bridge round-trip (fake tool, real broker)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_routes_approve_then_resume() -> None:
    tool = FakeRecipesTool()
    events: list = []
    bridge, broker = _bridge(tool, events)

    result = await bridge.handle_event("recipe:approval", dict(GATE_PAYLOAD))
    assert result.action == "continue"

    head = await _wait_head(broker)
    assert head.prompt == "Recipe 'demo-flow' · stage 'plan' — Ship the plan?"
    assert head.options[:3] == STANDARD_OPTIONS
    assert head.detail.tool_name == "recipes"
    assert head.detail.rule == "recipe approval gate"

    broker.answer(head.ticket_id, ALLOW_ONCE)
    await _settled(bridge)

    assert tool.ops() == ["approvals", "approve", "resume"]
    approve = tool.calls[1]
    assert approve["session_id"] == "recipe_20260721_100000_a1b2"
    assert approve["stage_name"] == "plan"
    assert tool.calls[2]["session_id"] == "recipe_20260721_100000_a1b2"
    assert any("approved and resumed" in getattr(e, "message", "") for e in events)


@pytest.mark.asyncio
async def test_deny_routes_deny_operation() -> None:
    tool = FakeRecipesTool()
    events: list = []
    bridge, broker = _bridge(tool, events)

    await bridge.handle_event("recipe:approval", dict(GATE_PAYLOAD))
    head = await _wait_head(broker)
    broker.answer(head.ticket_id, DENY)
    await _settled(bridge)

    assert tool.ops() == ["approvals", "deny"]
    deny = tool.calls[1]
    assert deny["session_id"] == "recipe_20260721_100000_a1b2"
    assert deny["stage_name"] == "plan"
    assert deny["reason"] == "Denied via approval bar"
    assert any("denied" in getattr(e, "message", "") for e in events)


@pytest.mark.asyncio
async def test_nested_gate_prefers_newest_pending_entry() -> None:
    """A sub-recipe gate is mirrored onto the parent session under the SAME
    stage name, written child-first; only the parent's approve/resume
    forward down the chain — the bridge must pick the newest entry, not
    the one whose recipe_name matches the event (the event carries the
    CHILD recipe's name)."""
    tool = FakeRecipesTool(
        pending=[
            {
                "session_id": "recipe_child",
                "recipe_name": "demo-flow",  # matches the event's name
                "stage_name": "plan",
                "approval_prompt": "Ship the plan?",
                "approval_requested_at": "2026-07-21T10:00:00",
            },
            {
                "session_id": "recipe_parent",
                "recipe_name": "outer-flow",
                "stage_name": "plan",
                "approval_prompt": "Ship the plan?",
                "approval_requested_at": "2026-07-21T10:00:01",  # mirrored after
            },
        ]
    )
    events: list = []
    bridge, broker = _bridge(tool, events)

    await bridge.handle_event("recipe:approval", dict(GATE_PAYLOAD))
    head = await _wait_head(broker)
    broker.answer(head.ticket_id, ALLOW_ONCE)
    await _settled(bridge)

    assert tool.calls[1]["operation"] == "approve"
    assert tool.calls[1]["session_id"] == "recipe_parent"


@pytest.mark.asyncio
async def test_already_settled_gate_notifies_instead_of_acting() -> None:
    tool = FakeRecipesTool(pending=[])
    events: list = []
    bridge, broker = _bridge(tool, events)

    await bridge.handle_event("recipe:approval", dict(GATE_PAYLOAD))
    head = await _wait_head(broker)
    broker.answer(head.ticket_id, ALLOW_ONCE)
    await _settled(bridge)

    assert tool.ops() == ["approvals"]
    assert any("already settled" in getattr(e, "message", "") for e in events)


@pytest.mark.asyncio
async def test_resume_waits_for_the_live_turn_to_finish() -> None:
    """The paused_for_approval tool result tells the MODEL to approve/resume
    too — resuming mid-turn risks a double executor run, so the bridge
    approves immediately but resumes only once the turn is idle."""
    tool = FakeRecipesTool()
    events: list = []
    executing = {"live": True}
    bridge, broker = _bridge(tool, events, executing=lambda: executing["live"])

    await bridge.handle_event("recipe:approval", dict(GATE_PAYLOAD))
    head = await _wait_head(broker)
    broker.answer(head.ticket_id, ALLOW_ONCE)

    for _ in range(50):
        if "approve" in tool.ops():
            break
        await asyncio.sleep(0.01)
    assert "approve" in tool.ops()
    await asyncio.sleep(0.05)
    assert "resume" not in tool.ops()  # still mid-turn

    executing["live"] = False
    await _settled(bridge)
    assert tool.ops() == ["approvals", "approve", "resume"]


@pytest.mark.asyncio
async def test_missing_recipes_tool_is_a_loud_notification() -> None:
    broker = ApprovalBroker()
    events: list = []
    bridge = RecipeApprovalBridge(
        broker=broker,
        tools=lambda: {},
        emit=events.append,
        is_executing=lambda: False,
    )
    await bridge.handle_event("recipe:approval", dict(GATE_PAYLOAD))
    head = await _wait_head(broker)
    broker.answer(head.ticket_id, ALLOW_ONCE)
    await _settled(bridge)

    assert any(
        "no recipes tool" in getattr(e, "message", "") and getattr(e, "level", "") == "error"
        for e in events
    )


# --------------------------------------------------------------------------
# Offline end-to-end: real foundation lifecycle, fake recipes module
# --------------------------------------------------------------------------

_PROVIDER_MODULE = '''
"""Fake provider (recipes offline tests)."""


class FakeProvider:
    name = "rfake"

    def __init__(self, config):
        self.config = dict(config or {})

    def get_info(self):
        from amplifier_core import ProviderInfo

        return ProviderInfo(id="rfake", display_name="Recipes Fake Provider")

    async def list_models(self):
        from amplifier_core import ModelInfo

        return [ModelInfo(id="rfake-model", display_name="Recipes Fake Model")]

    async def complete(self, request=None, **kwargs):
        return {"content": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}

    def parse_tool_calls(self, response):
        return []


async def mount(coordinator, config=None):
    await coordinator.mount("providers", FakeProvider(config), name="rfake")
    return None
'''

_CONTEXT_MODULE = '''
"""Fake context (recipes offline tests)."""


class FakeContext:
    def __init__(self, config):
        self._messages = []

    async def add_message(self, message):
        self._messages.append(dict(message))

    async def get_messages(self):
        return list(self._messages)

    async def set_messages(self, messages):
        self._messages = [dict(m) for m in messages]

    async def get_messages_for_request(self):
        return list(self._messages)

    async def clear(self):
        self._messages = []


async def mount(coordinator, config=None):
    await coordinator.mount("context", FakeContext(config))
    return None
'''

_RECIPES_TOOL_MODULE = '''
"""Fake tool-recipes (recipes offline tests).

Mirrors the real module's gate contract: ``execute`` persists a pending
approval, emits ``recipe:approval`` through coordinator.hooks (the same
awaited ``hooks.emit`` the real executor uses in ``_show_progress``),
and returns ``paused_for_approval``; ``approvals``/``approve``/``deny``/
``resume`` are the only way to settle the gate.
"""


class FakeRecipesTool:
    name = "recipes"
    description = "Fake tool-recipes (records operations)."
    input_schema = {
        "type": "object",
        "properties": {"operation": {"type": "string"}},
        "required": ["operation"],
    }

    def __init__(self, coordinator, config):
        self.coordinator = coordinator
        self.config = dict(config or {})
        self.calls = []
        self.pending = []

    async def execute(self, tool_input):
        from amplifier_core import ToolResult

        operation = tool_input.get("operation")
        self.calls.append(dict(tool_input))
        if operation == "execute":
            self.pending.append(
                {
                    "session_id": "recipe_20260721_100000_a1b2",
                    "recipe_name": "demo-flow",
                    "stage_name": "plan",
                    "approval_prompt": "Ship the plan?",
                    "approval_timeout": 0,
                    "approval_requested_at": "2026-07-21T10:00:00",
                    "approval_default": "deny",
                }
            )
            await self.coordinator.hooks.emit(
                "recipe:approval",
                {
                    "name": "demo-flow",
                    "description": "two-stage demo recipe",
                    "current_step": 0,
                    "total_steps": 2,
                    "steps": [],
                    "status": "waiting_approval",
                    "prompt": "Ship the plan?",
                    "stage_name": "plan",
                },
            )
            return ToolResult(
                success=True,
                output={
                    "status": "paused_for_approval",
                    "recipe": "demo-flow",
                    "session_id": "recipe_20260721_100000_a1b2",
                    "stage_name": "plan",
                    "approval_prompt": "Ship the plan?",
                },
            )
        if operation == "approvals":
            return ToolResult(
                success=True,
                output={"pending_approvals": list(self.pending), "count": len(self.pending)},
            )
        if operation == "approve":
            return ToolResult(success=True, output={"status": "approved"})
        if operation == "deny":
            self.pending = []
            return ToolResult(success=True, output={"status": "denied"})
        if operation == "resume":
            self.pending = []
            return ToolResult(
                success=True,
                output={"status": "completed", "recipe": "demo-flow", "summary": "done"},
            )
        return ToolResult(success=False, error={"message": f"unknown op {operation}"})


async def mount(coordinator, config=None):
    tool = FakeRecipesTool(coordinator, config)
    await coordinator.mount("tools", tool, name=tool.name)
    return None
'''

_LOOP_MODULE = '''
"""Fake orchestrator (recipes offline tests): one recipes-tool turn."""


class FakeLoop:
    def __init__(self, config):
        self.config = dict(config or {})

    async def execute(self, prompt, context, providers, tools, hooks, coordinator):
        await context.add_message({"role": "user", "content": prompt})
        tool = tools.get("recipes")
        result = await tool.execute({"operation": "execute", "recipe_path": "demo.yaml"})
        final = f"recipe status: {result.output.get('status')}"
        await context.add_message({"role": "assistant", "content": final})
        await hooks.emit(
            "orchestrator:complete",
            {"orchestrator": "loop-rfake", "turn_count": 1, "status": "success"},
        )
        return final


async def mount(coordinator, config=None):
    await coordinator.mount("orchestrator", FakeLoop(config))
    return None
'''

_MODULES = {
    "amplifier-module-provider-rfake/amplifier_module_provider_rfake": _PROVIDER_MODULE,
    "amplifier-module-context-rfake/amplifier_module_context_rfake": _CONTEXT_MODULE,
    "amplifier-module-tool-recipes-rfake/amplifier_module_tool_recipes_rfake": _RECIPES_TOOL_MODULE,
    "amplifier-module-loop-rfake/amplifier_module_loop_rfake": _LOOP_MODULE,
}

_BUNDLE_TEMPLATE = """\
---
bundle:
  name: recipes-offline
  version: 0.0.1
  description: Offline recipes-gate bundle with fake modules.

session:
  orchestrator:
    module: loop-rfake
    source: file://{modules}/amplifier-module-loop-rfake
  context:
    module: context-rfake
    source: file://{modules}/amplifier-module-context-rfake

providers:
  - module: provider-rfake
    source: file://{modules}/amplifier-module-provider-rfake
    config:
      default_model: rfake-model

tools:
  - module: tool-recipes-rfake
    source: file://{modules}/amplifier-module-tool-recipes-rfake
---

Recipes offline test bundle.
"""


@pytest.fixture(scope="session")
def recipes_workspace(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("recipes-runtime")
    modules = root / "modules"
    for rel, source in _MODULES.items():
        package = modules / rel
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")

    project = root / "proj"
    bundles = project / ".amplifier" / "bundles"
    bundles.mkdir(parents=True)
    (bundles / "recipes-offline.md").write_text(
        _BUNDLE_TEMPLATE.format(modules=modules), encoding="utf-8"
    )

    home = root / "home"
    home.mkdir()
    return {"project": project, "home": home}


@pytest.fixture
def recipes_env(
    recipes_workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    monkeypatch.setenv("HOME", str(recipes_workspace["home"]))
    return recipes_workspace


def _drain(runtime: RealRuntime) -> list:
    events = []
    while not runtime.queue.empty():
        events.append(runtime.queue.get_nowait())
    return events


@pytest.mark.asyncio
async def test_offline_recipe_gate_raises_bar_and_answer_resumes(recipes_env) -> None:
    """Real lifecycle: the fake recipes tool pauses at a gate mid-turn; the
    normalized ApprovalRequired lands on the queue, the broker ticket
    raises the bar, and Allow routes approve → resume through the tool's
    own operations once the turn is idle."""
    runtime = RealRuntime(
        bundle="recipes-offline", project_dir=recipes_env["project"], mode=lambda: "auto"
    )
    await runtime.start()
    try:
        response = await runtime.submit("run the demo recipe")
        assert response == "recipe status: paused_for_approval"

        events = _drain(runtime)
        required = [e for e in events if e.kind == "approval_required"]
        assert len(required) == 1
        assert required[0].prompt == "Recipe 'demo-flow' · stage 'plan' — Ship the plan?"
        assert required[0].options == STANDARD_OPTIONS

        head = runtime.broker.head
        assert head is not None, "gate did not raise an approval ticket"
        assert head.prompt == required[0].prompt
        assert head.detail.tool_name == "recipes"

        runtime.broker.answer(head.ticket_id, ALLOW_ONCE)

        assert runtime._initialized is not None
        tool = runtime._initialized.coordinator.get("tools")["recipes"]
        for _ in range(500):
            ops = [c.get("operation") for c in tool.calls]
            if "resume" in ops:
                break
            await asyncio.sleep(0.01)
        assert ops == ["execute", "approvals", "approve", "resume"]
        approve = tool.calls[2]
        assert approve["session_id"] == "recipe_20260721_100000_a1b2"
        assert approve["stage_name"] == "plan"

        followups = _drain(runtime)
        assert any(
            e.kind == "notification" and "approved and resumed" in e.message for e in followups
        )
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_offline_recipe_gate_deny_stops_the_recipe(recipes_env) -> None:
    runtime = RealRuntime(
        bundle="recipes-offline", project_dir=recipes_env["project"], mode=lambda: "auto"
    )
    await runtime.start()
    try:
        await runtime.submit("run the demo recipe")
        head = runtime.broker.head
        assert head is not None
        runtime.broker.answer(head.ticket_id, DENY)

        assert runtime._initialized is not None
        tool = runtime._initialized.coordinator.get("tools")["recipes"]
        for _ in range(500):
            ops = [c.get("operation") for c in tool.calls]
            if "deny" in ops:
                break
            await asyncio.sleep(0.01)
        assert ops == ["execute", "approvals", "deny"]
        assert tool.calls[2]["reason"] == "Denied via approval bar"
        assert "resume" not in ops
    finally:
        await runtime.cleanup()
