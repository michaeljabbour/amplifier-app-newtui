from __future__ import annotations

import asyncio

from amplifier_app_tui.kernel.skill_activation import (
    activate_skill_result,
    parse_skill_request,
    skill_payload,
)


class _Context:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def add_message(self, message: dict) -> None:
        self.messages.append(message)


class _Coordinator:
    def __init__(self, context: object | None = None) -> None:
        self.context = context
        self.session_state: dict[str, object] = {}

    def get(self, mount: str):
        return self.context if mount == "context" else None


def test_parse_skill_request_preserves_arguments() -> None:
    request = parse_skill_request("  council   inspect this exact target  ")
    assert request.name == "council"
    assert request.arguments == "inspect this exact target"
    assert skill_payload(request) == {
        "skill_name": "council",
        "arguments": "inspect this exact target",
    }
    assert skill_payload(parse_skill_request("simplify")) == {"skill_name": "simplify"}


def test_inline_skill_enters_next_turn_context_once() -> None:
    context = _Context()
    coordinator = _Coordinator(context)
    request = parse_skill_request("simplify src/app.py")
    activation = asyncio.run(
        activate_skill_result(
            coordinator,
            request,
            {"content": "# simplify\n\nRemove accidental complexity."},
        )
    )

    assert activation.context_added is True
    assert activation.kind == "inline"
    assert len(context.messages) == 1
    assert "Remove accidental complexity" in context.messages[0]["content"]
    assert "src/app.py" in context.messages[0]["content"]
    assert coordinator.session_state["ui.loaded_skills"] == [
        {"name": "simplify", "arguments": "src/app.py", "kind": "inline"}
    ]


def test_fork_result_is_visible_to_the_parent_next_turn() -> None:
    context = _Context()
    activation = asyncio.run(
        activate_skill_result(
            _Coordinator(context),
            parse_skill_request("council proposal.md"),
            {
                "context": "fork",
                "message": "The council completed.\n\nPASS with two notes.",
                "response": "PASS with two notes.",
            },
        )
    )

    assert activation.context_added is True
    assert activation.kind == "fork"
    assert "forked session" in context.messages[0]["content"]
    assert "PASS with two notes" in context.messages[0]["content"]


def test_missing_context_is_an_honest_partial_activation() -> None:
    activation = asyncio.run(
        activate_skill_result(
            _Coordinator(),
            parse_skill_request("simplify"),
            {"content": "body"},
        )
    )
    assert activation.display == "body"
    assert activation.context_added is False
    assert activation.reason == "live context is unavailable"
