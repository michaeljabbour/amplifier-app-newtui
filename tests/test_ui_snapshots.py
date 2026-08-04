"""Whole-screen snapshots for interaction states with high visual risk."""

from __future__ import annotations

from pathlib import Path
import re
from time import monotonic

from amplifier_app_tui.kernel.demo import BRAINSTORM_PROMPT, BUILD_PROMPT
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.live_tail import LiveTail
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id
from textual._doc import take_svg_screenshot
from textual.app import App, ComposeResult

from .test_flow_helpers import GatedDemoAdapter, SIZE, seed_done, wait_for


_SNAPSHOT = (
    Path(__file__).parent
    / "__snapshots__"
    / "test_ui_snapshots"
    / "test_double_esc_rewind_snapshot.raw"
)
_PLAN_SNAPSHOT = (
    Path(__file__).parent
    / "__snapshots__"
    / "test_ui_snapshots"
    / "test_plan_panel_bottom_strip_snapshot.raw"
)
_DYNAMIC_TERMINAL_ID = re.compile(r"terminal-\d+")


def _clean_svg(value: str) -> str:
    """Remove Textual's per-process namespace and trailing whitespace."""
    stable_ids = _DYNAMIC_TERMINAL_ID.sub("terminal-SNAPSHOT", value)
    return "\n".join(line.rstrip() for line in stable_ids.splitlines()) + "\n"


def test_double_esc_rewind_snapshot(monkeypatch) -> None:
    """The stable post-interrupt rewind screen is regression-locked."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)

    async def interrupt_then_rewind(pilot) -> None:
        await seed_done(pilot, app)
        app.submit_prompt(BRAINSTORM_PROMPT)
        assert await wait_for(pilot, lambda: app.turn_active)
        await pilot.press("escape")
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        # The double-esc "backtrack to rewind" gesture (EscSequence in
        # ui/app_support.py) only fires when the second Esc lands within
        # keymap.ESC_BACKTRACK_WINDOW_SECONDS (0.75s) of the first one, a
        # real wall-clock window measured from time.monotonic(). The
        # `wait_for` above is the genuine precondition for a deterministic
        # screenshot (the interrupted turn must actually settle -- recap
        # rendered, checkpoints synced -- before the rewind picker opens on
        # top of it) but it is a polling loop, so its OWN wall-clock cost is
        # unbounded on a loaded CI runner and was silently eating into the
        # SAME 0.75s budget the second press below needs: under load the
        # settle-wait alone could exceed 0.75s, so the "backtrack" was
        # already expired by the time the second Esc arrived and the
        # picker never opened (issue: race between a load-sensitive test
        # poll and a fixed product timing constant, not a real regression).
        # Re-arm the interrupt to "now" immediately before the second press
        # so the backtrack window is always freshly open here regardless of
        # how long settling took to observe; the real ESC chain,
        # `consume_backtrack`, and `action_open_rewind` below still run
        # for real over the real key press.
        app.esc_sequence.arm_interrupt(monotonic())
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.rewind.display)

    actual = take_svg_screenshot(
        app=app,
        terminal_size=SIZE,
        run_before=interrupt_then_rewind,
    )
    expected = _SNAPSHOT.read_text(encoding="utf-8")
    assert expected == _clean_svg(expected), "snapshot must remain whitespace-clean"
    assert _clean_svg(actual) == expected


def test_plan_panel_bottom_strip_snapshot(monkeypatch) -> None:
    """Post-build-turn bottom strip: plan collapsed to 'Plan 3/3', still visible."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)

    async def run_build(pilot) -> None:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: app.plan_panel.display)
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        assert app.plan_panel.plan_lines == ("Plan 3/3",)

    actual = take_svg_screenshot(app=app, terminal_size=SIZE, run_before=run_build)
    expected = _PLAN_SNAPSHOT.read_text(encoding="utf-8")
    assert expected == _clean_svg(expected), "snapshot must remain whitespace-clean"
    assert _clean_svg(actual) == expected


_TAIL_SNAPSHOT = (
    Path(__file__).parent / "__snapshots__" / "test_ui_snapshots" / "test_lane_tail_snapshot.raw"
)


class _LaneTailShot(App[None]):
    """Minimal deterministic harness: LiveTail in lane mode, no timers."""

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)

    def on_mount(self) -> None:
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield LiveTail(id="live-tail")


def test_lane_tail_snapshot(monkeypatch) -> None:
    """The dim ┆-guttered lane tail rendering is regression-locked."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    app = _LaneTailShot()

    async def paint_tail(pilot) -> None:
        tail = app.query_one("#live-tail", LiveTail)
        tail.show_lane_tail(
            "…the queue bridge normalizes delegate lifecycle events at a single\n"
            "boundary, so the lanes are fed from the same UIEvent union as the\n"
            "transcript — checking trackers/task_status.py next"
        )
        await pilot.pause()

    actual = take_svg_screenshot(app=app, terminal_size=(90, 8), run_before=paint_tail)
    expected = _TAIL_SNAPSHOT.read_text(encoding="utf-8")
    assert expected == _clean_svg(expected), "snapshot must remain whitespace-clean"
    assert _clean_svg(actual) == expected
