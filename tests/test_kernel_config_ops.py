"""``kernel/config_ops`` -- seed from mount plan + persist to a settings scope.

The side-effecting half of ``/config`` (the pure logic lives in
``model/config``). Save reuses newtui's own scope machinery
(``kernel/bundle_admin``), never amplifier-app-cli's ``AppSettings``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from amplifier_app_newtui.kernel import config_ops
from amplifier_app_newtui.model.config import SessionConfigState


def test_state_from_plan_seeds_categories() -> None:
    plan = {
        "providers": [{"module": "provider-anthropic", "id": "anthropic"}],
        "tools": [{"module": "tool-filesystem", "name": "read_file"}],
        "hooks": [{"module": "hooks-mode"}],
    }
    state = config_ops.state_from_plan(plan, bundle="anchors")
    assert state.bundle == "anchors"
    names = {(i.category, i.name) for i in state.items()}
    assert ("providers", "anthropic") in names
    assert ("tools", "read_file") in names
    assert ("hooks", "hooks-mode") in names


def test_amplifier_home_honours_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path))
    assert config_ops.amplifier_home() == tmp_path
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    assert config_ops.amplifier_home() == Path.home() / ".amplifier"


def test_save_writes_global_scope_under_configurator_key(tmp_path: Path) -> None:
    from amplifier_app_newtui.model.config import ConfigItem

    state = SessionConfigState([ConfigItem("tools", "bash", True)], bundle="anchors")
    state.toggle("tools", "bash", enable=False)
    state.set_value("session.reasoning_effort", "high")

    ok, message = config_ops.save_config(state, scope="global", project_dir=tmp_path, home=tmp_path)
    assert ok and "global scope" in message
    target = tmp_path / "settings.yaml"
    assert target.is_file()
    data = yaml.safe_load(target.read_text())
    assert data["configurator"] == {
        "disabled": {"tools": ["bash"]},
        "overrides": {"session.reasoning_effort": "high"},
    }


def test_save_project_scope_targets_project_amplifier_dir(tmp_path: Path) -> None:
    from amplifier_app_newtui.model.config import ConfigItem

    state = SessionConfigState([ConfigItem("tools", "bash", True)], bundle="b")
    state.toggle("tools", "bash", enable=False)
    ok, _ = config_ops.save_config(state, scope="project", project_dir=tmp_path, home=tmp_path)
    assert ok
    assert (tmp_path / ".amplifier" / "settings.yaml").is_file()


def test_save_preserves_unrelated_existing_settings(tmp_path: Path) -> None:
    from amplifier_app_newtui.model.config import ConfigItem

    target = tmp_path / "settings.yaml"
    target.write_text(yaml.safe_dump({"bundle": {"active": "anchors"}}))
    state = SessionConfigState([ConfigItem("tools", "bash", True)], bundle="b")
    state.set_value("x", "hello")
    config_ops.save_config(state, scope="global", project_dir=tmp_path, home=tmp_path)
    data = yaml.safe_load(target.read_text())
    # The pre-existing block survives the merge; the configurator block is added.
    assert data["bundle"] == {"active": "anchors"}
    assert data["configurator"]["overrides"] == {"x": "hello"}


def test_save_with_no_changes_drops_stale_configurator_block(tmp_path: Path) -> None:
    target = tmp_path / "settings.yaml"
    target.write_text(yaml.safe_dump({"configurator": {"disabled": {"tools": ["old"]}}}))
    state = SessionConfigState(bundle="b")  # no session changes
    ok, message = config_ops.save_config(state, scope="global", project_dir=tmp_path, home=tmp_path)
    assert ok and "no session changes" in message
    data = yaml.safe_load(target.read_text()) or {} if target.exists() else {}
    assert "configurator" not in data
