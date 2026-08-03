"""Evidence links for real sessions (DESIGN-SPEC §10, ADR-0007 resolution 9).

The demo script ships hand-authored claims; a real session derives them
from the same normalized UIEvent stream that ui-events.jsonl records
(ADR-0007: the event log "powers … evidence links"). The collector taps
the queue bridge, keeps the running turn's completed top-level tool
calls, and when ``PromptComplete`` identifies the production final answer
it pairs the answer's leading sentences (verbatim excerpts) with the turn's
tool calls in order — rendering as the mockup's
``¹ "quote" → <tool call>`` block.

Compliance item D7 extends the collector with a second, independent
index: :attr:`EvidenceCollector._records` persists a
:class:`~amplifier_app_tui.model.evidence.ToolCallRecord` per
``tool_call_id`` — the durable provenance the evidence side panel joins
against (:func:`~amplifier_app_tui.model.evidence.build_evidence_detail`).
This is captured at the same ``ToolPost`` observation point as the claim
pairing above, but keyed independently of it: provenance must resolve
regardless of how the transcript renderer later groups/digests ToolLine
blocks for display (never infer provenance from display order).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..model.evidence import EvidenceLink, ToolCallRecord
from .events import ContentBlockEnd, PromptComplete, PromptSubmit, ToolPost, UIEvent
from .persistence import is_top_level_session

MAX_CLAIMS = 4
"""Cap on derived claims per answer (the mockup block stays compact)."""

QUOTE_MAX_CHARS = 60
"""Claim quotes stay short phrases; cut at a word boundary, verbatim."""

REF_MAX_CHARS = 60
"""Tool refs are one-line human-readable references."""

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

_HINT_KEYS = ("command", "file_path", "path", "pattern", "url", "query")
"""First present string input becomes the tool ref's detail hint."""

_OUTPUT_KEYS = ("output", "stdout", "content", "text", "body", "message")
"""First present string result key becomes the provenance record's output
preview (mirrors ``_HINT_KEYS``' single-source-of-truth shape)."""

MAIN_AGENT_LABEL = "main agent"
"""Originating-agent label for top-level evidence (AC2). The collector
only ever observes the top-level session (subagent lanes ground their
own transcripts, see :meth:`EvidenceCollector.observe`), so every
provenance record it builds names the same, honest originator."""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _quote(sentence: str) -> str:
    """A short verbatim excerpt of *sentence* (word-boundary prefix)."""
    sentence = sentence.strip()
    if len(sentence) > QUOTE_MAX_CHARS:
        head, _, _ = sentence[: QUOTE_MAX_CHARS + 1].rpartition(" ")
        sentence = head or sentence[:QUOTE_MAX_CHARS]
    return sentence.rstrip(".!?,;: ")


