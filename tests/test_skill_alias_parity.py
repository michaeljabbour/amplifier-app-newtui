"""CLI/TUI resolution parity over the ONE shared alias fixture (B2 AC1 + AC5).

"Parity tests use one shared alias fixture against CLI and TUI command
resolution" (AC5): this file drives the SAME fixture
(``tests/test_skill_alias_fixture.py``) through both execution paths
side by side and asserts they resolve identically (AC1):

- the "CLI" path: ``CommandRegistry.parse_and_run`` against a plain
  ``CommandContext`` fake — no Textual; the registry/commands layer
  alone (matches ``tests/test_commands_skills.py``'s style).
- the "TUI" path: the real ``TuiApp`` driven over Textual's Pilot
  (matches ``tests/test_flow_skill_aliases.py``'s style).

Both paths ultimately run through the exact same
``commands.skills.register_skill_commands`` / ``CommandRegistry.parse_and_run``
machinery — a single registry, no parallel lookup table (module docstring
of ``commands/skills.py``). This file is the evidence that parity holds,
not just an assertion that it should.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.commands.builtin import build_registry
from amplifier_app_tui.commands.skills import register_skill_commands
from amplifier_app_tui.kernel.session_ops import SkillInfo
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

from .test_flow_helpers import SIZE, seed_done, type_text, wait_for
from .test_skill_alias_fixture import SKILL_ALIAS_FIXTURE

_SPELLINGS = [
    ("/cranky-old-sam", "cranky-old-sam"),
    ("/cosam", "cranky-old-sam"),
    ("/release-notes", "release-notes"),
    ("/relnotes", "release-notes"),
]
"""Every canonical name and every alias in the shared fixture, paired
with the canonical skill it must resolve to — both surfaces are
parametrized over this SAME list."""


class _FixtureSkillsAdapter(DemoRuntimeAdapter):
    """The TUI-side half of the parity fixture: advertises the SAME
    ``SKILL_ALIAS_FIXTURE`` the CLI-side half registers directly."""

    def __init__(self) -> None:
        super().__init__(instant=True)
        self.loaded: list[str] = []

    async def list_skills(self) -> tuple[SkillInfo, ...]:
        return SKILL_ALIAS_FIXTURE

    async def load_skill(self, name: str) -> tuple[bool, str]:
        self.loaded.append(name)
        return (True, f"# {name}\n\nbe crusty")


@pytest.mark.parametrize(("spelling", "expected"), _SPELLINGS)
def test_cli_path_resolves_every_alias_to_its_canonical_skill(
    fake_command_context, spelling: str, expected: str
) -> None:
    """AC1 (CLI half): every spelling in the shared fixture resolves to
    its canonical skill via the bare registry — no Textual involved."""
    registry = build_registry()
    register_skill_commands(registry, SKILL_ALIAS_FIXTURE)
    assert registry.parse_and_run(fake_command_context, spelling)
    assert fake_command_context.calls == [f"load_skill:{expected}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("spelling", "expected"), _SPELLINGS)
async def test_tui_path_resolves_every_alias_to_its_canonical_skill(
    spelling: str, expected: str
) -> None:
    """AC1 (TUI half): the SAME fixture, driven through the real app
    over Textual's Pilot, resolves identically to the CLI half above."""
    adapter = _FixtureSkillsAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert await wait_for(pilot, lambda: app._commands.get(spelling) is not None)
        await type_text(pilot, spelling)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: adapter.loaded == [expected])


def test_cli_and_tui_tests_share_the_one_fixture_module() -> None:
    """AC5, made literal: this file, ``test_commands_skills.py``, and
    ``test_flow_skill_aliases.py`` all import their skill data from
    ``test_skill_alias_fixture`` — not three independently hand-rolled
    lookalikes that merely happen to agree today."""
    from . import test_commands_skills, test_flow_skill_aliases

    assert test_commands_skills.SKILL_ALIAS_FIXTURE is SKILL_ALIAS_FIXTURE
    assert test_flow_skill_aliases.SKILL_ALIAS_FIXTURE is SKILL_ALIAS_FIXTURE
