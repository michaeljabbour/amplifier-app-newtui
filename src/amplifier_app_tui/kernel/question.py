"""Host-provided ``question`` tool: ask the USER without freezing Auto mode.

Re-expression (behavior only, no opencode TS vendored) of opencode's
``question`` tool. The model calls it to ask the user one or more structured
questions (multiple-choice ± free text).  Interactive modes pause until the
user answers and return those answers as the tool result.  Auto mode parks the
questions and returns immediately, so the model can continue independent work;
an answer is injected at a later provider boundary through the existing
needs-you/steering bridge.

Routing (the point of this slice): the tool goes through the EXISTING
deferred-decision / needs-you plumbing so BOTH clients (the in-process Textual
TUI and any protocol/Rust front-end) get the UX via the SAME path they already
speak -- no new protocol op, ``kernel/serve.py`` untouched:

- ask   -> :meth:`NeedsYouQueue.defer` (fires ``RealRuntime._decision_deferred``
           -> ``Notification(level="decision", ...)`` the clients already render)
- reply -> :meth:`NeedsYouQueue.answer` -- the SAME entry point ``serve.py``'s
           ``{"op":"decision"}`` op and the TUI's ``app_support.apply_decision``
           both call
- outside Auto, the tool BLOCKS in :meth:`execute` until every deferred question
  is answered (or dismissed), then consumes each answer so the
  ``StepBoundaryBridge`` never re-injects it;
- in Auto, the tool returns a successful "deferred" result immediately and
  leaves each item pending.  Work continues, and a later answer is injected by
  ``StepBoundaryBridge`` exactly once.

Cancellation (app philosophy -- deny-and-continue, replacing the donor's
``Effect.orDie``): a dismissed question resolves to ``"Unanswered"`` and the turn
proceeds; it never halts. An interrupted turn cancels the ``execute`` task at the
``await`` and the listener is removed in ``finally``.

Layering (ADR-0007): ``kernel/`` is the only layer allowed to import
amplifier-core. This satisfies the amplifier_core Tool protocol
(``name`` / ``description`` / ``input_schema`` / ``execute``) exactly like
``kernel/bundle_summon.py``'s ``LoadBundleTool`` and mounts onto the live
coordinator's ``tools`` point the same way.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..model.queues import NeedsYouQueue

logger = logging.getLogger(__name__)

QUESTION_TOOL_NAME = "question"
"""Mount name and model-facing tool name."""

DESCRIPTION = (
    "Ask the user one or more structured questions mid-turn. Outside Auto mode, "
    "wait for the answer before continuing. In Auto mode, the questions are "
    "deferred and independent work continues; answers arrive when available. "
    "Use it to gather preferences, clarify ambiguous "
    "instructions, get a decision on an implementation choice, or offer a "
    "direction. Each question may list `options` (label + short description); set "
    "`multiple` true to allow selecting more than one. A free-text answer is "
    "always available, so do NOT add an 'Other' / catch-all option yourself. If "
    "you recommend an option, make it the first one and append '(Recommended)'. "
    "The user's answers come back as the tool result."
)


def _clean(value: object, limit: int) -> str:
    """Collapse whitespace/control chars and cap length."""
    return " ".join(str(value).split())[:limit]


@dataclass(frozen=True)
class QuestionOption:
    """One choice: display ``label`` + optional ``description``."""

    label: str
    description: str = ""


@dataclass(frozen=True)
class QuestionPrompt:
    """One question the model asks the user."""

    question: str
    header: str = ""
    options: tuple[QuestionOption, ...] = ()
    multiple: bool = False
    custom: bool = True

    @property
    def labels(self) -> tuple[str, ...]:
        """Option labels -> the needs-you decision's actionable chips."""
        return tuple(option.label for option in self.options if option.label)

    @property
    def descriptions(self) -> tuple[str, ...]:
        """Option descriptions aligned index-for-index to :attr:`labels`
        (blank string where an option carried no description). The donor's
        per-option help line -- carried through so BOTH clients can render it."""
        return tuple(option.description for option in self.options if option.label)


def parse_questions(raw: object) -> list[QuestionPrompt]:
    """Parse the tool input's ``questions`` into :class:`QuestionPrompt` objects.

    Tolerant of shape drift (an LLM is the caller): a bare dict is accepted as a
    single question, non-dict entries and blank questions are skipped, and an
    option missing a label is dropped. String options are accepted as bare
    labels.
    """
    if isinstance(raw, dict):
        raw = [raw]
    prompts: list[QuestionPrompt] = []
    if not isinstance(raw, (list, tuple)):
        return prompts
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        question = _clean(entry.get("question", ""), 4096)
        if not question:
            continue
        options: list[QuestionOption] = []
        for opt in entry.get("options") or ():
            if isinstance(opt, dict):
                label = _clean(opt.get("label", ""), 200)
                if label:
                    options.append(QuestionOption(label, _clean(opt.get("description", ""), 500)))
            elif isinstance(opt, str):
                label = _clean(opt, 200)
                if label:
                    options.append(QuestionOption(label))
        prompts.append(
            QuestionPrompt(
                question=question,
                header=_clean(entry.get("header", ""), 200),
                options=tuple(options),
                multiple=bool(entry.get("multiple", False)),
                custom=entry.get("custom", True) is not False,
            )
        )
    return prompts


