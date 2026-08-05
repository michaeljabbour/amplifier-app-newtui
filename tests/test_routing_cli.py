"""``amplifier-tui routing`` group wiring (click CliRunner).

Admin logic is unit-tested in ``test_kernel_routing_admin``; this covers the
CLI plumbing (help/subcommands, list table, use roundtrip + unknown reject)
with settings + matrix cache redirected to ``tmp_path``. A bundle-cache
matrix is seeded so discovery never attempts a network fetch.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner
from rich.console import Console

from amplifier_app_tui.kernel import bundle_admin
from amplifier_app_tui.main import _manage_matrix_target, main


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


def _seed_providers(paths, providers: list[dict]) -> None:
    bundle_admin.write_scope(paths.global_settings, {"config": {"providers": providers}})


def test_routing_group_lists_subcommands() -> None:
    result = CliRunner().invoke(main, ["routing", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "use", "show", "create", "manage"):
        assert sub in result.output


def _offline_fetch(monkeypatch) -> None:
    """Simulate offline: the lazy routing-bundle fetch does nothing.

    With the fresh-install fix, ``fetch=True`` genuinely downloads the
    routing-matrix bundle, so the "no matrices" branches only occur offline —
    these tests pin that offline rendering without touching the network."""
    from amplifier_app_tui.kernel import routing_admin

    monkeypatch.setattr(routing_admin, "_ensure_routing_bundle_cached", lambda home: None)


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
    _offline_fetch(monkeypatch)
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


# -- show -------------------------------------------------------------------


def test_routing_show_active_resolution(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    bundle_admin.write_scope(
        paths.global_settings,
        {
            "routing": {"matrix": "balanced"},
            "config": {
                "providers": [
                    {"module": "provider-anthropic", "config": {"default_model": "claude-opus"}},
                ]
            },
        },
    )
    result = CliRunner().invoke(main, ["routing", "show"])
    assert result.exit_code == 0
    assert "Routing: balanced" in result.output
    # The CANDIDATE's model is shown, not the provider's default_model pin:
    # hooks-routing writes the candidate into the agent's provider_preferences
    # and never reads default_model. Substituting the pin made every role show
    # the root session's model, hiding the matrix entirely.
    assert "claude-sonnet-*" in result.output
    assert "claude-opus" not in result.output
    # fast has no configured provider -> flagged.
    assert "no provider" in result.output
    assert "anthropic" in result.output


def test_routing_show_named_matrix_detailed(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    _seed_providers(paths, [{"module": "provider-openai"}])
    result = CliRunner().invoke(main, ["routing", "show", "balanced", "--detailed"])
    assert result.exit_code == 0
    assert "Matrix: balanced" in result.output
    assert "general" in result.output and "fast" in result.output
    assert "active" in result.output  # openai wins the fast role


def test_routing_show_unknown(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    result = CliRunner().invoke(main, ["routing", "show", "ghost"])
    assert result.exit_code == 1
    assert "unknown matrix: ghost" in result.output


def test_routing_show_empty(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    _offline_fetch(monkeypatch)
    result = CliRunner().invoke(main, ["routing", "show"])
    assert result.exit_code == 0
    assert "no routing matrices found" in result.output


# -- create -----------------------------------------------------------------


def test_routing_create_persists_matrix(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}, {"module": "provider-openai"}])
    # roles walked in order: general, fast -> provider #, model; then save; name.
    stdin = "1\nclaude-x\n2\ngpt-mini\ns\nmine\n"
    result = CliRunner().invoke(main, ["routing", "create"], input=stdin)
    assert result.exit_code == 0, result.output
    assert "saved custom matrix 'mine'" in result.output
    matrices = routing_admin_load(tmp_path)
    assert "mine" in matrices
    roles = matrices["mine"]["roles"]
    assert roles["general"]["candidates"] == [{"provider": "anthropic", "model": "claude-x"}]
    assert roles["fast"]["candidates"] == [{"provider": "openai", "model": "gpt-mini"}]


def test_routing_create_requires_general_and_fast(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])
    # skip general, skip fast during walk, then skip the required re-prompt.
    result = CliRunner().invoke(main, ["routing", "create"], input="s\ns\ns\n")
    assert result.exit_code == 1
    assert "cannot create matrix without required roles" in result.output


def test_routing_create_no_providers(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    result = CliRunner().invoke(main, ["routing", "create"])
    assert result.exit_code == 1
    assert "no providers configured" in result.output


def test_routing_create_add_role_then_save(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])
    # general -> anthropic/m1, fast -> anthropic/m2, then [a]dd 'coding', then save.
    stdin = "1\nm1\n1\nm2\na\ncoding\ncode work\n1\nm3\ns\nmine\n"
    result = CliRunner().invoke(main, ["routing", "create"], input=stdin)
    assert result.exit_code == 0, result.output
    matrices = routing_admin_load(tmp_path)
    assert set(matrices["mine"]["roles"]) == {"general", "fast", "coding"}
    assert matrices["mine"]["roles"]["coding"]["candidates"] == [
        {"provider": "anthropic", "model": "m3"}
    ]


# -- manage -----------------------------------------------------------------


def test_routing_manage_select(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    _seed_matrix(tmp_path / "home", "economy", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])
    # sorted names: [balanced, economy] -> select #2, then done.
    result = CliRunner().invoke(main, ["routing", "manage"], input="s2\nd\n")
    assert result.exit_code == 0, result.output
    assert "Available Matrices" in result.output
    assert "active routing matrix → economy" in result.output
    assert bundle_admin.read_scope(paths.global_settings)["routing"]["matrix"] == "economy"


def test_routing_manage_select_accepts_displayed_number(tmp_path: Path, monkeypatch) -> None:
    """A bare row number is a complete selection, matching the visible table."""
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "anthropic", _roles())
    _seed_matrix(tmp_path / "home", "runpod", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])

    result = CliRunner().invoke(main, ["routing", "manage"], input="1\nd\n")

    assert result.exit_code == 0, result.output
    assert "[1/name] select matrix" in result.output
    assert "active routing matrix → anthropic" in result.output
    assert bundle_admin.read_scope(paths.global_settings)["routing"]["matrix"] == "anthropic"


def test_routing_manage_select_accepts_exact_name(tmp_path: Path, monkeypatch) -> None:
    """A matrix name such as the bug report's ``runpod`` selects directly."""
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "anthropic", _roles())
    _seed_matrix(tmp_path / "home", "runpod", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])

    result = CliRunner().invoke(main, ["routing", "manage"], input="runpod\nd\n")

    assert result.exit_code == 0, result.output
    assert "active routing matrix → runpod" in result.output
    assert bundle_admin.read_scope(paths.global_settings)["routing"]["matrix"] == "runpod"


