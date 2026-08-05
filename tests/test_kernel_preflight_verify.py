"""Unit tests for real provider verification (kernel/preflight_verify.py, S4 AC4).

Each fake provider module is a genuine, importable Python package written to
a temp dir and grafted onto ``sys.path`` (mirrors ``tests/test_runtime_offline
.py``'s own fake-module pattern) -- ``verify_provider`` really imports and
really mounts it, exactly as it would a real bundle-sourced provider. No API
keys, no network: the only "live" calls in this file are to in-process fakes.

Module ids are unique per scenario so ``sys.modules`` caching (a real,
permanent effect of ``importlib.import_module``) can never leak state
between tests.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.preflight_verify import ProviderVerification, verify_provider


def _install_fake_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module_id: str, source: str
) -> None:
    """Write *source* as ``amplifier_module_<id>/__init__.py`` and graft it.

    Mirrors how ``ModuleActivator.activate()`` grafts a resolved bundle
    module's directory onto ``sys.path`` -- the exact precondition
    ``verify_provider``'s ``_import_provider_module`` depends on.
    """
    package_name = f"amplifier_module_{module_id.replace('-', '_')}"
    package_dir = tmp_path / package_name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, package_name, raising=False)


_FLEX_PROVIDER = '''
"""Fake provider module -- behavior driven entirely by config (offline)."""

CALLS = []


class FlexProvider:
    name = "flex"

    def __init__(self, config):
        self.config = dict(config or {})

    def get_info(self):
        from amplifier_core import ProviderInfo

        return ProviderInfo(
            id="flex",
            display_name="Flex",
            credential_env_vars=list(self.config.get("credential_env_vars") or []),
        )

    async def list_models(self):
        import asyncio

        from amplifier_core import ModelInfo

        if self.config.get("list_models_raises"):
            raise RuntimeError(self.config["list_models_raises"])
        if self.config.get("list_models_hangs"):
            await asyncio.sleep(self.config["list_models_hangs"])
        return [
            ModelInfo(id=m, display_name=m, context_window=1000, max_output_tokens=100)
            for m in (self.config.get("list_models_return") or [])
        ]

    async def complete(self, request=None, **kwargs):
        return {}

    def parse_tool_calls(self, response):
        return []


async def mount(coordinator, config=None):
    config = config or {}
    provider = FlexProvider(config)
    await coordinator.mount("providers", provider, name="flex")

    async def _cleanup():
        CALLS.append("mount_cleanup")

    coordinator.register_cleanup(lambda: CALLS.append("coordinator_cleanup"))
    return _cleanup
'''

_MISSING_PROTOCOL_PROVIDER = '''
"""Fake provider that mounts but omits parse_tool_calls (protocol violation)."""


class Incomplete:
    name = "incomplete"

    def get_info(self):
        from amplifier_core import ProviderInfo

        return ProviderInfo(id="incomplete", display_name="Incomplete")

    async def list_models(self):
        return []

    async def complete(self, request=None, **kwargs):
        return {}

    # no parse_tool_calls() -- structurally fails the Provider protocol


async def mount(coordinator, config=None):
    await coordinator.mount("providers", Incomplete(), name="incomplete")
    return None
'''

_RAISING_MOUNT_PROVIDER = '''
"""Fake provider whose mount() raises, echoing a secret from config."""


async def mount(coordinator, config=None):
    config = config or {}
    raise ValueError(f"cannot start client: bad key {config.get('api_key')}")
'''

_NO_MOUNT_FUNCTION = '''
"""Fake module with no mount() at all."""

value = 1
'''

_MISSING_DEPENDENCY_PROVIDER = '''
"""Fake provider whose OWN import fails on a FOREIGN third-party package --
the "not pip-installed yet" shape that must degrade, not block."""

import some_definitely_missing_third_party_sdk_xyz  # noqa: F401
'''


# ---------------------------------------------------------------------------
# Check 1: real provider mounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_mount_satisfies_protocol_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-flexok", _FLEX_PROVIDER)
    result = await verify_provider(module_id="provider-flexok", config={}, model="")
    assert result == ProviderVerification(ok=True)


@pytest.mark.asyncio
async def test_bundle_module_never_resolved_fails_closed() -> None:
    """Nothing was ever grafted onto sys.path for this module id -- the
    plan claimed a provider that flatly does not exist. This must NOT
    degrade (see module docstring: only a FOREIGN missing dependency
    degrades, not the bundle module itself)."""
    result = await verify_provider(module_id="provider-totally-never-exists", config={}, model="")
    assert result.ok is False
    assert "failed to import" in (result.error or "")
    assert result.remediation is not None


@pytest.mark.asyncio
async def test_missing_third_party_dependency_degrades_not_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle module that resolved fine but whose OWN SDK import isn't
    pip-installed yet (this preflight runs with install_deps=False) must
    degrade -- the real launch's install_deps=True pass fixes this next,
    so hard-failing here would block a launch that was about to work."""
    _install_fake_module(tmp_path, monkeypatch, "provider-missingdep", _MISSING_DEPENDENCY_PROVIDER)
    result = await verify_provider(module_id="provider-missingdep", config={}, model="")
    assert result == ProviderVerification(ok=True)


