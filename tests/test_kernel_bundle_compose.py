"""In-session overlay composition (``kernel/bundle_compose.py``).

Duck-typed over the coordinator's ``loader.load`` seam — the same contract
``AmplifierSession.initialize()`` drives per module — so the mount logic tests
with a plain fake, no real session. Covers: additive sections mount, per-module
failure is best-effort (never aborts the overlay), non-composable sections are
reported (honest boundary), and cleanups are collected for teardown.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel.bundle_compose import (
    COMPOSABLE_SECTIONS,
    mount_overlay_modules,
)


class _FakeLoader:
    """A ModuleLoader stand-in: ``load`` returns an async mount fn.

    Records every mounted module id and hands back a cleanup callable so the
    caller can prove teardown handles are collected. ``fail`` names module ids
    whose mount raises (per-module failure path)."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.loaded: list[str] = []
        self.cleaned: list[str] = []
        self._fail = fail or set()

    def load(self, module_id, config=None, source_hint=None, coordinator=None):  # noqa: ANN001
        del config, source_hint, coordinator

        async def _mount(coord):  # noqa: ANN001
            del coord
            if module_id in self._fail:
                raise RuntimeError(f"boom: {module_id}")
            self.loaded.append(module_id)
            return lambda: self.cleaned.append(module_id)

        return _mount


class _FakeCoordinator:
    def __init__(self, loader: _FakeLoader | None) -> None:
        self.loader = loader


def _plan() -> dict:
    return {
        "tools": [{"module": "tool-x", "config": {"a": 1}}, {"module": "tool-y"}],
        "hooks": [{"module": "hook-z", "source": "git+..."}],
        "agents": [{"module": "agent-q"}],
        "providers": [{"module": "provider-anthropic"}],  # non-composable
    }


@pytest.mark.asyncio
async def test_mounts_additive_sections_only() -> None:
    loader = _FakeLoader()
    result = await mount_overlay_modules(_FakeCoordinator(loader), _plan())
    assert result.ok is True
    # tools + hooks + agents mount; the provider is NOT hot-swapped.
    assert set(result.mounted) == {"tool-x", "tool-y", "hook-z", "agent-q"}
    assert "provider-anthropic" not in result.mounted
    assert result.deferred_sections == ("providers",)
    assert result.skipped == ()
    # Every mounted module contributed a cleanup handle.
    assert len(result.cleanups) == 4


@pytest.mark.asyncio
async def test_composable_sections_constant_is_the_additive_set() -> None:
    assert COMPOSABLE_SECTIONS == ("tools", "hooks", "agents")


@pytest.mark.asyncio
async def test_per_module_failure_is_best_effort() -> None:
    loader = _FakeLoader(fail={"tool-y"})
    result = await mount_overlay_modules(_FakeCoordinator(loader), _plan())
    assert "tool-y" in result.skipped
    assert "tool-x" in result.mounted and "hook-z" in result.mounted
    assert result.ok is True  # at least one module mounted


@pytest.mark.asyncio
async def test_no_loader_skips_every_module_without_raising() -> None:
    result = await mount_overlay_modules(_FakeCoordinator(None), _plan())
    assert result.mounted == ()
    assert set(result.skipped) == {"tool-x", "tool-y", "hook-z", "agent-q"}
    assert result.ok is False


@pytest.mark.asyncio
async def test_empty_plan_is_ok_with_nothing_mounted() -> None:
    result = await mount_overlay_modules(_FakeCoordinator(_FakeLoader()), {})
    assert result.ok is True
    assert result.mounted == () and result.skipped == ()
    assert result.deferred_sections == ()


@pytest.mark.asyncio
async def test_summary_reports_mounted_and_deferred() -> None:
    result = await mount_overlay_modules(_FakeCoordinator(_FakeLoader()), _plan())
    summary = result.summary("heavy")
    assert "loaded" in summary and "heavy" in summary
    assert "4 module(s) mounted" in summary
    assert "providers attach at next session start" in summary


@pytest.mark.asyncio
async def test_junk_entries_are_dropped() -> None:
    plan = {"tools": ["notadict", {"no_module_key": 1}, {"module": "tool-ok"}]}
    result = await mount_overlay_modules(_FakeCoordinator(_FakeLoader()), plan)
    assert result.mounted == ("tool-ok",)
