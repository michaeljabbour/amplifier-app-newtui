"""First-run setup logic (``kernel/setup.py``).

The keys.env read/write + env-prefix derivation, against ``tmp_path``.
``discover_providers`` (live ``ModuleLoader``) is covered via the init CLI
smoke test with a stubbed discovery, not here.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

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
    from amplifier_app_newtui.kernel import bundle_admin

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
