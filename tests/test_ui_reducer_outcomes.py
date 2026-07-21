"""Real-runtime turn close-out outcomes (DESIGN-SPEC §3 / §11).

Without a demo spec the reducer must derive the turn rule from the
enriched ``PromptComplete`` the RealRuntime synthesizes after its
end-of-turn git snapshot:

- files changed → ``shipped`` (``N files · +A/−D · tests ✔`` label,
  dim rule, ledger shipped count, footer ▲);
- no files → ``answer`` (or ``· plan ready`` in plan mode);
- cancelled → ``· interrupted`` plus the italic
  ``Interrupted. Goal: … Context saved; resume or restate direction.``
  recap block, exactly like the demo scripts it.

Offline: fake events straight into the reducer, no Textual, no git.
"""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import (
    Answer,
    BlockIdAllocator,
    Narration,
    TranscriptBlock,
    TurnRule,
)
from amplifier_app_newtui.model.evidence import EvidenceLink
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.model.turn import OutcomeLedger
from amplifier_app_newtui.ui.reducer import TranscriptReducer


class FakeHost:
    """Minimal ReducerHost: records blocks, ignores presentation."""

    def __init__(self, mode_id: str = "chat") -> None:
        self.mode_id = mode_id
        self.blocks: list[TranscriptBlock] = []
        self.notices: list[str] = []
        self.stream_events: list[tuple[str, str]] = []

    def append_block(self, block: TranscriptBlock) -> None:
        self.blocks.append(block)

    def replace_block(self, block: TranscriptBlock) -> None:
        for i, existing in enumerate(self.blocks):
            if existing.id == block.id:
                self.blocks[i] = block
                return

    def remove_block(self, block_id: str) -> None:
        self.blocks = [b for b in self.blocks if b.id != block_id]

    def show_notice(self, text: str) -> None:
        self.notices.append(text)

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        pass

    def turn_started(self) -> None:
        pass

    def turn_finished(self) -> None:
        pass

    def lanes_changed(self) -> None:
        pass

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        pass

    def decision_deferred(self, message: str) -> None:
        pass

    def stream_opened(self, block_type: str) -> None:
        self.stream_events.append(("opened", block_type))

    def stream_delta(self, text: str) -> None:
        self.stream_events.append(("delta", text))

    def stream_closed(self) -> None:
        self.stream_events.append(("closed", ""))


def make_reducer(mode_id: str = "chat") -> tuple[TranscriptReducer, FakeHost]:
    host = FakeHost(mode_id)
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    return reducer, host


def last_rule(host: FakeHost) -> TurnRule:
    rules = [b for b in host.blocks if isinstance(b, TurnRule)]
    assert rules, f"no TurnRule in {[type(b).__name__ for b in host.blocks]}"
    return rules[-1]


def answer_text(block: Answer) -> str:
    return "".join(segment.text for segment in block.spans)


def test_production_text_stays_styled_and_final_response_promotes_exactly_once() -> None:
    evidence = (EvidenceLink(claim_quote="Done", tool_ref="$ pytest"),)
    host = FakeHost()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
        evidence_lookup=lambda text: evidence if text.strip() == "Done." else (),
    )
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="do it", ts=1.0))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="text",
            block={"type": "text", "text": "Checking the files."},
            ts=2.0,
        )
    )
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="text",
            block={"type": "text", "text": "Done."},
            ts=3.0,
        )
    )

    candidates = [block for block in host.blocks if isinstance(block, Answer)]
    assert [answer_text(block) for block in candidates] == ["Checking the files.", "Done."]
    assert all(not block.clickable for block in candidates)
    promoted_id = candidates[-1].id

    reducer.handle(ev.PromptComplete(session_id="root", response="Done.", ts=4.0))

    answers = [block for block in host.blocks if isinstance(block, Answer)]
    assert len(answers) == 2
    final = next(block for block in answers if block.id == promoted_id)
    assert answer_text(final) == "Done."
    assert final.evidence_refs == evidence
    assert final.clickable
    # The earlier intermediate prose remains once; the final is replaced in place.
    assert [answer_text(block) for block in answers].count("Done.") == 1


