"""Tests for ui/palette.py — command palette strip (DESIGN-SPEC §6)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.ui.palette import (
    CMD_COL_MIN_WIDTH,
    PALETTE_GROUPS,
    PaletteStrip,
    command_row_cells,
    filter_commands,
    group_header_text,
    show_group_headers,
)
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id


@dataclass(frozen=True)
class Cmd:
    """Minimal CommandSpec-conforming record (the protocol contract)."""

    group: str
    name: str
    desc: str
    tag: str


# The mockup's command table, verbatim (DESIGN-SPEC §6 minimum set).
COMMANDS = (
    Cmd("During", "/mode", "cycle or jump posture: chat, plan, brainstorm, build, auto", "built-in"),
    Cmd("During", "/plan", "read-only planning; hands the plan to build", "built-in"),
    Cmd("During", "/brainstorm", "no tools, divergent output; /plan to converge", "built-in"),
    Cmd("During", "/context", "context usage grid + suggestions", "built-in"),
    Cmd("Parallel", "/tasks", "agent lanes: one line per subagent", "built-in"),
    Cmd("Ship", "/ledger", "session outcome ledger: spend vs yield", "built-in"),
    Cmd("Between", "/rewind", "fork from any turn-rule checkpoint", "built-in"),
    Cmd("Repair", "/permissions", "edit trust slots: boundary, blocks, exceptions", "built-in"),
    Cmd("Repair", "/doctor", "setup checkup; reports, then fixes on confirm", "skill"),
    Cmd("Repair", "/improve", "tune config from ledger + denial log", "skill"),
)


class PaletteHost(App[None]):
    """Minimal host app: registers spec themes, records palette messages."""

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.runs: list[str] = []
        self.closed = 0

    def compose(self) -> ComposeResult:
        yield PaletteStrip(COMMANDS)

    def on_palette_strip_command_run(self, message: PaletteStrip.CommandRun) -> None:
        self.runs.append(message.command.name)

    def on_palette_strip_closed(self, message: PaletteStrip.Closed) -> None:
        self.closed += 1


# -- pure helpers -------------------------------------------------------


def test_real_command_registry_satisfies_palette_protocol() -> None:
    """commands.builtin specs must render as palette rows unchanged."""
    from amplifier_app_newtui.commands.builtin import BUILTIN_COMMANDS
    from amplifier_app_newtui.ui.palette import CommandSpec as PaletteCommandSpec

    assert BUILTIN_COMMANDS
    for spec in BUILTIN_COMMANDS:
        assert isinstance(spec, PaletteCommandSpec)
        assert command_row_cells(spec) == (spec.name, spec.desc, spec.tag)
        assert spec.group in PALETTE_GROUPS


def test_filter_is_substring_on_command_name() -> None:
    assert [c.name for c in filter_commands(COMMANDS, "/")] == [c.name for c in COMMANDS]
    assert [c.name for c in filter_commands(COMMANDS, "/mo")] == ["/mode"]
    assert [c.name for c in filter_commands(COMMANDS, "/re")] == ["/rewind"]
    assert filter_commands(COMMANDS, "/nope") == ()


def test_group_headers_only_when_filter_is_exactly_slash() -> None:
    assert show_group_headers("/")
    assert not show_group_headers("/m")
    assert not show_group_headers("")


def test_row_cells_and_groups_match_spec() -> None:
    assert PALETTE_GROUPS == ("During", "Parallel", "Ship", "Between", "Repair")
    assert command_row_cells(COMMANDS[0]) == (
        "/mode",
        "cycle or jump posture: chat, plan, brainstorm, build, auto",
        "built-in",
    )
    assert group_header_text("During") == "DURING"
    # 150px at JetBrains Mono 12.5px (7.5px/cell) == 20 cells.
    assert CMD_COL_MIN_WIDTH == 20


# -- widget behavior ----------------------------------------------------


@pytest.mark.asyncio
async def test_open_on_slash_shows_group_headers_and_selects_first() -> None:
    app = PaletteHost()
    async with app.run_test() as pilot:
        strip = app.query_one(PaletteStrip)
        assert not strip.is_open
        strip.apply_filter("/")
        await pilot.pause()
        assert strip.is_open
        assert strip.selected_command is not None
        assert strip.selected_command.name == "/mode"
        # Group headers present, in mockup order, displayed uppercase.
        from amplifier_app_newtui.ui.palette import _GroupHeader  # test-only

        headers = [h.group for h in strip.query(_GroupHeader)]
        assert headers == list(PALETTE_GROUPS)


@pytest.mark.asyncio
async def test_narrow_filter_hides_group_headers() -> None:
    app = PaletteHost()
    async with app.run_test() as pilot:
        strip = app.query_one(PaletteStrip)
        strip.apply_filter("/do")
        await pilot.pause()
        from amplifier_app_newtui.ui.palette import _GroupHeader  # test-only

        assert [c.name for c in strip.filtered_commands] == ["/doctor"]
        assert list(strip.query(_GroupHeader)) == []


@pytest.mark.asyncio
async def test_zero_matches_hides_strip() -> None:
    app = PaletteHost()
    async with app.run_test() as pilot:
        strip = app.query_one(PaletteStrip)
        strip.apply_filter("/")
        await pilot.pause()
        assert strip.is_open
        strip.apply_filter("/zzz")
        await pilot.pause()
        assert not strip.is_open


@pytest.mark.asyncio
async def test_arrows_move_selection_and_enter_runs_selected() -> None:
    app = PaletteHost()
    async with app.run_test() as pilot:
        strip = app.query_one(PaletteStrip)
        strip.apply_filter("/")
        await pilot.pause()
        strip.focus()
        await pilot.pause()
        await pilot.press("down", "down")
        assert strip.selected_command is not None
        assert strip.selected_command.name == "/brainstorm"
        await pilot.press("up")
        assert strip.selected_command.name == "/plan"
        # Clamped at the top.
        await pilot.press("up", "up", "up")
        assert strip.selected_command.name == "/mode"
        await pilot.press("enter")
        await pilot.pause()
        assert app.runs == ["/mode"]


@pytest.mark.asyncio
async def test_selection_highlight_tracks_selected_row() -> None:
    app = PaletteHost()
    async with app.run_test() as pilot:
        strip = app.query_one(PaletteStrip)
        strip.apply_filter("/")
        await pilot.pause()
        from amplifier_app_newtui.ui.palette import _CommandRow  # test-only

        rows = list(strip.query(_CommandRow))
        assert rows[0].has_class("-selected")
        strip.move_selection(1)
        await pilot.pause()
        assert not rows[0].has_class("-selected")
        assert rows[1].has_class("-selected")


@pytest.mark.asyncio
async def test_click_runs_that_row() -> None:
    app = PaletteHost()
    async with app.run_test(size=(100, 40)) as pilot:
        strip = app.query_one(PaletteStrip)
        strip.apply_filter("/le")
        await pilot.pause()
        assert [c.name for c in strip.filtered_commands] == ["/ledger"]
        await pilot.click("#palette-row-0")
        await pilot.pause()
        assert app.runs == ["/ledger"]


@pytest.mark.asyncio
async def test_close_action_posts_closed_and_escape_is_not_bound_locally() -> None:
    # Esc is resolved by the app via keymap.ESC_CHAIN (spec §5) — the strip
    # has no local escape binding, so Esc bubbles even while it holds focus.
    app = PaletteHost()
    async with app.run_test() as pilot:
        strip = app.query_one(PaletteStrip)
        strip.apply_filter("/")
        await pilot.pause()
        strip.focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.closed == 0  # bubbled: no local handling
        strip.action_close()
        await pilot.pause()
        assert app.closed == 1
