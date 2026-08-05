"""Runtime ownership/persistence seam for same-session MCP changes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from amplifier_app_tui.kernel import mcp_config
from amplifier_app_tui.kernel.runtime import RealRuntime


class _Reconciler:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def add(self, name, spec, *, configured, previously_configured):  # noqa: ANN001
        self.calls.append(("add", name, spec, configured, previously_configured))
        return SimpleNamespace(ok=True, message="connected live")

    async def reload(self, name, spec, *, configured):  # noqa: ANN001
        self.calls.append(("reload", name, spec, configured))
        return SimpleNamespace(ok=True, message="reloaded live")

    async def remove(self, name, *, configured, previously_configured):  # noqa: ANN001
        self.calls.append(("remove", name, configured, previously_configured))
        return SimpleNamespace(ok=True, message="disconnected live")


def _runtime(tmp_path, monkeypatch: pytest.MonkeyPatch) -> tuple[RealRuntime, _Reconciler]:
    user_path = tmp_path / "user" / "mcp.json"
    monkeypatch.setattr(mcp_config, "mcp_config_path", lambda amplifier_home=None: user_path)
    runtime = RealRuntime(project_dir=tmp_path)
    runtime._resolved = SimpleNamespace(  # type: ignore[assignment]
        settings={},
        project_dir=tmp_path,
        mount_plan={"tools": [{"module": "tool-mcp"}]},
    )
    runtime._initialized = SimpleNamespace(coordinator=object())  # type: ignore[assignment]
    reconciler = _Reconciler()
    runtime._live_mcp = reconciler
    return runtime, reconciler


@pytest.mark.asyncio
async def test_add_persists_then_connects_a_proven_new_server_live(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, reconciler = _runtime(tmp_path, monkeypatch)

    ok, detail = await runtime.add_mcp_server("docs", "docs-server", ("--stdio",))

    assert ok is True
    assert detail == "mcp docs · connected live"
    assert reconciler.calls == [
        (
            "add",
            "docs",
            {"command": "docs-server", "args": ["--stdio"]},
            True,
            False,
        )
    ]
    saved = json.loads(mcp_config.mcp_config_path().read_text(encoding="utf-8"))
    assert saved["mcpServers"]["docs"]["command"] == "docs-server"


@pytest.mark.asyncio
async def test_higher_priority_project_definition_is_not_falsely_connected(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = tmp_path / ".amplifier" / "mcp.json"
    project_path.parent.mkdir(parents=True)
    project_path.write_text(
        json.dumps({"mcpServers": {"docs": {"command": "project-server"}}}),
        encoding="utf-8",
    )
    runtime, reconciler = _runtime(tmp_path, monkeypatch)

    ok, detail = await runtime.add_mcp_server("docs", "user-server")

    assert ok is False
    assert "still overrides it" in detail
    assert reconciler.calls == []


@pytest.mark.asyncio
async def test_reload_uses_the_effective_server_spec(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, reconciler = _runtime(tmp_path, monkeypatch)
    mcp_config.add_stdio_server(mcp_config.mcp_config_path(), "docs", "docs-server")

    ok, detail = await runtime.reload_mcp_server("docs")

    assert ok is True and detail == "mcp docs · reloaded live"
    assert reconciler.calls == [("reload", "docs", {"command": "docs-server"}, True)]


@pytest.mark.asyncio
async def test_remove_disconnects_only_after_effective_config_is_absent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, reconciler = _runtime(tmp_path, monkeypatch)
    mcp_config.add_stdio_server(mcp_config.mcp_config_path(), "docs", "docs-server")

    ok, detail = await runtime.remove_mcp_server("docs")

    assert ok is True and detail == "mcp docs · disconnected live"
    assert reconciler.calls == [("remove", "docs", False, True)]
    assert runtime._effective_mcp_servers() == {}


@pytest.mark.asyncio
async def test_project_owned_server_cannot_be_removed_from_the_global_command(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = tmp_path / ".amplifier" / "mcp.json"
    project_path.parent.mkdir(parents=True)
    project_path.write_text(
        json.dumps({"mcpServers": {"docs": {"command": "project-server"}}}),
        encoding="utf-8",
    )
    runtime, reconciler = _runtime(tmp_path, monkeypatch)

    ok, detail = await runtime.remove_mcp_server("docs")

    assert ok is False
    assert "configured by project, environment, or bundle scope" in detail
    assert reconciler.calls == []


@pytest.mark.asyncio
async def test_list_reports_effective_inline_scope(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _reconciler = _runtime(tmp_path, monkeypatch)
    runtime._resolved.mount_plan["tools"][0]["config"] = {  # type: ignore[union-attr]
        "servers": {"docs": {"url": "https://docs.example.test/mcp", "type": "http"}}
    }

    assert await runtime.mcp_servers() == {"docs": "http · https://docs.example.test/mcp"}