@pytest.mark.asyncio
async def test_strict_missing_third_party_dependency_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostic/explicit-override checks cannot call an unmounted provider
    ready merely because the following real launch *might* self-heal it."""
    _install_fake_module(
        tmp_path,
        monkeypatch,
        "provider-missingdep-strict",
        _MISSING_DEPENDENCY_PROVIDER,
    )
    result = await verify_provider(
        module_id="provider-missingdep-strict",
        config={},
        model="",
        strict=True,
    )
    assert result.ok is False
    assert "some_definitely_missing_third_party_sdk_xyz" in (result.error or "")
    assert result.remediation is not None and "without --model" in result.remediation


@pytest.mark.asyncio
async def test_no_mount_function_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-nomount", _NO_MOUNT_FUNCTION)
    result = await verify_provider(module_id="provider-nomount", config={}, model="")
    assert result.ok is False
    assert "no mount()" in (result.error or "")


@pytest.mark.asyncio
async def test_mounted_object_missing_protocol_method_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-incomplete", _MISSING_PROTOCOL_PROVIDER)
    result = await verify_provider(module_id="provider-incomplete", config={}, model="")
    assert result.ok is False
    assert "does not satisfy the Provider protocol" in (result.error or "")


@pytest.mark.asyncio
async def test_mount_raising_fails_closed_and_never_leaks_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-raisingmount", _RAISING_MOUNT_PROVIDER)
    secret = "sk-live-super-secret-0000000000"
    result = await verify_provider(
        module_id="provider-raisingmount", config={"api_key": secret}, model=""
    )
    assert result.ok is False
    assert "failed to mount" in (result.error or "")
    assert secret not in (result.error or "")
    assert "***" in (result.error or "")


@pytest.mark.asyncio
async def test_cleanup_runs_both_paths_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mount()-returned cleanup callable AND anything registered via
    coordinator.register_cleanup() must both run -- "clean up whatever
    you create", belt-and-suspenders like amplifier_core.validation
    .provider.ProviderValidator does for the identical reason."""
    _install_fake_module(tmp_path, monkeypatch, "provider-flexclean", _FLEX_PROVIDER)
    import amplifier_module_provider_flexclean as flex_module  # type: ignore[import-not-found]

    flex_module.CALLS.clear()
    result = await verify_provider(module_id="provider-flexclean", config={}, model="")
    assert result.ok is True
    assert set(flex_module.CALLS) == {"mount_cleanup", "coordinator_cleanup"}


