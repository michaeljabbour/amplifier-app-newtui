"""Top-level allowed-dirs/denied-dirs CLI plumbing."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from amplifier_app_newtui.kernel import bundle_admin
from amplifier_app_newtui.kernel.directory_permissions import configured_entries
from amplifier_app_newtui.main import main


def test_allowed_dirs_add_list_remove_roundtrip(tmp_path: Path, monkeypatch) -> None:
    paths = bundle_admin.settings_paths(tmp_path / "project", tmp_path / "home")
    monkeypatch.setattr(bundle_admin, "settings_paths", lambda *args, **kwargs: paths)
    target = tmp_path / "shared"
    runner = CliRunner()

    added = runner.invoke(main, ["allowed-dirs", "add", str(target), "--project"])
    assert added.exit_code == 0
    assert configured_entries(paths, "allowed")[0].path == str(target.resolve())

    listed = runner.invoke(main, ["allowed-dirs", "list", "--project"])
    assert listed.exit_code == 0
    assert str(target.resolve()) in listed.stdout
    assert "project-default" in listed.stdout

    removed = runner.invoke(main, ["allowed-dirs", "remove", str(target), "--project"])
    assert removed.exit_code == 0
    assert configured_entries(paths, "allowed") == ()


def test_denied_dirs_uses_same_settings_shape(tmp_path: Path, monkeypatch) -> None:
    paths = bundle_admin.settings_paths(tmp_path / "project", tmp_path / "home")
    monkeypatch.setattr(bundle_admin, "settings_paths", lambda *args, **kwargs: paths)
    target = tmp_path / "project" / ".git"
    result = CliRunner().invoke(main, ["denied-dirs", "add", str(target), "--local"])
    assert result.exit_code == 0
    assert configured_entries(paths, "denied")[0].scope == "local"
