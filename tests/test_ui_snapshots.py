"""Whole-screen snapshots for interaction states with high visual risk."""

from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.kernel.demo import BRAINSTORM_PROMPT
from amplifier_app_newtui.ui.app import NewTuiApp
from textual._doc import take_svg_screenshot

from .test_flow_helpers import GatedDemoAdapter, SIZE, seed_done, wait_for


_SNAPSHOT = (
    Path(__file__).parent
    / "__snapshots__"
    / "test_ui_snapshots"
    / "test_double_esc_rewind_snapshot.raw"
)


def _clean_svg(value: str) -> str:
    """Keep generated SVG reviewable by git without trailing whitespace."""
    return "\n".join(line.rstrip() for line in value.splitlines()) + "\n"


def test_double_esc_rewind_snapshot() -> None:
    """The stable post-interrupt rewind screen is regression-locked."""
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)

    async def interrupt_then_rewind(pilot) -> None:
        await seed_done(pilot, app)
        app.submit_prompt(BRAINSTORM_PROMPT)
        assert await wait_for(pilot, lambda: app.turn_active)
        await pilot.press("escape")
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
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
