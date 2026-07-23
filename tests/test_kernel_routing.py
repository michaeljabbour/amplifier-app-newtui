"""Model-routing wiring: settings bridge + spawner preference application.

Routing affects delegated sub-agents only and is single-provider-safe
(unmounted providers are skipped). These cover the newtui glue: the
settings→hook-config bridge and the spawner's routing application order.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import amplifier_foundation.spawn_utils as su

from amplifier_app_newtui.kernel.config import (
    ROUTING_MATRIX_BUNDLE_URI,
    composed_overlay_uris,
    inject_routing_config,
    routing_enabled,
)
from amplifier_app_newtui.kernel.spawner import _apply_routing, _as_preferences


# -- settings bridge --------------------------------------------------------


def test_inject_routing_config_sets_matrix_and_user_dir(tmp_path: Path) -> None:
    (tmp_path / "routing").mkdir()
    plan = {"hooks": [{"module": "hooks-routing", "config": {"default_matrix": "anthropic"}}]}
    settings = {"routing": {"matrix": "balanced", "overrides": {"coding": {}}}}
    inject_routing_config(plan, settings, tmp_path)
    cfg = plan["hooks"][0]["config"]
    assert cfg["default_matrix"] == "balanced"  # settings win over bundle default
    assert cfg["overrides"] == {"coding": {}}
    assert str(tmp_path / "routing") in cfg["custom_routing_dirs"]


def test_inject_routing_config_noop_without_hook(tmp_path: Path) -> None:
    plan = {"hooks": [{"module": "hooks-approval", "config": {}}]}
    inject_routing_config(plan, {"routing": {"matrix": "x"}}, tmp_path)
    assert plan["hooks"][0]["config"] == {}


def test_inject_routing_config_keeps_bundle_default_when_no_settings(tmp_path: Path) -> None:
    plan = {"hooks": [{"module": "hooks-routing", "config": {"default_matrix": "anthropic"}}]}
    inject_routing_config(plan, {}, tmp_path)
    assert plan["hooks"][0]["config"]["default_matrix"] == "anthropic"


# -- preference coercion ----------------------------------------------------


def test_as_preferences_coerces_dicts_and_skips_invalid() -> None:
    out = _as_preferences(
        [
            {"provider": "anthropic", "model": "claude-sonnet"},
            {"provider": "anthropic"},  # missing model → skipped
            su.ProviderPreference(provider="openai", model="gpt-x"),
        ]
    )
    assert [(p.provider, p.model) for p in out] == [
        ("anthropic", "claude-sonnet"),
        ("openai", "gpt-x"),
    ]


# -- spawner routing application order --------------------------------------


def _capture(monkeypatch):
    seen: dict = {}

    async def fake_apply(mount_plan, prefs, coordinator):
        seen["prefs"] = prefs
        return mount_plan

    monkeypatch.setattr(su, "apply_provider_preferences_with_resolution", fake_apply)
    return seen


def test_apply_routing_explicit_prefs_win(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    coord = SimpleNamespace(get_capability=lambda n: None)
    asyncio.run(
        _apply_routing(
            {"providers": []}, coord, [{"provider": "anthropic", "model": "m"}], "coding"
        )
    )
    assert [(p.provider, p.model) for p in seen["prefs"]] == [("anthropic", "m")]


def test_apply_routing_resolves_model_role_via_capability(monkeypatch) -> None:
    seen = _capture(monkeypatch)

    class Resolver:
        async def resolve(self, role):
            return [su.ProviderPreference(provider="anthropic", model=f"claude-{role}")]

    coord = SimpleNamespace(
        get_capability=lambda n: Resolver() if n == "model_role_resolver" else None
    )
    asyncio.run(_apply_routing({"providers": []}, coord, None, "fast"))
    assert seen["prefs"][0].model == "claude-fast"


def test_apply_routing_falls_back_to_agent_config_prefs(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    coord = SimpleNamespace(get_capability=lambda n: None)
    config = {"providers": [], "provider_preferences": [{"provider": "anthropic", "model": "cfg"}]}
    asyncio.run(_apply_routing(config, coord, None, None))
    assert seen["prefs"][0].model == "cfg"


def test_apply_routing_noop_when_nothing_to_apply(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    coord = SimpleNamespace(get_capability=lambda n: None)
    asyncio.run(_apply_routing({"providers": []}, coord, None, None))
    assert "prefs" not in seen  # apply never called


def test_apply_routing_swallows_errors(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(su, "apply_provider_preferences_with_resolution", boom)
    coord = SimpleNamespace(get_capability=lambda n: None)
    # must not raise
    asyncio.run(_apply_routing({"providers": []}, coord, [{"provider": "a", "model": "m"}], None))


# -- overlay mount opt-in (issue #52: actually mounting hooks-routing) -------


def test_routing_enabled_matrix_name_opts_in() -> None:
    assert routing_enabled({"routing": {"matrix": "balanced"}}) is True


def test_routing_enabled_explicit_flag_wins_over_matrix() -> None:
    # enabled:false disables even when a matrix is named ...
    assert routing_enabled({"routing": {"enabled": False, "matrix": "balanced"}}) is False
    # ... and enabled:true opts in with no matrix (bundle's default matrix).
    assert routing_enabled({"routing": {"enabled": True}}) is True


def test_routing_enabled_off_by_default() -> None:
    assert routing_enabled({}) is False
    assert routing_enabled({"routing": {}}) is False
    assert routing_enabled({"routing": {"matrix": ""}}) is False
    assert routing_enabled({"routing": "nonsense"}) is False


def test_composed_overlay_uris_appends_routing_when_enabled() -> None:
    assert composed_overlay_uris({"routing": {"matrix": "anthropic"}}) == (
        ROUTING_MATRIX_BUNDLE_URI,
    )


def test_composed_overlay_uris_keeps_user_overlays_first() -> None:
    settings = {
        "bundle": {"app": ["git+https://x/a@main"]},
        "routing": {"matrix": "anthropic"},
    }
    assert composed_overlay_uris(settings) == (
        "git+https://x/a@main",
        ROUTING_MATRIX_BUNDLE_URI,
    )


def test_composed_overlay_uris_dedupes_when_user_already_listed_routing() -> None:
    user_uri = (
        "git+https://github.com/microsoft/amplifier-bundle-routing-matrix@main"
        "#subdirectory=behaviors/routing.yaml"
    )
    settings = {"bundle": {"app": [user_uri]}, "routing": {"matrix": "anthropic"}}
    # composed exactly once — the user's own entry, not doubled.
    assert composed_overlay_uris(settings) == (user_uri,)


def test_composed_overlay_uris_empty_or_user_only_when_routing_off() -> None:
    assert composed_overlay_uris({}) == ()
    assert composed_overlay_uris({"bundle": {"app": ["git+https://x/a@main"]}}) == (
        "git+https://x/a@main",
    )
