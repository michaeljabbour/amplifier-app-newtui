"""``amplifier-tui init`` wiring (click CliRunner).

Provider discovery is stubbed so the test is offline and deterministic;
keys are written to a ``tmp_path`` keys file, never the real ~/.amplifier.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

import amplifier_app_tui.main as main_mod
from amplifier_app_tui.kernel import routing_admin, setup
from amplifier_app_tui.main import main

_CHOICES = (
    setup.ProviderChoice(
        "provider-anthropic", "Anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"
    ),
    setup.ProviderChoice("provider-openai", "OpenAI", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
)


def _stub(monkeypatch, tmp_path: Path):
    path = tmp_path / "keys.env"
    written: list = []

    async def _discover(*a, **k):
        return _CHOICES

    monkeypatch.setattr(setup, "discover_providers", _discover)
    monkeypatch.setattr(setup, "keys_file", lambda *a, **k: path)
    monkeypatch.setattr(
        setup,
        "setup_status",
        lambda *a, **k: setup.SetupStatus(keys_path=path, stored_keys=(), active_bundle=None),
    )
    # Never touch real settings — capture the provider-config write instead.
    monkeypatch.setattr(
        setup,
        "write_provider_config",
        lambda paths, scope, entry: written.append(entry) or (tmp_path / "settings.yaml"),
    )
    return path, written


def test_init_help_lists_options() -> None:
    result = CliRunner().invoke(main, ["init", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--api-key" in result.output


def test_init_writes_key_non_interactive(tmp_path: Path, monkeypatch) -> None:
    path, written = _stub(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["init", "-p", "anthropic", "--api-key", "sk-test", "-y"])
    assert result.exit_code == 0
    assert setup.read_keys(path) == {"ANTHROPIC_API_KEY": "sk-test"}
    assert "wrote ANTHROPIC_API_KEY" in result.output
    # It also persists a config.providers entry (not just the key).
    (entry,) = written
    assert entry["module"] == "provider-anthropic"
    assert entry["config"]["api_key"] == "${ANTHROPIC_API_KEY}"
    assert "configured provider provider-anthropic" in result.output


def test_init_writes_model_into_config(tmp_path: Path, monkeypatch) -> None:
    _path, written = _stub(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        main, ["init", "-p", "anthropic", "--api-key", "k", "--model", "claude-x", "-y"]
    )
    assert result.exit_code == 0
    (entry,) = written
    assert entry["config"]["default_model"] == "claude-x"


def test_init_writes_base_url_too(tmp_path: Path, monkeypatch) -> None:
    path, _written = _stub(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        main,
        ["init", "-p", "openai", "--api-key", "k", "--base-url", "https://x/v1", "-y"],
    )
    assert result.exit_code == 0
    keys = setup.read_keys(path)
    assert keys["OPENAI_API_KEY"] == "k"
    assert keys["OPENAI_BASE_URL"] == "https://x/v1"


def test_init_unknown_provider_errors(tmp_path: Path, monkeypatch) -> None:
    _stub(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["init", "-p", "nope", "--api-key", "k", "-y"])
    assert result.exit_code == 1
    assert "unknown provider" in result.output


def test_init_yes_without_provider_is_status_only(tmp_path: Path, monkeypatch) -> None:
    path, _written = _stub(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["init", "-y"])
    assert result.exit_code == 0
    assert "providers:" in result.output
    assert not path.exists()  # nothing written


def test_init_requires_key_with_yes(tmp_path: Path, monkeypatch) -> None:
    _stub(monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["init", "-p", "anthropic", "-y"])
    assert result.exit_code == 1
    assert "--api-key required" in result.output


def _no_matrices(monkeypatch) -> None:
    """Neutralize the wizard's routing step so provider-only paths stay offline."""
    monkeypatch.setattr(routing_admin, "list_matrices", lambda *a, **k: ())


def test_init_interactive_selection_and_key(tmp_path: Path, monkeypatch) -> None:
    path, _written = _stub(monkeypatch, tmp_path)
    _no_matrices(monkeypatch)
    # stdin: choose provider #1, then type the key at the hidden prompt.
    result = CliRunner().invoke(main, ["init"], input="1\nsk-interactive\n")
    assert result.exit_code == 0
    assert setup.read_keys(path)["ANTHROPIC_API_KEY"] == "sk-interactive"
    # No matrices discovered → wizard prints the fetch hint and finishes clean.
    assert "no routing matrices found" in result.output


def _matrix(name: str, *, active: bool = False) -> routing_admin.MatrixEntry:
    return routing_admin.MatrixEntry(
        name=name,
        active=active,
        description=f"{name} matrix",
        updated="2026-05-12",
        covered=2,
        total=2,
        has_providers=True,
    )


def test_init_wizard_selects_provider_then_routing(tmp_path: Path, monkeypatch) -> None:
    """No-flag init runs the full wizard: provider + key, then routing."""
    path, written = _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(
        routing_admin,
        "list_matrices",
        lambda *a, **k: (_matrix("balanced", active=True), _matrix("quality")),
    )
    selected: dict = {}
    monkeypatch.setattr(
        routing_admin,
        "set_active_matrix",
        lambda paths, name, scope: selected.update(name=name, scope=scope) or (tmp_path / "s.yaml"),
    )
    # stdin: provider #1, key, then routing matrix #2 (quality).
    result = CliRunner().invoke(main, ["init"], input="1\nsk-wizard\n2\n")
    assert result.exit_code == 0
    assert setup.read_keys(path)["ANTHROPIC_API_KEY"] == "sk-wizard"
    assert written  # provider persisted
    assert selected == {"name": "quality", "scope": "global"}
    assert "active routing matrix → quality" in result.output


def test_init_wizard_blank_routing_keeps_current(tmp_path: Path, monkeypatch) -> None:
    """A blank routing answer leaves the matrix untouched."""
    _path, _written = _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(
        routing_admin, "list_matrices", lambda *a, **k: (_matrix("balanced", active=True),)
    )
    touched: list = []
    monkeypatch.setattr(
        routing_admin,
        "set_active_matrix",
        lambda *a, **k: touched.append(a) or (tmp_path / "s.yaml"),
    )
    # provider #1, key, then blank (keep current routing).
    result = CliRunner().invoke(main, ["init"], input="1\nsk-x\n\n")
    assert result.exit_code == 0
    assert touched == []  # routing left as-is


def test_init_wizard_invalid_routing_selection(tmp_path: Path, monkeypatch) -> None:
    _path, _written = _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(routing_admin, "list_matrices", lambda *a, **k: (_matrix("balanced"),))
    touched: list = []
    monkeypatch.setattr(
        routing_admin,
        "set_active_matrix",
        lambda *a, **k: touched.append(a) or (tmp_path / "s.yaml"),
    )
    result = CliRunner().invoke(main, ["init"], input="1\nsk-x\n9\n")
    assert result.exit_code == 0
    assert "invalid selection: 9" in result.output
    assert touched == []


def test_init_any_flag_bypasses_wizard(tmp_path: Path, monkeypatch) -> None:
    """Passing a flag must never reach the routing wizard step."""
    path, _written = _stub(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("wizard routing step must not run on the flag path")

    monkeypatch.setattr(main_mod, "_select_routing_interactive", _boom)
    result = CliRunner().invoke(main, ["init", "-p", "anthropic", "--api-key", "sk-flag", "-y"])
    assert result.exit_code == 0
    assert setup.read_keys(path)["ANTHROPIC_API_KEY"] == "sk-flag"