@pytest.mark.asyncio
async def test_cleanup_runs_even_when_a_later_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-flexcleanfail", _FLEX_PROVIDER)
    import amplifier_module_provider_flexcleanfail as flex_module  # type: ignore[import-not-found]

    flex_module.CALLS.clear()
    monkeypatch.delenv("FLEX_MISSING_CRED_CLEANUP", raising=False)
    result = await verify_provider(
        module_id="provider-flexcleanfail",
        config={"credential_env_vars": ["FLEX_MISSING_CRED_CLEANUP"]},
        model="",
    )
    assert result.ok is False
    assert set(flex_module.CALLS) == {"mount_cleanup", "coordinator_cleanup"}


# ---------------------------------------------------------------------------
# Check 2: credential viability (offline: presence + non-blank, always on)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_credential_fails_closed_naming_only_the_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-credmissing", _FLEX_PROVIDER)
    monkeypatch.delenv("FLEX_CRED_MISSING_XYZ", raising=False)
    result = await verify_provider(
        module_id="provider-credmissing",
        config={"credential_env_vars": ["FLEX_CRED_MISSING_XYZ"]},
        model="",
    )
    assert result.ok is False
    assert "FLEX_CRED_MISSING_XYZ" in (result.error or "")
    assert "not set" in (result.error or "")
    assert result.remediation is not None and "init" in result.remediation


@pytest.mark.asyncio
async def test_blank_credential_value_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Well-formed means non-blank -- whitespace-only is not a usable key."""
    _install_fake_module(tmp_path, monkeypatch, "provider-credblank", _FLEX_PROVIDER)
    monkeypatch.setenv("FLEX_CRED_BLANK_XYZ", "   ")
    result = await verify_provider(
        module_id="provider-credblank",
        config={"credential_env_vars": ["FLEX_CRED_BLANK_XYZ"]},
        model="",
    )
    assert result.ok is False
    assert "FLEX_CRED_BLANK_XYZ" in (result.error or "")


@pytest.mark.asyncio
async def test_present_credential_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-credok", _FLEX_PROVIDER)
    monkeypatch.setenv("FLEX_CRED_OK_XYZ", "a-real-looking-value")
    result = await verify_provider(
        module_id="provider-credok",
        config={"credential_env_vars": ["FLEX_CRED_OK_XYZ"]},
        model="",
    )
    assert result == ProviderVerification(ok=True)


@pytest.mark.asyncio
async def test_keyless_provider_has_nothing_to_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``credential_env_vars=[]`` (ollama-shaped) always passes."""
    _install_fake_module(tmp_path, monkeypatch, "provider-keyless", _FLEX_PROVIDER)
    result = await verify_provider(module_id="provider-keyless", config={}, model="")
    assert result == ProviderVerification(ok=True)


