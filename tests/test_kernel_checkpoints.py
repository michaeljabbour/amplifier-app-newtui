"""Workspace checkpoint persistence and conflict-safe restore tests."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_tui.kernel.checkpoints import (
    WorkspaceCheckpointUnavailableError,
    WorkspaceCheckpointStore,
    WorkspaceRestoreOutcome,
    _tool_paths,
)

ROOT = "session-root"


class FakeHooks:
    def __init__(self) -> None:
        self.registered: list[tuple[str, int, str]] = []
        self.unregistered: list[str] = []

    def register(
        self,
        event: str,
        _handler: Any,
        *,
        priority: int = 0,
        name: str = "",
    ) -> Any:
        self.registered.append((event, priority, name))
        return lambda: self.unregistered.append(name)


def _store(
    tmp_path: Path,
    *,
    max_checkpoints: int = 100,
    max_file_bytes: int = 8 * 1024 * 1024,
    max_checkpoint_snapshots: int = 512,
    max_checkpoint_bytes: int = 64 * 1024 * 1024,
) -> tuple[WorkspaceCheckpointStore, Path, Path]:
    workspace = tmp_path / "workspace"
    session = tmp_path / "session"
    workspace.mkdir()
    return (
        WorkspaceCheckpointStore(
            session,
            workspace,
            ROOT,
            max_checkpoints=max_checkpoints,
            max_file_bytes=max_file_bytes,
            max_checkpoint_snapshots=max_checkpoint_snapshots,
            max_checkpoint_bytes=max_checkpoint_bytes,
        ),
        workspace,
        session,
    )


async def _event(
    store: WorkspaceCheckpointStore,
    event: str,
    tool_name: str,
    call_id: str,
    tool_input: dict[str, Any] | None = None,
    *,
    session_id: str = ROOT,
) -> None:
    result = await store.handle_event(
        event,
        {
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "tool_input": tool_input or {},
        },
    )
    assert result.action == "continue"


def _json_files(path: Path) -> list[dict[str, Any]]:
    return [json.loads(item.read_text(encoding="utf-8")) for item in path.glob("*.json")]


@pytest.mark.asyncio
async def test_registers_all_boundaries_and_ignores_child_and_shell_edits(
    tmp_path: Path,
) -> None:
    store, workspace, _session = _store(tmp_path)
    hooks = FakeHooks()
    unregister = store.register_hooks(hooks)
    assert hooks.registered == [
        ("tool:pre", 980, "tui-workspace-checkpoint-tool-pre"),
        ("tool:post", 980, "tui-workspace-checkpoint-tool-post"),
        ("tool:error", 980, "tui-workspace-checkpoint-tool-error"),
    ]

    child_file = workspace / "child.txt"
    shell_file = workspace / "shell.txt"
    child_file.write_text("before child", encoding="utf-8")
    shell_file.write_text("before shell", encoding="utf-8")
    store.begin("cp-root-filter", "child and bash writes are outside the contract")

    await _event(
        store,
        "tool:pre",
        "write_file",
        "child-call",
        {"file_path": "child.txt"},
        session_id="session-child",
    )
    child_file.write_text("after child", encoding="utf-8")
    await _event(
        store,
        "tool:post",
        "write_file",
        "child-call",
        session_id="session-child",
    )
    await _event(store, "tool:pre", "bash", "shell-call", {"command": "edit shell.txt"})
    shell_file.write_text("after shell", encoding="utf-8")
    await _event(store, "tool:post", "bash", "shell-call")

    store.finish("cp-root-filter")
    outcome = store.restore("cp-root-filter")
    assert outcome == WorkspaceRestoreOutcome(checkpoint_id="cp-root-filter")
    assert outcome.summary == "nothing to restore"
    assert child_file.read_text(encoding="utf-8") == "after child"
    assert shell_file.read_text(encoding="utf-8") == "after shell"

    unregister()
    assert hooks.unregistered == [
        "tui-workspace-checkpoint-tool-error",
        "tui-workspace-checkpoint-tool-post",
        "tui-workspace-checkpoint-tool-pre",
    ]


@pytest.mark.asyncio
async def test_hook_capture_exception_never_blocks_the_native_tool(tmp_path: Path) -> None:
    store, _workspace, _session = _store(tmp_path)
    store.begin("cp-bad-path", "malformed direct tool path")

    result = await store.handle_event(
        "tool:pre",
        {
            "session_id": ROOT,
            "tool_name": "edit_file",
            "tool_call_id": "bad-path",
            "tool_input": {"file_path": "bad\x00path"},
        },
    )

    assert result.action == "continue"
    store.finish("cp-bad-path")
    outcome = store.restore("cp-bad-path")
    assert "(unknown)" in outcome.skipped_paths
    assert any("checkpoint capture failed" in warning for warning in outcome.warnings)


@pytest.mark.asyncio
async def test_preimage_is_durable_before_tool_runs_and_restores_after_reopen(
    tmp_path: Path,
) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "nested" / "note.txt"
    target.parent.mkdir()
    target.write_bytes(b"before\x00bytes")
    target.chmod(0o640)

    store.begin("cp-durable", "rewrite note")
    await _event(
        store,
        "tool:pre",
        "write_file",
        "write-1",
        {"file_path": "nested/note.txt"},
    )

    checkpoint_root = session / "workspace-checkpoints"
    pending = _json_files(checkpoint_root / "pending")
    assert len(pending) == 1
    before = pending[0]["targets"][0]["before"]
    assert before["kind"] == "regular"
    assert (checkpoint_root / "blobs" / before["digest"]).read_bytes() == b"before\x00bytes"
    assert stat.S_IMODE(checkpoint_root.stat().st_mode) == 0o700
    for persisted in checkpoint_root.rglob("*"):
        if persisted.is_file():
            assert stat.S_IMODE(persisted.stat().st_mode) == 0o600

    target.write_bytes(b"after")
    target.chmod(0o600)
    await _event(store, "tool:post", "write_file", "write-1")
    store.finish("cp-durable")
    assert not list((checkpoint_root / "pending").iterdir())

    reopened = WorkspaceCheckpointStore(session, workspace, ROOT)
    outcome = reopened.restore("cp-durable")
    assert outcome.restored_paths == ("nested/note.txt",)
    assert outcome.skipped_paths == ()
    assert outcome.summary == "restored 1 file"
    assert target.read_bytes() == b"before\x00bytes"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.parametrize(
    ("include_target", "expected"),
    [(True, b"state-a"), (False, b"state-b")],
)
@pytest.mark.asyncio
async def test_pre_prompt_range_respects_include_target(
    tmp_path: Path,
    include_target: bool,
    expected: bytes,
) -> None:
    store, workspace, _session = _store(tmp_path)
    target = workspace / "story.txt"
    target.write_bytes(b"state-a")

    store.begin("cp-1", "first prompt")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-1",
        {"file_path": "story.txt"},
    )
    target.write_bytes(b"state-b")
    await _event(store, "tool:post", "edit_file", "edit-1")
    store.finish("cp-1")

    store.begin("cp-2", "second prompt")
    await _event(
        store,
        "tool:pre",
        "apply_patch",
        "patch-2",
        {"type": "update_file", "path": "story.txt", "diff": "@@ replacement"},
    )
    target.write_bytes(b"state-c")
    await _event(store, "tool:post", "apply_patch", "patch-2")
    store.finish("cp-2")

    outcome = store.restore("cp-1", include_target=include_target)
    assert outcome.restored_paths == ("story.txt",)
    assert target.read_bytes() == expected


@pytest.mark.asyncio
async def test_created_and_deleted_files_restore_to_their_pre_prompt_states(
    tmp_path: Path,
) -> None:
    store, workspace, _session = _store(tmp_path)
    old = workspace / "old.txt"
    old.write_bytes(b"keep me")
    old.chmod(0o640)
    fresh = workspace / "fresh.txt"
    store.begin("cp-create-delete", "create one and delete another")

    await _event(
        store,
        "tool:pre",
        "create_file",
        "create-1",
        {"path": "fresh.txt"},
    )
    fresh.write_bytes(b"new")
    await _event(store, "tool:post", "create_file", "create-1")

    await _event(
        store,
        "tool:pre",
        "delete_file",
        "delete-1",
        {"path": "old.txt"},
    )
    old.unlink()
    await _event(store, "tool:post", "delete_file", "delete-1")
    store.finish("cp-create-delete")

    outcome = store.restore("cp-create-delete")
    assert set(outcome.restored_paths) == {"fresh.txt", "old.txt"}
    assert not fresh.exists()
    assert old.read_bytes() == b"keep me"
    assert stat.S_IMODE(old.stat().st_mode) == 0o640


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected"),
    [
        ("write_file", {"file_path": "write.txt"}, ("write.txt",)),
        ("edit_file", {"file_path": "edit.txt"}, ("edit.txt",)),
        ("create_file", {"path": "create.txt"}, ("create.txt",)),
        ("delete_file", {"path": "delete.txt"}, ("delete.txt",)),
        (
            "apply_patch",
            {"type": "update_file", "path": "native.txt", "diff": "@@"},
            ("native.txt",),
        ),
    ],
)
def test_structured_tool_path_shapes(
    tool_name: str,
    tool_input: dict[str, Any],
    expected: tuple[str, ...],
) -> None:
    assert _tool_paths(tool_name, tool_input) == expected


@pytest.mark.asyncio
async def test_function_apply_patch_tracks_add_update_delete_and_move(
    tmp_path: Path,
) -> None:
    store, workspace, _session = _store(tmp_path)
    update = workspace / "update.txt"
    deleted = workspace / "deleted.txt"
    source = workspace / "source.txt"
    added = workspace / "added.txt"
    destination = workspace / "destination.txt"
    update.write_bytes(b"update-before")
    deleted.write_bytes(b"delete-before")
    source.write_bytes(b"move-before")
    patch = """*** Begin Patch
