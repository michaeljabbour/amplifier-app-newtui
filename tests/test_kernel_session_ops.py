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

from amplifier_app_tui.kernel import session_ops
from amplifier_app_tui.kernel.model_routing import activate_live_matrix


class FakeProvider:
    def __init__(
        self,
        default_model: str = "m1",
        models: tuple[str, ...] = ("m1", "m2"),
        priority: int | None = None,
    ):
        self.default_model = default_model
        self.config: dict[str, object] = {"default_model": default_model}
        self._models = models
        if priority is not None:
            # Real modules snapshot the attribute from config at mount; both
            # read sites must exist for priority-selection tests.
            self.priority = priority
            self.config["priority"] = priority

    def list_models(self):
        return [SimpleNamespace(id=m) for m in self._models]


class FrozenPriorityProvider(FakeProvider):
    """A provider whose ``priority`` is read-only (the property pattern).

    ``set_model``'s promotion must still land via the ``config`` dict when
    the attribute refuses writes."""

    def __init__(
        self, default_model: str = "f1", models: tuple[str, ...] = ("f1", "f2"), priority: int = 5
    ):
        super().__init__(default_model, models)
        self.config["priority"] = priority

    @property
    def priority(self):
        return self.config.get("priority", 100)


class StalePriorityProvider(FakeProvider):
    """A provider with a genuinely immutable priority snapshot."""

    def __init__(
        self, default_model: str = "s1", models: tuple[str, ...] = ("s1", "s2"), priority: int = 5
    ):
        super().__init__(default_model, models)
        self._priority_snapshot = priority
        self.config["priority"] = priority

    @property
    def priority(self):
        return self._priority_snapshot


class FakeContext:
    def __init__(self, messages: list[dict] | None = None):
        self._messages = list(messages or [])
        self.compacted: str | None = None
        self.cleared = False

    async def get_messages(self):
        return list(self._messages)

    async def add_message(self, message: dict) -> None:
        self._messages.append(message)

    async def compact(self, focus: str = ""):
        self.compacted = focus
        self._messages = self._messages[-1:]

    async def clear(self):
        self.cleared = True
        self._messages = []


class EphemeralCompactContext(FakeContext):
    """context-simple shape: explicit compact is a protocol no-op."""

    async def compact(self):
        self.compacted = "automatic"


class FakeCoordinator:
    def __init__(
        self,
        mounts,
        *,
        session_id="sess1234",
        config=None,
        session_state=None,
        capabilities=None,
    ):
        self._mounts = mounts
        self.session_id = session_id
        self.config = config or {}
        self.session_state: dict[str, object] = session_state if session_state is not None else {}
        self.capabilities = capabilities or {}

    def get(self, name):
        return self._mounts.get(name)

    def get_capability(self, name):
        return self.capabilities.get(name)

    def register_capability(self, name, value):
        self.capabilities[name] = value


class RejectingCapabilityCoordinator(FakeCoordinator):
    def register_capability(self, name, value):
        if name == "session.routing":
            raise RuntimeError("capability registry rejected update")
        super().register_capability(name, value)


class FakeRoleResolver:
    def __init__(self):
        self._matrix_roles = {}
        self._providers = {}
        self.known_roles = ()
        self.name = "old"

    async def resolve(self, role):
        candidate = self._matrix_roles[role]["candidates"][0]
        return [SimpleNamespace(provider=candidate["provider"], model=candidate["model"])]


class FailingRoleResolver(FakeRoleResolver):
    async def resolve(self, role):
        if role == "broken":
            raise RuntimeError("cannot resolve broken role")
        return await super().resolve(role)


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