@pytest.mark.asyncio
async def test_all_required_vars_must_be_present_not_just_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Azure-shaped: two vars required together (matches
    ``detect_provider_from_env``'s own ``all(...)`` semantics)."""
    _install_fake_module(tmp_path, monkeypatch, "provider-credtwo", _FLEX_PROVIDER)
    monkeypatch.setenv("FLEX_CRED_TWO_KEY", "present")
    monkeypatch.delenv("FLEX_CRED_TWO_ENDPOINT", raising=False)
    result = await verify_provider(
        module_id="provider-credtwo",
        config={"credential_env_vars": ["FLEX_CRED_TWO_KEY", "FLEX_CRED_TWO_ENDPOINT"]},
        model="",
    )
    assert result.ok is False
    assert "FLEX_CRED_TWO_ENDPOINT" in (result.error or "")
    assert "FLEX_CRED_TWO_KEY" not in (result.error or "")  # only the MISSING one is named


# ---------------------------------------------------------------------------
# Check 3: selected-model availability (static default; live is opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_model_configured_has_nothing_to_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank model -- the provider's own default applies."""
    _install_fake_module(tmp_path, monkeypatch, "provider-nomodel", _FLEX_PROVIDER)
    result = await verify_provider(module_id="provider-nomodel", config={}, model="")
    assert result == ProviderVerification(ok=True)


@pytest.mark.asyncio
async def test_static_tier_never_calls_list_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (non-live) path: a configured model passes without ever
    invoking list_models() -- if it DID get called, list_models_raises
    below would turn this into a failure."""
    _install_fake_module(tmp_path, monkeypatch, "provider-static", _FLEX_PROVIDER)
    result = await verify_provider(
        module_id="provider-static",
        config={"list_models_raises": "must never be called"},
        model="some-model",
        live_verify=False,
    )
    assert result == ProviderVerification(ok=True)


@pytest.mark.asyncio
async def test_live_tier_passes_when_model_is_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-liveok", _FLEX_PROVIDER)
    result = await verify_provider(
        module_id="provider-liveok",
        config={"list_models_return": ["model-a", "model-b"]},
        model="model-b",
        live_verify=True,
    )
    assert result == ProviderVerification(ok=True)


@pytest.mark.asyncio
async def test_strict_live_tier_fails_when_model_catalog_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty catalog is inconclusive, not proof an override is valid."""
    _install_fake_module(tmp_path, monkeypatch, "provider-liveempty", _FLEX_PROVIDER)
    result = await verify_provider(
        module_id="provider-liveempty",
        config={"list_models_return": []},
        model="model-nope",
        live_verify=True,
        strict=True,
    )
    assert result.ok is False
    assert "returned no models" in (result.error or "")
    assert "model-nope" in (result.error or "")


@pytest.mark.asyncio
async def test_strict_live_tier_probes_even_when_provider_uses_its_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor has no explicit override but still needs a real readiness signal."""
    _install_fake_module(tmp_path, monkeypatch, "provider-livedefault", _FLEX_PROVIDER)
    result = await verify_provider(
        module_id="provider-livedefault",
        config={"list_models_return": ["provider-default"]},
        model="",
        live_verify=True,
        strict=True,
    )
    assert result == ProviderVerification(ok=True)


@pytest.mark.asyncio
async def test_live_tier_fails_when_model_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-liveunknown", _FLEX_PROVIDER)
    result = await verify_provider(
        module_id="provider-liveunknown",
        config={"list_models_return": ["model-a", "model-b"]},
        model="model-nope",
        live_verify=True,
    )
    assert result.ok is False
    assert "model-nope" in (result.error or "")
    assert "model-a" in (result.error or "") and "model-b" in (result.error or "")


@pytest.mark.asyncio
async def test_live_tier_surfaces_list_models_errors_rather_than_swallowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the user opts into the network round trip (--dry-run), an
    error IS the signal (often an auth failure) -- swallowing it would
    make the opt-in tier pointless."""
    _install_fake_module(tmp_path, monkeypatch, "provider-liveerr", _FLEX_PROVIDER)
    result = await verify_provider(
        module_id="provider-liveerr",
        config={"list_models_raises": "401 unauthorized"},
        model="some-model",
        live_verify=True,
    )
    assert result.ok is False
    assert "401 unauthorized" in (result.error or "")


@pytest.mark.asyncio
async def test_live_tier_times_out_rather_than_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-livehang", _FLEX_PROVIDER)
    result = await verify_provider(
        module_id="provider-livehang",
        config={"list_models_hangs": 5},
        model="some-model",
        live_verify=True,
        live_timeout=0.05,
    )
    assert result.ok is False
    assert "timed out" in (result.error or "")


@pytest.mark.asyncio
async def test_live_tier_scrubs_secrets_out_of_list_models_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(tmp_path, monkeypatch, "provider-liveerrsecret", _FLEX_PROVIDER)
    secret = "sk-another-super-secret-value"
    result = await verify_provider(
        module_id="provider-liveerrsecret",
        config={"api_key": secret, "list_models_raises": f"denied for key {secret}"},
        model="some-model",
        live_verify=True,
    )
    assert result.ok is False
    assert secret not in (result.error or "")
