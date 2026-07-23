"""Source-override administration logic (``kernel/source_admin.py``).

Pure settings/scope-file work over ``tmp_path`` — no amplifier session, no
real ``~/.amplifier``. Mirrors the app-cli ``source`` group behavioral
contract (auto-detect, module vs bundle keys, provider-config cleanup).
"""

from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.kernel import bundle_admin, source_admin


def _paths(tmp_path: Path):
    return bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")


# -- auto-detection ---------------------------------------------------------


def test_detect_module_by_entry_point(tmp_path: Path) -> None:
    mod = tmp_path / "amplifier-thing"
    mod.mkdir()
    (mod / "pyproject.toml").write_text(
        '[project.entry-points."amplifier.modules"]\nx = "x"\n', encoding="utf-8"
    )
    assert source_admin.detect_source_type("thing", str(mod)) == "module"


def test_detect_bundle_by_resource_dirs(tmp_path: Path) -> None:
    bundle = tmp_path / "myteam"
    (bundle / "agents").mkdir(parents=True)
    assert source_admin.detect_source_type("myteam", str(bundle)) == "bundle"


def test_detect_falls_back_to_identifier_prefix(tmp_path: Path) -> None:
    # No local dir to inspect -> naming convention decides.
    missing = str(tmp_path / "nope")
    assert source_admin.detect_source_type("provider-anthropic", missing) == "module"
    assert source_admin.detect_source_type("hooks-routing", missing) == "module"
    assert source_admin.detect_source_type("some-bundle", missing) == "bundle"


def test_is_local_source() -> None:
    assert source_admin.is_local_source("/abs/path")
    assert source_admin.is_local_source("./rel")
    assert not source_admin.is_local_source("git+https://x/y.git")
    assert not source_admin.is_local_source("https://x/y")


# -- add / remove -----------------------------------------------------------


def test_add_and_remove_module_source(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source_admin.add_source(paths, "module", "provider-anthropic", "~/dev/prov", "global")
    data = bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))
    assert data["sources"]["modules"] == {"provider-anthropic": "~/dev/prov"}

    removed_module, removed_bundle = source_admin.remove_source(
        paths, "provider-anthropic", "global"
    )
    assert (removed_module, removed_bundle) == (True, False)
    # Section pruned entirely.
    assert bundle_admin.read_scope(bundle_admin.scope_file(paths, "global")) == {}


def test_add_and_remove_bundle_source(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source_admin.add_source(paths, "bundle", "foundation", "~/dev/foundation", "project")
    data = bundle_admin.read_scope(paths.project_settings)
    assert data["sources"]["bundles"] == {"foundation": "~/dev/foundation"}

    removed_module, removed_bundle = source_admin.remove_source(paths, "foundation", "project")
    assert (removed_module, removed_bundle) == (False, True)


def test_remove_missing_returns_false_false(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert source_admin.remove_source(paths, "ghost", "global") == (False, False)


def test_remove_force_module_only_leaves_bundle(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source_admin.add_source(paths, "module", "dual", "~/m", "global")
    source_admin.add_source(paths, "bundle", "dual", "~/b", "global")
    removed_module, removed_bundle = source_admin.remove_source(
        paths, "dual", "global", module=True, bundle=False
    )
    assert (removed_module, removed_bundle) == (True, False)
    data = bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))
    assert data["sources"]["bundles"] == {"dual": "~/b"}
    assert "modules" not in data["sources"]


def test_add_preserves_other_settings_keys(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    path = bundle_admin.scope_file(paths, "global")
    bundle_admin.write_scope(path, {"bundle": {"active": "newtui"}})
    source_admin.add_source(paths, "module", "tool-x", "/src", "global")
    data = bundle_admin.read_scope(path)
    assert data["bundle"] == {"active": "newtui"}  # untouched
    assert data["sources"]["modules"] == {"tool-x": "/src"}


# -- provider-config cleanup ------------------------------------------------


def test_cleanup_provider_config_drops_local_source(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    path = bundle_admin.scope_file(paths, "global")
    bundle_admin.write_scope(
        path,
        {"config": {"providers": [{"module": "provider-anthropic", "source": "/local/clone"}]}},
    )
    assert source_admin.cleanup_provider_config_source(paths, "provider-anthropic", "global")
    entry = bundle_admin.read_scope(path)["config"]["providers"][0]
    assert "source" not in entry  # foundation resolves the default now


def test_cleanup_provider_config_keeps_git_source(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    path = bundle_admin.scope_file(paths, "global")
    bundle_admin.write_scope(
        path,
        {
            "config": {
                "providers": [{"module": "provider-anthropic", "source": "git+https://x/y.git"}]
            }
        },
    )
    assert not source_admin.cleanup_provider_config_source(paths, "provider-anthropic", "global")
    entry = bundle_admin.read_scope(path)["config"]["providers"][0]
    assert entry["source"] == "git+https://x/y.git"  # remote sources untouched


# -- list / resolve ---------------------------------------------------------


def test_list_sources_merges_modules_then_bundles(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source_admin.add_source(paths, "module", "provider-z", "/m", "global")
    source_admin.add_source(paths, "bundle", "team", "/b", "global")
    entries = source_admin.list_sources(tmp_path / "proj", tmp_path / "home")
    assert [(e.name, e.kind) for e in entries] == [
        ("provider-z", "module"),
        ("team", "bundle"),
    ]


def test_resolve_module_reports_layers(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    source_admin.add_source(paths, "module", "provider-x", "/settings/src", "global")
    # overrides.<id>.source wins over sources.modules for the effective value.
    override_path = bundle_admin.scope_file(paths, "project")
    bundle_admin.write_scope(
        override_path, {"overrides": {"provider-x": {"source": "/override/src"}}}
    )
    monkeypatch.setenv("AMPLIFIER_MODULE_PROVIDER_X", "/env/src")

    report = source_admin.resolve_module("provider-x", tmp_path / "proj", tmp_path / "home")
    assert report.env_var == "AMPLIFIER_MODULE_PROVIDER_X"
    assert report.env_value == "/env/src"
    assert report.settings_source == "/settings/src"
    assert report.effective_source == "/override/src"
    assert report.workspace_found is False


def test_resolve_module_workspace_detected(tmp_path: Path) -> None:
    workspace = tmp_path / "proj" / ".amplifier" / "modules" / "provider-x"
    workspace.mkdir(parents=True)
    report = source_admin.resolve_module("provider-x", tmp_path / "proj", tmp_path / "home")
    assert report.workspace_found is True
    assert report.settings_source is None
