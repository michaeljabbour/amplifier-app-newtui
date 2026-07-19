"""MCP server config store (``kernel/mcp_config.py``) against tmp_path."""

from __future__ import annotations

import json
from pathlib import Path

from amplifier_app_newtui.kernel import mcp_config


def test_add_read_remove_stdio_server(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    mcp_config.add_stdio_server(path, "postgres", "npx", ("-y", "@mcp/postgres"))
    servers = mcp_config.read_servers(path)
    assert servers == {"postgres": {"command": "npx", "args": ["-y", "@mcp/postgres"]}}
    # File uses the canonical top-level key.
    assert json.loads(path.read_text())["mcpServers"]

    assert mcp_config.remove_server(path, "postgres") is True
    assert mcp_config.read_servers(path) == {}
    assert mcp_config.remove_server(path, "postgres") is False


def test_add_preserves_existing_servers(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    mcp_config.add_stdio_server(path, "a", "cmd-a")
    mcp_config.add_stdio_server(path, "b", "cmd-b", ("--flag",))
    servers = mcp_config.read_servers(path)
    assert set(servers) == {"a", "b"}
    assert servers["a"] == {"command": "cmd-a"}  # no args key when none given


def test_read_servers_missing_or_malformed(tmp_path: Path) -> None:
    assert mcp_config.read_servers(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert mcp_config.read_servers(bad) == {}


def test_describe_server_variants() -> None:
    assert "stdio" in mcp_config.describe_server({"command": "npx", "args": ["x"]})
    assert "deepwiki" not in mcp_config.describe_server({"command": "npx"})
    assert "http" in mcp_config.describe_server({"url": "https://x/mcp"})
    assert mcp_config.describe_server("garbage") == "?"
