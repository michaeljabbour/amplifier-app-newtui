"""The host-provided ``question`` tool (``kernel/question.py``).

Re-expression of opencode's ``question`` tool over the app's EXISTING
deferred-decision / needs-you plumbing. The load-bearing invariants:

- the tool defers each question onto the shared ``NeedsYouQueue`` and BLOCKS
  until the user answers through the SAME ``needs_you.answer`` seam the serve
  ``{"op":"decision"}`` op and the TUI ``apply_decision`` both drive;
- an answered question is CONSUMED, so the ``StepBoundaryBridge`` never
  re-injects the answer as a next-turn instruction (double-answer + turn
  miscount);
- a dismissed question resolves to ``Unanswered`` and the turn continues
  (deny-and-continue, replacing the donor's ``Effect.orDie``);
- governance classifies ``question`` as READ (auto-allowed wherever tools run).

Duck-typed over bare kernel/model objects: no session, no model, no network.
"""

from __future__ import annotations

import asyncio

import pytest

from amplifier_app_tui.kernel.question import (
    QUESTION_TOOL_NAME,
    QuestionOption,
    QuestionPrompt,
    QuestionTool,
    format_deferred_output,
    format_output,
    parse_questions,
)
from amplifier_app_tui.kernel.steering import StepBoundaryBridge
from amplifier_app_tui.model.queues import NeedsYouQueue, SteeringQueue
from amplifier_app_tui.model.trust import CapabilityClass, classify_tool, resolve


async def _wait_pending(queue: NeedsYouQueue, count: int = 1) -> None:
    """Yield to the loop until the tool has deferred *count* questions."""
    for _ in range(500):
        if len(queue.pending) >= count:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"tool never deferred {count} question(s)")


# ---------------------------------------------------------------------------
# Schema + pure helpers (no loop)
# ---------------------------------------------------------------------------


def test_schema_contract() -> None:
    tool = QuestionTool(NeedsYouQueue())
    assert tool.name == QUESTION_TOOL_NAME == "question"
    assert "ask the user" in tool.description.lower()
    schema = tool.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["questions"]
    item = schema["properties"]["questions"]["items"]
    assert item["required"] == ["question"]
    for field in ("question", "header", "options", "multiple", "custom"):
        assert field in item["properties"], field
    assert item["properties"]["options"]["items"]["required"] == ["label"]


def test_parse_questions_is_tolerant() -> None:
    prompts = parse_questions(
        [
            {
                "question": "  Which colors?  ",
                "header": "colors",
                "options": [
                    {"label": "Yellow", "description": "warm"},
                    "Blue",  # bare-string option
                    {"description": "no label"},  # dropped
                ],
                "multiple": True,
            },
            {"question": ""},  # blank -> skipped
            "not-a-dict",  # skipped
        ]
    )
    assert len(prompts) == 1
    prompt = prompts[0]
    assert prompt.question == "Which colors?"
    assert prompt.header == "colors"
    assert prompt.multiple is True
    assert prompt.custom is True  # default on
    assert prompt.labels == ("Yellow", "Blue")


def test_parse_questions_accepts_bare_dict_and_custom_flag() -> None:
    prompts = parse_questions({"question": "Ship?", "custom": False})
    assert len(prompts) == 1
    assert prompts[0].custom is False
    assert parse_questions("junk") == []
    assert parse_questions(None) == []


def test_format_output_marks_unanswered() -> None:
    text = format_output([("Pick one", "Blue"), ("Other", "")])
    assert 'User has answered your questions: "Pick one"="Blue", "Other"="Unanswered".' in text
    assert "continue with the user's answers in mind" in text


def test_format_deferred_output_tells_auto_to_continue() -> None:
    text = format_deferred_output([QuestionPrompt(question="Pick a target?")])
    assert '"Pick a target?"' in text
    assert "Auto mode is continuing" in text
    assert "do not wait or repeat" in text


def test_trust_classifies_question_as_read() -> None:
    assert classify_tool("question") is CapabilityClass.READ
    # auto-allowed wherever tools run; brainstorm ("no tools") denies it.
    assert resolve("build", "question").decision == "allow"
    assert resolve("chat", "question").decision == "allow"
    assert resolve("plan", "question").decision == "allow"
    assert resolve("auto", "question").decision == "allow"
    assert resolve("brainstorm", "question").decision == "deny"


def test_prompt_labels_dataclass() -> None:
    prompt = QuestionPrompt(
        question="q",
        options=(QuestionOption("A", "a"), QuestionOption("", "blank"), QuestionOption("B")),
    )
    assert prompt.labels == ("A", "B")


# ---------------------------------------------------------------------------
# The blocking defer -> answer -> tool-result round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_round_trip_multiple_select() -> None:
    queue = NeedsYouQueue()
    tool = QuestionTool(queue)
    task = asyncio.create_task(
        tool.execute(
            {
                "questions": [
                    {
                        "question": "Which colors do you want?",
                        "header": "colors",
                        "options": [
                            {"label": "Yellow", "description": "warm"},
                            {"label": "Blue", "description": "cool"},
                            {"label": "Red", "description": "hot"},
                        ],
                        "multiple": True,
                    }
                ]
            }
        )
    )
    await _wait_pending(queue)
    item = queue.pending[0]
    assert item.choices == ("Yellow", "Blue", "Red")
    assert item.reason == "colors"  # header -> reason

    # answer through the SAME seam serve.py / TUI use
    queue.answer(item.decision_id, "Yellow, Blue")
    result = await asyncio.wait_for(task, timeout=5)

    assert result.success is True
    assert result.error is None
    assert '"Which colors do you want?"="Yellow, Blue"' in result.output
    assert "Red" not in result.output  # only what the user selected


