"""Tests for kernel/config.py — the resolve_config golden path.

Pure parts (settings merge, discovery, overrides) are tested directly;
the async golden path is exercised end-to-end against a tiny local
bundle with no modules (offline, no API keys).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_newtui.kernel.config import (
    DEFAULT_BUNDLE,
    BundleNotFoundError,
    SettingsPaths,
    active_bundle_name,
    apply_module_overrides,
    build_source_resolver,
    bundle_search_paths,
    deep_merge,
    discover_bundle,
    expand_env_placeholders,
    get_project_slug,
    is_bundle_uri,
    list_available_bundles,
    load_merged_settings,
    overlay_uris,
    packaged_bundles_dir,
    resolve_config,
)

# --------------------------------------------------------------------------
# deep_merge / settings
# --------------------------------------------------------------------------


def test_deep_merge_nested_overlay_wins() -> None:
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    overlay = {"a": {"y": 3, "z": 4}, "c": 5}
    merged = deep_merge(base, overlay)
    assert merged == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1, "c": 5}
    # inputs untouched
    assert base == {"a": {"x": 1, "y": 2}, "b": 1}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_settings_three_scope_merge_most_specific_wins(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"
    paths = SettingsPaths.default(project, home)
    _write(paths.global_settings, "bundle:\n  active: global-bundle\ntheme: slate\n")
    _write(paths.project_settings, "bundle:\n  active: proj-bundle\n")
    _write(paths.local_settings, "theme: carbon\n")

    settings = load_merged_settings(paths)
    assert settings["bundle"]["active"] == "proj-bundle"  # project beats global
    assert settings["theme"] == "carbon"  # local beats global
    assert active_bundle_name(settings) == "proj-bundle"


def test_settings_missing_and_malformed_files_skipped(tmp_path: Path) -> None:
    paths = SettingsPaths.default(tmp_path / "p", tmp_path / "h")
    _write(paths.global_settings, ": not: valid: yaml: [\n")
    settings = load_merged_settings(paths)
    assert settings == {}


def test_overlay_uris_and_active_bundle_defaults() -> None:
    assert overlay_uris({}) == ()
    assert active_bundle_name({}) is None
    settings = {"bundle": {"app": ["git+https://x/a@main", "git+https://x/b@main"]}}
    assert overlay_uris(settings) == ("git+https://x/a@main", "git+https://x/b@main")


def test_build_source_resolver_precedence() -> None:
    settings = {
        "sources": {"modules": {"tool-a": "/general/a", "tool-b": "/general/b"}},
        "overrides": {"tool-b": {"source": "/specific/b"}},
    }
    resolve = build_source_resolver(settings)
    assert resolve("tool-a", "git+orig") == "/general/a"
    assert resolve("tool-b", "git+orig") == "/specific/b"  # overrides win
    assert resolve("tool-c", "git+orig") == "git+orig"  # passthrough


# --------------------------------------------------------------------------
# bundle discovery
# --------------------------------------------------------------------------


def test_discover_bundle_precedence_project_user_packaged(tmp_path: Path) -> None:
    project = tmp_path / "proj" / ".amplifier" / "bundles"
    user = tmp_path / "home" / "bundles"
    _write(user / "mybundle.md", "---\nbundle:\n  name: mybundle\n---\n")
    paths = bundle_search_paths(tmp_path / "proj", tmp_path / "home")

    assert discover_bundle("mybundle", paths) == str(user / "mybundle.md")

    # project copy takes precedence once present
    _write(project / "mybundle.md", "---\nbundle:\n  name: mybundle\n---\n")
    assert discover_bundle("mybundle", paths) == str(project / "mybundle.md")


def test_discover_bundle_directory_and_yaml_forms(tmp_path: Path) -> None:
    base = tmp_path / "bundles"
    _write(base / "dirbundle" / "bundle.md", "---\nbundle:\n  name: dirbundle\n---\n")
    _write(base / "yamlbundle.yaml", "bundle:\n  name: yamlbundle\n")
    assert discover_bundle("dirbundle", [base]) == str(base / "dirbundle" / "bundle.md")
    assert discover_bundle("yamlbundle", [base]) == str(base / "yamlbundle.yaml")
    assert discover_bundle("missing", [base]) is None


def test_discover_bundle_uri_passthrough(tmp_path: Path) -> None:
    uri = "git+https://github.com/org/bundle@main"
    assert is_bundle_uri(uri)
    assert discover_bundle(uri, []) == uri


def test_packaged_default_bundle_is_discoverable(tmp_path: Path) -> None:
    paths = bundle_search_paths(tmp_path, tmp_path / "home")
    found = discover_bundle(DEFAULT_BUNDLE, paths)
    assert found is not None
    assert Path(found) == packaged_bundles_dir() / "newtui.md"


def test_list_available_bundles(tmp_path: Path) -> None:
    base = tmp_path / "bundles"
    _write(base / "alpha.md", "x")
    _write(base / "beta" / "bundle.md", "x")
    _write(base / "notabundle.txt", "x")
    assert list_available_bundles([base]) == ("alpha", "beta")


# --------------------------------------------------------------------------
# mount-plan overrides (in place — no dual representation)
# --------------------------------------------------------------------------


def test_apply_module_overrides_merges_in_place() -> None:
    mount_plan = {
        "providers": [{"module": "provider-anthropic", "config": {"priority": 1}}],
        "tools": [{"module": "tool-filesystem", "config": {"allowed_write_paths": ["/a"]}}],
    }
    settings = {
        "config": {
            "providers": [
                {"module": "provider-anthropic", "config": {"default_model": "claude-x"}},
                {"module": "provider-openai", "config": {"priority": 10}},
            ]
        },
        "modules": {
            "tools": [{"module": "tool-filesystem", "config": {"allowed_write_paths": ["/b"]}}]
        },
    }
    result = apply_module_overrides(mount_plan, settings)
    assert result is mount_plan  # SAME object — no drift
    anthropic = mount_plan["providers"][0]
    assert anthropic["config"] == {"priority": 1, "default_model": "claude-x"}
    assert mount_plan["providers"][1]["module"] == "provider-openai"  # appended
    assert mount_plan["tools"][0]["config"]["allowed_write_paths"] == ["/b"]


def test_apply_generic_overrides_before_specific() -> None:
    mount_plan = {"providers": [{"module": "provider-anthropic", "config": {"a": 1}}]}
    settings = {
        "overrides": {"provider-anthropic": {"config": {"a": 2, "b": 3}}},
        "config": {"providers": [{"module": "provider-anthropic", "config": {"a": 9}}]},
    }
    apply_module_overrides(mount_plan, settings)
    # generic applied first, specific config.providers wins on overlap
    assert mount_plan["providers"][0]["config"] == {"a": 9, "b": 3}


# --------------------------------------------------------------------------
# project slug
# --------------------------------------------------------------------------


def test_expand_env_placeholders_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """``${VAR}``/``${VAR:default}`` expand in place (amplifier-app-cli
    ``expand_env_vars`` parity); a whole-value unset ``${VAR}`` is DROPPED
    so providers fall back to their SDK defaults instead of getting ""."""
    monkeypatch.setenv("NEWTUI_TEST_URL", "https://example.test")
    monkeypatch.delenv("NEWTUI_TEST_UNSET", raising=False)
    plan = {
        "providers": [
            {
                "module": "provider-anthropic",
                "config": {
                    "base_url": "${NEWTUI_TEST_URL}",
                    "unset_whole": "${NEWTUI_TEST_UNSET}",
                    "unset_partial": "prefix-${NEWTUI_TEST_UNSET}",
                    "with_default": "${NEWTUI_TEST_UNSET:https://default.test}",
                    "nested": ["${NEWTUI_TEST_URL}/v1", 7],
                },
            }
        ],
        "untouched": 42,
    }
    inner = plan["providers"][0]["config"]
    result = expand_env_placeholders(plan)
    assert result is plan  # in place — mount_plan identity preserved
    assert plan["providers"][0]["config"] is inner
    assert inner["base_url"] == "https://example.test"
    assert "unset_whole" not in inner  # dropped, not ""
    assert inner["unset_partial"] == "prefix-"  # embedded stays reference-compatible
    assert inner["with_default"] == "https://default.test"
    assert inner["nested"] == ["https://example.test/v1", 7]
    assert plan["untouched"] == 42


def test_get_project_slug(tmp_path: Path) -> None:
    slug = get_project_slug(tmp_path)
    assert slug.startswith("-")
    assert "/" not in slug and ":" not in slug


# --------------------------------------------------------------------------
# resolve_config golden path (offline, tiny local bundle)
# --------------------------------------------------------------------------

MINI_BUNDLE = """---
bundle:
  name: mini
  version: 0.0.1
  description: offline test bundle with no modules
