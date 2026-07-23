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
  normalized :class:`~amplifier_app_newtui.kernel.events.UIEvent` dump
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
import shutil
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
"""Pre-rename UIEvent log name — now owned by foundation's hooks-logging
for canonical hook records; read-only fallback, never written."""


def _json_default(value: object) -> str:
    """Last-resort JSON encoder for provider metadata values."""
    return str(value)


def is_top_level_session(session_id: str) -> bool:
    """Spawned sub-sessions carry ``_`` (``{parent}-{hex}_{agent}``)."""
    return "_" not in session_id


def _validate_session_id(session_id: str) -> str:
    if not session_id or not session_id.strip():
        raise ValueError("session_id cannot be empty")
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(f"Invalid session_id: {session_id}")
    return session_id


def _write_with_backup(path: Path, content: str) -> None:
    """Atomic write with backup: existing file → ``.backup``, tmp → replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".backup")
        try:
            backup.write_bytes(path.read_bytes())
        except OSError:
            logger.warning("Could not write backup for %s", path, exc_info=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


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
                Path.home()
                / ".amplifier"
                / "projects"
                / get_project_slug(project_dir)
                / "sessions"
            )
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_recovery_failed = False
        """Set by :meth:`_load_transcript` when a resumed session's
        transcript file(s) existed but were ALL unreadable — the history
        is lost. The runtime surfaces it as a user-facing Notification,
        mirroring ``_load_metadata``'s ``recovered`` marker (which was the
        only side of this pair that spoke up)."""


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
        return self._load_transcript(session_dir), self._load_metadata(session_dir)

    def get_metadata(self, session_id: str) -> dict[str, Any]:
        session_dir = self.session_dir(session_id)
        if not session_dir.exists():
            raise FileNotFoundError(f"Session '{session_id}' not found")
        return self._load_metadata(session_dir)

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
            except (OSError, json.JSONDecodeError):
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
            except (OSError, json.JSONDecodeError):
                logger.warning("Failed to load %s", path, exc_info=True)
        if main.exists() or backup.exists():
            return {
                "session_id": session_dir.name,
                "recovered": True,
                "recovery_time": datetime.now(UTC).isoformat(),
            }
        return {}

    # -- ui-events.jsonl (append-only normalized UIEvents) ------------------

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
        for path in self.events_read_paths(session_id):
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and isinstance(record.get("kind"), str):
                        yield record

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
        if self.exists(partial_id) and (
            not top_level_only or is_top_level_session(partial_id)
        ):
            return partial_id
        matches = [
            sid
            for sid in self.list_sessions(top_level_only=top_level_only)
            if sid.startswith(partial_id)
        ]
        if not matches:
            raise FileNotFoundError(f"No session found matching '{partial_id}'")
        if len(matches) > 1:
            preview = ", ".join(m[:12] + "…" for m in matches[:3])
            extra = f" and {len(matches) - 3} more" if len(matches) > 3 else ""
            raise ValueError(
                f"Ambiguous session ID '{partial_id}' matches "
                f"{len(matches)} sessions: {preview}{extra}"
            )
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

    HOOK_NAME = "newtui.incremental_save"

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
        context = self.session.coordinator.get("context")
        if context is None or not hasattr(context, "get_messages"):
            return False
        messages = await context.get_messages()
        if len(messages) <= self._last_message_count:
            return False
        self._last_message_count = len(messages)

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
    "TRANSCRIPT_FILENAME",
    "IncrementalSaver",
    "SessionStore",
    "is_top_level_session",
]
