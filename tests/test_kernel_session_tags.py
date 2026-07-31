"""kernel/session_manager.py — session tags (HGT: session-tags-backend).

The donor (opencode) has no first-class session tags; this is the idiomatic
host re-expression persisting tags in ``metadata.json``. Everything runs
against a tmp-dir :class:`SessionStore` — nothing touches ``~/.amplifier``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_tui.kernel import session_manager as sm
from amplifier_app_tui.kernel.persistence import SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


def _seed(store: SessionStore, session_id: str, **meta: object) -> None:
    store.save(session_id, [], {"session_id": session_id, "bundle": "tui", **meta})


# -- normalize_tag ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Frontend", "frontend"),
        ("  URGENT  ", "urgent"),
        ("bug-fix", "bug-fix"),
        ("v2_ready", "v2_ready"),
        ("a" * 40, "a" * 32),  # clamp
        ("", None),
        ("   ", None),
        ("bad tag!", None),  # space + punctuation
        ("-leading", None),  # must start alnum
        ("emoji\U0001f600", None),
    ],
)
def test_normalize_tag(raw: str, expected: str | None) -> None:
    assert sm.normalize_tag(raw) == expected


def test_normalize_tag_is_idempotent() -> None:
    once = sm.normalize_tag("Hello-World")
    assert once is not None
    assert sm.normalize_tag(once) == once


# -- add / read / persistence ----------------------------------------------


def test_add_tags_normalizes_dedupes_sorts_and_persists(store: SessionStore) -> None:
    _seed(store, "abc123")
    out = sm.add_tags(store, "abc123", ["Urgent", "frontend", "urgent"])
    assert out.ok
    assert out.tags == ("frontend", "urgent")  # sorted + deduped
    assert set(out.changed) == {"frontend", "urgent"}
    assert out.rejected == ()
    # persisted to metadata.json (fresh store sees it)
    fresh = SessionStore(base_dir=store.base_dir)
    meta = fresh.get_metadata("abc123")
    assert sorted(meta["tags"]) == ["frontend", "urgent"]


def test_add_tags_reports_rejected_and_keeps_valid(store: SessionStore) -> None:
    _seed(store, "abc123")
    out = sm.add_tags(store, "abc123", ["ok", "bad tag!", ""])
    assert out.ok
    assert out.tags == ("ok",)
    assert out.rejected == ("bad tag!",)


def test_add_tags_second_call_is_additive_and_changed_only_new(store: SessionStore) -> None:
    _seed(store, "abc123")
    sm.add_tags(store, "abc123", ["frontend"])
    out = sm.add_tags(store, "abc123", ["frontend", "backend"])
    assert out.tags == ("backend", "frontend")
    assert out.changed == ("backend",)  # frontend already present


def test_add_tags_cap_refuses_whole_and_does_not_write(store: SessionStore) -> None:
    _seed(store, "abc123")
    first = sm.add_tags(store, "abc123", [f"t{i}" for i in range(sm.MAX_TAGS)])
    assert first.ok and len(first.tags) == sm.MAX_TAGS
    out = sm.add_tags(store, "abc123", ["one-too-many"])
    assert not out.ok
    assert "max" in out.error
    assert "one-too-many" not in sm.read_tags(store, "abc123")


def test_add_tags_unknown_session_errors(store: SessionStore) -> None:
    out = sm.add_tags(store, "nope", ["x"])
    assert not out.ok
    assert "no session found" in out.error


def test_add_tags_resolves_prefix(store: SessionStore) -> None:
    _seed(store, "deadbeef")
    out = sm.add_tags(store, "dead", ["x"])
    assert out.ok
    assert out.session_id == "deadbeef"


def test_add_tags_ambiguous_prefix_errors(store: SessionStore) -> None:
    _seed(store, "abc123")
    _seed(store, "abd999")
    out = sm.add_tags(store, "ab", ["x"])
    assert not out.ok


# -- remove -----------------------------------------------------------------


def test_remove_tags_detaches_and_reports_changed(store: SessionStore) -> None:
    _seed(store, "abc123")
    sm.add_tags(store, "abc123", ["frontend", "urgent"])
    out = sm.remove_tags(store, "abc123", ["urgent", "absent"])
    assert out.ok
    assert out.tags == ("frontend",)
    assert out.changed == ("urgent",)
    assert sm.read_tags(store, "abc123") == ("frontend",)


def test_remove_absent_tag_is_noop(store: SessionStore) -> None:
    _seed(store, "abc123")
    sm.add_tags(store, "abc123", ["frontend"])
    out = sm.remove_tags(store, "abc123", ["nope"])
    assert out.ok
    assert out.changed == ()
    assert out.tags == ("frontend",)


# -- get / read -------------------------------------------------------------


def test_get_tags_and_read_tags(store: SessionStore) -> None:
    _seed(store, "abc123")
    assert sm.read_tags(store, "abc123") == ()
    sm.add_tags(store, "abc123", ["b", "a"])
    got = sm.get_tags(store, "abc123")
    assert got.ok and got.tags == ("a", "b")


def test_read_tags_coerces_malformed_metadata(store: SessionStore) -> None:
    _seed(store, "abc123", tags=["OK", 5, "bad tag!", "ok", {"x": 1}])
    assert sm.read_tags(store, "abc123") == ("ok",)


def test_read_tags_non_list_degrades_to_empty(store: SessionStore) -> None:
    _seed(store, "abc123", tags="frontend")
    assert sm.read_tags(store, "abc123") == ()


# -- filter (sessions_by_tag) + summary -------------------------------------


def test_sessions_by_tag_filters(store: SessionStore) -> None:
    _seed(store, "aaa111")
    _seed(store, "bbb222")
    _seed(store, "ccc333")
    sm.add_tags(store, "aaa111", ["frontend"])
    sm.add_tags(store, "bbb222", ["frontend", "urgent"])
    sm.add_tags(store, "ccc333", ["backend"])
    ids = {s.session_id for s in sm.sessions_by_tag(store, "Frontend")}
    assert ids == {"aaa111", "bbb222"}


def test_sessions_by_tag_invalid_tag_returns_empty(store: SessionStore) -> None:
    _seed(store, "aaa111")
    assert sm.sessions_by_tag(store, "bad tag!") == []


def test_summary_for_exposes_tags(store: SessionStore) -> None:
    _seed(store, "abc123")
    sm.add_tags(store, "abc123", ["b", "a"])
    summary = sm.summary_for(store, "abc123")
    assert summary.tags == ("a", "b")


# -- ensure_session_dir -----------------------------------------------------


def test_ensure_session_dir_creates_missing(store: SessionStore) -> None:
    assert not store.exists("fresh01")
    assert sm.ensure_session_dir(store, "fresh01", bundle="tui")
    assert store.exists("fresh01")
    # now taggable
    out = sm.add_tags(store, "fresh01", ["x"])
    assert out.ok and out.tags == ("x",)


def test_ensure_session_dir_keeps_existing_metadata(store: SessionStore) -> None:
    _seed(store, "abc123", name="keep me")
    sm.ensure_session_dir(store, "abc123")
    assert store.get_metadata("abc123")["name"] == "keep me"
