"""Flow tests — skill aliases + the unknown-slash notice (story #1 + B2).

End-to-end over DemoRuntime + Pilot: discovered skills (and their
``shortcut:`` aliases) register as palette commands at boot, so
``/cosam`` invokes ``cranky-old-sam`` exactly like ``/skill`` would —
and a ``/``-prefixed input that matches NOTHING shows an error notice
instead of silently costing a provider turn.

B2 compliance (2026-08-02 audit follow-up) extends this "TUI execution
path" half of the CLI/TUI parity story (see ``tests/test_skill_alias_parity.py``
for the side-by-side proof) with the SHARED fixture
(``tests/test_skill_alias_fixture.py``) plus coverage for: argument
forwarding (AC1), the canonical row naming its alias (AC2), nearby
suggestions on an unrecognized alias (AC3), and collision surfacing at
boot (AC4).
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel.session_ops import SkillInfo
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

from .test_flow_helpers import SIZE, blocks_of, seed_done, type_text, wait_for
from .test_skill_alias_fixture import COLLIDING_ALIAS_FIXTURE, SKILL_ALIAS_FIXTURE


class SkillfulDemoAdapter(DemoRuntimeAdapter):
    """Demo adapter that advertises the shared skill+alias fixture."""

    def __init__(self) -> None:
        super().__init__(instant=True)
        self.loaded: list[str] = []

    async def list_skills(self) -> tuple[SkillInfo, ...]:
        return SKILL_ALIAS_FIXTURE

    async def load_skill(self, name: str) -> tuple[bool, str]:
        self.loaded.append(name)
        return (True, f"# {name}\n\nbe crusty")


class CollidingSkillsDemoAdapter(DemoRuntimeAdapter):
    """Demo adapter whose discovered skills collide by design (AC4):
    two skills both declare the SAME ``shortcut: cosam``."""

    async def list_skills(self) -> tuple[SkillInfo, ...]:
        return COLLIDING_ALIAS_FIXTURE


def _answer_text(app: TuiApp) -> str:
    return "".join(seg.text for block in blocks_of(app, "answer") for seg in block.spans)


@pytest.mark.asyncio
async def test_unknown_slash_shows_notice_and_never_submits_a_turn() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
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
async def test_unknown_alias_gets_a_nearby_suggestion_notice() -> None:
    """AC3: a typo'd alias gets a "did you mean ...?" hint instead of a
    bare rejection — and never silently invokes a different command."""
    adapter = SkillfulDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert await wait_for(pilot, lambda: app._commands.get("/cosam") is not None)
        user_lines = len(blocks_of(app, "user_line"))

        await type_text(pilot, "/cosm")  # typo: missing the final "a"
        await pilot.press("enter")
        assert await wait_for(
            pilot,
            lambda: (
                app.notice_slot.current
                == "unknown command: /cosm · did you mean /cosam? · / lists commands"
            ),
        )
        assert len(blocks_of(app, "user_line")) == user_lines
        assert adapter.loaded == []  # never silently invoked the real skill
        assert not app.turn_active


@pytest.mark.asyncio
async def test_skill_name_and_shortcut_register_and_show_in_palette() -> None:
    adapter = SkillfulDemoAdapter()
    app = TuiApp(adapter)
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
async def test_canonical_row_names_its_alias_in_the_palette() -> None:
    """AC2: autocomplete/help show aliases alongside canonical names —
    both directions. The alias row already named its target; the
    canonical row now names its alias too."""
    app = TuiApp(SkillfulDemoAdapter())
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert await wait_for(pilot, lambda: app._commands.get("/cranky-old-sam") is not None)

        await type_text(pilot, "/cranky-old-sam")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        assert [c.name for c in app.palette.filtered_commands] == ["/cranky-old-sam"]
        canonical = app.palette.filtered_commands[0]
        assert "alias /cosam" in canonical.desc


@pytest.mark.asyncio
async def test_shortcut_invokes_the_aliased_skill() -> None:
    adapter = SkillfulDemoAdapter()
    app = TuiApp(adapter)
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
async def test_shortcut_forwards_trailing_arguments(monkeypatch) -> None:
    """AC1 (judgment call): text after the alias forwards to
    ``load_skill`` exactly like ``/skill <name> <rest>`` would."""
    adapter = SkillfulDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert await wait_for(pilot, lambda: app._commands.get("/cosam") is not None)

        await type_text(pilot, "/cosam draft the release notes")
        await pilot.press("enter")
        assert await wait_for(
            pilot, lambda: adapter.loaded == ["cranky-old-sam draft the release notes"]
        )


@pytest.mark.asyncio
async def test_skill_full_name_invokes_too() -> None:
    adapter = SkillfulDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert await wait_for(pilot, lambda: app._commands.get("/cranky-old-sam") is not None)

        await type_text(pilot, "/cranky-old-sam")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: adapter.loaded == ["cranky-old-sam"])


@pytest.mark.asyncio
async def test_alias_collision_is_surfaced_at_boot() -> None:
    """AC4: alias collisions are detected deterministically and
    surfaced at configuration load — a rich diagnostic listing in the
    transcript (never just a silent skip)."""
    app = TuiApp(CollidingSkillsDemoAdapter())
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        assert await wait_for(
            pilot, lambda: "already claimed by cranky-old-sam" in _answer_text(app)
        )
        text = _answer_text(app)
        assert "Skill aliases" in text
        assert "/cosam" in text
        assert "wanted by crusty-old-sam" in text
        # First registration still wins: the earlier skill's alias resolves.
        assert app._commands.get("/cosam") is not None
        assert app._commands.get("/crusty-old-sam") is not None  # canonical name is unique, kept
