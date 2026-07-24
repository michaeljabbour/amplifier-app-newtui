"""``amplifier-newtui notify`` group wiring (click CliRunner).

Admin logic is unit-tested in ``test_kernel_notify_admin`` and the bridges in
``test_kernel_notify_config``; this covers the CLI plumbing: help/subcommands,
show, set roundtrip + unknown-key reject (nonzero exit), enable/disable,
topic -> keys.env, and ``notify test`` firing the real OSC 777 escape.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from amplifier_app_newtui.kernel import bundle_admin, notify_admin
from amplifier_app_newtui.main import main


def _redirect(monkeypatch, tmp_path: Path):
    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")
    monkeypatch.setattr(bundle_admin, "settings_paths", lambda *a, **k: paths)
    monkeypatch.setattr(notify_admin, "settings_paths", lambda *a, **k: paths)
    return paths


def _clean_env(monkeypatch) -> None:
    for var in (
        "AMPLIFIER_NOTIFY",
        "AMPLIFIER_TERMINAL_NOTIFICATIONS",
        "AMPLIFIER_NTFY_TOPIC",
        "AMPLIFIER_NTFY_SERVER",
        "AMPLIFIER_NOTIFY_PUSH_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)


def test_notify_group_lists_subcommands() -> None:
    result = CliRunner().invoke(main, ["notify", "--help"])
    assert result.exit_code == 0
    for sub in ("show", "set", "enable", "disable", "test"):
        assert sub in result.output


def test_notify_show_default_desktop_ceiling(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    _clean_env(monkeypatch)
    result = CliRunner().invoke(main, ["notify", "show"])
    assert result.exit_code == 0
    assert "ladder ceiling : desktop" in result.output
    assert "topic    : not set" in result.output


def test_notify_set_desktop_disable_persists_and_shows(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _clean_env(monkeypatch)
    result = CliRunner().invoke(main, ["notify", "set", "desktop.enabled", "false"])
    assert result.exit_code == 0
    data = bundle_admin.read_scope(paths.global_settings)
    assert data["config"]["notifications"]["desktop"]["enabled"] is False
    # And `show` reflects the persisted disable (settings fold onto the env gate).
    shown = CliRunner().invoke(main, ["notify", "show"])
    assert "desktop rung   : off  (from settings)" in shown.output


def test_notify_set_unknown_key_errors_nonzero(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["notify", "set", "desktop.sound", "true"])
    assert result.exit_code == 1
    assert "unknown key" in result.output


def test_notify_set_bad_bool_errors_nonzero(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["notify", "set", "suppress", "maybe"])
    assert result.exit_code == 1
    assert "invalid value" in result.output


def test_notify_set_push_priority_persists(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["notify", "set", "push.priority", "high", "--project"])
    assert result.exit_code == 0
    data = bundle_admin.read_scope(paths.project_settings)
    assert data["config"]["notifications"]["push"]["priority"] == "high"


def test_notify_set_topic_writes_keys_env(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["notify", "set", "topic", "amplifier-me-x7"])
    assert result.exit_code == 0
    keys_env = tmp_path / "home" / "keys.env"
    assert "AMPLIFIER_NTFY_TOPIC=amplifier-me-x7" in keys_env.read_text()
    assert "secret" in result.output.lower()


def test_notify_enable_disable_push(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _clean_env(monkeypatch)
    enabled = CliRunner().invoke(main, ["notify", "enable", "push"])
    assert enabled.exit_code == 0
    assert (
        bundle_admin.read_scope(paths.global_settings)["config"]["notifications"]["push"]["enabled"]
        is True
    )
    # Enabling push with no topic warns.
    assert "no ntfy topic" in enabled.output
    disabled = CliRunner().invoke(main, ["notify", "disable", "push"])
    assert disabled.exit_code == 0
    assert (
        bundle_admin.read_scope(paths.global_settings)["config"]["notifications"]["push"]["enabled"]
        is False
    )


def test_notify_test_emits_osc_escape_when_forced(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("AMPLIFIER_TERMINAL_NOTIFICATIONS", "force")
    result = CliRunner().invoke(main, ["notify", "test"])
    assert result.exit_code == 0
    assert "\x1b]777;notify;" in result.output  # the real OSC 777 escape fired
    assert "desktop (OSC 777)" in result.output


def test_notify_test_silenced_fires_nothing(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("AMPLIFIER_NOTIFY", "off")
    result = CliRunner().invoke(main, ["notify", "test"])
    assert result.exit_code == 0
    assert "nothing fired" in result.output
    assert "\x1b]777;notify;" not in result.output