def format_output(pairs: Sequence[tuple[str, str]]) -> str:
    """The donor's model-facing result string.

    ``User has answered your questions: "<q>"="<a>", .... You can now continue
    with the user's answers in mind.`` An empty answer renders ``"Unanswered"``.
    """
    body = ", ".join(f'"{question}"="{answer or "Unanswered"}"' for question, answer in pairs)
    return (
        f"User has answered your questions: {body}. "
        "You can now continue with the user's answers in mind."
    )


def format_deferred_output(prompts: Sequence[QuestionPrompt]) -> str:
    """Model-facing Auto-mode result: the questions are parked, not failed."""
    questions = ", ".join(f'"{prompt.question}"' for prompt in prompts)
    return (
        f"Questions deferred to the user: {questions}. Auto mode is continuing; "
        "do not wait or repeat the questions. Continue independent work now. "
        "The user's answers will be injected when available."
    )


class QuestionTool:
    """Structured questions over needs-you, nonblocking in Auto mode.

    Duck-typed over :class:`~amplifier_app_tui.model.queues.NeedsYouQueue`, so
    it unit-tests with a bare queue -- no session, no model, no network.
    """

    def __init__(
        self,
        needs_you: NeedsYouQueue,
        *,
        mode: Callable[[], str] | None = None,
    ) -> None:
        self._needs_you = needs_you
        self._mode = mode

    def _auto_continues(self) -> bool:
        if self._mode is None:
            return False
        try:
            return self._mode() == "auto"
        except Exception:  # noqa: BLE001 - a broken mode source must not change semantics
            return False

    @property
    def name(self) -> str:
        return QUESTION_TOOL_NAME

    @property
    def description(self) -> str:
        return DESCRIPTION

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Questions to ask the user.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The complete question to ask.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Very short label for the question (<=30 chars).",
                            },
                            "options": {
                                "type": "array",
                                "description": "Available choices.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Display text (1-5 words, concise).",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Explanation of the choice.",
                                        },
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multiple": {
                                "type": "boolean",
                                "description": "Allow selecting more than one choice.",
                            },
                            "custom": {
                                "type": "boolean",
                                "description": "Allow typing a custom answer (default true).",
                            },
                        },
                        "required": ["question"],
                    },
                }
            },
            "required": ["questions"],
        }

    async def execute(self, input: dict[str, Any]) -> Any:  # noqa: A002 -- Tool protocol arg name
        from amplifier_core.models import ToolResult

        prompts = parse_questions((input or {}).get("questions"))
        if not prompts:
            message = (
                "question tool requires a non-empty 'questions' array "
                "(each item needs a 'question')."
            )
            return ToolResult(success=False, error={"message": message}, output=message)

        auto_continues = self._auto_continues()
        loop = asyncio.get_running_loop()
        index_by_id: dict[str, int] = {}
        try:
            for i, prompt in enumerate(prompts):
                reason = prompt.header
                if auto_continues:
                    reason = (
                        f"{reason} · Auto continues while this waits"
                        if reason
                        else "Auto continues while this waits"
                    )
                item = self._needs_you.defer(
                    prompt.question,
                    reason=reason,
                    choices=prompt.labels,
                    descriptions=prompt.descriptions,
                    multiple=prompt.multiple,
                    custom=prompt.custom,
                )
                index_by_id[item.decision_id] = i
        except ValueError as error:
            for decision_id in index_by_id:
                try:
                    self._needs_you.dismiss(decision_id)
                except (KeyError, ValueError):
                    pass
            message = f"could not ask question: {error}"
            return ToolResult(success=False, error={"message": message}, output=message)

        if auto_continues:
            # Leave items pending.  Their eventual answers are consumed by the
            # provider-boundary bridge as context, while this tool result lets
            # the current loop keep making progress immediately.
            return ToolResult(success=True, output=format_deferred_output(prompts))

        answers: dict[int, str] = {}
        done = asyncio.Event()

        def _resolve() -> None:
            # Fires on whichever loop called needs_you.answer/dismiss (the UI loop
            # via apply_decision, or the serve loop via the decision op), so it
            # bridges back to the tool's loop with call_soon_threadsafe.
            for item in self._needs_you.items:
                idx = index_by_id.get(item.decision_id)
                if idx is None or idx in answers:
                    continue
                if item.status == "answered":
                    answers[idx] = item.answer
                elif item.status in ("dismissed", "consumed"):
                    answers[idx] = ""  # dismissed -> Unanswered; the turn continues
            if len(answers) >= len(index_by_id):
                loop.call_soon_threadsafe(done.set)

        remove = self._needs_you.add_listener(_resolve)
        try:
            _resolve()  # an answer may have raced in before we subscribed
            await done.wait()
        finally:
            remove()
            # Consume every answered item so consume_answered() (the step-boundary
            # bridge) never RE-injects the answer as a next-turn instruction. Safe
            # here: no provider:request runs mid tool-execution, so the bridge
            # cannot have consumed them first. No-op for dismissed items.
            for decision_id in index_by_id:
                self._needs_you.consume(decision_id)

        pairs = [(prompts[i].question, answers.get(i, "")) for i in range(len(prompts))]
        return ToolResult(success=True, output=format_output(pairs))


__all__ = [
    "DESCRIPTION",
    "QUESTION_TOOL_NAME",
    "QuestionOption",
    "QuestionPrompt",
    "QuestionTool",
    "format_deferred_output",
    "format_output",
    "parse_questions",
]