def test_routing_manage_view_accepts_name_after_shortcut(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "anthropic", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])

    result = CliRunner().invoke(main, ["routing", "manage"], input="v anthropic\nd\n")

    assert result.exit_code == 0, result.output
    assert "Matrix: anthropic" in result.output


def test_routing_manage_control_letter_name_remains_selectable(tmp_path: Path, monkeypatch) -> None:
    """A legal matrix named like a control uses ``:done`` as the escape hatch."""
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "d", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])

    result = CliRunner().invoke(main, ["routing", "manage"], input="d\n:done\n")

    assert result.exit_code == 0, result.output
    assert "active routing matrix → d" in result.output
    assert bundle_admin.read_scope(paths.global_settings)["routing"]["matrix"] == "d"


def test_routing_manage_numeric_name_has_unambiguous_explicit_form(
    tmp_path: Path, monkeypatch
) -> None:
    """Bare digits pick a displayed row; ``select`` can force a numeric name."""
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "2", _roles())
    _seed_matrix(tmp_path / "home", "anthropic", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])

    result = CliRunner().invoke(
        main,
        ["routing", "manage"],
        input="2\nselect 2\nd\n",
    )

    assert result.exit_code == 0, result.output
    assert "active routing matrix → anthropic" in result.output
    assert "active routing matrix → 2" in result.output
    assert bundle_admin.read_scope(paths.global_settings)["routing"]["matrix"] == "2"


def test_routing_manage_rejects_ambiguous_casefold_name() -> None:
    console = Console(record=True, width=120)

    selected = _manage_matrix_target(
        console,
        "FoO",
        ["FOO", "foo"],
        prompt="matrix number or name",
    )

    assert selected is None
    assert "ambiguous matrix name: FoO" in console.export_text()
    assert (
        _manage_matrix_target(
            console,
            "FOO",
            ["FOO", "foo"],
            prompt="matrix number or name",
        )
        == "FOO"
    )


def test_routing_manage_invalid_number_does_not_write(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "anthropic", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])

    result = CliRunner().invoke(main, ["routing", "manage"], input="99\nd\n")

    assert result.exit_code == 0, result.output
    assert "out of range: 1-1" in result.output
    assert "routing" not in bundle_admin.read_scope(paths.global_settings)


def test_routing_manage_view_details(tmp_path: Path, monkeypatch) -> None:
    paths = _redirect(monkeypatch, tmp_path)
    _seed_matrix(tmp_path / "home", "balanced", _roles())
    _seed_providers(paths, [{"module": "provider-anthropic"}])
    result = CliRunner().invoke(main, ["routing", "manage"], input="v1\nd\n")
    assert result.exit_code == 0, result.output
    assert "Matrix: balanced" in result.output


def test_routing_manage_empty(tmp_path: Path, monkeypatch) -> None:
    _redirect(monkeypatch, tmp_path)
    _offline_fetch(monkeypatch)
    result = CliRunner().invoke(main, ["routing", "manage"])
    assert result.exit_code == 0
    assert "no routing matrices found" in result.output


def routing_admin_load(tmp_path: Path) -> dict:
    from amplifier_app_tui.kernel import routing_admin

    return routing_admin.load_all_matrices(routing_admin.discover_matrix_files(tmp_path / "home"))
