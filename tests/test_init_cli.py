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


def _stub(monkeypatch, tmp_path: Path, *, schema=None, choices=None):
    """Offline init wiring: a fixed provider list, a tmp keys file, no settings write.

    ``onboarding_choices`` is stubbed whole (not just ``discover_providers``)
    so the numbered menu stays exactly ``_CHOICES`` — the real one now also
    unions the module catalog, which is covered in test_kernel_providers.

    ``_resolve_provider_schema`` returns *schema*; ``None`` (the default)
    selects the degraded basic flow, which is what the pre-existing
    key-prompt expectations describe.
    """
    path = tmp_path / "keys.env"
    written: list = []

    offered = _CHOICES if choices is None else tuple(choices)

    async def _choices(*a, **k):
        return offered

    async def _schema(choice):
        return schema

    monkeypatch.setattr(setup, "onboarding_choices", _choices)
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
