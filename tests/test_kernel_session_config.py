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
    ensure_project_write_path,
    expand_env_placeholders,
    get_project_slug,
    is_bundle_uri,
    list_available_bundles,
    load_keys_env,
    load_merged_settings,
    map_provider_ids_to_instance_ids,
    overlay_uris,
    packaged_bundles_dir,
    resolve_config,
)
from amplifier_app_newtui.kernel.compaction import (
    CompactionConfig,
    CompactionRuntimeBinding,
    apply_compaction_settings,
    compaction_config,
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


def test_discover_bundle_plain_local_paths(tmp_path: Path) -> None:
    # A plain path to an existing bundle file/dir resolves without a URI
    # prefix or a search-path hit (foundation's load_bundle takes it directly).
    bundle = tmp_path / "bundles" / "dev.md"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("---\nbundle:\n  name: dev\n---\n")
    assert discover_bundle(str(bundle), []) == str(bundle)  # absolute file
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "bundle.md").write_text("---\nbundle:\n  name: pkg\n---\n")
    assert discover_bundle(str(pkg), []) == str(pkg / "bundle.md")  # dir → bundle.md
    assert discover_bundle(str(tmp_path / "nope.md"), []) is None  # missing path


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
    assert mount_plan["tools"][0]["config"]["allowed_write_paths"] == ["/a", "/b"]


def test_context_compaction_settings_apply_to_effective_mount_plan() -> None:
    mount_plan = {
        "session": {
            "context": {
                "module": "context-simple",
                "config": {
                    "max_tokens": 200_000,
                    "compact_threshold": 0.8,
                    "auto_compact": True,
                },
            }
        }
    }
    result = apply_compaction_settings(
        mount_plan,
        {
            "context": {
                "max_tokens": 128_000,
                "compact_threshold": 0.7,
                "auto_compact": False,
            }
        },
    )
    assert result is mount_plan
    assert mount_plan["session"]["context"]["config"] == {
        "max_tokens": 128_000,
        "compact_threshold": 0.7,
        "auto_compact": False,
    }
    assert compaction_config(mount_plan).threshold_tokens == 89_600


def test_context_compaction_settings_support_legacy_top_level_mount() -> None:
    mount_plan = {
        "context": {
            "module": "context-simple",
            "config": {"max_tokens": 200_000, "auto_compact": True},
        }
    }
    apply_compaction_settings(
        mount_plan,
        {"context": {"max_tokens": 64_000, "auto_compact": False}},
    )
    assert mount_plan["context"]["config"] == {
        "max_tokens": 64_000,
        "auto_compact": False,
    }
    assert compaction_config(mount_plan).max_tokens == 64_000


def test_native_session_context_wins_over_legacy_top_level_mount() -> None:
    mount_plan = {
        "session": {
            "context": {
                "module": "context-simple",
                "config": {"max_tokens": 200_000},
            }
        },
        "context": {
            "module": "context-simple",
            "config": {"max_tokens": 32_000},
        },
    }
    apply_compaction_settings(mount_plan, {"context": {"max_tokens": 96_000}})
    assert mount_plan["session"]["context"]["config"]["max_tokens"] == 96_000
    assert mount_plan["context"]["config"]["max_tokens"] == 32_000
    assert compaction_config(mount_plan).max_tokens == 96_000


def test_runtime_binding_disables_legacy_threshold_only_context() -> None:
    class LegacyContext:
        max_tokens = 200_000
        compact_threshold = 0.8

    context = LegacyContext()
    effective = CompactionRuntimeBinding(
        context,
        CompactionConfig(
            max_tokens=128_000,
            compact_threshold=0.7,
            auto_compact=False,
        ),
    ).apply()
    assert context.max_tokens == 128_000
    assert context.compact_threshold == float("inf")
    assert effective.accounting == "estimated"


@pytest.mark.asyncio
async def test_runtime_binding_uses_native_switch_and_observed_accounting() -> None:
    class ModernContext:
        max_tokens = 200_000
        compact_threshold = 0.8
        auto_compact = True

        def __init__(self) -> None:
            self.observed: list[int] = []

        async def record_observed_input_tokens(self, tokens: int) -> None:
            self.observed.append(tokens)

    context = ModernContext()
    binding = CompactionRuntimeBinding(
        context,
        CompactionConfig(compact_threshold=0.75, auto_compact=False),
    )
    effective = binding.apply()
    assert context.auto_compact is False
    assert context.compact_threshold == 0.75
    assert effective.accounting == "provider-observed"
    assert await binding.observe_input_tokens(12_345)
    assert context.observed == [12_345]


def test_invalid_context_compaction_settings_are_ignored(caplog) -> None:
    mount_plan = {
        "session": {
            "context": {
                "module": "context-simple",
                "config": {"max_tokens": 200_000, "compact_threshold": 0.8},
            }
        }
    }
    apply_compaction_settings(
        mount_plan,
        {"context": {"max_tokens": -1, "compact_threshold": 2, "auto_compact": "yes"}},
    )
    assert mount_plan["session"]["context"]["config"] == {
        "max_tokens": 200_000,
        "compact_threshold": 0.8,
    }
    assert "Ignoring invalid context.max_tokens" in caplog.text


