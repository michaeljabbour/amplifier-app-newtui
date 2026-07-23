"""Tests for kernel/persistence.py — SessionStore + IncrementalSaver.

Everything runs against tmp directories with fake payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_newtui.kernel.events import normalize
from amplifier_app_newtui.kernel.persistence import (
    EVENTS_FILENAME,
    LEGACY_EVENTS_FILENAME,
    METADATA_FILENAME,
    TRANSCRIPT_FILENAME,
    IncrementalSaver,
    SessionStore,
    is_top_level_session,
)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


# --------------------------------------------------------------------------
# save / load
# --------------------------------------------------------------------------


def test_save_load_roundtrip(store: SessionStore) -> None:
    transcript = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    metadata = {"session_id": "s1", "bundle": "newtui"}
    store.save("s1", transcript, metadata)

    loaded_transcript, loaded_metadata = store.load("s1")
    assert loaded_transcript == transcript
    assert loaded_metadata == metadata
    # atomic-write artifacts in place
    session_dir = store.session_dir("s1")
    assert (session_dir / TRANSCRIPT_FILENAME).exists()
    assert (session_dir / METADATA_FILENAME).exists()


def test_system_and_developer_messages_skipped(store: SessionStore) -> None:
    transcript = [
        {"role": "system", "content": "secret system prompt"},
        {"role": "developer", "content": "context files"},
        {"role": "user", "content": "hi"},
    ]
    store.save("s1", transcript, {})
    loaded, _ = store.load("s1")
    assert loaded == [{"role": "user", "content": "hi"}]


def test_second_save_creates_backup_and_recovery_uses_it(store: SessionStore) -> None:
    store.save("s1", [{"role": "user", "content": "v1"}], {"v": 1})
    store.save("s1", [{"role": "user", "content": "v2"}], {"v": 2})
    session_dir = store.session_dir("s1")
    backup = session_dir / (TRANSCRIPT_FILENAME + ".backup")
    assert backup.exists()
    assert "v1" in backup.read_text(encoding="utf-8")

    # corrupt the main transcript → load falls back to backup
    (session_dir / TRANSCRIPT_FILENAME).write_text("{not json!!", encoding="utf-8")
    loaded, _ = store.load("s1")
    assert loaded == [{"role": "user", "content": "v1"}]


# --------------------------------------------------------------------------
# secret scrubbing at the transcript + metadata sinks (issue #23)
# --------------------------------------------------------------------------

_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_transcript_save_redacts_secrets(store: SessionStore) -> None:
    transcript = [
        {"role": "user", "content": f"here is my key {_AWS_KEY}"},
        {
            "role": "assistant",
            "content": (
                "cat ~/.aws/credentials\n[default]\n"
                f"aws_access_key_id = {_AWS_KEY}\n"
                f"aws_secret_access_key = {_AWS_SECRET}\n"
            ),
        },
    ]
    store.save("s1", transcript, {})

    # raw bytes on disk carry no plaintext secret
    raw = (store.session_dir("s1") / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")
    assert _AWS_KEY not in raw
    assert _AWS_SECRET not in raw
    assert "[REDACTED]" in raw

    # and the round-tripped transcript is redacted, structure preserved
    loaded, _ = store.load("s1")
    assert loaded[0]["role"] == "user"
    assert _AWS_KEY not in loaded[0]["content"]
    assert _AWS_SECRET not in loaded[1]["content"]


def test_transcript_redacts_nested_content_blocks(store: SessionStore) -> None:
    transcript = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": f"token {_AWS_KEY}"}],
        }
    ]
    store.save("s1", transcript, {})
    loaded, _ = store.load("s1")
    assert loaded[0]["content"][0]["text"] == "token [REDACTED]"


def test_metadata_value_pattern_redacted(store: SessionStore) -> None:
    # A secret-shaped VALUE under a non-sensitive KEY (key-based redaction
    # alone would miss it); the shared value scrub catches it.
    store.save("s1", [], {"note": f"deployed with {_AWS_KEY}"})
    raw = (store.session_dir("s1") / METADATA_FILENAME).read_text(encoding="utf-8")
    assert _AWS_KEY not in raw
    assert "[REDACTED]" in raw


def test_load_missing_session_raises(store: SessionStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.load("nope")


def test_update_metadata(store: SessionStore) -> None:
    store.save("s1", [], {"a": 1})
    updated = store.update_metadata("s1", {"b": 2})
    assert updated == {"a": 1, "b": 2}
    assert store.get_metadata("s1") == {"a": 1, "b": 2}


@pytest.mark.parametrize("bad_id", ["", "  ", "a/b", "a\\b", ".", ".."])
def test_invalid_session_ids_rejected(store: SessionStore, bad_id: str) -> None:
    with pytest.raises(ValueError):
        store.save(bad_id, [], {})


def test_unserializable_metadata_degrades_to_str(store: SessionStore) -> None:
    store.save("s1", [], {"path": Path("/tmp/x")})
    assert store.get_metadata("s1")["path"] == "/tmp/x"


# --------------------------------------------------------------------------
# ui-events.jsonl — append-only normalized UIEvents
# --------------------------------------------------------------------------


def test_append_and_read_events(store: SessionStore) -> None:
    usage = normalize(
        "provider:response",
        {
            "session_id": "s1",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "claude-sonnet-4",
        },
    )
    assert usage is not None
    tool = normalize(
        "tool:post",
        {"session_id": "s1", "tool_name": "bash", "tool_call_id": "tc1", "result": {"ok": 1}},
    )
    assert tool is not None

    store.append_event("s1", usage)
    store.append_event("s1", tool)

    records = list(store.read_events("s1"))
    assert [r["kind"] for r in records] == ["provider_response_usage", "tool_post"]
    assert records[0]["input_tokens"] == 100
    assert records[0]["session_id"] == "s1"
    assert records[1]["tool_call_id"] == "tc1"

    # append-only: file has exactly two lines
    lines = store.events_path("s1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_read_events_skips_bad_lines(store: SessionStore) -> None:
    store.session_dir("s1").mkdir(parents=True)
    (store.session_dir("s1") / EVENTS_FILENAME).write_text(
        'not json\n{"kind": "session_start", "session_id": "s1"}\n[1,2]\n',
        encoding="utf-8",
    )
    records = list(store.read_events("s1"))
    assert len(records) == 1
    assert records[0]["kind"] == "session_start"


def test_read_events_missing_file_yields_nothing(store: SessionStore) -> None:
    assert list(store.read_events("ghost")) == []


def test_append_event_accepts_plain_mapping(store: SessionStore) -> None:
    store.append_event("s1", {"kind": "custom", "x": 1})
    assert list(store.read_events("s1")) == [{"kind": "custom", "x": 1}]


def test_append_event_writes_ui_events_never_legacy(store: SessionStore) -> None:
    """The app's UIEvent log is ui-events.jsonl; events.jsonl belongs to
    foundation's hooks-logging and must never receive app records."""
    assert EVENTS_FILENAME == "ui-events.jsonl"
    store.append_event("s1", {"kind": "custom", "x": 1})
    assert (store.session_dir("s1") / EVENTS_FILENAME).is_file()
    assert not (store.session_dir("s1") / LEGACY_EVENTS_FILENAME).exists()
    assert store.events_path("s1").name == EVENTS_FILENAME


