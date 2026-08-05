"""Native MCP prompt discovery/execution contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from amplifier_app_tui.kernel.mcp_prompts import (
    discover_mcp_prompts,
    execute_mcp_prompt,
    prompt_command,
)


class _Coordinator:
    def __init__(self, tools: dict[str, Any] | None = None) -> None:
        self.tools = tools or {}

    def get(self, category: str) -> dict[str, Any]:
        assert category == "tools"
        return self.tools


class _Prompt:
    def __init__(
        self,
        server: str = "github",
        prompt: str = "triage",
        *,
        description: str = "Triage one issue",
        properties: tuple[str, ...] = ("issue",),
        required: tuple[str, ...] = ("issue",),
        body: str = "Triage it",
    ) -> None:
        self.server_name = server
        self.prompt_name = prompt
        self.description = description
        self.input_schema = {
            "type": "object",
            "properties": {name: {"type": "string"} for name in properties},
            "required": list(required),
        }
        self.body = body
        self.inputs: list[dict[str, Any]] = []

    async def execute(self, input: dict[str, Any]) -> Any:
        self.inputs.append(input)
        return SimpleNamespace(success=True, output={"messages": self.body})


def test_discovery_uses_public_prompt_attributes_only_and_is_deterministic() -> None:
    later = _Prompt("Docs Server", "Review.Code", description="Review docs")
    earlier = _Prompt("alpha", "first", description="First")
    ordinary_tool = SimpleNamespace(server_name="alpha", execute=lambda _input: None)
    coordinator = _Coordinator({"z": later, "ordinary": ordinary_tool, "a": earlier})

    prompts = discover_mcp_prompts(coordinator)

    assert [(item.command, item.server, item.prompt) for item in prompts] == [
        ("/alpha:first", "alpha", "first"),
        ("/docsserver:reviewcode", "Docs Server", "Review.Code"),
    ]
    assert prompts[1].description == "Review docs"
    assert prompt_command("Docs Server", "Review.Code") == "/docsserver:reviewcode"


@pytest.mark.asyncio
async def test_execute_single_argument_uses_native_wrapper_output() -> None:
    wrapper = _Prompt(body="[user]\nTriage #42")
    coordinator = _Coordinator({"mcp_github_prompt_triage": wrapper})

    result = await execute_mcp_prompt(coordinator, "github", "triage", "#42")

    assert result == (True, "[user]\nTriage #42")
    assert wrapper.inputs == [{"issue": "#42"}]


@pytest.mark.asyncio
async def test_execute_supports_json_and_multi_key_value_arguments() -> None:
    wrapper = _Prompt(
        properties=("issue", "tone"),
        required=("issue", "tone"),
    )
    coordinator = _Coordinator({"prompt": wrapper})

    assert (await execute_mcp_prompt(coordinator, "github", "triage", 'issue="#42" tone=brief'))[0]
    assert wrapper.inputs[-1] == {"issue": "#42", "tone": "brief"}
    assert (
        await execute_mcp_prompt(coordinator, "github", "triage", '{"issue":"#7","tone":"full"}')
    )[0]
    assert wrapper.inputs[-1] == {"issue": "#7", "tone": "full"}


@pytest.mark.asyncio
async def test_required_argument_errors_do_not_call_wrapper() -> None:
    wrapper = _Prompt()

    ok, detail = await execute_mcp_prompt(_Coordinator({"prompt": wrapper}), "github", "triage", "")

    assert ok is False
    assert detail == "Required MCP prompt arguments: issue"
    assert wrapper.inputs == []


@pytest.mark.asyncio
async def test_invocation_re_resolves_reloaded_wrapper_instead_of_caching() -> None:
    old = _Prompt(body="old")
    coordinator = _Coordinator({"prompt": old})
    assert await execute_mcp_prompt(coordinator, "github", "triage", "#1") == (True, "old")

    new = _Prompt(body="new")
    coordinator.tools["prompt"] = new
    assert await execute_mcp_prompt(coordinator, "github", "triage", "#2") == (True, "new")

    assert old.inputs == [{"issue": "#1"}]
    assert new.inputs == [{"issue": "#2"}]


@pytest.mark.asyncio
async def test_removed_and_failed_wrappers_fail_loudly() -> None:
    coordinator = _Coordinator()
    assert await execute_mcp_prompt(coordinator, "github", "triage", "#1") == (
        False,
        "MCP prompt is no longer mounted: /github:triage",
    )

    class _Failed(_Prompt):
        async def execute(self, input: dict[str, Any]) -> Any:
            return SimpleNamespace(success=False, output=None, error="server unavailable")

    ok, detail = await execute_mcp_prompt(
        _Coordinator({"prompt": _Failed()}), "github", "triage", "#1"
    )
    assert ok is False
    assert "server unavailable" in detail
