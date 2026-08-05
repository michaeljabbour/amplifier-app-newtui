"""Session persistence: transcript.jsonl / metadata.json / ui-events.jsonl.

Layout (foundation-compatible, shared with amplifier-app-cli):

    ~/.amplifier/projects/<project-slug>/sessions/<session-id>/
        transcript.jsonl   # user/assistant messages (system/developer skipped)
        metadata.json      # session metadata (secrets redacted)
        ui-events.jsonl    # append-only normalized UIEvent log (ADR-0007 §9)

Guarantees:

- **Atomic write + backup** for transcript/metadata (a reader always
  sees old or new content, never a partial write; ``.backup`` recovery
  on corruption).
- **ui-events.jsonl is append-only** — one JSON object per line, each a
  normalized :class:`~amplifier_app_tui.kernel.events.UIEvent` dump
  plus its ``kind``. Powers cost re-seed on resume (kernel/cost.py),
  evidence links, lane replay and contract tests. The name deliberately
  differs from ``events.jsonl``: foundation's ``hooks-logging`` owns that
  filename for canonical ISO-timestamped hook records
  (``session_log_template``), and the app's float-``ts`` UIEvent schema
  must never mix into it. Sessions written before the rename logged
  UIEvents to ``events.jsonl``; readers fall back to it
  (:meth:`SessionStore.events_path` / :meth:`SessionStore.events_read_paths`)
  and skip foreign/unparseable lines.
- **Debounced incremental save** on ``tool:post`` via
  :class:`IncrementalSaver` (crash recovery between tool calls, not
  just between turns).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..model.redaction import scrub_value
from .config import get_project_slug
from .events import UIEvent

logger = logging.getLogger(__name__)

TRANSCRIPT_FILENAME = "transcript.jsonl"
METADATA_FILENAME = "metadata.json"
EVENTS_FILENAME = "ui-events.jsonl"
LEGACY_EVENTS_FILENAME = "events.jsonl"
REWIND_INTENT_FILENAME = "rewind-intent.json"
"""Pre-rename UIEvent log name — now owned by foundation's hooks-logging
for canonical hook records; read-only fallback, never written."""


def _json_default(value: object) -> str:
    """Last-resort JSON encoder for provider metadata values."""
    return str(value)


def is_top_level_session(session_id: str) -> bool:
    """Spawned sub-sessions carry ``_`` (``{parent}-{hex}_{agent}``)."""
    return "_" not in session_id


class AmbiguousSessionError(ValueError):
    """Raised by :meth:`SessionStore.find_session` when a prefix matches >1 session.

    Subclasses ``ValueError`` so every EXISTING ``except ValueError`` call site
    (rename/delete/tag/fork/export -- see ``session_manager.py``) keeps working
    unchanged: ``str(error)`` renders the identical truncated-preview message
    they already echo. The ADDITION is :attr:`matches`, the full unfiltered
    candidate list, which resume-path callers (S3) use to render an actionable
    table instead of a 3-item text preview.
    """

    def __init__(self, partial_id: str, matches: list[str]) -> None:
        self.partial_id = partial_id
        self.matches = matches
        preview = ", ".join(m[:12] + "…" for m in matches[:3])
        extra = f" and {len(matches) - 3} more" if len(matches) > 3 else ""
        super().__init__(
            f"Ambiguous session ID '{partial_id}' matches {len(matches)} sessions: {preview}{extra}"
        )


def _validate_session_id(session_id: str) -> str:
    if not session_id or not session_id.strip():
        raise ValueError("session_id cannot be empty")
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(f"Invalid session_id: {session_id}")
    return session_id


def _write_private_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace one private file and fsync its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _write_with_backup(path: Path, content: str) -> None:
    """Durable atomic write with a durable copy of the prior value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".backup")
        try:
            _write_private_bytes(backup, path.read_bytes())
        except OSError:
            logger.warning("Could not write backup for %s", path, exc_info=True)
    _write_private_bytes(path, content.encode("utf-8"))


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically fsync one private transaction record."""
    payload = json.dumps(value, ensure_ascii=False, default=_json_default).encode("utf-8")
    _write_private_bytes(path, payload)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write every byte, including when the OS reports a short append."""
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("event log append made no progress")
        remaining = remaining[written:]


