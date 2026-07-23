"""Configured-provider logic (``kernel/setup.py``): the merged view, the
first-run gate condition, and ``provider use/remove`` — the app-cli
``config.providers`` contract re-expressed over newtui scope files.

Pure dict/file work against ``tmp_path`` scopes; never the real ~/.amplifier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_newtui.kernel import bundle_admin, setup

_CRED_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_TOKEN",
)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite hermetic: the developer's real keys must never leak in."""
    for var in _CRED_VARS:
        monkeypatch.delenv(var, raising=False)


def _paths(tmp_path: Path):
    return bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")


def _seed(tmp_path: Path, *module_ids: str) -> None:
    paths = _paths(tmp_path)
    for module_id in module_ids:
        prefix = setup.provider_env_prefix(module_id)
        setup.write_provider_config(
            paths, "global", setup.provider_config_entry(module_id, key_var=f"{prefix}_API_KEY")
        )


def test_configured_providers_empty(tmp_path: Path) -> None:
    assert setup.configured_providers(tmp_path / "proj", tmp_path / "home") == ()


def test_configured_providers_marks_primary_by_priority(tmp_path: Path) -> None:
    # write_provider_config prepends+demotes: openai (newest) is primary.
    _seed(tmp_path, "provider-anthropic", "provider-openai")
    providers = setup.configured_providers(tmp_path / "proj", tmp_path / "home")
    assert [(p.name, p.priority, p.primary) for p in providers] == [
        ("openai", 1, True),
        ("anthropic", 10, False),
    ]


def test_configured_providers_local_scope_shadows_global(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    setup.write_provider_config(
        paths, "global", setup.provider_config_entry("provider-anthropic", key_var="ANTHROPIC_API_KEY")
    )
    setup.write_provider_config(
        paths, "local", setup.provider_config_entry("provider-anthropic", key_var="ANTHROPIC_API_KEY")
    )
    providers = setup.configured_providers(tmp_path / "proj", tmp_path / "home")
    assert len(providers) == 1  # merged by identity key
    assert providers[0].scope == "local"  # most specific scope wins


def test_has_configured_provider_true_when_configured(tmp_path: Path) -> None:
    _seed(tmp_path, "provider-anthropic")
    assert setup.has_configured_provider(tmp_path / "proj", tmp_path / "home")


def test_has_configured_provider_false_on_fresh_machine(tmp_path: Path) -> None:
    assert not setup.has_configured_provider(tmp_path / "proj", tmp_path / "home")


def test_has_configured_provider_env_credential_counts(tmp_path: Path, monkeypatch) -> None:
    # No config.providers, but the bundle's default provider can mount from env.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    assert setup.has_configured_provider(tmp_path / "proj", tmp_path / "home")


def test_has_configured_provider_keys_env_credential_counts(tmp_path: Path) -> None:
    # ``amplifier_home`` is the .amplifier dir itself (as keys_file resolves it).
    home = tmp_path / "home"
    home.mkdir()
    (home / "keys.env").write_text("OPENAI_API_KEY=sk-o\n", encoding="utf-8")
    assert setup.has_configured_provider(tmp_path / "proj", home)


def test_use_provider_switches_primary(tmp_path: Path) -> None:
    _seed(tmp_path, "provider-anthropic", "provider-openai")  # openai primary
    paths = _paths(tmp_path)
    target = setup.use_provider(
        paths, "anthropic", project_dir=tmp_path / "proj", amplifier_home=tmp_path / "home"
    )
    assert target is not None and target.name == "anthropic"
    providers = setup.configured_providers(tmp_path / "proj", tmp_path / "home")
    primary = next(p for p in providers if p.primary)
    assert primary.name == "anthropic" and primary.priority == 1
    demoted = next(p for p in providers if not p.primary)
    assert demoted.name == "openai" and demoted.priority == 10


def test_use_provider_unknown_returns_none(tmp_path: Path) -> None:
    _seed(tmp_path, "provider-anthropic")
    paths = _paths(tmp_path)
    assert (
        setup.use_provider(
            paths, "nope", project_dir=tmp_path / "proj", amplifier_home=tmp_path / "home"
        )
        is None
    )


def test_remove_provider_drops_entry(tmp_path: Path) -> None:
    _seed(tmp_path, "provider-anthropic", "provider-openai")
    paths = _paths(tmp_path)
    removed = setup.remove_provider(
        paths, "openai", project_dir=tmp_path / "proj", amplifier_home=tmp_path / "home"
    )
    assert removed is not None and removed.name == "openai"
    providers = setup.configured_providers(tmp_path / "proj", tmp_path / "home")
    assert [p.name for p in providers] == ["anthropic"]


def test_remove_last_provider_clears_section(tmp_path: Path) -> None:
    _seed(tmp_path, "provider-anthropic")
    paths = _paths(tmp_path)
    setup.remove_provider(
        paths, "anthropic", project_dir=tmp_path / "proj", amplifier_home=tmp_path / "home"
    )
    data = bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))
    assert "config" not in data  # the empty section is pruned, not left as {}


