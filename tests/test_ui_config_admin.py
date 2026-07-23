"""``/config`` slash-command controller (``ui/config_admin``).

Fake host + adapter over a real
:class:`~amplifier_app_newtui.model.config.SessionConfigState`, so the
show/toggle/set/diff/save round-trips are exercised end-to-end with no
Textual and no live session (mirrors ``test_ui_directory_admin``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from amplifier_app_newtui.kernel import config_ops
from amplifier_app_newtui.model.blocks import Answer
from amplifier_app_newtui.model.config import (
    ConfigSnapshotView,
    default_config_state,
)
from amplifier_app_newtui.ui.config_admin import manage


class FakeConfigAdapter:
    def __init__(self, *, home: Path, project_dir: Path) -> None:
        self.state = default_config_state("anchors")
        self._home = home
        self._project_dir = project_dir

    async def config_view(self) -> ConfigSnapshotView:
        return ConfigSnapshotView.of(self.state)

    async def config_toggle(self, category: str, name: str, enable: bool):
        return self.state.toggle(category, name, enable=enable)

    async def config_set(self, path: str, value: str):
        return self.state.set_value(path, value)

    async def config_diff(self):
        return self.state.diff()

    async def config_save(self, scope: str):
        return config_ops.save_config(
            self.state, scope=scope, project_dir=self._project_dir, home=self._home
        )


class FakeAllocator:
    def __init__(self) -> None:
        self._n = 0

    def next_id(self) -> str:
        self._n += 1
        return f"b{self._n}"


class FakeHost:
    def __init__(self, tmp_path: Path) -> None:
        self.adapter = FakeConfigAdapter(home=tmp_path, project_dir=tmp_path)
        self.allocator = FakeAllocator()
        self.blocks: list[Any] = []
        self.notices: list[str] = []

    def append_block(self, block: Any) -> None:
        self.blocks.append(block)

    def show_notice(self, text: str, duration: float | None = None) -> None:
        self.notices.append(text)


def _text(block: Answer) -> str:
    return "".join(s.text for s in block.spans)


@pytest.mark.asyncio
async def test_help_posts_subcommand_listing(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    await manage(host, "")
    assert len(host.blocks) == 1
    assert "save" in _text(host.blocks[0])


@pytest.mark.asyncio
async def test_show_posts_full_config(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    await manage(host, "show")
    text = _text(host.blocks[0])
    assert "tools" in text and "providers" in text


@pytest.mark.asyncio
async def test_toggle_round_trips_and_reposts_category(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    await manage(host, "tools disable bash")
    # Notice confirms the toggle; a refreshed tools view is re-posted.
    assert host.notices == ["\u2713 Disabled bash"]
    assert len(host.blocks) == 1
    text = _text(host.blocks[0])
    assert "bash" in text and "\u25cb " in text  # hollow glyph = disabled
    item = host.adapter.state.find("tools", "bash")
    assert item is not None and item.enabled is False


@pytest.mark.asyncio
async def test_toggle_hooks_read_only_notice_no_block(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    await manage(host, "hooks disable hooks-mode")
    assert host.notices and "read-only" in host.notices[0]
    assert host.blocks == []  # a refused toggle re-posts nothing


@pytest.mark.asyncio
async def test_set_round_trips_and_reposts_show(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    await manage(host, "set session.reasoning_effort high")
    assert host.notices == ["\u2713 Set session.reasoning_effort = 'high'"]
    assert host.adapter.state.value("session.reasoning_effort") == "high"
    assert "set values" in _text(host.blocks[0])


@pytest.mark.asyncio
async def test_diff_reports_session_changes(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    await manage(host, "tools disable bash")
    host.blocks.clear()
    await manage(host, "diff")
    text = _text(host.blocks[0])
    assert "tools bash" in text and "disabled" in text


@pytest.mark.asyncio
async def test_save_writes_scope_and_notices_path(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    await manage(host, "tools disable bash")
    host.notices.clear()
    await manage(host, "save --scope global")
    assert host.notices and "global scope" in host.notices[0]
    written = yaml.safe_load((tmp_path / "settings.yaml").read_text())
    assert written["configurator"]["disabled"] == {"tools": ["bash"]}


@pytest.mark.asyncio
async def test_error_invocation_only_notices(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    await manage(host, "frobnicate")
    assert host.blocks == []
    assert host.notices and "unknown /config subcommand" in host.notices[0]
