"""Headless boot smoke: the full app on the DemoRuntime (Pilot).

Covers the integrator contract: the app boots offline, registers the
spec themes, renders the session banner + seed transcript from the demo
event stream, and accepts typed input that starts a scripted turn.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import (
    BUILD_PROMPT,
    DEMO_BANNER,
    DEMO_TURN_BY_KEY,
    SEED_PROMPT,
)
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, theme_id


async def _wait_for(pilot, predicate, *, tries: int = 80) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


@pytest.mark.asyncio
async def test_demo_boot_banner_seed_and_typed_turn() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert app.theme == theme_id(DEFAULT_THEME)

        # Session banner rendered from the adapter's ready() callback.
        assert await _wait_for(
            pilot,
            lambda: any(b.kind == "session_banner" for b in app.transcript.blocks),
        )
        banner = next(b for b in app.transcript.blocks if b.kind == "session_banner")
        assert (banner.headline, banner.detail) == DEMO_BANNER

        # Seed turn replayed: user line + batch tool line + turn rule t1.
        assert await _wait_for(
            pilot,
            lambda: any(b.kind == "turn_rule" for b in app.transcript.blocks),
        )
        blocks = app.transcript.blocks
        user_lines = [b for b in blocks if b.kind == "user_line"]
        assert user_lines and user_lines[0].text == SEED_PROMPT
        assert any(
            b.kind == "tool_line" and b.summary == "Ran 2 shell commands" for b in blocks
        )
        rule = next(b for b in blocks if b.kind == "turn_rule")
        assert rule.checkpoint_id == "t1"
        assert app.ledger.turn_count == 1
        assert not app.turn_active

        # Type 'hi' + Enter → the next scripted demo turn (build) starts.
        await pilot.press("h", "i", "enter")
        assert await _wait_for(
            pilot,
            lambda: any(
                b.kind == "user_line" and b.text == BUILD_PROMPT
                for b in app.transcript.blocks
            ),
        )
        assert app.turn_active
        assert app.title_bar.running


@pytest.mark.asyncio
async def test_demo_build_turn_reaches_approval_bar() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        await _wait_for(
            pilot, lambda: any(b.kind == "turn_rule" for b in app.transcript.blocks)
        )
        await pilot.press("h", "i", "enter")
        # The scripted build turn stops at the pytest approval.
        assert await _wait_for(pilot, lambda: app.approval_bar is not None)
        bar = app.approval_bar
        assert bar is not None
        assert bar.options == ("Allow once", "Allow always", "Deny")
        # Confirm the default (Allow once) → the turn runs to its rule.
        await pilot.press("enter")
        assert await _wait_for(
            pilot,
            lambda: sum(b.kind == "turn_rule" for b in app.transcript.blocks) >= 2,
        )
        assert app.approval_bar is None
        assert app.ledger.turn_count == 2
        assert app.ledger.last_shipped


@pytest.mark.asyncio
async def test_demo_full_sequence_all_five_turns() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:

        def rules() -> int:
            return sum(b.kind == "turn_rule" for b in app.transcript.blocks)

        await _wait_for(pilot, lambda: rules() >= 1)  # seed
        for expected in (2, 3, 4, 5, 6):  # build, auto, plan, brainstorm, agents
            await pilot.press("h", "i", "enter")
            if expected == 2:  # build turn stops at the pytest approval
                assert await _wait_for(pilot, lambda: app.approval_bar is not None)
                await pilot.press("enter")
            assert await _wait_for(pilot, lambda: rules() >= expected), expected
        blocks = app.transcript.blocks
        # Auto turn: force-push blocked + decision deferred to the queue.
        assert any(b.kind == "blocked" for b in blocks)
        assert app.adapter.needs_you.pending_count == 1
        # Plan turn: read-only plan block landed.
        assert any(b.kind == "plan" and b.read_only for b in blocks)
        # Brainstorm turn: the four ideas.
        assert sum(b.kind == "brainstorm_idea" for b in blocks) == 4
        # Agents turn: three lanes spawned and completed.
        assert len(app.lanes.lanes) == 3
        assert all(record.lane.state == "done" for record in app.lanes.lanes)
        # Session cost tracks the mockup's cumulative chain.
        assert app.reducer.session_cost == DEMO_TURN_BY_KEY["agents"].cost_after
        # ctrl-y prints the needs-you block for the deferred push decision.
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert any(b.kind == "needs_you" for b in app.transcript.blocks)