def test_stream_then_durable_close_never_replays_raw_final_markdown() -> None:
    """Real ordering: stream ends, durable text lands, PromptComplete promotes it."""
    reducer, host = make_reducer()
    response = "## Result\n\n**Done.**"
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="do it", ts=1.0))
    reducer.handle(ev.StreamBlockStart(session_id="root", block_type="text", ts=2.0))
    reducer.handle(
        ev.StreamBlockDelta(
            session_id="root", block_type="text", text=response, ts=2.1
        )
    )
    reducer.handle(ev.StreamBlockEnd(session_id="root", block_type="text", ts=2.2))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="text",
            block={"type": "text", "text": response},
            ts=2.3,
        )
    )

    provisional = [block for block in host.blocks if isinstance(block, Answer)]
    assert len(provisional) == 1
    assert not provisional[0].clickable
    assert not any(isinstance(block, Narration) for block in host.blocks)

    reducer.handle(ev.PromptComplete(session_id="root", response=response, ts=2.5))
    final = [block for block in host.blocks if isinstance(block, Answer)]
    assert len(final) == 1
    assert final[0].id == provisional[0].id
    assert final[0].clickable
    assert "".join(segment.text for segment in final[0].spans).count("Done.") == 1


def test_prompt_complete_appends_one_fallback_answer_without_durable_text() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="answer me", ts=1.0))
    reducer.handle(ev.PromptComplete(session_id="root", response="The final answer.", ts=2.0))

    answers = [block for block in host.blocks if isinstance(block, Answer)]
    assert len(answers) == 1
    assert answer_text(answers[0]) == "The final answer."


def test_explicit_demo_answer_is_not_duplicated_at_prompt_complete() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="demo", ts=1.0))
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="root",
            block_type="text",
            block={"type": "text", "text": "Scripted answer.", "demo_role": "answer"},
            ts=2.0,
        )
    )
    reducer.handle(ev.PromptComplete(session_id="root", response="Scripted answer.", ts=3.0))

    answers = [block for block in host.blocks if isinstance(block, Answer)]
    assert len(answers) == 1
    assert answer_text(answers[0]) == "Scripted answer."


def test_foreign_session_execution_cannot_mutate_root_transcript_or_close_out() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="delegate", ts=1.0))
    reducer.handle(
        ev.StreamBlockStart(session_id="child", parent_id="root", block_type="text", ts=2.0)
    )
    reducer.handle(
        ev.StreamBlockDelta(
            session_id="child", parent_id="root", block_type="text", text="child", ts=2.1
        )
    )
    reducer.handle(
        ev.StreamBlockEnd(session_id="child", parent_id="root", block_type="text", ts=2.2)
    )
    reducer.handle(
        ev.ToolPre(
            session_id="child",
            parent_id="root",
            tool_name="bash",
            tool_call_id="child-call",
            tool_input={"command": "cat secret"},
            ts=2.3,
        )
    )
    reducer.handle(
        ev.ToolPost(
            session_id="child",
            parent_id="root",
            tool_name="bash",
            tool_call_id="child-call",
            tool_input={"command": "cat secret"},
            result={"output": "child output"},
            ts=2.4,
        )
    )
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="child",
            parent_id="root",
            block_type="text",
            block={"type": "text", "text": "child internal narration"},
            ts=2.5,
        )
    )
    reducer.handle(
        ev.OrchestratorComplete(session_id="child", parent_id="root", status="cancelled", ts=2.6)
    )

    assert host.stream_events == []
    assert not any(block.kind == "tool_line" for block in host.blocks)
    assert not any(
        isinstance(block, Narration) and block.text == "child internal narration"
        for block in host.blocks
    )

    reducer.handle(ev.PromptComplete(session_id="root", response="Root answer.", ts=3.0))
    answers = [block for block in host.blocks if isinstance(block, Answer)]
    assert [answer_text(block) for block in answers] == ["Root answer."]
    assert last_rule(host).label.endswith(" · answer")


