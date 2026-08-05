"""Session-local workspace checkpoints for safe, direct-edit undo.

Amplifier Foundation can fork conversation state, but it does not snapshot the
workspace.  This module supplies the missing kernel mechanism for the TUI.  It
tracks only root-session calls to the structured filesystem tools whose target
paths are knowable *before* execution.  Shell commands, MCP tools and child
sessions are deliberately outside the contract.

The important safety property is compare-and-swap restoration: a recorded file
is restored only while its current bytes still equal the last state written by
the tracked tool chain.  Manual, shell, subagent or concurrent changes therefore
turn into explicit skips instead of being overwritten.
"""

from __future__ import annotations

import hashlib
import ctypes
import json
import logging
import os
import secrets
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from amplifier_core import HookResult

_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)
_TRACKED_TOOLS = frozenset({"write_file", "edit_file", "create_file", "delete_file", "apply_patch"})
_PATCH_PATH_MARKERS = (
    "*** Add File: ",
    "*** Update File: ",
    "*** Delete File: ",
)


class WorkspaceCheckpointUnavailableError(RuntimeError):
    """Checkpoint ownership/storage could not safely accept a user turn."""


@dataclass(frozen=True, slots=True)
class WorkspaceRestoreOutcome:
    """Result of one best-effort, conflict-safe workspace restore."""

    checkpoint_id: str
    restored_paths: tuple[str, ...] = ()
    skipped_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        restored = len(self.restored_paths)
        skipped = len(self.skipped_paths)
        if restored == 0 and skipped == 0:
            return "nothing to restore"
        text = f"restored {restored} file{'s' if restored != 1 else ''}"
        if skipped:
            text += f" · skipped {skipped}"
        return text