def test_events_path_falls_back_to_legacy_only_session(store: SessionStore) -> None:
    """Sessions written before the rename logged UIEvents to events.jsonl."""
    store.session_dir("s1").mkdir(parents=True)
    legacy = store.session_dir("s1") / LEGACY_EVENTS_FILENAME
    legacy.write_text('{"kind": "session_start", "session_id": "s1"}\n', encoding="utf-8")

    assert store.events_path("s1") == legacy
    assert [record["kind"] for record in store.read_events("s1")] == ["session_start"]

    # Once the current file exists it wins; the legacy file is read-only history.
    store.append_event("s1", {"kind": "custom"})
    assert store.events_path("s1").name == EVENTS_FILENAME


def test_read_events_spans_legacy_then_current(store: SessionStore) -> None:
    """A rename-straddling session replays its whole history, oldest first."""
    store.session_dir("s1").mkdir(parents=True)
    (store.session_dir("s1") / LEGACY_EVENTS_FILENAME).write_text(
        '{"kind": "session_start", "session_id": "s1"}\n', encoding="utf-8"
    )
    store.append_event("s1", {"kind": "tool_post", "tool_call_id": "t1"})

    assert [record["kind"] for record in store.read_events("s1")] == [
        "session_start",
        "tool_post",
    ]
    assert store.events_read_paths("s1") == (
        store.session_dir("s1") / LEGACY_EVENTS_FILENAME,
        store.session_dir("s1") / EVENTS_FILENAME,
    )


def test_read_events_skips_foreign_hooks_logging_records(store: SessionStore) -> None:
    """hooks-logging's ISO-timestamped hook records (no ``kind``) share the
    legacy filename in mixed files written before the rename — skipped."""
    store.session_dir("s1").mkdir(parents=True)
    (store.session_dir("s1") / LEGACY_EVENTS_FILENAME).write_text(
        '{"ts": "2026-07-21T00:00:00Z", "event": "tool:pre", "data": {"tool": "bash"}}\n'
        '{"kind": "tool_pre", "session_id": "s1", "ts": 12.5}\n'
        "not json\n",
        encoding="utf-8",
    )
    records = list(store.read_events("s1"))
    assert [record["kind"] for record in records] == ["tool_pre"]


# --------------------------------------------------------------------------
# listing / lookup
# --------------------------------------------------------------------------


