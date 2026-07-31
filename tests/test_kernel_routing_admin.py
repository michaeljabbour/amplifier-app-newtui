"""Routing-matrix discovery + selection logic (``kernel/routing_admin.py``).

Filesystem/settings work over ``tmp_path`` (a scoped ``amplifier_home``) — no
network, no session. Seeds both matrix sources: the composed-bundle cache and
the user routing dir. Mirrors the app-cli ``routing list/use`` contract.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from amplifier_app_tui.kernel import bundle_admin, routing_admin


def _write_matrix(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _balanced() -> dict:
    return {
        "name": "balanced",
        "description": "Quality/cost balance.",
        "updated": "2026-05-12",
        "roles": {
            "general": {
                "description": "catch-all",
                "candidates": [
                    {"provider": "anthropic", "model": "claude-sonnet-*"},
                    {"provider": "openai", "model": "gpt-x"},
                ],
            },
            "fast": {
                "description": "quick",
                "candidates": [{"provider": "openai", "model": "gpt-mini"}],
            },
        },
    }


def _seed(home: Path) -> None:
    # Bundle-cache matrix + a user matrix.
    _write_matrix(
        home / "cache" / "amplifier-bundle-routing-matrix-abc" / "routing" / "balanced.yaml",
        _balanced(),
    )
    _write_matrix(
        home / "routing" / "mine.yaml",
        {"name": "mine", "description": "custom", "updated": "2026-07-01", "roles": {}},
    )


# -- discovery --------------------------------------------------------------


def test_discover_matrix_files_bundle_and_user(tmp_path: Path) -> None:
    _seed(tmp_path)
    files = routing_admin.discover_matrix_files(tmp_path)
    names = {p.name for p in files}
    assert names == {"balanced.yaml", "mine.yaml"}


def test_discover_empty_home_no_fetch(tmp_path: Path) -> None:
    assert routing_admin.discover_matrix_files(tmp_path, fetch=False) == []


def test_load_all_matrices_keys_by_name_skips_nameless(tmp_path: Path) -> None:
    _write_matrix(tmp_path / "routing" / "ok.yaml", {"name": "ok", "roles": {}})
    _write_matrix(tmp_path / "routing" / "bad.yaml", {"roles": {}})  # no name
    matrices = routing_admin.load_all_matrices(routing_admin.discover_matrix_files(tmp_path))
    assert set(matrices) == {"ok"}


# -- compatibility / resolution --------------------------------------------


def test_configured_provider_types_module_and_id() -> None:
    settings = {
        "config": {
            "providers": [
                {"module": "provider-anthropic"},
                {"module": "provider-chat-completions", "id": "qwen-3.6"},
            ]
        }
    }
    assert routing_admin.configured_provider_types(settings) == {
        "anthropic",
        "chat-completions",
        "qwen-3.6",
    }


def test_check_compatibility_counts_covered_roles() -> None:
    # Only anthropic configured -> general covered, fast not.
    covered, total = routing_admin.check_compatibility(_balanced(), {"anthropic"})
    assert (covered, total) == (1, 2)


def test_resolve_matrix_picks_first_configured_candidate() -> None:
    rows = {
        r.role: (r.model, r.provider) for r in routing_admin.resolve_matrix(_balanced(), {"openai"})
    }
    # anthropic not configured -> general falls through to openai/gpt-x.
    assert rows["general"] == ("gpt-x", "openai")
    assert rows["fast"] == ("gpt-mini", "openai")


def test_resolve_matrix_marks_unservable_role_none() -> None:
    rows = {r.role: (r.model, r.provider) for r in routing_admin.resolve_matrix(_balanced(), set())}
    assert rows["general"] == (None, None)


# -- active matrix ----------------------------------------------------------


def test_active_matrix_default_and_from_settings() -> None:
    assert routing_admin.active_matrix({}) == "balanced"
    assert routing_admin.active_matrix({"routing": {"matrix": "quality"}}) == "quality"


def test_set_active_matrix_roundtrip_preserves_overrides(tmp_path: Path) -> None:
    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")
    path = bundle_admin.scope_file(paths, "global")
    bundle_admin.write_scope(path, {"routing": {"overrides": {"coding": "x"}}})
    routing_admin.set_active_matrix(paths, "economy", "global")
    routing = bundle_admin.read_scope(path)["routing"]
    assert routing["matrix"] == "economy"
    assert routing["overrides"] == {"coding": "x"}  # preserved


# -- list integration -------------------------------------------------------


def test_list_matrices_marks_active_and_compat(tmp_path: Path) -> None:
    _seed(tmp_path)
    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path)
    bundle_admin.write_scope(
        bundle_admin.scope_file(paths, "global"),
        {
            "routing": {"matrix": "balanced"},
            "config": {"providers": [{"module": "provider-anthropic"}]},
        },
    )
    entries = {e.name: e for e in routing_admin.list_matrices(tmp_path / "proj", tmp_path)}
    assert entries["balanced"].active is True
    assert entries["balanced"].has_providers is True
    assert (entries["balanced"].covered, entries["balanced"].total) == (1, 2)
    assert entries["mine"].active is False


# -- provider selectors -----------------------------------------------------


def test_provider_selectors_single_instance_uses_type_name() -> None:
    settings = {"config": {"providers": [{"module": "provider-anthropic"}]}}
    assert routing_admin.provider_selectors(settings) == ["anthropic"]


def test_provider_selectors_multi_instance_uses_id() -> None:
    settings = {
        "config": {
            "providers": [
                {"module": "provider-anthropic"},
                {"module": "provider-chat-completions", "id": "qwen"},
                {"module": "provider-chat-completions", "id": "ornith"},
            ]
        }
    }
    # Ambiguous module -> instance ids; single-instance module -> type name.
    assert routing_admin.provider_selectors(settings) == ["anthropic", "qwen", "ornith"]


def test_provider_default_model_and_primary() -> None:
    settings = {
        "config": {
            "providers": [
                {"module": "provider-anthropic", "config": {"default_model": "claude-opus"}},
                {"module": "provider-openai"},
            ]
        }
    }
    assert routing_admin.provider_default_model(settings, "anthropic") == "claude-opus"
    assert routing_admin.provider_default_model(settings, "openai") is None
    assert routing_admin.primary_provider_type(settings) == "anthropic"


def test_provider_selectors_empty_when_none_configured() -> None:
    assert routing_admin.provider_selectors({}) == []
    assert routing_admin.primary_provider_type({}) is None


# -- effective resolution (show) -------------------------------------------


def test_resolve_effective_applies_default_model() -> None:
    settings = {
        "config": {
            "providers": [
                {"module": "provider-anthropic", "config": {"default_model": "claude-opus"}},
            ]
        }
    }
    rows = {r.role: r for r in routing_admin.resolve_effective(_balanced(), settings)}
    # general -> anthropic candidate; display model overridden by default_model.
    assert (rows["general"].provider, rows["general"].model) == ("anthropic", "claude-opus")
    # fast has no anthropic candidate -> unservable.
    assert rows["fast"].provider is None and rows["fast"].model is None


def test_matrix_waterfall_flags_active_and_missing() -> None:
    roles = {r.role: r for r in routing_admin.matrix_waterfall(_balanced(), {"openai"})}
    general = roles["general"]
    assert general.servable is True
    # anthropic candidate not configured; openai is the active winner.
    assert [(c.provider, c.configured, c.active) for c in general.candidates] == [
        ("anthropic", False, False),
        ("openai", True, True),
    ]


def test_matrix_waterfall_unservable_when_no_provider() -> None:
    rows = {r.role: r for r in routing_admin.matrix_waterfall(_balanced(), set())}
    assert rows["general"].servable is False
    assert all(not c.active for c in rows["general"].candidates)


# -- role discovery + custom matrix authoring ------------------------------


def test_discover_roles_first_description_wins(tmp_path: Path) -> None:
    _write_matrix(
        tmp_path / "routing" / "a.yaml",
        {"name": "a", "roles": {"general": {"description": "first"}}},
    )
    _write_matrix(
        tmp_path / "routing" / "b.yaml",
        {"name": "b", "roles": {"general": {"description": "second"}, "fast": {}}},
    )
    roles = routing_admin.discover_roles(routing_admin.discover_matrix_files(tmp_path))
    assert roles["general"] == "first"
    assert roles["fast"] == ""


def test_matrix_name_valid() -> None:
    assert routing_admin.matrix_name_valid("my-matrix_1")
    assert not routing_admin.matrix_name_valid("-leading")
    assert not routing_admin.matrix_name_valid("has space")
    assert not routing_admin.matrix_name_valid("")


def test_build_and_save_custom_matrix_roundtrip(tmp_path: Path) -> None:
    assignments = {
        "general": {"description": "catch-all", "provider": "anthropic", "model": "claude-x"},
        "fast": {"description": "quick", "provider": "openai", "model": "gpt-mini"},
    }
    data = routing_admin.build_custom_matrix("mine", assignments, updated="2026-07-24")
    assert data["name"] == "mine"
    assert data["updated"] == "2026-07-24"
    assert data["roles"]["general"]["candidates"] == [
        {"provider": "anthropic", "model": "claude-x"}
    ]

    out_dir = routing_admin.custom_routing_dir(tmp_path)
    saved = routing_admin.save_matrix(data, out_dir)
    assert saved == out_dir / "mine.yaml"
    # Re-discoverable + resolvable through the normal read path.
    matrices = routing_admin.load_all_matrices(routing_admin.discover_matrix_files(tmp_path))
    assert "mine" in matrices
    rows = {
        r.role: r.provider
        for r in routing_admin.resolve_matrix(matrices["mine"], {"anthropic", "openai"})
    }
    assert rows == {"general": "anthropic", "fast": "openai"}