class WorkspaceCheckpointStore:
    """Persist and restore pre-prompt snapshots for one root session.

    ``checkpoint_id`` values are opaque identities.  They are hashed for file
    names and may never be reused, including after a conversation rewind.
    """

    def __init__(
        self,
        session_dir: Path,
        workspace_root: Path,
        root_session_id: str,
        max_checkpoints: int = 100,
        max_file_bytes: int = 8 * 1024 * 1024,
        max_checkpoint_snapshots: int = 512,
        max_checkpoint_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_checkpoints < 1:
            raise ValueError("max_checkpoints must be at least 1")
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be at least 1")
        if max_checkpoint_snapshots < 1:
            raise ValueError("max_checkpoint_snapshots must be at least 1")
        if max_checkpoint_bytes < 1:
            raise ValueError("max_checkpoint_bytes must be at least 1")
        if not root_session_id.strip():
            raise ValueError("root_session_id cannot be empty")

        self.session_dir = Path(session_dir)
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._workspace_identity = _directory_identity(self.workspace_root)
        self.root_session_id = root_session_id
        self.max_checkpoints = max_checkpoints
        self.max_file_bytes = max_file_bytes
        self.max_checkpoint_snapshots = max_checkpoint_snapshots
        self.max_checkpoint_bytes = max_checkpoint_bytes

        self._root = self.session_dir / "workspace-checkpoints"
        self._manifests = self._root / "manifests"
        self._pending_dir = self._root / "pending"
        self._blobs = self._root / "blobs"
        self._restores = self._root / "restores"
        self._index_path = self._root / "index.json"
        workspace_key = hashlib.sha256(str(self.workspace_root).encode("utf-8")).hexdigest()[:24]
        # Every TUI session targeting this workspace shares the same lease.
        # Root structured-tool turns and restores therefore cannot overlap
        # across sessions, while idle sessions remain concurrently usable.
        self._ownership_path = (
            self.session_dir.parent / f".workspace-checkpoints-{workspace_key}.lock"
        )
        self._transaction_path = self._root / "restore-transaction.json"
        self._visible_intent_path = self._root / "visible-branch-intent.json"
        self._lock = threading.RLock()
        self._ownership_lock = FileLock(
            str(self._ownership_path),
            timeout=0,
            mode=0o600,
            thread_local=False,
        )
        self._ownership_proxy: Any | None = None
        self._active_checkpoint: str | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._recovery_required: set[str] = set()
        self._recovering_journals = False

        for directory in (
            self._root,
            self._manifests,
            self._pending_dir,
            self._blobs,
            self._restores,
        ):
            _ensure_private_dir(directory)
        with self._exclusive_storage("initialize"):
            if not self._index_path.exists():
                self._write_index({"schema": _SCHEMA_VERSION, "order": [], "used": []})
            try:
                self._reconcile_staged_visible_locked()
            except (OSError, RuntimeError, ValueError) as error:
                raise WorkspaceCheckpointUnavailableError(
                    f"workspace branch recovery failed: {error}"
                ) from error

    def begin(self, checkpoint_id: str, prompt: str) -> None:
        """Open an immutable pre-prompt checkpoint before the turn runs."""
        checkpoint_id = _valid_id(checkpoint_id)
        with self._lock:
            if self._active_checkpoint is not None:
                raise RuntimeError(f"checkpoint still active: {self._active_checkpoint}")
            try:
                self._acquire_storage("begin checkpoint")
                if self._recovery_required:
                    pending = ", ".join(sorted(self._recovery_required))
                    raise WorkspaceCheckpointUnavailableError(
                        "an interrupted workspace restore still needs attention "
                        f"({pending}); retry that checkpoint before sending"
                    )
                index = self._read_index()
                if checkpoint_id in index["used"] or self._manifest_path(checkpoint_id).exists():
                    raise ValueError(f"checkpoint id already exists: {checkpoint_id}")
                manifest = {
                    "schema": _SCHEMA_VERSION,
                    "checkpoint_id": checkpoint_id,
                    # File undo does not need prompt contents. Keep only a digest
                    # so private checkpoint storage does not duplicate sensitive
                    # conversation text.
                    "prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "root_session_id": self.root_session_id,
                    "workspace_root": str(self.workspace_root),
                    "created_ns": time.time_ns(),
                    "finished": False,
                    "captured_snapshots": 0,
                    "captured_bytes": 0,
                    "operations": [],
                    "warnings": [],
                }
                self._write_manifest(manifest)
                index["order"].append(checkpoint_id)
                index["used"].append(checkpoint_id)
                self._write_index(index)
                self._active_checkpoint = checkpoint_id
                try:
                    self._prune()
                except (OSError, ValueError) as error:
                    # Retention maintenance must never discard an otherwise
                    # durable checkpoint or block the user turn.
                    try:
                        manifest = self._load_manifest(checkpoint_id)
                        manifest["warnings"].append(
                            {
                                "path": "(checkpoint)",
                                "reason": f"retention cleanup deferred: {error}",
                            }
                        )
                        self._write_manifest(manifest)
                    except Exception:  # noqa: BLE001 — checkpoint is already durable
                        logger.warning(
                            "workspace checkpoint retention cleanup failed", exc_info=True
                        )
            except Exception:
                if self._active_checkpoint is None:
                    self._release_storage()
                raise

    def finish(self, checkpoint_id: str) -> None:
        """Finalize a checkpoint after its prompt closes."""
        checkpoint_id = _valid_id(checkpoint_id)
        with self._lock:
            if self._active_checkpoint != checkpoint_id:
                raise RuntimeError(f"checkpoint is not active: {checkpoint_id}")
            try:
                self._finalize_active_checkpoint(checkpoint_id)
            finally:
                # Any storage failure degrades this checkpoint, not every
                # future turn. Pending records remain durable unless their
                # finalized manifest reached disk first.
                self._active_checkpoint = None
                self._release_storage()

    def _finalize_active_checkpoint(self, checkpoint_id: str) -> None:
        manifest = self._load_manifest(checkpoint_id)
        # A denied/missing tool may never emit post/error. Its persisted
        # preimage remains useful for diagnosis, but without an expected
        # after-state it cannot participate in a safe CAS restore.
        pending_calls = dict(self._pending)
        for pending in self._persisted_pending(checkpoint_id):
            call_id = str(pending.get("tool_call_id") or "")
            if call_id:
                pending_calls.setdefault(call_id, pending)
        finalized_pending: list[tuple[str, Mapping[str, Any]]] = []
        for call_id, pending in tuple(pending_calls.items()):
            if pending.get("checkpoint_id") != checkpoint_id:
                continue
            for target in pending.get("targets", []):
                path = str(target.get("path") or target.get("raw_path") or "(unknown)")
                before = target.get("before")
                reason = str(target.get("reason") or "")
                if isinstance(before, Mapping) and before.get("kind") == "skipped":
                    reason = str(before.get("reason") or reason)
                suffix = f": {reason}" if reason else ""
                manifest["warnings"].append(
                    {
                        "path": path,
                        "reason": f"unfinished tool call {call_id}; not restorable{suffix}",
                    }
                )
            finalized_pending.append((call_id, pending))
        manifest["finished"] = True
        manifest["finished_ns"] = time.time_ns()
        # Finalized manifest reaches disk before its durable preimage records
        # are deleted. A failed write therefore remains diagnosable.
        self._write_manifest(manifest)
        for call_id, pending in finalized_pending:
            self._discard_pending(call_id, pending)
        self._garbage_collect_blobs()

    def register_hooks(self, hooks: Any, *, priority: int = 980) -> Callable[[], None]:
        """Register the root direct-edit observer; return one cleanup callback."""
        unregisters: list[Callable[..., object]] = []
        for event in ("tool:pre", "tool:post", "tool:error"):
            unregister = hooks.register(
                event,
                self.handle_event,
                priority=priority,
                name=f"tui-workspace-checkpoint-{event.replace(':', '-')}",
            )
            if callable(unregister):
                unregisters.append(unregister)

        def unregister_all() -> None:
            for unregister in reversed(unregisters):
                unregister()

        return unregister_all

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        """Capture root direct-edit pre/post states without affecting execution."""
        if str(data.get("session_id") or "") != self.root_session_id:
            return HookResult(action="continue")
        tool_name = str(data.get("tool_name") or data.get("name") or "")
        if tool_name not in _TRACKED_TOOLS:
            return HookResult(action="continue")
        call_id = str(data.get("tool_call_id") or data.get("tool_use_id") or data.get("id") or "")
        try:
            with self._lock:
                if event == "tool:pre":
                    self._handle_pre(
                        call_id, tool_name, _mapping(data.get("tool_input") or data.get("input"))
                    )
                elif event in ("tool:post", "tool:error"):
                    self._handle_after(call_id, event)
        except Exception as error:  # noqa: BLE001 — observer must never block the tool
            logger.warning("workspace checkpoint hook capture failed", exc_info=True)
            self._warn_active(f"{tool_name} checkpoint capture failed: {error}")
        return HookResult(action="continue")

    def _warn_active(self, reason: str) -> None:
        """Best-effort durable warning for a hook observer failure."""
        checkpoint_id = self._active_checkpoint
        if checkpoint_id is None:
            return
        try:
            manifest = self._load_manifest(checkpoint_id)
            manifest["warnings"].append({"path": "(unknown)", "reason": reason})
            self._write_manifest(manifest)
        except Exception:  # noqa: BLE001 — logging is the final fallback
            logger.debug("could not persist checkpoint warning", exc_info=True)

    def restore(
        self,
        checkpoint_id: str,
        *,
        include_target: bool = True,
        retain_target: bool = False,
    ) -> WorkspaceRestoreOutcome:
        """Undo direct edits from *checkpoint_id* onward using strict CAS.

        A checkpoint is cut before its prompt.  Consequently
        ``include_target=True`` (the default) undoes the selected prompt's
        edits and every later checkpoint; false undoes only later prompts.
        """
        checkpoint_id = _valid_id(checkpoint_id)
        with self._lock, self._exclusive_storage("restore checkpoint"):
            if self._active_checkpoint is not None:
                raise RuntimeError("cannot restore while a checkpoint is active")
            index = self._read_index()
            try:
                target_index = index["order"].index(checkpoint_id)
            except ValueError as error:
                raise KeyError(f"unknown checkpoint: {checkpoint_id}") from error
            start = target_index if include_target else target_index + 1
            selected = index["order"][start:]
            manifests = [self._load_manifest(item) for item in selected]
            for manifest in manifests:
                if not manifest.get("finished"):
                    self._finalize_incomplete_for_restore(manifest)

            warnings: list[str] = []
            skipped: list[str] = []
            chains: dict[str, list[dict[str, Any]]] = {}
            unknown_capture_warning = False
            for manifest in manifests:
                for warning in manifest.get("warnings", []):
                    path = str(warning.get("path") or "(unknown)")
                    reason = str(warning.get("reason") or "not tracked")
                    _append_unique(skipped, path)
                    warnings.append(f"{path}: {reason}")
                    if path == "(unknown)":
                        unknown_capture_warning = True
                for operation in manifest.get("operations", []):
                    for change in operation.get("changes", []):
                        path = str(change.get("path") or "")
                        if path:
                            chains.setdefault(path, []).append(change)

            # A known warning invalidates that path's chain; an unknown hook
            # capture failure invalidates the whole selected range. Restoring
            # an intermediate state is worse than leaving a file untouched.
            if unknown_capture_warning:
                for path in chains:
                    _append_unique(skipped, path)
                chains.clear()
                warnings.append(
                    "checkpoint capture failed for an unknown target; all tracked files left unchanged"
                )
            else:
                for path in tuple(chains):
                    if path in skipped:
                        chains.pop(path, None)

            plans: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
            already_restored: list[str] = []
            restored_states: dict[str, dict[str, Any]] = {}
            for relative, chain in chains.items():
                if not self._continuous(chain):
                    _append_unique(skipped, relative)
                    warnings.append(f"{relative}: tracked state chain diverged; left unchanged")
                    continue
                expected = chain[-1]["after"]
                desired = chain[0]["before"]
                current = self._capture_relative(relative, persist_blob=False)
                if current.get("kind") == "skipped":
                    _append_unique(skipped, relative)
                    warnings.append(
                        f"{relative}: {current.get('reason') or 'not safely snapshotable'}; "
                        "left unchanged"
                    )
                    continue
                if _states_equal(current, desired):
                    # A prior attempt may have changed the file and then
                    # failed while advancing history. Treat the desired bytes
                    # as an idempotent success only after retrying the parent
                    # directory durability barrier. The first attempt may have
                    # completed its replace/unlink but failed that exact fsync.
                    try:
                        confirmed = self._confirm_idempotent_restore(relative, current)
                    except (OSError, ValueError, RuntimeError) as error:
                        _append_unique(skipped, relative)
                        warnings.append(
                            f"{relative}: restored state is not durably confirmed: {error}"
                        )
                        continue
                    already_restored.append(relative)
                    restored_states[relative] = confirmed
                    continue
                if not _states_equal(current, expected, check_identity=True):
                    _append_unique(skipped, relative)
                    warnings.append(f"{relative}: changed since checkpoint; left unchanged")
                    continue
                plans.append((relative, expected, desired))

            restored: list[str] = list(already_restored)
            journal = {
                "schema": _SCHEMA_VERSION,
                "restore_id": f"restore-{time.time_ns()}",
                "checkpoint_id": checkpoint_id,
                "include_target": include_target,
                "retain_target": retain_target,
                "created_ns": time.time_ns(),
                "entries": [],
            }
            journal_path = self._restores / f"{journal['restore_id']}.json"
            self._write_json(journal_path, journal)
            journal_writable = True
            for relative, expected, desired in plans:
                if not journal_writable:
                    _append_unique(skipped, relative)
                    warnings.append(
                        f"{relative}: restore journal unavailable after prior failure; unchanged"
                    )
                    continue
                entry = {"path": relative, "status": "in_progress"}
                journal["entries"].append(entry)
                try:
                    # Crash evidence must precede the file mutation.
                    self._write_json(journal_path, journal)
                except OSError as error:
                    _append_unique(skipped, relative)
                    warnings.append(f"{relative}: restore journal unavailable: {error}; unchanged")
                    entry["status"] = "skipped"
                    journal_writable = False
                    continue
                try:
                    restored_state = self._apply_restore(relative, expected, desired)
                except (OSError, ValueError, RuntimeError) as error:
                    _append_unique(skipped, relative)
                    warnings.append(f"{relative}: {error}; left unchanged")
                    status = "skipped"
                else:
                    restored.append(relative)
                    restored_states[relative] = restored_state
                    status = "restored"
                entry["status"] = status
                if journal_writable:
                    try:
                        self._write_json(journal_path, journal)
                    except OSError as error:
                        journal_writable = False
                        warnings.append(
                            f"restore journal update failed: {error}; file results are in summary"
                        )
            # Do not bless an uncooperative write that raced after our
            # replace. The exact state returned by _apply_restore is the only
            # state eligible to become a trusted predecessor boundary.
            for relative in tuple(restored):
                written = restored_states.get(relative)
                current = self._capture_relative(relative, persist_blob=False)
                if written is None or not _states_equal(
                    current,
                    written,
                    check_identity=True,
                ):
                    restored.remove(relative)
                    restored_states.pop(relative, None)
                    _append_unique(skipped, relative)
                    warnings.append(
                        f"{relative}: changed immediately after restore; lineage not advanced"
                    )

            if skipped or warnings:
                # Preserve a retryable branch, but remove changes that already
                # restored successfully so a retry only attempts unresolved
                # paths instead of turning completed work into CAS conflicts.
                unresolved = set(skipped)
                for manifest in manifests:
                    for operation in manifest.get("operations", []):
                        operation["changes"] = [
                            change
                            for change in operation.get("changes", [])
                            if str(change.get("path") or "") in unresolved
                        ]
                predecessor_updates = self._rebased_predecessors(
                    index["order"][:start],
                    restored_states,
                )
                try:
                    self._commit_restore_state(index, [*predecessor_updates, *manifests])
                except OSError as error:
                    journal["recovery_required"] = True
                    self._recovery_required.add(checkpoint_id)
                    warnings.append(f"checkpoint retry state could not be saved: {error}")
            else:
                # A complete restore creates a new history branch. Retire the
                # selected pre-prompt checkpoint and descendants as bounded
                # tombstones: later UI checkpoints can report "already
                # restored" rather than treating a consumed id as corruption.
                branch = index["order"][start:]
                if retain_target:
                    # A code-only restore leaves the conversation timeline
                    # intact. Every still-visible descendant remains a valid
                    # baseline for later code-only or combined restore, so
                    # preserve the branch as zero-op anchors rather than
                    # falsely reporting those checkpoints as consumed.
                    retired: list[str] = []
                else:
                    retired = list(branch)
                    del index["order"][start:]
                predecessor_updates = self._rebased_predecessors(
                    index["order"][:start],
                    restored_states,
                )
                replacement_manifests = (
                    [self._anchor_manifest(anchor_id) for anchor_id in branch]
                    if retain_target
                    else [self._retired_manifest(retired_id) for retired_id in retired]
                )
                try:
                    self._commit_restore_state(
                        index,
                        [*predecessor_updates, *replacement_manifests],
                    )
                except OSError as error:
                    journal["recovery_required"] = True
                    self._recovery_required.add(checkpoint_id)
                    warnings.append(
                        f"checkpoint history could not be advanced: {error}; "
                        "recovery will retry before the next checkpoint operation"
                    )
                else:
                    try:
                        self._prune_retired_manifests()
                    except OSError as error:
                        warnings.append(f"checkpoint history cleanup deferred: {error}")
                    try:
                        self._garbage_collect_blobs()
                    except OSError as error:
                        warnings.append(f"checkpoint blob cleanup deferred: {error}")

            # The journal covers both file mutations and their lineage commit.
            # Mark it finished only after history is either advanced or a
            # retryable partial state is durable. A process exit in between
            # must leave an unfinished marker for constructor recovery.
            journal["finished_ns"] = time.time_ns()
            if journal_writable:
                try:
                    self._write_json(journal_path, journal)
                except OSError as error:
                    warnings.append(
                        f"restore journal finalization failed: {error}; file results are in summary"
                    )

            # Partial/conflicted restores also create journals. Bound them on
            # every attempt, not only after a fully successful branch cut.
            try:
                self._prune_restore_journals()
            except OSError as error:
                warnings.append(f"restore journal cleanup deferred: {error}")

            if (
                not skipped
                and not warnings
                and not self._recovering_journals
                and checkpoint_id in self._recovery_required
            ):
                try:
                    self._clear_persisted_recovery_requirement(checkpoint_id)
                except (OSError, ValueError) as error:
                    self._recovery_required.add(checkpoint_id)
                    warnings.append(
                        f"workspace restore recovery marker could not be cleared: {error}"
                    )
                else:
                    self._recovery_required.discard(checkpoint_id)
            return WorkspaceRestoreOutcome(
                checkpoint_id=checkpoint_id,
                restored_paths=tuple(restored),
                skipped_paths=tuple(skipped),
                warnings=tuple(warnings),
            )

    @property
    def recovery_required(self) -> tuple[str, ...]:
        return tuple(sorted(self._recovery_required))

    def _finalize_incomplete_for_restore(self, manifest: dict[str, Any]) -> None:
        """Make a crash-interrupted finish explicit before any file mutation."""
        checkpoint_id = str(manifest["checkpoint_id"])
        pending = self._persisted_pending(checkpoint_id)
        for record in pending:
            call_id = str(record.get("tool_call_id") or "(unknown)")
            for target in record.get("targets", []):
                path = str(target.get("path") or target.get("raw_path") or "(unknown)")
                before = target.get("before")
                reason = str(target.get("reason") or "")
                if isinstance(before, Mapping) and before.get("kind") == "skipped":
                    reason = str(before.get("reason") or reason)
                suffix = f": {reason}" if reason else ""
                manifest.setdefault("warnings", []).append(
                    {
                        "path": path,
                        "reason": f"unfinished tool call {call_id}; not restorable{suffix}",
                    }
                )
        manifest["finished"] = True
        manifest["finished_ns"] = time.time_ns()
        # Commit the explicit final state before removing durable pre records.
        self._write_manifest(manifest)
        for record in pending:
            call_id = str(record.get("tool_call_id") or "")
            if call_id:
                self._discard_pending(call_id, record)

    def checkpoint_status(self, checkpoint_id: str) -> str:
        """Return ``active``, ``retired``, or ``expired`` for one opaque id."""
        checkpoint_id = _valid_id(checkpoint_id)
        with self._lock, self._exclusive_storage("read checkpoint status"):
            if checkpoint_id in self._read_index()["order"]:
                return "active"
            path = self._manifest_path(checkpoint_id)
            if not path.exists():
                return "expired"
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return "expired"
            if (
                isinstance(raw, dict)
                and raw.get("checkpoint_id") == checkpoint_id
                and raw.get("workspace_root") == str(self.workspace_root)
                and raw.get("root_session_id") == self.root_session_id
            ):
                # A valid manifest outside the active index is consumed even
                # if the final tombstone rewrite was interrupted.
                return "retired"
            return "expired"

    def reconcile_visible(self, checkpoint_ids: Iterable[str]) -> None:
        """Compact code lineage abandoned only by a conversation restore.

        The durable ``used`` set remains append-only, while ``order`` follows
        the conversation branch still reachable from the UI. Descendant file
        transitions are folded into the last visible manifest because code
        was intentionally left at its newer state; discarding those changes
        would break the CAS chain for a later code restore.
        """
        visible = {_valid_id(item) for item in checkpoint_ids if item}
        with self._lock, self._exclusive_storage("reconcile checkpoint branch"):
            if self._active_checkpoint is not None:
                raise RuntimeError("cannot reconcile while a checkpoint is active")
            index = self._read_index()
            kept = [item for item in index["order"] if item in visible]
            if kept:
                last_kept = index["order"].index(kept[-1])
                if any(item not in visible for item in index["order"][: last_kept + 1]):
                    raise ValueError("visible checkpoint branch is not a contiguous prefix")
                removed = index["order"][last_kept + 1 :]
            else:
                removed = list(index["order"])
            if not removed:
                return
            replacements: list[dict[str, Any]] = []
            if kept:
                branch_base = self._load_manifest(kept[-1])
                if not branch_base.get("finished"):
                    self._finalize_incomplete_for_restore(branch_base)
                for hidden_id in removed:
                    hidden = self._load_manifest(hidden_id)
                    if not hidden.get("finished"):
                        self._finalize_incomplete_for_restore(hidden)
                    branch_base.setdefault("operations", []).extend(hidden.get("operations", []))
                    branch_base.setdefault("warnings", []).extend(hidden.get("warnings", []))
                    branch_base["captured_snapshots"] = int(
                        branch_base.get("captured_snapshots") or 0
                    ) + int(hidden.get("captured_snapshots") or 0)
                    branch_base["captured_bytes"] = int(
                        branch_base.get("captured_bytes") or 0
                    ) + int(hidden.get("captured_bytes") or 0)
                replacements.append(branch_base)
            replacements.extend(self._retired_manifest(item) for item in removed)
            index["order"] = kept
            self._commit_restore_state(
                index,
                replacements,
            )
            for checkpoint_id in removed:
                self._remove_pending_for_checkpoint(checkpoint_id)
            self._prune_retired_manifests()
            self._garbage_collect_blobs()

    def stage_visible_reconcile(
        self,
        checkpoint_ids: Iterable[str],
        marker_event_id: str,
    ) -> None:
        """Durably stage branch compaction before conversation mutation."""
        if not marker_event_id:
            raise ValueError("visible branch intent requires a rewind marker id")
        visible = [_valid_id(item) for item in checkpoint_ids if item]
        with self._lock, self._exclusive_storage("stage conversation branch"):
            self._write_json(
                self._visible_intent_path,
                {
                    "schema": _SCHEMA_VERSION,
                    "root_session_id": self.root_session_id,
                    "workspace_root": str(self.workspace_root),
                    "marker_event_id": marker_event_id,
                    "visible": visible,
                    "cancelled": False,
                },
            )

    @property
    def pending_visible_reconcile(self) -> bool:
        if not self._visible_intent_path.exists():
            return False
        try:
            raw = json.loads(self._visible_intent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        return not (isinstance(raw, dict) and raw.get("cancelled") is True)

    def cancel_visible_reconcile(self) -> None:
        """Cancel a staged branch compaction after conversation rollback."""
        with self._lock, self._exclusive_storage("cancel conversation branch"):
            if not self._visible_intent_path.exists():
                return
            try:
                raw = json.loads(self._visible_intent_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"visible branch intent is unreadable: {error}") from error
            if not isinstance(raw, dict):
                raise ValueError("visible branch intent is invalid")
            raw["cancelled"] = True
            self._write_json(self._visible_intent_path, raw)
            try:
                self._visible_intent_path.unlink()
            except FileNotFoundError:
                return
            except OSError:
                logger.warning("could not remove cancelled visible branch intent", exc_info=True)
                return
            _fsync_dir(self._root)

    def reconcile_staged_visible(self) -> bool:
        """Commit a staged branch only after its rewind marker is durable."""
        with self._lock, self._exclusive_storage("reconcile conversation branch"):
            return self._reconcile_staged_visible_locked()

    def _reconcile_staged_visible_locked(self) -> bool:
        if not self._visible_intent_path.exists():
            return False
        try:
            raw = json.loads(self._visible_intent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"visible branch intent is unreadable: {error}") from error
        if not isinstance(raw, dict):
            raise ValueError("visible branch intent is invalid")
        if raw.get("cancelled") is True:
            try:
                self._visible_intent_path.unlink()
            except OSError:
                logger.warning("could not remove cancelled visible branch intent", exc_info=True)
            return True
        if raw.get("root_session_id") != self.root_session_id:
            raise ValueError("visible branch intent belongs to another session")
        if raw.get("workspace_root") != str(self.workspace_root):
            raise ValueError("visible branch intent belongs to another workspace")
        marker_event_id = raw.get("marker_event_id")
        visible = raw.get("visible")
        if not isinstance(marker_event_id, str) or not isinstance(visible, list):
            raise ValueError("visible branch intent has invalid fields")
        if not self._rewind_marker_exists(marker_event_id):
            return False
        self.reconcile_visible(item for item in visible if isinstance(item, str))
        self._visible_intent_path.unlink()
        _fsync_dir(self._root)
        return True

    def _rewind_marker_exists(self, event_id: str) -> bool:
        path = self.session_dir / "ui-events.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(record, dict)
                and record.get("kind") == "rewind_marker"
                and record.get("event_id") == event_id
            ):
                return True
        return False

    # -- hook capture -------------------------------------------------

    def _handle_pre(self, call_id: str, tool_name: str, tool_input: Mapping[str, Any]) -> None:
        checkpoint_id = self._active_checkpoint
        if checkpoint_id is None:
            return
        manifest = self._load_manifest(checkpoint_id)
        if not call_id:
            manifest["warnings"].append(
                {"path": "(unknown)", "reason": f"{tool_name} call lacked tool_call_id"}
            )
            self._write_manifest(manifest)
            return
        if call_id in self._pending or self._pending_path(checkpoint_id, call_id).exists():
            manifest["warnings"].append(
                {"path": "(unknown)", "reason": f"duplicate tool_call_id {call_id}"}
            )
            self._write_manifest(manifest)
            return
        raw_paths = _tool_paths(tool_name, tool_input)
        if not raw_paths:
            manifest["warnings"].append(
                {"path": "(unknown)", "reason": f"could not resolve targets for {tool_name}"}
            )
            self._write_manifest(manifest)
            return
        targets: list[dict[str, Any]] = []
        captured_snapshots = int(manifest.get("captured_snapshots") or 0)
        captured_bytes = int(manifest.get("captured_bytes") or 0)
        for raw_path in raw_paths:
            relative, reason = self._safe_relative(raw_path)
            if relative is None:
                targets.append({"raw_path": raw_path, "reason": reason})
                continue
            if captured_snapshots >= self.max_checkpoint_snapshots:
                targets.append(
                    {
                        "path": relative,
                        "before": {
                            "kind": "skipped",
                            "reason": (
                                f"checkpoint exceeds {self.max_checkpoint_snapshots} snapshot limit"
                            ),
                        },
                    }
                )
                continue
            state = self._capture_relative(
                relative,
                persist_blob=True,
                max_persist_bytes=max(0, self.max_checkpoint_bytes - captured_bytes),
            )
            size = int(state.get("size") or 0) if state.get("kind") == "regular" else 0
            if state.get("kind") in {"regular", "absent"}:
                captured_snapshots += 1
                captured_bytes += size
            targets.append({"path": relative, "before": state})
        manifest["captured_snapshots"] = captured_snapshots
        manifest["captured_bytes"] = captured_bytes
        self._write_manifest(manifest)
        pending = {
            "schema": _SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id,
            "tool_call_id": call_id,
            "tool_name": tool_name,
            "created_ns": time.time_ns(),
            "targets": targets,
        }
        # The preimage reaches durable private storage before tool:pre returns.
        self._write_json(self._pending_path(checkpoint_id, call_id), pending)
        self._pending[call_id] = pending

    def _handle_after(self, call_id: str, event: str) -> None:
        if not call_id:
            return
        pending = self._pending.get(call_id) or self._load_pending(call_id)
        if pending is None:
            return
        checkpoint_id = str(pending["checkpoint_id"])
        manifest = self._load_manifest(checkpoint_id)
        changes: list[dict[str, Any]] = []
        for target in pending.get("targets", []):
            relative = str(target.get("path") or "")
            if not relative:
                raw_path = str(target.get("raw_path") or "(unknown)")
                manifest["warnings"].append(
                    {"path": raw_path, "reason": str(target.get("reason") or "unsafe path")}
                )
                continue
            before = target.get("before")
            if not isinstance(before, dict) or before.get("kind") == "skipped":
                manifest["warnings"].append(
                    {
                        "path": relative,
                        "reason": str((before or {}).get("reason") or "not safely snapshotable"),
                    }
                )
                continue
            # The after-state is needed only as a CAS digest/mode, never as
            # restore payload. Persisting its bytes would double checkpoint
            # storage outside the preimage byte budget.
            after = self._capture_relative(relative, persist_blob=False)
            if after.get("kind") == "skipped":
                manifest["warnings"].append(
                    {"path": relative, "reason": str(after.get("reason") or "unsafe after-state")}
                )
                continue
            if not _states_equal(before, after):
                changes.append({"path": relative, "before": before, "after": after})
        manifest["operations"].append(
            {
                "tool_call_id": call_id,
                "tool_name": pending["tool_name"],
                "event": event,
                "finished_ns": time.time_ns(),
                "changes": changes,
            }
        )
        self._write_manifest(manifest)
        self._discard_pending(call_id, pending)

    # -- state capture / restore -------------------------------------

    def _safe_relative(self, raw_path: str) -> tuple[str | None, str]:
        if not raw_path or raw_path.startswith("@"):
            return None, "mention or empty path is not safely trackable"
        raw = Path(raw_path).expanduser()
        candidate = raw if raw.is_absolute() else self.workspace_root / raw
        absolute = Path(os.path.abspath(candidate))
        try:
            relative = absolute.relative_to(self.workspace_root)
        except ValueError:
            return None, "outside workspace root"
        if any(part.casefold() == ".git" for part in relative.parts):
            return None, "git metadata is never checkpointed"
        current = self.workspace_root
        for part in relative.parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                continue
            except OSError as error:
                return None, f"cannot inspect path: {error}"
            if stat.S_ISLNK(info.st_mode):
                return None, "symlinked paths are not checkpointed"
        try:
            resolved = absolute.resolve(strict=False)
            resolved.relative_to(self.workspace_root)
        except (OSError, ValueError):
            return None, "path resolves outside workspace root"
        return relative.as_posix(), ""

    def _capture_relative(
        self,
        relative: str,
        *,
        persist_blob: bool,
        max_persist_bytes: int | None = None,
    ) -> dict[str, Any]:
        safe, reason = self._safe_relative(relative)
        if safe is None or safe != Path(relative).as_posix():
            return {"kind": "skipped", "reason": reason or "unsafe path"}
        if not _secure_dirfd_supported():
            return {
                "kind": "skipped",
                "reason": "secure descriptor-relative traversal is unavailable",
            }
        try:
            with self._bound_parent(safe) as (parent_fd, leaf):
                return self._capture_bound(
                    parent_fd,
                    leaf,
                    persist_blob=persist_blob,
                    max_persist_bytes=max_persist_bytes,
                )
        except (OSError, RuntimeError, ValueError) as error:
            return {
                "kind": "skipped",
                "reason": f"cannot bind workspace path safely: {error}",
            }

    @contextmanager
    def _bound_parent(self, relative: str) -> Iterator[tuple[int, str]]:
        """Bind every existing parent component without following symlinks."""
        if not _secure_dirfd_supported():
            raise RuntimeError("secure descriptor-relative traversal is unavailable")
        parts = Path(relative).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("path has no safe leaf component")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        current_fd = os.open(self.workspace_root, flags)
        try:
            root_identity = _directory_identity_fd(current_fd)
            if self._workspace_identity is None or root_identity != self._workspace_identity:
                raise RuntimeError("workspace root changed")
            for part in parts[:-1]:
                next_fd = os.open(part, flags, dir_fd=current_fd)
                try:
                    info = os.fstat(next_fd)
                    if not stat.S_ISDIR(info.st_mode):
                        raise RuntimeError(f"parent component is not a directory: {part}")
                except Exception:
                    os.close(next_fd)
                    raise
                os.close(current_fd)
                current_fd = next_fd
            yield current_fd, parts[-1]
        finally:
            os.close(current_fd)

    def _capture_bound(
        self,
        parent_fd: int,
        leaf: str,
        *,
        persist_blob: bool,
        max_persist_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Capture one leaf while its verified parent descriptor stays bound."""
        parent_identity = _directory_identity_fd(parent_fd)
        try:
            before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"kind": "absent", "parent_identity": parent_identity}
        except OSError as error:
            return {"kind": "skipped", "reason": f"cannot stat: {error}"}
        if stat.S_ISLNK(before.st_mode):
            return {"kind": "skipped", "reason": "symlinks are not checkpointed"}
        if not stat.S_ISREG(before.st_mode):
            return {"kind": "skipped", "reason": "non-regular files are not checkpointed"}
        if before.st_nlink != 1:
            return {"kind": "skipped", "reason": "hard-linked files are not checkpointed"}
        if before.st_size > self.max_file_bytes:
            return {
                "kind": "skipped",
                "reason": f"file exceeds {self.max_file_bytes} byte checkpoint limit",
            }
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as error:
            return {"kind": "skipped", "reason": f"cannot open safely: {error}"}
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, self.max_file_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > self.max_file_bytes:
                    return {
                        "kind": "skipped",
                        "reason": f"file exceeds {self.max_file_bytes} byte checkpoint limit",
                    }
            after = os.fstat(descriptor)
            try:
                extended_attributes = _extended_attributes_fd(descriptor)
                extended_acl = _has_extended_acl_fd(descriptor)
                final = os.fstat(descriptor)
                named_final = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as error:
                return {"kind": "skipped", "reason": f"cannot inspect file metadata: {error}"}
        finally:
            os.close(descriptor)
        identity_before = _snapshot_identity(before)
        identity_open = _snapshot_identity(opened)
        identity_after = _snapshot_identity(after)
        if identity_before != identity_open or identity_open != identity_after:
            return {"kind": "skipped", "reason": "file changed while being snapshotted"}
        identity_final = _snapshot_identity(final)
        if identity_after != identity_final or identity_final != _snapshot_identity(named_final):
            return {"kind": "skipped", "reason": "file metadata changed while being snapshotted"}
        if extended_attributes or extended_acl:
            return {
                "kind": "skipped",
                "reason": "extended attributes or ACLs are not safely restorable",
            }
        geteuid = getattr(os, "geteuid", None)
        getegid = getattr(os, "getegid", None)
        effective_uid = geteuid() if callable(geteuid) else None
        effective_gid = getegid() if callable(getegid) else None
        if (
            effective_uid is not None
            and effective_gid is not None
            and (final.st_uid != effective_uid or final.st_gid != effective_gid)
        ):
            return {
                "kind": "skipped",
                "reason": "file ownership is not safely restorable",
            }
        file_flags = int(getattr(final, "st_flags", 0))
        if file_flags:
            return {
                "kind": "skipped",
                "reason": "file flags are not safely restorable",
            }
        data = b"".join(chunks)
        if persist_blob and max_persist_bytes is not None and len(data) > max_persist_bytes:
            return {
                "kind": "skipped",
                "reason": (f"checkpoint exceeds {self.max_checkpoint_bytes} total byte limit"),
            }
        digest = hashlib.sha256(data).hexdigest()
        if persist_blob:
            self._write_blob(digest, data)
        return {
            "kind": "regular",
            "digest": digest,
            "size": len(data),
            "mode": stat.S_IMODE(final.st_mode),
            "flags": file_flags,
            "identity": [final.st_dev, final.st_ino],
            "parent_identity": parent_identity,
            **(
                {"uid": final.st_uid, "gid": final.st_gid}
                if effective_uid is not None and effective_gid is not None
                else {}
            ),
        }

    def _continuous(self, chain: list[dict[str, Any]]) -> bool:
        return all(
            _states_equal(previous["after"], current["before"], check_identity=True)
            for previous, current in zip(chain, chain[1:], strict=False)
        )

    def _rebased_predecessors(
        self,
        checkpoint_ids: Iterable[str],
        restored_states: Mapping[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Refresh terminal CAS identities after an atomic restore replace."""
        manifests = [self._load_manifest(item) for item in checkpoint_ids]
        changed: set[str] = set()
        for relative, current in restored_states.items():
            found = False
            for manifest in reversed(manifests):
                for operation in reversed(manifest.get("operations", [])):
                    for change in reversed(operation.get("changes", [])):
                        if str(change.get("path") or "") == relative:
                            change["after"] = current
                            changed.add(str(manifest["checkpoint_id"]))
                            found = True
                            break
                    if found:
                        break
                if found:
                    break
        return [
            manifest
            for manifest in manifests
            if str(manifest.get("checkpoint_id") or "") in changed
        ]

    def _apply_restore(
        self, relative: str, expected: dict[str, Any], desired: dict[str, Any]
    ) -> dict[str, Any]:
        safe, reason = self._safe_relative(relative)
        if safe is None or safe != relative:
            raise ValueError(reason or "unsafe path")
        if not _secure_dirfd_supported():
            raise RuntimeError("secure descriptor-relative restore is unavailable")
        with self._bound_parent(relative) as (parent_fd, leaf):
            current = self._capture_bound(parent_fd, leaf, persist_blob=False)
            if not _states_equal(current, expected, check_identity=True):
                raise RuntimeError("changed during restore")
            if desired.get("kind") == "absent":
                if expected.get("kind") == "absent":
                    return current
                if not _states_equal(
                    self._capture_bound(parent_fd, leaf, persist_blob=False),
                    expected,
                    check_identity=True,
                ):
                    raise RuntimeError("changed during restore")
                os.unlink(leaf, dir_fd=parent_fd)
                _fsync_fd_strict(parent_fd)
                return self._capture_bound(parent_fd, leaf, persist_blob=False)
            if desired.get("kind") != "regular":
                raise ValueError("desired state is not restorable")
            digest = str(desired.get("digest") or "")
            blob = self._blob_path(digest)
            try:
                data = blob.read_bytes()
            except OSError as error:
                raise RuntimeError(f"checkpoint blob unavailable: {error}") from error
            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError("checkpoint blob failed integrity check")
            descriptor, temp_leaf = _create_bound_temp(parent_fd)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(data)
                    handle.flush()
                    fchown = getattr(os, "fchown", None)
                    if callable(fchown) and "uid" in desired and "gid" in desired:
                        fchown(handle.fileno(), int(desired["uid"]), int(desired["gid"]))
                    # Ownership changes and writes may clear setuid/setgid, so
                    # the checkpoint mode is the final metadata operation.
                    os.fchmod(handle.fileno(), int(desired.get("mode", 0o600)))
                    os.fsync(handle.fileno())
                # Stage the payload first, then perform the final CAS as close
                # as possible to the atomic rename. The parent descriptor
                # remains bound throughout, so directory renames and symlink
                # swaps cannot redirect either side of the mutation.
                if not _states_equal(
                    self._capture_bound(parent_fd, leaf, persist_blob=False),
                    expected,
                    check_identity=True,
                ):
                    raise RuntimeError("changed during restore")
                os.rename(
                    temp_leaf,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                _fsync_fd_strict(parent_fd)
            finally:
                try:
                    os.unlink(temp_leaf, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            restored = self._capture_bound(parent_fd, leaf, persist_blob=False)
            if not _states_equal(restored, desired):
                raise RuntimeError("restored file did not match its checkpoint preimage")
            return restored

    def _confirm_idempotent_restore(
        self,
        relative: str,
        current: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry the durability barrier before advancing an idempotent restore."""
        safe, reason = self._safe_relative(relative)
        if safe is None or safe != relative:
            raise ValueError(reason or "unsafe path")
        if not _secure_dirfd_supported():
            raise RuntimeError("secure descriptor-relative restore is unavailable")
        with self._bound_parent(relative) as (parent_fd, leaf):
            if current.get("parent_identity") != _directory_identity_fd(parent_fd):
                raise RuntimeError("parent directory changed")
            _fsync_fd_strict(parent_fd)
            confirmed = self._capture_bound(parent_fd, leaf, persist_blob=False)
            if not _states_equal(confirmed, current, check_identity=True):
                raise RuntimeError("changed while confirming restore durability")
            return confirmed

    # -- persistence --------------------------------------------------

    def _acquire_storage(self, purpose: str) -> None:
        """Take the session-wide process lock, failing closed on contention."""
        if self._ownership_proxy is not None:
            raise RuntimeError("workspace checkpoint storage is already owned")
        try:
            self._ownership_proxy = self._ownership_lock.acquire(timeout=0)
            try:
                os.chmod(self._ownership_path, 0o600, follow_symlinks=False)
            except OSError:
                pass
        except Timeout as error:
            raise WorkspaceCheckpointUnavailableError(
                f"workspace checkpoint storage is in use by another TUI; cannot {purpose}"
            ) from error
        try:
            self._recover_restore_transaction()
            self._recover_incomplete_restore_journals()
        except WorkspaceCheckpointUnavailableError:
            self._release_storage()
            raise
        except Exception as error:
            self._release_storage()
            raise WorkspaceCheckpointUnavailableError(
                f"workspace checkpoint recovery failed: {error}"
            ) from error

    def _release_storage(self) -> None:
        if self._ownership_proxy is None:
            return
        self._ownership_proxy = None
        self._ownership_lock.release()

    def close(self) -> None:
        """Release any operation lease left during runtime cleanup."""
        with self._lock:
            if self._active_checkpoint is not None:
                raise RuntimeError("cannot close while a checkpoint is active")
            self._release_storage()

    @contextmanager
    def _exclusive_storage(self, purpose: str) -> Iterator[None]:
        """Own storage for one operation, preserving an active turn lease."""
        already_owned = self._ownership_proxy is not None
        if not already_owned:
            self._acquire_storage(purpose)
        try:
            yield
        finally:
            if not already_owned:
                self._release_storage()

    def _manifest_path(self, checkpoint_id: str) -> Path:
        return self._manifests / f"{_key(checkpoint_id)}.json"

    def _pending_path(self, checkpoint_id: str, call_id: str) -> Path:
        return self._pending_dir / f"{_key(checkpoint_id + chr(0) + call_id)}.json"

    def _blob_path(self, digest: str) -> Path:
        return self._blobs / digest

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self._write_json(self._manifest_path(str(manifest["checkpoint_id"])), manifest)

    def _load_manifest(self, checkpoint_id: str) -> dict[str, Any]:
        try:
            raw = json.loads(self._manifest_path(checkpoint_id).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise KeyError(f"unknown checkpoint: {checkpoint_id}") from error
        if not isinstance(raw, dict) or raw.get("checkpoint_id") != checkpoint_id:
            raise ValueError(f"invalid checkpoint manifest: {checkpoint_id}")
        if raw.get("workspace_root") != str(self.workspace_root):
            raise ValueError(f"checkpoint belongs to another workspace: {checkpoint_id}")
        if raw.get("root_session_id") != self.root_session_id:
            raise ValueError(f"checkpoint belongs to another root session: {checkpoint_id}")
        return raw

    def _write_retired_manifest(self, checkpoint_id: str) -> None:
        """Replace a consumed manifest with a bounded, blob-free tombstone."""
        self._write_manifest(self._retired_manifest(checkpoint_id))

    def _retired_manifest(self, checkpoint_id: str) -> dict[str, Any]:
        return {
            "schema": _SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id,
            "root_session_id": self.root_session_id,
            "workspace_root": str(self.workspace_root),
            "retired": True,
            "retired_ns": time.time_ns(),
            "operations": [],
            "warnings": [],
        }

    def _write_anchor_manifest(self, checkpoint_id: str) -> None:
        """Keep a zero-op baseline so future code-only branches can unwind."""
        self._write_manifest(self._anchor_manifest(checkpoint_id))

    def _anchor_manifest(self, checkpoint_id: str) -> dict[str, Any]:
        return {
            "schema": _SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id,
            "root_session_id": self.root_session_id,
            "workspace_root": str(self.workspace_root),
            "created_ns": time.time_ns(),
            "finished": True,
            "anchor": True,
            "captured_snapshots": 0,
            "captured_bytes": 0,
            "operations": [],
            "warnings": [],
        }

    def _prune_retired_manifests(self) -> None:
        retired: list[tuple[int, Path]] = []
        for path in self._manifests.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and raw.get("retired") is True:
                retired.append((int(raw.get("retired_ns") or 0), path))
        retired.sort(key=lambda item: item[0])
        for _retired_ns, path in retired[: -self.max_checkpoints]:
            path.unlink(missing_ok=True)

    def _prune_restore_journals(self) -> None:
        journals = sorted(
            self._restores.glob("restore-*.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        removable: list[Path] = []
        for path in journals:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # An unreadable journal may be the only durable crash marker.
                # Preserve it rather than pruning away evidence needed for a
                # fail-closed recovery decision.
                continue
            if isinstance(raw, dict) and raw.get("recovery_required") is True:
                continue
            removable.append(path)
        excess = max(0, len(journals) - self.max_checkpoints)
        for path in removable[:excess]:
            path.unlink(missing_ok=True)

    def _read_index(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"workspace checkpoint index is unreadable: {error}") from error
        order = raw.get("order") if isinstance(raw, dict) else None
        if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
            raise ValueError("workspace checkpoint index has invalid order")
        used = raw.get("used", order) if isinstance(raw, dict) else order
        if not isinstance(used, list) or not all(isinstance(item, str) for item in used):
            raise ValueError("workspace checkpoint index has invalid used ids")
        return {"schema": _SCHEMA_VERSION, "order": list(order), "used": list(used)}

    def _write_index(self, index: dict[str, Any]) -> None:
        self._write_json(self._index_path, index)

    def _commit_restore_state(
        self,
        index: Mapping[str, Any],
        manifests: Iterable[Mapping[str, Any]],
    ) -> None:
        """Atomically-intended branch update recoverable across process exit."""
        transaction = {
            "schema": _SCHEMA_VERSION,
            "root_session_id": self.root_session_id,
            "workspace_root": str(self.workspace_root),
            "index": dict(index),
            "manifests": [dict(manifest) for manifest in manifests],
        }
        # The transaction is durable before any manifest/index replacement.
        # A later owner completes these idempotent writes before doing work.
        self._write_json(self._transaction_path, transaction)
        self._apply_restore_transaction(transaction)

    def _recover_restore_transaction(self) -> None:
        if not self._transaction_path.exists():
            return
        try:
            raw = json.loads(self._transaction_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"workspace restore transaction is unreadable: {error}") from error
        if not isinstance(raw, dict):
            raise ValueError("workspace restore transaction is invalid")
        self._apply_restore_transaction(raw)

    def _recover_incomplete_restore_journals(self) -> None:
        """CAS-safely resume a process-killed file restore before new work."""
        if self._recovering_journals or not self._index_path.exists():
            return
        self._recovering_journals = True
        try:
            for path in sorted(self._restores.glob("restore-*.json")):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise WorkspaceCheckpointUnavailableError(
                        f"workspace restore journal is unreadable ({path.name}): {error}"
                    ) from error
                if not isinstance(raw, dict):
                    raise WorkspaceCheckpointUnavailableError(
                        f"workspace restore journal is invalid ({path.name})"
                    )
                checkpoint_id = raw.get("checkpoint_id")
                if not isinstance(checkpoint_id, str) or not checkpoint_id:
                    raise WorkspaceCheckpointUnavailableError(
                        f"workspace restore journal lacks a checkpoint id ({path.name})"
                    )
                index = self._read_index()
                if raw.get("finished_ns"):
                    if raw.get("recovery_required") is True:
                        if checkpoint_id in index["order"]:
                            self._recovery_required.add(checkpoint_id)
                        else:
                            # A committed branch cut is durable proof that the
                            # retry completed even if clearing this older
                            # marker was interrupted.
                            raw["recovery_required"] = False
                            raw["recovery_cleared_ns"] = time.time_ns()
                            raw["recovery_clear_reason"] = "branch state was committed"
                            self._write_json(path, raw)
                            self._recovery_required.discard(checkpoint_id)
                    continue
                if checkpoint_id not in index["order"]:
                    raw["finished_ns"] = time.time_ns()
                    raw["recovery"] = "branch state was already committed"
                    raw["recovery_required"] = False
                    self._write_json(path, raw)
                    continue
                try:
                    outcome = self.restore(
                        checkpoint_id,
                        include_target=bool(raw.get("include_target", True)),
                        retain_target=bool(raw.get("retain_target", False)),
                    )
                except (KeyError, OSError, RuntimeError, ValueError) as error:
                    self._recovery_required.add(checkpoint_id)
                    raw["recovery_error"] = str(error)
                    raw["recovery_required"] = True
                else:
                    raw["recovery"] = outcome.summary
                    raw["recovery_warnings"] = list(outcome.warnings)
                    if outcome.skipped_paths or outcome.warnings:
                        self._recovery_required.add(checkpoint_id)
                        raw["recovery_required"] = True
                    else:
                        raw["recovery_required"] = False
                raw["finished_ns"] = time.time_ns()
                raw["recovered_ns"] = time.time_ns()
                self._write_json(path, raw)
        finally:
            self._recovering_journals = False

    def _clear_persisted_recovery_requirement(self, checkpoint_id: str) -> None:
        """Durably release a recovered-checkpoint send gate after retry."""
        for path in sorted(self._restores.glob("restore-*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ValueError(f"restore journal is unreadable: {path.name}") from error
            if not isinstance(raw, dict):
                raise ValueError(f"restore journal is invalid: {path.name}")
            if (
                raw.get("checkpoint_id") != checkpoint_id
                or raw.get("recovery_required") is not True
            ):
                continue
            raw["recovery_required"] = False
            raw["recovery_cleared_ns"] = time.time_ns()
            raw["recovery_clear_reason"] = "explicit restore completed"
            self._write_json(path, raw)

    def _apply_restore_transaction(self, transaction: Mapping[str, Any]) -> None:
        if transaction.get("root_session_id") != self.root_session_id:
            raise ValueError("workspace restore transaction belongs to another session")
        if transaction.get("workspace_root") != str(self.workspace_root):
            raise ValueError("workspace restore transaction belongs to another workspace")
        index = transaction.get("index")
        manifests = transaction.get("manifests")
        if not isinstance(index, Mapping) or not isinstance(manifests, list):
            raise ValueError("workspace restore transaction has invalid state")
        for manifest in manifests:
            if not isinstance(manifest, dict):
                raise ValueError("workspace restore transaction has invalid manifest")
            self._write_manifest(manifest)
        self._write_index(dict(index))
        self._transaction_path.unlink()
        _fsync_dir(self._root)

    def _write_json(self, path: Path, value: Mapping[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        _atomic_private_write(path, data)

    def _write_blob(self, digest: str, data: bytes) -> None:
        path = self._blob_path(digest)
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError:
                existing = b""
            if hashlib.sha256(existing).hexdigest() == digest:
                return
        _atomic_private_write(path, data)

    def _load_pending(self, call_id: str) -> dict[str, Any] | None:
        for path in self._pending_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and raw.get("tool_call_id") == call_id:
                self._pending[call_id] = raw
                return raw
        return None

    def _persisted_pending(self, checkpoint_id: str) -> tuple[dict[str, Any], ...]:
        pending: list[dict[str, Any]] = []
        for path in self._pending_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and raw.get("checkpoint_id") == checkpoint_id:
                pending.append(raw)
        return tuple(pending)

    def _discard_pending(self, call_id: str, pending: Mapping[str, Any]) -> None:
        self._pending.pop(call_id, None)
        path = self._pending_path(str(pending["checkpoint_id"]), call_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _prune(self) -> None:
        index = self._read_index()
        excess = max(0, len(index["order"]) - self.max_checkpoints)
        if not excess:
            return
        removed = index["order"][:excess]
        index["order"] = index["order"][excess:]
        # Commit the new active index first. If this write fails, every old
        # manifest still exists and the next begin can retry safely.
        self._write_index(index)
        for old in removed:
            try:
                self._manifest_path(old).unlink()
            except FileNotFoundError:
                pass
            self._remove_pending_for_checkpoint(old)
        self._garbage_collect_blobs()

    def _remove_pending_for_checkpoint(self, checkpoint_id: str) -> None:
        for path in self._pending_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict) and raw.get("checkpoint_id") == checkpoint_id:
                path.unlink(missing_ok=True)

    def _garbage_collect_blobs(self) -> None:
        referenced: set[str] = set()
        for path in (*self._manifests.glob("*.json"), *self._pending_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            _collect_digests(raw, referenced)
        for blob in self._blobs.iterdir():
            if blob.is_file() and blob.name not in referenced:
                try:
                    blob.unlink()
                except OSError:
                    pass


def _tool_paths(tool_name: str, tool_input: Mapping[str, Any]) -> tuple[str, ...]:
    if tool_name != "apply_patch":
        value = tool_input.get("file_path") or tool_input.get("path")
        return (str(value),) if isinstance(value, str) and value.strip() else ()
    direct = tool_input.get("path") or tool_input.get("file_path")
    paths: list[str] = []
    if isinstance(direct, str) and direct.strip():
        paths.append(direct.strip())
    patch = tool_input.get("patch")
    if not isinstance(patch, str):
        candidate = tool_input.get("diff")
        patch = candidate if isinstance(candidate, str) and "*** " in candidate else ""
    for line in patch.splitlines():
        marker = next(
            (item for item in _PATCH_PATH_MARKERS if line.rstrip().startswith(item)), None
        )
        if marker is not None:
            value = line.rstrip()[len(marker) :].strip()
            if value:
                paths.append(value)
            continue
        if line.rstrip().startswith("*** Move to: "):
            value = line.rstrip()[len("*** Move to: ") :].strip()
            if value:
                paths.append(value)
    return tuple(dict.fromkeys(paths))


def _states_equal(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    check_identity: bool = False,
) -> bool:
    if left.get("kind") != right.get("kind"):
        return False
    if left.get("kind") == "absent":
        return "parent_identity" not in right or left.get("parent_identity") == right.get(
            "parent_identity"
        )
    if left.get("kind") != "regular":
        return False
    metadata_matches = (
        left.get("digest") == right.get("digest")
        and left.get("size") == right.get("size")
        and left.get("mode") == right.get("mode")
    )
    for field in ("uid", "gid", "flags", "parent_identity"):
        if field in right and left.get(field) != right.get(field):
            metadata_matches = False
    if check_identity and "identity" in right and left.get("identity") != right.get("identity"):
        metadata_matches = False
    return metadata_matches


def _directory_identity(path: Path) -> list[int] | None:
    """Stable direct-parent identity used to reject replaced directory trees."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    return [info.st_dev, info.st_ino]


def _directory_identity_fd(descriptor: int) -> list[int] | None:
    """Identity for one already-bound directory descriptor."""
    try:
        info = os.fstat(descriptor)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return [info.st_dev, info.st_ino]


def _snapshot_identity(info: os.stat_result) -> tuple[int, ...]:
    """Fields that must remain stable across one descriptor-bound read."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _secure_dirfd_supported() -> bool:
    """Whether this host can bind traversal and mutation to directory fds."""
    required = (os.open, os.stat, os.unlink, os.rename)
    return bool(
        os.name == "posix"
        and getattr(os, "O_DIRECTORY", 0)
        and getattr(os, "O_NOFOLLOW", 0)
        and all(function in os.supports_dir_fd for function in required)
        and os.stat in os.supports_follow_symlinks
    )


def _extended_attributes_fd(descriptor: int) -> tuple[str, ...]:
    """List xattrs through the same file descriptor used for capture."""
    listxattr = getattr(os, "listxattr", None)
    if callable(listxattr):
        try:
            attributes: Any = listxattr(descriptor)
        except (TypeError, ValueError, NotImplementedError) as error:
            raise OSError("descriptor xattr probe is unavailable") from error
        return tuple(str(item) for item in attributes)
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        list_xattr = libc.flistxattr
        list_xattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        list_xattr.restype = ctypes.c_ssize_t
        size = list_xattr(descriptor, None, 0, 0)
        if size < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, "xattr probe could not inspect the file")
        if size == 0:
            return ()
        buffer = ctypes.create_string_buffer(size)
        received = list_xattr(descriptor, buffer, size, 0)
        if received < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, "xattr probe could not inspect the file")
        return tuple(
            item.decode("utf-8", errors="replace")
            for item in buffer.raw[:received].split(b"\x00")
            if item
        )
    # Proceeding without the metadata probe could silently strip attributes
    # on restore, so unknown POSIX surfaces fail closed.
    raise OSError("descriptor xattr probe is unavailable")


def _has_extended_acl_fd(descriptor: int) -> bool:
    """Detect macOS extended ACLs through an already-bound file descriptor."""
    if sys.platform != "darwin":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    acl_get_fd = libc.acl_get_fd_np
    acl_get_fd.argtypes = [ctypes.c_int, ctypes.c_int]
    acl_get_fd.restype = ctypes.c_void_p
    acl_free = libc.acl_free
    acl_free.argtypes = [ctypes.c_void_p]
    acl_free.restype = ctypes.c_int
    acl = acl_get_fd(descriptor, 0x00000100)  # ACL_TYPE_EXTENDED
    if not acl:
        error_number = ctypes.get_errno()
        if error_number in {0, 2}:  # no extended ACL
            return False
        raise OSError(error_number, "ACL probe could not inspect the file")
    acl_free(acl)
    return True


def _create_bound_temp(parent_fd: int) -> tuple[int, str]:
    """Create one private staging file inside an already-bound directory."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(32):
        leaf = f".amplifier-checkpoint-{secrets.token_hex(12)}"
        try:
            descriptor = os.open(leaf, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        try:
            os.fchmod(descriptor, 0o600)
        except Exception:
            os.close(descriptor)
            try:
                os.unlink(leaf, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        return descriptor, leaf
    raise FileExistsError("could not allocate a checkpoint staging file")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("checkpoint_id cannot be empty")
    if len(value) > 512:
        raise ValueError("checkpoint_id is too long")
    return value


def _ensure_private_dir(path: Path) -> None:
    """Create/verify a checkpoint directory without following a leaf symlink."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=False)
        info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"checkpoint storage is not a real directory: {path}")
    path.chmod(0o700)
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    if mode & 0o077:
        raise PermissionError(f"checkpoint storage is not private: {path}")


def _atomic_private_write(path: Path, data: bytes) -> None:
    _ensure_private_dir(path.parent)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_dir_strict(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fsync_dir_strict(path: Path) -> None:
    """Durably commit a transaction-critical rename on supported POSIX hosts."""
    if os.name == "nt":
        # Windows has no portable directory-fsync primitive; atomic replace
        # remains the strongest available contract there.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_fd_strict(descriptor: int) -> None:
    """Durably commit changes relative to one bound POSIX directory."""
    os.fsync(descriptor)


def _collect_digests(value: Any, output: set[str]) -> None:
    if isinstance(value, dict):
        digest = value.get("digest")
        if value.get("kind") == "regular" and isinstance(digest, str):
            output.add(digest)
        for nested in value.values():
            _collect_digests(nested, output)
    elif isinstance(value, list):
        for nested in value:
            _collect_digests(nested, output)


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


__all__ = ["WorkspaceCheckpointStore", "WorkspaceRestoreOutcome"]
