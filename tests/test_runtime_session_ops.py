"""``RealRuntime`` in-session op wrappers (issue #30, pairs with #28).

Site 3 of the collapsed passthrough ladder: the thin ``RealRuntime``
methods that guard the coordinator (``coord is None`` before the session
is live) and delegate to ``kernel/session_ops`` on the runtime loop. The
session-op *functions* were already covered by
``test_kernel_session_ops.py``; the runtime *wrappers* around them were
the thin coverage the audit flagged. This file pins them directly with a
duck-typed coordinator hung on ``_initialized`` — no boot, no thread.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from amplifier_app_newtui.kernel.runtime import RealRuntime
from amplifier_app_newtui.kernel.session_ops import ModelListing, StatusInfo


class FakeProvider:
    def __init__(self, default_model: str = "m1", models: tuple[str, ...] = ("m1", "m2")) -> None:
        self.default_model = default_model
        self.config: dict[str, object] = {"default_model": default_model}
        self._models = models

    def list_models(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=m) for m in self._models]


class FakeContext:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self._messages = list(messages or [])
        self.cleared = False

    async def get_messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    async def compact(self, focus: str = "") -> None:
        self._messages = self._messages[-1:]

    async def clear(self) -> None:
        self.cleared = True
        self._messages = []


class FakeSkillsTool:
    """``load_skill`` tool surface used by list_skills / load_skill."""

    async def execute(self, payload: dict[str, Any]) -> SimpleNamespace:
        if payload.get("list"):
            return SimpleNamespace(
                success=True,
                output={"skills": [{"name": "brainstorming", "description": "d"}]},
            )
        name = payload.get("skill_name", "")
        return SimpleNamespace(success=True, output={"content": f"body of {name}"}, error=None)


class FakeCoordinator:
    def __init__(self, **mounts: Any) -> None:
        self._mounts = mounts
        self.session_id = "sess1234"
        self.config: dict[str, Any] = {}
        self.session_state: dict[str, object] = {}

    def get(self, name: str) -> Any:
        return self._mounts.get(name)


def _runtime(coord: Any | None) -> RealRuntime:
    """A RealRuntime with *coord* hung on ``_initialized`` (or unstarted)."""
    runtime = RealRuntime(bundle=None)
    if coord is not None:
        runtime._initialized = SimpleNamespace(coordinator=coord)  # type: ignore[assignment]
    return runtime


def _full_coord() -> FakeCoordinator:
    return FakeCoordinator(
        providers={"anthropic": FakeProvider("m1", ("m1", "m2", "m3"))},
        orchestrator=SimpleNamespace(config={"reasoning_effort": "high"}),
        context=FakeContext([{"role": "user"}, {"role": "assistant"}]),
        tools={
            "read": object(),
            "write": object(),
            "mcp_srv_do": object(),
            "load_skill": FakeSkillsTool(),
        },
        agents={"explorer": object()},
    )


# ---------------------------------------------------------------------------
# Coordinator-None guards: every wrapper answers neutrally before the
# session is initialized (no exception, no coordinator access).
# ---------------------------------------------------------------------------

# (method, args, expected neutral return before the session exists)
NONE_GUARDS: tuple[tuple[str, tuple[Any, ...], Any], ...] = (
    ("list_models", (), ModelListing(provider="", current="")),
    ("set_model", ("m2",), (False, "session still starting")),
    ("get_effort", (), None),
    ("set_effort", ("high",), (False, "session still starting")),
    ("compact", ("",), (False, "session still starting")),
    ("clear_context", (), (False, 0)),
    ("status", (), StatusInfo()),
    ("list_tools", (), ()),
    ("list_agents", (), ()),
    ("list_skills", (), ()),
    ("load_skill", ("brainstorming",), (False, "session still starting")),
    ("mcp_tools", (), ()),
)


@pytest.mark.parametrize(
    ("method", "args", "expected"), NONE_GUARDS, ids=[c[0] for c in NONE_GUARDS]
)
def test_wrapper_is_neutral_before_the_session_exists(
    method: str, args: tuple[Any, ...], expected: Any
) -> None:
    runtime = _runtime(None)
    result = asyncio.run(getattr(runtime, method)(*args))
    assert result == expected


# ---------------------------------------------------------------------------
# Live delegation: with a coordinator mounted, each wrapper returns what
# the session_ops function derives from it.
# ---------------------------------------------------------------------------


def test_list_models_delegates() -> None:
    runtime = _runtime(_full_coord())
    listing = asyncio.run(runtime.list_models())
    assert listing.provider == "anthropic"
    assert listing.current == "m1"
    assert listing.available == ("m1", "m2", "m3")


def test_set_model_delegates_and_refreshes_footer_model() -> None:
    provider = FakeProvider("m1", ("m1", "m2"))
    runtime = _runtime(FakeCoordinator(providers={"anthropic": provider}))
    ok, detail = asyncio.run(runtime.set_model("m2"))
    assert ok
    assert provider.default_model == "m2"
    assert detail == "anthropic · m2"
    # The wrapper keeps its footer copy live (provider-qualified).
    assert runtime.model_name == "anthropic/m2"


def test_get_and_set_effort_delegate() -> None:
    orch = SimpleNamespace(config={"reasoning_effort": "medium"})
    runtime = _runtime(FakeCoordinator(orchestrator=orch))
    assert asyncio.run(runtime.get_effort()) == "medium"
    ok, level = asyncio.run(runtime.set_effort("max"))
    assert ok and level == "xhigh"
    assert orch.config["reasoning_effort"] == "xhigh"


def test_compact_and_clear_delegate() -> None:
    context = FakeContext([{"role": "user"}, {"role": "assistant"}, {"role": "user"}])
    runtime = _runtime(FakeCoordinator(context=context))
    ok, detail = asyncio.run(runtime.compact("focus"))
    assert ok and detail == "3 → 1 messages"

    context2 = FakeContext([{"role": "user"}, {"role": "assistant"}])
    runtime2 = _runtime(FakeCoordinator(context=context2))
    ok, count = asyncio.run(runtime2.clear_context())
    assert ok and count == 2
    assert context2.cleared is True


def test_status_joins_coordinator_fields() -> None:
    runtime = _runtime(_full_coord())
    info = asyncio.run(runtime.status())
    assert info.session_id == "sess1234"
    assert info.provider == "anthropic"
    assert info.model == "m1"
    assert info.effort == "high"
    assert info.messages == 2
    assert info.tools == 4
    assert info.agents == ("explorer",)


def test_list_tools_and_agents_delegate() -> None:
    runtime = _runtime(_full_coord())
    assert asyncio.run(runtime.list_tools()) == ("load_skill", "mcp_srv_do", "read", "write")
    assert asyncio.run(runtime.list_agents()) == ("explorer",)


def test_mcp_tools_filters_prefix() -> None:
    runtime = _runtime(_full_coord())
    assert asyncio.run(runtime.mcp_tools()) == ("mcp_srv_do",)


def test_list_and_load_skill_delegate() -> None:
    runtime = _runtime(_full_coord())
    skills = asyncio.run(runtime.list_skills())
    assert [s.name for s in skills] == ["brainstorming"]
    ok, body = asyncio.run(runtime.load_skill("brainstorming"))
    assert ok and body == "body of brainstorming"
