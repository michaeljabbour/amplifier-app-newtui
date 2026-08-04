"""Evidence links: grounding final-answer claims in tool calls.

DESIGN-SPEC §10: clicking a final answer prints an evidence block whose
numbered teal claims read ``¹ "quote" → <tool call that grounds it>``.

Compliance item D7 (evidence side panel) extends this with a richer,
provenance-linked detail view:

- :class:`ToolCallRecord` is the durable provenance record for one tool
  call, keyed by its correlation id (``tool_call_id``) — captured directly
  off the normalized event stream (``kernel/evidence.py``) at the moment
  the call completes, never inferred from wherever the transcript
  renderer later chooses to group/digest ``ToolLine`` blocks for display.
- :class:`EvidenceDetail` is the join of an :class:`EvidenceLink` (the
  claim) against its :class:`ToolCallRecord` (the provenance), with an
  explicit ``status``/``fallback`` for the cases where the join cannot
  produce something to show (AC5): the link never carried a correlation
  id (``unavailable``), the id no longer resolves to a record
  (``expired``), or the record resolves but its output exceeds the
  panel's size budget (``oversized`` — still shown, truncated).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class EvidenceLink(BaseModel):
    """One claim-to-tool grounding pair.

    ``claim_quote`` is the verbatim answer excerpt (rendered quoted, teal);
    ``tool_ref`` is a human-readable reference to the grounding tool call
    (e.g. ``pytest run · 34 passed``). ``tool_call_id`` optionally keeps
    the machine correlation key so evidence can deep-link to the ToolLine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_quote: str
    tool_ref: str
    tool_call_id: str = ""


MAX_EVIDENCE_OUTPUT_CHARS = 2_000
"""Side-panel output budget (D7 AC5 "oversized" threshold). Past this the
detail panel shows a truncated preview plus an explicit note pointing at
the full in-transcript expand (``enter`` on the correlated ToolLine, AC1)
instead of dumping an unbounded tool result into a small side panel."""


class ToolCallRecord(BaseModel):
    """Durable provenance for one tool call, keyed by ``tool_call_id``.

    Populated by ``kernel.evidence.EvidenceCollector`` directly off the
    normalized event stream (``ToolPost``) — the same correlation key
    :class:`EvidenceLink` carries, per its docstring. This is intentionally
    independent of however many ``ToolLine`` blocks the transcript groups
    calls into for display: provenance is persisted at the source, never
    inferred from display order (brief design note).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str
    tool_name: str
    tool_input: dict[str, Any]
    output: str = ""
    """Bounded human-readable rendering of the tool's result payload."""
    ts: float = 0.0
    """Unix epoch seconds when the tool call completed (event envelope)."""
    agent: str = ""
    """Originating agent label (AC2) — the session/lane that ran the call."""


EvidenceDetailStatus = Literal["ready", "unavailable", "expired", "oversized"]
"""AC5 fallback taxonomy:

- ``ready`` — the record resolved and fits the panel's size budget.
- ``unavailable`` — the claim carries no correlation id at all (nothing
  was ever linked — e.g. legacy/demo data predating tool-call tracking).
- ``expired`` — the claim carries a correlation id, but no provenance
  record resolves for it now (session restarted, history trimmed past
  it, or the record was never observed for another honest reason).
- ``oversized`` — the record resolved but its output exceeds the panel's
  character budget; content is still shown, truncated, with a note.
"""


class EvidenceDetail(BaseModel):
    """One evidence claim's supporting detail, ready for the side panel.

    AC2 fields: ``tool_name`` + ``input_summary`` identify the producing
    tool call and its inputs/query; ``timestamp`` and ``agent`` name when
    and who; ``output`` is the source/output body. AC5: ``status`` +
    ``fallback`` make unavailable/expired/oversized evidence an explicit,
    legible state rather than a dead or silently-empty panel.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EvidenceDetailStatus
    claim_quote: str
    tool_ref: str
    tool_call_id: str = ""
    tool_name: str = ""
    input_summary: str = ""
    output: str = ""
    output_truncated: bool = False
    timestamp: float = 0.0
    agent: str = ""
    fallback: str = ""
    """Explicit user-facing message when ``status != "ready"`` (AC5) —
    empty only for the fully-ready case."""


def format_evidence_timestamp(ts: float) -> str:
    """``YYYY-MM-DD HH:MM:SS`` local wall-clock, or ``""`` for an unset ts.

    A session-scoped detail view spans at most a few hours, but showing
    the date too costs nothing and stays honest across a midnight-crossing
    session or a resumed one (never guess — an absent timestamp renders
    as nothing, not a fabricated ``00:00:00``).
    """
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def build_evidence_detail(
    link: EvidenceLink,
    record: ToolCallRecord | None,
    *,
    max_output_chars: int = MAX_EVIDENCE_OUTPUT_CHARS,
) -> EvidenceDetail:
    """Join *link* (the claim) with *record* (its provenance), or produce
    an explicit AC5 fallback when the join cannot resolve.

    Never infers provenance from display order (brief design note): the
    only correlation used is ``link.tool_call_id`` against *record*,
    which the caller resolves from the durable provenance store
    (``EvidenceCollector.record_for`` / ``RuntimeAdapter.evidence_tool_call``)
    — never from wherever the transcript currently renders a ToolLine.
    """
    if not link.tool_call_id:
        return EvidenceDetail(
            status="unavailable",
            claim_quote=link.claim_quote,
            tool_ref=link.tool_ref,
            fallback="Evidence unavailable — this claim carries no tool-call reference.",
        )
    if record is None:
        return EvidenceDetail(
            status="expired",
            claim_quote=link.claim_quote,
            tool_ref=link.tool_ref,
            tool_call_id=link.tool_call_id,
            fallback="Evidence expired — the grounding tool call is no longer in this session.",
        )
    output = record.output
    truncated = len(output) > max_output_chars
    if truncated:
        output = output[:max_output_chars].rstrip() + "…"
    return EvidenceDetail(
        status="oversized" if truncated else "ready",
        claim_quote=link.claim_quote,
        tool_ref=link.tool_ref,
        tool_call_id=link.tool_call_id,
        tool_name=record.tool_name,
        input_summary=_input_summary(record.tool_input),
        output=output,
        output_truncated=truncated,
        timestamp=record.ts,
        agent=record.agent,
        fallback=(
            f"Output truncated to {max_output_chars:,} chars — "
            "press enter on the grounding tool line in the transcript for the full body."
            if truncated
            else ""
        ),
    )


_SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "prompt")
"""First present string input becomes the summary (mirrors kernel/evidence.py's
``_HINT_KEYS`` — kept as a separate, slightly broader tuple here since the
detail panel has room for one more fallback key (``prompt``) that the
compact ``tool_ref`` label does not."""


def _input_summary(tool_input: dict[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    if tool_input:
        return ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
    return ""


__all__ = [
    "MAX_EVIDENCE_OUTPUT_CHARS",
    "EvidenceDetail",
    "EvidenceDetailStatus",
    "EvidenceLink",
    "ToolCallRecord",
    "build_evidence_detail",
    "format_evidence_timestamp",
]
