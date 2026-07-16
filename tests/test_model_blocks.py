"""Tests for the transcript block grammar (model/blocks.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from amplifier_app_newtui.model.blocks import (
    GLYPH_BLOCKED,
    GLYPH_PLAN_ACTIVE,
    GLYPH_PLAN_DONE,
    GLYPH_PLAN_PENDING,
    GLYPH_PROMPT,
    GLYPH_SPINNER_FRAMES,
    Answer,
    Blocked,
    BlockIdAllocator,
    BrainstormIdea,
    ContextBlock,
    DoctorBlock,
    DoctorFinding,
    EvidenceBlock,
    ImproveBlock,
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

_ADAPTER: TypeAdapter = TypeAdapter(TranscriptBlock)


def test_block_id_allocator_is_monotonic() -> None:
    ids = BlockIdAllocator()
    assert [ids.next_id() for _ in range(3)] == ["b1", "b2", "b3"]


def test_every_block_kind_has_stable_id_and_roundtrips() -> None:
    """Every block in the union carries id + kind and JSON round-trips."""
    telemetry = TurnTelemetry(secs=4.0, tokens_down=3200, cached_pct=80, cost=Decimal("0.12"))
    blocks: list[TranscriptBlock] = [
        SessionBanner(id="b1", headline="Amplifier 0.1.0 · core 1.6.0", detail="Bundle: dev"),
        UserLine(id="b2", text="fix the bug", mode="build"),
        Narration(id="b3", text="Reading the failing test"),
        ToolLine(id="b4", summary="Ran 2 shell commands", body=("$ pytest", "34 passed")),
        LiveCommand(id="b5", command="pytest -q"),
        PlanBlock(
            id="b6",
            title="Fix flaky retry test",
            telemetry=telemetry,
            items=(
                PlanItem(text="reproduce", state="done"),
                PlanItem(text="patch", state="active"),
                PlanItem(text="verify", state="pending"),
            ),
        ),
        Blocked(
            id="b7",
            cmd="git push",
            reason="denied by user",
            continuation="continuing without push",
        ),
        WorkingStatus(id="b8", telemetry=telemetry, agent_count=2),
        Recap(id="b9", goal="ship the fix", next="run full suite"),
        Answer(
            id="b10",
            spans=(
                Segment(text="Fixed in "),
                Segment(text="retry.py", style_token="teal"),
            ),
            evidence_refs=(EvidenceLink(claim_quote="34 passed", tool_ref="pytest run"),),
        ),
        SteerEcho(id="b11", text="also update the docs"),
        TurnRule(id="b12", checkpoint_id="t1", label="24s · 3.2k tok, 80% cached · $0.12 · answer"),
        EvidenceBlock(
            id="b13",
            links=(EvidenceLink(claim_quote="34 passed", tool_ref="pytest run"),),
        ),
        LedgerBlock(
            id="b14",
            session="a1b2c3",
            bundle="dev",
            turns=4,
            spend=Decimal("1.02"),
            shipped=2,
            answer_only=2,
            cache_hit_pct=74,
        ),
        ContextBlock(id="b15", used_pct=31, segments=(("conversation", 4), ("free", 6))),
        NeedsYouBlock(
            id="b16",
            items=(
                NeedsYouEntry(
                    decision_id="decision-1",
                    question="push to fork?",
                    choices=(NeedsYouChoice(label="yes · push to fork", answer="yes"),),
                ),
            ),
        ),
        DoctorBlock(id="b17", healthy=("provider ok",), findings=(DoctorFinding(number=1, text="no git remote"),)),
        ImproveBlock(id="b18"),
        BrainstormIdea(id="b19", text="event-sourced transcript", number=1),
    ]
    seen_kinds = set()
    for block in blocks:
        assert block.id
        assert block.kind
        seen_kinds.add(block.kind)
        dumped = block.model_dump_json()
        restored = _ADAPTER.validate_json(dumped)
        assert restored == block, f"{block.kind} did not round-trip"
    assert len(seen_kinds) == 19


def test_blocks_are_frozen() -> None:
    line = Narration(id="b1", text="hello")
    with pytest.raises(ValidationError):
        line.text = "changed"  # type: ignore[misc]


def test_kind_discriminates_union() -> None:
    restored = _ADAPTER.validate_python(
        {"id": "b9", "kind": "recap", "goal": "g", "next": "n"}
    )
    assert isinstance(restored, Recap)


def test_segment_uses_token_names_not_hex() -> None:
    segment = Segment(text="code", style_token="teal", bold=True)
    assert segment.style_token == "teal"
    with pytest.raises(ValidationError):
        Segment(text="bad", style_token="#6fc3c3")  # type: ignore[arg-type]


def test_plan_item_states_match_spec() -> None:
    for state in ("pending", "active", "done"):
        assert PlanItem(text="x", state=state).state == state  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PlanItem(text="x", state="completed")  # type: ignore[arg-type]


def test_spec_glyphs_exact() -> None:
    assert GLYPH_PROMPT == "❯"
    assert GLYPH_SPINNER_FRAMES == ("✳", "✦", "✧", "✦")
    assert (GLYPH_PLAN_DONE, GLYPH_PLAN_ACTIVE, GLYPH_PLAN_PENDING) == ("✔", "■", "□")
    assert GLYPH_BLOCKED == "⊘"


def test_tool_line_expand_toggle_via_copy() -> None:
    """Expansion is modeled as an immutable copy keyed by the stable id."""
    tool = ToolLine(id="b4", summary="Ran 1 shell command", body=("out",), status="completed")
    expanded = tool.model_copy(update={"expanded": True})
    assert expanded.id == tool.id
    assert expanded.expanded and not tool.expanded


def test_turn_rule_carries_checkpoint_id() -> None:
    rule = TurnRule(id="b12", checkpoint_id="t3", label="12s · 1.1k tok · $0.05 · answer")
    assert rule.checkpoint_id == "t3"
    assert not rule.shipped
