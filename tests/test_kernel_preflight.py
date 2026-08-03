"""Unit tests for the pre-takeover mount/provider preflight (kernel/preflight.py, S4/AC4).

``run_preflight`` wraps ``resolve_config`` (the exact function the real boot
calls) and never goes further: these tests fake ``resolve_config`` so no real
bundle/network work happens, and prove the "no session creation" contract
directly (a ``prepared`` stand-in that fails the test if ``create_session``
is ever called).
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# success: reports what would mount
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ok_reports_bundle_provider_model_routing(monkeypatch: pytest.MonkeyPatch) -> None:
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