def test_real_turn_with_file_changes_ships() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(prompt="refactor the store", ts=1.0))
    reducer.handle(
        ev.ProviderResponseUsage(input_tokens=100, output_tokens=3200, model="fake", ts=2.0)
    )
    reducer.handle(
        ev.PromptComplete(
            response="done",
            files_changed=3,
            diffstat="+142/−38",
            tests_ok=True,
            ts=13.0,
        )
    )
    rule = last_rule(host)
    assert rule.shipped
    assert rule.label.endswith("3 files · +142/−38 · tests ✔")
    recorded = reducer.ledger.turns[-1]
    assert recorded.outcome.kind == "shipped"
    assert recorded.outcome.files_changed == 3
    assert recorded.outcome.diffstat == "+142/−38"
    assert recorded.outcome.tests_ok is True
    assert reducer.ledger.last_shipped  # footer ▲ yield glyph


def test_context_compaction_is_visible_and_persistent() -> None:
    reducer, host = make_reducer()
    reducer.handle(
        ev.ContextCompacted(
            before_tokens=120_000,
            after_tokens=60_000,
            before_messages=42,
            after_messages=23,
            strategy_level=3,
        )
    )
    narration = host.blocks[-1]
    assert narration.kind == "narration"
    assert narration.text == (
        "Context compacted · 120,000 → 60,000 tokens"
        " · 42 → 23 messages · strategy 3"
    )
    assert host.notices[-1] == narration.text


def test_real_turn_with_unpriceable_usage_marks_rule_cost_estimated() -> None:
    """Never lie: an unknown model with no cost_usd renders ``~$`` not ``$0.00``."""
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(prompt="ask the mystery model", ts=1.0))
    reducer.handle(
        ev.ProviderResponseUsage(
            input_tokens=100, output_tokens=3200, model="mystery-model-9000", ts=2.0
        )
    )
    reducer.handle(ev.PromptComplete(response="done", ts=13.0))
    rule = last_rule(host)
    assert "~$0.00" in rule.label
    assert reducer.ledger.turns[-1].telemetry.estimated
    # session-level flag feeds the footer's ~$ total
    assert reducer.unpriced_usage == 1


def test_real_turn_with_priced_usage_keeps_plain_dollar() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(prompt="priced turn", ts=1.0))
    reducer.handle(
        ev.ProviderResponseUsage(
            input_tokens=1000, output_tokens=1000, model="claude-sonnet-4", ts=2.0
        )
    )
    reducer.handle(ev.PromptComplete(response="done", ts=4.0))
    rule = last_rule(host)
    assert "~$" not in rule.label
    assert "$0.02" in rule.label  # 1k in + 1k out on the fallback table
    assert reducer.unpriced_usage == 0


def test_real_turn_failed_tests_render_tests_cross() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(prompt="fix the flake", ts=1.0))
    reducer.handle(
        ev.PromptComplete(response="tried", files_changed=1, diffstat="+4/−1", tests_ok=False, ts=5.0)
    )
    assert last_rule(host).label.endswith("1 file · +4/−1 · tests ✗")


def test_real_turn_without_file_changes_stays_answer_only() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(prompt="explain the store", ts=1.0))
    reducer.handle(ev.PromptComplete(response="it stores", ts=4.0))
    rule = last_rule(host)
    assert not rule.shipped
    assert rule.label.endswith(" · answer")
    assert reducer.ledger.turns[-1].outcome.kind == "answer"
    assert not reducer.ledger.last_shipped


def test_real_plan_mode_turn_is_plan_ready() -> None:
    reducer, host = make_reducer(mode_id="plan")
    reducer.handle(ev.PromptSubmit(prompt="how should we do it?", ts=1.0))
    reducer.handle(ev.PromptComplete(response="plan", ts=3.0))
    rule = last_rule(host)
    assert not rule.shipped
    assert rule.label.endswith(" · plan ready")
    assert reducer.ledger.turns[-1].outcome.kind == "plan_ready"


