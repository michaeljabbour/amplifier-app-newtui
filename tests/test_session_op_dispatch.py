"""The typed ``SessionOp`` dispatch: one declaration site, wired everywhere.

Issue #30 collapses the ~14-op session passthrough ladder onto a single
typed :class:`SessionOp` registry plus ONE marshalling seam
(``RuntimeAdapter._run_op``). Before the collapse the same op was
re-declared as a neutral stub on the base adapter and a
``_runtime is None`` guard + thread-marshalling twin on
``RealRuntimeAdapter`` — two hand-written rungs that drifted independently.

This file is the guard that keeps the collapse honest: it derives its
surface from :data:`SESSION_OPS` (the one declaration site), so a new op
added to the app without an entry here — or a registry entry missing its
runtime backing — fails HERE, not in production. Op #15 cannot silently
miss a site.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from amplifier_app_newtui.kernel.runtime import RealRuntime
from amplifier_app_newtui.ui.runtime_adapter import (
    SESSION_OPS,
    RealRuntimeAdapter,
    RuntimeAdapter,
    SessionOp,
)

# The 14 canonical passthrough ops named in issue #30. The registry may
# ride extra ops on the same seam (native modes, workspace files), but it
# must never silently drop one of these.
CANONICAL_OPS: frozenset[str] = frozenset(
    {
        "interrupt",
        "list_models",
        "set_model",
        "get_effort",
        "set_effort",
        "compact",
        "clear_context",
        "status",
        "list_tools",
        "list_agents",
        "diff",
        "list_skills",
        "load_skill",
        "mcp_tools",
    }
)

# Ops the real adapter is allowed to redeclare on top of the shared seam
# because they carry an extra side effect (``set_model`` refreshes the
# footer's model copy after marshalling). Everything else MUST be inherited
# so the twin is provably gone.
SIDE_EFFECT_OVERRIDES: frozenset[str] = frozenset({"set_model"})

REGISTRY_NAMES: frozenset[str] = frozenset(op.name for op in SESSION_OPS)


def test_registry_is_the_single_declaration_site() -> None:
    names = [op.name for op in SESSION_OPS]
    assert len(names) == len(set(names)), "an op is declared twice in the registry"
    assert CANONICAL_OPS <= REGISTRY_NAMES, (
        f"registry dropped canonical ops: {sorted(CANONICAL_OPS - REGISTRY_NAMES)}"
    )


@pytest.mark.parametrize("op", SESSION_OPS, ids=lambda op: op.name)
def test_op_is_wired_at_every_back_site(op: SessionOp[object]) -> None:
    """Each registry op is reachable at all three back sites."""
    # Site 1 — the base adapter exposes it as a public async method.
    base = inspect.getattr_static(RuntimeAdapter, op.name, None)
    assert base is not None, f"base RuntimeAdapter is missing {op.name!r}"
    assert inspect.iscoroutinefunction(base), f"{op.name!r} must be async on the base"

    # Site 2 — the real adapter routes through the ONE overridden seam and
    # does NOT hand-declare a twin (except the sanctioned side-effect ops).
    if op.name not in SIDE_EFFECT_OVERRIDES:
        assert op.name not in vars(RealRuntimeAdapter), (
            f"{op.name!r} is still a hand-written twin on RealRuntimeAdapter"
        )

    # Site 3 — RealRuntime carries the same-named marshalling target.
    assert hasattr(RealRuntime, op.name), f"RealRuntime is missing {op.name!r}"


def test_real_adapter_declares_one_seam_not_a_twin_per_op() -> None:
    """The collapse's headline: the real adapter overrides ``_run_op`` and,
    apart from the sanctioned side-effect ops, redeclares no session op."""
    assert "_run_op" in vars(RealRuntimeAdapter)
    redeclared = REGISTRY_NAMES & set(vars(RealRuntimeAdapter))
    assert redeclared == SIDE_EFFECT_OVERRIDES


@pytest.mark.parametrize("op", SESSION_OPS, ids=lambda op: op.name)
def test_base_dispatch_returns_the_demo_default(op: SessionOp[object]) -> None:
    adapter = RuntimeAdapter()
    assert asyncio.run(adapter._run_op(op)) == op.demo


@pytest.mark.parametrize("op", SESSION_OPS, ids=lambda op: op.name)
def test_real_dispatch_returns_the_starting_default_before_boot(
    op: SessionOp[object],
) -> None:
    adapter = RealRuntimeAdapter(bundle="x")  # never started: _runtime is None
    assert asyncio.run(adapter._run_op(op)) == op.starting
