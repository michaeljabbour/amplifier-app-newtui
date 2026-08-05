"""Turn a successful ``load_skill`` tool result into live session context.

The command palette invokes ``load_skill`` outside the orchestrator's normal
tool loop.  Rendering the returned body in the transcript is therefore not
enough: unless the body is also added to the mounted context, the next model
request cannot follow it.  This module owns that small, testable bridge.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SkillRequest:
    """Canonical skill command input."""

    name: str
    arguments: str = ""


@dataclass(frozen=True)
class SkillActivation:
    """The display payload and whether it entered live model context."""

    display: str
    context_added: bool
    kind: str = "inline"
    reason: str = ""


def parse_skill_request(value: str) -> SkillRequest:
    """Split ``NAME [ARGS]`` once, preserving the user's argument string."""
    parts = value.strip().split(maxsplit=1)
    if not parts:
        return SkillRequest("")
    return SkillRequest(parts[0], parts[1] if len(parts) == 2 else "")


def skill_payload(request: SkillRequest) -> dict[str, Any]:
    """Build the current tool-skills request without an empty arguments key."""
    payload: dict[str, Any] = {"skill_name": request.name}
    if request.arguments:
        payload["arguments"] = request.arguments
    return payload


def _result_text(output: Any) -> tuple[str, str]:
    """Return ``(text, kind)`` for inline and fork tool result shapes."""
    if isinstance(output, str):
        return (output, "inline")
    if not isinstance(output, dict):
        return ("", "inline")
    if output.get("content") is not None:
        return (str(output.get("content") or ""), "inline")
    # Fork skills execute immediately.  Current tool-skills returns both a
    # human-ready message and the raw child response; prefer the former while
    # accepting older/newer result variants.
    for key in ("message", "response", "output"):
        if output.get(key) is not None:
            return (str(output.get(key) or ""), "fork")
    return ("", "fork" if output.get("context") == "fork" else "inline")


async def activate_skill_result(
    coordinator: Any,
    request: SkillRequest,
    output: Any,
) -> SkillActivation:
    """Insert a loaded skill/result exactly once into the live context.

    Inline skill instructions and completed fork-skill results both need a
    model-visible record because this invocation happened outside the normal
    orchestrator tool-call path.  The transcript remains a separate UI
    concern; this function only owns model context and a small session ledger.
    """
    text, kind = _result_text(output)
    if not text:
        return SkillActivation("", False, kind, "skill returned no content")

    getter = getattr(coordinator, "get", None)
    context = getter("context") if callable(getter) else None
    add_message = getattr(context, "add_message", None)
    if not callable(add_message):
        return SkillActivation(text, False, kind, "live context is unavailable")

    label = (
        f"Skill /{request.name} completed in a forked session. Its result follows:"
        if kind == "fork"
        else f"Skill /{request.name} is active for this session. Follow these instructions:"
    )
    if request.arguments:
        label += f"\nInvocation arguments: {request.arguments}"
    message = {
        "role": "system",
        "content": f"{label}\n\n{text}",
        "metadata": {
            # context-simple replaces ordinary stored system messages whenever
            # the bundle has a dynamic system-prompt factory.  It deliberately
            # preserves hook-origin system messages, so palette-driven skill
            # activation must use that public marker or the skill disappears
            # from the very next provider request.
            "source": "hook",
            "injected_by": "amplifier-tui-skill",
            "skill_name": request.name,
            "skill_kind": kind,
        },
    }
    try:
        pending = add_message(message)
        if inspect.isawaitable(pending):
            await pending
    except Exception as error:  # noqa: BLE001 - surface an honest partial activation
        return SkillActivation(text, False, kind, f"could not add skill to context: {error}")

    state = getattr(coordinator, "session_state", None)
    if isinstance(state, dict):
        loaded = state.setdefault("ui.loaded_skills", [])
        if isinstance(loaded, list):
            loaded.append(
                {
                    "name": request.name,
                    "arguments": request.arguments,
                    "kind": kind,
                }
            )
    return SkillActivation(text, True, kind)


__all__ = [
    "SkillActivation",
    "SkillRequest",
    "activate_skill_result",
    "parse_skill_request",
    "skill_payload",
]