def test_remove_provider_unknown_returns_none(tmp_path: Path) -> None:
    _seed(tmp_path, "provider-anthropic")
    paths = _paths(tmp_path)
    assert (
        setup.remove_provider(
            paths, "nope", project_dir=tmp_path / "proj", amplifier_home=tmp_path / "home"
        )
        is None
    )


@pytest.mark.asyncio
async def test_onboarding_choices_falls_back_to_known_table(monkeypatch) -> None:
    # newtui mounts providers from the bundle, so discovery can be empty; the
    # setup flow must still offer the known providers with correct key vars.
    async def _empty(*a, **k):
        return ()

    monkeypatch.setattr(setup, "discover_providers", _empty)
    choices = await setup.onboarding_choices()
    by_id = {c.module_id: c for c in choices}
    assert "provider-anthropic" in by_id
    assert by_id["provider-anthropic"].key_var == "ANTHROPIC_API_KEY"
    # github-copilot's authoritative var is GITHUB_TOKEN, not the naive prefix.
    assert by_id["provider-github-copilot"].key_var == "GITHUB_TOKEN"
    # keyless providers (ollama) are omitted from the key-setup flow.
    assert "provider-ollama" not in by_id


@pytest.mark.asyncio
async def test_onboarding_choices_merges_partial_discovery(monkeypatch) -> None:
    # After any real session boot, foundation pip-installs the bundle's
    # provider module, so discovery returns a PARTIAL set (anthropic only).
    # The other known providers must stay offerable — this is the regression
    # that broke `provider add openai` on a machine that had booted once.
    discovered = setup.ProviderChoice(
        "provider-anthropic", "Anthropic (discovered)", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"
    )

    async def _partial(*a, **k):
        return (discovered,)

    monkeypatch.setattr(setup, "discover_providers", _partial)
    choices = await setup.onboarding_choices()
    by_id = {c.module_id: c for c in choices}
    assert by_id["provider-anthropic"] is discovered  # discovery wins for its module
    assert "provider-openai" in by_id  # known table fills the gaps
    assert "provider-gemini" in by_id
    assert "provider-ollama" not in by_id  # keyless still omitted


@pytest.mark.asyncio
async def test_onboarding_choices_keeps_unknown_discovered_modules(monkeypatch) -> None:
    # A discovered provider outside the known table (e.g. vllm) is offered too.
    extra = setup.ProviderChoice("provider-x", "X", "X_API_KEY", "X_BASE_URL")

    async def _found(*a, **k):
        return (extra,)

    monkeypatch.setattr(setup, "discover_providers", _found)
    choices = await setup.onboarding_choices()
    by_id = {c.module_id: c for c in choices}
    assert by_id["provider-x"] is extra
    assert "provider-anthropic" in by_id  # table entries still present