def test_context_compaction_settings_do_not_leak_into_other_modules(caplog) -> None:
    mount_plan = {"session": {"context": {"module": "context-custom", "config": {"own": True}}}}
    apply_compaction_settings(mount_plan, {"context": {"auto_compact": True}})
    assert mount_plan["session"]["context"]["config"] == {"own": True}
    assert "is not context-simple" in caplog.text


def test_permission_paths_union_across_settings_scopes(tmp_path: Path) -> None:
    paths = SettingsPaths.default(tmp_path / "project", tmp_path / "home")
    _write(
        paths.global_settings,
        "modules:\n  tools:\n    - module: tool-filesystem\n"
        "      config:\n        allowed_write_paths: [/global]\n"
        "        denied_write_paths: [/blocked-global]\n",
    )
    _write(
        paths.project_settings,
        "modules:\n  tools:\n    - module: tool-filesystem\n"
        "      config:\n        allowed_write_paths: [/project-extra]\n"
        "        denied_write_paths: [/blocked-project]\n",
    )
    settings = load_merged_settings(paths)
    config = settings["modules"]["tools"][0]["config"]
    assert config["allowed_write_paths"] == ["/global", "/project-extra"]
    assert config["denied_write_paths"] == ["/blocked-global", "/blocked-project"]


def test_project_path_is_always_preserved_in_filesystem_allowlist(tmp_path: Path) -> None:
    project = tmp_path / "project"
    plan = {
        "tools": [
            {
                "module": "tool-filesystem",
                "config": {"allowed_write_paths": [str(tmp_path / "shared")]},
            }
        ]
    }
    ensure_project_write_path(plan, project)
    assert plan["tools"][0]["config"]["allowed_write_paths"] == [
        str(project.resolve()),
        str((tmp_path / "shared").resolve()),
    ]


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
# keys.env loading + provider id→instance_id (reference-CLI parity)
# --------------------------------------------------------------------------


def test_load_keys_env_sets_missing_and_never_clobbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "keys.env").write_text(
        "# comment\n"
        'VLLM_BASE_URL="https://vllm.test/v1"\n'
        "VLLM_API_KEY=secret\n"
        "ALREADY_SET=from_file\n"
        "\n"
    )
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    monkeypatch.setenv("ALREADY_SET", "from_env")  # exported env must win

    load_keys_env(tmp_path)

    import os

    assert os.environ["VLLM_BASE_URL"] == "https://vllm.test/v1"  # quotes stripped
    assert os.environ["VLLM_API_KEY"] == "secret"
    assert os.environ["ALREADY_SET"] == "from_env"  # not clobbered


def test_load_keys_env_missing_file_is_noop(tmp_path: Path) -> None:
    load_keys_env(tmp_path)  # no keys.env — must not raise


def test_map_provider_ids_to_instance_ids() -> None:
    plan = {
        "providers": [
            {"module": "provider-anthropic"},  # no id — left as default
            {"module": "provider-vllm", "id": "openmj"},  # id → instance_id
            {"module": "provider-x", "id": "x", "instance_id": "keep"},  # respected
        ]
    }
    map_provider_ids_to_instance_ids(plan)
    assert "instance_id" not in plan["providers"][0]
    assert plan["providers"][1]["instance_id"] == "openmj"
    assert plan["providers"][2]["instance_id"] == "keep"


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


# A local bundle that already mounts a *sourceless* hooks-routing hook. No
# ``source:`` ⇒ Bundle.prepare() never adds it to modules_to_activate and
# never touches the network, so this exercises the settings→hook bridge that
# resolve_config runs unconditionally (inject_routing_config) fully offline.
ROUTING_LOCAL_BUNDLE = """---
bundle:
  name: routing-local
  version: 0.0.1
  description: offline bundle that already mounts a sourceless hooks-routing

hooks:
  - module: hooks-routing
    config:
      default_matrix: balanced
---

Test instruction body.
"""


@pytest.mark.asyncio
async def test_resolve_config_bridges_routing_settings_into_mounted_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The golden path patches a mounted hooks-routing from settings.routing.

    ``routing.enabled: false`` keeps this offline (no routing-matrix overlay
    fetch) while ``routing.matrix`` / ``routing.overrides`` still drive the
    bridge — proving inject_routing_config runs inside resolve_config and
    lands on the hook the bundle mounted.
    """
    project = tmp_path / "proj"
    home = tmp_path / "home"
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))
    _write(project / ".amplifier" / "bundles" / "routing-local.md", ROUTING_LOCAL_BUNDLE)
    _write(
        project / ".amplifier" / "settings.yaml",
        "routing:\n"
        "  enabled: false\n"
        "  matrix: anthropic\n"
        "  overrides:\n"
        "    coding:\n"
        "      candidates: []\n",
    )
    (home / "routing").mkdir(parents=True)

    resolved = await resolve_config(
        "routing-local", project_dir=project, amplifier_home=home, install_deps=False
    )

    # No overlay was composed — enabled:false suppressed the network fetch.
    assert resolved.overlays == ()
    hooks = resolved.mount_plan.get("hooks") or []
    routing = next(h for h in hooks if h.get("module") == "hooks-routing")
    assert routing["config"]["default_matrix"] == "anthropic"  # settings won
    assert routing["config"]["overrides"] == {"coding": {"candidates": []}}
    assert str(home / "routing") in routing["config"]["custom_routing_dirs"]


CONTEXT_SIMPLE_BUNDLE = """---
bundle:
  name: context-simple-default
  version: 0.0.1
  description: offline bundle mirroring Foundation's session.context shape

