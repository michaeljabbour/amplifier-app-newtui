"""Flow tests — DESIGN-SPEC §6: the command palette.

End-to-end over DemoRuntime + Pilot: ``/`` opens the strip with group
headers (bare ``/`` only), live substring filtering, first row
highlighted, Enter running the top match (echoed as a user line first),
click running any row, and Esc closing the palette ahead of the
running-interrupt in the esc chain (§5 priority).
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import footer_right_text
from amplifier_app_newtui.ui.palette import _CommandRow, _GroupHeader

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    seed_done,
    type_text,
    wait_for,
)

ALL_COMMANDS = (
    "/mode",
    "/plan",
    "/brainstorm",
    "/context",
    "/tasks",
    "/ledger",
    "/rewind",
    "/permissions",
    "/doctor",
    "/improve",
)


@pytest.mark.asyncio
async def test_slash_opens_palette_with_group_headers_and_filters() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        await pilot.press("/")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        assert tuple(c.name for c in app.palette.filtered_commands) == ALL_COMMANDS
        # Group headers show only when the filter is exactly "/".
        assert await wait_for(
            pilot, lambda: len(list(app.palette.query(_CommandRow))) == len(ALL_COMMANDS)
        )
        headers = [h.group for h in app.palette.query(_GroupHeader)]
        assert headers == ["During", "Parallel", "Ship", "Between", "Repair"]
        # First row highlighted (bg-tab via -selected).
        rows = list(app.palette.query(_CommandRow))
        assert rows[0].has_class("-selected")
        assert app.palette.selected_command is not None
        assert app.palette.selected_command.name == "/mode"
        # Footer hints swap to the palette set.
        assert app.footer_bar.state.context == "palette"
        assert footer_right_text(app.footer_bar.state) == "↑↓ select · enter run · esc close"
        # Rows carry the right-aligned origin tag data (built-in / skill).
        assert {c.tag for c in app.palette.filtered_commands} == {"built-in", "skill"}

        # Live substring filter as you type: "/led" → only /ledger, no headers.
        await type_text(pilot, "led")
        assert await wait_for(
            pilot,
            lambda: tuple(c.name for c in app.palette.filtered_commands) == ("/ledger",),
        )
        assert await wait_for(pilot, lambda: not list(app.palette.query(_GroupHeader)))


@pytest.mark.asyncio
async def test_enter_runs_top_match_with_user_line_echo() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "/led")
        assert await wait_for(
            pilot,
            lambda: tuple(c.name for c in app.palette.filtered_commands) == ("/ledger",),
        )
        await pilot.press("enter")
        await pilot.pause()
        # Echoed as a user line first, then the ledger block printed.
        assert any(b.text == "/ledger" for b in blocks_of(app, "user_line"))
        assert blocks_of(app, "ledger")
        assert not app.palette.is_open
        assert app.composer.text == ""


@pytest.mark.asyncio
async def test_arrow_selection_and_click_runs_any_row() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await pilot.press("/")
        assert await wait_for(
            pilot, lambda: len(list(app.palette.query(_CommandRow))) == len(ALL_COMMANDS)
        )
        # ↓ moves the highlight to the second row.
        await pilot.press("down")
        await pilot.pause()
        assert app.palette.selected_command is not None
        assert app.palette.selected_command.name == "/plan"

        # Click any row runs it: row 3 = /context.
        await pilot.click("#palette-row-3")
        await pilot.pause()
        assert any(b.text == "/context" for b in blocks_of(app, "user_line"))
        assert blocks_of(app, "context")
        assert not app.palette.is_open


@pytest.mark.asyncio
async def test_esc_closes_palette_before_interrupting_running_turn() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "hi")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.turn_active)

        await pilot.press("/")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        # Esc chain priority (spec §5): palette closes first…
        await pilot.press("escape")
        await pilot.pause()
        assert not app.palette.is_open
        assert app.turn_active  # …the running turn was NOT interrupted
        assert app.composer.text == ""

        # …and only the next Esc reaches interrupt-running.
        await pilot.press("escape")
        await pilot.pause()
        assert app.notice_slot.current == "turn interrupted · context saved"
        adapter.release()  # let the parked script finish cleanly
