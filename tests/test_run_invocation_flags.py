"""Per-invocation ``run`` overrides: --model / --provider / --mode / --resume.

Two layers are exercised:

- the kernel seam (:func:`apply_run_overrides`) that mutates only the in-memory
  mount plan, so ``--model``/``--provider`` stay ephemeral to one invocation and
  never touch a settings scope file; and
- the ``run`` CLI wiring that threads each flag through to ``RealRuntime`` (mode
  posture + provider/model overrides + resume seeding) and fails loud with a
  nonzero exit on any unknown value.
"""

from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner

from amplifier_app_newtui.kernel.config import (
    ProviderNotConfiguredError,
    apply_run_overrides,
)
from amplifier_app_newtui.kernel.persistence import SessionStore
from amplifier_app_newtui.main import main

# ---------------------------------------------------------------------------
# kernel seam: apply_run_overrides is ephemeral (no settings persistence)
# ---------------------------------------------------------------------------


def _plan(*providers: dict) -> dict:
    return {"providers": [dict(p) for p in providers]}


def test_provider_override_promotes_and_sets_model() -> None:
    plan = _plan(
        {"module": "provider-openai", "config": {"default_model": "gpt"}},
        {"module": "provider-anthropic", "config": {"default_model": "old"}},
    )
    returned = apply_run_overrides(plan, provider="anthropic", model="claude-new")
    # Same object, mutated in place (risk #9: the mounted plan is never a copy).
    assert returned is plan
    # The named provider is promoted to highest priority (front)...
    assert plan["providers"][0]["module"] == "provider-anthropic"
    # ...and its default_model is overridden for THIS boot only.
    assert plan["providers"][0]["config"]["default_model"] == "claude-new"
    # The other provider is preserved (multi-provider setup stays intact).
    assert plan["providers"][1]["module"] == "provider-openai"


def test_model_only_targets_priority_provider() -> None:
    plan = _plan({"module": "provider-anthropic", "config": {}})
    apply_run_overrides(plan, model="claude-x")
    assert plan["providers"][0]["config"]["default_model"] == "claude-x"


def test_provider_matched_by_instance_id() -> None:
    plan = _plan(
        {"module": "provider-anthropic", "id": "primary", "config": {}},
        {"module": "provider-anthropic", "instance_id": "backup", "config": {}},
    )
    apply_run_overrides(plan, provider="backup")
    assert plan["providers"][0].get("instance_id") == "backup"


def test_unknown_provider_raises_with_available_names() -> None:
    plan = _plan({"module": "provider-anthropic", "config": {}})
    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        apply_run_overrides(plan, provider="openai")
    assert "openai" in str(excinfo.value)
    assert "anthropic" in str(excinfo.value)


def test_no_providers_configured_raises() -> None:
    with pytest.raises(ProviderNotConfiguredError):
        apply_run_overrides({"providers": []}, provider="anthropic")


def test_no_override_is_a_noop() -> None:
    plan = _plan({"module": "provider-anthropic", "config": {"default_model": "keep"}})
    before = {"providers": [dict(plan["providers"][0])]}
    apply_run_overrides(plan)
    assert plan == before


def test_apply_run_overrides_writes_no_files(tmp_path, monkeypatch) -> None:
    """The override seam mutates a dict only — it must never touch the disk."""
    monkeypatch.chdir(tmp_path)
    plan = _plan({"module": "provider-anthropic", "config": {}})
    apply_run_overrides(plan, provider="anthropic", model="claude-x")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# CLI wiring: each flag threads through to RealRuntime
# ---------------------------------------------------------------------------