session:
  context:
    module: context-simple
    config:
      max_tokens: 200000
      compact_threshold: 0.8
      auto_compact: true
---

Test instruction body.
"""


@pytest.mark.asyncio
async def test_resolve_config_applies_compaction_to_prepared_default_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise Foundation's real ``session.context`` prepared-plan shape.

    Uses a local bundle declaring ``session.context`` directly (no ``source:``
    on the module spec, so ``Bundle.prepare()`` never adds it to
    ``modules_to_activate`` / never touches the network) rather than the
    repo-root ``bundle.md`` \u2014 that file's ``includes:`` is a SHA-pinned
    ``git+https://`` fetch of Foundation's anchors bundle, and resolving it
    live would break this file's documented offline-only convention (see
    module docstring) and depends on network reachability this suite must
    not require.
    """

    project = tmp_path / "project"
    home = tmp_path / "home"
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))
    _write(
        project / ".amplifier" / "bundles" / "context-simple-default.md",
        CONTEXT_SIMPLE_BUNDLE,
    )
    _write(
        project / ".amplifier" / "settings.local.yaml",
        "context:\n  max_tokens: 128000\n  compact_threshold: 0.7\n  auto_compact: false\n",
    )

    resolved = await resolve_config(
        "context-simple-default",
        project_dir=project,
        amplifier_home=home,
        install_deps=False,
    )

    context = resolved.mount_plan["session"]["context"]
    assert context["module"] == "context-simple"
    assert context["config"] == {
        "max_tokens": 128_000,
        "compact_threshold": 0.7,
        "auto_compact": False,
    }
    assert compaction_config(resolved.mount_plan) == CompactionConfig(
        max_tokens=128_000,
        compact_threshold=0.7,
        auto_compact=False,
    )


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


def test_packaged_bundle_declares_cli_response_contract() -> None:
    from amplifier_app_newtui.kernel.config import packaged_bundles_dir

    text = (packaged_bundles_dir() / "newtui.md").read_text(encoding="utf-8")
    contract = """## Terminal response contract

You are Amplifier, driven through a full-screen terminal UI. Prefer running
tools over speculating. This surface renders a supported Markdown subset:

- Lead with the answer, result, or current blocker.
- Default to short, direct responses with small paragraphs or flat lists.
- Do not repeat the prompt, tool logs, task state, or internal narration that
  the UI already displays.
- Close implementation work with what changed, verification, and any blocker
  or required next action.
- Do not emit Markdown images. Keep tables to four columns or fewer and lists
  shallow.
- Put layout-sensitive or copyable structured content in language-tagged fenced
  code blocks.
- Expand only when the user asks or correctness requires the detail.
"""
    assert contract in text


# -- bare-name resolution + graceful fallback (Samuel's feedback, 2026-07-21) --


def test_packaged_anchors_pointer_resolves_and_matches_the_wrapper_pin() -> None:
    """`bundle.active: anchors` (a valid app-cli default) must resolve in
    newtui too — a packaged pointer at the same pinned foundation SHA."""
    import re

    paths = bundle_search_paths(Path("/nonexistent-proj"), Path("/nonexistent-home"))
    uri = discover_bundle("anchors", paths)
    assert uri is not None and uri.endswith("anchors.md")
    newtui_uri = discover_bundle("newtui", paths)
    assert newtui_uri is not None
    pin = re.search(r"amplifier-foundation@([0-9a-f]{40})", Path(newtui_uri).read_text())
    assert pin is not None
    assert f"amplifier-foundation@{pin.group(1)}" in Path(uri).read_text()


def test_settings_bundle_falls_back_to_default_with_notice(tmp_path: Path) -> None:
    """A settings-configured bundle that can't resolve must degrade to the
    packaged default with a loud notice — not kill the boot ('session
    failed to start · Bundle 'x' not found')."""
    from amplifier_app_newtui.kernel.config import resolve_bundle_source

    paths = bundle_search_paths(tmp_path, tmp_path / "home")
    name, uri, notice = resolve_bundle_source(None, {"bundle": {"active": "missing-bundle"}}, paths)
    assert name == DEFAULT_BUNDLE
    assert uri.endswith("newtui.md")
    assert notice is not None and "missing-bundle" in notice and DEFAULT_BUNDLE in notice


def test_explicit_bundle_flag_still_fails_loud(tmp_path: Path) -> None:
    from amplifier_app_newtui.kernel.config import resolve_bundle_source

    paths = bundle_search_paths(tmp_path, tmp_path / "home")
    with pytest.raises(BundleNotFoundError):
        resolve_bundle_source("missing-bundle", {}, paths)
