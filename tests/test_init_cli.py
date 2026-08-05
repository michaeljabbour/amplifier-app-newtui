"""``amplifier-tui init`` wiring (click CliRunner).

Provider discovery is stubbed so the test is offline and deterministic;
keys are written to a ``tmp_path`` keys file, never the real ~/.amplifier.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

import amplifier_app_tui.main as main_mod
from amplifier_app_tui.kernel import setup
from amplifier_app_tui.main import main

_CHOICES = (
    setup.ProviderChoice(
        "provider-anthropic", "Anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"
    ),
    setup.ProviderChoice("provider-openai", "OpenAI", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
)


def _stub(monkeypatch, tmp_path: Path, *, schema=None, choices=None, stub_schema=True):
    """Offline init wiring: a fixed provider list, a tmp keys file, no settings write.

    ``onboarding_choices`` is stubbed whole (not just ``discover_providers``)
    so the numbered menu stays exactly ``_CHOICES`` — the real one now also
    unions the module catalog, which is covered in test_kernel_providers.

    ``_resolve_provider_schema`` returns *schema*; ``None`` (the default)
    selects the degraded basic flow, which is what the pre-existing
    key-prompt expectations describe. ``stub_schema=False`` keeps the real
    resolver (the issue-#182 install-path tests exercise it directly).

    The console reads settings through ``bundle_admin.settings_paths``, so
    the whole run is pinned to a scratch ``AMPLIFIER_HOME`` + project cwd —
    never the real ~/.amplifier.
    """
    home = tmp_path / "amp-home"
    proj = tmp_path / "proj"
    home.mkdir(exist_ok=True)
    proj.mkdir(exist_ok=True)
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))
    monkeypatch.chdir(proj)

    path = tmp_path / "keys.env"
    written: list = []

    offered = _CHOICES if choices is None else tuple(choices)

    async def _choices(*a, **k):
        return offered

    async def _schema(choice):
        return schema

    monkeypatch.setattr(setup, "onboarding_choices", _choices)
    if stub_schema:
        monkeypatch.setattr(main_mod, "_resolve_provider_schema", _schema)
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
    assert _global_settings(tmp_path)["routing"]["matrix"] == "anthropic"
    assert "routing matrix → anthropic" in result.output


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


# ---------------------------------------------------------------------------
# The init console — providers/routing tables + [p]/[r]/[w]/[d] actions
# (app-cli `amplifier init` parity). Menus are driven by scripted stdin.
# ---------------------------------------------------------------------------


def _amp_home(tmp_path: Path) -> Path:
    return tmp_path / "amp-home"


def _seed_matrix(tmp_path: Path, name: str, roles: dict | None = None) -> None:
    """A bundle-cache matrix so routing discovery never attempts a fetch."""
    routing_dir = _amp_home(tmp_path) / "cache" / "amplifier-bundle-routing-matrix-t" / "routing"
    routing_dir.mkdir(parents=True, exist_ok=True)
    (routing_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "description": f"{name} matrix",
                "updated": "2026-05-12",
                "roles": roles
                or {"general": {"candidates": [{"provider": "anthropic", "model": "claude-*"}]}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _seed_providers(tmp_path: Path, providers: list[dict]) -> Path:
    """Real config.providers entries in the scratch global scope."""
    path = _amp_home(tmp_path) / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"config": {"providers": providers}}), encoding="utf-8")
    return path


def _global_settings(tmp_path: Path) -> dict:
    return yaml.safe_load((_amp_home(tmp_path) / "settings.yaml").read_text()) or {}


def test_provider_use_syncs_the_selected_provider_matrix(tmp_path: Path, monkeypatch) -> None:
    _stub(monkeypatch, tmp_path)
    _seed_providers(
        tmp_path,
        [
            {
                "module": "provider-openai",
                "config": {"priority": 1, "default_model": "gpt-exact"},
            },
            {
                "module": "provider-anthropic",
                "config": {"priority": 2, "default_model": "claude-exact"},
            },
        ],
    )

    result = CliRunner().invoke(main, ["provider", "use", "anthropic"])

    assert result.exit_code == 0, result.output
    assert "primary provider → anthropic" in result.output
    assert "routing matrix → anthropic" in result.output
    stored = _global_settings(tmp_path)
    assert stored["routing"]["matrix"] == "anthropic"
    priorities = {
        entry["module"]: entry["config"]["priority"] for entry in stored["config"]["providers"]
    }
    assert priorities == {"provider-openai": 10, "provider-anthropic": 1}


def test_init_console_first_run_adds_provider(tmp_path: Path, monkeypatch) -> None:
    """No providers → straight into the provider console; [a] adds one."""
    path, written = _stub(monkeypatch, tmp_path)
    # [a] add · pick #1 (Anthropic) · key at the hidden prompt · done · done.
    result = CliRunner().invoke(main, ["init"], input="a\n1\nsk-interactive\nd\nd\n")
    assert result.exit_code == 0, result.output
    assert "No providers configured" in result.output
    assert "Available providers:" in result.output
    assert setup.read_keys(path)["ANTHROPIC_API_KEY"] == "sk-interactive"
    (entry,) = written
    assert entry["module"] == "provider-anthropic"
    assert "Amplifier Setup" in result.output  # lands on the dashboard after


def test_init_console_dashboard_renders_tables(tmp_path: Path, monkeypatch) -> None:
    """With a provider configured, init renders both tables then exits on [d]."""
    _stub(monkeypatch, tmp_path)
    _seed_providers(
        tmp_path,
        [{"module": "provider-anthropic", "config": {"default_model": "claude-x", "priority": 1}}],
    )
    _seed_matrix(tmp_path, "balanced")
    result = CliRunner().invoke(main, ["init"], input="\n")  # blank → default [d]
    assert result.exit_code == 0, result.output
    assert "Amplifier Setup" in result.output
    assert "Providers" in result.output
    assert "★ anthropic" in result.output
    assert "claude-x" in result.output
    assert "Routing: balanced" in result.output
    assert "Manage providers" in result.output


def test_init_console_routing_select(tmp_path: Path, monkeypatch) -> None:
    """[r] opens the routing console; [s2] activates the second matrix."""
    _stub(monkeypatch, tmp_path)
    _seed_providers(tmp_path, [{"module": "provider-anthropic", "config": {"priority": 1}}])
    _seed_matrix(tmp_path, "balanced")
    _seed_matrix(tmp_path, "quality")
    result = CliRunner().invoke(main, ["init"], input="r\ns2\nd\nd\n")
    assert result.exit_code == 0, result.output
    assert "Available Matrices" in result.output
    assert "active routing matrix → quality" in result.output
    assert _global_settings(tmp_path)["routing"]["matrix"] == "quality"


def test_init_console_routing_selects_bare_matrix_name(tmp_path: Path, monkeypatch) -> None:
    """The setup dashboard shares the intuitive number/name routing picker."""
    _stub(monkeypatch, tmp_path)
    _seed_providers(tmp_path, [{"module": "provider-anthropic", "config": {"priority": 1}}])
    _seed_matrix(tmp_path, "anthropic")
    _seed_matrix(tmp_path, "runpod")

    result = CliRunner().invoke(main, ["init"], input="r\nanthropic\nd\nd\n")

    assert result.exit_code == 0, result.output
    assert "active routing matrix → anthropic" in result.output
    assert _global_settings(tmp_path)["routing"]["matrix"] == "anthropic"


def test_init_console_routing_invalid_selection(tmp_path: Path, monkeypatch) -> None:
    _stub(monkeypatch, tmp_path)
    _seed_providers(tmp_path, [{"module": "provider-anthropic", "config": {"priority": 1}}])
    _seed_matrix(tmp_path, "balanced")
    result = CliRunner().invoke(main, ["init"], input="r\ns9\nd\nd\n")
    assert result.exit_code == 0, result.output
    assert "out of range: 1-1" in result.output
    assert "routing" not in _global_settings(tmp_path)


def test_init_any_flag_bypasses_console(tmp_path: Path, monkeypatch) -> None:
    """Passing a flag must never open the interactive console."""
    path, _written = _stub(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("the console must not open on the flag path")

    monkeypatch.setattr(main_mod, "_init_console", _boom)
    result = CliRunner().invoke(main, ["init", "-p", "anthropic", "--api-key", "sk-flag", "-y"])
    assert result.exit_code == 0
    assert setup.read_keys(path)["ANTHROPIC_API_KEY"] == "sk-flag"


def test_provider_console_remove(tmp_path: Path, monkeypatch) -> None:
    """[p] → [r2] + confirm removes the numbered entry from settings."""
    _stub(monkeypatch, tmp_path)
    _seed_providers(
        tmp_path,
        [
            {"module": "provider-anthropic", "config": {"priority": 1}},
            {"module": "provider-openai", "config": {"priority": 2}},
        ],
    )
    result = CliRunner().invoke(main, ["init"], input="p\nr2\ny\nd\nd\n")
    assert result.exit_code == 0, result.output
    assert "✓ Removed provider: openai" in result.output
    remaining = _global_settings(tmp_path)["config"]["providers"]
    assert [p["module"] for p in remaining] == ["provider-anthropic"]


def test_provider_console_reorder(tmp_path: Path, monkeypatch) -> None:
    """[p] → [p] `2 1` swaps the two providers' priorities."""
    _stub(monkeypatch, tmp_path)
    _seed_providers(
        tmp_path,
        [
            {"module": "provider-anthropic", "config": {"priority": 1}},
            {"module": "provider-openai", "config": {"priority": 2}},
        ],
    )
    result = CliRunner().invoke(main, ["init"], input="p\np\n2 1\nd\nd\n")
    assert result.exit_code == 0, result.output
    assert "✓ Priorities updated." in result.output
    by_module = {
        p["module"]: p["config"]["priority"]
        for p in _global_settings(tmp_path)["config"]["providers"]
    }
    assert by_module == {"provider-anthropic": 2, "provider-openai": 1}


def test_provider_console_test_connections(tmp_path: Path, monkeypatch) -> None:
    """[p] → [t] pings each provider via list_models and tabulates ✓/✗."""
    _stub(monkeypatch, tmp_path)
    _seed_providers(
        tmp_path,
        [
            {"module": "provider-anthropic", "config": {"priority": 1}},
            {"module": "provider-openai", "config": {"priority": 2}},
        ],
    )

    async def _listing(module_id, collected=None, **k):
        if module_id == "provider-anthropic":
            return setup.ModelCatalog(models=(setup.ProviderModel(id="claude-x"),))
        return setup.ModelCatalog(error="ConnectionError: unreachable")

    monkeypatch.setattr(setup, "list_provider_models", _listing)
    result = CliRunner().invoke(main, ["init"], input="p\nt\nd\nd\n")
    assert result.exit_code == 0, result.output
    assert "Provider Test Results" in result.output
    assert "1 model(s) available" in result.output
    assert "ConnectionError: unreachable" in result.output


def test_console_scope_toggle(tmp_path: Path, monkeypatch) -> None:
    """[w] switches the write scope; a later add persists to that scope."""
    _stub(monkeypatch, tmp_path)
    _seed_providers(tmp_path, [{"module": "provider-anthropic", "config": {"priority": 1}}])
    result = CliRunner().invoke(main, ["init"], input="w\n2\nd\n")
    assert result.exit_code == 0, result.output
    assert "✓ Switched to project scope." in result.output


def test_provider_console_edit_preserves_unspecified_values(tmp_path: Path, monkeypatch) -> None:
    """[e1] re-runs the field wizard with stored values as Enter-to-keep defaults.

    Ambient VLLM_BASE_URL / VLLM_API_KEY / VLLM_CONTEXT_WINDOW are already
    cleared by the autouse ``_offline_provider_setup`` fixture (conftest.py)
    so this test's Enter-to-keep assertions depend only on the stored
    values seeded below, never on the developer's shell.
    """
    path, _written = _stub(monkeypatch, tmp_path, schema=_VLLM_SCHEMA, choices=(_VLLM_CHOICE,))
    _models(monkeypatch, "m1", "m2")
    setup.write_key(path, "VLLM_BASE_URL", "http://old:8000/v1", update_environ=False)
    setup.write_key(path, "VLLM_API_KEY", "sk-old", update_environ=False)
    _seed_providers(
        tmp_path,
        [
            {
                "module": "provider-vllm",
                "config": {
                    "base_url": "${VLLM_BASE_URL}",
                    "api_key": "${VLLM_API_KEY}",
                    "default_model": "old-model",
                    "priority": 3,
                },
            }
        ],
    )
    # [p] · [e1] · Enter×3 (keep base_url/api_key, skip context_window) ·
    # Enter (keep current model) · done · done.
    result = CliRunner().invoke(main, ["init"], input="p\ne1\n\n\n\n\nd\nd\n")
    assert result.exit_code == 0, result.output
    assert "✓ Provider updated: vllm (old-model)" in result.output
    (entry,) = _global_settings(tmp_path)["config"]["providers"]
    assert entry["config"] == {
        "base_url": "${VLLM_BASE_URL}",
        "api_key": "${VLLM_API_KEY}",
        "default_model": "old-model",
        "priority": 3,  # editing must not reshuffle priorities
    }
    # The stored values survive untouched in keys.env.
    keys = setup.read_keys(path)
    assert keys["VLLM_BASE_URL"] == "http://old:8000/v1"
    assert keys["VLLM_API_KEY"] == "sk-old"


# ---------------------------------------------------------------------------
# Issue #182 — schema unreadable must never degrade to key-only setup for
# providers whose catalog declares required fields; the console offers a real
# install (uv pip) before falling back to those fields.
# ---------------------------------------------------------------------------


def test_add_schema_unavailable_prompts_catalog_required_fields(
    tmp_path: Path, monkeypatch
) -> None:
    path, written = _stub(monkeypatch, tmp_path, schema=None, choices=(_VLLM_CHOICE,))
    _models(monkeypatch, "glm")
    monkeypatch.setattr(setup, "instance_id_in_use", lambda *a, **k: False)
    result = CliRunner().invoke(
        main,
        ["provider", "add", "vllm"],
        input="https://pod.example/v1\nsk-abc\n1\n",  # Server URL · key · model #1
    )
    assert result.exit_code == 0, result.output
    assert "prompting for the catalog's required fields" in result.output
    assert setup.read_keys(path)["VLLM_BASE_URL"] == "https://pod.example/v1"
    (entry,) = written
    assert entry["config"]["base_url"] == "${VLLM_BASE_URL}"
    assert entry["config"]["default_model"] == "glm"


def _install_stubs(monkeypatch, *, schema_after_install):
    """Drive the real _resolve_provider_schema: graft fails, install is offered."""

    async def _avail(module_id, source_uri, **k):
        return setup.ProviderAvailability(
            module_id, False, reason="fetched, but its dependencies are not installed"
        )

    monkeypatch.setattr(setup, "ensure_provider_available", _avail)

    schemas: list = [None]

    def _load(module_id):
        return schemas.pop(0) if schemas else schema_after_install

    monkeypatch.setattr(setup, "load_provider_info", _load)

    installs: list = []

    async def _install(module_id, source_uri, **k):
        installs.append((module_id, source_uri))
        return True, "installed"

    monkeypatch.setattr(setup, "install_provider_module", _install)
    monkeypatch.setattr(setup, "instance_id_in_use", lambda *a, **k: False)
    return installs


def test_add_installs_module_on_confirm(tmp_path: Path, monkeypatch) -> None:
    _path, written = _stub(monkeypatch, tmp_path, choices=(_VLLM_CHOICE,), stub_schema=False)
    _models(monkeypatch, "glm")
    installs = _install_stubs(monkeypatch, schema_after_install=_VLLM_SCHEMA)
    result = CliRunner().invoke(
        main,
        ["provider", "add", "vllm"],
        # y (install) · Server URL · key · context window · model #1
        input="y\nhttps://pod.example/v1\nsk-abc\n131072\n1\n",
    )
    assert result.exit_code == 0, result.output
    assert "installing provider-vllm" in result.output
    assert installs == [("provider-vllm", setup.PROVIDER_SOURCES["provider-vllm"])]
    (entry,) = written
    assert entry["config"]["base_url"] == "${VLLM_BASE_URL}"


def test_add_install_declined_falls_back_to_catalog_fields(tmp_path: Path, monkeypatch) -> None:
    """Declining the install still prompts the catalog's required fields —
    never the key-only basic flow (the issue-#182 regression shape)."""
    _path, written = _stub(monkeypatch, tmp_path, choices=(_VLLM_CHOICE,), stub_schema=False)
    _models(monkeypatch, "glm")
    installs = _install_stubs(monkeypatch, schema_after_install=None)
    result = CliRunner().invoke(
        main,
        ["provider", "add", "vllm"],
        input="n\nhttps://pod.example/v1\nsk-abc\n1\n",  # n (no install) · URL · key · model
    )
    assert result.exit_code == 0, result.output
    assert installs == []
    assert "prompting for the catalog's required fields" in result.output
    (entry,) = written
    assert entry["config"]["base_url"] == "${VLLM_BASE_URL}"
    assert entry["config"]["api_key"] == "${VLLM_API_KEY}"


def test_yes_keeps_basic_path_and_skips_install(tmp_path: Path, monkeypatch) -> None:
    """--yes never installs and never prompts (today's scripted contract)."""
    path, written = _stub(monkeypatch, tmp_path, choices=(_VLLM_CHOICE,), stub_schema=False)

    async def _boom(*a, **k):
        raise AssertionError("--yes must never install")

    monkeypatch.setattr(setup, "install_provider_module", _boom)
    monkeypatch.setattr(setup, "load_provider_info", lambda module_id: None)
    result = CliRunner().invoke(main, ["provider", "add", "vllm", "--api-key", "sk-x", "-y"])
    assert result.exit_code == 0, result.output
    assert setup.read_keys(path)["VLLM_API_KEY"] == "sk-x"
    (entry,) = written
    assert entry["module"] == "provider-vllm"


# ---------------------------------------------------------------------------
# Field-driven setup: the provider's own schema drives the prompts, and the
# Default Model menu lists what the endpoint actually serves.
# ---------------------------------------------------------------------------


def _field(field_id: str, **kw) -> setup.ProviderConfigField:
    return setup.ProviderConfigField(
        id=field_id,
        display_name=kw.pop("display_name", field_id),
        prompt=kw.pop("prompt", ""),
        field_type=kw.pop("field_type", "text"),
        **kw,
    )


_VLLM_CHOICE = setup.ProviderChoice(
    "provider-vllm",
    "vllm",
    "VLLM_API_KEY",
    "VLLM_BASE_URL",
    display="vLLM",
    source_uri=setup.PROVIDER_SOURCES["provider-vllm"],
)

_VLLM_SCHEMA = setup.ProviderFields(
    module_id="provider-vllm",
    key_var="VLLM_API_KEY",
    key_field_id="api_key",
    base_url_var="VLLM_BASE_URL",
    base_url_default="http://localhost:8000/v1",
    has_models=True,
    display_name="vLLM",
    config_fields=(
        _field(
            "base_url",
            display_name="Server URL",
            env_var="VLLM_BASE_URL",
            default="http://localhost:8000/v1",
            required=True,
        ),
        _field("api_key", display_name="API Key", field_type="secret", env_var="VLLM_API_KEY"),
        _field("context_window", display_name="Context Window", env_var="VLLM_CONTEXT_WINDOW"),
    ),
)


def _models(monkeypatch, *ids: str, error: str | None = None):
    async def _listing(*a, **k):
        return setup.ModelCatalog(
            models=tuple(setup.ProviderModel(id=i, display_name=i) for i in ids), error=error
        )

    monkeypatch.setattr(setup, "list_provider_models", _listing)


def test_provider_add_drives_the_declared_schema_and_model_menu(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole point of the port: vLLM is asked for its server URL (a field
    the old one-key flow never prompted for), every env-var-bearing field lands
    in keys.env as a ${VAR}, and the default model is chosen from the models the
    endpoint really serves rather than typed blind."""
    path, written = _stub(monkeypatch, tmp_path, schema=_VLLM_SCHEMA, choices=(_VLLM_CHOICE,))
    _models(monkeypatch, "deepseek-ai/DeepSeek-V4-Flash-0731", "zai-org/GLM-5.2-FP8")
    monkeypatch.setattr(setup, "instance_id_in_use", lambda *a, **k: False)

    result = CliRunner().invoke(
        main,
        ["provider", "add", "vllm"],
        # server URL · api key · context window · model choice [2]
        input="https://pod-4000.proxy.runpod.net/v1\nsk-abc\n131072\n2\n",
    )
    assert result.exit_code == 0, result.output
    assert "Configuring vLLM" in result.output
    assert "[1] deepseek-ai/DeepSeek-V4-Flash-0731" in result.output
    assert "[2] zai-org/GLM-5.2-FP8" in result.output

    # Secrets AND endpoints go to keys.env; settings only ever see ${VAR}.
    assert setup.read_keys(path) == {
        "VLLM_BASE_URL": "https://pod-4000.proxy.runpod.net/v1",
        "VLLM_API_KEY": "sk-abc",
        "VLLM_CONTEXT_WINDOW": "131072",
    }
    (entry,) = written
    assert entry["config"] == {
        "base_url": "${VLLM_BASE_URL}",
        "api_key": "${VLLM_API_KEY}",
        "context_window": "${VLLM_CONTEXT_WINDOW}",
        "default_model": "zai-org/GLM-5.2-FP8",
        "priority": 1,
    }
    # Not installed in this run ⇒ the source is persisted so the next boot
    # installs the module properly.
    assert entry["source"] == setup.PROVIDER_SOURCES["provider-vllm"]


def test_provider_add_model_listing_failure_falls_back_to_free_text(
    tmp_path: Path, monkeypatch
) -> None:
    _path, written = _stub(monkeypatch, tmp_path, schema=_VLLM_SCHEMA, choices=(_VLLM_CHOICE,))
    _models(monkeypatch, error="ConnectionError: endpoint unreachable")
    monkeypatch.setattr(setup, "instance_id_in_use", lambda *a, **k: False)

    result = CliRunner().invoke(
        main,
        ["provider", "add", "vllm"],
        input="http://localhost:8000/v1\n\n\nsome-local-model\n",
    )
    assert result.exit_code == 0, result.output
    assert "could not list models · ConnectionError: endpoint unreachable" in result.output
    (entry,) = written
    assert entry["config"]["default_model"] == "some-local-model"


def test_provider_add_second_instance_gets_its_own_credential_var(
    tmp_path: Path, monkeypatch
) -> None:
    """Reusing VLLM_API_KEY for a second instance would overwrite the first
    instance's key in keys.env and silently break it."""
    path, written = _stub(monkeypatch, tmp_path, schema=_VLLM_SCHEMA, choices=(_VLLM_CHOICE,))
    _models(monkeypatch, "glm")
    monkeypatch.setattr(setup, "claimed_env_vars", lambda *a, **k: {"VLLM_API_KEY"})

    result = CliRunner().invoke(
        main,
        ["provider", "add", "vllm", "--instance-id", "runpod"],
        input="https://pod.example/v1\nsk-second\n\n1\n",
    )
    assert result.exit_code == 0, result.output
    assert "VLLM_RUNPOD_API_KEY" in result.output
    assert setup.read_keys(path)["VLLM_RUNPOD_API_KEY"] == "sk-second"
    assert "VLLM_API_KEY" not in setup.read_keys(path)
    (entry,) = written
    assert entry["id"] == "runpod"
    assert entry["config"]["api_key"] == "${VLLM_RUNPOD_API_KEY}"


def test_yes_needs_no_key_when_the_secret_is_optional(tmp_path: Path, monkeypatch) -> None:
    """vLLM's api_key is required=False (a local endpoint needs none), so -y
    must not demand --api-key the way it does for anthropic."""
    _path, written = _stub(monkeypatch, tmp_path, choices=(_VLLM_CHOICE,))
    monkeypatch.setattr(setup, "load_provider_info", lambda module_id: _VLLM_SCHEMA)
    result = CliRunner().invoke(main, ["provider", "add", "vllm", "-y"])
    assert result.exit_code == 0, result.output
    (entry,) = written
    assert entry["module"] == "provider-vllm"
    assert "api_key" not in entry["config"]


def test_yes_performs_no_network(tmp_path: Path, monkeypatch) -> None:
    _stub(monkeypatch, tmp_path)
    calls: list[str] = []

    async def _boom(*a, **k):
        calls.append("fetched")
        raise AssertionError("--yes must never touch the network")

    monkeypatch.setattr(setup, "ensure_provider_available", _boom)
    monkeypatch.setattr(setup, "list_provider_models", _boom)
    result = CliRunner().invoke(main, ["init", "-p", "anthropic", "--api-key", "sk-x", "-y"])
    assert result.exit_code == 0, result.output
    assert calls == []
