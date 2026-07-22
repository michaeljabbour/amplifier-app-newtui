"""Headless boot smoke: the full app on the DemoRuntime (Pilot).

Covers the integrator contract: the app boots offline, registers the
spec themes, renders the session banner + seed transcript from the demo
event stream, and accepts typed input that starts a scripted turn.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from amplifier_app_newtui.kernel.demo import (
    DEMO_BANNER,
    DEMO_MODEL,
    DEMO_SESSION_COST_START,
    DEMO_TURN_BY_KEY,
    SEED_PROMPT,
)
from amplifier_app_newtui.kernel.events import (
    ContentBlockEnd,
    PromptComplete,
    PromptSubmit,
    ProviderResponseUsage,
    ToolPost,
    ToolPre,
)
from amplifier_app_newtui.ui import app_support
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.runtime_adapter import RuntimeAdapter
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, theme_id

from .test_flow_helpers import set_mode


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
        assert any(b.kind == "tool_line" and b.summary == "Ran 2 shell commands" for b in blocks)
        rule = next(b for b in blocks if b.kind == "turn_rule")
        assert rule.checkpoint_id == "t1"
        assert app.ledger.turn_count == 1
        assert not app.turn_active

        # The app boots in auto (§4 amendment); switch to chat so the
        # build turn parks at its pytest approval — a stable mid-turn
        # state for the running assertions below.
        await set_mode(pilot, app, "chat")

        # Type 'hi' + Enter → the next scripted demo turn (build) starts;
        # the user line echoes the typed text verbatim (mockup send()).
        await pilot.press("h", "i", "enter")
        assert await _wait_for(
            pilot,
            lambda: any(b.kind == "user_line" and b.text == "hi" for b in app.transcript.blocks),
        )
        assert app.turn_active
        assert app.title_bar.running
        await pilot.pause()
        assert app.title == app.title_bar.terminal_title_text()
        first_terminal_title = app.title
        app.title_bar.advance_spinner()
        await pilot.pause()
        assert app.title == app.title_bar.terminal_title_text()
        assert app.title != first_terminal_title


@pytest.mark.asyncio
async def test_demo_build_turn_reaches_approval_bar() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        await _wait_for(
            pilot,
            lambda: (
                any(b.kind == "turn_rule" for b in app.transcript.blocks) and not app.turn_active
            ),
        )
        # The pytest approval only asks in chat (spec §4); the app boots
        # in auto (§4 amendment), so put it in chat explicitly.
        await set_mode(pilot, app, "chat")
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

        await _wait_for(pilot, lambda: rules() >= 1 and not app.turn_active)  # seed
        # Start from chat (the app boots in auto — §4 amendment) so the
        # scripted sequence plays its full original path: the build turn
        # asks the chat-mode pytest approval, then each turn's mode
        # notice moves the posture along (auto → plan → brainstorm → build).
        await set_mode(pilot, app, "chat")
        for expected in (2, 3, 4, 5, 6):  # build, auto, plan, brainstorm, agents
            await pilot.press("h", "i", "enter")
            if expected == 2:  # build turn stops at the pytest approval
                assert await _wait_for(pilot, lambda: app.approval_bar is not None)
                await pilot.press("enter")
            assert await _wait_for(pilot, lambda expected=expected: rules() >= expected), expected
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


class _LateBaselineAdapter(DemoRuntimeAdapter):
    """Demo adapter that learns its resume cost baseline inside ``start()``.

    Mirrors ``RealRuntimeAdapter``: on resume, ``session_cost_start`` is
    unknown at construction (the app builds its reducer then) and is set
    only during the boot worker, before ``ready()``. The propagation must
    happen through the ``announce_ready`` handoff, like ``turn_base``.
    """

    def __init__(self) -> None:
        super().__init__(instant=True)
        # $0.40 restored spend (mockup mount $0.57 minus the seed's $0.17).
        self._resume_baseline = self.session_cost_start
        self.session_cost_start = Decimal("0")  # not yet known at __init__

    async def start(self, ready) -> None:  # noqa: ANN001
        self.session_cost_start = self._resume_baseline  # learned during boot
        await super().start(ready)


@pytest.mark.asyncio
async def test_resume_cost_baseline_set_in_adapter_start_reaches_reducer() -> None:
    """Resumed prior spend learned in ``start()`` lands in footer $ and
    checkpoint ``cost_at`` (spec §11: one session cost basis everywhere)."""
    adapter = _LateBaselineAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(
            pilot,
            lambda: (
                any(b.kind == "turn_rule" for b in app.transcript.blocks) and not app.turn_active
            ),
        )
        # Seed turn cost $0.17 on top of the $0.40 resumed baseline.
        assert app.reducer.session_cost == DEMO_SESSION_COST_START  # $0.57
        assert app.ledger.checkpoints[0].cost_at == DEMO_SESSION_COST_START
        assert app_support.footer_state(app).cost == DEMO_SESSION_COST_START


@pytest.mark.asyncio
async def test_real_turn_pulse_survives_the_bottom_ride() -> None:
    """The real-turn working pulse rides to the bottom under new content
    via remove + re-append. Textual prunes asynchronously, so the
    re-append must mint a fresh block id — reusing the removed id mounted
    a duplicate widget id, the reducer event died (swallowed by
    ``_consume_events``) and live turns lost the pulse and the digest."""
    sid = "live-root"
    adapter = RuntimeAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        adapter.queue.put_nowait(PromptSubmit(session_id=sid, prompt="hi"))
        adapter.queue.put_nowait(
            ToolPre(
                session_id=sid,
                tool_name="bash",
                tool_call_id="c1",
                tool_input={"command": "ls"},
            )
        )
        adapter.queue.put_nowait(
            ToolPost(
                session_id=sid,
                tool_name="bash",
                tool_call_id="c1",
                tool_input={"command": "ls"},
                result={"success": True},
            )
        )
        assert await _wait_for(
            pilot, lambda: any(b.kind == "tool_line" for b in app.transcript.blocks)
        )
        kinds = [b.kind for b in app.transcript.blocks]
        assert kinds[-1] == "working_status"  # the pulse rode below the digest
        adapter.queue.put_nowait(PromptComplete(session_id=sid, response="done"))
        assert await _wait_for(pilot, lambda: not app.turn_active)
        assert not any(b.kind == "working_status" for b in app.transcript.blocks)


@pytest.mark.asyncio
async def test_resume_replay_rebuilds_full_transcript_at_announce_ready() -> None:
    """An adapter exposing stored UIEvents gets the full-fidelity replay
    (tool digests + turn rules with checkpoints — DESIGN-SPEC §3/§11);
    the prose restored_history renders only as the fallback."""
    sid = "restored-root"
    adapter = RuntimeAdapter()
    adapter.turn_base = 1
    adapter.session_cost_start = Decimal("0.42")
    adapter.restored_history = (("user", "fix the bug"), ("assistant", "All done."))
    adapter.restored_events = (
        PromptSubmit(session_id=sid, ts=0.0, prompt="fix the bug"),
        ToolPre(
            session_id=sid,
            ts=1.0,
            tool_name="bash",
            tool_call_id="c1",
            tool_input={"command": "uv run pytest -q"},
        ),
        ToolPost(
            session_id=sid,
            ts=2.0,
            tool_name="bash",
            tool_call_id="c1",
            tool_input={"command": "uv run pytest -q"},
            result={"success": True},
        ),
        ContentBlockEnd(
            session_id=sid,
            ts=3.0,
            block_type="text",
            block={"type": "text", "text": "All done."},
        ),
        PromptComplete(session_id=sid, ts=4.0, response="All done."),
    )
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(
            pilot, lambda: any(b.kind == "turn_rule" for b in app.transcript.blocks)
        )
        kinds = [b.kind for b in app.transcript.blocks]
        assert "tool_line" in kinds  # digest, not the prose-only fallback
        assert kinds.count("user_line") == 1  # replay replaced prose, not doubled
        # The replayed turn's checkpoint chains with the restored history
        # (spec §9) and the kernel cost baseline stays the footer basis.
        assert [c.turn_id for c in app.ledger.checkpoints] == [1]
        assert app.reducer.session_cost == Decimal("0.42")
        assert not app.turn_active  # replay never arms turn timers/bells


def test_demo_adapter_advertises_demo_model() -> None:
    """Story #4: the demo session has a model identity for the footer too."""
    assert DemoRuntimeAdapter().model_name == DEMO_MODEL


