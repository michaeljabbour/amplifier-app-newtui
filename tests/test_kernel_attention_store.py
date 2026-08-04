"""Durable attention-state persistence (B7 gap 1).

Pure kernel-level coverage for :mod:`kernel.attention_store` (atomic
tmp-write + ``os.replace`` under the SAME ``kernel.file_lock`` idiom
``kernel/session_control.py`` uses) and the shared :mod:`kernel.file_lock`
extraction itself. UI-level hydration/dedupe-survives-restart behavior is
covered in ``tests/test_ui_notifications.py``.
"""

from __future__ import annotations

from pathlib import Path

from amplifier_app_tui.kernel.attention_store import (
    ATTENTION_FILENAME,
    AttentionRow,
    AttentionStore,
)
from amplifier_app_tui.kernel.file_lock import locked


def test_save_then_load_round_trips_rows_and_current(tmp_path: Path) -> None:
    store = AttentionStore(tmp_path)
    by_id = {
        "s1:completion:turn-1": AttentionRow(
            session_id="s1",
            reason="completion",
            event_id="s1:completion:turn-1",
            detail="",
            created_at=100.0,
            acknowledged=False,
        ),
        "s1:error:err-1": AttentionRow(
            session_id="s1",
            reason="error",
            event_id="s1:error:err-1",
            detail="boom",
            created_at=101.0,
            acknowledged=True,
        ),
    }
    current = {"s1": "s1:error:err-1"}

    store.save(by_id, current)
    loaded_by_id, loaded_current = store.load()

    assert loaded_by_id == by_id
    assert loaded_current == current


def test_save_is_atomic_no_tmp_file_left_behind(tmp_path: Path) -> None:
    store = AttentionStore(tmp_path)
    store.save({}, {})
    entries = list(tmp_path.iterdir())
    # Only the final file remains -- no stray .tmp<pid> sibling.
    assert entries == [tmp_path / ATTENTION_FILENAME]


def test_load_missing_file_returns_empty_never_raises(tmp_path: Path) -> None:
    store = AttentionStore(tmp_path / "does-not-exist-yet")
    by_id, current = store.load()
    assert by_id == {}
    assert current == {}


def test_load_corrupted_json_degrades_to_empty(tmp_path: Path) -> None:
    (tmp_path / ATTENTION_FILENAME).write_text("{not valid json", encoding="utf-8")
    store = AttentionStore(tmp_path)
    by_id, current = store.load()
    assert by_id == {}
    assert current == {}


def test_save_never_raises_when_directory_is_unwritable(tmp_path: Path) -> None:
    """A destination/persistence failure must never block or crash the
    session (B7 hard requirement) -- point the store at a path that cannot
    be created (a file standing where a directory is expected)."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    store = AttentionStore(blocker / "session-dir")
    store.save({}, {})  # must not raise
    by_id, current = store.load()
    assert by_id == {}
    assert current == {}


def test_save_uses_a_short_lock_timeout_by_default() -> None:
    """Durability must not make the notification path slow or blocking --
    the default lock timeout here is a small fraction of session_control's
    5s default, not a copy of it."""
    from amplifier_app_tui.kernel import attention_store

    assert attention_store._LOCK_TIMEOUT < 1.0


def test_attention_row_as_dict_round_trips_via_from_dict() -> None:
    row = AttentionRow(
        session_id="s1", reason="awaiting_approval", event_id="e1", detail="d", created_at=5.0
    )
    restored = AttentionRow.from_dict(row.as_dict())
    assert restored == row


def test_locked_is_reused_from_session_control_not_reinvented() -> None:
    """B7 gap 1 explicitly asks for reuse of session_control's idiom, not a
    second persistence mechanism -- prove session_control imports the SAME
    shared helper this module also uses."""
    from amplifier_app_tui.kernel import session_control

    assert session_control._file_lock is locked


def test_file_lock_breaks_a_stale_lock(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    lock_path = target.with_name(target.name + ".lock")
    lock_path.write_text("", encoding="utf-8")
    # Backdate the lock file well past any reasonable stale_after.
    old = 0.0
    import os

    os.utime(lock_path, (old, old))

    acquired_body_ran = False
    with locked(target, timeout=1.0, stale_after=0.01):
        acquired_body_ran = True
    assert acquired_body_ran
    assert not lock_path.exists()  # cleaned up after use