def _sanitize_message(message: Any) -> Any:
    """Ensure a transcript message is JSON-serializable.

    Prefers foundation's ``sanitize_message`` (handles provider model
    objects); degrades to a JSON round-trip with ``str`` fallback.
    """
    try:
        from amplifier_foundation import sanitize_message

        return sanitize_message(message)
    except ImportError:  # pragma: no cover — foundation is a hard dependency
        raw = message if isinstance(message, dict) else getattr(message, "model_dump", dict)()
        return json.loads(json.dumps(raw, ensure_ascii=False, default=_json_default))


def _redact_secrets(metadata: dict[str, Any]) -> dict[str, Any]:
    """Redact secret-looking values before persisting metadata.

    Two complementary layers, shared with the transcript/export/copy
    sinks: amplifier-core's key-based ``redact_secrets`` (kernel-only)
    scrubs sensitive metadata KEYS, then the shared value-pattern scrub
    (``model.redaction``) catches secret-shaped VALUES (AWS keys, bearer
    tokens) that key redaction misses (issue #23).
    """
    try:
        from amplifier_core.utils.truncate import redact_secrets

        redacted = redact_secrets(metadata)
    except ImportError:  # pragma: no cover — amplifier-core is a hard dependency
        redacted = metadata
    return scrub_value(redacted)


