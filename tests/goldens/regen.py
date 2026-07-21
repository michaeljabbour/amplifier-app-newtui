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

    cd /Users/michaeljabbour/dev/amplifier-app-newtui
    uv run python tests/goldens/regen.py

then review the diff — a golden change IS a rendering change.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from amplifier_app_newtui.kernel.demo import (
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
from amplifier_app_newtui.model.blocks import (
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
    ToolLine,
    TodoItem,
    TranscriptBlock,
    TurnRule,
    UserLine,
    WorkingStatus,
)
from amplifier_app_newtui.model.evidence import EvidenceLink
from amplifier_app_newtui.model.turn import TurnTelemetry
from amplifier_app_newtui.ui.live_tail import answer_spans
from amplifier_app_newtui.ui.transcript import render_block_markup

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


def variant_blocks() -> tuple[tuple[str, TranscriptBlock], ...]:
    """State variants of expandable kinds — same golden rigor, labeled headers."""
    collapsed = next(b for b in canonical_blocks() if b.kind == "delegate_summary")
    return (("delegate_summary (expanded)", collapsed.model_copy(update={"expanded": True})),)


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
