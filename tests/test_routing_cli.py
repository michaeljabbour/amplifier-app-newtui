"""``amplifier-newtui routing`` group wiring (click CliRunner).

Admin logic is unit-tested in ``test_kernel_routing_admin``; this covers the
CLI plumbing (help/subcommands, list table, use roundtrip + unknown reject)
with settings + matrix cache redirected to ``tmp_path``. A bundle-cache
matrix is seeded so discovery never attempts a network fetch.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from amplifier_app_newtui.kernel import bundle_admin
from amplifier_app_newtui.main import main


def _seed_matrix(home: Path, name: str, roles: dict) -> None:
    routing_dir = home / "cache" / "amplifier-bundle-routing-matrix-t" / "routing"
    routing_dir.mkdir(parents=True, exist_ok=True)
    (routing_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "description": f"{name} matrix",
                "updated": "2026-05-12",
                "roles": roles,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _redirect(monkeypatch, tmp_path: Path):
    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")
    monkeypatch.setattr(bundle_admin, "settings_paths", lambda *a, **k: paths)
    return paths


def _roles() -> dict:
    return {
        "general": {"candidates": [{"provider": "anthropic", "model": "claude-sonnet-*"}]},
        "fast": {"candidates": [{"provider": "openai", "model": "gpt-mini"}]},
    }


def test_routing_group_lists_subcommands() -> None:
    result = CliRunner().invoke(main, ["routing", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "use"):
        assert sub in result.output


def test_routing_list_renders_and_marks_active(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    _seed_matrix(tmp_path / "home", "economy", _roles())
    bundle_admin.write_scope(
        paths.global_settings,
        {
            "routing": {"matrix": "economy"},
            "config": {"providers": [{"module": "provider-anthropic"}]},
        },
    )
    result = CliRunner().invoke(main, ["routing", "list"])
    assert result.exit_code == 0
    assert "Routing Matrices" in result.output
    assert "balanced" in result.output
    assert "economy" in result.output
    assert "roles" in result.output  # compatibility column populated


def test_routing_list_empty(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["routing", "list"])
    assert result.exit_code == 0
    assert "no routing matrices found" in result.output


def test_routing_use_roundtrip(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "quality", _roles())
    result = CliRunner().invoke(main, ["routing", "use", "quality"])
    assert result.exit_code == 0
    assert "active routing matrix" in result.output
    data = bundle_admin.read_scope(paths.global_settings)
    assert data["routing"]["matrix"] == "quality"


def test_routing_use_scope_project(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "quality", _roles())
    result = CliRunner().invoke(main, ["routing", "use", "quality", "--project"])
    assert result.exit_code == 0
    assert bundle_admin.read_scope(paths.project_settings)["routing"]["matrix"] == "quality"
    assert not paths.global_settings.is_file()


def test_routing_use_rejects_unknown(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    result = CliRunner().invoke(main, ["routing", "use", "ghost"])
    assert result.exit_code == 1
    assert "unknown matrix: ghost" in result.output
