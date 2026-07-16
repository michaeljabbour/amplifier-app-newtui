"""Flow tests — DESIGN-SPEC §10: ledger, evidence, context.

End-to-end over DemoRuntime + Pilot: ctrl-l prints the session ledger
scrollback block (exact strings), clicking a final answer prints its
evidence block (numbered teal claims → grounding tool calls), and
``/context`` prints the usage header + segmented bar.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from amplifier_app_newtui.kernel.demo import DEMO_EVIDENCE, DEMO_MEMORY_TOKENS
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.transcript import render_block

from .test_flow_helpers import SIZE, blocks_of, seed_done, type_text, wait_for


def _line(block, index: int, width: int = 200) -> str:
    return "".join(s.text for s in render_block(block, width)[index])


@pytest.mark.asyncio
async def test_ctrl_l_prints_session_ledger() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await pilot.press("ctrl+l")
        await pilot.pause()

        ledger = blocks_of(app, "ledger")[-1]
        assert (ledger.session, ledger.bundle) == ("e07d", "anchors")
        assert ledger.turns == 1
        # Mockup cmdLedger prints ``this.cost`` — the session cost shown in
        # the footer (seeded $0.57), not the recorded-turn sum.
        assert ledger.spend == Decimal("0.57")
        assert (ledger.shipped, ledger.answer_only) == (0, 1)
        assert ledger.cache_hit_pct == 91
        # Exact scrollback strings (spec §10).
        assert _line(ledger, 0) == "· Session ledger  e07d · anchors"
        assert _line(ledger, 1) == (
            "  1 turns · $0.57 · 0 shipped · 1 answer-only · cache hit 91%"
        )
        # Mockup cmdLedger ends with this exact notice.
        assert app.notice_slot.current == "ledger printed to scrollback"


@pytest.mark.asyncio
async def test_clicking_final_answer_prints_evidence_block() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        answer = next(
            b for b in blocks_of(app, "answer") if getattr(b, "evidence_refs", ())
        )
        widget = app.query_one(f"#block-{answer.id}")
        widget.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(f"#block-{answer.id}")
        assert await wait_for(pilot, lambda: blocks_of(app, "evidence"))

        evidence = blocks_of(app, "evidence")[-1]
        assert len(evidence.links) == len(DEMO_EVIDENCE) == 2
        # Exact header + first numbered claim (spec §10).
        assert _line(evidence, 0) == (
            "· Evidence  1/2 · ←/→ select · enter expand · esc close"
        )
        assert _line(evidence, 1) == (
            '  ¹ "dashboard and steering wheel"'
            " → Ran 2 shell commands (pyproject entry points)"
        )
        assert _line(evidence, 2).startswith('  ² "loads bundles" → ')
        # Mockup revealEvidence ends with this exact notice.
        assert app.notice_slot.current == (
            "evidence revealed · every claim traces to a tool call"
        )


@pytest.mark.asyncio
async def test_evidence_block_keys_select_expand_and_close() -> None:
    """Spec §10: the header's advertised keys actually work —
    ←/→ select (header 1/N tracks), enter expand, esc close."""
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        answer = next(
            b for b in blocks_of(app, "answer") if getattr(b, "evidence_refs", ())
        )
        widget = app.query_one(f"#block-{answer.id}")
        widget.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(f"#block-{answer.id}")
        assert await wait_for(pilot, lambda: blocks_of(app, "evidence"))

        evidence_id = blocks_of(app, "evidence")[-1].id
        # The block takes the keyboard so the advertised keys are live.
        assert app.focused is app.query_one(f"#block-{evidence_id}")

        # → selects the next claim; the header counter tracks (2/2).
        await pilot.press("right")
        evidence = blocks_of(app, "evidence")[-1]
        assert evidence.selected == 1
        assert _line(evidence, 0).startswith("· Evidence  2/2 · ")
        # ← selects back (clamped at the first claim).
        await pilot.press("left", "left")
        evidence = blocks_of(app, "evidence")[-1]
        assert evidence.selected == 0
        assert _line(evidence, 0).startswith("· Evidence  1/2 · ")

        # enter expands the selected claim: the demo links carry no
        # correlation key, so the grounding reference surfaces as notice.
        await pilot.press("enter")
        await pilot.pause()
        assert app.notice_slot.current == f"grounded by {DEMO_EVIDENCE[0].source}"

        # esc closes the block and hands the keyboard back.
        await pilot.press("escape")
        await pilot.pause()
        assert not blocks_of(app, "evidence")
        assert app.focused is not None
        assert app.composer in app.focused.ancestors


@pytest.mark.asyncio
async def test_context_command_prints_usage_grid_and_bar() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        # Seed turn usage: 83.9k output tokens + the 8k persistent cached
        # prefix (memory bucket) → 46% of the 200k window.
        assert app.reducer.total_tokens == 83_900
        assert app.reducer.memory_tokens == DEMO_MEMORY_TOKENS == 8_000

        await type_text(pilot, "/context")
        await pilot.press("enter")
        await pilot.pause()

        # Echoed as a user line first (palette-run semantics, spec §6).
        assert any(b.text == "/context" for b in blocks_of(app, "user_line"))
        context = blocks_of(app, "context")[-1]
        assert context.used_pct == 46
        assert context.window_label == "200k"
        assert _line(context, 0) == "· Context  46% of 200k"
        # ONE dim line: bar (█ used / ░ free) + legend together (mockup).
        assert len(render_block(context, 200)) == 2
        bar_line = _line(context, 1)
        assert "█" in bar_line and "░" in bar_line
        assert "conversation" in bar_line and "free" in bar_line
        # The memory bucket is populated in the demo, never ``memory 0``
        # (mockup cmdContext legend shows ``memory 8k``).
        assert "memory 8k" in bar_line
