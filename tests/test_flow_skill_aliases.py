"""Flow tests — skill aliases + the unknown-slash notice (story #1).

End-to-end over DemoRuntime + Pilot: discovered skills (and their
``shortcut:`` aliases) register as palette commands at boot, so
``/cosam`` invokes ``cranky-old-sam`` exactly like ``/skill`` would —
and a ``/``-prefixed input that matches NOTHING shows an error notice
instead of silently costing a provider turn.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.session_ops import SkillInfo
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter

from .test_flow_helpers import SIZE, blocks_of, seed_done, type_text, wait_for


class SkillfulDemoAdapter(DemoRuntimeAdapter):
    """Demo adapter that advertises one skill with a shortcut alias."""

    def __init__(self) -> None:
        super().__init__(instant=True)
        self.loaded: list[str] = []

    async def list_skills(self) -> tuple[SkillInfo, ...]:
        return (SkillInfo("cranky-old-sam", "crusty code review", shortcut="cosam"),)

    async def load_skill(self, name: str) -> tuple[bool, str]:
        self.loaded.append(name)
        return (True, f"# {name}\n\nbe crusty")


@pytest.mark.asyncio
async def test_unknown_slash_shows_notice_and_never_submits_a_turn() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        user_lines = len(blocks_of(app, "user_line"))

        await type_text(pilot, "/frobnicate now")
        await pilot.press("enter")
        assert await wait_for(
            pilot,
            lambda: app.notice_slot.current == "unknown command: /frobnicate · / lists commands",
        )
        # No chat turn: no user line appended, composer idle.
        assert len(blocks_of(app, "user_line")) == user_lines
        assert not app.turn_active


@pytest.mark.asyncio
async def test_skill_name_and_shortcut_register_and_show_in_palette() -> None:
    adapter = SkillfulDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert await wait_for(pilot, lambda: app._commands.get("/cosam") is not None)
        assert app._commands.get("/cranky-old-sam") is not None

        await type_text(pilot, "/cosam")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        assert [c.name for c in app.palette.filtered_commands] == ["/cosam"]
        alias = app.palette.filtered_commands[0]
        assert alias.tag == "skill" and "cranky-old-sam" in alias.desc


@pytest.mark.asyncio
async def test_shortcut_invokes_the_aliased_skill() -> None:
    adapter = SkillfulDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert await wait_for(pilot, lambda: app._commands.get("/cosam") is not None)

        await type_text(pilot, "/cosam")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: adapter.loaded == ["cranky-old-sam"])
        # Echoed as a user line, loaded through the /skill path (notice + block).
        assert await wait_for(
            pilot, lambda: app.notice_slot.current == "skill loaded · cranky-old-sam"
        )
        lines = blocks_of(app, "user_line")
        assert lines and lines[-1].text == "/cosam"
        assert not app.turn_active  # a skill load is not a provider turn


@pytest.mark.asyncio
async def test_skill_full_name_invokes_too() -> None:
    adapter = SkillfulDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert await wait_for(pilot, lambda: app._commands.get("/cranky-old-sam") is not None)

        await type_text(pilot, "/cranky-old-sam")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: adapter.loaded == ["cranky-old-sam"])