def test_set_model_retargets_live_matrix_without_replacing_exact_root_model(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    routing = home / "routing"
    routing.mkdir(parents=True)
    (routing / "b.yaml").write_text(
        "name: b\nroles:\n  coding:\n    candidates:\n      - {provider: b, model: delegated-b}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))
    a = FakeProvider("a1", ("a1",), priority=1)
    b = FakeProvider("b1", ("b1", "b2"), priority=2)
    resolver = FakeRoleResolver()
    coord = FakeCoordinator(
        {"providers": {"a": a, "b": b}},
        config={
            "providers": [
                {"module": "provider-anthropic", "id": "a"},
                {"module": "provider-vllm", "id": "b"},
            ],
            "agents": {"builder": {"model_role": "coding"}},
        },
        capabilities={"model_role_resolver": resolver},
    )

    ok, detail = asyncio.run(session_ops.set_model(coord, "b b2"))

    assert ok and detail == "b · b2 · routing b"
    assert b.default_model == "b2"
    assert b.config["default_model"] == "b2"
    assert resolver.name == "b"
    assert resolver.known_roles == ("coding",)
    assert coord.config["agents"]["builder"]["provider_preferences"] == [
        {"provider": "b", "model": "delegated-b"}
    ]
    assert coord.capabilities["session.routing"]["matrix"] == "b"
    assert coord.session_state["ui.routing_matrix"] == {"name": "b", "live": True}


def test_live_matrix_agent_resolution_failure_is_preflight_only(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    routing = home / "routing"
    routing.mkdir(parents=True)
    (routing / "b.yaml").write_text(
        "name: b\nroles:\n  broken:\n    candidates:\n      - {provider: b, model: delegated-b}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))
    resolver = FailingRoleResolver()
    resolver._matrix_roles = {"old": {"candidates": []}}
    resolver._providers = {"old": object()}
    resolver.known_roles = ("old",)
    agent = {
        "model_role": "broken",
        "provider_preferences": [{"provider": "old", "model": "old-model"}],
    }
    hook_config = {"default_matrix": "old"}
    coord = FakeCoordinator(
        {"providers": {"b": FakeProvider("b1", ("b1",))}},
        config={
            "providers": [{"module": "provider-vllm", "id": "b"}],
            "agents": {"builder": agent},
            "hooks": [{"module": "hooks-routing", "config": hook_config}],
        },
        session_state={"ui.routing_matrix": {"name": "old", "live": True}},
        capabilities={
            "model_role_resolver": resolver,
            "session.routing": {"matrix": "old"},
        },
    )

    result = asyncio.run(activate_live_matrix(coord, "b"))

    assert not result.live
    assert "could not resolve agent 'builder'" in result.reason
    assert resolver.name == "old"
    assert resolver._matrix_roles == {"old": {"candidates": []}}
    assert resolver.known_roles == ("old",)
    assert agent["provider_preferences"] == [{"provider": "old", "model": "old-model"}]
    assert hook_config == {"default_matrix": "old"}
    assert coord.capabilities["session.routing"] == {"matrix": "old"}
    assert coord.session_state == {"ui.routing_matrix": {"name": "old", "live": True}}


def test_live_matrix_commit_failure_rolls_back_all_python_surfaces(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    routing = home / "routing"
    routing.mkdir(parents=True)
    (routing / "b.yaml").write_text(
        "name: b\nroles:\n  coding:\n    candidates:\n      - {provider: b, model: delegated-b}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))
    resolver = FakeRoleResolver()
    old_roles = {"old": {"candidates": [{"provider": "old", "model": "old-model"}]}}
    old_providers = {"old": FakeProvider("old-model", ("old-model",))}
    resolver._matrix_roles = old_roles
    resolver._providers = old_providers
    resolver.known_roles = ("old",)
    agent = {
        "model_role": "coding",
        "provider_preferences": [{"provider": "old", "model": "old-model"}],
    }
    hook_config = {"default_matrix": "old", "other": True}
    old_ui = {"name": "old", "live": True}
    coord = RejectingCapabilityCoordinator(
        {"providers": {"b": FakeProvider("b1", ("b1",))}},
        config={
            "providers": [{"module": "provider-vllm", "id": "b"}],
            "agents": {"builder": agent},
            "hooks": [{"module": "hooks-routing", "config": hook_config}],
        },
        session_state={"ui.routing_matrix": old_ui},
        capabilities={
            "model_role_resolver": resolver,
            "session.routing": {"matrix": "old"},
        },
    )

    result = asyncio.run(activate_live_matrix(coord, "b"))

    assert not result.live
    assert "live matrix update rolled back" in result.reason
    assert resolver.name == "old"
    assert resolver._matrix_roles is old_roles
    assert resolver._providers is old_providers
    assert resolver.known_roles == ("old",)
    assert agent["provider_preferences"] == [{"provider": "old", "model": "old-model"}]
    assert hook_config == {"default_matrix": "old", "other": True}
    assert coord.capabilities["session.routing"] == {"matrix": "old"}
    assert coord.session_state == {"ui.routing_matrix": old_ui}


def test_set_model_explicit_provider_infers_family_without_coordinator_settings(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    routing = home / "routing"
    routing.mkdir(parents=True)
    (routing / "anthropic.yaml").write_text(
        "name: anthropic\nroles:\n  coding:\n    candidates:\n"
        "      - {provider: claude-primary, model: delegated-claude}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))
    provider = FakeProvider("old", ("old", "claude-root"))
    provider.module_id = "provider-anthropic"
    resolver = FakeRoleResolver()
    coord = FakeCoordinator(
        {"providers": {"claude-primary": provider}},
        config={},
        capabilities={"model_role_resolver": resolver},
    )

    ok, detail = asyncio.run(session_ops.set_model(coord, "claude-primary claude-root"))

    assert ok and detail == "claude-primary · claude-root · routing anthropic"
    assert provider.default_model == "claude-root"
    assert resolver.name == "anthropic"


def test_set_model_non_live_matrix_reports_divergence_not_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path / "empty-home"))
    provider = FakeProvider("old", ("old", "new"))
    coord = _coord(providers={"anthropic": provider})

    ok, detail = asyncio.run(session_ops.set_model(coord, "new"))

    assert ok
    assert "matrix source is not cached" in detail
    assert "root/delegates may diverge" in detail
    assert "pending restart" not in detail
    assert coord.session_state["ui.routing_matrix"] == {
        "name": "anthropic",
        "live": False,
        "reason": "matrix source is not cached",
        "divergent": True,
    }


def test_set_model_picks_the_provider_that_advertises_the_model() -> None:
    a = FakeProvider("a1", ("a1", "a2"))
    b = FakeProvider("b1", ("b1", "b2"))
    coord = _coord(providers={"a": a, "b": b})
    ok, _ = asyncio.run(session_ops.set_model(coord, "b2"))
    assert ok
    assert b.default_model == "b2"
    assert a.default_model == "a1"  # untouched


def test_set_model_explicit_provider_form_targets_that_provider() -> None:
    """``/model <provider> <model>`` wins over the list_models scan order."""
    a = FakeProvider("a1", ("shared",))
    b = FakeProvider("b1", ("shared",))
    coord = _coord(providers={"a": a, "b": b})
    ok, detail = asyncio.run(session_ops.set_model(coord, "b shared"))
    assert ok
    assert detail.startswith("b · shared · delegated routing unchanged")
    assert "root/delegates may diverge" in detail
    assert "pending restart" not in detail
    assert b.default_model == "shared"
    assert a.default_model == "a1"  # untouched despite advertising the model
    assert coord.session_state["ui.model_override"] == {"provider": "b", "model": "shared"}


def test_set_model_unknown_two_token_prefix_must_still_be_advertised() -> None:
    """A non-provider first token is a bare model, never an implicit target."""
    a = FakeProvider("a1", ())
    coord = _coord(providers={"a": a})
    ok, detail = asyncio.run(session_ops.set_model(coord, "zz weird"))
    assert not ok
    assert "not advertised by any mounted provider" in detail
    assert "/model <provider> <model>" in detail
    assert a.default_model == "a1"


def test_set_model_unique_advertiser_wins_over_sticky_provider() -> None:
    """Fresh model evidence must beat a previous provider override."""
    a = FakeProvider("a1", ("a1", "a2"))
    b = FakeProvider("b1", ("b1",))
    coord = _coord(providers={"a": a, "b": b})
    asyncio.run(session_ops.set_model(coord, "b b1"))
    ok, _ = asyncio.run(session_ops.set_model(coord, "a2"))
    assert ok
    assert a.default_model == "a2"
    assert b.default_model == "b1"
    assert coord.session_state["ui.model_override"] == {"provider": "a", "model": "a2"}


def test_set_model_sticky_override_ignored_when_provider_unmounted() -> None:
    a = FakeProvider("a1", ("a1", "a2"))
    coord = _coord(
        providers={"a": a},
        session_state={"ui.model_override": {"provider": "gone", "model": "g1"}},
    )
    ok, _ = asyncio.run(session_ops.set_model(coord, "a2"))
    assert ok
    assert a.default_model == "a2"


def test_set_model_ambiguous_bare_model_fails_without_mutation() -> None:
    a = FakeProvider("a1", ("shared",))
    b = FakeProvider("b1", ("shared",))
    coord = FakeCoordinator(
        {"providers": {"a": a, "b": b}},
        session_state={"ui.model_override": {"provider": "b", "model": "b1"}},
    )

    ok, detail = asyncio.run(session_ops.set_model(coord, "shared"))

    assert not ok
    assert "advertised by multiple providers (a, b)" in detail
    assert "/model <provider> <model>" in detail
    assert (a.default_model, b.default_model) == ("a1", "b1")
    assert coord.session_state == {"ui.model_override": {"provider": "b", "model": "b1"}}


def test_set_model_unadvertised_bare_model_fails_without_primary_fallback() -> None:
    a = FakeProvider("a1", ("a1",), priority=1)
    b = FakeProvider("b1", ("b1",), priority=2)
    coord = _coord(providers={"a": a, "b": b})

    ok, detail = asyncio.run(session_ops.set_model(coord, "invented-model"))

    assert not ok
    assert "not advertised by any mounted provider" in detail
    assert (a.default_model, b.default_model) == ("a1", "b1")
    assert coord.session_state == {}


def test_set_model_promotes_a_non_serving_provider_so_turns_reach_it() -> None:
    """Mutating ``default_model`` alone never reroutes: the orchestrator
    selects strictly by priority-min (``loop-streaming::_select_provider``).
    Switching to another provider must lower its priority below the rest."""
    a = FakeProvider("a1", ("a1",), priority=1)
    b = FakeProvider("b1", ("b1", "b2"), priority=2)
    coord = _coord(providers={"a": a, "b": b})
    ok, detail = asyncio.run(session_ops.set_model(coord, "b2"))
    assert ok and detail.startswith("b · b2 · delegated routing unchanged")
    assert b.priority == 0
    assert b.config["priority"] == 0
    assert a.priority == 1  # others untouched
    assert a.config["priority"] == 1
    assert session_ops._primary_provider(coord)[0] == "b"


def test_set_model_on_the_serving_provider_leaves_priorities_alone() -> None:
    a = FakeProvider("a1", ("a1", "a2"), priority=1)
    b = FakeProvider("b1", ("b1",), priority=2)
    coord = _coord(providers={"a": a, "b": b})
    ok, _ = asyncio.run(session_ops.set_model(coord, "a2"))
    assert ok
    assert (a.priority, a.config["priority"]) == (1, 1)
    assert (b.priority, b.config["priority"]) == (2, 2)


def test_set_model_promotes_on_a_priority_tie() -> None:
    """A tie can still resolve to the other provider in mount order."""
    a = FakeProvider("a1", ("a1",), priority=5)
    b = FakeProvider("b1", ("b1", "b2"), priority=5)
    coord = _coord(providers={"a": a, "b": b})
    ok, _ = asyncio.run(session_ops.set_model(coord, "b2"))
    assert ok
    assert b.priority == 4
    assert session_ops._primary_provider(coord)[0] == "b"


def test_set_model_config_backed_read_only_priority_promotes_via_config() -> None:
    a = FakeProvider("a1", ("a1",), priority=1)
    b = FrozenPriorityProvider("b1", ("b1", "b2"), priority=2)
    coord = _coord(providers={"a": a, "b": b})
    ok, _ = asyncio.run(session_ops.set_model(coord, "b2"))
    assert ok
    assert b.config["priority"] == 0
    assert session_ops._primary_provider(coord)[0] == "b"


def test_set_model_stale_read_only_priority_fails_closed_and_rolls_back() -> None:
    a = FakeProvider("a1", ("a1",), priority=1)
    b = StalePriorityProvider("b1", ("b1", "b2"), priority=2)
    coord = _coord(providers={"a": a, "b": b})
    ok, detail = asyncio.run(session_ops.set_model(coord, "b2"))
    assert not ok
    assert "read-only routing priority" in detail
    assert b.default_model == "b1"
    assert b.config == {"default_model": "b1", "priority": 2}
    assert "ui.model_override" not in coord.session_state
    assert session_ops._primary_provider(coord)[0] == "a"


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


def test_get_effort_falls_back_to_the_serving_providers_config() -> None:
    """No per-turn override → the provider's own config applies (its
    Phase-3 fallback), so report THAT instead of nothing."""
    provider = FakeProvider("m1", ("m1",))
    provider.config["reasoning_effort"] = "high"
    coord = _coord(providers={"anthropic": provider})
    assert session_ops.get_effort(coord) == "high"


def test_get_effort_fallback_reads_the_legacy_effort_alias() -> None:
    provider = FakeProvider("m1", ("m1",))
    provider.config["effort"] = "max"  # surfaced verbatim — distinct on 4.8
    coord = _coord(providers={"anthropic": provider})
    assert session_ops.get_effort(coord) == "max"


def test_get_effort_orchestrator_override_beats_provider_config() -> None:
    provider = FakeProvider("m1", ("m1",))
    provider.config["effort"] = "low"
    orch = SimpleNamespace(config={"reasoning_effort": "xhigh"})
    coord = _coord(orchestrator=orch, providers={"anthropic": provider})
    assert session_ops.get_effort(coord) == "xhigh"


def test_get_effort_fallback_targets_the_serving_provider_only() -> None:
    serving = FakeProvider("s1", ("s1",), priority=1)
    serving.config["effort"] = "high"
    other = FakeProvider("o1", ("o1",), priority=2)
    other.config["effort"] = "low"
    coord = _coord(providers={"serving": serving, "other": other})
    assert session_ops.get_effort(coord) == "high"


# -- /compact and /clear ----------------------------------------------------


def test_compact_invokes_context_and_reports_delta() -> None:
    context = FakeContext([{"role": "user"}, {"role": "assistant"}, {"role": "user"}])
    coord = _coord(context=context)
    ok, detail = asyncio.run(session_ops.compact_context(coord, "focus here"))
    assert ok
    assert context.compacted == "focus here"
    assert detail == "3 → 1 messages"


def test_compact_noop_reports_no_persistent_change_truthfully() -> None:
    context = EphemeralCompactContext([{"role": "user"}, {"role": "assistant"}])
    ok, detail = asyncio.run(session_ops.compact_context(_coord(context=context)))
    assert ok
    assert detail == "2 messages · no persistent change; request-view compaction may be automatic"


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
    context = FakeContext()
    coord = _coord(tools={"load_skill": FakeSkillsTool()}, context=context)
    ok, content = asyncio.run(session_ops.load_skill(coord, "design-patterns"))
    assert ok and "body" in content
    assert len(context._messages) == 1
    assert context._messages[0]["role"] == "system"
    assert "body" in context._messages[0]["content"]
    assert context._messages[0]["metadata"]["source"] == "hook"
    assert context._messages[0]["metadata"]["injected_by"] == "amplifier-tui-skill"
    assert coord.session_state["ui.loaded_skills"] == [
        {"name": "design-patterns", "arguments": "", "kind": "inline"}
    ]


def test_load_skill_splits_name_and_arguments_for_the_real_tool_contract() -> None:
    tool = FakeSkillsTool()
    context = FakeContext()
    coord = _coord(tools={"load_skill": tool}, context=context)

    ok, _ = asyncio.run(session_ops.load_skill(coord, "design-patterns inspect src/app.py"))

    assert ok is True
    assert tool.calls == [{"skill_name": "design-patterns", "arguments": "inspect src/app.py"}]
    assert "Invocation arguments: inspect src/app.py" in context._messages[0]["content"]


def test_load_skill_fork_result_is_not_rendered_blank() -> None:
    class ForkSkillsTool:
        async def execute(self, payload):
            assert payload == {"skill_name": "council", "arguments": "proposal.md"}
            return FakeResult(
                True,
                {
                    "context": "fork",
                    "message": "The council completed.\n\nPASS with two notes.",
                    "response": "PASS with two notes.",
                },
            )

    context = FakeContext()
    ok, content = asyncio.run(
        session_ops.load_skill(
            _coord(tools={"load_skill": ForkSkillsTool()}, context=context),
            "council proposal.md",
        )
    )
    assert ok is True
    assert "PASS with two notes" in content
    assert "forked session" in context._messages[0]["content"]


def test_load_skill_fails_honestly_when_context_is_unavailable() -> None:
    coord = _coord(tools={"load_skill": FakeSkillsTool()})
    ok, message = asyncio.run(session_ops.load_skill(coord, "design-patterns"))
    assert ok is False
    assert "not active for the next turn" in message


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


# ---------------------------------------------------------------------------
# _primary_provider — the provider that will actually serve the turn
# ---------------------------------------------------------------------------


def test_primary_provider_picks_lowest_priority_not_mount_order() -> None:
    """Mount order follows the mount plan, whose index 0 is pinned to the
    bundle-declared provider. "First mounted" therefore made ``/model`` and
    ``/status`` report — and ``/model <name>`` MUTATE — a provider that was not
    the one answering. The orchestrator picks lowest priority; so do we."""
    anthropic = FakeProvider(default_model="claude-sonnet-4-5-20250929")
    anthropic.config["priority"] = 2
    runpod = FakeProvider(default_model="zai-org/GLM-5.2-FP8")
    runpod.config["priority"] = 1
    coord = FakeCoordinator({"providers": {"anthropic": anthropic, "runpod": runpod}})

    name, provider = session_ops._primary_provider(coord)
    assert name == "runpod"
    assert provider is runpod


def test_primary_provider_reads_the_priority_attribute_too() -> None:
    # loop-streaming checks `provider.priority` before `provider.config`;
    # the vllm and anthropic modules both stash it as an attribute.
    low = FakeProvider(default_model="a")
    low.priority = 1  # type: ignore[attr-defined]
    high = FakeProvider(default_model="b")
    high.priority = 50  # type: ignore[attr-defined]
    coord = FakeCoordinator({"providers": {"high": high, "low": low}})
    assert session_ops._primary_provider(coord)[0] == "low"


def test_primary_provider_ties_fall_back_to_mount_order() -> None:
    first, second = FakeProvider(default_model="a"), FakeProvider(default_model="b")
    coord = FakeCoordinator({"providers": {"first": first, "second": second}})
    assert session_ops._primary_provider(coord)[0] == "first"


def test_primary_provider_empty_is_blank() -> None:
    assert session_ops._primary_provider(FakeCoordinator({"providers": {}})) == ("", None)
