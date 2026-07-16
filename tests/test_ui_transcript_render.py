"""Golden width-matrix tests for the pure transcript renderer.

Port of the test intents of amplifier-app-cli
``tests/test_transcript_golden_widths.py``: every block kind rendered at
widths 40/80/120, semantic must-contain markers, plus exact-string checks
for every glyph/label DESIGN-SPEC §3/§10/§11 quotes verbatim.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from rich.cells import cell_len
from rich.style import Style
from textual.content import Content

from amplifier_app_newtui.model.blocks import (
    Answer,
    Blocked,
    BrainstormIdea,
    ContextBlock,
    DoctorBlock,
    DoctorFinding,
    EvidenceBlock,
    ImproveBlock,
    ImproveProposal,
    LedgerBlock,
    LiveCommand,
    Narration,
    NeedsYouBlock,
    NeedsYouChoice,
    NeedsYouEntry,
    PlanBlock,
    PlanItem,
    Recap,
    Segment,
    SessionBanner,
    SteerEcho,
    ToolLine,
    TranscriptBlock,
    TurnRule,
    UserLine,
    WorkingStatus,
)
from amplifier_app_newtui.model.evidence import EvidenceLink
from amplifier_app_newtui.model.turn import TurnTelemetry
from amplifier_app_newtui.ui.segments import (
    line_plain,
    lines_markup,
    lines_plain,
    segment_style,
    to_rich_text,
)
from amplifier_app_newtui.ui.transcript import (
    TOOL_EXPAND_HINT,
    render_block,
    render_block_markup,
)

GOLDEN_WIDTHS = (40, 80, 120)

TEL = TurnTelemetry(secs=68, tokens_down=83_900, cached_pct=91, cost=Decimal("0.17"))
LIVE_TEL = TurnTelemetry(secs=8, tokens_down=3_200)


def _blocks() -> dict[str, TranscriptBlock]:
    return {
        "session_banner": SessionBanner(
            id="b1",
            headline="Amplifier 0.1.0 · core 1.6.0",
            detail="Bundle: dev | Provider: anthropic | claude-fable-5 · session a1b2c3",
        ),
        "user": UserLine(
            id="b2", text="Please verify the persistence boundary", mode="build"
        ),
        "narration": Narration(id="b3", text="Checking the durable session store"),
        "tool_collapsed": ToolLine(
            id="b4",
            summary="Ran 2 shell commands",
            body=("1214 passed", "build succeeded"),
            status="completed",
        ),
        "tool_expanded": ToolLine(
            id="b5",
            summary="Ran 2 shell commands",
            body=("1214 passed", "build succeeded"),
            expanded=True,
            status="completed",
        ),
        "tool_failed": ToolLine(
            id="b6", summary="Test suite failed", body=("1 failed",), status="failed"
        ),
        "live_command": LiveCommand(id="b7", command="uv run pytest tests -q"),
        "plan": PlanBlock(
            id="b8",
            title="Refactor session store",
            telemetry=TEL,
            items=(
                PlanItem(text="Audit persistence paths", state="done"),
                PlanItem(text="Migrate durable history", state="active"),
                PlanItem(text="Add reconciliation", state="pending"),
            ),
        ),
        "plan_read_only": PlanBlock(id="b9", title="Ship checklist", read_only=True),
        "blocked": Blocked(
            id="b10",
            cmd="git push --force origin main",
            reason="denied by user",
            continuation="continuing without push",
        ),
        "working": WorkingStatus(id="b11", telemetry=LIVE_TEL, agent_count=3),
        "recap": Recap(
            id="b12", goal="durable chat history", next="resume migration"
        ),
        "answer": Answer(
            id="b13",
            spans=(
                Segment(text="Run "),
                Segment(text="pytest", style_token="teal"),
                Segment(text=" — it is ", style_token="fg"),
                Segment(text="done", style_token="bright", bold=True),
                Segment(text=".\nSecond line.", style_token="fg"),
            ),
            evidence_refs=(
                EvidenceLink(claim_quote="it is done", tool_ref="pytest run"),
            ),
        ),
        "steer": SteerEcho(id="b14", text="focus on the tests"),
        "rule_shipped": TurnRule(
            id="b15",
            checkpoint_id="t1",
            label=f"{TEL.label()} · 3 files · +142/−38 · tests ✔",
            shipped=True,
        ),
        "rule_answer": TurnRule(
            id="b16", checkpoint_id="t2", label=f"{TEL.label()} · answer"
        ),
        "evidence": EvidenceBlock(
            id="b17",
            links=(
                EvidenceLink(
                    claim_quote="all tests pass", tool_ref="pytest run · 34 passed"
                ),
                EvidenceLink(claim_quote="3 files changed", tool_ref="git diff --stat"),
            ),
        ),
        "ledger": LedgerBlock(
            id="b18",
            session="a1b2c3",
            bundle="dev-bundle",
            turns=3,
            spend=Decimal("1.24"),
            shipped=2,
            answer_only=1,
            cache_hit_pct=91,
        ),
        "context": ContextBlock(
            id="b19",
            used_pct=42,
            segments=(
                ("conversation", 5),
                ("tools", 2),
                ("memory", 1),
                ("free", 2),
            ),
        ),
        "needs_you": NeedsYouBlock(
            id="b20",
            items=(
                NeedsYouEntry(
                    decision_id="d1",
                    question="push branch to fork?",
                    reason="net access denied",
                    choices=(NeedsYouChoice(label="yes · push to fork", answer="push"),),
                ),
            ),
        ),
        "doctor": DoctorBlock(
            id="b21",
            headline="1 finding · nothing changed yet",
            healthy=("provider mounted", "bundle resolves"),
            findings=(DoctorFinding(number=1, text="bundle override unused"),),
        ),
        "improve": ImproveBlock(
            id="b22",
            proposals=(
                ImproveProposal(
                    title="allowlist:",
                    action="uv run pytest",
                    rationale="approved 22/22 times · add to auto",
                ),
                ImproveProposal(
                    title="trust slot:",
                    rationale=(
                        "3 denials on push-to-fork all overridden"
                        " · add fork remote to boundary"
                    ),
                ),
            ),
        ),
        "brainstorm": BrainstormIdea(id="b23", text="event-sourced transcript", number=2),
    }


GOLDEN_MARKERS: dict[str, tuple[str, ...]] = {
    "session_banner": ("Amplifier 0.1.0", "core 1.6.0", "session a1b2c3"),
    "user": ("❯", "[build]", "persistence boundary"),
    "narration": ("●", "durable session store"),
    "tool_collapsed": ("●", "Ran 2 shell commands", "· click to expand"),
    "tool_expanded": ("●", "1214 passed", "build succeeded"),
    "tool_failed": ("●", "Test suite failed"),
    "live_command": ("└", "$ uv run pytest tests -q"),
    "plan": ("·", "Refactor session store", "✔", "■", "□", "↓ 83.9k tok"),
    "plan_read_only": ("(read-only)",),
    "blocked": ("⊘", "git push --force", "continuing without push"),
    "working": ("✳", "Coordinating 3 agents", "esc to interrupt"),
    "recap": ("✳", "Goal:", "Next:"),
    "answer": ("pytest", "done", "Second line."),
    "steer": ("↳", "steer queued:", "applies at next step boundary"),
    "rule_shipped": ("tests ✔", "$0.17", "91% cached"),
    "rule_answer": ("· answer",),
    "evidence": ("Evidence", "1/2", "¹", "²", "→", "esc close"),
    "ledger": ("Session ledger", "a1b2c3", "$1.24", "cache hit 91%"),
    "context": ("Context", "42% of 200k", "████████░░"),
    "needs_you": ("Needs you", "1 deferred decision", "[yes · push to fork]"),
    "doctor": ("Doctor", "✔", "provider mounted", "1 bundle override unused"),
    "improve": ("Improve", "allowlist:", "uv run pytest", "trust slot:"),
    "brainstorm": ("2 event-sourced transcript",),
}


@pytest.mark.parametrize("width", GOLDEN_WIDTHS)
@pytest.mark.parametrize("name", tuple(GOLDEN_MARKERS))
def test_block_golden_markers_at_width(name: str, width: int) -> None:
    rendered = lines_plain(render_block(_blocks()[name], width))
    normalized = " ".join(rendered.split())
    for marker in GOLDEN_MARKERS[name]:
        assert marker in normalized, (name, width, marker, rendered)


# -- exact spec strings (DESIGN-SPEC §3) --------------------------------------


def test_user_line_exact() -> None:
    lines = render_block(_blocks()["user"], 80)
    assert line_plain(lines[0]) == "❯ [build] Please verify the persistence boundary"
    prompt, badge, text = lines[0]
    assert (prompt.style_token, prompt.bold) == ("green", True)
    assert badge.style_token == "green"  # build mode badge is green
    assert text.style_token == "bright"


def test_user_line_mode_badge_colors() -> None:
    cases = {
        "chat": "dim",
        "plan": "blue",
        "brainstorm": "teal",
        "build": "green",
        "auto": "orange",
        "delegated": "teal",  # focused-subagent brief badge (mockup §8)
    }
    for mode, token in cases.items():
        line = render_block(UserLine(id="x", text="t", mode=mode), 80)[0]
        assert line[1].style_token == token, mode


def test_narration_exact() -> None:
    line = render_block(_blocks()["narration"], 80)[0]
    assert line_plain(line) == "● Checking the durable session store"
    assert line[0].style_token == "bright"
    assert line[1].style_token == "fg"


def test_tool_line_collapsed_exact() -> None:
    lines = render_block(_blocks()["tool_collapsed"], 80)
    assert lines_plain(lines) == "  ● Ran 2 shell commands · click to expand"
    assert lines[0][-1].style_token == "dimmer"
    assert TOOL_EXPAND_HINT == " · click to expand"


def test_tool_line_expanded_shows_indented_body_and_keeps_hint() -> None:
    # Mockup toolLine never mutates its head on toggle: the '· click to
    # expand' hint stays visible while the body is expanded.
    lines = render_block(_blocks()["tool_expanded"], 80)
    assert line_plain(lines[0]) == "  ● Ran 2 shell commands · click to expand"
    assert line_plain(lines[1]) == "      1214 passed"
    assert line_plain(lines[2]) == "      build succeeded"
    assert all(seg.style_token == "dimmer" for seg in lines[1])


def test_tool_line_failed_is_red() -> None:
    line = render_block(_blocks()["tool_failed"], 80)[0]
    assert line[0].style_token == "red"


def test_live_command_exact() -> None:
    line = render_block(_blocks()["live_command"], 80)[0]
    assert line_plain(line) == "  └ $ uv run pytest tests -q"
    assert line[0].style_token == "dimmer"
    assert line[1].style_token == "dim"


def test_plan_exact() -> None:
    lines = render_block(_blocks()["plan"], 80)
    # One space between the title and the telemetry paren (mockup: the
    # title segment carries the trailing space).
    assert line_plain(lines[0]) == f"· Refactor session store {TEL.suffix()}"
    assert lines[0][0].style_token == "orange"
    assert line_plain(lines[1]) == "  ✔ Audit persistence paths"
    assert lines[1][0].style_token == "green"
    assert line_plain(lines[2]) == "  ■ Migrate durable history"
    # Mockup L331: plain orange prefix — only the step text is bright bold.
    assert lines[2][0] == Segment(text="  ■ ", style_token="orange")
    assert lines[2][1].bold and lines[2][1].style_token == "bright"
    assert line_plain(lines[3]) == "  □ Add reconciliation"
    assert lines[3][0].style_token == "dimmer"


def test_plan_read_only_suffix() -> None:
    header = render_block(_blocks()["plan_read_only"], 80)[0]
    assert line_plain(header) == "· Ship checklist (read-only)"


def test_blocked_exact() -> None:
    line = render_block(_blocks()["blocked"], 80)[0]
    assert line_plain(line) == (
        "  ⊘ blocked · git push --force origin main"
        " · denied by user · continuing without push"
    )
    assert line[0].style_token == "red"
    assert line[-1].style_token == "dim"


def test_working_status_exact_and_spinner_frames() -> None:
    # Fan-out turn (mockup runAgentsTurn): 'Coordinating N agents · Ns ·
    # ↓ X.Xk tok · esc to interrupt' — integer secs, always one-decimal k.
    line = render_block(_blocks()["working"], 80)[0]
    assert line_plain(line) == (
        "✳ Coordinating 3 agents · 8s · ↓ 3.2k tok · esc to interrupt"
    )
    assert line[0].style_token == "orange"
    assert line[-1].style_token == "dimmer"
    for frame, glyph in enumerate(("✳", "✦", "✧", "✦", "✳")):
        block = _blocks()["working"].model_copy(update={"spinner_frame": frame})
        assert render_block(block, 80)[0][0].text == f"{glyph} "


def test_working_status_single_agent_exact() -> None:
    # Single-agent turns always show '· 1 agent ·' (mockup runTurn line).
    block = _blocks()["working"].model_copy(update={"agent_count": 1})
    line = render_block(block, 80)[0]
    assert line_plain(line) == (
        "✳ working · 8s · ↓ 3.2k tok · 1 agent"
        " · esc to interrupt · type to steer"
    )
    assert render_block(
        block.model_copy(update={"agent_count": 0}), 80
    ) == render_block(block, 80)


def test_recap_exact_italic_dim() -> None:
    line = render_block(_blocks()["recap"], 80)[0]
    assert line_plain(line) == "✳ Goal: durable chat history. Next: resume migration."
    assert line[0].style_token == "dimmer"
    assert line[1].italic and line[1].style_token == "dim"


def test_steer_echo_exact() -> None:
    line = render_block(_blocks()["steer"], 80)[0]
    assert line_plain(line) == (
        '  ↳ steer queued: "focus on the tests" · applies at next step boundary'
    )
    assert line[0].style_token == "teal"
    assert line[-1].style_token == "dimmer"


@pytest.mark.parametrize("width", GOLDEN_WIDTHS)
def test_turn_rule_fills_width_exactly(width: int) -> None:
    for name in ("rule_shipped", "rule_answer"):
        block = _blocks()[name]
        assert isinstance(block, TurnRule)
        lines = render_block(block, width)
        if len(lines) == 1:
            assert cell_len(line_plain(lines[0])) == width
            assert line_plain(lines[0]).endswith(block.label)
        else:  # narrow fallback: full rule line + right-aligned label line
            assert line_plain(lines[0]) == "─" * width
            assert line_plain(lines[1]).endswith(block.label)


def test_turn_rule_label_dim_when_shipped_dimmer_otherwise() -> None:
    shipped = render_block(_blocks()["rule_shipped"], 200)[0]
    answer = render_block(_blocks()["rule_answer"], 200)[0]
    assert shipped[-1].style_token == "dim"
    assert answer[-1].style_token == "dimmer"
    assert shipped[0].style_token == "rule"


def test_evidence_exact() -> None:
    lines = render_block(_blocks()["evidence"], 80)
    assert line_plain(lines[0]) == (
        "· Evidence  1/2 · ←/→ select · enter expand · esc close"
    )
    # Header counter + hints are ONE dimmer run (mockup showEvidence).
    assert lines[0][-1].style_token == "dimmer"
    assert line_plain(lines[1]) == '  ¹ "all tests pass" → pytest run · 34 passed'
    assert line_plain(lines[2]) == '  ² "3 files changed" → git diff --stat'
    # No background highlight on claims (mockup renders them plain).
    assert all(seg.bg_token is None for line in lines for seg in line)


def test_ledger_exact() -> None:
    lines = render_block(_blocks()["ledger"], 80)
    assert line_plain(lines[0]) == "· Session ledger  a1b2c3 · dev-bundle"
    # Header after the blue '· ' is one plain fg run; stats line is dim.
    assert lines[0][1].style_token == "fg" and not lines[0][1].bold
    assert line_plain(lines[1]) == (
        "  3 turns · $1.24 · 2 shipped · 1 answer-only · cache hit 91%"
    )
    assert lines[1][0].style_token == "dim"


def test_context_exact_bar() -> None:
    lines = render_block(_blocks()["context"], 80)
    assert line_plain(lines[0]) == "· Context  42% of 200k"
    assert lines[0][1].style_token == "fg" and not lines[0][1].bold
    # ONE dim line combining bar + legend (mockup cmdContext).
    assert len(lines) == 2
    assert line_plain(lines[1]) == "  ████████░░  conversation · tools · memory · free"
    assert all(seg.style_token == "dim" for seg in lines[1])


def test_needs_you_exact_chip_styling() -> None:
    lines = render_block(_blocks()["needs_you"], 80)
    # Header is one plain orange run, count never pluralized (mockup).
    assert line_plain(lines[0]) == "· Needs you  1 deferred decision"
    assert lines[0][1].style_token == "orange" and not lines[0][1].bold
    # Row number: '  1 ' orange, no period; two spaces before the chip.
    assert lines[1][0] == Segment(text="  1 ", style_token="orange")
    assert line_plain(lines[1]) == (
        "  1 push branch to fork? · net access denied  [yes · push to fork]"
    )
    chip = lines[1][-1]
    assert chip.text == "[yes · push to fork]"
    assert chip.style_token == "green" and chip.bg_token == "bg-tab"


def test_needs_you_highlight_renders_teal() -> None:
    block = NeedsYouBlock(
        id="x",
        items=(
            NeedsYouEntry(
                decision_id="d1",
                question="Push to fork mj/waypoint instead?",
                highlight="mj/waypoint",
            ),
        ),
    )
    row = render_block(block, 80)[1]
    assert line_plain(row) == "  1 Push to fork mj/waypoint instead?"
    accent = row[2]
    assert accent.text == "mj/waypoint" and accent.style_token == "teal"


def test_doctor_exact() -> None:
    lines = render_block(_blocks()["doctor"], 80)
    assert line_plain(lines[0]) == "· Doctor  1 finding · nothing changed yet"
    assert lines[0][0].style_token == "blue"
    assert lines[0][1].style_token == "fg"
    assert line_plain(lines[1]) == "  ✔ provider mounted"
    assert lines[1][0].style_token == "green"
    # Finding rows: orange number (no period) + dim text.
    assert line_plain(lines[3]) == "  1 bundle override unused"
    assert lines[3][0].style_token == "orange"
    assert lines[3][1].style_token == "dim"


def test_improve_exact() -> None:
    lines = render_block(_blocks()["improve"], 80)
    assert line_plain(lines[0]) == (
        "· Improve  from ledger + denial log · proposes, never applies silently"
    )
    assert lines[0][1].style_token == "fg"
    # Allowlist row: dim '  1 allowlist: ' + green action + dim tail.
    assert line_plain(lines[1]) == (
        "  1 allowlist: uv run pytest approved 22/22 times · add to auto"
    )
    assert lines[1][1] == Segment(text="uv run pytest", style_token="green")
    # Trust-slot row: one dim run, the action named exactly once.
    assert line_plain(lines[2]) == (
        "  2 trust slot: 3 denials on push-to-fork all overridden"
        " · add fork remote to boundary"
    )
    assert all(seg.style_token == "dim" for seg in lines[2])


def test_answer_splits_newlines_and_keeps_span_styles() -> None:
    lines = render_block(_blocks()["answer"], 80)
    assert len(lines) == 2
    assert line_plain(lines[0]) == "Run pytest — it is done."
    assert line_plain(lines[1]) == "Second line."
    code = lines[0][1]
    assert code.style_token == "teal" and code.text == "pytest"
    emphasis = lines[0][3]
    assert emphasis.style_token == "bright" and emphasis.bold


def test_session_banner_focus_note_replaces_headline() -> None:
    banner = SessionBanner(
        id="x",
        headline="Amplifier 0.1.0",
        focus_note=(
            "focused: test-writer · subagent of a1b2c3 · own context window"
            " · results report back to parent · esc back"
        ),
    )
    lines = render_block(banner, 80)
    assert len(lines) == 1
    assert line_plain(lines[0]).startswith("focused: test-writer · subagent of")
    # 'focused: <name> ' bright bold, the remainder dim (mockup focusLane).
    assert lines[0][0] == Segment(
        text="focused: test-writer ", style_token="bright", bold=True
    )
    assert lines[0][1].style_token == "dim"


# -- segments: markup + rich bridges ------------------------------------------


def test_segment_style_token_variables() -> None:
    assert segment_style(Segment(text="x")) == "$fg"
    assert segment_style(Segment(text="x", style_token="teal", bold=True)) == "bold $teal"
    assert (
        segment_style(
            Segment(text="x", style_token="green", bg_token="bg-tab", italic=True)
        )
        == "italic $green on $bg-tab"
    )


def test_markup_uses_theme_variables_and_escapes_brackets() -> None:
    markup = render_block_markup(_blocks()["user"], 80)
    assert "[bold $green]" in markup
    assert "#" not in markup  # never a color value
    # The literal "[build]" badge must be escaped, not parsed as markup.
    plain = Content.from_markup(markup).plain
    assert plain == "❯ [build] Please verify the persistence boundary"


@pytest.mark.parametrize("name", tuple(GOLDEN_MARKERS))
def test_markup_roundtrip_matches_plain(name: str) -> None:
    lines = render_block(_blocks()[name], 80)
    assert Content.from_markup(lines_markup(lines)).plain == lines_plain(lines)


def test_to_rich_text_resolves_tokens_from_mapping_only() -> None:
    variables = {"green": "cyan", "bright": "magenta", "dim": "yellow"}
    line = render_block(_blocks()["user"], 80)[0]
    text = to_rich_text(line, variables)
    assert text.plain == "❯ [build] Please verify the persistence boundary"
    first_style = text.spans[0].style
    assert isinstance(first_style, Style)
    assert first_style.color is not None
    assert first_style.color.name == "cyan"  # token resolved via mapping
    # Without a mapping, no colors at all.
    uncolored = to_rich_text(line)
    assert all(
        isinstance(span.style, Style) and span.style.color is None
        for span in uncolored.spans
    )