@pytest.mark.asyncio
async def test_execute_auto_defers_and_returns_without_waiting() -> None:
    queue = NeedsYouQueue()
    tool = QuestionTool(queue, mode=lambda: "auto")

    result = await asyncio.wait_for(
        tool.execute(
            {
                "questions": [
                    {
                        "question": "Which direction?",
                        "options": [{"label": "Local"}, {"label": "Upstream"}],
                    }
                ]
            }
        ),
        timeout=1,
    )

    assert result.success is True
    assert "Auto mode is continuing" in result.output
    item = queue.pending[0]
    assert item.reason == "Auto continues while this waits"
    queue.answer(item.decision_id, "Local")

    # Auto's late answer is delivered exactly once at the next model boundary.
    bridge = StepBoundaryBridge("root", SteeringQueue(), needs_you=queue)
    injected = await bridge.handle_event("provider:request", {"session_id": "root"})
    assert injected.action == "inject_context"
    assert "Local" in str(injected.context_injection)
    again = await bridge.handle_event("provider:request", {"session_id": "root"})
    assert again.action == "continue"


@pytest.mark.asyncio
async def test_execute_multiple_questions_any_order() -> None:
    queue = NeedsYouQueue()
    tool = QuestionTool(queue)
    task = asyncio.create_task(
        tool.execute(
            {
                "questions": [
                    {"question": "First?", "options": [{"label": "a"}]},
                    {"question": "Second?", "options": [{"label": "b"}]},
                ]
            }
        )
    )
    await _wait_pending(queue, 2)
    first, second = queue.pending
    # answer out of order — the tool still returns them in ASK order
    queue.answer(second.decision_id, "beta")
    queue.answer(first.decision_id, "alpha")
    result = await asyncio.wait_for(task, timeout=5)
    assert result.success is True
    assert result.output.index('"First?"="alpha"') < result.output.index('"Second?"="beta"')


@pytest.mark.asyncio
async def test_execute_dismiss_is_unanswered_and_continues() -> None:
    queue = NeedsYouQueue()
    tool = QuestionTool(queue)
    task = asyncio.create_task(
        tool.execute({"questions": [{"question": "Proceed?", "options": []}]})
    )
    await _wait_pending(queue)
    queue.dismiss(queue.pending[0].decision_id)
    result = await asyncio.wait_for(task, timeout=5)
    assert result.success is True  # never halts the turn
    assert '"Proceed?"="Unanswered"' in result.output


@pytest.mark.asyncio
async def test_execute_empty_questions_fails_cleanly() -> None:
    tool = QuestionTool(NeedsYouQueue())
    result = await tool.execute({"questions": []})
    assert result.success is False
    assert "questions" in result.output


@pytest.mark.asyncio
async def test_answered_question_is_not_reinjected_by_bridge() -> None:
    """After the tool returns the answer, the step-boundary bridge finds
    nothing to inject — the question answer is delivered ONCE (as tool result),
    never a second time as a next-turn instruction."""
    queue = NeedsYouQueue()
    tool = QuestionTool(queue)
    task = asyncio.create_task(
        tool.execute({"questions": [{"question": "Push?", "options": [{"label": "yes"}]}]})
    )
    await _wait_pending(queue)
    queue.answer(queue.pending[0].decision_id, "yes")
    await asyncio.wait_for(task, timeout=5)

    # the item is consumed, not left "answered"
    assert [item.status for item in queue.items] == ["consumed"]

    bridge = StepBoundaryBridge("root", SteeringQueue(), needs_you=queue)
    result = await bridge.handle_event("provider:request", {"session_id": "root"})
    assert result.action == "continue"  # no re-injection


@pytest.mark.asyncio
async def test_queue_full_dismisses_partial_defers() -> None:
    """A queue at its limit refuses the defer; the tool cleans up any partial
    defers and reports failure rather than leaking half-asked questions."""
    queue = NeedsYouQueue()
    # fill the queue to its cap with unrelated pending decisions
    for _ in range(queue._MAX_DECISIONS):  # noqa: SLF001 — white-box cap check
        queue.defer("prior decision")
    tool = QuestionTool(queue)
    result = await tool.execute({"questions": [{"question": "One more?"}]})
    assert result.success is False
    assert "could not ask question" in result.output
    # nothing extra was left parked beyond the pre-filled cap
    assert len(queue.pending) == queue._MAX_DECISIONS  # noqa: SLF001


# ---------------------------------------------------------------------------
# The targeted-consume helper the tool relies on
# ---------------------------------------------------------------------------


def test_queue_consume_only_transitions_answered() -> None:
    queue = NeedsYouQueue()
    item = queue.defer("q?", choices=("a",))
    # pending -> consume is a no-op
    assert queue.consume(item.decision_id) is None
    assert queue.pending[0].status == "pending"
    # answered -> consume transitions and returns the item
    queue.answer(item.decision_id, "a")
    consumed = queue.consume(item.decision_id)
    assert consumed is not None and consumed.status == "consumed"
    # idempotent / unknown ids are no-ops
    assert queue.consume(item.decision_id) is None
    assert queue.consume("decision-does-not-exist") is None
    # a consumed item is not re-picked by consume_answered
    assert queue.consume_answered() == ()
