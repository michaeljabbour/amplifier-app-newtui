"""Unit tests for the pre-takeover mount/provider preflight (kernel/preflight.py, S4/AC4).

``run_preflight`` wraps ``resolve_config`` (the exact function the real boot
calls) and never goes further: these tests fake ``resolve_config`` so no real
bundle/network work happens, and prove the "no session creation" contract
directly (a ``prepared`` stand-in that fails the test if ``create_session``
is ever called).

The AC4 follow-up (real provider mounting/credential/model checks) has its
OWN unit tests at the ``preflight_verify`` layer
(``tests/test_kernel_preflight_verify.py``); the tests below cover only the
INTEGRATION seam -- that ``run_preflight`` calls ``verify_provider`` with the
right arguments, surfaces its failure verbatim, and respects the
``preflight.*`` settings escape hatches -- plus one true end-to-end test
proving the whole pipeline (fake ``resolve_config`` -> mount plan -> a REAL
imported/mounted fake provider module) works together.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_tui.kernel import preflight as preflight_mod
from amplifier_app_tui.kernel.config import (
    BundleNotFoundError,
    ProviderNotConfiguredError,
    ResolvedConfig,
)
from amplifier_app_tui.kernel.preflight import PreflightReport, run_preflight
from amplifier_app_tui.kernel.preflight_verify import ProviderVerification


class _NeverMount:
    """Stand-in for ``prepared`` -- fails the test if the heavy mount step runs.

    Preflight must resolve config and stop; it must never attempt the actual
    module-mounting session creation (that stays a real-boot-only cost).
    """

    async def create_session(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError("preflight must never create a session (no real mount)")


def _resolved(
    *,
    providers: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    bundle_name: str = "tui",
    bundle_uri: str = "file:///tui.md",
) -> ResolvedConfig:
    mount_plan: dict[str, Any] = {
        "providers": providers if providers is not None else [],
        "tools": tools if tools is not None else [],
    }
    return ResolvedConfig(
        bundle_name=bundle_name,
        bundle_uri=bundle_uri,
        settings=settings if settings is not None else {},
        prepared=_NeverMount(),
        mount_plan=mount_plan,
        project_dir=Path.cwd(),
    )


def _patch_resolve_config(monkeypatch: pytest.MonkeyPatch, fake) -> None:
    monkeypatch.setattr(preflight_mod, "resolve_config", fake)


async def _always_ok_verify_provider(**_kwargs: Any) -> ProviderVerification:
    """Bypass the AC4 real-mount/credential/model check.

    These are PLAN-only tests (which provider/model wins, is routing
    enabled) predating that check; none of them install a real importable
    provider module, so without this they would trip the (entirely
    correct) new "provider module failed to import" failure for a bare
    placeholder id like ``provider-anthropic``. The real-mount/credential/
    model behavior has its own dedicated tests (test_kernel_preflight_verify
    .py, plus the "AC4 follow-up" + "true end-to-end" sections below).
    """
    return ProviderVerification(ok=True)


def _bypass_provider_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight_mod, "verify_provider", _always_ok_verify_provider)


# ---------------------------------------------------------------------------
# success: reports what would mount
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_reports_bundle_provider_model_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    _bypass_provider_verification(monkeypatch)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(
            providers=[{"module": "provider-anthropic", "config": {"default_model": "claude-x"}}],
            tools=[{"module": "tool-bash"}],
            settings={"routing": {"enabled": True}},
            bundle_name="tui",
        )

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight("tui")
    assert report == PreflightReport(
        ok=True,
        bundle_name="tui",
        bundle_uri="file:///tui.md",
        provider="anthropic",
        model="claude-x",
        provider_count=1,
        tool_count=1,
        routing_enabled=True,
    )


@pytest.mark.asyncio
async def test_ok_selects_lowest_priority_provider_not_list_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule as the orchestrator/banner: lowest ``config.priority`` wins,
    list position does not (mirrors ``runtime._provider_and_model``)."""
    _bypass_provider_verification(monkeypatch)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(
            providers=[
                {"module": "provider-openai", "config": {"priority": 5, "default_model": "gpt"}},
                {
                    "module": "provider-anthropic",
                    "config": {"priority": 1, "default_model": "claude-x"},
                },
            ],
        )

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight(None)
    assert report.ok is True
    assert report.provider == "anthropic"
    assert report.model == "claude-x"
    assert report.provider_count == 2


@pytest.mark.asyncio
async def test_routing_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _bypass_provider_verification(monkeypatch)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(providers=[{"module": "provider-anthropic", "config": {}}])

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight(None)
    assert report.ok is True
    assert report.routing_enabled is False


# ---------------------------------------------------------------------------
# failure: no providers configured (the same hard-fail MountReport.no_provider
# would raise ProviderMountError for, once mounting is attempted for real)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_providers_fails_with_init_remediation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(providers=[], bundle_name="minimal")

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight("minimal")
    assert report.ok is False
    assert report.bundle_name == "minimal"
    assert report.error == "no provider configured"
    assert report.remediation is not None
    assert "init" in report.remediation


