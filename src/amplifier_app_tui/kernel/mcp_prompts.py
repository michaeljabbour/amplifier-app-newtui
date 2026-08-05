"""Native MCP prompt discovery and execution.

``tool-mcp`` mounts prompts into Amplifier's ordinary ``tools`` registry as
``MCPPromptWrapper`` objects.  This bridge deliberately depends only on that
wrapper's public, duck-typed surface: ``server_name``, ``prompt_name``,
``description``, ``input_schema`` and ``execute``.  Prompt bodies are never
cached here.  Each invocation re-resolves the current mounted wrapper so a
live MCP reload cannot leave a slash command bound to a stopped client.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MCPPromptInfo:
    """One mounted MCP prompt exposed as a namespaced slash command."""

    command: str
    server: str
    prompt: str
    description: str = ""


def _tools(coordinator: Any) -> Mapping[str, Any]:
    try:
        mounted = coordinator.get("tools")
    except Exception:  # noqa: BLE001 - discovery must not make session boot fail
        return {}
    return mounted if isinstance(mounted, Mapping) else {}


def _token(value: Any) -> str:
    """App-CLI-compatible slash token for one MCP server/prompt name."""

    return "".join(
        character for character in str(value) if character.isalnum() or character in {"-", "_"}
    )[:128]


def prompt_command(server: Any, prompt: Any) -> str:
    """Return ``/server:prompt`` or ``""`` when either token is unusable."""

    server_token = _token(server)
    prompt_token = _token(prompt)
    if not server_token or not prompt_token:
        return ""
    return f"/{server_token}:{prompt_token}".lower()


def discover_mcp_prompts(coordinator: Any) -> tuple[MCPPromptInfo, ...]:
    """Describe live native prompt wrappers without retaining the wrappers.

    All descriptors are returned, including normalized-command collisions;
    the command registry owns the user-visible first-registration policy and
    collision diagnostics.
    """

    discovered: list[tuple[str, str, str, str, str]] = []
    for mount_name, wrapper in _tools(coordinator).items():
        try:
            server = str(getattr(wrapper, "server_name", "") or "")
            prompt = str(getattr(wrapper, "prompt_name", "") or "")
            execute = getattr(wrapper, "execute", None)
        except Exception:  # noqa: BLE001 - one malformed wrapper must not hide peers
            continue
        command = prompt_command(server, prompt)
        if not command or not callable(execute):
            continue
        try:
            description = str(getattr(wrapper, "description", "") or "MCP prompt")
        except Exception:  # noqa: BLE001 - optional display metadata
            description = "MCP prompt"
        discovered.append((command, server, prompt, description, str(mount_name)))

    discovered.sort(key=lambda item: (item[0], item[1], item[2], item[4]))
    return tuple(
        MCPPromptInfo(
            command=command,
            server=server,
            prompt=prompt,
            description=description,
        )
        for command, server, prompt, description, _mount_name in discovered
    )


def _resolve_wrapper(coordinator: Any, server: str, prompt: str) -> Any | None:
    """Resolve the current exact wrapper, deterministically, at call time."""

    matches: list[tuple[str, Any]] = []
    for mount_name, wrapper in _tools(coordinator).items():
        try:
            if (
                str(getattr(wrapper, "server_name", "")) == server
                and str(getattr(wrapper, "prompt_name", "")) == prompt
                and callable(getattr(wrapper, "execute", None))
            ):
                matches.append((str(mount_name), wrapper))
        except Exception:  # noqa: BLE001 - ignore a broken unrelated mount
            continue
    return min(matches, key=lambda item: item[0])[1] if matches else None


def _parse_arguments(wrapper: Any, args: str) -> dict[str, Any] | str:
    """Parse app-CLI-compatible MCP prompt arguments or return an error."""

    try:
        schema = getattr(wrapper, "input_schema", {})
    except Exception as error:  # noqa: BLE001 - public metadata can be provider-backed
        return f"Could not read MCP prompt arguments: {error}"
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    required = schema.get("required", ()) if isinstance(schema, Mapping) else ()
    if not isinstance(properties, Mapping):
        properties = {}
    if not isinstance(required, (list, tuple, set, frozenset)):
        required = ()

    text = args.strip()
    if not text:
        missing = [str(name) for name in required if name in properties]
        return f"Required MCP prompt arguments: {', '.join(missing)}" if missing else {}
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            return f"Invalid MCP prompt JSON: {error}"
        return value if isinstance(value, dict) else "MCP prompt JSON must be an object."
    if len(properties) == 1:
        return {str(next(iter(properties))): text}
    try:
        tokens = shlex.split(text)
    except ValueError as error:
        return f"Invalid MCP prompt arguments: {error}"
    values: dict[str, Any] = {}
    for token in tokens:
        name, separator, value = token.partition("=")
        if not separator or name not in properties:
            return "Use key=value arguments: " + ", ".join(str(name) for name in properties)
        values[name] = value
    missing = [str(name) for name in required if not values.get(str(name))]
    return f"Required MCP prompt arguments: {', '.join(missing)}" if missing else values


async def execute_mcp_prompt(
    coordinator: Any,
    server: str,
    prompt: str,
    args: str = "",
) -> tuple[bool, str]:
    """Fetch one native prompt body from the currently mounted wrapper."""

    wrapper = _resolve_wrapper(coordinator, server, prompt)
    command = prompt_command(server, prompt) or f"/{server}:{prompt}"
    if wrapper is None:
        return (False, f"MCP prompt is no longer mounted: {command}")
    parsed = _parse_arguments(wrapper, args)
    if isinstance(parsed, str):
        return (False, parsed)
    try:
        result = wrapper.execute(parsed)
        if isinstance(result, Awaitable):
            result = await result
    except Exception as error:  # noqa: BLE001 - native failure is a command result
        return (False, f"MCP prompt {command} failed: {error}")
    if not bool(getattr(result, "success", False)):
        error = getattr(result, "error", None) or getattr(result, "output", None)
        return (False, f"MCP prompt {command} failed: {error or 'tool reported failure'}")
    output = getattr(result, "output", None)
    messages = output.get("messages") if isinstance(output, Mapping) else None
    if not isinstance(messages, str) or not messages.strip():
        return (False, f"MCP prompt {command} returned no prompt messages.")
    return (True, messages)


__all__ = [
    "MCPPromptInfo",
    "discover_mcp_prompts",
    "execute_mcp_prompt",
    "prompt_command",
]
