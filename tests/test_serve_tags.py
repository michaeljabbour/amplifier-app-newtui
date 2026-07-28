"""Offline test of the additive ``serve`` tag protocol ops.

Drives :func:`amplifier_app_newtui.kernel.serve.serve_loop` with a minimal fake
runtime whose only non-trivial surface is a real :class:`SessionStore` in a
tmp dir (the exact seam the live CLI ``serve`` uses). Proves the tag ops the
Rust client consumes: JSON round-trip, on-disk persistence, and tag filtering
— no API key, no network.
"""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from typing import IO, Any, cast

import pytest

from amplifier_app_newtui.kernel.persistence import METADATA_FILENAME, SessionStore
from amplifier_app_newtui.kernel.serve import serve_loop

pytestmark = pytest.mark.asyncio


class _PipeStdin:
    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def feed(self, obj: dict[str, Any]) -> None:
        self._q.put(json.dumps(obj) + "\n")

    def close(self) -> None:
        self._q.put(None)

    def __iter__(self) -> _PipeStdin:
        return self

    def __next__(self) -> str:
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _Capture:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def write(self, s: str) -> int:
        for part in s.splitlines():
            part = part.strip()
            if part:
                self.lines.append(json.loads(part))
        return len(s)

    def flush(self) -> None:
        pass

    def find(self, type_: str, op: str | None = None) -> dict[str, Any] | None:
        return next(
            (r for r in self.lines if r.get("type") == type_ and (op is None or r.get("op") == op)),
            None,
        )


class _NoBroker:
    head = None

    def add_listener(self, listener: Any) -> None:
        del listener


class _FakeRuntime:
    """Minimal serve_loop surface + a real store (the tag ops' only dependency)."""

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.bundle_name = "newtui"
        self.model_name = "test-model"
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.broker = _NoBroker()

    async def cleanup(self) -> None:
        pass


async def _drive(runtime: _FakeRuntime, ops: list[dict[str, Any]]) -> _Capture:
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )
    for op in ops:
        stdin.feed(op)
    await asyncio.sleep(0.3)
    stdin.close()
    await asyncio.wait_for(server, timeout=5.0)
    return out


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


async def test_tag_add_list_roundtrip_and_persist(store: SessionStore) -> None:
    live = "a" * 32
    store.save(live, [], {"session_id": live, "bundle": "newtui", "name": "live"})
    runtime = _FakeRuntime(store, live)

    out = await _drive(
        runtime,
        [
            {"op": "tag.add", "tags": ["Frontend", "urgent", "bad tag!"]},
            {"op": "tag.list"},
        ],
    )

    added = out.find("tag.updated", "tag.add")
    assert added is not None
    assert added["ok"] is True
    assert added["session_id"] == live
    assert added["tags"] == ["frontend", "urgent"]  # normalized + sorted
    assert set(added["changed"]) == {"frontend", "urgent"}
    assert added["rejected"] == ["bad tag!"]

    listed = out.find("tag.list", "tag.list")
    assert listed is not None
    assert listed["ok"] is True
    assert listed["tags"] == ["frontend", "urgent"]

    # persisted to disk for a fresh process to read
    meta = json.loads(
        (SessionStore(base_dir=store.base_dir).session_dir(live) / METADATA_FILENAME).read_text()
    )
    assert sorted(meta["tags"]) == ["frontend", "urgent"]


async def test_tag_add_singular_tag_field(store: SessionStore) -> None:
    live = "c" * 32
    store.save(live, [], {"session_id": live, "bundle": "newtui"})
    out = await _drive(_FakeRuntime(store, live), [{"op": "tag.add", "tag": "solo"}])
    added = out.find("tag.updated", "tag.add")
    assert added is not None and added["tags"] == ["solo"]


async def test_tag_remove_roundtrip(store: SessionStore) -> None:
    live = "a" * 32
    store.save(live, [], {"session_id": live, "bundle": "newtui", "tags": ["frontend", "urgent"]})
    out = await _drive(_FakeRuntime(store, live), [{"op": "tag.remove", "tags": ["urgent"]}])
    removed = out.find("tag.updated", "tag.remove")
    assert removed is not None
    assert removed["ok"] is True
    assert removed["tags"] == ["frontend"]
    assert removed["changed"] == ["urgent"]


async def test_tag_sessions_filter(store: SessionStore) -> None:
    live = "a" * 32
    other = "b" * 32
    third = "d" * 32
    for sid in (live, other, third):
        store.save(sid, [], {"session_id": sid, "bundle": "newtui", "name": sid[:4]})
    runtime = _FakeRuntime(store, live)

    out = await _drive(
        runtime,
        [
            {"op": "tag.add", "tags": ["frontend"]},  # live
            {"op": "tag.add", "session_id": other, "tags": ["frontend"]},
            {"op": "tag.add", "session_id": third, "tags": ["backend"]},
            {"op": "tag.sessions", "tag": "Frontend"},
        ],
    )
    filtered = out.find("tag.sessions", "tag.sessions")
    assert filtered is not None
    assert filtered["ok"] is True
    assert filtered["tag"] == "frontend"
    ids = {s["session_id"] for s in filtered["sessions"]}
    assert ids == {live, other}
    # each carries its tag list
    for entry in filtered["sessions"]:
        assert "frontend" in entry["tags"]


async def test_tag_list_unknown_session_errors(store: SessionStore) -> None:
    live = "a" * 32
    store.save(live, [], {"session_id": live, "bundle": "newtui"})
    out = await _drive(_FakeRuntime(store, live), [{"op": "tag.list", "session_id": "zzzz"}])
    listed = out.find("tag.list", "tag.list")
    assert listed is not None
    assert listed["ok"] is False
    assert listed["tags"] == []
    assert "no session found" in listed["error"]


async def test_tag_add_defaults_persist_lazy_live_session(store: SessionStore) -> None:
    """The live session may not be persisted yet; tag.add materializes it."""
    live = "e" * 32
    assert not store.exists(live)
    out = await _drive(_FakeRuntime(store, live), [{"op": "tag.add", "tags": ["fresh"]}])
    added = out.find("tag.updated", "tag.add")
    assert added is not None and added["ok"] is True
    assert store.exists(live)
    assert added["tags"] == ["fresh"]