def test_list_and_find_sessions_top_level_filter(store: SessionStore) -> None:
    store.save("aaaa-1111", [], {})
    store.save("aaaa-2222", [], {})
    store.save("aaaa-1111-abcdef01_explorer", [], {})  # spawned sub-session

    assert not is_top_level_session("aaaa-1111-abcdef01_explorer")
    top = store.list_sessions()
    assert set(top) == {"aaaa-1111", "aaaa-2222"}
    assert set(store.list_sessions(top_level_only=False)) == {
        "aaaa-1111",
        "aaaa-2222",
        "aaaa-1111-abcdef01_explorer",
    }

    assert store.find_session("aaaa-2") == "aaaa-2222"
    with pytest.raises(ValueError):
        store.find_session("aaaa")  # ambiguous
    with pytest.raises(FileNotFoundError):
        store.find_session("zzzz")


# --------------------------------------------------------------------------
# IncrementalSaver — debounced save on tool:post
# --------------------------------------------------------------------------


class FakeContext:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def get_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)


class FakeCoordinator:
    def __init__(self, context: FakeContext) -> None:
        self._context = context

    def get(self, name: str) -> Any:
        return self._context if name == "context" else None


class FakeSession:
    def __init__(self, context: FakeContext) -> None:
        self.coordinator = FakeCoordinator(context)


@pytest.mark.asyncio
async def test_incremental_saver_debounces_on_message_count(store: SessionStore) -> None:
    context = FakeContext()
    saver = IncrementalSaver(
        store,
        "s1",
        session=FakeSession(context),
        base_metadata={"bundle": "newtui", "model": "claude-sonnet-4"},
    )

    context.messages = [{"role": "user", "content": "hi"}]
    assert await saver.maybe_save() is True
    assert await saver.maybe_save() is False  # debounced: no growth

    context.messages.append({"role": "assistant", "content": "hello"})
    assert await saver.maybe_save() is True

    transcript, metadata = store.load("s1")
    assert len(transcript) == 2
    assert metadata["bundle"] == "newtui"
    assert metadata["turn_count"] == 1
    assert metadata["incremental"] is True
    assert "created" in metadata


@pytest.mark.asyncio
async def test_incremental_saver_hook_never_raises(store: SessionStore) -> None:
    class BrokenContext:
        async def get_messages(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

    session = FakeSession(FakeContext())
    session.coordinator._context = BrokenContext()  # type: ignore[assignment]
    saver = IncrementalSaver(store, "s1", session=session)

    result = await saver.on_tool_post("tool:post", {"tool_name": "bash"})
    assert result.action == "continue"


@pytest.mark.asyncio
async def test_incremental_saver_preserves_existing_metadata(store: SessionStore) -> None:
    store.save("s1", [], {"name": "my session", "created": "2026-01-01T00:00:00+00:00"})
    context = FakeContext()
    context.messages = [{"role": "user", "content": "hi"}]
    saver = IncrementalSaver(store, "s1", session=FakeSession(context))
    await saver.maybe_save()
    metadata = store.get_metadata("s1")
    assert metadata["name"] == "my session"  # preserved (e.g. session-naming hook)
    assert metadata["created"] == "2026-01-01T00:00:00+00:00"


def test_saved_files_are_valid_jsonl(store: SessionStore) -> None:
    store.save("s1", [{"role": "user", "content": "hi"}], {"a": 1})
    for line in (
        (store.session_dir("s1") / TRANSCRIPT_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ):
        json.loads(line)


# --------------------------------------------------------------------------
# delete / cleanup_old_sessions (session-manager lifecycle)
# --------------------------------------------------------------------------


def test_delete_removes_session_tree(store: SessionStore) -> None:
    store.save("s1", [{"role": "user", "content": "hi"}], {"session_id": "s1"})
    assert store.exists("s1")
    assert store.delete("s1") is True
    assert not store.exists("s1")


def test_delete_missing_returns_false(store: SessionStore) -> None:
    assert store.delete("ghost") is False


def test_cleanup_old_sessions_removes_by_mtime(store: SessionStore) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    store.save("fresh", [], {"session_id": "fresh"})
    store.save("stale", [], {"session_id": "stale"})
    old = (datetime.now(UTC) - timedelta(days=60)).timestamp()
    os.utime(store.session_dir("stale"), (old, old))

    assert store.cleanup_old_sessions(days=30) == 1
    assert store.exists("fresh")
    assert not store.exists("stale")


def test_cleanup_old_sessions_skips_subsessions(store: SessionStore) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    # Spawned sub-sessions carry '_' and are never top-level cleanup targets.
    store.save("parent-abc_agent", [], {"session_id": "parent-abc_agent"})
    old = (datetime.now(UTC) - timedelta(days=99)).timestamp()
    os.utime(store.session_dir("parent-abc_agent"), (old, old))
    assert store.cleanup_old_sessions(days=30) == 0
    assert store.exists("parent-abc_agent")


def test_cleanup_old_sessions_rejects_negative_days(store: SessionStore) -> None:
    with pytest.raises(ValueError):
        store.cleanup_old_sessions(days=-1)