---

Test instruction body.
"""


@pytest.mark.asyncio
async def test_resolve_config_golden_path_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))  # keep foundation state in tmp
    _write(project / ".amplifier" / "bundles" / "mini.md", MINI_BUNDLE)
    _write(
        project / ".amplifier" / "settings.yaml",
        "config:\n  providers:\n    - module: provider-anthropic\n      config:\n        priority: 1\n",
    )

    resolved = await resolve_config(
        "mini", project_dir=project, amplifier_home=home, install_deps=False
    )

    assert resolved.bundle_name == "mini"
    assert resolved.bundle_uri.endswith("mini.md")
    assert resolved.overlays == ()
    # settings provider override landed in the prepared mount plan itself
    assert resolved.mount_plan is resolved.prepared.mount_plan
    providers = resolved.mount_plan.get("providers") or []
    assert any(p.get("module") == "provider-anthropic" for p in providers)


@pytest.mark.asyncio
async def test_resolve_config_unknown_bundle_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path / "home"))
    with pytest.raises(BundleNotFoundError) as excinfo:
        await resolve_config(
            "definitely-not-a-bundle",
            project_dir=tmp_path / "proj",
            amplifier_home=tmp_path / "home",
        )
    assert "definitely-not-a-bundle" in str(excinfo.value)


def test_packaged_bundle_matches_repo_root_bundle() -> None:
    """The packaged default bundle is a byte-for-byte copy of the repo-root
    bundle.md (NOTES-kernel-runtime contract: edit one → re-copy the other)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    packaged = root / "src" / "amplifier_app_newtui" / "data" / "bundles" / "newtui.md"
    assert packaged.read_bytes() == (root / "bundle.md").read_bytes()
