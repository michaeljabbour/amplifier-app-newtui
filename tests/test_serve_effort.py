"""Offline unit tests for the ``serve`` reasoning-effort protocol ops.

Re-expresses opencode's ``variant.cycle`` (donor pointer
``packages/tui/src/component/dialog-variant.tsx``) as protocol access to
amplifier's orthogonal-to-model dimension -- the reasoning-effort tier --
through the ``serve`` stdio protocol. See ``.ai/oc_donor.md`` / ``.ai/oc_plan.md``.

Drives :func:`serve_loop` against a REAL ``RealRuntime`` on the fake-module
offline bundle from ``test_runtime_offline`` (real ``session_ops.get_effort`` /
``set_effort`` mutating the ``FakeLoop`` orchestrator's ``config`` dict) -- no
API key, no network -- plus a pure-function check of the cycle ring. Reuses the
``_PipeStdin`` / ``_Capture`` / ``_wait_until`` harness from
``test_serve_offline`` verbatim.
"""

from __future__ import annotations

import asyncio
from typing import IO, Any, cast

import pytest

from amplifier_app_tui.kernel.serve import _next_effort, serve_loop
from amplifier_app_tui.kernel.session_ops import EFFORT_LEVELS
from tests.test_runtime_offline import _started_runtime
from tests.test_serve_offline import _Capture, _PipeStdin, _wait_until


def _states(out: _Capture) -> list[dict[str, Any]]:
    """The ``effort.state`` records emitted so far, in wire order."""
    with out._lock:
        return [dict(r) for r in out.lines if r.get("type") == "effort.state"]


@pytest.mark.asyncio
async def test_serve_effort_get_set_roundtrip(offline_env) -> None:
    """effort.get/effort.set round-trip end-to-end over the real protocol:
    read the unset tier, set one, read it back, resolve the ``max`` alias, and
    reject an invalid level without mutating state."""
    runtime = await _started_runtime(offline_env["project"])
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))
    )

    async def step(op: dict[str, Any], n: int) -> dict[str, Any]:
        stdin.feed(op)
        await _wait_until(lambda: len(_states(out)) >= n)
        return _states(out)[n - 1]

    # get at session start: unset -> null, canonical ring advertised, pure read.
    s = await step({"op": "effort.get"}, 1)
    assert s["effort"] is None
    assert s["levels"] == list(EFFORT_LEVELS)
    assert "ok" not in s and "detail" not in s

    # set high -> ok + canonical detail + current echoes the change.
    s = await step({"op": "effort.set", "effort": "high"}, 2)
    assert s["ok"] is True
    assert s["detail"] == "high"
    assert s["effort"] == "high"

    # get echoes the persisted tier.
    s = await step({"op": "effort.get"}, 3)
    assert s["effort"] == "high"

    # the "max" alias normalizes to "xhigh" (app-cli parity).
    s = await step({"op": "effort.set", "effort": "max"}, 4)
    assert s["ok"] is True
    assert s["effort"] == "xhigh"

    # invalid level: ok:false, helpful detail, tier UNCHANGED (still xhigh).
    s = await step({"op": "effort.set", "effort": "bogus"}, 5)
    assert s["ok"] is False
    assert "must be one of" in s["detail"]
    assert s["effort"] == "xhigh"

    stdin.close()
    assert await server == 0


@pytest.mark.asyncio
async def test_serve_effort_cycle_walks_the_ring_and_wraps(offline_env) -> None:
    """effort.cycle re-expresses the donor's headline op: from unset it enters
    the ring at the first tier, advances one tier per op, and wraps
    ``xhigh`` -> ``none`` (no Default slot -- the host set_effort has no unset;
    documented in .ai/oc_donor.md)."""
    runtime = await _started_runtime(offline_env["project"])
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))
    )

    expected = ["none", "minimal", "low", "medium", "high", "xhigh", "none"]
    for i, want in enumerate(expected, start=1):
        stdin.feed({"op": "effort.cycle"})
        await _wait_until(lambda i=i: len(_states(out)) >= i)
        s = _states(out)[i - 1]
        assert s["effort"] == want, f"cycle {i}: expected {want}, got {s['effort']!r}"
        assert s["ok"] is True
        assert s["levels"] == list(EFFORT_LEVELS)

    stdin.close()
    assert await server == 0


def test_next_effort_ring_is_a_pure_wrapping_cycle() -> None:
    """The cycle order lives in ONE home. Unset/unknown enters at the first
    tier; a valid tier advances one and wraps at the end."""
    assert _next_effort(None) == "none"  # unset enters the ring
    assert _next_effort("bogus") == "none"  # unknown enters the ring
    assert _next_effort("none") == "minimal"
    assert _next_effort("high") == "xhigh"
    assert _next_effort("xhigh") == "none"  # wrap

    # A full walk from unset visits every tier exactly once, in order.
    cur: str | None = None
    seen: list[str] = []
    for _ in range(len(EFFORT_LEVELS)):
        cur = _next_effort(cur)
        seen.append(cur)
    assert seen == list(EFFORT_LEVELS)