class SessionStore:
    """Filesystem persistence for one project's sessions.

    Contract:
    - Inputs: session_id (str), transcript (list), metadata (dict),
      normalized UIEvents.
    - Side effects: writes under
      ``~/.amplifier/projects/<slug>/sessions/<id>/``.
    - Errors: ``FileNotFoundError`` for missing sessions, ``ValueError``
      for invalid ids.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        project_dir: Path | None = None,
    ) -> None:
        if base_dir is None:
            base_dir = (
                Path.home() / ".amplifier" / "projects" / get_project_slug(project_dir) / "sessions"
            )
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_recovery_failed = False
        """Set by :meth:`_load_transcript` when a resumed session's
        transcript file(s) existed but were ALL unreadable — the history
        is lost. The runtime surfaces it as a user-facing Notification,
        mirroring ``_load_metadata``'s ``recovered`` marker (which was the
        only side of this pair that spoke up)."""
        self.rewind_recovery_failed = False
        """A pending restore transaction could not be reconciled on load."""
        self.rewind_recovery_interrupted = False
        """A combined restore stopped before code success; conversation stayed put."""

    # -- paths -------------------------------------------------------------

    def session_dir(self, session_id: str) -> Path:
        return self.base_dir / _validate_session_id(session_id)

    def events_path(self, session_id: str) -> Path:
        """The session's UIEvent log — single source of the filename.

        Falls back to the legacy ``events.jsonl`` only when no
        ``ui-events.jsonl`` exists (sessions written before the rename).
        """
        current = self.session_dir(session_id) / EVENTS_FILENAME
        if not current.exists():
            legacy = self.session_dir(session_id) / LEGACY_EVENTS_FILENAME
            if legacy.is_file():
                return legacy
        return current

    def events_read_paths(self, session_id: str) -> tuple[Path, ...]:
        """Existing UIEvent-log files, oldest first.

        A pre-rename session resumed under this build has UIEvents split
        across the legacy ``events.jsonl`` and ``ui-events.jsonl``;
        readers that must see the whole history (cost re-seed, replay)
        consume both. Foreign hook records sharing the legacy filename
        are skipped by kind-aware readers.
        """
        session_dir = self.session_dir(session_id)
        candidates = (session_dir / LEGACY_EVENTS_FILENAME, session_dir / EVENTS_FILENAME)
        return tuple(path for path in candidates if path.is_file())

    def exists(self, session_id: str) -> bool:
        try:
            path = self.session_dir(session_id)
        except ValueError:
            return False
        return path.is_dir()

    # -- save --------------------------------------------------------------

    def save(self, session_id: str, transcript: list[Any], metadata: dict[str, Any]) -> None:
        """Save transcript + metadata atomically (each with backup)."""
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._save_transcript(session_dir, transcript)
        self._save_metadata(session_dir, metadata)
        logger.debug("Session %s saved", session_id)

    def _save_transcript(self, session_dir: Path, transcript: list[Any]) -> None:
        lines: list[str] = []
        for message in transcript:
            msg_dict = message if isinstance(message, dict) else message.model_dump()
            # Keep only the actual conversation: system prompts are merged
            # by providers at request time; developer messages are context.
            if msg_dict.get("role") in ("system", "developer"):
                continue
            # Scrub secret-shaped values at the sink (issue #23) so all
            # block kinds are covered — the transcript path previously
            # only JSON-sanitized, never redacted. Same rules as export,
            # copy and the metadata path (model.redaction).
            lines.append(
                json.dumps(
                    scrub_value(_sanitize_message(message)),
                    ensure_ascii=False,
                    default=_json_default,
                )
            )
        content = "\n".join(lines) + "\n" if lines else ""
        _write_with_backup(session_dir / TRANSCRIPT_FILENAME, content)

    def _save_metadata(self, session_dir: Path, metadata: dict[str, Any]) -> None:
        content = json.dumps(
            _redact_secrets(metadata), indent=2, ensure_ascii=False, default=_json_default
        )
        _write_with_backup(session_dir / METADATA_FILENAME, content)

    # -- load --------------------------------------------------------------

    def load(self, session_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Load (transcript, metadata) with `.backup` corruption recovery."""
        session_dir = self.session_dir(session_id)
        if not session_dir.exists():
            raise FileNotFoundError(f"Session '{session_id}' not found")
        self.rewind_recovery_failed = False
        self.rewind_recovery_interrupted = False
        try:
            self.reconcile_rewind_intent(session_id)
        except (OSError, TypeError, ValueError):
            self.rewind_recovery_failed = True
            logger.warning(
                "Failed to reconcile checkpoint restore for %s", session_id, exc_info=True
            )
        return self._load_transcript(session_dir), self._load_metadata(session_dir)

    def get_metadata(self, session_id: str) -> dict[str, Any]:
        session_dir = self.session_dir(session_id)
        if not session_dir.exists():
            raise FileNotFoundError(f"Session '{session_id}' not found")
        return self._load_metadata(session_dir)

    def transcript_ok(self, session_id: str) -> bool:
        """Best-effort transcript-readability probe (S2 compliance gap 3).

        Reuses :meth:`_load_transcript` -- the EXACT logic a real resume
        runs -- so a listing's prediction of "will this resume cleanly"
        can never disagree with what an actual resume finds. Returns
        ``False`` only when ``transcript.jsonl`` (and its ``.backup``, if
        present) EXISTED but neither parsed; a session with no transcript
        file at all (brand new, nothing saved yet) is NOT unreadable --
        see :data:`~amplifier_app_tui.kernel.session_manager.SessionState`
        for why absence and corruption are different states.

        Does not materialize metadata or keep the parsed message list --
        callers that only need the boolean (session listings) avoid
        holding a second full transcript copy in memory alongside whatever
        :func:`~amplifier_app_tui.kernel.session_manager._message_count`
        already read.
        """
        self._load_transcript(self.session_dir(session_id))
        return not self.transcript_recovery_failed

    def update_metadata(self, session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        metadata = self.get_metadata(session_id)
        metadata.update(updates)
        self._save_metadata(self.session_dir(session_id), metadata)
        return metadata

    def _load_transcript(self, session_dir: Path) -> list[dict[str, Any]]:
        main = session_dir / TRANSCRIPT_FILENAME
        backup = session_dir / (TRANSCRIPT_FILENAME + ".backup")
        self.transcript_recovery_failed = False
        for path, from_backup in ((main, False), (backup, True)):
            if not path.exists():
                continue
            try:
                transcript = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if from_backup:
                    logger.info("Loaded transcript from backup")
                return transcript
            except (OSError, ValueError):
                # ValueError also covers json.JSONDecodeError (a subclass)
                # AND UnicodeDecodeError from a non-UTF-8-corrupted file --
                # same "never silently pass corruption through" reasoning
                # as _load_metadata above.
                logger.warning("Failed to load %s", path, exc_info=True)
        if main.exists() or backup.exists():
            # Both main and .backup existed but neither parsed: a resumed
            # session silently loses its history. _load_metadata already
            # flags this case with a ``recovered`` marker; raise an
            # equivalent signal so the transcript loss is surfaced too.
            self.transcript_recovery_failed = True
            logger.warning(
                "Transcript recovery failed for %s: resumed history is unavailable",
                session_dir.name,
            )
        return []

    def _load_metadata(self, session_dir: Path) -> dict[str, Any]:
        main = session_dir / METADATA_FILENAME
        backup = session_dir / (METADATA_FILENAME + ".backup")
        for path, from_backup in ((main, False), (backup, True)):
            if not path.exists():
                continue
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                if from_backup:
                    logger.info("Loaded metadata from backup")
                return metadata
            except (OSError, ValueError):
                # ValueError also covers json.JSONDecodeError (a subclass)
                # AND UnicodeDecodeError from a non-UTF-8-corrupted file --
                # either way this candidate is unusable; try the next one
                # (S2: every parse-failure shape must reach the "recovered"
                # marker below, not just the JSON-specific one).
                logger.warning("Failed to load %s", path, exc_info=True)
        if main.exists() or backup.exists():
            return {
                "session_id": session_dir.name,
                "recovered": True,
                "recovery_time": datetime.now(UTC).isoformat(),
            }
        return {}

    # -- ui-events.jsonl (append-only normalized UIEvents) ------------------

    def begin_rewind_intent(
        self,
        session_id: str,
        *,
        marker: UIEvent | Mapping[str, Any],
        messages: list[dict[str, Any]],
        ready: bool = True,
    ) -> None:
        """Durably stage conversation state and its rewind marker.

        The record is written before live context mutation. If the process
        exits anywhere between context replacement, transcript save, and
        marker append, :meth:`load` completes the same transaction first.
        """
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        marker_record = (
            dict(marker) if isinstance(marker, Mapping) else marker.model_dump(mode="json")
        )
        safe_messages: list[dict[str, Any]] = []
        for message in messages:
            sanitized = _sanitize_message(message)
            if not isinstance(sanitized, dict):
                raise TypeError("checkpoint restore intent contains an invalid message")
            # Match transcript persistence exactly: provider system prompts
            # and developer context are reconstructed at request time and
            # must not leak into this temporary private transaction file.
            if sanitized.get("role") in {"system", "developer"}:
                continue
            redacted = scrub_value(sanitized)
            if not isinstance(redacted, dict):
                raise TypeError("checkpoint restore intent contains an invalid message")
            safe_messages.append(redacted)
        _write_private_json(
            session_dir / REWIND_INTENT_FILENAME,
            {
                "schema": 1,
                "session_id": session_id,
                "marker": marker_record,
                "messages": safe_messages,
                "ready": ready,
            },
        )

    def arm_rewind_intent(self, session_id: str) -> None:
        """Allow a staged combined restore to reconcile after code succeeds."""
        path = self.session_dir(session_id) / REWIND_INTENT_FILENAME
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("session_id") != session_id:
            raise ValueError("checkpoint restore intent has invalid session identity")
        if raw.get("cancelled") is True:
            raise ValueError("checkpoint restore intent was cancelled")
        raw["ready"] = True
        _write_private_json(path, raw)

    def cancel_rewind_intent(self, session_id: str) -> None:
        """Neutralize a staged intent after a live combined-restore rollback."""
        path = self.session_dir(session_id) / REWIND_INTENT_FILENAME
        if not path.exists():
            return
        # Cancellation is written before unlink so a failed unlink cannot
        # resurrect the abandoned restore on the next process start.
        _write_private_json(
            path,
            {"schema": 1, "session_id": session_id, "cancelled": True},
        )
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            # The durable cancelled marker is sufficient; load() will remove
            # it without applying the abandoned restore.
            logger.warning("Could not remove cancelled rewind intent %s", path, exc_info=True)
            return
        _fsync_directory(path.parent)

    def reconcile_rewind_intent(self, session_id: str) -> bool:
        """Finish a staged restore transaction; return whether one existed."""
        session_dir = self.session_dir(session_id)
        path = session_dir / REWIND_INTENT_FILENAME
        if not path.exists():
            return False
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("session_id") != session_id:
            raise ValueError("checkpoint restore intent has invalid session identity")
        if raw.get("cancelled") is True:
            path.unlink(missing_ok=True)
            try:
                _fsync_directory(session_dir)
            except OSError:
                # If the unlink is lost across a crash, the durable cancelled
                # record is harmless and will be removed again on next load.
                logger.warning("Could not fsync cancelled rewind intent removal", exc_info=True)
            return True
        if raw.get("ready", True) is not True:
            # A process exit during the code half of a combined restore must
            # never consume the conversation retry target on startup. The
            # workspace journal/manifests retain the partial code evidence.
            path.unlink(missing_ok=True)
            try:
                _fsync_directory(session_dir)
            except OSError:
                logger.warning("Could not fsync unready rewind intent removal", exc_info=True)
            self.rewind_recovery_interrupted = True
            return True
        messages = raw.get("messages")
        marker = raw.get("marker")
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise ValueError("checkpoint restore intent has invalid messages")
        if not isinstance(marker, dict) or marker.get("kind") != "rewind_marker":
            raise ValueError("checkpoint restore intent has invalid marker")
        event_id = marker.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("checkpoint restore intent marker lacks event_id")

        metadata = self._load_metadata(session_dir)
        metadata = {
            **metadata,
            "session_id": session_id,
            "created": metadata.get("created", datetime.now(UTC).isoformat()),
            "turn_count": sum(1 for message in messages if message.get("role") == "user"),
            "incremental": True,
            "rewind_reconciled": True,
        }
        self.save(session_id, messages, metadata)
        marker_exists = any(
            record.get("event_id") == event_id for record in self.read_events(session_id)
        )
        if not marker_exists:
            self.append_event_critical(session_id, marker)
            marker_exists = any(
                record.get("event_id") == event_id for record in self.read_events(session_id)
            )
        if not marker_exists:
            # Keep the intent: the next startup/retry can safely append the
            # same event id after the event log is repaired.
            raise OSError("checkpoint restore marker was not readable after append")
        path.unlink()
        try:
            _fsync_directory(session_dir)
        except OSError:
            # Transcript and unique marker were both re-read before unlink.
            # A crash may resurrect the intent, but replay is idempotent by
            # event id; do not wedge the live runtime after the path vanished.
            logger.warning("Could not fsync rewind intent removal", exc_info=True)
        self.rewind_recovery_failed = False
        return True

    def append_event_critical(self, session_id: str, event: UIEvent | Mapping[str, Any]) -> None:
        """Durably append one transaction-critical event or raise."""
        record = dict(event) if isinstance(event, Mapping) else event.model_dump(mode="json")
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / EVENTS_FILENAME
        flags = os.O_APPEND | os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps(record, ensure_ascii=False, default=_json_default) + "\n").encode(
            "utf-8"
        )
        try:
            size = os.fstat(descriptor).st_size
            if size and os.pread(descriptor, 1, size - 1) != b"\n":
                # Isolate the transaction marker from an interrupted final
                # JSONL record. Readers skip the malformed prior line while
                # the newly appended marker remains independently parseable.
                _write_all(descriptor, b"\n")
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(session_dir)

    def append_event(self, session_id: str, event: UIEvent | Mapping[str, Any]) -> None:
        """Append one normalized UIEvent to the session's ui-events.jsonl.

        Always the current filename — the legacy ``events.jsonl`` now
        belongs to hooks-logging and must never receive app records.
        Never raises: event logging is best-effort and must not break a
        running turn.
        """
        record: dict[str, Any]
        if isinstance(event, Mapping):
            record = dict(event)
        else:
            record = event.model_dump(mode="json")
        try:
            session_dir = self.session_dir(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            with (session_dir / EVENTS_FILENAME).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=_json_default))
                handle.write("\n")
        except (OSError, ValueError, TypeError):
            logger.warning("Failed to append event for %s", session_id, exc_info=True)

    def read_events(self, session_id: str) -> Iterator[dict[str, Any]]:
        """Iterate UIEvent records, oldest first, across the log files.

        Skips blank/unparseable lines and foreign records (anything
        without a string ``kind`` — e.g. hooks-logging's ISO-timestamped
        hook events sharing a legacy mixed file).
        """
        for _path, _line_no, record in self.read_events_located(session_id):
            yield record

    def read_events_located(self, session_id: str) -> Iterator[tuple[Path, int, dict[str, Any]]]:
        """Like :meth:`read_events`, but also yields each record's own location.

        ``(path, line_no, record)`` — ``line_no`` is the record's 1-based
        line number within ``path`` (the log is JSONL: one record per
        line). This is the read-side half of the S5 AC2 safe-recovery-
        reference contract: a location cheap to recompute on every read
        (the log is append-only, so a record's line never moves), letting
        :func:`~amplifier_app_tui.kernel.events.parse_event` build an
        ``UnsupportedBlock`` placeholder that can point a user/support
        engineer at the exact persisted line — without the placeholder
        ever carrying the line's own content. Same skip rules as
        :meth:`read_events` (blank/unparseable lines, foreign records
        without a string ``kind``) — that method is now a thin projection
        of this one, so the two can never drift apart.
        """
        for path in self.events_read_paths(session_id):
            with path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and isinstance(record.get("kind"), str):
                        yield path, line_no, record

    # -- listing / lookup ----------------------------------------------------

    def list_sessions(self, *, top_level_only: bool = True) -> list[str]:
        """Session ids, newest first (by directory mtime)."""
        if not self.base_dir.exists():
            return []
        entries: list[tuple[str, float]] = []
        for session_dir in self.base_dir.iterdir():
            if not session_dir.is_dir() or session_dir.name.startswith("."):
                continue
            if top_level_only and not is_top_level_session(session_dir.name):
                continue
            try:
                mtime = session_dir.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append((session_dir.name, mtime))
        entries.sort(key=lambda item: item[1], reverse=True)
        return [name for name, _ in entries]

    def find_session(self, partial_id: str, *, top_level_only: bool = True) -> str:
        """Resolve a session id prefix to exactly one full id."""
        partial_id = partial_id.strip()
        if not partial_id:
            raise ValueError("Session ID cannot be empty")
        if self.exists(partial_id) and (not top_level_only or is_top_level_session(partial_id)):
            return partial_id
        matches = [
            sid
            for sid in self.list_sessions(top_level_only=top_level_only)
            if sid.startswith(partial_id)
        ]
        if not matches:
            raise FileNotFoundError(f"No session found matching '{partial_id}'")
        if len(matches) > 1:
            raise AmbiguousSessionError(partial_id, matches)
        return matches[0]

    # -- lifecycle mutation (delete / cleanup) ------------------------------

    def delete(self, session_id: str) -> bool:
        """Remove a session directory and everything under it.

        Reference contract: amplifier-app-cli ``session delete`` /
        ``SessionStore`` — the id is validated (path-traversal guard), the
        whole ``sessions/<id>/`` tree is removed, and the return says
        whether it existed. Never resolves prefixes: callers resolve via
        :meth:`find_session` first (so an ambiguous prefix cannot silently
        delete the wrong session).
        """
        session_dir = self.session_dir(session_id)
        if not session_dir.is_dir():
            return False
        shutil.rmtree(session_dir)
        logger.info("Deleted session %s", session_id)
        return True

    def cleanup_old_sessions(self, days: int = 30) -> int:
        """Delete top-level sessions whose directory mtime predates *days*.

        Reference: amplifier-app-cli ``SessionStore.cleanup_old_sessions``
        — sessions older than the cutoff are removed and the count is
        returned. ``days`` must be non-negative (``days=0`` removes every
        top-level session). Spawned sub-sessions and dotfiles are skipped;
        a single unreadable/undeletable entry is logged and skipped, never
        fatal.
        """
        if days < 0:
            raise ValueError("days must be non-negative")
        if not self.base_dir.exists():
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).timestamp()
        removed = 0
        for session_dir in self.base_dir.iterdir():
            if not session_dir.is_dir() or session_dir.name.startswith("."):
                continue
            if not is_top_level_session(session_dir.name):
                continue
            try:
                if session_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(session_dir)
                    removed += 1
            except OSError:
                logger.warning("Failed to remove old session %s", session_dir.name, exc_info=True)
        if removed:
            logger.info("Cleaned up %d old sessions", removed)
        return removed