def test_real_interrupted_turn_appends_recap_and_never_ships() -> None:
    prompt = "refactor the session store"
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(prompt=prompt, ts=1.0))
    reducer.handle(ev.CancelCompleted(ts=6.0))
    # Even a cancelled turn that touched files must NOT count as shipped.
    reducer.handle(
        ev.PromptComplete(response="", files_changed=2, diffstat="+9/−1", tests_ok=None, ts=7.0)
    )
    rule = last_rule(host)
    assert not rule.shipped
    assert rule.label.endswith(" · interrupted")
    assert reducer.ledger.turns[-1].outcome.kind == "interrupted"
    # The italic recap sits directly above the rule, demo shape exactly.
    recap = host.blocks[host.blocks.index(rule) - 1]
    assert isinstance(recap, Answer)
    assert not recap.clickable
    assert recap.spans[0].text == "✳ "
    assert recap.spans[0].style_token == "dimmer"
    assert recap.spans[1].text == (
        f"Interrupted. Goal: {prompt[:40]}. Context saved; resume or restate direction."
    )
    assert recap.spans[1].style_token == "dim"
    assert recap.spans[1].italic
    assert host.notices[-1] == "turn interrupted · context saved"


def test_real_interrupted_recap_comes_from_orchestrator_cancelled_too() -> None:
    reducer, host = make_reducer()
    reducer.handle(ev.PromptSubmit(prompt="build the thing", ts=1.0))
    reducer.handle(ev.OrchestratorComplete(status="cancelled", ts=5.0))
    reducer.handle(ev.PromptComplete(response="", ts=5.5))
    rule = last_rule(host)
    assert rule.label.endswith(" · interrupted")
    recap = host.blocks[host.blocks.index(rule) - 1]
    assert isinstance(recap, Answer)
    assert recap.spans[1].text.startswith("Interrupted. Goal: build the thing.")


def test_demo_spec_interrupted_close_out_adds_no_extra_recap() -> None:
    """The demo scripts its own recap event; the spec path must not add one."""

    class Spec:
        duration_ms = 6000
        tokens = 1000
        cached_pct = 50
        cost = Decimal("0.05")
        cost_after = Decimal("0.05")
        outcome = "interrupted"
        shipped = False
        rule_label = "6s · 1.0k tok, 50% cached · $0.05 · interrupted"
        checkpoint_label = "store refactor · interrupted"

    host = FakeHost()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
        spec_lookup=lambda prompt: Spec(),
    )
    reducer.handle(ev.PromptSubmit(prompt="refactor the store", ts=1.0))
    reducer.handle(ev.CancelCompleted(ts=2.0))
    reducer.handle(ev.PromptComplete(response="", ts=3.0))
    rule = last_rule(host)
    assert rule.label == Spec.rule_label
    before_rule = host.blocks[host.blocks.index(rule) - 1]
    # Directly above the rule is the user line — no synthesized recap.
    assert not isinstance(before_rule, Answer)


def test_permissions_block_renders_slot_labels_not_bound_methods() -> None:
    """Regression: /permissions once rendered ``<bound method TrustSlot.label …>``
    because ``slot.label`` was never called (found live in forge, 2026-07-16)."""
    from amplifier_app_newtui.commands.permissions import PermissionSurface
    from amplifier_app_newtui.model.blocks import BlockIdAllocator
    from amplifier_app_newtui.ui.app_support import permissions_block

    surface = PermissionSurface(mode="auto")
    surface.add_exception("uv run pytest")
    block = permissions_block(surface, "auto read,write · classifier-gated", BlockIdAllocator())
    text = "".join(segment.text for segment in block.spans)
    assert "bound method" not in text
    assert "path policy · allowed roots + protected paths enforced" in text
    assert "execution confinement" not in text
    assert "read · allow" in text
    assert "always allowed: uv run pytest" in text
    assert "boundary: within project" in text


def test_improve_block_empty_state_renders_placeholder_row() -> None:
    """/improve with no evidence must say so, not print a bare header."""
    from amplifier_app_newtui.commands.improve import build_improve_block
    from amplifier_app_newtui.ui.transcript import render_block

    block = build_improve_block("b1", ())
    lines = render_block(block, 120)
    assert len(lines) == 2
    assert "no proposals yet" in "".join(s.text for s in lines[1])


