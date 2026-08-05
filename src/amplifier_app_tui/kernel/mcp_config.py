"""MCP server config store — the file behind ``/mcp add|remove``.

``tool-mcp`` reads MCP server definitions from ``~/.amplifier/mcp.json``
(and project ``./.amplifier/mcp.json``), top-level key ``mcpServers``,
and connects to each at session start. This module is the small
read/modify/write layer over that file (mirroring app-cli's
``McpConfigStore``): atomic writes, never raises on a bad file.

The runtime persists through this layer, then reconciles the effective
configuration with the current session. New servers can connect immediately;
boot-owned aggregate connections remain an explicit restart boundary unless
the mounted module publishes a per-server ``mcp.reconcile`` capability.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

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


def read_effective_servers(
    *,
    project_dir: Path,
    user_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    inline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the same MCP config scopes the pinned ``tool-mcp`` reads.

    Priority is user < project < ``AMPLIFIER_MCP_CONFIG`` < inline.  The
    command surface uses this snapshot *before* writing a change so the live
    reconciler can distinguish a genuinely new server (safe for a targeted
    connection) from one owned by the boot-time aggregate manager.
    """
    merged: dict[str, Any] = {}
    merged.update(read_servers(user_path or mcp_config_path()))
    merged.update(read_servers(project_dir / ".amplifier" / "mcp.json"))
    env = environ if environ is not None else os.environ
    env_path = env.get("AMPLIFIER_MCP_CONFIG")
    if env_path:
        merged.update(read_servers(Path(env_path).expanduser()))
    if isinstance(inline, Mapping):
        servers = inline.get("servers")
        if isinstance(servers, Mapping):
            merged.update({str(name): spec for name, spec in servers.items()})
    return merged


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
    "read_effective_servers",
    "read_servers",
    "remove_server",
]
