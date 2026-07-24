"""config.notifications -> ladder-env + ntfy-push bridges (issue #106).

Covers the two bridges the runtime applies in ``resolve_config`` -- the
desktop/suppress keys lowered onto the attention-ladder env vars the native
OSC 777 path reads, and the ntfy push keys folded onto the mounted
``hooks-notify-push`` config -- plus env-vs-settings precedence and the
byte-identical no-op when nothing is configured.
"""

from __future__ import annotations

import copy
from typing import Any

from amplifier_app_newtui.kernel.config import (
    apply_notification_ladder_env,
    inject_notifications_config,
    merged_push_settings,
    notification_settings,
)
from amplifier_app_newtui.ui import notifications

PUSH_HOOK = "hooks-notify-push"


def _plan(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """A mount plan with the ntfy push hook mounted as the wrapper does."""
    return {
        "hooks": [
            {"module": "hooks-approval", "config": {}},
            {
                "module": PUSH_HOOK,
                "config": config
                if config is not None
                else {"listen_event": "orchestrator:complete"},
            },
        ]
    }


def _push_config(plan: dict[str, Any]) -> dict[str, Any]:
    for hook in plan["hooks"]:
        if hook.get("module") == PUSH_HOOK:
            return hook["config"]
    raise AssertionError("push hook not found")


# -- ntfy push hook bridge --------------------------------------------------


def test_push_bridge_maps_server_priority_tags_and_preserves_listen_event() -> None:
    plan = _plan()
    settings = {
        "config": {
            "notifications": {
                "push": {
                    "enabled": True,
                    "server": "https://ntfy.example.com",
                    "priority": "high",
                    "tags": ["robot", "warning"],
                }
            }
        }
    }
    inject_notifications_config(plan, settings, environ={})
    cfg = _push_config(plan)
    assert cfg["enabled"] is True
    assert cfg["server"] == "https://ntfy.example.com"
    assert cfg["priority"] == "high"
    assert cfg["tags"] == ["robot", "warning"]
    # The mounted listen_event pin must survive the merge.
    assert cfg["listen_event"] == "orchestrator:complete"


def test_push_bridge_ntfy_block_wins_over_push_block() -> None:
    settings = {
        "config": {
            "notifications": {
                "push": {"server": "https://push.example", "priority": "low"},
                "ntfy": {"server": "https://ntfy.example", "enabled": True},
            }
        }
    }
    merged = merged_push_settings(notification_settings(settings))
    assert merged["server"] == "https://ntfy.example"  # ntfy wins
    assert merged["priority"] == "low"  # push-only key retained
    plan = _plan()
    inject_notifications_config(plan, settings, environ={})
    assert _push_config(plan)["server"] == "https://ntfy.example"


def test_push_bridge_topic_is_never_written_to_hook_config() -> None:
    """The ntfy topic is a secret: it must never land in the hook config."""
    settings = {"config": {"notifications": {"ntfy": {"enabled": True, "topic": "s3cr3t"}}}}
    plan = _plan()
    inject_notifications_config(plan, settings, environ={})
    assert "topic" not in _push_config(plan)


def test_push_bridge_env_server_wins_over_settings() -> None:
    settings = {"config": {"notifications": {"push": {"server": "https://settings.example"}}}}
    plan = _plan()
    inject_notifications_config(
        plan, settings, environ={"AMPLIFIER_NTFY_SERVER": "https://env.example"}
    )
    # env wins -> the settings server is not injected onto the hook config.
    assert "server" not in _push_config(plan)


def test_push_bridge_env_enabled_wins_over_settings() -> None:
    settings = {"config": {"notifications": {"push": {"enabled": True}}}}
    plan = _plan()
    inject_notifications_config(plan, settings, environ={"AMPLIFIER_NOTIFY_PUSH_ENABLED": "false"})
    assert "enabled" not in _push_config(plan)


def test_push_bridge_noop_when_hook_absent() -> None:
    plan = {"hooks": [{"module": "hooks-approval", "config": {}}]}
    before = copy.deepcopy(plan)
    inject_notifications_config(
        plan, {"config": {"notifications": {"push": {"server": "x"}}}}, environ={}
    )
    assert plan == before


def test_push_bridge_noop_byte_identical_when_unconfigured() -> None:
    plan = _plan()
    before = copy.deepcopy(plan)
    inject_notifications_config(plan, {}, environ={})
    assert plan == before
    inject_notifications_config(plan, {"config": {}}, environ={})
    assert plan == before


# -- desktop/suppress ladder env bridge -------------------------------------


def test_ladder_env_suppress_silences_via_amplifier_notify() -> None:
    env: dict[str, str] = {}
    apply_notification_ladder_env({"config": {"notifications": {"suppress": True}}}, env)
    assert env["AMPLIFIER_NOTIFY"] == "off"
    # And the pure ladder then fires nothing, even for a deferred decision.
    assert notifications.notification_rungs("decision_deferred", focused=False, environ=env) == ()


def test_ladder_env_desktop_disabled_drops_desktop_rung_keeps_bell() -> None:
    env: dict[str, str] = {"TERM_PROGRAM": "ghostty"}
    apply_notification_ladder_env(
        {"config": {"notifications": {"desktop": {"enabled": False}}}}, env
    )
    assert env["AMPLIFIER_TERMINAL_NOTIFICATIONS"] == "off"
    rungs = notifications.notification_rungs("decision_deferred", focused=False, environ=env)
    assert rungs == ("bell",)  # bell survives, desktop suppressed


def test_ladder_env_desktop_enabled_true_forces_any_terminal() -> None:
    env: dict[str, str] = {}  # no allowlisted TERM_PROGRAM
    apply_notification_ladder_env(
        {"config": {"notifications": {"desktop": {"enabled": True}}}}, env
    )
    assert env["AMPLIFIER_TERMINAL_NOTIFICATIONS"] == "force"
    rungs = notifications.notification_rungs("decision_deferred", focused=False, environ=env)
    assert rungs == ("bell", "desktop")


def test_ladder_env_explicit_env_wins_over_settings() -> None:
    env = {"AMPLIFIER_NOTIFY": "desktop", "AMPLIFIER_TERMINAL_NOTIFICATIONS": "off"}
    before = dict(env)
    apply_notification_ladder_env(
        {"config": {"notifications": {"suppress": True, "desktop": {"enabled": True}}}}, env
    )
    assert env == before  # explicit env vars are never overwritten


def test_ladder_env_noop_byte_identical_when_unconfigured() -> None:
    env = {"PATH": "/usr/bin", "TERM_PROGRAM": "ghostty"}
    before = dict(env)
    apply_notification_ladder_env({}, env)
    assert env == before
    apply_notification_ladder_env({"config": {"notifications": {}}}, env)
    assert env == before


def test_ladder_env_accepts_string_booleans() -> None:
    env: dict[str, str] = {}
    apply_notification_ladder_env(
        {"config": {"notifications": {"suppress": "true", "desktop": {"enabled": "false"}}}}, env
    )
    assert env["AMPLIFIER_NOTIFY"] == "off"
    assert env["AMPLIFIER_TERMINAL_NOTIFICATIONS"] == "off"
