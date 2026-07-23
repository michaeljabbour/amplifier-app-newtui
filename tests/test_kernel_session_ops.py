"""In-session coordinator ops (``kernel/session_ops.py``).

These are the amplifier-integration functions behind ``/model``,
``/effort``, ``/compact``, ``/clear``, ``/status``, ``/tools`` and
``/agents``. They run against a duck-typed coordinator, so the fakes here
mirror the amplifier-core mechanism surface app-cli drives (providers
with ``default_model``/``config``/``list_models``; an orchestrator with a
``config`` dict; a context with ``get_messages``/``compact``/``clear``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from amplifier_app_newtui.kernel import session_ops


class FakeProvider:
    def __init__(self, default_model: str = "m1", models: tuple[str, ...] = ("m1", "m2")):
        self.default_model = default_model
        self.config: dict[str, object] = {"default_model": default_model}
        self._models = models

    def list_models(self):
        return [SimpleNamespace(id=m) for m in self._models]


class FakeContext:
    def __init__(self, messages: list[dict] | None = None):
        self._messages = list(messages or [])
        self.compacted: str | None = None
        self.cleared = False

    async def get_messages(self):
        return list(self._messages)

    async def compact(self, focus: str = ""):
        self.compacted = focus
        self._messages = self._messages[-1:]

    async def clear(self):
        self.cleared = True
        self._messages = []


class FakeCoordinator:
    def __init__(self, mounts, *, session_id="sess1234", config=None, session_state=None):
        self._mounts = mounts
        self.session_id = session_id
        self.config = config or {}
        self.session_state: dict[str, object] = session_state if session_state is not None else {}

    def get(self, name):
        return self._mounts.get(name)


def _coord(**mounts):
    return FakeCoordinator(mounts)


# -- /model -----------------------------------------------------------------


def test_list_models_reports_current_and_available() -> None:
    coord = _coord(providers={"anthropic": FakeProvider("m1", ("m1", "m2", "m3"))})
    listing = asyncio.run(session_ops.list_models(coord))
    assert listing.provider == "anthropic"
    assert listing.current == "m1"
    assert listing.available == ("m1", "m2", "m3")


def test_list_models_no_provider_is_empty_not_error() -> None:
    listing = asyncio.run(session_ops.list_models(_coord()))
    assert listing == session_ops.ModelListing(provider="", current="")


def test_set_model_mutates_provider_config_and_session_state() -> None:
    provider = FakeProvider("m1", ("m1", "m2"))
    coord = _coord(providers={"anthropic": provider})
    ok, detail = asyncio.run(session_ops.set_model(coord, "m2"))
    assert ok
    assert provider.default_model == "m2"
    assert provider.config["default_model"] == "m2"
    assert coord.session_state["ui.model_override"] == {"provider": "anthropic", "model": "m2"}
    assert "m2" in detail


def test_set_model_picks_the_provider_that_advertises_the_model() -> None:
    a = FakeProvider("a1", ("a1", "a2"))
    b = FakeProvider("b1", ("b1", "b2"))
    coord = _coord(providers={"a": a, "b": b})
    ok, _ = asyncio.run(session_ops.set_model(coord, "b2"))
    assert ok
    assert b.default_model == "b2"
    assert a.default_model == "a1"  # untouched


def test_set_model_empty_and_no_provider_fail_cleanly() -> None:
    assert (
        asyncio.run(session_ops.set_model(_coord(providers={"a": FakeProvider()}), ""))[0] is False
    )
    assert asyncio.run(session_ops.set_model(_coord(), "m2"))[0] is False


# -- /effort ----------------------------------------------------------------


def test_effort_get_set_and_max_alias() -> None:
    orch = SimpleNamespace(config={"reasoning_effort": "medium"})
    coord = _coord(orchestrator=orch)
    assert session_ops.get_effort(coord) == "medium"
    ok, level = session_ops.set_effort(coord, "high")
    assert ok and level == "high"
    assert orch.config["reasoning_effort"] == "high"
    ok, level = session_ops.set_effort(coord, "MAX")
    assert ok and level == "xhigh"  # max → xhigh
    assert coord.session_state["ui.effort_override"] == "xhigh"


def test_effort_invalid_level_rejected() -> None:
    orch = SimpleNamespace(config={"reasoning_effort": "low"})
    coord = _coord(orchestrator=orch)
    ok, _ = session_ops.set_effort(coord, "turbo")
    assert ok is False
    assert orch.config["reasoning_effort"] == "low"  # unchanged


def test_effort_without_orchestrator_is_none_and_fails_set() -> None:
    assert session_ops.get_effort(_coord()) is None
    assert session_ops.set_effort(_coord(), "high")[0] is False


# -- /compact and /clear ----------------------------------------------------


def test_compact_invokes_context_and_reports_delta() -> None:
    context = FakeContext([{"role": "user"}, {"role": "assistant"}, {"role": "user"}])
    coord = _coord(context=context)
    ok, detail = asyncio.run(session_ops.compact_context(coord, "focus here"))
    assert ok
    assert context.compacted == "focus here"
    assert detail == "3 → 1 messages"


def test_clear_returns_count_and_calls_clear() -> None:
    context = FakeContext([{"role": "user"}, {"role": "assistant"}])
    coord = _coord(context=context)
    ok, count = asyncio.run(session_ops.clear_context(coord))
    assert ok and count == 2
    assert context.cleared is True


def test_compact_and_clear_without_context_fail_cleanly() -> None:
    assert asyncio.run(session_ops.compact_context(_coord(), ""))[0] is False
    assert asyncio.run(session_ops.clear_context(_coord())) == (False, 0)


# -- /status /tools /agents -------------------------------------------------


def test_status_snapshot_joins_coordinator_fields() -> None:
    coord = _coord(
        providers={"anthropic": FakeProvider("m1")},
        orchestrator=SimpleNamespace(config={"reasoning_effort": "high"}),
        context=FakeContext([{"role": "user"}]),
        tools={"read": object(), "write": object()},
        agents={"explorer": object()},
    )
    info = asyncio.run(session_ops.status_snapshot(coord))
    assert info.session_id == "sess1234"
    assert info.provider == "anthropic"
    assert info.model == "m1"
    assert info.effort == "high"
    assert info.messages == 1
    assert info.tools == 2
    assert info.agents == ("explorer",)


def test_list_tools_sorted_and_empty() -> None:
    coord = _coord(tools={"write": object(), "read": object()})
    assert asyncio.run(session_ops.list_tools(coord)) == ("read", "write")
    assert asyncio.run(session_ops.list_tools(_coord())) == ()


def test_list_agents_from_mount_then_config_fallback() -> None:
    coord = _coord(agents={"b": object(), "a": object()})
    assert asyncio.run(session_ops.list_agents(coord)) == ("a", "b")
    # No mounted agents mechanism → fall back to coordinator config roster.
    coord2 = FakeCoordinator({}, config={"agents": {"explorer": {}, "critic": {}}})
    assert asyncio.run(session_ops.list_agents(coord2)) == ("critic", "explorer")


def test_normalize_effort_table() -> None:
    assert session_ops.normalize_effort("HIGH") == "high"
    assert session_ops.normalize_effort("max") == "xhigh"
    assert session_ops.normalize_effort("nope") is None


# -- /skills /skill /mcp ----------------------------------------------------


class FakeResult:
    def __init__(self, success, output=None, error=None):
        self.success = success
        self.output = output
        self.error = error


class FakeSkillsTool:
    def __init__(self):
        self.calls: list[dict] = []

    async def execute(self, payload):
        self.calls.append(payload)
        if payload.get("list"):
            return FakeResult(
                True,
                {
                    "skills": [
                        {"name": "design-patterns", "description": "SOLID etc."},
                        {"name": "simplify", "description": "cut cruft"},
                    ]
                },
            )
        if payload.get("skill_name") == "design-patterns":
            return FakeResult(
                True, {"content": "# design-patterns\n\nbody", "skill_name": "design-patterns"}
            )
        return FakeResult(False, error={"message": "Skill 'x' not found"})


def test_list_skills() -> None:
    coord = _coord(tools={"load_skill": FakeSkillsTool()})
    skills = asyncio.run(session_ops.list_skills(coord))
    assert [s.name for s in skills] == ["design-patterns", "simplify"]
    assert skills[0].description == "SOLID etc."
    # The list output has no shortcut field — the alias defaults empty.
    assert [s.shortcut for s in skills] == ["", ""]


def test_list_skills_no_tool_is_empty() -> None:
    assert asyncio.run(session_ops.list_skills(_coord())) == ()


class FakeCatalogSkillsTool(FakeSkillsTool):
    """The real tool-skills surface: ``get_effective_skills`` returns the
    merged catalog of ``SkillMetadata`` — the only place shortcuts live
    (the ``{"list": true}`` output carries name + description only)."""

    def get_effective_skills(self):
        return {
            "simplify": SimpleNamespace(description="cut cruft", shortcut=None),
            "cranky-old-sam": SimpleNamespace(description="crusty review", shortcut="cosam"),
        }


def test_list_skills_prefers_catalog_and_carries_shortcuts() -> None:
    coord = _coord(tools={"load_skill": FakeCatalogSkillsTool()})
    skills = asyncio.run(session_ops.list_skills(coord))
    assert [(s.name, s.shortcut) for s in skills] == [
        ("cranky-old-sam", "cosam"),
        ("simplify", ""),
    ]
    assert skills[0].description == "crusty review"


def test_list_skills_broken_catalog_falls_back_to_list() -> None:
    tool = FakeSkillsTool()
    tool.get_effective_skills = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    coord = _coord(tools={"load_skill": tool})
    skills = asyncio.run(session_ops.list_skills(coord))
    assert [s.name for s in skills] == ["design-patterns", "simplify"]


def test_load_skill_returns_content() -> None:
    coord = _coord(tools={"load_skill": FakeSkillsTool()})
    ok, content = asyncio.run(session_ops.load_skill(coord, "design-patterns"))
    assert ok and "body" in content


def test_load_skill_not_found_and_empty_name() -> None:
    coord = _coord(tools={"load_skill": FakeSkillsTool()})
    ok, msg = asyncio.run(session_ops.load_skill(coord, "missing"))
    assert ok is False and "not found" in msg
    assert asyncio.run(session_ops.load_skill(coord, ""))[0] is False


def test_list_mcp_tools_filters_prefix() -> None:
    coord = _coord(
        tools={
            "read": object(),
            "mcp_postgres_query": object(),
            "mcp_deepwiki_search": object(),
        }
    )
    assert asyncio.run(session_ops.list_mcp_tools(coord)) == (
        "mcp_deepwiki_search",
        "mcp_postgres_query",
    )
    assert asyncio.run(session_ops.list_mcp_tools(_coord())) == ()
