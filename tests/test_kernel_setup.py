"""First-run setup logic (``kernel/setup.py``).

The keys.env read/write + env-prefix derivation, against ``tmp_path``.
``discover_providers`` (live ``ModuleLoader``) is covered via the init CLI
smoke test with a stubbed discovery, not here.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from amplifier_app_newtui.kernel import setup


def test_provider_env_prefix() -> None:
    assert setup.provider_env_prefix("provider-anthropic") == "ANTHROPIC"
    assert setup.provider_env_prefix("provider-openai") == "OPENAI"
    assert setup.provider_env_prefix("amplifier-module-provider-vllm") == "VLLM"


def test_write_key_creates_reads_and_chmods(tmp_path: Path) -> None:
    path = tmp_path / "keys.env"
    setup.write_key(path, "ANTHROPIC_API_KEY", "sk-abc", update_environ=False)
    assert setup.read_keys(path) == {"ANTHROPIC_API_KEY": "sk-abc"}
    assert setup.stored_key_names(path) == {"ANTHROPIC_API_KEY"}
    # Secret file locked down (POSIX).
    assert (path.stat().st_mode & 0o777) == 0o600


def test_write_key_updates_in_place_and_preserves_others(tmp_path: Path) -> None:
    path = tmp_path / "keys.env"
    path.write_text("# creds\nOPENAI_API_KEY=old\nHF_TOKEN=hf-1\n", encoding="utf-8")
    setup.write_key(path, "OPENAI_API_KEY", "new", update_environ=False)
    text = path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=new" in text
    assert "old" not in text
    assert "HF_TOKEN=hf-1" in text  # untouched
    assert "# creds" in text  # comment preserved


def test_write_key_updates_environ(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("XYZ_API_KEY", raising=False)
    setup.write_key(tmp_path / "keys.env", "XYZ_API_KEY", "live")
    assert os.environ["XYZ_API_KEY"] == "live"


def test_read_keys_ignores_comments_and_blank(tmp_path: Path) -> None:
    path = tmp_path / "keys.env"
    path.write_text('\n# a comment\nANTHROPIC_API_KEY="quoted"\nbad line\n', encoding="utf-8")
    assert setup.read_keys(path) == {"ANTHROPIC_API_KEY": "quoted"}


def test_load_provider_info_reads_authoritative_env_var(monkeypatch) -> None:
    # Keep the offline suite hermetic: the provider packages are runtime
    # modules, not frozen app dependencies. A provider's get_info() remains
    # the authoritative source rather than the <PREFIX>_API_KEY convention.
    module_name = "amplifier_module_provider_anthropic"
    module = ModuleType(module_name)

    class AnthropicProvider:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def get_info(self):  # noqa: ANN201 - provider protocol fake
            return SimpleNamespace(
                config_fields=(
                    SimpleNamespace(
                        id="api_key",
                        field_type="secret",
                        env_var="ANTHROPIC_API_KEY",
                        default=None,
                    ),
                    SimpleNamespace(
                        id="base_url",
                        field_type="string",
                        env_var="ANTHROPIC_BASE_URL",
                        default="https://api.anthropic.com",
                    ),
                )
            )

        async def list_models(self):  # noqa: ANN201 - provider protocol fake
            return []

    AnthropicProvider.__module__ = module_name
    module.AnthropicProvider = AnthropicProvider
    monkeypatch.setitem(sys.modules, module_name, module)

    info = setup.load_provider_info("provider-anthropic")
    assert info is not None
    assert info.key_var == "ANTHROPIC_API_KEY"
    assert info.base_url_var == "ANTHROPIC_BASE_URL"


def test_load_provider_info_none_for_unknown() -> None:
    assert setup.load_provider_info("provider-does-not-exist") is None


def test_provider_config_entry_uses_placeholders() -> None:
    entry = setup.provider_config_entry(
        "provider-openai",
        key_var="OPENAI_API_KEY",
        model="gpt-x",
        base_url="https://x/v1",
        base_url_var="OPENAI_BASE_URL",
    )
    assert entry == {
        "module": "provider-openai",
        "config": {
            "default_model": "gpt-x",
            "api_key": "${OPENAI_API_KEY}",
            "base_url": "${OPENAI_BASE_URL}",
            "priority": 1,
        },
    }


def test_write_provider_config_prepends_and_demotes(tmp_path: Path) -> None:
    from amplifier_app_newtui.kernel import bundle_admin

    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")
    # Seed an existing active provider at priority 1.
    setup.write_provider_config(
        paths, "global", setup.provider_config_entry("provider-openai", key_var="OPENAI_API_KEY")
    )
    setup.write_provider_config(
        paths, "global", setup.provider_config_entry("provider-anthropic", key_var="ANTHROPIC_API_KEY")
    )
    providers = bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))["config"]["providers"]
    assert providers[0]["module"] == "provider-anthropic"  # newest is active
    assert providers[0]["config"]["priority"] == 1
    assert providers[1]["module"] == "provider-openai"
    assert providers[1]["config"]["priority"] == 10  # demoted


def test_detect_provider_from_env(monkeypatch) -> None:
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    assert setup.detect_provider_from_env() is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    assert setup.detect_provider_from_env() == "provider-openai"


def test_setup_status_reads_keys_and_bundle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home).mkdir()
    (home / "keys.env").write_text("ANTHROPIC_API_KEY=x\n", encoding="utf-8")
    status = setup.setup_status(tmp_path / "proj", home)
    assert status.stored_keys == ("ANTHROPIC_API_KEY",)
    assert status.active_bundle is None  # nothing set in tmp scopes
    assert status.keys_path == home / "keys.env"
