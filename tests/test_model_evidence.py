"""model/evidence.py — ToolCallRecord, EvidenceDetail, build_evidence_detail.

Compliance item D7 AC2/AC5: the detail view identifies the producing tool
call, inputs, timestamp, source/output and originating agent (AC2), and
unavailable/expired/oversized evidence produces an explicit fallback
rather than a dead control (AC5). Pure dataclass/function tests — no
Textual, no kernel (ADR-0007 layering: model/ imports neither).
"""

from __future__ import annotations

from amplifier_app_tui.model.evidence import (
    EvidenceLink,
    ToolCallRecord,
    build_evidence_detail,
    format_evidence_timestamp,
)


def _record(**overrides: object) -> ToolCallRecord:
    base: dict[str, object] = dict(
        tool_call_id="c1",
        tool_name="bash",
        tool_input={"command": "uv run pytest -q"},
        output="41 passed in 3.2s",
        ts=1_700_000_000.0,
        agent="main agent",
    )
    base.update(overrides)
    return ToolCallRecord(**base)  # type: ignore[arg-type]


def test_ready_identifies_tool_input_timestamp_output_and_agent() -> None:
    """AC2: every required fact is present and correctly sourced."""
    link = EvidenceLink(
        claim_quote="41 tests pass", tool_ref="$ uv run pytest -q", tool_call_id="c1"
    )
    detail = build_evidence_detail(link, _record())
    assert detail.status == "ready"
    assert detail.tool_name == "bash"
    assert detail.input_summary == "uv run pytest -q"
    assert detail.timestamp == 1_700_000_000.0
    assert detail.agent == "main agent"
    assert detail.output == "41 passed in 3.2s"
    assert detail.fallback == ""
    # The claim itself always travels with its detail (never disconnected).
    assert detail.claim_quote == "41 tests pass"
    assert detail.tool_ref == "$ uv run pytest -q"


def test_unavailable_when_link_carries_no_correlation_id() -> None:
    """AC5: no tool_call_id at all -> 'unavailable', explicit fallback."""
    link = EvidenceLink(claim_quote="a claim", tool_ref="some tool")
    detail = build_evidence_detail(link, None)
    assert detail.status == "unavailable"
    assert detail.fallback  # non-empty, user-legible
    assert "unavailable" in detail.fallback.lower()
    assert detail.tool_name == ""
    assert detail.output == ""


def test_expired_when_correlation_id_present_but_record_missing() -> None:
    """AC5: had a link, but nothing resolves for it now -> 'expired'."""
    link = EvidenceLink(claim_quote="a claim", tool_ref="some tool", tool_call_id="gone")
    detail = build_evidence_detail(link, None)
    assert detail.status == "expired"
    assert detail.tool_call_id == "gone"
    assert detail.fallback
    assert "expired" in detail.fallback.lower()


def test_oversized_truncates_but_still_shows_partial_content() -> None:
    """AC5: oversized is NOT a dead end -- truncated content plus an
    explicit, actionable note, not a blank panel."""
    link = EvidenceLink(claim_quote="a claim", tool_ref="grep", tool_call_id="c1")
    huge_output = "x" * 5_000
    detail = build_evidence_detail(link, _record(output=huge_output), max_output_chars=2_000)
    assert detail.status == "oversized"
    assert detail.output_truncated is True
    assert len(detail.output) <= 2_001  # budget + ellipsis
    assert detail.output.endswith("…")
    assert detail.fallback
    assert "truncated" in detail.fallback.lower()
    # Identity facts still present -- truncation only bounds the output.
    assert detail.tool_name == "bash"
    assert detail.agent == "main agent"


def test_ready_stays_ready_at_exactly_the_budget() -> None:
    link = EvidenceLink(claim_quote="c", tool_ref="r", tool_call_id="c1")
    exact = "y" * 2_000
    detail = build_evidence_detail(link, _record(output=exact), max_output_chars=2_000)
    assert detail.status == "ready"
    assert detail.output == exact
    assert detail.output_truncated is False


def test_input_summary_falls_back_to_key_value_when_no_hint_key_present() -> None:
    link = EvidenceLink(claim_quote="c", tool_ref="r", tool_call_id="c1")
    detail = build_evidence_detail(link, _record(tool_input={"n": 3}))
    assert detail.input_summary == "n=3"


def test_input_summary_empty_for_no_input() -> None:
    link = EvidenceLink(claim_quote="c", tool_ref="r", tool_call_id="c1")
    detail = build_evidence_detail(link, _record(tool_input={}))
    assert detail.input_summary == ""


def test_format_evidence_timestamp_empty_for_unset() -> None:
    assert format_evidence_timestamp(0.0) == ""
    assert format_evidence_timestamp(-1.0) == ""


def test_format_evidence_timestamp_shape() -> None:
    formatted = format_evidence_timestamp(1_700_000_000.0)
    assert len(formatted) == len("2023-11-14 22:13:20")
    assert formatted[4] == "-" and formatted[7] == "-" and formatted[10] == " "
