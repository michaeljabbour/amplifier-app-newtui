"""Durable persistence for :class:`AttentionRecord` state (B7 gap 1).

``ui.notifications.AttentionCenter`` used to keep its dedupe/acknowledgement
bookkeeping in plain in-memory dicts: a second process pointed at the same
session directory could never observe another's attention state, and a
restart lost it outright. This module is the durable half, following the
EXACT idiom :mod:`kernel.session_control` already established for
``control.json`` -- atomic tmp-write + ``os.replace``, guarded by the SAME
``kernel.file_lock`` O_EXCL lock with stale-lock breaking -- rather than
inventing a second persistence mechanism.

Layering (ADR-0007): pure ``kernel/`` logic over the filesystem, no Textual,
no amplifier-core, no dependency on :mod:`ui.notifications` (the ui layer
depends on kernel, never the reverse) -- :class:`AttentionRow` is a
deliberately plain mirror of ``AttentionRecord``'s fields, not an import of
the ui-side dataclass.

Non-blocking by design: :func:`kernel.file_lock.locked` is given a SHORT
timeout here (a fraction of session_control's 5s default) because a
notification must never stall the UI. Unlike a replace-only writer, the
dedupe and acknowledgement operations are read-modify-write transactions:
they check the helper's acquisition result and fail closed rather than run
unlocked. Every public method also never raises: a read failure returns empty
state and a mutation failure returns ``None`` -- a persistence problem must
never block or crash the session, and a stale writer must never overwrite a
newer acknowledgement.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .file_lock import locked as _file_lock

logger = logging.getLogger(__name__)

ATTENTION_FILENAME = "attention.json"
"""Durable attention state, kept beside ``control.json`` in the session dir."""

SCHEMA_VERSION = 1

_LOCK_TIMEOUT = 0.25
"""Deliberately short vs. session_control's 5s default (module docstring)."""

_STALE_AFTER = 30.0


@dataclass(frozen=True)
class AttentionRow:
    """Plain, kernel-side mirror of one ``ui.notifications.AttentionRecord``.

    Deliberately NOT the ui dataclass itself (layering: kernel/ never
    imports ui/) -- ``reason`` is a plain ``str`` here rather than the
    ui-side ``Literal`` restriction, since kernel/ has no reason to know
    the closed set of reasons; the ui layer both narrows and widens at its
    own boundary when it converts to/from ``AttentionRecord``.
    """

    session_id: str
    reason: str
    event_id: str
    detail: str = ""
    created_at: float = 0.0
    acknowledged: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AttentionRow:
        return cls(
            session_id=str(raw.get("session_id", "")),
            reason=str(raw.get("reason", "")),
            event_id=str(raw.get("event_id", "")),
            detail=str(raw.get("detail", "")),
            created_at=float(raw.get("created_at") or 0.0),
            acknowledged=bool(raw.get("acknowledged", False)),
        )


