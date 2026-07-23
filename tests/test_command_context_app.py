"""Behavioral tests for the REAL ``AppCommandContext`` (commands↔app boundary).

The 2026-07 test audit found the real adapter is never *driven*: no test
instantiates ``AppCommandContext`` (``grep "AppCommandContext(" tests/`` →
nothing) and ``test_command_context_contract.py`` is an inspect-only
surface check. These tests build the real adapter over a running
:class:`~amplifier_app_newtui.ui.app.NewTuiApp` (headless ``App.run_test``)
and drive it, so the delegation onto the composition root's public surface
is exercised end-to-end — data surfaces return the app's live objects and
actions land visible effects, with no widget object crossing the boundary.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.commands.context import ContextUsage
from amplifier_app_newtui.model.blocks import Answer, Segment
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.command_context import AppCommandContext
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.runtime_adapter import RuntimeAdapter
from amplifier_app_newtui.ui.themes import theme_id


async def _wait_for(pilot, predicate, *, tries: int = 80) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


async def _booted(pilot, app: NewTuiApp) -> None:
    """Pause until the demo seed turn has settled (stable base state)."""
    await _wait_for(
        pilot,
        lambda: any(b.kind == "turn_rule" for b in app.transcript.blocks)
        and not app.turn_active,
    )


@pytest.mark.asyncio
async def test_data_surfaces_delegate_to_the_composition_root() -> None:
    """Every data surface returns the app's own live object/value — the
    adapter is a thin delegating view, not a copy."""
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        await _booted(pilot, app)
        ctx = AppCommandContext(app)

        # Identity: the adapter hands back the app's live objects.
        assert ctx.ledger is app.ledger
        assert ctx.denial_log is adapter.denial_log
        assert ctx.steering is adapter.steering
        assert ctx.needs_you is adapter.needs_you

        # Scalars mirror the composition root.
        assert ctx.session_cost == app.reducer.session_cost
        assert ctx.session_short == adapter.session_short
        assert ctx.bundle_name == adapter.bundle_name

        # context_usage() is the app's clamped view; tallies are tuples.
        usage = ctx.context_usage()
        assert isinstance(usage, ContextUsage)
        assert usage.window > 0
        assert isinstance(ctx.approval_tallies(), tuple)
        assert isinstance(ctx.overridden_denials(), tuple)
        assert ctx.mcp_server_stats() == ()

        # next_block_id() draws from the app allocator (unique, monotone).
        ids = [ctx.next_block_id() for _ in range(3)]
        assert len(set(ids)) == 3


@pytest.mark.asyncio
async def test_echo_and_post_block_reach_the_transcript() -> None:
    """``echo_user_line`` and ``post_block`` land real blocks; the id is
    the one the app minted, proving no pre-built widget crossed over."""
    app = NewTuiApp(RuntimeAdapter())
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        ctx = AppCommandContext(app)

        ctx.echo_user_line("drive the real adapter")
        await pilot.pause()
        echoed = [b for b in app.transcript.blocks if b.kind == "user_line"]
        assert echoed and echoed[-1].text == "drive the real adapter"

        answer = Answer(
            id=ctx.next_block_id(),
            spans=(Segment(text="posted through the boundary"),),
        )
        ctx.post_block(answer)
        await pilot.pause()
        assert any(b.id == answer.id for b in app.transcript.blocks)


@pytest.mark.asyncio
async def test_show_notice_lands_on_the_notice_slot() -> None:
    app = NewTuiApp(RuntimeAdapter())
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        ctx = AppCommandContext(app)
        ctx.show_notice("boundary notice")
        await pilot.pause()
        assert app.notice_slot.current == "boundary notice"


@pytest.mark.asyncio
async def test_set_theme_switches_the_running_app_theme() -> None:
    app = NewTuiApp(RuntimeAdapter())
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        ctx = AppCommandContext(app)

        ctx.set_theme("carbon")
        await pilot.pause()
        assert app.theme == theme_id("carbon")

        # Unknown theme is rejected with a listing notice, theme unchanged.
        ctx.set_theme("chartreuse")
        await pilot.pause()
        assert app.theme == theme_id("carbon")
        assert "unknown theme" in (app.notice_slot.current or "")


@pytest.mark.asyncio
async def test_copy_answer_copies_the_last_real_answer() -> None:
    """``copy_answer`` extracts the newest clickable answer and hands it to
    the app clipboard, returning the char count; no answer → 0, no copy."""
    app = NewTuiApp(RuntimeAdapter())
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        ctx = AppCommandContext(app)

        copied: list[str] = []
        app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]

        # Nothing to copy yet on a bare session.
        assert ctx.copy_answer() == 0
        assert copied == []

        text = "the final answer text"
        ctx.post_block(Answer(id=ctx.next_block_id(), spans=(Segment(text=text),)))
        await pilot.pause()
        assert ctx.copy_answer() == len(text)
        assert copied == [text]


@pytest.mark.asyncio
async def test_about_info_reports_live_session_identity() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        await _booted(pilot, app)
        ctx = AppCommandContext(app)
        version, core_version, bundle, session = ctx.about_info()
        assert isinstance(version, str) and version
        assert isinstance(core_version, str)  # "" when core absent, never raises
        assert bundle == adapter.bundle_name
        assert session == adapter.session_short


@pytest.mark.asyncio
async def test_show_status_drives_a_status_block_through_the_worker() -> None:
    """A full round-trip: the adapter's ``show_status`` triggers the app
    worker, which asks the runtime and appends a status answer block."""
    app = NewTuiApp(RuntimeAdapter())
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        ctx = AppCommandContext(app)
        before = len(app.transcript.blocks)
        ctx.show_status()
        assert await _wait_for(pilot, lambda: len(app.transcript.blocks) > before)
        assert app.transcript.blocks[-1].kind == "answer"


# Pure-forwarding actions: (ctx method, args, app method it must invoke).
# Driving the real adapter over the real app and spying the target proves
# each forwards to the composition root with the exact arguments — the
# lines the contract test could only inspect statically.
_FORWARDING: tuple[tuple[str, tuple[object, ...], str], ...] = (
    ("cycle_mode", (), "action_cycle_mode"),
    ("set_mode", ("plan",), "set_mode_by_id"),
    ("toggle_lanes", (), "action_toggle_lanes"),
    ("open_rewind", (), "action_open_rewind"),
    ("open_permissions", (), "open_permissions"),
    ("manage_directories", ("add", "src"), "manage_directories"),
    ("quit_app", (), "exit"),
    ("show_modes", (), "show_native_modes"),
    ("set_native_mode", ("debug",), "activate_native_mode"),
    ("show_model", ("gpt",), "show_model"),
    ("apply_effort", ("high",), "apply_effort"),
    ("compact_context", ("focus",), "compact_context"),
    ("clear_context", (), "clear_context"),
    ("show_tools", (), "show_tools"),
    ("show_agents", (), "show_agents"),
    ("show_diff", ("staged",), "show_diff"),
    ("show_skills", (), "show_skills"),
    ("load_skill", ("brainstorming",), "load_skill"),
    ("manage_mcp", ("list",), "manage_mcp"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("ctx_method, args, app_method", _FORWARDING)
async def test_action_forwards_to_the_app(
    ctx_method: str, args: tuple[object, ...], app_method: str
) -> None:
    app = NewTuiApp(RuntimeAdapter())
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause(0.2)
        ctx = AppCommandContext(app)

        seen: list[tuple[object, ...]] = []
        # Session ops forward to the extracted SessionOpsController (#31);
        # everything else still forwards to the App composition root.
        target = app.session_ops if hasattr(type(app.session_ops), app_method) else app
        setattr(target, app_method, lambda *a, **k: seen.append(a))
        getattr(ctx, ctx_method)(*args)
        await pilot.pause()
        assert seen == [args]


@pytest.mark.asyncio
async def test_export_transcript_writes_under_the_cwd(tmp_path, monkeypatch) -> None:
    """``export_transcript`` returns the path it wrote (real file I/O)."""
    monkeypatch.chdir(tmp_path)
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        await _booted(pilot, app)
        ctx = AppCommandContext(app)
        path = ctx.export_transcript()
        assert (tmp_path / path).is_file()
        assert path.endswith(".md")
