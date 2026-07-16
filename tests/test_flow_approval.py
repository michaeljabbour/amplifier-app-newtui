"""Flow tests — DESIGN-SPEC §7: approvals & the needs-you queue.

End-to-end over DemoRuntime + Pilot: the inline approval bar (arrows /
enter / esc semantics, ``› `` selection, composer swap), deny →
``⊘ blocked · … · denied by user · continuing without …`` with the turn
continuing to its (denied) close-out, and the auto-mode deferred
decision → footer badge → ctrl-y Needs-you block → chip action logging
``Applying decision: …`` and clearing the badge.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import (
    APPROVAL_OPTIONS,
    AUTO_BLOCK_CONTINUATION,
    AUTO_BLOCK_REASON,
    DEMO_DEFERRED_DECISION,
    DENY_BLOCKED_CMD,
    FORCE_PUSH_COMMAND,
    PYTEST_APPROVAL_PROMPT,
    build_denied_spec,
)
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.app_support import APPROVAL_NOTICE
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import (
    footer_left_text,
    footer_right_text,
    footer_waiting_text,
)
from amplifier_app_newtui.ui.transcript import render_block

from .test_flow_helpers import SIZE, blocks_of, rules, seed_done, type_text, wait_for


async def _reach_pytest_approval(pilot, app: NewTuiApp) -> None:
    """Seed, then run the build turn up to its pytest approval."""
    await seed_done(pilot, app)
    await type_text(pilot, "hi")
    await pilot.press("enter")
    assert await wait_for(pilot, lambda: app.approval_bar is not None)


@pytest.mark.asyncio
async def test_approval_bar_replaces_composer_arrows_and_confirm() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _reach_pytest_approval(pilot, app)
        bar = app.approval_bar
        assert bar is not None
        assert bar.prompt == PYTEST_APPROVAL_PROMPT
        assert bar.options == APPROVAL_OPTIONS  # verbatim Allow once/always/Deny
        # The bar replaces the composer while open.
        assert app.composer.display is False
        assert app.notice_slot.current == APPROVAL_NOTICE
        assert app.footer_bar.state.context == "approval"
        assert (
            footer_right_text(app.footer_bar.state)
            == "arrows select · enter confirm · esc deny"
        )

        # Selected option prefixed "› "; arrows/tab cycle the selection.
        assert bar.option_texts() == ("› Allow once", "Allow always", "Deny")
        await pilot.press("right")
        assert bar.option_texts() == ("Allow once", "› Allow always", "Deny")
        await pilot.press("tab")
        assert bar.option_texts() == ("Allow once", "Allow always", "› Deny")
        # Shift+tab also cycles the selection (mockup: e.key === "Tab"
        # matches with or without shift) — it must NOT cycle the mode.
        mode_before = app.mode_id
        await pilot.press("shift+tab")
        assert bar.option_texts() == ("› Allow once", "Allow always", "Deny")
        assert app.mode_id == mode_before
        await pilot.press("right")
        assert bar.option_texts() == ("Allow once", "› Allow always", "Deny")
        await pilot.press("left")

        # Enter confirms → the composer comes back and the turn ships.
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert app.approval_bar is None
        assert app.composer.display is True
        rule = blocks_of(app, "turn_rule")[-1]
        assert rule.label.endswith("3 files · +142/−38 · tests ✔")
        # Footer ▲ yield glyph after a shipped turn (spec §10).
        state = app.footer_bar.state
        assert state.shipped
        assert " ▲" in footer_left_text(state)


@pytest.mark.asyncio
async def test_approval_keeps_keyboard_when_lanes_toggle_while_open() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _reach_pytest_approval(pilot, app)

        # ctrl+t may open the lanes panel (mockup fires it during an
        # approval) but the approval bar keeps the keyboard (spec §7)…
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.lanes_panel.display
        assert app.approval_bar is not None

        # …so Esc still resolves Deny (mockup: the approval branch runs
        # before the esc chain), not close-lanes.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert app.approval_bar is None
        blocked = blocks_of(app, "blocked")[-1]
        assert blocked.cmd == DENY_BLOCKED_CMD


@pytest.mark.asyncio
async def test_esc_denies_blocked_line_and_turn_continues() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _reach_pytest_approval(pilot, app)

        # Esc = Deny.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)

        # ⊘ blocked · <thing> · denied by user · continuing without <thing>.
        blocked = blocks_of(app, "blocked")[-1]
        assert blocked.cmd == DENY_BLOCKED_CMD
        assert blocked.reason == "denied by user"
        assert blocked.continuation == "continuing without test run"
        line = "".join(s.text for s in render_block(blocked, 200)[0])
        assert line == (
            "  ⊘ blocked · uv run pytest · denied by user · continuing without test run"
        )

        # The deny never halted the turn: the answer landed and the rule
        # closed out on the mockup's denied telemetry (no "tests ✔").
        assert any(
            "tests skipped by your denial" in "".join(s.text for s in b.spans)
            for b in blocks_of(app, "answer")
        )
        rule = blocks_of(app, "turn_rule")[-1]
        assert rule.label == build_denied_spec().rule_label
        assert "tests ✔" not in rule.label


@pytest.mark.asyncio
async def test_auto_mode_deferred_decision_ctrl_y_needs_you_flow() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        # Build turn first (approve), then the auto turn.
        await _reach_pytest_approval(pilot, app)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        await type_text(pilot, "hi")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 3 and not app.turn_active)
        assert app.mode_id == "auto"

        # Trust-boundary block rendered ⊘ but the run continued to a
        # shipped-locally outcome.
        blocked = blocks_of(app, "blocked")[-1]
        assert blocked.cmd == FORCE_PUSH_COMMAND
        assert blocked.reason == AUTO_BLOCK_REASON
        assert blocked.continuation == AUTO_BLOCK_CONTINUATION
        assert blocks_of(app, "turn_rule")[-1].shipped

        # Deferred decision → footer badge "1 decision waiting · ctrl-y".
        assert adapter.needs_you.pending_count == 1
        state = app.footer_bar.state
        assert state.waiting == 1
        assert footer_waiting_text(state) == "1 decision waiting · ctrl-y"

        # ctrl-y prints the orange Needs-you block with the chip.
        await pilot.press("ctrl+y")
        await pilot.pause()
        needs_you = blocks_of(app, "needs_you")[-1]
        assert len(needs_you.items) == 1
        entry = needs_you.items[0]
        assert entry.question == DEMO_DEFERRED_DECISION.text
        assert entry.choices[0].label == DEMO_DEFERRED_DECISION.chip_label
        header = "".join(s.text for s in render_block(needs_you, 200)[0])
        assert header == "· Needs you  1 deferred decision"

        # Acting on the decision logs "Applying decision: …" and clears
        # the badge; scrollback is append-only (mockup §7), so the
        # Needs-you listing stays in the transcript. The click handler is
        # per decision row (mockup html:286-292), never the header.
        await pilot.click(f"#needs-you-row-{entry.decision_id}")
        await pilot.pause()
        assert adapter.needs_you.pending_count == 0
        assert app.footer_bar.state.waiting == 0
        assert blocks_of(app, "needs_you") == [needs_you]
        # Spec §12: transcript clicks never strand the keyboard — the
        # composer keeps keyboard focus through the row/chip click.
        await pilot.press("z")
        assert app.composer.text == "z"
        # The applied decision is a narration line: bright "● " marker +
        # the verbatim mockup text (design-v3-cohesive.html:289).
        applied = [
            b for b in blocks_of(app, "narration")
            if b.text == DEMO_DEFERRED_DECISION.applied_narration
        ]
        assert len(applied) == 1
        line = "".join(s.text for s in render_block(applied[0], 200)[0])
        assert line == f"● {DEMO_DEFERRED_DECISION.applied_narration}"