class IncrementalSaver:
    """Debounced transcript save after each tool completion.

    Registered on ``tool:post`` (priority 900, below tracing). Debounces
    on message count: a save happens only when the context has grown
    since the last save. Best-effort — never raises into the hook chain.

    Usage::

        saver = IncrementalSaver(store, session_id, session=session,
                                 base_metadata={"bundle": ..., "model": ...})
        unregister = saver.register(session.coordinator.get("hooks"))
    """

    HOOK_NAME = "tui.incremental_save"

    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        *,
        session: Any,
        base_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.session = session
        self.base_metadata = dict(base_metadata or {})
        self._last_message_count = 0

    async def maybe_save(self) -> bool:
        """Save if the context grew since the last save. Returns True on save."""
        return await self._save(force=False)

    async def force_save(self) -> bool:
        """Persist the context even when rewind made its message count shrink."""
        return await self._save(force=True)

    def mark_saved_message_count(self, count: int) -> None:
        """Align the debounce watermark after external intent reconciliation."""
        self._last_message_count = max(0, count)

    async def _save(self, *, force: bool) -> bool:
        context = self.session.coordinator.get("context")
        if context is None or not hasattr(context, "get_messages"):
            return False
        messages = await context.get_messages()
        if not force and len(messages) <= self._last_message_count:
            return False
        try:
            existing = self.store.get_metadata(self.session_id)
        except FileNotFoundError:
            existing = {}
        metadata = {
            **existing,
            **self.base_metadata,
            "session_id": self.session_id,
            "created": existing.get("created", datetime.now(UTC).isoformat()),
            "turn_count": sum(1 for m in messages if m.get("role") == "user"),
            "incremental": True,
        }
        self.store.save(self.session_id, messages, metadata)
        # Advance the debounce watermark only after both transcript and
        # metadata reached their atomic save seam. A failed write must remain
        # retryable on the next tool completion or forced checkpoint save.
        self._last_message_count = len(messages)
        logger.debug("Incremental save: %d messages", len(messages))
        return True

    async def on_tool_post(self, event: str, data: dict[str, Any]) -> Any:
        """``tool:post`` hook handler — always continues."""
        from amplifier_core.models import HookResult

        try:
            await self.maybe_save()
        except Exception:  # noqa: BLE001 — incremental save is best-effort
            logger.warning("Incremental save failed", exc_info=True)
        return HookResult(action="continue")

    def register(self, hooks: Any, *, priority: int = 900):
        """Register on ``tool:post``; returns the unregister handle."""
        return hooks.register(
            "tool:post", self.on_tool_post, priority=priority, name=self.HOOK_NAME
        )


__all__ = [
    "EVENTS_FILENAME",
    "LEGACY_EVENTS_FILENAME",
    "METADATA_FILENAME",
    "REWIND_INTENT_FILENAME",
    "TRANSCRIPT_FILENAME",
    "IncrementalSaver",
    "SessionStore",
    "is_top_level_session",
]
