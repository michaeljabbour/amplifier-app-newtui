"""First-run setup logic (``kernel/setup.py``).

The keys.env read/write + env-prefix derivation, against ``tmp_path``.
``discover_providers`` (live ``ModuleLoader``) is covered via the init CLI
smoke test with a stubbed discovery, not here.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from amplifier_app_tui.kernel import setup

# Captured at import, BEFORE the autouse `_offline_provider_setup` fixture
# stubs it out — these tests exercise the real implementation, and everything
# it would reach outside the process is supplied by a fake provider class.
_REAL_LIST_PROVIDER_MODELS = setup.list_provider_models
_REAL_INSTALL_PROVIDER_MODULE = setup.install_provider_module


def _paths(tmp_path: Path):
    from amplifier_app_tui.kernel import bundle_admin

    return bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")


def _read_providers(paths) -> list[dict]:
    from amplifier_app_tui.kernel import bundle_admin

    return bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))["config"]["providers"]


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


# -- advisory lock (concurrent write_key must not drop keys) -----------------


def test_keys_lock_path_sits_next_to_store(tmp_path: Path) -> None:
    path = tmp_path / "keys.env"
    assert setup.keys_lock_path(path) == tmp_path / "keys.env.lock"
    # The lock is a sidecar; the store still reads back and stays chmod 600.
    setup.write_key(path, "ANTHROPIC_API_KEY", "sk", update_environ=False)
    assert setup.read_keys(path) == {"ANTHROPIC_API_KEY": "sk"}
    assert (path.stat().st_mode & 0o777) == 0o600


def test_concurrent_writers_preserve_all_keys(tmp_path: Path) -> None:
    """N threads each save a *distinct* provider key against one shared store.

    Without the advisory lock this read-modify-write is last-writer-wins and
    freshly-saved keys get silently dropped; with it every key survives.
    """
    path = tmp_path / "keys.env"
    names = [f"PROVIDER_{i}_API_KEY" for i in range(12)]
    ready = threading.Barrier(len(names))
    errors: list[BaseException] = []

    def writer(name: str) -> None:
        ready.wait()  # release all writers together to maximise contention
        try:
            setup.write_key(path, name, name.lower(), update_environ=False)
        except Exception as exc:  # noqa: BLE001 — surface worker failure to the assert
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(nm,)) for nm in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    stored = setup.read_keys(path)
    assert set(stored) == set(names)  # nothing dropped
    assert all(stored[name] == name.lower() for name in names)


def test_write_key_serialises_on_advisory_lock(tmp_path: Path) -> None:
    """Holding the lock blocks a concurrent write_key until release.

    Proof the lock (not luck) is what serialises writers: while the lock is
    held the second writer cannot finish its read-modify-write; once released
    it completes and both keys are present.
    """
    path = tmp_path / "keys.env"
    setup.write_key(path, "FIRST_KEY", "1", update_environ=False)
    done = threading.Event()

    def writer() -> None:
        setup.write_key(path, "SECOND_KEY", "2", update_environ=False)
        done.set()

    thread = threading.Thread(target=writer)
    with setup._keys_lock(path):
        thread.start()
        assert not done.wait(timeout=0.5)  # blocked while the lock is held
    thread.join(timeout=10)
    assert done.is_set()  # released -> the writer completed
    assert setup.read_keys(path) == {"FIRST_KEY": "1", "SECOND_KEY": "2"}


def test_advisory_lock_released_when_write_raises(tmp_path: Path, monkeypatch) -> None:
    """A failure inside the guarded write still releases the lock.

    The atomic replace is forced to raise; the ``with`` context frees the
    lock on the way out, so a later writer is never wedged. Also proves the
    original store is untouched when the write fails (atomic-replace intact).
    """
    path = tmp_path / "keys.env"
    setup.write_key(path, "KEEP_KEY", "keep", update_environ=False)

    def boom(_self: Path, _target: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        setup.write_key(path, "DOOMED_KEY", "x", update_environ=False)
    monkeypatch.undo()

    # Lock is free: a non-blocking acquire succeeds immediately.
    lock = setup._keys_lock(path)
    lock.acquire(timeout=0)
    try:
        assert lock.is_locked
    finally:
        lock.release()

    # The failed write left the store intact; a fresh write now goes through.
    assert setup.read_keys(path) == {"KEEP_KEY": "keep"}
    setup.write_key(path, "RECOVER_KEY", "ok", update_environ=False)
    assert setup.read_keys(path) == {"KEEP_KEY": "keep", "RECOVER_KEY": "ok"}


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
    from amplifier_app_tui.kernel import bundle_admin

    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")
    # Seed an existing active provider at priority 1.
    setup.write_provider_config(
        paths, "global", setup.provider_config_entry("provider-openai", key_var="OPENAI_API_KEY")
    )
    setup.write_provider_config(
        paths,
        "global",
        setup.provider_config_entry("provider-anthropic", key_var="ANTHROPIC_API_KEY"),
    )
    providers = bundle_admin.read_scope(bundle_admin.scope_file(paths, "global"))["config"][
        "providers"
    ]
    assert providers[0]["module"] == "provider-anthropic"  # newest is active
    assert providers[0]["config"]["priority"] == 1
    assert providers[1]["module"] == "provider-openai"
    assert providers[1]["config"]["priority"] == 10  # demoted


def test_detect_provider_from_env(monkeypatch) -> None:
    for v in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GITHUB_TOKEN",
    ):
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


# ---------------------------------------------------------------------------
# Dynamic provider setup: catalog, schema, model probe, instance credentials
# ---------------------------------------------------------------------------


def test_provider_sources_catalog_covers_vllm() -> None:
    # The absence of a vllm entry (in either source) is why `amplifier-tui init`
    # could not offer it on a machine where the module was not yet installed.
    assert "provider-vllm" in setup.PROVIDER_SOURCES
    assert setup.PROVIDER_SOURCES["provider-vllm"].startswith("git+https://")
    assert "provider-chat-completions" in setup.PROVIDER_SOURCES


def test_should_show_field_supports_every_pattern() -> None:
    def field(**show_when):
        return setup.ProviderConfigField(
            id="x", display_name="X", prompt="", field_type="text", show_when=show_when or None
        )

    assert setup.should_show_field(field(), {})  # no condition ⇒ always
    assert setup.should_show_field(field(model="opus"), {"model": "OPUS"})  # case-insensitive
    assert not setup.should_show_field(field(model="opus"), {"model": "sonnet"})
    assert setup.should_show_field(field(model="contains:son"), {"model": "sonnet"})
    assert not setup.should_show_field(field(model="contains:son"), {"model": "opus"})
    assert setup.should_show_field(field(model="not_contains:son"), {"model": "opus"})
    assert not setup.should_show_field(field(model="not_contains:son"), {"model": "sonnet"})
    assert setup.should_show_field(field(model="startswith:claude"), {"model": "claude-x"})
    assert not setup.should_show_field(field(model="startswith:claude"), {"model": "gpt"})
    assert setup.should_show_field(field(model="not_startswith:gpt"), {"model": "claude"})
    assert not setup.should_show_field(field(model="not_startswith:gpt"), {"model": "gpt-5"})


def test_resolve_placeholder(monkeypatch) -> None:
    monkeypatch.setenv("SOME_URL", "https://real/v1")
    assert setup.resolve_placeholder("${SOME_URL}") == "https://real/v1"
    assert setup.resolve_placeholder("${UNSET_XYZ}") is None
    assert setup.resolve_placeholder("literal") == "literal"
    assert setup.resolve_placeholder(None) is None


def test_sanitize_env_token_and_instance_suggestion() -> None:
    assert setup.sanitize_env_token("run-pod 2") == "RUN_POD_2"
    assert setup.sanitize_env_token("--x--") == "X"
    assert setup.suggest_instance_env_var("provider-vllm", "runpod", set()) == "VLLM_RUNPOD_API_KEY"
    # A "<type>-<name>" id does not duplicate the type prefix.
    assert (
        setup.suggest_instance_env_var("provider-vllm", "vllm-runpod", set())
        == "VLLM_RUNPOD_API_KEY"
    )


def test_suggest_instance_env_var_refuses_useless_or_colliding_ids() -> None:
    # An id that IS just the type name carries no distinguishing information.
    with pytest.raises(ValueError, match="empty suffix"):
        setup.suggest_instance_env_var("provider-vllm", "vllm", set())
    # Two ids differing only in separator style sanitize identically — emitting
    # the name anyway would recreate the very collision this prevents.
    with pytest.raises(ValueError, match="already in use"):
        setup.suggest_instance_env_var("provider-vllm", "run_pod", {"VLLM_RUN_POD_API_KEY"})


def test_instantiate_provider_threads_real_values_and_respects_rung_order() -> None:
    """VLLMProvider raises ValueError when constructed without base_url, so a
    ladder that stops at the first exception, or omits base_url, cannot build
    it at all."""

    class VllmLike:
        def __init__(self, base_url=None, api_key=None, config=None):
            if base_url is None:
                raise ValueError("base_url or client must be provided")
            self.base_url = base_url
            self.api_key = api_key

    inst = setup._instantiate_provider(VllmLike, {"base_url": "https://pod/v1", "api_key": "sk-1"})
    assert inst is not None
    assert inst.base_url == "https://pod/v1"
    assert inst.api_key == "sk-1"


def test_instantiate_provider_handles_host_style_constructors() -> None:
    class OllamaLike:
        def __init__(self, host=None, config=None):
            if host is None:
                raise TypeError("host required")
            self.host = host

    inst = setup._instantiate_provider(OllamaLike, {"host": "http://localhost:11434"})
    assert inst is not None and inst.host == "http://localhost:11434"


class _FakeModel:
    def __init__(self, ident: str) -> None:
        self.id = ident
        self.display_name = ident
        self.capabilities = ["tools", "fast"]


def _fake_provider_module(monkeypatch, cls) -> None:
    monkeypatch.setattr(setup, "_load_provider_class", lambda module_id: cls)
    setup.reset_provider_info_cache()


@pytest.mark.asyncio
async def test_list_provider_models_success_and_close(monkeypatch) -> None:
    closed: list[bool] = []

    class P:
        def __init__(self, **kw):
            pass

        async def list_models(self):
            return [_FakeModel("glm"), _FakeModel("qwen")]

        async def close(self):
            closed.append(True)

    _fake_provider_module(monkeypatch, P)
    catalog = await _REAL_LIST_PROVIDER_MODELS("provider-vllm", {})
    assert [m.id for m in catalog.models] == ["glm", "qwen"]
    assert catalog.models[0].capabilities == ("tools", "fast")
    assert catalog.error is None
    assert closed == [True]  # the throwaway probe is always closed


@pytest.mark.asyncio
async def test_list_provider_models_accepts_sync_listers(monkeypatch) -> None:
    class P:
        def __init__(self, **kw):
            pass

        def list_models(self):
            return [_FakeModel("m1")]

    _fake_provider_module(monkeypatch, P)
    catalog = await _REAL_LIST_PROVIDER_MODELS("provider-x", {})
    assert [m.id for m in catalog.models] == ["m1"]


@pytest.mark.asyncio
async def test_list_provider_models_reports_errors_instead_of_raising(monkeypatch) -> None:
    class P:
        def __init__(self, **kw):
            pass

        async def list_models(self):
            raise ConnectionError("endpoint unreachable")

    _fake_provider_module(monkeypatch, P)
    catalog = await _REAL_LIST_PROVIDER_MODELS("provider-vllm", {})
    assert catalog.models == ()
    assert "ConnectionError" in (catalog.error or "")


@pytest.mark.asyncio
async def test_list_provider_models_times_out(monkeypatch) -> None:
    class P:
        def __init__(self, **kw):
            pass

        async def list_models(self):
            await asyncio.sleep(5)

    _fake_provider_module(monkeypatch, P)
    catalog = await _REAL_LIST_PROVIDER_MODELS("provider-vllm", {}, timeout=0.01)
    assert catalog.models == ()
    assert "timed out" in (catalog.error or "")


def test_load_provider_info_describes_a_keyless_provider(monkeypatch) -> None:
    """Returning None for a provider with no secret is what made ollama
    unrepresentable — and therefore unofferable."""

    class Info:
        display_name = "Ollama"
        config_fields = [
            SimpleNamespace(
                id="host",
                display_name="Host",
                prompt="Ollama host",
                field_type="text",
                env_var="OLLAMA_HOST",
                default="http://localhost:11434",
                required=True,
            )
        ]

    class P:
        def __init__(self, **kw):
            pass

        def get_info(self):
            return Info()

        def list_models(self):
            return []

    _fake_provider_module(monkeypatch, P)
    info = setup.load_provider_info("provider-ollama")
    assert info is not None
    assert info.key_var is None
    assert info.display_name == "Ollama"
    assert [f.id for f in info.config_fields] == ["host"]
    assert info.config_fields[0].required is True


def test_provider_config_entry_supports_id_source_and_collected_config() -> None:
    entry = setup.provider_config_entry(
        "provider-vllm",
        config={"base_url": "${VLLM_BASE_URL}", "default_model": "glm"},
        instance_id="runpod",
        source="git+https://example/vllm@main",
    )
    assert entry == {
        "module": "provider-vllm",
        "id": "runpod",
        "source": "git+https://example/vllm@main",
        "config": {
            "base_url": "${VLLM_BASE_URL}",
            "default_model": "glm",
            "priority": 1,
        },
    }


def test_write_provider_config_keeps_a_distinct_instance(tmp_path: Path) -> None:
    """`id: runpod` and a plain `provider-vllm` entry are different providers;
    matching on the module alone made adding one delete the other."""
    paths = _paths(tmp_path)
    setup.write_provider_config(
        paths, "global", setup.provider_config_entry("provider-vllm", key_var="VLLM_API_KEY")
    )
    setup.write_provider_config(
        paths,
        "global",
        setup.provider_config_entry(
            "provider-vllm", key_var="VLLM_RUNPOD_API_KEY", instance_id="runpod"
        ),
    )
    entries = _read_providers(paths)
    assert [e.get("id") or e["module"] for e in entries] == ["runpod", "provider-vllm"]
    # Same identity replaces rather than duplicating.
    setup.write_provider_config(
        paths,
        "global",
        setup.provider_config_entry(
            "provider-vllm", key_var="VLLM_RUNPOD_API_KEY", instance_id="runpod", model="glm"
        ),
    )
    entries = _read_providers(paths)
    assert len(entries) == 2
    assert entries[0]["config"]["default_model"] == "glm"


# ---------------------------------------------------------------------------
# Issue #183 — keys_file / source cache must honor AMPLIFIER_HOME
# ---------------------------------------------------------------------------


def test_keys_file_honors_amplifier_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path / "scoped"))
    assert setup.keys_file() == tmp_path / "scoped" / "keys.env"
    # An explicit argument still wins over the env var.
    assert setup.keys_file(tmp_path / "explicit") == tmp_path / "explicit" / "keys.env"
    monkeypatch.delenv("AMPLIFIER_HOME")
    assert setup.keys_file() == Path.home() / ".amplifier" / "keys.env"


def test_amplifier_home_dir_resolution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path / "scoped"))
    assert setup._amplifier_home_dir(None) == tmp_path / "scoped"
    assert setup._amplifier_home_dir(tmp_path) == tmp_path


# ---------------------------------------------------------------------------
# Issue #182 — install-into-environment + the static required-field catalog
# ---------------------------------------------------------------------------


def test_install_provider_module_runs_uv_pip(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    seen: dict = {}

    def _run(cmd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(setup, "_load_provider_class", lambda module_id: object)
    ok, detail = asyncio.run(_REAL_INSTALL_PROVIDER_MODULE("provider-vllm", str(tmp_path)))
    assert ok, detail
    assert seen["cmd"][:4] == ["uv", "pip", "install", "-e"]
    assert seen["cmd"][4] == str(tmp_path)  # local dir source is used directly
    assert "--python" in seen["cmd"]


def test_install_provider_module_reports_failure(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom: no such build backend")

    monkeypatch.setattr(subprocess, "run", _run)
    ok, detail = asyncio.run(_REAL_INSTALL_PROVIDER_MODULE("provider-vllm", str(tmp_path)))
    assert not ok
    assert "boom" in detail


def test_install_provider_module_verifies_import(tmp_path: Path, monkeypatch) -> None:
    """rc == 0 alone is not success — the module must actually import after."""
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(setup, "_load_provider_class", lambda module_id: None)
    ok, detail = asyncio.run(_REAL_INSTALL_PROVIDER_MODULE("provider-vllm", str(tmp_path)))
    assert not ok
    assert "fails to import" in detail


def test_fallback_fields_cover_endpoint_bearing_providers() -> None:
    """The catalog must at least declare the endpoint field these providers
    cannot mount without (the issue-#182 non-mountable-entry shape)."""
    for module_id, endpoint_field in (
        ("provider-vllm", "base_url"),
        ("provider-chat-completions", "base_url"),
        ("provider-ollama", "host"),
        ("provider-azure-openai", "azure_endpoint"),
    ):
        fields = setup.fallback_provider_fields(module_id)
        assert fields is not None, module_id
        required = [f.id for f in fields.config_fields if f.required]
        assert endpoint_field in required, module_id
    # Providers a bare key genuinely configures have no fallback entry.
    assert setup.fallback_provider_fields("provider-anthropic") is None


def test_friendly_provider_name_table_and_titlecase() -> None:
    assert setup.friendly_provider_name("provider-vllm") == "vLLM"
    assert setup.friendly_provider_name("provider-chat-completions") == "OpenAI-Compatible"
    assert setup.friendly_provider_name("provider-something-new") == "Something New"


# ---------------------------------------------------------------------------
# Console writers — edit (priority-preserving replace) and reorder
# ---------------------------------------------------------------------------


def test_replace_provider_config_preserves_position_and_priorities(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    setup.write_provider_config(
        paths, "global", setup.provider_config_entry("provider-anthropic", key_var="A", priority=1)
    )
    setup.write_provider_config(
        paths, "global", setup.provider_config_entry("provider-vllm", key_var="V")
    )
    # vllm is now priority 1, anthropic demoted to 10. Editing anthropic must
    # neither promote it nor demote vllm.
    edited = setup.provider_config_entry(
        "provider-anthropic", config={"api_key": "${A}", "default_model": "claude-x"}, priority=10
    )
    setup.replace_provider_config(paths, "global", edited)
    entries = _read_providers(paths)
    by_module = {e["module"]: e["config"] for e in entries}
    assert by_module["provider-anthropic"]["default_model"] == "claude-x"
    assert by_module["provider-anthropic"]["priority"] == 10
    assert by_module["provider-vllm"]["priority"] == 1


def test_replace_provider_config_appends_when_absent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = setup.provider_config_entry("provider-vllm", key_var="V", priority=4)
    setup.replace_provider_config(paths, "project", entry)
    from amplifier_app_tui.kernel import bundle_admin

    data = bundle_admin.read_scope(bundle_admin.scope_file(paths, "project"))
    assert data["config"]["providers"] == [entry]


def test_set_provider_priorities_rewrites_matching_scopes(tmp_path: Path) -> None:
    from amplifier_app_tui.kernel import bundle_admin

    paths = _paths(tmp_path)
    bundle_admin.write_scope(
        bundle_admin.scope_file(paths, "global"),
        {
            "config": {
                "providers": [
                    {"module": "provider-anthropic", "config": {"priority": 1}},
                    {"module": "provider-vllm", "id": "runpod", "config": {"priority": 2}},
                ]
            }
        },
    )
    bundle_admin.write_scope(
        bundle_admin.scope_file(paths, "local"),
        {"config": {"providers": [{"module": "provider-anthropic", "config": {"priority": 7}}]}},
    )
    setup.set_provider_priorities(paths, {"provider-anthropic": 2, "runpod": 1})
    global_entries = _read_providers(paths)
    assert {e.get("id") or e["module"]: e["config"]["priority"] for e in global_entries} == {
        "provider-anthropic": 2,
        "runpod": 1,
    }
    local = bundle_admin.read_scope(bundle_admin.scope_file(paths, "local"))
    assert local["config"]["providers"][0]["config"]["priority"] == 2


def test_configured_providers_carries_raw_config_and_source(tmp_path: Path) -> None:
    from amplifier_app_tui.kernel import bundle_admin

    paths = _paths(tmp_path)
    bundle_admin.write_scope(
        bundle_admin.scope_file(paths, "global"),
        {
            "config": {
                "providers": [
                    {
                        "module": "provider-vllm",
                        "source": "git+https://example/vllm@main",
                        "config": {"base_url": "${VLLM_BASE_URL}", "priority": 1},
                    }
                ]
            }
        },
    )
    (entry,) = setup.configured_providers(tmp_path / "proj", tmp_path / "home")
    assert entry.config == {"base_url": "${VLLM_BASE_URL}", "priority": 1}
    assert entry.source == "git+https://example/vllm@main"
