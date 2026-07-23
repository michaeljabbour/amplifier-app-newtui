"""``amplifier-newtui source`` group wiring (click CliRunner).

The admin logic is unit-tested in ``test_kernel_source_admin``; this covers
the CLI plumbing (help/subcommands, auto-detect, scope writes) with settings
redirected to ``tmp_path`` — never the real ~/.amplifier.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from amplifier_app_newtui.kernel import bundle_admin
from amplifier_app_newtui.main import main


def _redirect(monkeypatch, tmp_path: Path):
    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")
    monkeypatch.setattr(bundle_admin, "settings_paths", lambda *a, **k: paths)
    return paths


def test_source_group_lists_subcommands() -> None:
    result = CliRunner().invoke(main, ["source", "--help"])
    assert result.exit_code == 0
    for sub in ("add", "remove", "list", "show"):
        assert sub in result.output


def test_source_add_autodetects_module_by_prefix(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        main, ["source", "add", "provider-anthropic", "/dev/prov"]
    )
    assert result.exit_code == 0
    assert "module source provider-anthropic" in result.output
    data = bundle_admin.read_scope(paths.global_settings)
    assert data["sources"]["modules"] == {"provider-anthropic": "/dev/prov"}


def test_source_add_force_bundle_and_scope(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        main, ["source", "add", "team", "/dev/team", "--bundle", "--project"]
    )
    assert result.exit_code == 0
    assert "bundle source team" in result.output
    data = bundle_admin.read_scope(paths.project_settings)
    assert data["sources"]["bundles"] == {"team": "/dev/team"}
    assert not paths.global_settings.is_file()


def test_source_add_rejects_conflicting_flags(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        main, ["source", "add", "x", "/y", "--module", "--bundle"]
    )
    assert result.exit_code == 1
    assert "both --module and --bundle" in result.output


def test_source_remove_roundtrip(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["source", "add", "tool-x", "/src"])
    removed = runner.invoke(main, ["source", "remove", "tool-x"])
    assert removed.exit_code == 0
    assert "removed module source tool-x" in removed.output
    assert bundle_admin.read_scope(paths.global_settings) == {}


def test_source_remove_missing_reports(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["source", "remove", "ghost"])
    assert result.exit_code == 0
    assert "no source override for ghost" in result.output


def test_source_list_renders_tables(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["source", "add", "provider-z", "/m"])
    runner.invoke(main, ["source", "add", "team", "/b", "--bundle"])
    listed = runner.invoke(main, ["source", "list"])
    assert listed.exit_code == 0
    assert "Source Overrides" in listed.output
    assert "provider-z" in listed.output
    assert "team" in listed.output


def test_source_list_empty(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["source", "list"])
    assert result.exit_code == 0
    assert "no source overrides configured" in result.output


def test_source_show_reports_effective_override(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["source", "add", "provider-x", "/settings/src"])
    shown = runner.invoke(main, ["source", "show", "provider-x"])
    assert shown.exit_code == 0
    assert "module: provider-x" in shown.output
    assert "/settings/src" in shown.output
    assert "effective override" in shown.output
