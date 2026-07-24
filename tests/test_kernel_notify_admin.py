"""notify_admin scope-file writers, keys.env topic + effective status.

Pure file/dict logic over ``tmp_path`` scope files (no amplifier session).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_newtui.kernel import notify_admin
from amplifier_app_newtui.kernel.bundle_admin import read_scope, settings_paths


def _paths(tmp_path: Path):
    return settings_paths(tmp_path / "proj", tmp_path / "home")


# -- set_key: settings scope writes -----------------------------------------


def test_set_key_writes_desktop_enabled_to_scope_file(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    result = notify_admin.set_key(paths, "desktop.enabled", "false", "global")
    assert result.value is False
    assert result.path == paths.global_settings
    data = read_scope(paths.global_settings)
    assert data["config"]["notifications"]["desktop"]["enabled"] is False


def test_set_key_writes_push_keys_typed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    notify_admin.set_key(paths, "push.server", "https://ntfy.example", "project")
    notify_admin.set_key(paths, "push.priority", "high", "project")
    notify_admin.set_key(paths, "push.tags", "robot, warning", "project")
    push = read_scope(paths.project_settings)["config"]["notifications"]["push"]
    assert push["server"] == "https://ntfy.example"
    assert push["priority"] == "high"
    assert push["tags"] == ["robot", "warning"]  # comma-split into a list


def test_set_key_unknown_raises(tmp_path: Path) -> None:
    with pytest.raises(notify_admin.UnknownNotifyKeyError):
        notify_admin.set_key(_paths(tmp_path), "desktop.sound", "true", "global")


def test_set_key_bad_bool_raises(tmp_path: Path) -> None:
    with pytest.raises(notify_admin.InvalidNotifyValueError):
        notify_admin.set_key(_paths(tmp_path), "suppress", "maybe", "global")


def test_set_enabled_roundtrip(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    notify_admin.set_enabled(paths, "push", True, "global")
    assert read_scope(paths.global_settings)["config"]["notifications"]["push"]["enabled"] is True
    notify_admin.set_enabled(paths, "push", False, "global")
    assert read_scope(paths.global_settings)["config"]["notifications"]["push"]["enabled"] is False


# -- topic: keys.env (secret), never a settings scope -----------------------


def test_set_topic_writes_keys_env_not_settings(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    result = notify_admin.set_key(paths, "topic", "amplifier-me-x7k9", "global")
    assert result.is_secret is True
    keys_env = tmp_path / "home" / "keys.env"
    assert result.path == keys_env
    assert "AMPLIFIER_NTFY_TOPIC=amplifier-me-x7k9" in keys_env.read_text()
    # It must NOT appear in any settings scope file.
    assert not paths.global_settings.exists()


def test_set_topic_empty_rejected(tmp_path: Path) -> None:
    with pytest.raises(notify_admin.InvalidNotifyValueError):
        notify_admin.set_key(_paths(tmp_path), "topic", "   ", "global")


def test_write_topic_upserts_and_preserves_other_lines(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    keys_env = home / "keys.env"
    keys_env.write_text("# creds\nANTHROPIC_API_KEY=sk-abc\nAMPLIFIER_NTFY_TOPIC=old\n")
    notify_admin.write_topic_to_keys_env("new-topic", home)
    text = keys_env.read_text()
    assert "ANTHROPIC_API_KEY=sk-abc" in text  # unrelated line preserved
    assert "AMPLIFIER_NTFY_TOPIC=new-topic" in text
    assert "AMPLIFIER_NTFY_TOPIC=old" not in text  # replaced, not duplicated
    assert text.count("AMPLIFIER_NTFY_TOPIC=") == 1


def test_topic_configured_reads_env_and_keys_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert notify_admin.topic_configured(home, environ={}) is False
    assert notify_admin.topic_configured(home, environ={"AMPLIFIER_NTFY_TOPIC": "t"}) is True
    (home / "keys.env").write_text("AMPLIFIER_NTFY_TOPIC=fromfile\n")
    assert notify_admin.topic_configured(home, environ={}) is True


# -- effective_status: settings + env resolution ----------------------------


def test_effective_status_defaults_desktop_ceiling() -> None:
    status = notify_admin.effective_status({}, environ={}, amplifier_home=Path("/nope"))
    assert status.ceiling == "desktop"
    assert status.ceiling_source == "default"
    assert status.desktop_gate == "allowlist"
    assert status.suppress is False
    assert status.topic is False


def test_effective_status_reflects_settings_suppress_and_disable(tmp_path: Path) -> None:
    settings = {"config": {"notifications": {"suppress": True, "desktop": {"enabled": False}}}}
    status = notify_admin.effective_status(settings, environ={}, amplifier_home=tmp_path)
    assert status.ceiling == "off"
    assert status.ceiling_source == "settings"
    assert status.desktop_gate == "off"
    assert status.desktop_gate_source == "settings"
    assert status.suppress is True


def test_effective_status_env_wins_over_settings(tmp_path: Path) -> None:
    settings = {"config": {"notifications": {"suppress": True}}}
    status = notify_admin.effective_status(
        settings, environ={"AMPLIFIER_NOTIFY": "bell"}, amplifier_home=tmp_path
    )
    assert status.ceiling == "bell"
    assert status.ceiling_source == "env"


def test_effective_status_surfaces_push_fields(tmp_path: Path) -> None:
    settings = {
        "config": {
            "notifications": {
                "push": {
                    "enabled": True,
                    "server": "https://ntfy.example",
                    "priority": "high",
                    "tags": ["robot"],
                }
            }
        }
    }
    status = notify_admin.effective_status(settings, environ={}, amplifier_home=tmp_path)
    assert status.push_enabled is True
    assert status.push_server == "https://ntfy.example"
    assert status.push_priority == "high"
    assert status.push_tags == ("robot",)


def test_known_key_names_are_stable() -> None:
    names = notify_admin.known_key_names()
    assert "topic" in names
    assert "desktop.enabled" in names
    assert "push.server" in names
    assert "desktop.sound" not in names  # documented-unsupported, not settable
