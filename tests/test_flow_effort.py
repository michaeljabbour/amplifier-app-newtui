"""Flow test — HGT effort-tier cycle (ctrl+b) + footer indicator.

End-to-end over DemoRuntime + Pilot: the donor's ``variant.cycle`` mapped onto
amplifier's reasoning-effort tier. ctrl+b advances one tier in the canonical
ring (unset -> none -> minimal -> ... -> xhigh -> none) and the footer surfaces
an ``effort <tier>`` indicator only once the tier is set.

The binding-registration assert is the regression guard for the real bug the
forge probe caught: the keymap row + action existed and unit-passed, but
``ctrl+b`` was missing from ``_GLOBAL_ACTIONS`` so the chord was never bound.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.ui import app_support
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.footer import footer_left_text

from .test_flow_helpers import SIZE, seed_done


async def _settle(pilot) -> None:
    # The cycle runs on a worker; give it a few frames to finish.
    for _ in range(6):
        await pilot.pause()


@pytest.mark.asyncio
async def test_ctrl_e_is_bound_as_a_global_chord() -> None:
    # Regression guard: the chord must reach the app as a global priority
    # binding (the keymap row alone is not enough — it must be allow-listed).
    keys = [b.key for b in app_support.global_bindings()]
    assert "ctrl+b" in keys


@pytest.mark.asyncio
async def test_ctrl_e_cycles_effort_and_shows_footer_indicator() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        # Baseline: tier unset -> the footer omits the indicator entirely.
        assert app.current_effort is None
        assert "effort" not in footer_left_text(app.footer_bar.state)

        # The ring entry + advance mirrors the backend _next_effort exactly.
        for expected in ("none", "minimal", "low", "medium", "high", "xhigh", "none"):
            await pilot.press("ctrl+b")
            await _settle(pilot)
            assert app.current_effort == expected
            # The change notice matches what /effort shows for the same set.
            assert app.notice_slot.current == f"effort \u00b7 {expected}"
            # The footer indicator rides the left segment, before the cost.
            assert f" \u00b7 effort {expected} \u00b7 " in footer_left_text(app.footer_bar.state)


@pytest.mark.asyncio
async def test_ctrl_e_suppressed_while_an_approval_owns_the_keyboard() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        # Mount an approval (the bar owns the keyboard, spec §7).
        app.set_mode_by_id("chat")
        app.present_approval("t-1", "Run `pytest -q`?", ("Allow once", "Deny"))
        await pilot.pause()
        assert app.approval_bar is not None
        # ctrl+b must NOT cycle the tier while the approval is live.
        app.check_action  # sanity: method exists
        assert app.check_action("cycle_effort", ()) is False