def input_hint(tool_input: Mapping[str, Any]) -> str:
    """The first present string input worth showing as a detail hint.

    Extracted from :func:`tool_ref` so the evidence side panel's
    "inputs or query summary" (AC2) and the compact claim-row reference
    can never drift apart — both read the same key list.
    """
    for key in _HINT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def tool_ref(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    """Human-readable reference to one grounding tool call (spec §10)."""
    hint = input_hint(tool_input)
    if tool_name == "bash" and hint:
        return _clip(f"$ {hint}", REF_MAX_CHARS)
    if hint:
        return _clip(f"{tool_name} · {hint}", REF_MAX_CHARS)
    return tool_name


def result_output(result: Mapping[str, Any]) -> str:
    """A bounded, human-readable rendering of a tool's raw result payload
    (AC2 "source/output"). First present string under :data:`_OUTPUT_KEYS`
    wins; falls back to the raw mapping's ``repr`` so a result shaped
    unlike any known tool still shows *something* rather than nothing."""
    for key in _OUTPUT_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if result:
        return str(result)
    return ""


def derive_links(answer_text: str, calls: Sequence[tuple[str, str]]) -> tuple[EvidenceLink, ...]:
    """Pair the answer's leading sentences with the turn's tool calls.

    *calls* is ``(tool_ref, tool_call_id)`` in completion order. The
    pairing is positional (sentence i ↔ call i) — deterministic, and
    every claim quote is a verbatim excerpt of *answer_text*.
    """
    sentences = [s for s in _SENTENCE_SPLIT.split(answer_text) if s.strip()]
    links: list[EvidenceLink] = []
    for sentence, (ref, call_id) in zip(sentences, calls, strict=False):
        quote = _quote(sentence)
        if not quote:
            continue
        links.append(EvidenceLink(claim_quote=quote, tool_ref=ref, tool_call_id=call_id))
        if len(links) >= MAX_CLAIMS:
            break
    return tuple(links)


class EvidenceCollector:
    """Queue-bridge tap: the turn's tool calls → per-answer evidence.

    ``observe`` sees every normalized UIEvent at emit time — strictly
    before the reducer consumes it from the queue — so by the time the
    reducer finalizes an Answer block and asks ``links_for(text)``, the links
    for that exact final response are already derived. Explicit demo answers
    retain their immediate content-block binding.

    Alongside the answer-keyed claims, :attr:`_records` persists one
    :class:`ToolCallRecord` per ``tool_call_id`` (D7) — the durable
    provenance store :meth:`record_for` serves to the evidence side panel.
    """

    def __init__(self) -> None:
        self._calls: list[tuple[str, str]] = []
        self._by_answer: dict[str, tuple[EvidenceLink, ...]] = {}
        self._records: dict[str, ToolCallRecord] = {}

    def observe(self, event: UIEvent) -> None:
        """Track one emitted event (top-level session only, spec §8)."""
        if not is_top_level_session(event.session_id):
            return  # subagent lanes ground their own transcripts
        if isinstance(event, PromptSubmit):
            self._calls.clear()
        elif isinstance(event, ToolPost):
            if event.tool_name == "update_plan":
                return  # plan updates are not grounding evidence
            if str(event.result.get("status", "")) == "denied":
                return  # a denied call ran nothing — grounds no claim
            self._calls.append((tool_ref(event.tool_name, event.tool_input), event.tool_call_id))
            if event.tool_call_id:
                self._records[event.tool_call_id] = ToolCallRecord(
                    tool_call_id=event.tool_call_id,
                    tool_name=event.tool_name,
                    tool_input=dict(event.tool_input),
                    output=result_output(event.result),
                    ts=event.ts,
                    agent=MAIN_AGENT_LABEL,
                )
        elif isinstance(event, ContentBlockEnd):
            if event.block_type != "text":
                return
            text = str(event.block.get("text", ""))
            role = event.block.get("demo_role")
            if not text or role != "answer":
                return  # production text is provisional; demo non-answers are not targets
            self._by_answer[text] = derive_links(text, tuple(self._calls))
        elif isinstance(event, PromptComplete):
            text = event.response.strip()
            if text:
                self._by_answer[text] = derive_links(text, tuple(self._calls))

    def links_for(self, answer_text: str) -> tuple[EvidenceLink, ...]:
        """Evidence links derived for the answer with this exact text."""
        return self._by_answer.get(answer_text, ())

    def record_for(self, tool_call_id: str) -> ToolCallRecord | None:
        """The durable provenance record for *tool_call_id*, if observed.

        Independent of :meth:`links_for` and of how the transcript
        currently renders ToolLine blocks (D7 design note: persist stable
        links between agent event, tool call, and evidence artifact — do
        not infer provenance from display order).
        """
        return self._records.get(tool_call_id) if tool_call_id else None


__all__ = [
    "MAIN_AGENT_LABEL",
    "MAX_CLAIMS",
    "QUOTE_MAX_CHARS",
    "REF_MAX_CHARS",
    "EvidenceCollector",
    "derive_links",
    "input_hint",
    "result_output",
    "tool_ref",
]