@pytest.mark.asyncio
async def test_footer_state_carries_bare_model_name() -> None:
    """Story #4: the adapter's provider-qualified model id reaches the
    footer as the bare model name (``anthropic/x`` → ``x``)."""
    adapter = RuntimeAdapter()
    adapter.model_name = "anthropic/claude-fable-5"
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)):
        assert app_support.footer_state(app).model == "claude-fable-5"


@pytest.mark.asyncio
async def test_provider_usage_repaints_footer_before_prompt_complete() -> None:
    adapter = RuntimeAdapter()
    adapter.bundle_name = "newtui"
    adapter.session_short = "live01"
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        await adapter.queue.put(
            PromptSubmit(session_id="root", prompt="measure live spend", ts=1.0)
        )
        await adapter.queue.put(
            ProviderResponseUsage(
                session_id="root",
                output_tokens=1200,
                cost_usd=Decimal("0.42"),
                ts=2.0,
            )
        )
        assert await _wait_for(
            pilot,
            lambda: app.turn_active and app.footer_bar.state.cost == Decimal("0.42"),
        )
        assert app.reducer.session_cost == Decimal("0")
        await adapter.queue.put(PromptComplete(session_id="root", response="done", ts=3.0))
        assert await _wait_for(pilot, lambda: not app.turn_active)