class AttentionStore:
    """Durable ``attention.json`` beside one session directory.

    Mirrors :class:`kernel.session_control.SessionControl`'s persistence
    shape (``_read``/atomic-replace under a lock) at a much smaller scope:
    just the two dicts :class:`~amplifier_app_tui.ui.notifications.
    AttentionCenter` already keeps in memory (``by_id``, ``current``).
    """

    def __init__(
        self,
        session_dir: Path,
        *,
        lock_timeout: float = _LOCK_TIMEOUT,
        stale_after: float = _STALE_AFTER,
    ) -> None:
        self._path = Path(session_dir) / ATTENTION_FILENAME
        self._lock_timeout = lock_timeout
        self._stale_after = stale_after

    def load(self) -> tuple[dict[str, AttentionRow], dict[str, str]]:
        """The durable ``(by_id, current)`` state, or empty on any problem.

        Never raises: a missing file, a torn write from a crashed process,
        or a permissions problem all degrade to "nothing persisted yet" --
        exactly :meth:`kernel.session_control.SessionControl._read`'s own
        tolerance, so a durability problem can never block startup.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, {}
        if not isinstance(raw, dict):
            return {}, {}
        by_id: dict[str, AttentionRow] = {}
        for event_id, row in (raw.get("by_id") or {}).items():
            if isinstance(row, dict):
                by_id[str(event_id)] = AttentionRow.from_dict(row)
        current = {
            str(session_id): str(event_id)
            for session_id, event_id in (raw.get("current") or {}).items()
            if isinstance(session_id, str) and isinstance(event_id, str)
        }
        return by_id, current

    def _write_unlocked(
        self,
        by_id: Mapping[str, AttentionRow],
        current: Mapping[str, str],
    ) -> None:
        """Atomically replace the state file while the caller owns its lock."""

        payload = {
            "schema_version": SCHEMA_VERSION,
            "by_id": {event_id: row.as_dict() for event_id, row in by_id.items()},
            "current": dict(current),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, self._path)

    def record(
        self,
        row: AttentionRow,
    ) -> tuple[dict[str, AttentionRow], dict[str, str], AttentionRow, bool] | None:
        """Atomically claim one attention event ID.

        The returned boolean is true only for the process that inserted the
        event. Every contender reloads inside the inter-process critical
        section, so two centers bound before either write still agree on one
        winner. ``None`` means persistence was unavailable; callers may keep
        operating in memory, but must not replace durable state from a stale
        snapshot.
        """

        try:
            with _file_lock(
                self._path,
                timeout=self._lock_timeout,
                stale_after=self._stale_after,
            ) as acquired:
                if not acquired:
                    logger.debug("attention record lock unavailable; mutation skipped")
                    return None
                by_id, current = self.load()
                existing = by_id.get(row.event_id)
                if existing is not None:
                    return by_id, current, existing, False
                by_id[row.event_id] = row
                current[row.session_id] = row.event_id
                self._write_unlocked(by_id, current)
                return by_id, current, row, True
        except OSError:
            logger.debug("attention record persist failed (non-fatal)", exc_info=True)
            return None

    def acknowledge(
        self,
        session_id: str,
    ) -> tuple[dict[str, AttentionRow], dict[str, str], AttentionRow | None] | None:
        """Atomically acknowledge the current event for *session_id*.

        Acknowledgement is monotonic because the current durable row is read
        and replaced under the same acquired lock. A stale in-memory center
        never supplies the snapshot being written.
        """

        try:
            with _file_lock(
                self._path,
                timeout=self._lock_timeout,
                stale_after=self._stale_after,
            ) as acquired:
                if not acquired:
                    logger.debug("attention acknowledgement lock unavailable; mutation skipped")
                    return None
                by_id, current = self.load()
                event_id = current.get(session_id)
                record = None if event_id is None else by_id.get(event_id)
                if record is None or record.acknowledged:
                    return by_id, current, None
                acknowledged = replace(record, acknowledged=True)
                by_id[acknowledged.event_id] = acknowledged
                self._write_unlocked(by_id, current)
                return by_id, current, acknowledged
        except OSError:
            logger.debug("attention acknowledgement persist failed (non-fatal)", exc_info=True)
            return None

    def save(self, by_id: Mapping[str, AttentionRow], current: Mapping[str, str]) -> None:
        """Best-effort atomic durable write.

        Never raises and never blocks meaningfully: the lock is given a
        short timeout (module docstring) and any failure along the way --
        lock contention, a transient OSError, a read-only session dir --
        is logged at debug and swallowed. Losing one persist attempt only
        means a concurrent reader (or a later restart) sees slightly stale
        state, never a crash or a stall.
        """
        try:
            with _file_lock(
                self._path,
                timeout=self._lock_timeout,
                stale_after=self._stale_after,
            ) as acquired:
                if not acquired:
                    logger.debug("attention state lock unavailable; snapshot skipped")
                    return
                self._write_unlocked(by_id, current)
        except OSError:
            logger.debug("attention state persist failed (non-fatal)", exc_info=True)


__all__ = ["ATTENTION_FILENAME", "AttentionRow", "AttentionStore"]
