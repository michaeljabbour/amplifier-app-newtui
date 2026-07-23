"""Routing-matrix discovery + selection logic (``kernel/routing_admin.py``).

Filesystem/settings work over ``tmp_path`` (a scoped ``amplifier_home``) — no
network, no session. Seeds both matrix sources: the composed-bundle cache and
the user routing dir. Mirrors the app-cli ``routing list/use`` contract.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from amplifier_app_newtui.kernel import bundle_admin, routing_admin


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