class CapturingRuntime:
    """Records the kwargs ``run`` threads into ``RealRuntime`` for one boot."""

    instances: list["CapturingRuntime"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        mode = kwargs.get("mode")
        self.mode_value = mode() if callable(mode) else None
        self.bundle_name = str(kwargs.get("bundle") or "fake")
        self.model_name = "fake-model"
        self.session_id = "sess-capture"
        self.queue: asyncio.Queue = asyncio.Queue()
        type(self).instances.append(self)

    async def start(self) -> None:
        return None

    async def submit(self, prompt: str) -> str:
        return "captured response"

    async def cleanup(self) -> None:
        return None


@pytest.fixture
def capture(monkeypatch) -> type[CapturingRuntime]:
    CapturingRuntime.instances = []
    monkeypatch.setattr("amplifier_app_newtui.kernel.runtime.RealRuntime", CapturingRuntime)
    return CapturingRuntime


def test_default_run_threads_only_bundle(capture) -> None:
    """No overrides ⇒ the untouched ``RealRuntime(bundle=...)`` construction."""
    result = CliRunner().invoke(main, ["run", "hello"])
    assert result.exit_code == 0
    (runtime,) = capture.instances
    assert runtime.kwargs == {"bundle": None}
    assert runtime.mode_value is None


def test_mode_flag_threads_posture(capture) -> None:
    result = CliRunner().invoke(main, ["run", "--mode", "plan", "hello"])
    assert result.exit_code == 0
    (runtime,) = capture.instances
    assert runtime.mode_value == "plan"


def test_model_and_provider_flags_thread_as_ephemeral_overrides(capture) -> None:
    result = CliRunner().invoke(
        main, ["run", "--provider", "anthropic", "--model", "claude-x", "hello"]
    )
    assert result.exit_code == 0
    (runtime,) = capture.instances
    assert runtime.kwargs["provider_override"] == "anthropic"
    assert runtime.kwargs["model_override"] == "claude-x"


def test_provider_alone_threads_without_model(capture) -> None:
    result = CliRunner().invoke(main, ["run", "--provider", "anthropic", "hello"])
    assert result.exit_code == 0
    (runtime,) = capture.instances
    assert runtime.kwargs["provider_override"] == "anthropic"
    assert "model_override" not in runtime.kwargs


def test_resume_flag_seeds_from_named_session(capture, tmp_path, monkeypatch) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    full_id = "a1b2c3d4e5f600000000000000000000"
    store.save(full_id, [{"role": "user", "content": "earlier"}], {"bundle": "newtui"})
    monkeypatch.setattr("amplifier_app_newtui.main._session_store", lambda: store)

    result = CliRunner().invoke(main, ["run", "--resume", full_id[:8], "next"])
    assert result.exit_code == 0
    (runtime,) = capture.instances
    # The partial id is resolved to the full stored id and handed to the runtime,
    # which owns the existing resume/persistence machinery for context seeding.
    assert runtime.kwargs["resume_id"] == full_id


# ---------------------------------------------------------------------------
# CLI wiring: unknown / invalid values fail loud with a nonzero exit
# ---------------------------------------------------------------------------


def test_model_without_provider_errors(capture) -> None:
    result = CliRunner().invoke(main, ["run", "--model", "claude-x", "hello"])
    assert result.exit_code == 1
    assert "requires --provider" in result.stderr
    assert capture.instances == []  # never reached a boot


def test_unknown_mode_errors(capture) -> None:
    result = CliRunner().invoke(main, ["run", "--mode", "bogus", "hello"])
    assert result.exit_code == 1
    assert "unknown mode" in result.stderr
    assert "auto" in result.stderr  # valid ids are listed
    assert capture.instances == []


def test_unknown_resume_session_errors(capture, tmp_path, monkeypatch) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    monkeypatch.setattr("amplifier_app_newtui.main._session_store", lambda: store)
    result = CliRunner().invoke(main, ["run", "--resume", "deadbeef", "hello"])
    assert result.exit_code == 1
    assert "no session found" in result.stderr
    assert capture.instances == []


def test_ambiguous_resume_prefix_errors(capture, tmp_path, monkeypatch) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    for suffix in ("1", "2"):
        sid = "aaaa" + suffix + "0" * 27
        store.save(sid, [{"role": "user", "content": "x"}], {"bundle": "newtui"})
    monkeypatch.setattr("amplifier_app_newtui.main._session_store", lambda: store)
    result = CliRunner().invoke(main, ["run", "--resume", "aaaa", "hello"])
    assert result.exit_code == 1
    assert capture.instances == []


def test_overrides_do_not_write_settings_files(capture, tmp_path, monkeypatch) -> None:
    """--model/--provider must not persist anything under the amplifier home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    result = CliRunner().invoke(
        main, ["run", "--provider", "anthropic", "--model", "claude-x", "hello"]
    )
    assert result.exit_code == 0
    # The override rode the ephemeral runtime seam, not the persistent
    # provider-config writer: no settings scope file was created.
    assert not list(home.rglob("settings.yaml"))
    assert not list(home.rglob("config.yaml"))
