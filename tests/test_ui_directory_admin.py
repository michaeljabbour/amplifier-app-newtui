"""``/allowed-dirs`` / ``/denied-dirs`` slash-command controller: input
validation before the live policy is mutated. Fake host + adapter, no Textual."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_app_newtui.ui.directory_admin import manage


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def update_directory(self, kind: str, operation: str, path: str):
        self.calls.append((kind, operation, path))
        return True, f"session {kind} · {path}"

    async def directory_entries(self, kind: str) -> tuple[Any, ...]:
        return ()


class FakeAllocator:
    def __init__(self) -> None:
        self._n = 0

    def next_id(self) -> str:
        self._n += 1
        return f"b{self._n}"


class FakeHost:
    def __init__(self) -> None:
        self.adapter = FakeAdapter()
        self.allocator = FakeAllocator()
        self.blocks: list[Any] = []
        self.notices: list[str] = []

    def append_block(self, block: Any) -> None:
        self.blocks.append(block)

    def show_notice(self, text: str, duration: float | None = None) -> None:
        self.notices.append(text)


@pytest.mark.asyncio
async def test_add_existing_directory_reaches_adapter(tmp_path: Path) -> None:
    host = FakeHost()
    await manage(host, "allowed", f"add {tmp_path}")
    assert host.adapter.calls == [("allowed", "add", str(tmp_path))]


@pytest.mark.asyncio
async def test_add_nonexistent_path_is_refused(tmp_path: Path) -> None:
    """Regression: a doubled paste ("add ~/x/allowed-dirs add ~/x") used to be
    swallowed verbatim as one garbage path and stored in the session allowlist.
    Nonexistent directories must be refused before the adapter is called."""
    host = FakeHost()
    garbage = f"{tmp_path}/allowed-dirs add {tmp_path}"
    await manage(host, "allowed", f"add {garbage}")
    assert host.adapter.calls == []
    assert host.notices and "not an existing directory" in host.notices[0]


@pytest.mark.asyncio
async def test_add_strips_surrounding_quotes(tmp_path: Path) -> None:
    host = FakeHost()
    await manage(host, "allowed", f'add "{tmp_path}"')
    assert host.adapter.calls == [("allowed", "add", str(tmp_path))]


@pytest.mark.asyncio
async def test_remove_skips_existence_validation(tmp_path: Path) -> None:
    """Stale or garbage entries must remain removable even though the path
    does not exist on disk."""
    host = FakeHost()
    garbage = f"{tmp_path}/allowed-dirs add {tmp_path}"
    await manage(host, "allowed", f"remove {garbage}")
    assert host.adapter.calls == [("allowed", "remove", garbage)]


@pytest.mark.asyncio
async def test_add_file_is_refused(tmp_path: Path) -> None:
    """Allowed/denied entries are directories; a file path is a mistake."""
    target = tmp_path / "notes.txt"
    target.write_text("x")
    host = FakeHost()
    await manage(host, "allowed", f"add {target}")
    assert host.adapter.calls == []
    assert host.notices and "not an existing directory" in host.notices[0]