# ---------------------------------------------------------------------------
# failure: resolve_config raises -- every case must fail closed, never raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_not_found_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        raise BundleNotFoundError("no bundle named 'nope'")

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight("nope")
    assert report.ok is False
    assert "bundle not found" in report.error
    assert "nope" in report.error
    assert report.remediation is not None
    assert "bundle list" in report.remediation


@pytest.mark.asyncio
async def test_provider_override_not_configured_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        raise ProviderNotConfiguredError(
            "provider 'vllm' is not configured \u00b7 available: anthropic"
        )

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight(None, provider_override="vllm")
    assert report.ok is False
    assert report.error == "provider 'vllm' is not configured \u00b7 available: anthropic"
    assert report.remediation is not None
    assert "provider list" in report.remediation


@pytest.mark.asyncio
async def test_generic_resolution_failure_fails_closed_not_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything else ``resolve_config`` can raise (malformed bundle YAML, a
    broken ``includes:`` chain, ...) must come back as ``ok=False`` -- never
    propagate. Pre-takeover beats a raw traceback after."""

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        raise RuntimeError("boom: malformed overlay")

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight("custom")
    assert report.ok is False
    assert "failed to resolve mounts" in report.error
    assert "boom: malformed overlay" in report.error
    assert report.remediation is not None
    assert "doctor" in report.remediation


# ---------------------------------------------------------------------------
# arguments thread through to resolve_config unchanged (same call the real
# boot makes -- no extra/different network surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overrides_thread_through_to_resolve_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _bypass_provider_verification(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001
        seen["bundle"] = bundle
        seen.update(kwargs)
        return _resolved(providers=[{"module": "provider-anthropic", "config": {}}])

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight(
        "custom-bundle", provider_override="anthropic", model_override="claude-x"
    )
    assert report.ok is True
    assert seen["bundle"] == "custom-bundle"
    assert seen["provider_override"] == "anthropic"
    assert seen["model_override"] == "claude-x"


@pytest.mark.asyncio
async def test_skips_dependency_install_to_stay_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """``install_deps=False`` is deliberate (see module docstring): measured on a
    realistic bundle, the default ``install_deps=True`` costs ~0.6-0.9s PER
    MODULE (foundation's ``ModuleActivator`` shells out to verify/install each
    module's deps even when already satisfied) -- an extra full pass before
    every launch would roughly double real startup latency. Module SOURCE
    resolution (what actually fails for a bad --bundle) is unaffected."""
    seen: dict[str, Any] = {}

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        seen.update(kwargs)
        return _resolved(providers=[{"module": "provider-anthropic", "config": {}}])

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    await run_preflight(None)
    assert seen["install_deps"] is False


# ---------------------------------------------------------------------------
# AC4 follow-up: real provider mounting / credential / model verification
# (integration seam only -- see preflight_verify's own tests for the checks
# themselves)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_priority_provider_is_verified_with_its_real_module_and_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_preflight`` calls ``verify_provider`` with the WINNING (lowest-
    priority) provider's real module id + config + selected model."""
    seen: dict[str, Any] = {}

    async def fake_verify_provider(**kwargs: Any) -> ProviderVerification:
        seen.update(kwargs)
        return ProviderVerification(ok=True)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(
            providers=[
                {"module": "provider-openai", "config": {"priority": 5, "default_model": "gpt"}},
                {
                    "module": "provider-anthropic",
                    "config": {"priority": 1, "default_model": "claude-x", "api_key": "sk-x"},
                },
            ],
        )

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    monkeypatch.setattr(preflight_mod, "verify_provider", fake_verify_provider)
    report = await run_preflight(None)

    assert report.ok is True
    assert seen["module_id"] == "provider-anthropic"
    assert seen["config"] == {"priority": 1, "default_model": "claude-x", "api_key": "sk-x"}
    assert seen["model"] == "claude-x"
    assert seen["live_verify"] is False
    assert seen["strict"] is False


@pytest.mark.asyncio
async def test_provider_verification_failure_fails_closed_with_its_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that resolves in the plan but fails real verification
    (mount/credential/model) surfaces AS the preflight failure -- exactly
    the AC4 gap this closes: legible error + remediation, non-zero,
    no screen takeover (asserted at the CLI layer elsewhere)."""

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(providers=[{"module": "provider-anthropic", "config": {}}])

    async def fake_verify_provider(**kwargs: Any) -> ProviderVerification:  # noqa: ARG001
        return ProviderVerification(
            ok=False,
            error="provider 'provider-anthropic' is missing credentials: ANTHROPIC_API_KEY not set",
            remediation="run `amplifier-tui init` to configure a provider, or set the variable(s) named above",
        )

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    monkeypatch.setattr(preflight_mod, "verify_provider", fake_verify_provider)
    report = await run_preflight(None)

    assert report.ok is False
    assert (
        report.error
        == "provider 'provider-anthropic' is missing credentials: ANTHROPIC_API_KEY not set"
    )
    assert report.remediation is not None and "init" in report.remediation


@pytest.mark.asyncio
async def test_verify_provider_setting_false_skips_the_check_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``preflight.verify_provider: false`` is the escape hatch -- even a
    provider that would fail real verification is never asked."""

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(
            providers=[{"module": "provider-anthropic", "config": {}}],
            settings={"preflight": {"verify_provider": False}},
        )

    async def boom(**kwargs: Any) -> ProviderVerification:  # noqa: ARG001
        raise AssertionError("verify_provider must not run when disabled by settings")

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    monkeypatch.setattr(preflight_mod, "verify_provider", boom)
    report = await run_preflight(None)
    assert report.ok is True


@pytest.mark.asyncio
async def test_dry_run_live_verify_param_threads_to_verify_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_verify_provider(**kwargs: Any) -> ProviderVerification:
        seen.update(kwargs)
        return ProviderVerification(ok=True)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(providers=[{"module": "provider-anthropic", "config": {}}])

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    monkeypatch.setattr(preflight_mod, "verify_provider", fake_verify_provider)
    await run_preflight(None, verify_live=True)
    assert seen["live_verify"] is True
    assert seen["strict"] is False


@pytest.mark.asyncio
async def test_strict_preflight_forces_live_fail_closed_provider_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_verify_provider(**kwargs: Any) -> ProviderVerification:
        seen.update(kwargs)
        return ProviderVerification(ok=True)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(
            providers=[{"module": "provider-anthropic", "config": {}}],
            # Strict diagnostic truth cannot be disabled by the ordinary-
            # startup escape hatch.
            settings={"preflight": {"verify_provider": False}},
        )

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    monkeypatch.setattr(preflight_mod, "verify_provider", fake_verify_provider)
    await run_preflight(None, strict=True)
    assert seen["live_verify"] is True
    assert seen["strict"] is True


@pytest.mark.asyncio
async def test_verify_live_setting_also_opts_in_without_the_dry_run_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``preflight.verify_live: true`` has the same effect as ``--dry-run``
    on every launch, not only the CLI's own dry-run wiring."""
    seen: dict[str, Any] = {}

    async def fake_verify_provider(**kwargs: Any) -> ProviderVerification:
        seen.update(kwargs)
        return ProviderVerification(ok=True)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(
            providers=[{"module": "provider-anthropic", "config": {}}],
            settings={"preflight": {"verify_live": True}},
        )

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    monkeypatch.setattr(preflight_mod, "verify_provider", fake_verify_provider)
    await run_preflight(None)  # note: no verify_live kwarg passed by the caller
    assert seen["live_verify"] is True


def test_preflight_settings_defaults_and_junk_shapes() -> None:
    from amplifier_app_tui.kernel.preflight import _preflight_settings

    assert _preflight_settings({}) == (True, False)
    assert _preflight_settings({"preflight": "junk"}) == (True, False)
    assert _preflight_settings({"preflight": {"verify_provider": "nope"}}) == (True, False)
    assert _preflight_settings({"preflight": {"verify_provider": False}}) == (False, False)
    assert _preflight_settings({"preflight": {"verify_live": True}}) == (True, True)


# ---------------------------------------------------------------------------
# True end-to-end: a REAL fake provider module, actually imported and mounted
# ---------------------------------------------------------------------------


_REAL_FAKE_PROVIDER = '''
"""Fake provider module for a true end-to-end preflight test."""


class RealFakeProvider:
    name = "realfake"

    def __init__(self, config):
        self.config = dict(config or {})

    def get_info(self):
        from amplifier_core import ProviderInfo

        return ProviderInfo(id="realfake", display_name="Real Fake")

    async def list_models(self):
        return []

    async def complete(self, request=None, **kwargs):
        return {}

    def parse_tool_calls(self, response):
        return []


async def mount(coordinator, config=None):
    await coordinator.mount("providers", RealFakeProvider(config), name="realfake")
    return None
'''


@pytest.mark.asyncio
async def test_end_to_end_real_mount_through_run_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mocking of ``verify_provider`` at all: a real fake module gets
    really imported and really mounted by ``run_preflight`` itself."""
    package_name = "amplifier_module_provider_e2erealfake"
    package_dir = tmp_path / package_name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(textwrap.dedent(_REAL_FAKE_PROVIDER), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, package_name, raising=False)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(providers=[{"module": "provider-e2erealfake", "config": {}}])

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight(None)
    assert report.ok is True


@pytest.mark.asyncio
async def test_end_to_end_real_mount_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider module with no mount() at all -- resolved fine, but a
    real preflight run must still fail closed (not raise, not pass)."""
    package_name = "amplifier_module_provider_e2ebroken"
    package_dir = tmp_path / package_name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, package_name, raising=False)

    async def fake_resolve_config(bundle, **kwargs) -> ResolvedConfig:  # noqa: ANN001, ARG001
        return _resolved(providers=[{"module": "provider-e2ebroken", "config": {}}])

    _patch_resolve_config(monkeypatch, fake_resolve_config)
    report = await run_preflight(None)
    assert report.ok is False
    assert "no mount()" in (report.error or "")
    assert report.remediation is not None
