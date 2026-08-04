"""E6 -- cross-project session discovery (AC2 / AC5).

> **AC2** -- sequence approved follow-on actions and report status across
> multiple sessions.
> **AC5** -- the user can open the underlying session and inspect what the
> assistant did, why it paused, and what remains.

``SessionStore.list_sessions()`` enumerates exactly **one** project; there is
no cross-project registry. This module supplies the missing breadth, and it
does it the cheap way the design doc asked for: a **read-side scan** of
``~/.amplifier/projects/*/sessions/*/``, not a new write-side index.

**Why a scan and not an index** (the doc suggested it; this is the
confirmation, plus what would change the answer). Everything an
:class:`ActivityRow` needs is *already* written to disk by B6, B7 and the
event ledger. A projection over those files cannot drift from the truth,
because it **is** the truth, re-read. An index would be a second write
contract to keep in sync with the first -- and the failure mode of a stale
index is the worst one available here: a fleet view that confidently reports
a session is running when it is actually stuck. The scan is O(sessions) per
refresh, and :class:`SessionDiscovery` caches each row on its session
directory's mtime, so a steady-state refresh re-reads only what changed. Add a
write-side index only if the scan is **measured** too slow -- not before.

**Partial, never fatal.** An unreadable session directory produces a row
marked ``partial=True`` rather than raising -- the same posture B6 takes on an
unwritable audit file. A fleet view that dies because one directory has odd
permissions is a fleet view you cannot trust to be looking.

Scope note: this projection is per-user and local. It reads what that user's
own filesystem permissions already allow, and grants nothing new.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..attention_store import ATTENTION_FILENAME
from ..session_control import (
    AUDIT_FILENAME,
    CONTROL_FILENAME,
    attach_command,
    attach_ref,
)

STATE_RUNNING = "running"
STATE_AWAITING_YOU = "paused-awaiting-you"
STATE_FAILED = "failed"
STATE_IDLE = "idle"

_AUDIT_TAIL_BYTES = 64 * 1024
"""How much of ``control-audit.jsonl`` the projection reads. The trail is
append-only and grows without bound; only its tail can answer "why did this
pause", so only its tail is read."""


@dataclass(frozen=True)
class ActivityRow:
    """One session, as the fleet view sees it.

    Every field is a projection of a file B6/B7 already write -- see the
    module docstring on why nothing here is separately maintained.
    """

    session_id: str
    project: str
    session_dir: str
    ref: str
    attach_command: str
    state: str = STATE_IDLE
    holder: dict[str, Any] | None = None
    why_paused: str = ""
    needs_you: str = ""
    attention_event_id: str = ""
    handoff_ids: tuple[str, ...] = ()
    last_activity_at: float = 0.0
    partial: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "session_dir": self.session_dir,
            "ref": self.ref,
            "attach_command": self.attach_command,
            "state": self.state,
            "holder": self.holder,
            "why_paused": self.why_paused,
            "needs_you": self.needs_you,
            "attention_event_id": self.attention_event_id,
            "handoff_ids": list(self.handoff_ids),
            "last_activity_at": self.last_activity_at,
            "partial": self.partial,
        }


def default_projects_root() -> Path:
    """``~/.amplifier/projects`` -- where ``SessionStore`` writes."""
    return Path.home() / ".amplifier" / "projects"


def _read_json(path: Path) -> tuple[dict[str, Any] | None, bool]:
    """``(payload, failed)`` -- ``failed`` distinguishes "absent" from "broken".

    A session that never materialized a control plane has no ``control.json``
    at all; that is normal and is NOT a partial read. A file that exists but
    cannot be parsed is a genuine gap in what we can report, and the row says
    so.
    """
    if not path.exists():
        return None, False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, True
    return (raw, False) if isinstance(raw, dict) else (None, True)


def _audit_tail(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], False
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _AUDIT_TAIL_BYTES))
            blob = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return [], True
    entries: list[dict[str, Any]] = []
    for line in blob.splitlines()[1:] if len(blob) >= _AUDIT_TAIL_BYTES else blob.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue  # a truncated first line from the tail seek, or a torn append
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries, False


def _why_paused(entries: Iterable[Mapping[str, Any]]) -> str:
    why = ""
    for entry in entries:
        if entry.get("action") == "session.paused":
            detail = entry.get("detail")
            if isinstance(detail, Mapping):
                why = str(detail.get("why", "") or "")
        elif entry.get("action") in ("session.resumed", "handoff.claimed"):
            why = ""
    return why


