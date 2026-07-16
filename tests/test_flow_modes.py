"""Flow tests — DESIGN-SPEC §4: modes & trust (shift+tab, notices, tint).

End-to-end over DemoRuntime + Pilot: the shift+tab cycle (ADR-0005 order
chat → build → plan → auto → brainstorm), the ``mode <id> · <trust>``
notice, the mode tint in exactly three places (composer badge, composer
left edge, footer), badge-click cycling, and the plan turn's read-only
block + plan→build handoff.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import PLAN_PROMPT, PLAN_RECAP
from amplifier_app_newtui.model.modes import MODE_PROFILES
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.composer import ModeBadge
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import footer_left_text
from amplifier_app_newtui.ui.transcript import render_block

from .test_flow_helpers import SIZE, blocks_of, line_texts, rules, seed_done, type_text, wait_for

MODE_CLASSES = ("mode-chat", "mode-plan", "mode-brainstorm", "mode-build", "mode-auto")


@pytest.mark.asyncio
async def test_shift_tab_cycles_modes_with_notice_and_three_place_tint() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert app.mode_id == "chat"
        # chat's composer edge uses the rule token (spec §4) via mode-chat.
        assert app.composer.has_class("mode-chat")

        # ADR-0005 shift+tab cycle: chat → build → plan → auto → brainstorm → chat.
        for expected in ("build", "plan", "auto", "brainstorm", "chat"):
            await pilot.press("shift+tab")
            await pilot.pause()
            profile = MODE_PROFILES[expected]
            assert app.mode_id == expected
            # Notice: mode <id> · <trust> (exact string).
            assert app.notice_slot.current == f"mode {expected} · {profile.trust_str}"
            # Tint place 1: composer left edge.
            mode_class = f"mode-{expected}"
            assert app.composer.has_class(mode_class)
            assert not any(app.composer.has_class(c) for c in MODE_CLASSES if c != mode_class)
            # Tint place 2: composer [mode] badge.
            assert app.query_one(ModeBadge).has_class(mode_class)
            # Tint place 3: footer "mode <id>" segment.
            state = app.footer_bar.state
            assert state.mode_id == expected
            assert footer_left_text(state).startswith(f"mode {expected} · {profile.trust_str}")


@pytest.mark.asyncio
async def test_mode_badge_click_cycles() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert app.mode_id == "chat"
        await pilot.click(ModeBadge)
        await pilot.pause()
        assert app.mode_id == "build"
        assert app.notice_slot.current == "mode build · auto read,test · ask write,net,spend"


@pytest.mark.asyncio
async def test_plan_turn_read_only_block_and_handoff_to_build() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(PLAN_PROMPT)
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)

        # The plan turn's mode notice switched the app posture to plan.
        assert app.mode_id == "plan"
        plan = blocks_of(app, "plan")[-1]
        assert plan.read_only
        header = "".join(s.text for s in render_block(plan, 200)[0])
        assert "(read-only)" in header

        # Recap: "Plan ready. shift+tab to build hands it over for execution."
        assert any(PLAN_RECAP in text for text in line_texts(app))
        # Rule labeled "· plan ready".
        rule = blocks_of(app, "turn_rule")[-1]
        assert rule.label.endswith("· plan ready")

        # Switching plan → build offers/executes the handoff.
        await type_text(pilot, "/mode build")
        await pilot.press("enter")
        await pilot.pause()
        assert app.mode_id == "build"
        assert app.notice_slot.current == "plan handed to build"