def test_real_turn_mounts_working_line_immediately_and_ticks() -> None:
    """Supervisor feedback: spec-less (real) turns pulse from second zero."""
    from amplifier_app_newtui.kernel import events as ev
    from amplifier_app_newtui.ui.transcript import render_block

    reducer, host = make_reducer("auto")
    reducer.handle(ev.PromptSubmit(session_id="s", prompt="hi", ts=100.0))
    kinds = [b.kind for b in host.blocks]
    assert kinds == ["user_line", "working_status"]

    # 1s heartbeat: wall clock bumps the seconds and the spinner pulses.
    reducer.tick(103.0)
    working = host.blocks[-1]
    assert working.kind == "working_status"
    assert working.spinner_frame == 1
    line = "".join(s.text for s in render_block(working, 200)[0])
    assert "working · 3s" in line and "1 agent" in line

    # A running tool shows as the active branch of the live tree beneath
    # the pulse (not inline); the static '1 agent' fallback drops away.
    reducer.handle(
        ev.ToolPre(
            session_id="s",
            tool_call_id="t1",
            tool_name="bash",
            tool_input={"command": "uv run pytest -q"},
            ts=104.0,
        )
    )
    working = next(b for b in host.blocks if b.kind == "working_status")
    rendered = "\n".join(
        "".join(s.text for s in line) for line in render_block(working, 200)
    )
    assert "$ uv run pytest -q" in rendered  # in the tree
    assert working.activity_lines and working.activity_lines[-1].running
    assert "1 agent" not in rendered.splitlines()[0]  # not inline on the pulse
    # ...and the pulse rides at the BOTTOM, under the newest content.
    assert host.blocks[-1].kind == "working_status"

    # A durable answer flushes the burst into a digest and clears the tree.
    reducer.handle(
        ev.ToolPost(
            session_id="s",
            tool_call_id="t1",
            tool_name="bash",
            tool_input={"command": "uv run pytest -q"},
            result={"output": "ok"},
            ts=105.0,
        )
    )
    reducer.handle(
        ev.ContentBlockEnd(
            session_id="s",
            block_type="text",
            block={"type": "text", "text": "done"},
            ts=106.0,
        )
    )
    working = next(b for b in host.blocks if b.kind == "working_status")
    assert working.activity_lines == ()  # burst flushed — tree cleared
    digest = next(
        b for b in host.blocks if b.kind == "tool_line" and b.summary.startswith("Ran")
    )
    assert digest.summary == "Ran 1 shell command"


def test_mixed_tool_burst_collapses_to_one_humanized_digest() -> None:
    """A run of many tools between answers is ONE line — not one per tool
    (DESIGN-SPEC §3): ``Read 2 files · searched 1× · ran 1 shell command``
    with every op in the expandable body."""
    from amplifier_app_newtui.kernel import events as ev

    reducer, host = make_reducer("auto")
    reducer.handle(ev.PromptSubmit(session_id="s", prompt="investigate", ts=0.0))

    ops = [
        ("read_file", {"file_path": "src/a.py"}),
        ("read_file", {"file_path": "src/b.py"}),
        ("grep", {"pattern": "TODO"}),
        ("bash", {"command": "uv run pytest -q"}),
    ]
    for i, (tool, tool_input) in enumerate(ops):
        cid = f"t{i}"
        reducer.handle(
            ev.ToolPre(session_id="s", tool_call_id=cid, tool_name=tool, tool_input=tool_input)
        )
        reducer.handle(
            ev.ToolPost(
                session_id="s",
                tool_call_id=cid,
                tool_name=tool,
                tool_input=tool_input,
                result={"output": "ok"},
            )
        )

    digests = [b for b in host.blocks if b.kind == "tool_line"]
    assert len(digests) == 1  # the whole burst is a single line
    digest = digests[0]
    assert digest.summary == "Read 2 files · searched 1× · ran 1 shell command"
    # every op is preserved in the (collapsed) expandable body
    assert digest.body == ("read a.py", "read b.py", "searched TODO", "$ uv run pytest -q")
    # live tree beneath the pulse is bounded to the most recent ops
    working = next(b for b in host.blocks if b.kind == "working_status")
    assert len(working.activity_lines) <= 3
