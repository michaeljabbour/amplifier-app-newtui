"""kernel/evidence.py — §10 evidence links derived for real sessions.

DESIGN-SPEC §10: clicking a final answer prints numbered teal claims
``¹ "quote" → <tool call that grounds it>``. ADR-0007 resolution 9: real
sessions derive these from the normalized event stream (the same stream
events.jsonl records) — the collector taps the queue bridge, tracks the
turn's completed tool calls, and pairs them with verbatim answer excerpts.
"""

from __future__ import annotations

import asyncio

from amplifier_app_newtui.kernel.events import (
    ContentBlockEnd,
    PromptSubmit,
    ToolPost,
)
from amplifier_app_newtui.kernel.evidence import (
    MAX_CLAIMS,
    QUOTE_MAX_CHARS,
    EvidenceCollector,
    derive_links,
    tool_ref,
)
from amplifier_app_newtui.kernel.queue_bridge import QueueBridge

SID = "sess-1"


def _tool_post(
    name: str = "bash",
    call_id: str = "c1",
    command: str = "uv run pytest -q",
    result: dict | None = None,
    session_id: str = SID,
) -> ToolPost:
    tool_input = {"command": command} if command else {}
    return ToolPost(
        session_id=session_id,
        tool_name=name,
        tool_call_id=call_id,
        tool_input=tool_input,
        result=result if result is not None else {"status": "success"},
    )


def _answer(text: str, session_id: str = SID, **block_extra: str) -> ContentBlockEnd:
    return ContentBlockEnd(
        session_id=session_id,
        block_type="text",
        block={"type": "text", "text": text, **block_extra},
    )


# ---------------------------------------------------------------------------
# derive_links / tool_ref (pure derivation)
# ---------------------------------------------------------------------------


def test_derive_pairs_sentences_with_calls_in_order() -> None:
    answer = "All 41 tests pass. The store migration is verified."
    calls = (("$ uv run pytest -q", "c1"), ("read_file · store.py", "c2"), ("grep", "c3"))
    links = derive_links(answer, calls)
    assert len(links) == 2  # bounded by sentence count
    assert links[0].claim_quote == "All 41 tests pass"
    assert links[0].tool_ref == "$ uv run pytest -q"
    assert links[0].tool_call_id == "c1"
    assert links[1].tool_ref == "read_file · store.py"
    # Every claim quote is a verbatim excerpt of the answer (spec §10).
    for link in links:
        assert link.claim_quote in answer


def test_derive_bounded_by_calls_and_cap() -> None:
    answer = ". ".join(f"Sentence number {i} here" for i in range(10)) + "."
    assert derive_links(answer, ()) == ()
    many = tuple((f"tool-{i}", f"c{i}") for i in range(10))
    assert len(derive_links(answer, many)) == MAX_CLAIMS


def test_quote_cut_at_word_boundary_stays_verbatim() -> None:
    long_sentence = "word " * 40  # single sentence far beyond the cap
    links = derive_links(long_sentence, (("$ ls", "c1"),))
    assert len(links) == 1
    assert len(links[0].claim_quote) <= QUOTE_MAX_CHARS
    assert links[0].claim_quote in long_sentence
    assert not links[0].claim_quote.endswith(" ")


def test_tool_ref_shapes() -> None:
    assert tool_ref("bash", {"command": "git  status"}) == "$ git status"
    assert tool_ref("read_file", {"file_path": "src/app.py"}) == "read_file · src/app.py"
    assert tool_ref("web_search", {}) == "web_search"
    clipped = tool_ref("bash", {"command": "x" * 200})
    assert len(clipped) == 60 and clipped.endswith("…")


# ---------------------------------------------------------------------------
# EvidenceCollector (event-stream behavior)
# ---------------------------------------------------------------------------


def test_collector_derives_links_for_answer() -> None:
    collector = EvidenceCollector()
    collector.observe(PromptSubmit(session_id=SID, prompt="check the tests"))
    collector.observe(_tool_post())
    answer = "All 41 tests pass with no flakes."
    collector.observe(_answer(answer))
    links = collector.links_for(answer)
    assert len(links) == 1
    assert links[0].claim_quote == "All 41 tests pass with no flakes"
    assert links[0].tool_ref == "$ uv run pytest -q"
    assert links[0].tool_call_id == "c1"
    assert collector.links_for("some other answer") == ()


def test_collector_resets_calls_each_turn() -> None:
    collector = EvidenceCollector()
    collector.observe(PromptSubmit(session_id=SID, prompt="one"))
    collector.observe(_tool_post())
    collector.observe(PromptSubmit(session_id=SID, prompt="two"))
    answer = "Nothing ran this turn."
    collector.observe(_answer(answer))
    assert collector.links_for(answer) == ()


def test_collector_skips_non_grounding_events() -> None:
    collector = EvidenceCollector()
    collector.observe(PromptSubmit(session_id=SID, prompt="go"))
    # Denied calls, plan updates and subagent-lane calls ground nothing.
    collector.observe(_tool_post(result={"status": "denied", "reason": "trust"}))
    collector.observe(_tool_post(name="update_plan", call_id="c2", command=""))
    collector.observe(_tool_post(call_id="c3", session_id="sess-1-ab_researcher"))
    answer = "Nothing was actually executed."
    collector.observe(_answer(answer))
    assert collector.links_for(answer) == ()


def test_collector_ignores_narration_and_non_text() -> None:
    collector = EvidenceCollector()
    collector.observe(PromptSubmit(session_id=SID, prompt="go"))
    collector.observe(_tool_post())
    narration = "Applying steer: keep the journal"
    collector.observe(_answer(narration, demo_role="narration"))
    assert collector.links_for(narration) == ()
    collector.observe(
        ContentBlockEnd(session_id=SID, block_type="thinking", block={"text": "hmm"})
    )
    assert collector.links_for("hmm") == ()


def test_bridge_tap_sees_events_before_queue() -> None:
    seen: list[str] = []
    queue: asyncio.Queue = asyncio.Queue()
    bridge = QueueBridge(queue, tap=lambda event: seen.append(event.kind))
    bridge.emit(PromptSubmit(session_id=SID, prompt="hi"))
    assert seen == ["prompt_submit"]
    assert queue.qsize() == 1


def test_bridge_tap_failure_never_blocks_the_queue() -> None:
    def boom(event: object) -> None:
        raise RuntimeError("tap exploded")

    queue: asyncio.Queue = asyncio.Queue()
    bridge = QueueBridge(queue, tap=boom)
    bridge.emit(PromptSubmit(session_id=SID, prompt="hi"))
    assert queue.qsize() == 1
    assert bridge.dropped == 0
