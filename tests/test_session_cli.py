"""CLI: `amplifier-newtui session <verb>` + the `resume` picker.

Every test runs against a scratch ``$HOME`` (monkeypatched) so the stored
sessions live in a tmp dir, never the developer's real ``~/.amplifier``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

import amplifier_app_newtui.main as main_mod
from amplifier_app_newtui.kernel.persistence import SessionStore
from amplifier_app_newtui.main import main


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    """A scratch store the CLI and the test both resolve to (HOME + cwd)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return SessionStore()


def _seed(store: SessionStore, session_id: str, *, name: str = "", messages: int = 0) -> None:
    transcript = [{"role": "user", "content": f"m{i}"} for i in range(messages)]
    metadata = {"session_id": session_id, "bundle": "newtui"}
    if name:
        metadata["name"] = name
    store.save(session_id, transcript, metadata)


# -- session list -----------------------------------------------------------


def test_session_list_empty(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["session", "list"])
    assert result.exit_code == 0
    assert "no stored sessions" in result.output


def test_session_list_shows_rows(scratch: SessionStore) -> None:
    _seed(scratch, "abc12345", name="auth work", messages=3)
    result = CliRunner().invoke(main, ["session", "list"])
    assert result.exit_code == 0
    assert "auth work" in result.output
    assert "abc12345" in result.output


# -- session rename ---------------------------------------------------------


def test_session_rename_updates_metadata(scratch: SessionStore) -> None:
    _seed(scratch, "sess0001")
    result = CliRunner().invoke(main, ["session", "rename", "sess0001", "big", "refactor"])
    assert result.exit_code == 0
    assert "renamed" in result.output
    assert scratch.get_metadata("sess0001")["name"] == "big refactor"


def test_session_rename_prefix(scratch: SessionStore) -> None:
    _seed(scratch, "deadbeef")
    result = CliRunner().invoke(main, ["session", "rename", "dead", "shipped"])
    assert result.exit_code == 0
    assert scratch.get_metadata("deadbeef")["name"] == "shipped"


def test_session_rename_unknown_exits_nonzero(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["session", "rename", "ghost", "x"])
    assert result.exit_code == 1
    assert "no session found" in result.output


# -- session delete ---------------------------------------------------------


def test_session_delete_force(scratch: SessionStore) -> None:
    _seed(scratch, "victim01")
    result = CliRunner().invoke(main, ["session", "delete", "victim01", "--force"])
    assert result.exit_code == 0
    assert "deleted victim01" in result.output
    assert not scratch.exists("victim01")


def test_session_delete_confirm_no_keeps_it(scratch: SessionStore) -> None:
    _seed(scratch, "keepme01")
    result = CliRunner().invoke(main, ["session", "delete", "keepme01"], input="n\n")
    assert result.exit_code == 0
    assert "cancelled" in result.output
    assert scratch.exists("keepme01")


def test_session_delete_unknown_exits_nonzero(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["session", "delete", "ghost", "--force"])
    assert result.exit_code == 1
    assert "no session found" in result.output


# -- session cleanup --------------------------------------------------------


def test_session_cleanup_removes_old(scratch: SessionStore) -> None:
    _seed(scratch, "fresh001")
    _seed(scratch, "stale001")
    old = (datetime.now(UTC) - timedelta(days=45)).timestamp()
    os.utime(scratch.session_dir("stale001"), (old, old))
    result = CliRunner().invoke(main, ["session", "cleanup", "--days", "30", "--force"])
    assert result.exit_code == 0
    assert "removed 1" in result.output
    assert scratch.exists("fresh001")
    assert not scratch.exists("stale001")


# -- resume picker ----------------------------------------------------------


def test_resume_empty_store(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["resume"])
    assert result.exit_code == 0
    assert "no stored sessions" in result.output


def test_resume_picker_cancel(scratch: SessionStore) -> None:
    _seed(scratch, "aaaa1111")
    _seed(scratch, "bbbb2222")
    result = CliRunner().invoke(main, ["resume"], input="q\n")
    assert result.exit_code == 0
    assert "cancelled" in result.output


def test_resume_picker_selects_and_launches(
    scratch: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: dict[str, object] = {}

    async def fake_launch(*, demo: bool, bundle: str | None = None, resume_id: str | None = None) -> int:
        launched["resume_id"] = resume_id
        return 0

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    _seed(scratch, "aaaa1111", name="one")
    _seed(scratch, "bbbb2222", name="two")
    # Newest-first: bbbb2222 was saved last, so [1] is bbbb2222.
    os.utime(scratch.session_dir("bbbb2222"), None)
    result = CliRunner().invoke(main, ["resume"], input="1\n")
    assert result.exit_code == 0
    assert launched["resume_id"] in {"aaaa1111", "bbbb2222"}


def test_resume_direct_id_resolves_prefix(
    scratch: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_launch(*, demo: bool, bundle: str | None = None, resume_id: str | None = None) -> int:
        assert resume_id == "cafef00d"
        return 0

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    _seed(scratch, "cafef00d")
    result = CliRunner().invoke(main, ["resume", "cafe"])
    assert result.exit_code == 0


def test_resume_unknown_id_exits_nonzero(scratch: SessionStore) -> None:
    _seed(scratch, "cafef00d")
    result = CliRunner().invoke(main, ["resume", "zzz"])
    assert result.exit_code == 1
    assert "no session found" in result.output
