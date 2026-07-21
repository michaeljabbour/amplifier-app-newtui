"""Evidence links for real sessions (DESIGN-SPEC §10, ADR-0007 resolution 9).

The demo script ships hand-authored claims; a real session derives them
from the same normalized UIEvent stream that events.jsonl records
(ADR-0007: the event log "powers … evidence links"). The collector taps
the queue bridge, keeps the running turn's completed top-level tool
calls, and when ``PromptComplete`` identifies the production final answer
it pairs the answer's leading sentences (verbatim excerpts) with the turn's
tool calls in order — rendering as the mockup's
``¹ "quote" → <tool call>`` block.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..model.evidence import EvidenceLink
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


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _quote(sentence: str) -> str:
    """A short verbatim excerpt of *sentence* (word-boundary prefix)."""
    sentence = sentence.strip()
    if len(sentence) > QUOTE_MAX_CHARS:
        head, _, _ = sentence[: QUOTE_MAX_CHARS + 1].rpartition(" ")
        sentence = head or sentence[:QUOTE_MAX_CHARS]
    return sentence.rstrip(".!?,;: ")


def tool_ref(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    """Human-readable reference to one grounding tool call (spec §10)."""
    hint = ""
    for key in _HINT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            hint = " ".join(value.split())
            break
    if tool_name == "bash" and hint:
        return _clip(f"$ {hint}", REF_MAX_CHARS)
    if hint:
        return _clip(f"{tool_name} · {hint}", REF_MAX_CHARS)
    return tool_name


def derive_links(
    answer_text: str, calls: Sequence[tuple[str, str]]
) -> tuple[EvidenceLink, ...]:
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
    """

    def __init__(self) -> None:
        self._calls: list[tuple[str, str]] = []
        self._by_answer: dict[str, tuple[EvidenceLink, ...]] = {}

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
            self._calls.append(
                (tool_ref(event.tool_name, event.tool_input), event.tool_call_id)
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


__all__ = [
    "MAX_CLAIMS",
    "QUOTE_MAX_CHARS",
    "REF_MAX_CHARS",
    "EvidenceCollector",
    "derive_links",
    "tool_ref",
]
