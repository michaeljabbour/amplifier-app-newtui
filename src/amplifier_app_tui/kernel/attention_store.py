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
notification is a best-effort nicety, not a correctness-critical write --
if the lock cannot be acquired promptly, :class:`AttentionStore` simply
proceeds with the atomic write anyway (last-writer-wins is acceptable; a
stalled UI thread is not). Every public method also never raises: a read
failure returns empty state, a write failure is silently skipped -- a
destination/persistence problem must never block or crash the session (B7
hard requirement).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
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

    def save(self, by_id: Mapping[str, AttentionRow], current: Mapping[str, str]) -> None:
        """Best-effort atomic durable write.

        Never raises and never blocks meaningfully: the lock is given a
        short timeout (module docstring) and any failure along the way --
        lock contention, a transient OSError, a read-only session dir --
        is logged at debug and swallowed. Losing one persist attempt only
        means a concurrent reader (or a later restart) sees slightly stale
        state, never a crash or a stall.
        """
        payload = {
            "schema_version": SCHEMA_VERSION,
            "by_id": {event_id: row.as_dict() for event_id, row in by_id.items()},
            "current": dict(current),
        }
        try:
            with _file_lock(self._path, timeout=self._lock_timeout, stale_after=self._stale_after):
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
                tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
                os.replace(tmp, self._path)
        except OSError:
            logger.debug("attention state persist failed (non-fatal)", exc_info=True)


__all__ = ["ATTENTION_FILENAME", "AttentionRow", "AttentionStore"]
