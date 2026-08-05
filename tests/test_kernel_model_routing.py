"""Root-model to delegated-routing synchronization.

The exact provider ``default_model`` and the routing matrix are deliberately
separate controls.  These tests prove the shared matching/persistence seam
keeps them aligned without inventing matrices for unknown provider instances.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from amplifier_app_tui.kernel import bundle_admin
from amplifier_app_tui.kernel.model_routing import (
    apply_model_routing_hint,
    matching_matrix,
    persist_model_routing_hint,
)


def _seed_matrix(home: Path, name: str) -> None:
    routing = home / "routing"
    routing.mkdir(parents=True, exist_ok=True)
    (routing / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "roles": {
                    "general": {"candidates": [{"provider": name, "model": f"{name}-model"}]}
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_matching_matrix_prefers_instance_before_provider_family(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_matrix(home, "runpod")

    assert matching_matrix("runpod", "provider-vllm", home=home) == "runpod"
    assert matching_matrix("anthropic", "provider-anthropic", home=home) == "anthropic"
    assert matching_matrix("private", "provider-vllm", home=home) is None


def test_matching_matrix_prefers_known_family_over_colliding_instance_name(
    tmp_path: Path,
) -> None:
    """An instance nickname must not select an unrelated curated strategy."""
    assert matching_matrix("economy", "provider-anthropic", home=tmp_path) == "anthropic"


def test_in_memory_hint_preserves_user_matrix_unless_launch_is_explicit(tmp_path: Path) -> None:
    settings = {
        "config": {
            "providers": [
                {
                    "module": "provider-anthropic",
                    "config": {"priority": 1, "default_model": "claude-exact"},
                },
                {
                    "module": "provider-openai",
                    "config": {"priority": 2, "default_model": "gpt-exact"},
                },
            ]
        },
        "routing": {"matrix": "balanced"},
    }

    assert apply_model_routing_hint(settings, home=tmp_path) == "balanced"
    assert settings["routing"]["matrix"] == "balanced"

    assert (
        apply_model_routing_hint(
            settings,
            provider="openai",
            home=tmp_path,
            force=True,
        )
        == "openai"
    )
    assert settings["routing"]["matrix"] == "openai"
    # Matrix synchronization never rewrites either exact root model.
    providers = settings["config"]["providers"]
    assert providers[0]["config"]["default_model"] == "claude-exact"
    assert providers[1]["config"]["default_model"] == "gpt-exact"


def test_in_memory_explicit_provider_infers_family_without_settings(tmp_path: Path) -> None:
    settings: dict = {}

    selected = apply_model_routing_hint(
        settings,
        provider="anthropic-east",
        home=tmp_path,
        force=True,
    )

    assert selected == "anthropic"
    assert settings == {"routing": {"matrix": "anthropic", "enabled": True}}


def test_explicit_launch_reenables_companion_routing_overlay_in_memory(tmp_path: Path) -> None:
    settings = {
        "config": {
            "providers": [
                {
                    "module": "provider-anthropic",
                    "config": {"default_model": "claude-exact"},
                }
            ]
        },
        "routing": {"enabled": False, "matrix": "balanced"},
    }

    selected = apply_model_routing_hint(
        settings,
        provider="anthropic",
        home=tmp_path,
        force=True,
    )

    assert selected == "anthropic"
    assert settings["routing"] == {"enabled": True, "matrix": "anthropic"}


def test_persist_hint_writes_same_scope_and_preserves_siblings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    paths = bundle_admin.settings_paths(project, home)
    _seed_matrix(home, "runpod")
    bundle_admin.write_scope(
        paths.project_settings,
        {
            "config": {
                "providers": [
                    {
                        "module": "provider-vllm",
                        "id": "runpod",
                        "config": {"default_model": "exact-root-model"},
                    }
                ]
            },
            "routing": {"overrides": {"coding": {"candidates": []}}},
        },
    )

    selected = persist_model_routing_hint(
        paths,
        "project",
        provider_name="runpod",
        module_id="provider-vllm",
    )

    assert selected == ("runpod", paths.project_settings)
    stored = bundle_admin.read_scope(paths.project_settings)
    assert stored["routing"] == {
        "matrix": "runpod",
        "overrides": {"coding": {"candidates": []}},
    }
    assert stored["config"]["providers"][0]["config"]["default_model"] == "exact-root-model"


def test_persist_hint_does_not_write_unknown_matrix(tmp_path: Path) -> None:
    paths = bundle_admin.settings_paths(tmp_path / "project", tmp_path / "home")

    assert (
        persist_model_routing_hint(
            paths,
            "local",
            provider_name="private-endpoint",
            module_id="provider-vllm",
        )
        is None
    )
    assert not paths.local_settings.exists()
