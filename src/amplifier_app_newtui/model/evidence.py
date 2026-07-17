"""Evidence links: grounding final-answer claims in tool calls.

DESIGN-SPEC §10: clicking a final answer prints an evidence block whose
numbered teal claims read ``¹ "quote" → <tool call that grounds it>``.
"""

from __future__ import annotations

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


__all__ = ["EvidenceLink"]
