"""MCP server config store — the file behind ``/mcp add|remove``.

``tool-mcp`` reads MCP server definitions from ``~/.amplifier/mcp.json``
(and project ``./.amplifier/mcp.json``), top-level key ``mcpServers``,
and connects to each at session start. This module is the small
read/modify/write layer over that file (mirroring app-cli's
``McpConfigStore``): atomic writes, never raises on a bad file.

Servers connect when the session mounts tool-mcp, so edits here take
effect on the NEXT session — ``/mcp`` says so rather than pretending a
live reload exists (tool-mcp exposes none).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_KEY = "mcpServers"


def mcp_config_path(amplifier_home: Path | None = None) -> Path:
    return (amplifier_home or (Path.home() / ".amplifier")) / "mcp.json"


def read_config(path: Path) -> dict[str, Any]:
    """The full mcp.json document (``{}`` when missing/malformed)."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_servers(path: Path) -> dict[str, Any]:
    """The ``mcpServers`` mapping (name → server spec)."""
    servers = read_config(path).get(_KEY)
    return servers if isinstance(servers, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def add_stdio_server(path: Path, name: str, command: str, args: tuple[str, ...] = ()) -> None:
    """Add/replace a stdio MCP server (``command`` + ``args``)."""
    data = read_config(path)
    servers = data.get(_KEY)
    if not isinstance(servers, dict):
        servers = {}
        data[_KEY] = servers
    spec: dict[str, Any] = {"command": command}
    if args:
        spec["args"] = list(args)
    servers[name] = spec
    _write(path, data)


def remove_server(path: Path, name: str) -> bool:
    """Drop a server by name; True when it existed."""
    data = read_config(path)
    servers = data.get(_KEY)
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    if not servers:
        data.pop(_KEY, None)
    _write(path, data)
    return True


def describe_server(spec: Any) -> str:
    """A one-line summary of a server spec for ``/mcp list``."""
    if not isinstance(spec, dict):
        return "?"
    if spec.get("command"):
        args = " ".join(str(a) for a in spec.get("args", []) or [])
        return f"stdio · {spec['command']} {args}".strip()
    if spec.get("url"):
        return f"{spec.get('type', 'http')} · {spec['url']}"
    return "?"


__all__ = [
    "add_stdio_server",
    "describe_server",
    "mcp_config_path",
    "read_config",
    "read_servers",
    "remove_server",
]
