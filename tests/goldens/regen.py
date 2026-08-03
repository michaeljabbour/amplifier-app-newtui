"""Golden width-matrix fixtures for the transcript renderer.

Canonical block set: exactly one block of every kind in the
``TranscriptBlock`` union, populated with DemoRuntime's seed/script
strings (``kernel/demo.py``) so the goldens pin the mockup-verbatim
text, glyphs and theme tokens.

Each golden file ``transcript_w<width>.txt`` is the markup rendering
(``render_block_markup`` — text + ``$token`` style references) of every
canonical block at that width, in union order, separated by
``=== <kind> ===`` headers. Widths are the ADR-0007 matrix: 40/80/97/120.

Regenerate after an intentional renderer change:

    cd /Users/michaeljabbour/dev/amplifier-app-tui
    uv run python tests/goldens/regen.py

then review the diff — a golden change IS a rendering change.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from amplifier_app_tui.kernel.demo import (
    AUTO_BLOCK_CONTINUATION,
    AUTO_BLOCK_REASON,
    BRAINSTORM_IDEAS,
    DEMO_BANNER,
    DEMO_BUNDLE,
    DEMO_DEFERRED_DECISION,
    DEMO_EVIDENCE,
    DEMO_SESSION_ID,
    DEMO_TURN_BY_KEY,
    FORCE_PUSH_COMMAND,
    SEED_ANSWER,
    SEED_COMMANDS,
    SEED_NARRATION,
    SEED_PROMPT,
    SEED_TOOL_BODY,
    STORE_PLAN_TITLE,
    STORE_STEPS,
)
from amplifier_app_tui.model.blocks import (
    Answer,
    Blocked,
    BrainstormIdea,
    ContextBlock,
    DelegateEntry,
    DelegateSummaryBlock,
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
    SessionBanner,
    SteerEcho,
    Thinking,
    ToolLine,
    TodoItem,
    TranscriptBlock,
    TurnRule,
    UserLine,
    WorkingStatus,
)
from amplifier_app_tui.model.evidence import EvidenceLink
from amplifier_app_tui.model.turn import TurnTelemetry
from amplifier_app_tui.ui.live_tail import answer_spans
from amplifier_app_tui.ui.reducer import codemode_execute_block
from amplifier_app_tui.ui.transcript import render_block_markup

GOLDEN_DIR = Path(__file__).resolve().parent

WIDTHS: tuple[int, ...] = (40, 80, 97, 120)
"""ADR-0007 golden width matrix."""

_SEED = DEMO_TURN_BY_KEY["seed"]

# Answer source: the seed answer carries the mockup's selective emphasis
# (`amplifier` inline code + one bright-bold run) so the span splitter
# is exercised by the golden.
_ANSWER_SOURCE = SEED_ANSWER

_EVIDENCE_LINKS: tuple[EvidenceLink, ...] = tuple(
    EvidenceLink(claim_quote=claim.quote, tool_ref=claim.source) for claim in DEMO_EVIDENCE
)


def canonical_blocks() -> tuple[TranscriptBlock, ...]:
    """One block of every ``TranscriptBlock`` kind, in union order."""
    return (
        SessionBanner(id="g1", headline=DEMO_BANNER[0], detail=DEMO_BANNER[1]),
        UserLine(id="g2", text=SEED_PROMPT, mode="chat"),
        Narration(id="g3", text=SEED_NARRATION),
        ToolLine(
            id="g4",
            summary=f"Ran {len(SEED_COMMANDS)} shell commands",
            body=(SEED_TOOL_BODY,),
            status="completed",
        ),
        LiveCommand(id="g5", command=SEED_COMMANDS[0]),
        PlanBlock(
            id="g6",
            title=STORE_PLAN_TITLE,
            telemetry=TurnTelemetry(secs=3, tokens_down=1_400, cost=Decimal("0.07")),
            items=(
                PlanItem(text=STORE_STEPS[0], state="done"),
                PlanItem(text=STORE_STEPS[1], state="active"),
                PlanItem(text=STORE_STEPS[2], state="pending"),
            ),
        ),
        Blocked(
            id="g7",
            cmd=FORCE_PUSH_COMMAND,
            reason=AUTO_BLOCK_REASON,
            continuation=AUTO_BLOCK_CONTINUATION,
        ),
        WorkingStatus(
            id="g8",
            telemetry=TurnTelemetry(secs=8, tokens_down=3_200),
            agent_count=1,
        ),
        Recap(id="g9", goal="durable session store", next="open PR against main"),
        Thinking(
            id="g9t",
            text="The retry test is flaky under load.\nSwap the sleep for a deadline poll.",
            expanded=True,
        ),
        Answer(
            id="g10",
            spans=answer_spans(_ANSWER_SOURCE),
            evidence_refs=_EVIDENCE_LINKS,
        ),
        SteerEcho(id="g11", text="focus on the store tests first"),
        TurnRule(
            id="g12",
            checkpoint_id=_SEED.checkpoint_id,
            label=_SEED.rule_label,
            shipped=_SEED.shipped,
        ),
        EvidenceBlock(id="g13", links=_EVIDENCE_LINKS, selected=0),
        LedgerBlock(
            id="g14",
            session=DEMO_SESSION_ID[:6],
            bundle=DEMO_BUNDLE,
            turns=6,
            spend=Decimal("1.48"),
            shipped=3,
            answer_only=3,
            cache_hit_pct=88,
        ),
        ContextBlock(
            id="g15",
            used_pct=39,
            window_label="200k",
            segments=(("conversation", 4), ("tools", 2), ("memory", 1), ("free", 3)),
        ),
        NeedsYouBlock(
            id="g16",
            items=(
                NeedsYouEntry(
                    decision_id="d1",
                    question=DEMO_DEFERRED_DECISION.text,
                    reason="trust boundary",
                    choices=(
                        NeedsYouChoice(
                            label=DEMO_DEFERRED_DECISION.chip_label,
                            answer="push to fork",
                        ),
                    ),
                    highlight=DEMO_DEFERRED_DECISION.highlight,
                ),
            ),
        ),
        DoctorBlock(
            id="g17",
            headline="1 finding · nothing changed yet",
            healthy=("bundle anchors resolves", "provider OpenAI reachable"),
            findings=(
                DoctorFinding(
                    number=1,
                    text="uv run pytest denied 3× this session — consider a trust slot",
                ),
            ),
        ),
        ImproveBlock(
            id="g18",
            proposals=(
                ImproveProposal(
                    title="allowlist:",
                    action="uv run pytest",
                    rationale="approved 22/22 times · add to auto",
                ),
                ImproveProposal(
                    title="trust slot:",
                    rationale=(
                        "3 denials on push-to-fork all overridden · add fork remote to boundary"
                    ),
                ),
            ),
        ),
        BrainstormIdea(id="g19", text=BRAINSTORM_IDEAS[0][2:], number=1),
        DelegateSummaryBlock(
            id="g20",
            entries=(
                DelegateEntry(
                    agent="researcher", state="done", elapsed_s=4.4, snippet="3 findings"
                ),
                DelegateEntry(agent="coder", state="done", elapsed_s=6.0, snippet="2 files"),
                DelegateEntry(agent="tester", state="done", elapsed_s=2.6, snippet="tests ✔"),
            ),
            plan_final=(
                TodoItem(content=STORE_STEPS[0], status="completed"),
                TodoItem(content=STORE_STEPS[1], status="completed"),
                TodoItem(content=STORE_STEPS[2], status="completed"),
            ),
            duration_s=102.0,
        ),
    )


_CODEMODE_PROGRAM = (
    "totals = {}\n"
    "for path in tools.read.list_files({}):\n"
    '    totals[path] = len(tools.read.read_file({ "path": path }))\n'
    "return totals"
)
"""A model-authored Code Mode program: one confined pass, many bridged calls."""

_CALLOUT_ANSWER_SOURCE = (
    "Trimmed the retry wrapper.\n"
    "\n"
    "> ★ **Insight:** the caller already retries — the inner loop was "
    "belt-and-suspenders. Principle: one owner per concern."
)
"""An answer carrying an insight callout blockquote — the inline block
shape the hooks-inline-blocks module teaches the model to emit. Pins the
``▌`` gutter + hanging-indent wrap (the TUI-native callout rendering)."""


_POLISH_ANSWER_SOURCE = (
    "Rendering polish lands *italic* runs, real [docs](https://example.com/guide) "
    "links and bare https://amplifier.dev URLs as OSC 8 hyperlinks — this line is "
    "deliberately long so a wide terminal wraps it at the reading measure instead "
    "of stretching the paragraph across the whole width.\n"
    "\n"
    "- [x] italic, checkboxes, links\n"
    "- [ ] syntax highlighting (a non-goal)\n"
    "\n"
    "```python\n"
    "print('click a fence to copy it')\n"
    "```"
)
"""One answer exercising every inline rendering-polish item at once:
``*italic*``, ``- [x]``/``- [ ]`` task-list glyphs, a Markdown ``[text](url)``
link and a bare URL (both carry ``link=`` → OSC 8), a long prose line that
wraps at the 100-cell reading measure only where the width exceeds it (the
w120 golden), and a fenced code block (the click-to-copy target)."""


def variant_blocks() -> tuple[tuple[str, TranscriptBlock], ...]:
    """State variants of expandable kinds — same golden rigor, labeled headers."""
    collapsed = next(b for b in canonical_blocks() if b.kind == "delegate_summary")
    return (
        ("delegate_summary (expanded)", collapsed.model_copy(update={"expanded": True})),
        (
            # Same body as canonical g10 -- the ONLY diff is the AC2 start
            # marker, so this golden pins exactly what `final=True` adds.
            "answer (final)",
            Answer(
                id="g25",
                spans=answer_spans(_ANSWER_SOURCE),
                evidence_refs=_EVIDENCE_LINKS,
                final=True,
            ),
        ),
        (
            "answer (insight callout)",
            Answer(id="g21", spans=answer_spans(_CALLOUT_ANSWER_SOURCE)),
        ),
        (
            "answer (rendering polish)",
            Answer(id="g22", spans=answer_spans(_POLISH_ANSWER_SOURCE)),
        ),
        (
            "tool_line (code mode execute)",
            codemode_execute_block(
                {"code": _CODEMODE_PROGRAM},
                {
                    "output": '{\n  "a.py": 812,\n  "b.py": 344\n}',
                    "status": "completed",
                    "tool_calls": [
                        {"name": "read.list_files", "status": "completed"},
                        {"name": "read.read_file", "status": "completed"},
                        {"name": "write.write_file", "status": "error"},
                    ],
                },
                block_id="g23",
                tool_call_ids=("call-execute-1",),
                expanded=True,
            ),
        ),
        (
            "tool_line (code mode diagnostic)",
            codemode_execute_block(
                {"code": "import os\nreturn os.getcwd()"},
                {
                    "ok": False,
                    "error": True,
                    "diagnostic": {
                        "kind": "unsupported_syntax",
                        "message": "import is not available in code mode",
                        "suggestions": ["call the supplied tools instead of importing"],
                    },
                },
                block_id="g24",
                expanded=True,
            ),
        ),
    )


_LONG_TURN_PROMPT = (
    "Track down why the golden width matrix started drifting and add the anchor "
    "marker to the final answer everywhere it belongs."
)

_LONG_TURN_ANSWER = (
    "## Summary\n"
    "\n"
    "Six tool calls across two bursts, touching `reducer.py`, "
    "`transcript_render.py` and the golden fixtures. The regression traced "
    "back to `_finalize_response` never distinguishing a promoted answer "
    "from a merely-provisional one, so nothing marked the turn's real "
    "final-response start -- this line is deliberately long so a wide "
    "terminal wraps it at the reading measure instead of stretching the "
    "paragraph across the whole width, the same way a genuinely long turn's "
    "answer would.\n"
    "\n"
    "- [x] stamp `Answer.final` at both promotion sites and the demo path\n"
    "- [x] render the `● Final answer` start marker (label + weight, not color)\n"
    "- [ ] a fourth (light) theme — tracked separately, not this turn\n"
    "\n"
    "```python\n"
    "assert promoted.final and not provisional.final\n"
    "```"
)


def long_turn_blocks() -> tuple[TranscriptBlock, ...]:
    """AC5 regression fixture: a long turn, multiple tool calls, wrapped
    Markdown, rendered together (not in isolation) so their interaction at
    every golden width -- including the narrowest -- is pinned in one place.
    """
    return (
        UserLine(id="g26", text=_LONG_TURN_PROMPT, mode="build"),
        ToolLine(
            id="g27",
            summary="Read 3 files · searched 2× · ran 2 shell commands",
            body=(
                "read src/amplifier_app_tui/ui/reducer.py",
                "read src/amplifier_app_tui/ui/transcript_render.py",
                "read tests/test_golden_widths.py",
                "grep final_answer",
                "grep return_to_answer",
                "$ uv run pytest -q tests/test_ui_reducer_outcomes.py",
                "$ uv run ruff check .",
            ),
            expanded=True,
            status="completed",
        ),
        ToolLine(
            id="g28",
            summary="Wrote 1 file · edited 4 files",
            body=(
                "edit src/amplifier_app_tui/model/blocks.py",
                "edit src/amplifier_app_tui/ui/reducer.py",
                "edit src/amplifier_app_tui/ui/transcript_render.py",
                "edit tests/goldens/regen.py",
                "write tests/test_ui_reducer_outcomes.py",
            ),
            expanded=True,
            status="completed",
        ),
        Answer(
            id="g29",
            spans=answer_spans(_LONG_TURN_ANSWER),
            evidence_refs=_EVIDENCE_LINKS,
            final=True,
        ),
        TurnRule(id="g30", checkpoint_id="t-long-turn", label=_SEED.rule_label, shipped=True),
    )


def golden_text(width: int) -> str:
    """The full golden document for one width."""
    parts: list[str] = [f"# transcript renderer golden · width={width}", ""]
    for block in canonical_blocks():
        parts.append(f"=== {block.kind} ===")
        parts.append(render_block_markup(block, width))
        parts.append("")
    for label, block in variant_blocks():
        parts.append(f"=== {label} ===")
        parts.append(render_block_markup(block, width))
        parts.append("")
    parts.append("=== long turn (multi-tool regression, AC5) ===")
    for block in long_turn_blocks():
        parts.append(render_block_markup(block, width))
    parts.append("")
    return "\n".join(parts)


def golden_path(width: int) -> Path:
    return GOLDEN_DIR / f"transcript_w{width}.txt"


def main() -> None:
    for width in WIDTHS:
        path = golden_path(width)
        path.write_text(golden_text(width), encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
