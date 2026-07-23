"""``amplifier-newtui provider …`` + the first-run launch gate (click).

Provider discovery falls back to the known-credential table offline, so these
run without network. Settings/keys go to an isolated ``$HOME`` under
``tmp_path`` — never the real ~/.amplifier.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner
import pytest

from amplifier_app_newtui import main as main_mod
from amplifier_app_newtui.kernel import setup
from amplifier_app_newtui.main import main

_CRED_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_TOKEN",
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config at an isolated HOME and a clean project cwd."""
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in _CRED_VARS:
        monkeypatch.delenv(var, raising=False)

    # The live ModuleLoader is process-global and non-hermetic across the
    # in-process CliRunner invocations these tests chain; force the offline
    # fallback provider table (same hygiene as test_init_cli.py) so `add`
    # resolves anthropic/openai deterministically regardless of test order.
    async def _no_discovery(*a: object, **k: object) -> tuple[setup.ProviderChoice, ...]:
        return ()

    monkeypatch.setattr(setup, "discover_providers", _no_discovery)
    return home


def _add(provider_type: str, key: str) -> None:
    result = CliRunner().invoke(main, ["provider", "add", provider_type, "--api-key", key, "-y"])
    assert result.exit_code == 0, result.output


def test_provider_list_empty(isolated_home: Path) -> None:
    result = CliRunner().invoke(main, ["provider", "list"])
    assert result.exit_code == 0
    assert "no providers configured" in result.output


def test_provider_add_then_list_marks_primary(isolated_home: Path) -> None:
    _add("anthropic", "sk-a")
    _add("openai", "sk-o")  # newest becomes primary
    result = CliRunner().invoke(main, ["provider", "list"])
    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines[0].startswith("★") and "openai" in lines[0]
    assert any("anthropic" in ln and not ln.startswith("★") for ln in lines)
    # The key landed in the isolated keys.env, not the real one.
    assert setup.read_keys(isolated_home / ".amplifier" / "keys.env")["OPENAI_API_KEY"] == "sk-o"


def test_provider_use_switches_primary(isolated_home: Path) -> None:
    _add("anthropic", "sk-a")
    _add("openai", "sk-o")
    result = CliRunner().invoke(main, ["provider", "use", "anthropic"])
    assert result.exit_code == 0
    assert "primary provider" in result.output and "anthropic" in result.output
    listing = CliRunner().invoke(main, ["provider", "list"]).output.splitlines()
    primary = next(ln for ln in listing if ln.startswith("★"))
    assert "anthropic" in primary


def test_provider_use_unknown_errors(isolated_home: Path) -> None:
    _add("anthropic", "sk-a")
    result = CliRunner().invoke(main, ["provider", "use", "nope"])
    assert result.exit_code == 1
    assert "unknown provider" in result.output


def test_provider_remove(isolated_home: Path) -> None:
    _add("anthropic", "sk-a")
    _add("openai", "sk-o")
    result = CliRunner().invoke(main, ["provider", "remove", "openai"])
    assert result.exit_code == 0
    assert "removed provider: openai" in result.output
    listing = CliRunner().invoke(main, ["provider", "list"]).output
    assert "openai" not in listing and "anthropic" in listing


def test_provider_dashboard(isolated_home: Path) -> None:
    _add("anthropic", "sk-a")
    result = CliRunner().invoke(main, ["provider", "dashboard"])
    assert result.exit_code == 0
    assert "providers (★ = primary)" in result.output
    assert "anthropic" in result.output
    assert "provider use" in result.output


def test_provider_list_help() -> None:
    result = CliRunner().invoke(main, ["provider", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "add", "use", "remove", "dashboard"):
        assert sub in result.output


# -- first-run launch gate --------------------------------------------------


def test_gate_proceeds_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(setup, "has_configured_provider", lambda *a, **k: True)
    assert asyncio.run(main_mod._first_run_gate()) is None


def test_gate_noninteractive_no_creds_stops(monkeypatch) -> None:
    monkeypatch.setattr(setup, "has_configured_provider", lambda *a, **k: False)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)

    async def _no_env(*a, **k):
        return None

    monkeypatch.setattr(setup, "auto_init_from_env", _no_env)
    assert asyncio.run(main_mod._first_run_gate()) == 1


def test_gate_noninteractive_env_autoinits(monkeypatch) -> None:
    monkeypatch.setattr(setup, "has_configured_provider", lambda *a, **k: False)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)

    async def _from_env(*a, **k):
        return "provider-anthropic"

    monkeypatch.setattr(setup, "auto_init_from_env", _from_env)
    assert asyncio.run(main_mod._first_run_gate()) is None


def test_gate_interactive_onboards_then_proceeds(monkeypatch) -> None:
    calls = {"n": 0}

    def _has(*a, **k):
        # first check (gate entry) False; after onboarding True
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(setup, "has_configured_provider", _has)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main_mod.sys.stdout, "isatty", lambda: True)

    async def _fake_init(**kwargs):
        return 0

    monkeypatch.setattr(main_mod, "_init", _fake_init)
    assert asyncio.run(main_mod._first_run_gate()) is None


def test_gate_interactive_skip_does_not_launch(monkeypatch) -> None:
    monkeypatch.setattr(setup, "has_configured_provider", lambda *a, **k: False)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main_mod.sys.stdout, "isatty", lambda: True)

    async def _fake_init(**kwargs):
        return 0  # user entered nothing; still unconfigured

    monkeypatch.setattr(main_mod, "_init", _fake_init)
    # Skipped onboarding returns 0 (clean, no launch) rather than crashing later.
    assert asyncio.run(main_mod._first_run_gate()) == 0
