"""Bundle management logic (``kernel/bundle_admin.py``).

Settings/discovery paths only — the foundation-backed ``load_bundle_info``
/ ``check_updates`` need a real bundle source and are exercised by the
CLI smoke path, not here. Everything below runs against ``tmp_path`` so no
real ``~/.amplifier`` file is read or written.
"""

from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.kernel import bundle_admin


def _paths(tmp_path: Path):
    return bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")


def test_set_and_current_active_bundle_roundtrip(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    bundle_admin.set_active_bundle(paths, "superpowers", "global")
    assert bundle_admin.current_bundle(tmp_path / "proj", tmp_path / "home") == "superpowers"


def test_clear_active_bundle(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    bundle_admin.set_active_bundle(paths, "x", "global")
    assert bundle_admin.clear_active_bundle(paths, "global") is True
    assert bundle_admin.current_bundle(tmp_path / "proj", tmp_path / "home") is None
    # Second clear is a no-op.
    assert bundle_admin.clear_active_bundle(paths, "global") is False


def test_write_scope_preserves_other_keys(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    path = bundle_admin.scope_file(paths, "global")
    bundle_admin.write_scope(path, {"providers": {"anthropic": {"model": "m1"}}})
    bundle_admin.set_active_bundle(paths, "b", "global")
    data = bundle_admin.read_scope(path)
    assert data["providers"] == {"anthropic": {"model": "m1"}}  # untouched
    assert data["bundle"]["active"] == "b"


def test_add_and_remove_registered_bundle(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    bundle_admin.add_bundle(paths, "team", "git+https://x/team.git", "global")
    data = bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))
    assert data["bundle"]["added"] == {"team": "git+https://x/team.git"}
    assert "app" not in data["bundle"]  # not an overlay

    assert bundle_admin.remove_bundle(paths, "team", "global") is True
    data = bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))
    assert "bundle" not in data  # section emptied and dropped
    assert bundle_admin.remove_bundle(paths, "team", "global") is False


def test_add_app_bundle_also_registers_overlay(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    uri = "git+https://x/overlay.git"
    bundle_admin.add_bundle(paths, "overlay", uri, "global", as_app=True)
    section = bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))["bundle"]
    assert section["added"] == {"overlay": uri}
    assert section["app"] == [uri]
    # Removing it drops both the registry entry and the overlay URI.
    bundle_admin.remove_bundle(paths, "overlay", "global")
    assert bundle_admin.read_scope(bundle_admin.scope_file(paths, "global")) == {}


def test_list_bundles_merges_discovered_and_registered(tmp_path: Path) -> None:
    # A discovered on-disk bundle in the project search path.
    bundles_dir = tmp_path / "proj" / ".amplifier" / "bundles"
    bundles_dir.mkdir(parents=True)
    (bundles_dir / "local-one.md").write_text("# bundle\n", encoding="utf-8")

    paths = _paths(tmp_path)
    bundle_admin.add_bundle(paths, "remote-two", "git+https://x/two.git", "global")
    bundle_admin.set_active_bundle(paths, "local-one", "global")

    entries = {e.name: e for e in bundle_admin.list_bundles(tmp_path / "proj", tmp_path / "home")}
    assert entries["local-one"].source == "local"
    assert entries["local-one"].active is True
    assert entries["remote-two"].source == "added"
    assert entries["remote-two"].active is False


def test_scope_selection_writes_to_the_right_file(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    bundle_admin.set_active_bundle(paths, "p", "project")
    assert paths.project_settings.is_file()
    assert not paths.global_settings.is_file()
    assert bundle_admin.read_scope(paths.project_settings)["bundle"]["active"] == "p"
