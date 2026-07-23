"""Bounded, deadline-driven poller over a real session's UIEvent ledger.

The real lane observes semantic truth via the append-only
``ui-events.jsonl`` a real session persists (ADR-0007 §9), not the ANSI
screen: event kinds present, and the ``sum_prior_cost`` re-seed baseline
resume must reproduce.  The demo runtime does **not** persist (no
``append_event`` on the demo path), so these helpers are real-lane only.

Every wait here is a deadlined poll of :meth:`SessionStore.read_events`
(never ``time.sleep`` as synchronization) -- matching the tier's
flake-resistance clause.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from pathlib import Path

from amplifier_app_newtui.kernel.cost import sum_prior_cost
from amplifier_app_newtui.kernel.persistence import SessionStore


def store_for(project_dir: Path | None = None) -> SessionStore:
    """A :class:`SessionStore` rooted at the project's sessions dir."""
    return SessionStore(project_dir=project_dir)


def newest_session_id(store: SessionStore) -> str | None:
    """The most recently touched top-level session id, if any."""
    sessions = store.list_sessions(top_level_only=True)
    return sessions[0] if sessions else None


def event_kinds(store: SessionStore, session_id: str) -> set[str]:
    """Distinct ``kind`` values currently in the session's ledger."""
    return {record["kind"] for record in store.read_events(session_id)}


def poll_events(
    store: SessionStore,
    session_id: str,
    predicate: Callable[[Iterable[dict[str, object]]], bool],
    *,
    deadline_s: float = 60.0,
    interval_s: float = 0.5,
) -> bool:
    """Poll the ledger until *predicate* holds or *deadline_s* elapses.

    The poll interval is a *cadence*, not a synchronization sleep: the
    exit condition is the ledger predicate, and the whole loop is bounded
    by the deadline.  Returns ``True`` on match, ``False`` on timeout.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        if predicate(list(store.read_events(session_id))):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(interval_s, max(0.0, deadline - time.monotonic())))


def ledger_cost(store: SessionStore, session_id: str) -> Decimal | None:
    """``sum_prior_cost`` over the session's UIEvent log (re-seed baseline)."""
    return sum_prior_cost(store.events_path(session_id))
