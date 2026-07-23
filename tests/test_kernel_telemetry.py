"""context-intelligence-logging wiring: the newtui glue for issue #51.

The telemetry sink itself is the upstream ``hook-context-intelligence``
module, composed in via a ``bundle.app`` overlay — this app never vendors
one. These cover the app's own contribution: the settings->hook-config
bridge (custom destinations + legacy single-destination keys), the
guarantee that the boot suppression list never strips the hook, that its
``delegate:*`` coverage survives the bridge, and that the async cleanup
that drains in-flight HTTP is awaited on session end.
"""

from __future__ import annotations

import asyncio
from typing import Any

from amplifier_app_newtui.kernel.config import (
    expand_env_placeholders,
    inject_telemetry_config,
)
from amplifier_app_newtui.kernel.runtime import (
    _SUPPRESSED_HOOKS_DEFAULT,
    _apply_hook_suppression,
    suppressed_hooks_setting,
)
from amplifier_app_newtui.kernel.session_factory import (
    InitializedSession,
    MountReport,
)

CI_HOOK = "hook-context-intelligence"


def _plan_with_hook(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """A mount plan with the context-intelligence hook composed in."""
    return {
        "hooks": [
            {"module": "hooks-approval", "config": {}},
            {"module": CI_HOOK, "config": config if config is not None else {}},
        ]
    }


def _hook_config(plan: dict[str, Any]) -> dict[str, Any]:
    for hook in plan["hooks"]:
        if hook.get("module") == CI_HOOK:
            return hook["config"]
    raise AssertionError("hook not found")


# -- settings -> hook-config bridge -----------------------------------------


def test_bridge_maps_two_destinations_with_include_exclude_routing() -> None:
    """The acceptance shape: two destinations, one include-matched, one excluded."""
    plan = _plan_with_hook()
    settings = {
        "telemetry": {
            "destinations": {
                "team": {
                    "url": "https://ci.example.com",
                    "api_key": "static-key",
                    "include": ["*"],
                    "auth_mode": "static",
                },
                "scratch": {
                    "url": "http://localhost:8000",
                    "exclude": ["*"],
                },
            }
        }
    }
    inject_telemetry_config(plan, settings)
    dests = _hook_config(plan)["destinations"]
    assert set(dests) == {"team", "scratch"}
    assert dests["team"]["include"] == ["*"]
    assert dests["team"]["auth_mode"] == "static"
    assert dests["scratch"]["exclude"] == ["*"]


def test_bridge_maps_legacy_single_destination_keys() -> None:
    plan = _plan_with_hook()
    settings = {
        "telemetry": {
            "server_url": "https://ci.example.com",
            "api_key": "secret",
            "workspace": "my-workspace",
            "dispatch_timeout": 30,
            "dispatch_failure_threshold": 3,
        }
    }
    inject_telemetry_config(plan, settings)
    cfg = _hook_config(plan)
    assert cfg["context_intelligence_server_url"] == "https://ci.example.com"
    assert cfg["context_intelligence_api_key"] == "secret"
    assert cfg["workspace"] == "my-workspace"
    assert cfg["dispatch_timeout"] == 30
    assert cfg["dispatch_failure_threshold"] == 3


def test_bridge_is_noop_without_telemetry_settings() -> None:
    """No telemetry configured => local JSONL capture only (hook config untouched)."""
    plan = _plan_with_hook({"additional_events": ["delegate:agent_spawned"]})
    inject_telemetry_config(plan, {})
    assert _hook_config(plan) == {"additional_events": ["delegate:agent_spawned"]}
    assert "destinations" not in _hook_config(plan)


def test_bridge_is_noop_when_hook_not_mounted() -> None:
    """Behavior not composed => nothing to bridge, plan untouched, no raise."""
    plan = {"hooks": [{"module": "hooks-approval", "config": {}}]}
    inject_telemetry_config(plan, {"telemetry": {"server_url": "https://x"}})
    assert plan["hooks"][0]["config"] == {}


def test_bridge_unions_additional_events_preserving_delegate_defaults() -> None:
    """delegate:* coverage the behavior ships must never be clobbered."""
    plan = _plan_with_hook(
        {
            "additional_events": [
                "delegate:agent_spawned",
                "delegate:agent_completed",
            ]
        }
    )
    inject_telemetry_config(
        plan, {"telemetry": {"additional_events": ["custom:event", "delegate:agent_spawned"]}}
    )
    events = _hook_config(plan)["additional_events"]
    # existing delegate events kept; new one appended; no duplicates.
    assert events == [
        "delegate:agent_spawned",
        "delegate:agent_completed",
        "custom:event",
    ]


def test_bridge_empty_exclude_events_opts_back_in() -> None:
    plan = _plan_with_hook()
    inject_telemetry_config(plan, {"telemetry": {"exclude_events": []}})
    assert _hook_config(plan)["exclude_events"] == []


def test_bridge_junk_telemetry_shape_is_ignored() -> None:
    plan = _plan_with_hook({"workspace": "keep"})
    inject_telemetry_config(plan, {"telemetry": ["not", "a", "dict"]})
    assert _hook_config(plan) == {"workspace": "keep"}


def test_destination_secret_expands_from_env(monkeypatch) -> None:
    """${VAR} secrets in a destination resolve from keys.env after the bridge."""
    monkeypatch.setenv("CI_TEAM_KEY", "resolved-secret")
    plan = _plan_with_hook()
    settings = {
        "telemetry": {"destinations": {"team": {"url": "https://ci", "api_key": "${CI_TEAM_KEY}"}}}
    }
    inject_telemetry_config(plan, settings)
    expand_env_placeholders(plan)
    assert _hook_config(plan)["destinations"]["team"]["api_key"] == "resolved-secret"


def test_unset_destination_secret_is_dropped_not_emptied(monkeypatch) -> None:
    """An unset ${VAR} with no default is dropped, never sent as an empty key."""
    monkeypatch.delenv("CI_MISSING_KEY", raising=False)
    plan = _plan_with_hook()
    settings = {
        "telemetry": {
            "destinations": {"team": {"url": "https://ci", "api_key": "${CI_MISSING_KEY}"}}
        }
    }
    inject_telemetry_config(plan, settings)
    expand_env_placeholders(plan)
    team = _hook_config(plan)["destinations"]["team"]
    assert "api_key" not in team
    assert team["url"] == "https://ci"


# -- boot suppression must never strip the telemetry hook -------------------


def test_context_intelligence_hook_not_in_default_suppression() -> None:
    assert CI_HOOK not in _SUPPRESSED_HOOKS_DEFAULT


def test_apply_suppression_keeps_context_intelligence_hook() -> None:
    plan = _plan_with_hook()
    removed = _apply_hook_suppression(plan, lambda _event: None, _SUPPRESSED_HOOKS_DEFAULT)
    kept = [h["module"] for h in plan["hooks"]]
    assert CI_HOOK in kept
    assert CI_HOOK not in removed


def test_user_can_still_opt_into_suppressing_the_hook() -> None:
    """The default keeps it; an explicit hooks.suppress entry can drop it."""
    suppressed = suppressed_hooks_setting({"hooks": {"suppress": [CI_HOOK]}})
    assert CI_HOOK in suppressed
    plan = _plan_with_hook()
    removed = _apply_hook_suppression(plan, lambda _event: None, suppressed)
    assert CI_HOOK in removed
    assert all(h["module"] != CI_HOOK for h in plan["hooks"])


# -- async cleanup (drains in-flight HTTP) is awaited on session end --------


def test_session_cleanup_awaits_session_after_unregistering_hooks() -> None:
    """InitializedSession.cleanup() must await session.cleanup() (the CI hook's
    async drain of in-flight HTTP) *after* unregistering app hooks (#22)."""
    order: list[str] = []

    class _FakeSession:
        async def cleanup(self) -> None:
            order.append("session.cleanup")

    initialized = InitializedSession(
        session=_FakeSession(),  # type: ignore[arg-type]
        session_id="s1",
        resolved=None,  # type: ignore[arg-type]
        mount_report=MountReport(mounted_provider_count=1),
    )
    initialized.unregister_handles.append(lambda: order.append("unregister"))

    asyncio.run(initialized.cleanup())

    assert order == ["unregister", "session.cleanup"]
    assert initialized.unregister_handles == []
