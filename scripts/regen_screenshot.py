"""Regenerate the README screenshot (docs/images/demo-session.svg).

Boots the real app headlessly on the offline DemoRuntime — the same
``NewTuiApp`` + ``DemoRuntimeAdapter`` pairing the flow tests use, driven
through Textual's Pilot. The scripted seed turn replays on boot, then the
build prompt is typed so a second turn (tool digests, recap, answer,
shipped rule, footer cost) completes before the SVG screenshot is saved.
The demo runtime is deterministic (instant virtual clock), so the output
is reproducible.

Usage:
    uv run python scripts/regen_screenshot.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from amplifier_app_newtui.kernel.demo import BUILD_PROMPT
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "images"
OUT_NAME = "demo-session.svg"
SIZE = (100, 30)


async def _wait_for(pilot, predicate: Callable[[], object], *, tries: int = 200) -> None:
    """Poll *predicate* while letting the app process events (test pattern)."""
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(0.05)
    raise TimeoutError("app never reached the expected state")


async def main() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:

        def rules() -> int:
            return sum(b.kind == "turn_rule" for b in app.transcript.blocks)

        # Seed turn replays on boot: user line, tool digest, answer, rule t1.
        await _wait_for(pilot, lambda: rules() >= 1 and not app.turn_active)

        # Type the scripted build prompt verbatim. The app boots in auto
        # mode, so the build turn runs end-to-end (the pytest approval
        # only asks in chat mode) and cuts its shipped rule t2.
        await pilot.press(*("space" if ch == " " else ch for ch in BUILD_PROMPT))
        await pilot.press("enter")
        await _wait_for(pilot, lambda: rules() >= 2 and not app.turn_active)

        await pilot.pause()  # let the final frame settle
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        saved = app.save_screenshot(OUT_NAME, path=str(OUT_DIR))
        print(f"saved: {saved}")


if __name__ == "__main__":
    asyncio.run(main())