*** Update File: update.txt
@@
*** Add File: added.txt
+new
*** Delete File: deleted.txt
*** Update File: source.txt
*** Move to: destination.txt
@@
*** End Patch"""
    assert _tool_paths("apply_patch", {"patch": patch}) == (
        "update.txt",
        "added.txt",
        "deleted.txt",
        "source.txt",
        "destination.txt",
    )

    store.begin("cp-function-patch", "multi-file patch")
    await _event(
        store,
        "tool:pre",
        "apply_patch",
        "patch-function",
        {"patch": patch},
    )
    update.write_bytes(b"update-after")
    added.write_bytes(b"added-after")
    deleted.unlink()
    source.rename(destination)
    await _event(store, "tool:post", "apply_patch", "patch-function")
    store.finish("cp-function-patch")

    outcome = store.restore("cp-function-patch")
    assert set(outcome.restored_paths) == {
        "update.txt",
        "added.txt",
        "deleted.txt",
        "source.txt",
        "destination.txt",
    }
    assert update.read_bytes() == b"update-before"
    assert not added.exists()
    assert deleted.read_bytes() == b"delete-before"
    assert source.read_bytes() == b"move-before"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_tool_error_finalizes_and_restores_a_partial_edit(tmp_path: Path) -> None:
    store, workspace, _session = _store(tmp_path)
    target = workspace / "partial.txt"
    target.write_bytes(b"before")
    store.begin("cp-error", "patch may fail after a partial write")
    await _event(
        store,
        "tool:pre",
        "apply_patch",
        "patch-error",
        {"type": "update_file", "path": "partial.txt", "diff": "@@"},
    )
    target.write_bytes(b"partially changed")
    await _event(store, "tool:error", "apply_patch", "patch-error")
    store.finish("cp-error")

    outcome = store.restore("cp-error")
    assert outcome.restored_paths == ("partial.txt",)
    assert target.read_bytes() == b"before"


@pytest.mark.asyncio
async def test_manual_conflicts_and_diverged_tracked_chains_are_skipped(
    tmp_path: Path,
) -> None:
    store, workspace, _session = _store(tmp_path)
    manual = workspace / "manual.txt"
    diverged = workspace / "diverged.txt"
    unrelated = workspace / "unrelated.txt"
    manual.write_bytes(b"manual-a")
    diverged.write_bytes(b"chain-a")
    unrelated.write_bytes(b"untouched")
    store.begin("cp-conflicts", "two conflict shapes")

    await _event(
        store,
        "tool:pre",
        "edit_file",
        "manual-edit",
        {"file_path": "manual.txt"},
    )
    manual.write_bytes(b"manual-b")
    await _event(store, "tool:post", "edit_file", "manual-edit")

    await _event(
        store,
        "tool:pre",
        "edit_file",
        "chain-1",
        {"file_path": "diverged.txt"},
    )
    diverged.write_bytes(b"chain-b")
    await _event(store, "tool:post", "edit_file", "chain-1")
    diverged.write_bytes(b"external-between-tools")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "chain-2",
        {"file_path": "diverged.txt"},
    )
    diverged.write_bytes(b"chain-d")
    await _event(store, "tool:post", "edit_file", "chain-2")
    store.finish("cp-conflicts")

    manual.write_bytes(b"manual-c-after-checkpoint")
    outcome = store.restore("cp-conflicts")
    assert outcome.restored_paths == ()
    assert set(outcome.skipped_paths) == {"manual.txt", "diverged.txt"}
    assert any("changed since checkpoint" in warning for warning in outcome.warnings)
    assert any("state chain diverged" in warning for warning in outcome.warnings)
    assert manual.read_bytes() == b"manual-c-after-checkpoint"
    assert diverged.read_bytes() == b"chain-d"
    assert unrelated.read_bytes() == b"untouched"


@pytest.mark.asyncio
async def test_partial_conflict_keeps_only_unresolved_path_retryable(tmp_path: Path) -> None:
    store, workspace, _session = _store(tmp_path)
    clean = workspace / "clean.txt"
    conflict = workspace / "conflict.txt"
    clean.write_bytes(b"clean-a")
    conflict.write_bytes(b"conflict-a")
    store.begin("cp-retry", "two edits")
    await _event(store, "tool:pre", "edit_file", "clean", {"file_path": "clean.txt"})
    clean.write_bytes(b"clean-b")
    await _event(store, "tool:post", "edit_file", "clean")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "conflict",
        {"file_path": "conflict.txt"},
    )
    conflict.write_bytes(b"conflict-b")
    await _event(store, "tool:post", "edit_file", "conflict")
    store.finish("cp-retry")

    conflict.write_bytes(b"external")
    first = store.restore("cp-retry")
    assert first.restored_paths == ("clean.txt",)
    assert first.skipped_paths == ("conflict.txt",)
    assert clean.read_bytes() == b"clean-a"
    assert store.checkpoint_status("cp-retry") == "active"

    # Resolve the CAS conflict back to the tracked after-state and retry.
    conflict.write_bytes(b"conflict-b")
    second = store.restore("cp-retry")
    assert second.restored_paths == ("conflict.txt",)
    assert second.skipped_paths == ()
    assert clean.read_bytes() == b"clean-a"  # completed path was not retried
    assert conflict.read_bytes() == b"conflict-a"
    assert store.checkpoint_status("cp-retry") == "retired"


@pytest.mark.asyncio
async def test_unsafe_targets_are_reported_and_never_restored(tmp_path: Path) -> None:
    store, workspace, _session = _store(tmp_path, max_file_bytes=4)
    real = workspace / "real.txt"
    real.write_bytes(b"real")
    (workspace / "link.txt").symlink_to(real)
    hard_source = workspace / "hard-source.txt"
    hard_source.write_bytes(b"hard")
    os.link(hard_source, workspace / "hard.txt")
    (workspace / "large.txt").write_bytes(b"12345")
    (workspace / "directory").mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_bytes(b"git")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")

    targets = (
        "link.txt",
        "hard.txt",
        "large.txt",
        "directory",
        ".git/config",
        str(outside),
    )
    store.begin("cp-unsafe", "unsafe paths")
    for index, target in enumerate(targets):
        call_id = f"unsafe-{index}"
        await _event(
            store,
            "tool:pre",
            "edit_file",
            call_id,
            {"file_path": target},
        )
        await _event(store, "tool:post", "edit_file", call_id)
    store.finish("cp-unsafe")

    outcome = store.restore("cp-unsafe")
    assert set(outcome.skipped_paths) == set(targets)
    warning_text = "\n".join(outcome.warnings)
    assert "symlinked paths" in warning_text
    assert "hard-linked files" in warning_text
    assert "exceeds 4 byte" in warning_text
    assert "non-regular files" in warning_text
    assert "git metadata" in warning_text
    assert "outside workspace root" in warning_text
    assert real.read_bytes() == b"real"
    assert hard_source.read_bytes() == b"hard"
    assert outside.read_bytes() == b"outside"


@pytest.mark.asyncio
async def test_per_checkpoint_snapshot_and_total_byte_caps_degrade_to_warnings(
    tmp_path: Path,
) -> None:
    store, workspace, _session = _store(
        tmp_path,
        max_checkpoint_snapshots=1,
        max_checkpoint_bytes=3,
    )
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_bytes(b"123")
    second.write_bytes(b"45")
    store.begin("cp-caps", "bounded checkpoint")
    await _event(store, "tool:pre", "edit_file", "first", {"file_path": "first.txt"})
    first.write_bytes(b"abc")
    await _event(store, "tool:post", "edit_file", "first")
    await _event(store, "tool:pre", "edit_file", "second", {"file_path": "second.txt"})
    second.write_bytes(b"zz")
    await _event(store, "tool:post", "edit_file", "second")
    store.finish("cp-caps")

    outcome = store.restore("cp-caps")
    assert outcome.restored_paths == ("first.txt",)
    assert outcome.skipped_paths == ("second.txt",)
    assert any("snapshot limit" in warning for warning in outcome.warnings)
    assert first.read_bytes() == b"123"
    assert second.read_bytes() == b"zz"


@pytest.mark.asyncio
async def test_total_preimage_byte_cap_is_enforced_independently(tmp_path: Path) -> None:
    store, workspace, session = _store(
        tmp_path,
        max_checkpoint_snapshots=10,
        max_checkpoint_bytes=3,
    )
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_bytes(b"12")
    second.write_bytes(b"34")
    store.begin("cp-byte-cap", "bounded bytes")
    await _event(store, "tool:pre", "edit_file", "first", {"file_path": "first.txt"})
    first.write_bytes(b"aa")
    await _event(store, "tool:post", "edit_file", "first")
    await _event(store, "tool:pre", "edit_file", "second", {"file_path": "second.txt"})
    second.write_bytes(b"bb")
    await _event(store, "tool:post", "edit_file", "second")
    store.finish("cp-byte-cap")

    outcome = store.restore("cp-byte-cap")
    assert outcome.restored_paths == ("first.txt",)
    assert outcome.skipped_paths == ("second.txt",)
    assert any("total byte limit" in warning for warning in outcome.warnings)
    assert first.read_bytes() == b"12"
    assert second.read_bytes() == b"bb"
    blobs = tuple((session / "workspace-checkpoints" / "blobs").iterdir())
    assert len(blobs) == 1
    assert sum(blob.stat().st_size for blob in blobs) <= 3


def test_checkpoint_storage_rejects_a_symlinked_private_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    session = tmp_path / "session"
    outside = tmp_path / "outside-checkpoints"
    workspace.mkdir()
    session.mkdir()
    outside.mkdir()
    (session / "workspace-checkpoints").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="not a real directory"):
        WorkspaceCheckpointStore(session, workspace, ROOT)


@pytest.mark.asyncio
async def test_unfinished_call_is_warned_and_its_preimage_is_cleaned(tmp_path: Path) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "unfinished.txt"
    target.write_bytes(b"before")
    store.begin("cp-unfinished", "denied tool with no terminal event")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "unfinished-call",
        {"file_path": "unfinished.txt"},
    )
    assert list((session / "workspace-checkpoints" / "pending").iterdir())

    store.finish("cp-unfinished")
    assert not list((session / "workspace-checkpoints" / "pending").iterdir())
    outcome = store.restore("cp-unfinished")
    assert outcome.skipped_paths == ("unfinished.txt",)
    assert any("unfinished tool call unfinished-call" in item for item in outcome.warnings)
    assert target.read_bytes() == b"before"


def test_retention_uses_opaque_ids_and_refuses_reuse(tmp_path: Path) -> None:
    store, _workspace, session = _store(tmp_path, max_checkpoints=2)
    checkpoint_ids = ("alpha", "bravo/with spaces", "charlie:opaque")
    for checkpoint_id in checkpoint_ids:
        store.begin(checkpoint_id, "prompt")
        store.finish(checkpoint_id)

    index = json.loads(
        (session / "workspace-checkpoints" / "index.json").read_text(encoding="utf-8")
    )
    assert index["order"] == ["bravo/with spaces", "charlie:opaque"]
    manifests = list((session / "workspace-checkpoints" / "manifests").iterdir())
    assert len(manifests) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}\.json", item.name) for item in manifests)
    with pytest.raises(KeyError, match="unknown checkpoint"):
        store.restore("alpha")
    with pytest.raises(ValueError, match="already exists"):
        store.begin("charlie:opaque", "duplicate")


def test_retention_failure_does_not_poison_the_active_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _workspace, _session = _store(tmp_path, max_checkpoints=1)
    store.begin("cp-1", "first")
    store.finish("cp-1")
    monkeypatch.setattr(store, "_prune", lambda: (_ for _ in ()).throw(OSError("busy")))

    store.begin("cp-2", "second")
    store.finish("cp-2")

    outcome = store.restore("cp-2")
    assert any("retention cleanup deferred" in warning for warning in outcome.warnings)


@pytest.mark.asyncio
async def test_failed_manifest_finalization_keeps_pending_preimage_and_allows_next_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "pending.txt"
    target.write_bytes(b"before")
    store.begin("cp-finish-fails", "edit")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "pending-edit",
        {"file_path": "pending.txt"},
    )
    original_write_manifest = store._write_manifest

    def fail_finalized(manifest: dict[str, Any]) -> None:
        if manifest.get("finished"):
            raise OSError("disk full")
        original_write_manifest(manifest)

    monkeypatch.setattr(store, "_write_manifest", fail_finalized)
    with pytest.raises(OSError, match="disk full"):
        store.finish("cp-finish-fails")
    assert list((session / "workspace-checkpoints" / "pending").iterdir())

    monkeypatch.setattr(store, "_write_manifest", original_write_manifest)
    store.begin("cp-next", "next turn still works")
    store.finish("cp-next")


@pytest.mark.asyncio
async def test_missing_preimage_blob_skips_instead_of_overwriting(tmp_path: Path) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "blob.txt"
    target.write_bytes(b"before")
    store.begin("cp-blob", "edit file")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "blob-edit",
        {"file_path": "blob.txt"},
    )
    target.write_bytes(b"after")
    await _event(store, "tool:post", "edit_file", "blob-edit")
    store.finish("cp-blob")

    manifests = _json_files(session / "workspace-checkpoints" / "manifests")
    before_digest = manifests[0]["operations"][0]["changes"][0]["before"]["digest"]
    (session / "workspace-checkpoints" / "blobs" / before_digest).unlink()
    outcome = store.restore("cp-blob")
    assert outcome.restored_paths == ()
    assert outcome.skipped_paths == ("blob.txt",)
    assert any("checkpoint blob unavailable" in item for item in outcome.warnings)
    assert target.read_bytes() == b"after"


@pytest.mark.asyncio
async def test_restore_truncates_abandoned_lineage_so_a_new_branch_can_rewind(
    tmp_path: Path,
) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "branch.txt"
    target.write_bytes(b"A")

    store.begin("cp-1", "first branch edit")
    await _event(store, "tool:pre", "edit_file", "edit-1", {"file_path": "branch.txt"})
    target.write_bytes(b"B")
    await _event(store, "tool:post", "edit_file", "edit-1")
    store.finish("cp-1")

    store.begin("cp-2", "abandoned branch edit")
    await _event(store, "tool:pre", "edit_file", "edit-2", {"file_path": "branch.txt"})
    target.write_bytes(b"C")
    await _event(store, "tool:post", "edit_file", "edit-2")
    store.finish("cp-2")

    assert store.restore("cp-2").restored_paths == ("branch.txt",)
    assert target.read_bytes() == b"B"
    index_path = session / "workspace-checkpoints" / "index.json"
    assert json.loads(index_path.read_text(encoding="utf-8"))["order"] == ["cp-1"]

    store.begin("cp-3", "new branch edit")
    await _event(store, "tool:pre", "edit_file", "edit-3", {"file_path": "branch.txt"})
    target.write_bytes(b"D")
    await _event(store, "tool:post", "edit_file", "edit-3")
    store.finish("cp-3")

    outcome = store.restore("cp-1")
    assert outcome.restored_paths == ("branch.txt",)
    assert outcome.skipped_paths == ()
    assert target.read_bytes() == b"A"
    assert json.loads(index_path.read_text(encoding="utf-8"))["order"] == []


def test_workspace_lock_excludes_other_session_store_until_finish(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sessions = tmp_path / "sessions"
    workspace.mkdir()
    store_a = WorkspaceCheckpointStore(sessions / "a", workspace, "session-a")
    store_b = WorkspaceCheckpointStore(sessions / "b", workspace, "session-b")

    store_b.begin("b-existing", "checkpoint available for restore")
    store_b.finish("b-existing")
    store_a.begin("a-active", "hold the workspace lease")

    with pytest.raises(WorkspaceCheckpointUnavailableError, match="in use by another TUI"):
        store_b.begin("b-blocked", "must fail closed")
    with pytest.raises(WorkspaceCheckpointUnavailableError, match="in use by another TUI"):
        store_b.restore("b-existing")

    store_a.finish("a-active")
    assert store_b.restore("b-existing") == WorkspaceRestoreOutcome(checkpoint_id="b-existing")
    store_b.begin("b-after-release", "lease is available again")
    store_b.finish("b-after-release")


def test_workspace_lock_releases_when_begin_and_finish_use_different_threads(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    sessions = tmp_path / "sessions"
    workspace.mkdir()
    store_a = WorkspaceCheckpointStore(sessions / "a", workspace, "session-a")
    store_b = WorkspaceCheckpointStore(sessions / "b", workspace, "session-b")

    with (
        ThreadPoolExecutor(max_workers=1) as begin_pool,
        ThreadPoolExecutor(max_workers=1) as finish_pool,
    ):
        begin_thread = begin_pool.submit(
            lambda: (store_a.begin("a-threaded", "begin elsewhere"), threading.get_ident())[1]
        ).result()
        finish_thread = finish_pool.submit(
            lambda: (store_a.finish("a-threaded"), threading.get_ident())[1]
        ).result()

        # Keep both executors alive so the OS cannot recycle one exited
        # worker's thread id and accidentally weaken this regression.
        assert begin_thread != finish_thread
        store_b.begin("b-after-thread-hop", "the workspace lease was released")
        store_b.finish("b-after-thread-hop")


@pytest.mark.asyncio
async def test_code_only_restore_preserves_target_and_descendant_anchors(
    tmp_path: Path,
) -> None:
    store, workspace, _session = _store(tmp_path)
    target = workspace / "anchors.txt"
    target.write_bytes(b"A")

    store.begin("c1", "A to B")
    await _event(store, "tool:pre", "edit_file", "edit-c1", {"file_path": "anchors.txt"})
    target.write_bytes(b"B")
    await _event(store, "tool:post", "edit_file", "edit-c1")
    store.finish("c1")

    store.begin("c2", "B to C")
    await _event(store, "tool:pre", "edit_file", "edit-c2", {"file_path": "anchors.txt"})
    target.write_bytes(b"C")
    await _event(store, "tool:post", "edit_file", "edit-c2")
    store.finish("c2")

    first = store.restore("c1", retain_target=True)
    assert first.restored_paths == ("anchors.txt",)
    assert target.read_bytes() == b"A"
    assert store.checkpoint_status("c1") == "active"
    assert store.checkpoint_status("c2") == "active"
    assert store._load_manifest("c1")["anchor"] is True
    assert store._load_manifest("c2")["anchor"] is True

    store.begin("c3", "new branch A to D")
    await _event(store, "tool:pre", "edit_file", "edit-c3", {"file_path": "anchors.txt"})
    target.write_bytes(b"D")
    await _event(store, "tool:post", "edit_file", "edit-c3")
    store.finish("c3")

    later = store.restore("c2")
    assert later.restored_paths == ("anchors.txt",)
    assert later.skipped_paths == ()
    assert target.read_bytes() == b"A"


@pytest.mark.asyncio
async def test_reconcile_visible_compacts_hidden_descendants_into_kept_checkpoint(
    tmp_path: Path,
) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "conversation-branch.txt"
    target.write_bytes(b"A")

    for checkpoint_id, before, after in (
        ("c1", b"A", b"B"),
        ("c2", b"B", b"C"),
        ("c3", b"C", b"D"),
    ):
        assert target.read_bytes() == before
        store.begin(checkpoint_id, f"{before.decode()} to {after.decode()}")
        await _event(
            store,
            "tool:pre",
            "edit_file",
            f"edit-{checkpoint_id}",
            {"file_path": "conversation-branch.txt"},
        )
        target.write_bytes(after)
        await _event(store, "tool:post", "edit_file", f"edit-{checkpoint_id}")
        store.finish(checkpoint_id)

    store.reconcile_visible(["c1"])
    index_path = session / "workspace-checkpoints" / "index.json"
    assert json.loads(index_path.read_text(encoding="utf-8"))["order"] == ["c1"]
    assert store.checkpoint_status("c2") == "retired"
    assert store.checkpoint_status("c3") == "retired"
    assert target.read_bytes() == b"D"

    store.begin("c4", "new visible branch D to E")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-c4",
        {"file_path": "conversation-branch.txt"},
    )
    target.write_bytes(b"E")
    await _event(store, "tool:post", "edit_file", "edit-c4")
    store.finish("c4")

    outcome = store.restore("c1")
    assert outcome.restored_paths == ("conversation-branch.txt",)
    assert outcome.skipped_paths == ()
    assert target.read_bytes() == b"A"


@pytest.mark.asyncio
async def test_staged_visible_reconcile_waits_for_marker_and_recovers_on_reopen(
    tmp_path: Path,
) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "staged-branch.txt"
    target.write_bytes(b"A")

    for checkpoint_id, before, after in (("c1", b"A", b"B"), ("c2", b"B", b"C")):
        assert target.read_bytes() == before
        store.begin(checkpoint_id, f"{before.decode()} to {after.decode()}")
        await _event(
            store,
            "tool:pre",
            "edit_file",
            f"edit-{checkpoint_id}",
            {"file_path": "staged-branch.txt"},
        )
        target.write_bytes(after)
        await _event(store, "tool:post", "edit_file", f"edit-{checkpoint_id}")
        store.finish(checkpoint_id)

    marker_id = "rewind-visible-branch"
    store.stage_visible_reconcile(["c1"], marker_id)
    assert store.pending_visible_reconcile is True
    store.close()

    # A crash before the conversation marker lands must not trim code lineage.
    before_marker = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert before_marker.pending_visible_reconcile is True
    assert json.loads(before_marker._index_path.read_text(encoding="utf-8"))["order"] == [
        "c1",
        "c2",
    ]
    before_marker.close()

    (session / "ui-events.jsonl").write_text(
        json.dumps({"kind": "rewind_marker", "event_id": marker_id}) + "\n",
        encoding="utf-8",
    )
    recovered = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert recovered.pending_visible_reconcile is False
    assert json.loads(recovered._index_path.read_text(encoding="utf-8"))["order"] == ["c1"]
    assert recovered.checkpoint_status("c2") == "retired"
    assert target.read_bytes() == b"C"  # conversation-only rewind leaves code untouched

    # The hidden B -> C transition was folded into c1, so a later code rewind
    # can still traverse the complete A -> B -> C lineage.
    outcome = recovered.restore("c1")
    assert outcome.restored_paths == ("staged-branch.txt",)
    assert outcome.skipped_paths == ()
    assert target.read_bytes() == b"A"


def test_cancelled_visible_intent_survives_unlink_failure_and_cleans_on_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace, session = _store(tmp_path)
    store.begin("c1", "existing visible checkpoint")
    store.finish("c1")
    store.stage_visible_reconcile([], "marker-that-must-never-arrive")
    intent_path = store._visible_intent_path
    real_unlink = Path.unlink
    failed_once = False

    def fail_intent_unlink_once(path: Path, missing_ok: bool = False) -> None:
        nonlocal failed_once
        if path == intent_path and not failed_once:
            failed_once = True
            raise OSError("visible intent cleanup unavailable")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_intent_unlink_once)
    store.cancel_visible_reconcile()

    assert intent_path.is_file()
    assert json.loads(intent_path.read_text(encoding="utf-8"))["cancelled"] is True
    assert store.pending_visible_reconcile is False

    monkeypatch.setattr(Path, "unlink", real_unlink)
    store.close()
    reopened = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert not intent_path.exists()
    assert reopened.pending_visible_reconcile is False
    assert reopened.checkpoint_status("c1") == "active"
    reopened.begin("c2", "a new turn is not wedged by cancelled recovery")
    reopened.finish("c2")


@pytest.mark.asyncio
async def test_restore_transaction_recovers_mid_commit_on_same_store_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "transaction.txt"
    target.write_bytes(b"A")
    store.begin("c1", "A to B")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-c1",
        {"file_path": "transaction.txt"},
    )
    target.write_bytes(b"B")
    await _event(store, "tool:post", "edit_file", "edit-c1")
    store.finish("c1")

    real_write_index = store._write_index
    fail_once = True

    def interrupt_index_commit(index: dict[str, Any]) -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("interrupted index commit")
        real_write_index(index)

    monkeypatch.setattr(store, "_write_index", interrupt_index_commit)
    outcome = store.restore("c1")
    assert outcome.restored_paths == ("transaction.txt",)
    assert target.read_bytes() == b"A"
    assert any("history could not be advanced" in warning for warning in outcome.warnings)
    assert store._transaction_path.is_file()
    stale = json.loads(
        (session / "workspace-checkpoints" / "index.json").read_text(encoding="utf-8")
    )
    assert stale["order"] == ["c1"]

    # Acquiring the same store for its next operation completes the durable
    # transaction before reading the index.
    assert store.checkpoint_status("c1") == "retired"
    assert not store._transaction_path.exists()
    recovered = json.loads(
        (session / "workspace-checkpoints" / "index.json").read_text(encoding="utf-8")
    )
    assert recovered["order"] == []

    reopened = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert reopened.checkpoint_status("c1") == "retired"
    assert (
        json.loads((session / "workspace-checkpoints" / "index.json").read_text(encoding="utf-8"))[
            "order"
        ]
        == []
    )


@pytest.mark.asyncio
async def test_failed_restore_transaction_creation_persists_send_gate_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace, _session = _store(tmp_path)
    target = workspace / "transaction-create.txt"
    target.write_bytes(b"before")
    store.begin("c-transaction-create", "edit before transaction failure")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-transaction-create",
        {"file_path": target.name},
    )
    target.write_bytes(b"after")
    await _event(store, "tool:post", "edit_file", "edit-transaction-create")
    store.finish("c-transaction-create")

    real_write_json = store._write_json
    fail_transaction_once = True

    def fail_transaction_creation(path: Path, value: dict[str, Any]) -> None:
        nonlocal fail_transaction_once
        if path == store._transaction_path and fail_transaction_once:
            fail_transaction_once = False
            raise OSError("transaction storage unavailable")
        real_write_json(path, value)

    monkeypatch.setattr(store, "_write_json", fail_transaction_creation)
    outcome = store.restore("c-transaction-create")
    assert target.read_bytes() == b"before"
    assert outcome.restored_paths == ("transaction-create.txt",)
    assert any("history could not be advanced" in warning for warning in outcome.warnings)
    assert store.recovery_required == ("c-transaction-create",)
    required = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in store._restores.glob("restore-*.json")
    ]
    assert any(item.get("recovery_required") is True for item in required)
    with pytest.raises(WorkspaceCheckpointUnavailableError, match="still needs attention"):
        store.begin("blocked", "the failed lineage commit must gate new work")

    # The file mutation is idempotently confirmed and the lineage transaction
    # succeeds on explicit retry, which durably releases the gate.
    retry = store.restore("c-transaction-create")
    assert retry.skipped_paths == ()
    assert retry.warnings == ()
    assert store.recovery_required == ()
    assert store.checkpoint_status("c-transaction-create") == "retired"


@pytest.mark.asyncio
async def test_incomplete_restore_journal_auto_recovers_files_and_branch_on_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace, session = _store(tmp_path)
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_bytes(b"first-before")
    second.write_bytes(b"second-before")
    store.begin("c-journal", "edit two files")
    for call_id, target, after in (
        ("edit-first", first, b"first-after"),
        ("edit-second", second, b"second-after"),
    ):
        await _event(
            store,
            "tool:pre",
            "edit_file",
            call_id,
            {"file_path": target.name},
        )
        target.write_bytes(after)
        await _event(store, "tool:post", "edit_file", call_id)
    store.finish("c-journal")

    class SimulatedProcessExit(BaseException):
        pass

    real_apply_restore = store._apply_restore
    interrupted = False

    def exit_after_first_file(
        relative: str, expected: dict[str, Any], desired: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal interrupted
        restored = real_apply_restore(relative, expected, desired)
        if not interrupted:
            interrupted = True
            raise SimulatedProcessExit
        return restored

    monkeypatch.setattr(store, "_apply_restore", exit_after_first_file)
    with pytest.raises(SimulatedProcessExit):
        store.restore("c-journal")

    assert (first.read_bytes(), second.read_bytes()) == (b"first-before", b"second-after")
    unfinished = [
        path
        for path in (session / "workspace-checkpoints" / "restores").glob("restore-*.json")
        if not json.loads(path.read_text(encoding="utf-8")).get("finished_ns")
    ]
    assert len(unfinished) == 1
    store.close()

    # Constructor recovery sees the desired bytes on the first file as an
    # idempotent success, restores the remaining file, and commits the branch.
    reopened = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert (first.read_bytes(), second.read_bytes()) == (b"first-before", b"second-before")
    assert reopened.checkpoint_status("c-journal") == "retired"
    assert reopened.recovery_required == ()
    recovered_journal = json.loads(unfinished[0].read_text(encoding="utf-8"))
    assert recovered_journal["finished_ns"]
    assert recovered_journal["recovered_ns"]
    assert recovered_journal["recovery"] == "restored 2 files"


@pytest.mark.asyncio
async def test_process_exit_between_file_restore_and_lineage_commit_recovers_on_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace, session = _store(tmp_path)
    target = workspace / "lineage-window.txt"
    target.write_bytes(b"before")
    store.begin("c-lineage-window", "edit before process exit")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-lineage-window",
        {"file_path": target.name},
    )
    target.write_bytes(b"after")
    await _event(store, "tool:post", "edit_file", "edit-lineage-window")
    store.finish("c-lineage-window")

    class SimulatedProcessExit(BaseException):
        pass

    def exit_before_lineage_commit(
        _index: dict[str, Any], _manifests: list[dict[str, Any]]
    ) -> None:
        raise SimulatedProcessExit

    monkeypatch.setattr(store, "_commit_restore_state", exit_before_lineage_commit)
    with pytest.raises(SimulatedProcessExit):
        store.restore("c-lineage-window")
    assert target.read_bytes() == b"before"
    unfinished = [
        path
        for path in (session / "workspace-checkpoints" / "restores").glob("restore-*.json")
        if not json.loads(path.read_text(encoding="utf-8")).get("finished_ns")
    ]
    assert len(unfinished) == 1
    store.close()

    reopened = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert target.read_bytes() == b"before"
    assert reopened.checkpoint_status("c-lineage-window") == "retired"
    assert reopened.recovery_required == ()
    recovered_journal = json.loads(unfinished[0].read_text(encoding="utf-8"))
    assert recovered_journal["finished_ns"]
    assert recovered_journal["recovery"] == "restored 1 file"


@pytest.mark.asyncio
async def test_partial_journal_recovery_gate_survives_two_reopens_until_explicit_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace, session = _store(tmp_path)
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_bytes(b"first-before")
    second.write_bytes(b"second-before")
    store.begin("c-recovery-gate", "edit two files")
    for call_id, target, after in (
        ("edit-first", first, b"first-after"),
        ("edit-second", second, b"second-after"),
    ):
        await _event(
            store,
            "tool:pre",
            "edit_file",
            call_id,
            {"file_path": target.name},
        )
        target.write_bytes(after)
        await _event(store, "tool:post", "edit_file", call_id)
    store.finish("c-recovery-gate")

    class SimulatedProcessExit(BaseException):
        pass

    real_apply_restore = store._apply_restore
    interrupted = False

    def exit_after_first_file(
        relative: str, expected: dict[str, Any], desired: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal interrupted
        restored = real_apply_restore(relative, expected, desired)
        if not interrupted:
            interrupted = True
            raise SimulatedProcessExit
        return restored

    monkeypatch.setattr(store, "_apply_restore", exit_after_first_file)
    with pytest.raises(SimulatedProcessExit):
        store.restore("c-recovery-gate")
    assert first.read_bytes() == b"first-before"
    assert second.read_bytes() == b"second-after"

    # Force the constructor's automatic replay to conflict on the remaining
    # path. Its send gate must survive beyond this first recovered process.
    second.write_bytes(b"external-change")
    store.close()
    reopened_once = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert reopened_once.recovery_required == ("c-recovery-gate",)
    required_journals = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (session / "workspace-checkpoints" / "restores").glob("restore-*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("recovery_required") is True
    ]
    assert len(required_journals) == 1
    with pytest.raises(WorkspaceCheckpointUnavailableError, match="still needs attention"):
        reopened_once.begin("blocked", "must not send while recovery is unresolved")
    reopened_once.close()

    reopened_twice = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert reopened_twice.recovery_required == ("c-recovery-gate",)
    with pytest.raises(WorkspaceCheckpointUnavailableError, match="still needs attention"):
        reopened_twice.begin("still-blocked", "the durable gate must survive another reopen")

    # An explicit retry from the checkpoint's expected state resolves the
    # remaining path and durably clears every recovery-required journal.
    second.write_bytes(b"second-after")
    outcome = reopened_twice.restore("c-recovery-gate")
    assert outcome.skipped_paths == ()
    assert outcome.warnings == ()
    assert second.read_bytes() == b"second-before"
    assert reopened_twice.recovery_required == ()
    reopened_twice.close()

    resolved = WorkspaceCheckpointStore(session, workspace, ROOT)
    assert resolved.recovery_required == ()
    resolved.begin("allowed", "new work can start after explicit recovery")
    resolved.finish("allowed")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{", "restore journal is unreadable"),
        ("[]", "restore journal is invalid"),
        ("{}", "restore journal lacks a checkpoint id"),
    ],
)
def test_malformed_restore_journal_blocks_begin_and_reopen(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    store, workspace, session = _store(tmp_path)
    journal = store._restores / "restore-corrupt.json"
    journal.write_text(payload, encoding="utf-8")

    with pytest.raises(WorkspaceCheckpointUnavailableError, match=message):
        store.begin("blocked", "a corrupt crash marker must fail closed")
    assert store._active_checkpoint is None
    assert store._ownership_proxy is None
    assert journal.read_text(encoding="utf-8") == payload
    store.close()

    with pytest.raises(WorkspaceCheckpointUnavailableError, match=message):
        WorkspaceCheckpointStore(session, workspace, ROOT)
    assert journal.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("payload", ["{", "[]"])
def test_malformed_restore_transaction_blocks_begin_and_reopen(
    tmp_path: Path,
    payload: str,
) -> None:
    store, workspace, session = _store(tmp_path)
    transaction = store._transaction_path
    transaction.write_text(payload, encoding="utf-8")

    with pytest.raises(
        WorkspaceCheckpointUnavailableError,
        match="workspace checkpoint recovery failed",
    ):
        store.begin("blocked", "an uncertain lineage transaction must fail closed")
    assert store._active_checkpoint is None
    assert store._ownership_proxy is None
    assert transaction.read_text(encoding="utf-8") == payload
    store.close()

    with pytest.raises(
        WorkspaceCheckpointUnavailableError,
        match="workspace checkpoint recovery failed",
    ):
        WorkspaceCheckpointStore(session, workspace, ROOT)
    assert transaction.read_text(encoding="utf-8") == payload


@pytest.mark.asyncio
async def test_file_with_extended_attribute_is_skipped_when_supported(tmp_path: Path) -> None:
    setxattr = getattr(os, "setxattr", None)
    listxattr = getattr(os, "listxattr", None)
    store, workspace, _session = _store(tmp_path)
    target = workspace / "xattr.txt"
    target.write_bytes(b"before")
    if callable(setxattr) and callable(listxattr):
        attribute = "user.amplifier_checkpoint_test"
        try:
            setxattr(target, attribute, b"present", follow_symlinks=False)
            if attribute not in listxattr(target, follow_symlinks=False):
                pytest.skip("filesystem did not retain the test xattr")
        except (OSError, TypeError) as error:
            pytest.skip(f"filesystem xattrs unavailable: {error}")
    elif sys.platform == "darwin":
        subprocess.run(
            [
                "/usr/bin/xattr",
                "-w",
                "com.openai.amplifier-checkpoint-test",
                "present",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
    else:
        pytest.skip("platform has no xattr API")

    store.begin("cp-xattr", "xattrs cannot be restored safely")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-xattr",
        {"file_path": "xattr.txt"},
    )
    target.write_bytes(b"after")
    await _event(store, "tool:post", "edit_file", "edit-xattr")
    store.finish("cp-xattr")

    outcome = store.restore("cp-xattr")
    assert outcome.restored_paths == ()
    assert outcome.skipped_paths == ("xattr.txt",)
    assert any("extended attributes or ACLs" in warning for warning in outcome.warnings)
    assert target.read_bytes() == b"after"


@pytest.mark.asyncio
async def test_file_with_macos_extended_acl_is_skipped(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS ACL fixture")
    store, workspace, _session = _store(tmp_path)
    target = workspace / "acl.txt"
    target.write_bytes(b"before")
    subprocess.run(
        ["/bin/chmod", "+a", "everyone deny delete", str(target)],
        check=True,
        capture_output=True,
    )

    def remove_test_acl() -> None:
        if target.exists():
            subprocess.run(
                ["/bin/chmod", "-N", str(target)],
                check=False,
                capture_output=True,
            )

    request.addfinalizer(remove_test_acl)
    try:
        store.begin("cp-acl", "ACLs cannot be restored safely")
        await _event(store, "tool:pre", "edit_file", "edit-acl", {"file_path": "acl.txt"})
        target.write_bytes(b"after")
        await _event(store, "tool:post", "edit_file", "edit-acl")
        store.finish("cp-acl")

        outcome = store.restore("cp-acl")
        assert outcome.restored_paths == ()
        assert outcome.skipped_paths == ("acl.txt",)
        assert any("extended attributes or ACLs" in warning for warning in outcome.warnings)
        assert target.read_bytes() == b"after"
    finally:
        # Remove the deny-delete fixture so pytest can clean the temporary tree.
        subprocess.run(["/bin/chmod", "-N", str(target)], check=False, capture_output=True)


@pytest.mark.asyncio
async def test_incomplete_finalized_write_recovery_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace, _session = _store(tmp_path)
    target = workspace / "finish-retry.txt"
    target.write_bytes(b"before")
    store.begin("cp-finish-retry", "completed edit before finish write")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-finish-retry",
        {"file_path": "finish-retry.txt"},
    )
    target.write_bytes(b"after")
    await _event(store, "tool:post", "edit_file", "edit-finish-retry")

    real_write_manifest = store._write_manifest

    def fail_finished_manifest(manifest: dict[str, Any]) -> None:
        if manifest.get("finished"):
            raise OSError("finalized manifest unavailable")
        real_write_manifest(manifest)

    monkeypatch.setattr(store, "_write_manifest", fail_finished_manifest)
    with pytest.raises(OSError, match="finalized manifest unavailable"):
        store.finish("cp-finish-retry")
    with pytest.raises(OSError, match="finalized manifest unavailable"):
        store.restore("cp-finish-retry")

    monkeypatch.setattr(store, "_write_manifest", real_write_manifest)
    outcome = store.restore("cp-finish-retry")
    assert outcome.restored_paths == ("finish-retry.txt",)
    assert outcome.skipped_paths == ()
    assert outcome.warnings == ()
    assert target.read_bytes() == b"before"
    assert store.checkpoint_status("cp-finish-retry") == "retired"


def test_pruned_opaque_checkpoint_id_cannot_be_reused_after_reopen(tmp_path: Path) -> None:
    store, workspace, session = _store(tmp_path, max_checkpoints=1)
    opaque_id = "alpha/opaque id with spaces"
    store.begin(opaque_id, "first")
    store.finish(opaque_id)
    store.begin("bravo", "forces alpha out of active retention")
    store.finish("bravo")
    assert store.checkpoint_status(opaque_id) == "expired"

    reopened = WorkspaceCheckpointStore(session, workspace, ROOT, max_checkpoints=1)
    with pytest.raises(ValueError, match="checkpoint id already exists"):
        reopened.begin(opaque_id, "must not reuse an old identity")


@pytest.mark.asyncio
async def test_parent_directory_replacement_after_tracked_delete_skips_restore(
    tmp_path: Path,
) -> None:
    store, workspace, _session = _store(tmp_path)
    parent = workspace / "parent"
    parent.mkdir()
    target = parent / "deleted.txt"
    target.write_bytes(b"restore me")

    store.begin("cp-delete-parent", "delete a tracked nested file")
    await _event(
        store,
        "tool:pre",
        "delete_file",
        "delete-nested",
        {"path": "parent/deleted.txt"},
    )
    target.unlink()
    await _event(store, "tool:post", "delete_file", "delete-nested")
    store.finish("cp-delete-parent")

    parent.rmdir()
    parent.mkdir()
    outcome = store.restore("cp-delete-parent")

    assert outcome.restored_paths == ()
    assert outcome.skipped_paths == ("parent/deleted.txt",)
    assert any("changed since checkpoint" in warning for warning in outcome.warnings)
    assert not target.exists()


@pytest.mark.asyncio
async def test_idempotent_restore_retries_failed_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import amplifier_app_tui.kernel.checkpoints as checkpoints_module

    store, workspace, _session = _store(tmp_path)
    target = workspace / "durability.txt"
    target.write_bytes(b"before")

    store.begin("cp-durability", "change a file")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-durability",
        {"file_path": "durability.txt"},
    )
    target.write_bytes(b"after")
    await _event(store, "tool:post", "edit_file", "edit-durability")
    store.finish("cp-durability")

    real_fsync = checkpoints_module._fsync_fd_strict
    workspace_fsyncs = 0

    def fail_twice(descriptor: int) -> None:
        nonlocal workspace_fsyncs
        workspace_fsyncs += 1
        if workspace_fsyncs <= 2:
            raise OSError("directory fsync unavailable")
        real_fsync(descriptor)

    monkeypatch.setattr(checkpoints_module, "_fsync_fd_strict", fail_twice)

    first = store.restore("cp-durability")
    assert target.read_bytes() == b"before"
    assert first.restored_paths == ()
    assert first.skipped_paths == ("durability.txt",)
    assert store.checkpoint_status("cp-durability") == "active"

    # The bytes already match on retry, but history must remain active until
    # the failed parent-directory durability barrier itself succeeds.
    second = store.restore("cp-durability")
    assert second.restored_paths == ()
    assert second.skipped_paths == ("durability.txt",)
    assert any("not durably confirmed" in warning for warning in second.warnings)
    assert store.checkpoint_status("cp-durability") == "active"

    third = store.restore("cp-durability")
    assert third.restored_paths == ("durability.txt",)
    assert third.skipped_paths == ()
    assert store.checkpoint_status("cp-durability") == "retired"
    assert workspace_fsyncs == 3


def test_capture_fails_closed_when_intermediate_directory_becomes_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, workspace, _session = _store(tmp_path)
    parent = workspace / "nested"
    moved_parent = workspace / "nested-original"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    (parent / "target.txt").write_bytes(b"inside")
    (outside / "target.txt").write_bytes(b"outside-user-work")

    real_safe_relative = store._safe_relative
    swapped = False

    def swap_after_validation(raw_path: str) -> tuple[str | None, str]:
        nonlocal swapped
        result = real_safe_relative(raw_path)
        if not swapped:
            parent.rename(moved_parent)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(store, "_safe_relative", swap_after_validation)
    try:
        state = store._capture_relative("nested/target.txt", persist_blob=False)
        assert state["kind"] == "skipped"
        assert "cannot bind workspace path safely" in state["reason"]
        assert (outside / "target.txt").read_bytes() == b"outside-user-work"
    finally:
        if parent.is_symlink():
            parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(parent)


@pytest.mark.asyncio
async def test_restore_stays_bound_when_intermediate_directory_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, workspace, _session = _store(tmp_path)
    parent = workspace / "nested"
    moved_parent = workspace / "nested-original"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    target = parent / "target.txt"
    outside_target = outside / "target.txt"
    target.write_bytes(b"before")
    outside_target.write_bytes(b"outside-user-work")

    store.begin("cp-dirfd-race", "edit a nested file")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-dirfd-race",
        {"file_path": "nested/target.txt"},
    )
    target.write_bytes(b"after")
    await _event(store, "tool:post", "edit_file", "edit-dirfd-race")
    store.finish("cp-dirfd-race")

    real_capture_bound = store._capture_bound
    captures = 0
    swapped = False

    def capture_then_swap(
        parent_fd: int,
        leaf: str,
        *,
        persist_blob: bool,
        max_persist_bytes: int | None = None,
    ) -> dict[str, Any]:
        nonlocal captures, swapped
        captures += 1
        state = real_capture_bound(
            parent_fd,
            leaf,
            persist_blob=persist_blob,
            max_persist_bytes=max_persist_bytes,
        )
        # Call 1 is restore planning. Call 2 runs after _apply_restore has
        # bound the original parent descriptor; redirect the pathname then.
        if captures == 2:
            parent.rename(moved_parent)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return state

    monkeypatch.setattr(store, "_capture_bound", capture_then_swap)
    try:
        outcome = store.restore("cp-dirfd-race")
        assert swapped is True
        assert outside_target.read_bytes() == b"outside-user-work"
        assert (moved_parent / "target.txt").read_bytes() == b"before"
        # The mutation stayed on the bound original directory, but the public
        # path changed before final lineage verification, so history remains
        # retryable rather than blessing the now-hidden result.
        assert outcome.restored_paths == ()
        assert outcome.skipped_paths == ("nested/target.txt",)
        assert any("changed immediately after restore" in item for item in outcome.warnings)
    finally:
        if parent.is_symlink():
            parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(parent)


@pytest.mark.asyncio
async def test_bound_nested_create_edit_delete_restore_normally(tmp_path: Path) -> None:
    store, workspace, _session = _store(tmp_path)
    parent = workspace / "nested"
    parent.mkdir()
    edited = parent / "edited.txt"
    deleted = parent / "deleted.txt"
    created = parent / "created.txt"
    edited.write_bytes(b"edit-before")
    deleted.write_bytes(b"delete-before")

    store.begin("cp-bound-normal", "nested create edit delete")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "bound-edit",
        {"file_path": "nested/edited.txt"},
    )
    edited.write_bytes(b"edit-after")
    await _event(store, "tool:post", "edit_file", "bound-edit")
    await _event(
        store,
        "tool:pre",
        "delete_file",
        "bound-delete",
        {"path": "nested/deleted.txt"},
    )
    deleted.unlink()
    await _event(store, "tool:post", "delete_file", "bound-delete")
    await _event(
        store,
        "tool:pre",
        "create_file",
        "bound-create",
        {"path": "nested/created.txt"},
    )
    created.write_bytes(b"created-after")
    await _event(store, "tool:post", "create_file", "bound-create")
    store.finish("cp-bound-normal")

    outcome = store.restore("cp-bound-normal")
    assert set(outcome.restored_paths) == {
        "nested/created.txt",
        "nested/deleted.txt",
        "nested/edited.txt",
    }
    assert outcome.skipped_paths == ()
    assert edited.read_bytes() == b"edit-before"
    assert deleted.read_bytes() == b"delete-before"
    assert not created.exists()


@pytest.mark.asyncio
async def test_restore_fails_closed_without_secure_dirfd_primitives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import amplifier_app_tui.kernel.checkpoints as checkpoints_module

    store, workspace, _session = _store(tmp_path)
    target = workspace / "unsupported.txt"
    target.write_bytes(b"before")
    store.begin("cp-no-dirfd", "edit without secure host primitives")
    await _event(
        store,
        "tool:pre",
        "edit_file",
        "edit-no-dirfd",
        {"file_path": "unsupported.txt"},
    )
    target.write_bytes(b"after")
    await _event(store, "tool:post", "edit_file", "edit-no-dirfd")
    store.finish("cp-no-dirfd")

    monkeypatch.setattr(checkpoints_module, "_secure_dirfd_supported", lambda: False)
    outcome = store.restore("cp-no-dirfd")

    assert outcome.restored_paths == ()
    assert outcome.skipped_paths == ("unsupported.txt",)
    assert any(
        "secure descriptor-relative traversal is unavailable" in item for item in outcome.warnings
    )
    assert target.read_bytes() == b"after"
