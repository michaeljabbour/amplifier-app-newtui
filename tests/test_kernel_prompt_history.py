"""Tests for kernel/prompt_history.py — per-project cross-session ↑ store.

Everything runs against tmp directories (HOME is monkeypatched for the
slug-keying tests) so the real ``~/.amplifier`` is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_newtui.kernel.config import get_project_slug
from amplifier_app_newtui.kernel.prompt_history import (
    HISTORY_FILENAME,
    PromptHistoryStore,
    format_entry,
    parse_history,
)


@pytest.fixture
def store(tmp_path: Path) -> PromptHistoryStore:
    return PromptHistoryStore(path=tmp_path / "repl_history")


# --------------------------------------------------------------------------
# format round-trip (prompt-toolkit FileHistory compatibility)
# --------------------------------------------------------------------------


def test_format_and_parse_roundtrip_single_line() -> None:
    text = format_entry("hello world")
    assert parse_history(text) == ["hello world"]


def test_format_and_parse_roundtrip_multiline() -> None:
    text = format_entry("line one\nline two")
    assert parse_history(text) == ["line one\nline two"]


def test_parse_reads_appcli_written_file(tmp_path: Path) -> None:
    """A file in prompt-toolkit's on-disk format (what app-cli writes)
    reads back verbatim, so the two apps share one history file."""
    path = tmp_path / "repl_history"
    path.write_text(
        "\n# 2026-07-24 10:00:00.000001\n+first prompt\n"
        "\n# 2026-07-24 10:01:00.000002\n+second\n+multi\n",
        encoding="utf-8",
    )
    store = PromptHistoryStore(path=path)
    assert store.load() == ["first prompt", "second\nmulti"]


# --------------------------------------------------------------------------
# append / load
# --------------------------------------------------------------------------


def test_append_then_load_is_oldest_first(store: PromptHistoryStore) -> None:
    assert store.append("first") is True
    assert store.append("second") is True
    # Newest last so ↑ walks most-recent-first.
    assert store.load() == ["first", "second"]


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert PromptHistoryStore(path=tmp_path / "absent").load() == []


def test_append_skips_empty_and_whitespace(store: PromptHistoryStore) -> None:
    assert store.append("") is False
    assert store.append("   \n  ") is False
    assert store.load() == []


def test_append_strips_surrounding_whitespace(store: PromptHistoryStore) -> None:
    store.append("  padded  ")
    assert store.load() == ["padded"]


# --------------------------------------------------------------------------
# dedup (consecutive only — composer parity)
# --------------------------------------------------------------------------


def test_append_skips_consecutive_duplicate(store: PromptHistoryStore) -> None:
    assert store.append("same") is True
    assert store.append("same") is False
    assert store.load() == ["same"]


def test_non_consecutive_duplicate_is_kept(store: PromptHistoryStore) -> None:
    store.append("a")
    store.append("b")
    store.append("a")
    assert store.load() == ["a", "b", "a"]


def test_load_dedups_consecutive_from_disk(tmp_path: Path) -> None:
    """A file with consecutive dupes (e.g. app-cli, which does not dedup)
    is deduped on load to mirror the composer ring."""
    path = tmp_path / "repl_history"
    path.write_text(format_entry("dup") + format_entry("dup"), encoding="utf-8")
    assert PromptHistoryStore(path=path).load() == ["dup"]


# --------------------------------------------------------------------------
# secret scrubbing (model.redaction policy at the sink)
# --------------------------------------------------------------------------


def test_append_scrubs_secret_shaped_values(store: PromptHistoryStore) -> None:
    store.append("my key is AKIAIOSFODNN7EXAMPLE ok")
    (stored,) = store.load()
    assert "AKIAIOSFODNN7EXAMPLE" not in stored
    assert "[REDACTED]" in stored


# --------------------------------------------------------------------------
# cap / bound
# --------------------------------------------------------------------------


def test_cap_bounds_stored_and_loaded_entries(tmp_path: Path) -> None:
    store = PromptHistoryStore(path=tmp_path / "repl_history", max_entries=3)
    for i in range(6):
        store.append(f"p{i}")
    loaded = store.load()
    assert loaded == ["p3", "p4", "p5"]  # most recent kept, oldest dropped
    # The file itself was trimmed, not just the load view.
    assert PromptHistoryStore(path=tmp_path / "repl_history").load() == ["p3", "p4", "p5"]


def test_load_limit_caps_to_most_recent(store: PromptHistoryStore) -> None:
    for i in range(5):
        store.append(f"p{i}")
    assert store.load(limit=2) == ["p3", "p4"]


# --------------------------------------------------------------------------
# per-directory isolation (slug keying, HOME monkeypatched to tmp)
# --------------------------------------------------------------------------


def test_default_path_is_project_slug_keyed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    project = tmp_path / "work" / "proj-x"
    store = PromptHistoryStore(project_dir=project)
    expected = tmp_path / ".amplifier" / "projects" / get_project_slug(project) / HISTORY_FILENAME
    assert store.path == expected


def test_history_is_isolated_per_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    dir_x = tmp_path / "work" / "x"
    dir_y = tmp_path / "work" / "y"
    dir_x.mkdir(parents=True)
    dir_y.mkdir(parents=True)

    PromptHistoryStore(project_dir=dir_x).append("command A")

    # A fresh store for the SAME dir recalls it; a different dir does not.
    assert PromptHistoryStore(project_dir=dir_x).load() == ["command A"]
    assert PromptHistoryStore(project_dir=dir_y).load() == []