def project_row(session_dir: Path, project: str, *, now: float) -> ActivityRow:
    """Project one session directory into an :class:`ActivityRow`.

    Pure read: opens nothing for writing, takes no lock, and never raises.
    Deliberately callable on its own so the projection is unit-testable over a
    hand-built ``tmp_path`` tree with no store and no runtime.
    """
    session_id = session_dir.name
    partial = False
    control, failed = _read_json(session_dir / CONTROL_FILENAME)
    partial = partial or failed
    attention, failed = _read_json(session_dir / ATTENTION_FILENAME)
    partial = partial or failed
    entries, failed = _audit_tail(session_dir / AUDIT_FILENAME)
    partial = partial or failed

    holder: dict[str, Any] | None = None
    paused = False
    handoffs: tuple[str, ...] = ()
    if control is not None:
        lease = control.get("lease")
        if isinstance(lease, Mapping):
            expires_at = float(lease.get("expires_at") or 0.0)
            if expires_at > now:
                actor = lease.get("actor")
                holder = dict(actor) if isinstance(actor, Mapping) else None
        paused = bool(control.get("paused"))
        rows = control.get("handoffs")
        if isinstance(rows, list):
            handoffs = tuple(
                str(h.get("handoff_id", ""))
                for h in rows
                if isinstance(h, Mapping) and not h.get("claimed_by")
            )

    needs_you = ""
    event_id = ""
    if attention is not None:
        current = attention.get("current")
        by_id = attention.get("by_id")
        if isinstance(current, Mapping) and isinstance(by_id, Mapping):
            event_id = str(current.get(session_id, "") or "")
            row = by_id.get(event_id)
            if isinstance(row, Mapping) and not row.get("acknowledged"):
                needs_you = str(row.get("reason", "") or "")
            else:
                event_id = ""

    if paused:
        state = STATE_AWAITING_YOU
    elif needs_you == "error":
        state = STATE_FAILED
    elif holder is not None:
        state = STATE_RUNNING
    else:
        state = STATE_IDLE

    try:
        last_activity = session_dir.stat().st_mtime
    except OSError:
        last_activity = 0.0
        partial = True

    return ActivityRow(
        session_id=session_id,
        project=project,
        session_dir=str(session_dir),
        ref=attach_ref(session_id),
        attach_command=attach_command(session_id),
        state=state,
        holder=holder,
        why_paused=_why_paused(entries),
        needs_you=needs_you,
        attention_event_id=event_id,
        handoff_ids=handoffs,
        last_activity_at=last_activity,
        partial=partial,
    )


def discover_sessions(
    root: Path | None = None,
    *,
    now: float | None = None,
    project: str | None = None,
) -> list[ActivityRow]:
    """Every session across every project, most recently active first.

    Walks ``<root>/*/sessions/*/``. Sub-session directories (spawned agent
    lanes, which carry ``_``) are included: a stuck delegate is exactly the
    kind of thing the fleet view exists to surface.
    """
    base = Path(root) if root is not None else default_projects_root()
    clock = time.time() if now is None else now
    rows: list[ActivityRow] = []
    try:
        projects = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return []
    for project_dir in projects:
        if project is not None and project_dir.name != project:
            continue
        sessions_dir = project_dir / "sessions"
        try:
            candidates = sorted(s for s in sessions_dir.iterdir() if s.is_dir())
        except OSError:
            continue  # no sessions yet, or unreadable -- neither is an error
        for session_dir in candidates:
            if session_dir.name.startswith("."):
                continue
            rows.append(project_row(session_dir, project_dir.name, now=clock))
    rows.sort(key=lambda row: row.last_activity_at, reverse=True)
    return rows


class SessionDiscovery:
    """mtime-cached :func:`discover_sessions`.

    The cache key is the session directory's own mtime, so a row is re-read
    only when something in that session actually changed. This is the whole
    mitigation for "scan cost grows with session count": the *walk* stays
    O(sessions), but the per-session file reads collapse to O(changed).
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root) if root is not None else default_projects_root()
        self._now = now
        self._cache: dict[str, tuple[float, ActivityRow]] = {}

    def rows(self, *, project: str | None = None) -> list[ActivityRow]:
        clock = self._now()
        fresh: list[ActivityRow] = []
        try:
            projects = sorted(p for p in self.root.iterdir() if p.is_dir())
        except OSError:
            return []
        seen: set[str] = set()
        for project_dir in projects:
            if project is not None and project_dir.name != project:
                continue
            try:
                candidates = sorted(s for s in (project_dir / "sessions").iterdir() if s.is_dir())
            except OSError:
                continue
            for session_dir in candidates:
                if session_dir.name.startswith("."):
                    continue
                key = str(session_dir)
                seen.add(key)
                try:
                    mtime = session_dir.stat().st_mtime
                except OSError:
                    mtime = -1.0
                cached = self._cache.get(key)
                if cached is not None and cached[0] == mtime and mtime >= 0.0:
                    fresh.append(cached[1])
                    continue
                row = project_row(session_dir, project_dir.name, now=clock)
                self._cache[key] = (mtime, row)
                fresh.append(row)
        for stale in set(self._cache) - seen:
            del self._cache[stale]
        fresh.sort(key=lambda row: row.last_activity_at, reverse=True)
        return fresh

    def needing_attention(self) -> list[ActivityRow]:
        """The subset a person should look at: parked, failed, or asking."""
        return [
            row
            for row in self.rows()
            if row.state in (STATE_AWAITING_YOU, STATE_FAILED) or row.needs_you
        ]

    def find_by_event_id(self, event_id: str) -> ActivityRow | None:
        """Resolve a B7 attention ``event_id`` to its session, across projects.

        This is what makes reply-on-open work from a *notification*: the push
        payload carries only the event id, and a phone that taps it has no
        idea which project the session lives in.
        """
        if not event_id:
            return None
        return next((row for row in self.rows() if row.attention_event_id == event_id), None)


__all__ = [
    "STATE_AWAITING_YOU",
    "STATE_FAILED",
    "STATE_IDLE",
    "STATE_RUNNING",
    "ActivityRow",
    "SessionDiscovery",
    "default_projects_root",
    "discover_sessions",
    "project_row",
]
