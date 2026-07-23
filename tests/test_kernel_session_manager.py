"""kernel/session_manager.py — stored-session lifecycle (donor parity).

Everything runs against a tmp-dir :class:`SessionStore`; nothing touches
the developer's real ``~/.amplifier``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amplifier_app_newtui.kernel import session_manager as sm
from amplifier_app_newtui.kernel.persistence import METADATA_FILENAME, SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


def _seed(
    store: SessionStore,
    session_id: str,
    *,
    name: str = "",
    bundle: str = "newtui",
    messages: int = 0,
) -> None:
    transcript = [{"role": "user", "content": f"m{i}"} for i in range(messages)]
    metadata = {"session_id": session_id, "bundle": bundle}
    if name:
        metadata["name"] = name
    store.save(session_id, transcript, metadata)


# -- format_time_ago --------------------------------------------------------


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=3), "3m ago"),
        (timedelta(hours=2), "2h ago"),
        (timedelta(days=4), "4d ago"),
        (timedelta(days=45), "1mo ago"),
        (timedelta(days=800), "2y ago"),
    ],
)
def test_format_time_ago(delta: timedelta, expected: str) -> None:
    assert sm.format_time_ago(datetime.now(UTC) - delta) == expected


# -- summaries + ordering ---------------------------------------------------


def test_list_summaries_newest_first_with_metadata(store: SessionStore) -> None:
    _seed(store, "old", name="first", bundle="alpha", messages=2)
    _seed(store, "new", name="second", bundle="beta", messages=5)
    # Make "new" strictly newer than "old" by directory mtime.
    now = datetime.now(UTC).timestamp()
    os.utime(store.session_dir("old"), (now - 100, now - 100))
    os.utime(store.session_dir("new"), (now, now))

    summaries = sm.list_summaries(store)
    assert [s.session_id for s in summaries] == ["new", "old"]
    top = summaries[0]
    assert top.name == "second"
    assert top.bundle == "beta"
    assert top.messages == 5
    assert top.short_id == "new"


def test_summary_survives_missing_metadata(store: SessionStore) -> None:
    _seed(store, "s1", messages=1)
    (store.session_dir("s1") / METADATA_FILENAME).unlink()
    summary = sm.summary_for(store, "s1")
    assert summary.name == ""
    assert summary.bundle == "unknown"
    assert summary.messages == 1


def test_list_summaries_limit(store: SessionStore) -> None:
    for i in range(5):
        _seed(store, f"s{i}")
    assert len(sm.list_summaries(store, limit=3)) == 3


# -- resolve ----------------------------------------------------------------


def test_resolve_prefix_and_errors(store: SessionStore) -> None:
    _seed(store, "abc123")
    _seed(store, "abd999")
    assert sm.resolve(store, "abc") == "abc123"
    with pytest.raises(FileNotFoundError):
        sm.resolve(store, "zzz")
    with pytest.raises(ValueError):
        sm.resolve(store, "ab")  # ambiguous


# -- rename -----------------------------------------------------------------


def test_rename_persists_name_and_stamp(store: SessionStore) -> None:
    _seed(store, "sess-1")
    ok, detail = sm.rename(store, "sess-1", "auth refactor")
    assert ok
    assert detail == "auth refactor"
    metadata = store.get_metadata("sess-1")
    assert metadata["name"] == "auth refactor"
    assert "name_generated_at" in metadata


def test_rename_prefix_resolution(store: SessionStore) -> None:
    _seed(store, "deadbeef")
    ok, _ = sm.rename(store, "dead", "shipped")
    assert ok
    assert store.get_metadata("deadbeef")["name"] == "shipped"


def test_rename_clamps_to_max_length(store: SessionStore) -> None:
    _seed(store, "s")
    ok, detail = sm.rename(store, "s", "x" * 200)
    assert ok
    assert len(detail) == sm.MAX_NAME_LENGTH


def test_rename_rejects_bad_name(store: SessionStore) -> None:
    _seed(store, "s")
    ok, detail = sm.rename(store, "s", "no/slashes!")
    assert not ok
    assert "letters" in detail


def test_rename_empty_is_usage(store: SessionStore) -> None:
    _seed(store, "s")
    ok, detail = sm.rename(store, "s", "   ")
    assert not ok
    assert "usage" in detail


def test_rename_missing_session(store: SessionStore) -> None:
    ok, detail = sm.rename(store, "ghost", "x")
    assert not ok
    assert "no session found" in detail


# -- delete -----------------------------------------------------------------


def test_delete_removes_tree(store: SessionStore) -> None:
    _seed(store, "victim", messages=3)
    assert store.exists("victim")
    ok, resolved = sm.delete(store, "vic")  # prefix
    assert ok
    assert resolved == "victim"
    assert not store.exists("victim")


def test_delete_missing_session(store: SessionStore) -> None:
    ok, detail = sm.delete(store, "ghost")
    assert not ok
    assert "no session found" in detail


def test_delete_ambiguous_is_refused(store: SessionStore) -> None:
    _seed(store, "aa1")
    _seed(store, "aa2")
    ok, detail = sm.delete(store, "aa")
    assert not ok
    assert "mbiguous" in detail
    assert store.exists("aa1") and store.exists("aa2")  # nothing removed


# -- cleanup ----------------------------------------------------------------


def test_cleanup_removes_only_old(store: SessionStore) -> None:
    _seed(store, "fresh")
    _seed(store, "stale")
    old = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    os.utime(store.session_dir("stale"), (old, old))

    removed = sm.cleanup(store, days=30)
    assert removed == 1
    assert store.exists("fresh")
    assert not store.exists("stale")


def test_cleanup_days_zero_removes_all(store: SessionStore) -> None:
    _seed(store, "a")
    _seed(store, "b")
    assert sm.cleanup(store, days=0) == 2
    assert sm.list_summaries(store) == []


# -- branch -----------------------------------------------------------------


def test_branch_snapshots_into_new_session(store: SessionStore) -> None:
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    ok, branch_id = sm.branch(store, "parent-1", messages, name="spike", bundle="newtui")
    assert ok
    assert branch_id != "parent-1"
    metadata = store.get_metadata(branch_id)
    assert metadata["parent_id"] == "parent-1"
    assert metadata["name"] == "spike"
    assert metadata["bundle"] == "newtui"
    assert "branched_at" in metadata
    transcript, _ = store.load(branch_id)
    assert transcript == messages


def test_branch_default_name(store: SessionStore) -> None:
    ok, branch_id = sm.branch(store, "parent", [], bundle="newtui")
    assert ok
    assert store.get_metadata(branch_id)["name"].startswith("branch-")


def test_branch_rejects_bad_name(store: SessionStore) -> None:
    ok, detail = sm.branch(store, "parent", [], name="bad/name")
    assert not ok
    assert "letters" in detail
