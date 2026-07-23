"""Flow — the open command registry over the real app (story #2).

Any user-invocable ecosystem verb must be able to register as a slash
command at runtime: a mounted capability calls
``registry.register(spec, source=...)`` and — with no app-side refresh
call — the palette lists the row, dispatch resolves it exactly like a
built-in, and unregistering it restores the unknown-command notice.
Skills are the first rider of this mechanism (see
``test_flow_skill_aliases``); this flow drives it with a future-style
``recipe`` verb to prove no further registry or app change is needed.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.commands.registry import CommandContext, CommandSpec
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter

from .test_flow_helpers import SIZE, blocks_of, seed_done, type_text, wait_for


@pytest.mark.asyncio
async def test_runtime_registered_verb_shows_in_palette_and_dispatches() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        ran: list[str] = []

        def approve(ctx: CommandContext, args: str) -> None:
            del ctx
            ran.append(args)

        spec = CommandSpec(
            group="Parallel",
            name="/recipe-approve",
            desc="approve the pending recipe step",
            tag="recipe",
            handler=approve,
        )
        assert app._commands.register(spec, source="recipe")

        # Palette reflects the registration with no app-side refresh call.
        await type_text(pilot, "/recipe-approve")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        assert [c.name for c in app.palette.filtered_commands] == ["/recipe-approve"]
        assert app.palette.filtered_commands[0].tag == "recipe"

        # Dispatch resolves it like any built-in: echoed user line, no turn.
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: ran == [""])
        lines = blocks_of(app, "user_line")
        assert lines and lines[-1].text == "/recipe-approve"
        assert not app.turn_active


@pytest.mark.asyncio
async def test_unregistered_verb_is_unknown_again() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        spec = CommandSpec(
            group="Parallel",
            name="/pipeline-status",
            desc="pipeline run status",
            tag="pipeline",
            handler=lambda ctx, args: None,
        )
        app._commands.register(spec, source="pipeline")
        assert app._commands.unregister("/pipeline-status")

        await type_text(pilot, "/pipeline-status")
        assert not app.palette.filtered_commands  # palette dropped the row too
        await pilot.press("enter")
        assert await wait_for(
            pilot,
            lambda: (
                app.notice_slot.current == "unknown command: /pipeline-status · / lists commands"
            ),
        )
        assert not app.turn_active
