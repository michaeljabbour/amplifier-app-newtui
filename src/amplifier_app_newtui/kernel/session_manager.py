"""Store-level session lifecycle ops: rename / delete / cleanup / branch.

The interactive slash commands ``/model`` … act on the LIVE coordinator
(:mod:`~amplifier_app_newtui.kernel.session_ops`). THIS module is the
sibling for the *stored* session: the operations amplifier-app-cli
exposes as ``amplifier session <verb>`` (``commands/session.py``) and the
in-session ``/rename`` / ``/branch`` family (``ui/command_sessions.py``,
``ui/core_commands.py``). Re-expressed here over newtui's own
:class:`~amplifier_app_newtui.kernel.persistence.SessionStore` — no
amplifier-app-cli import, no vendored code.

Everything is a plain function over a ``SessionStore`` so it unit-tests
against a tmp-dir store with no coordinator, no Textual and no runtime
thread. Nothing here touches the developer's real ``~/.amplifier`` unless
handed a default-constructed store; tests and probes always pass an
explicit scratch ``base_dir``.

Behavioral contract (donor parity):

- **resolve** — a partial id resolves to exactly one full id
  (:meth:`SessionStore.find_session`): ``FileNotFoundError`` on no match,
  ``ValueError`` on an ambiguous prefix.
- **rename** — writes ``name`` (clamped to :data:`MAX_NAME_LENGTH`) plus a
  ``name_generated_at`` stamp into ``metadata.json`` via
  :meth:`SessionStore.update_metadata`. The name must match
  :data:`NAME_PATTERN` (letters / digits / space / ``. - _``).
- **delete** — removes the whole ``sessions/<id>/`` tree.
- **cleanup** — removes top-level sessions older than *days*.
- **branch** — snapshots a message list into a NEW top-level session id
  carrying ``parent_id`` provenance (the persisted-fork analog of the
  in-memory ``/rewind``).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .persistence import METADATA_FILENAME, TRANSCRIPT_FILENAME, SessionStore

MAX_NAME_LENGTH = 50
"""app-cli ``_rename_session`` clamps the stored name to 50 chars."""

MAX_DIRECTIVE_LENGTH = 2000
"""Clamp the stored fork directive: a starting instruction, not a document.
app-cli's ``/fork`` keeps only a 500-char metadata copy; newtui persists the
whole directive as the child's primed first turn but bounds it so a runaway
paste never bloats ``metadata.json``."""

NAME_PATTERN = re.compile(r"[\w .-]+")
"""app-cli ``core_commands._NAME_PATTERN`` — a friendly, path-safe label."""

PENDING_DIRECTIVE_KEY = "pending_directive"
"""Metadata key holding a fork child's not-yet-run directive (consume-once)."""


def _valid_name(name: str) -> bool:
    return bool(NAME_PATTERN.fullmatch(name))


@dataclass(frozen=True)
class SessionSummary:
    """One row of the resume picker / ``session list`` table.

    ``messages`` is the transcript line count (fast: one ``wc``-style pass,
    matching app-cli's ``_get_session_display_info``); ``mtime`` is the
    directory modification time used for newest-first ordering and the
    human ``time_ago`` label.
    """

    session_id: str
    name: str = ""
    bundle: str = "unknown"
    messages: int = 0
    mtime: float = 0.0

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def time_ago(self) -> str:
        if not self.mtime:
            return "unknown"
        return format_time_ago(datetime.fromtimestamp(self.mtime, tz=UTC))


def format_time_ago(dt: datetime) -> str:
    """Human-readable age of *dt* (``just now`` / ``5m ago`` / ``2d ago``).

    Ported thresholds from app-cli ``commands/session._format_time_ago``.
    """
    elapsed = (datetime.now(UTC) - dt).total_seconds()
    seconds = int(elapsed)
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{days // 365}y ago"


def _message_count(store: SessionStore, session_id: str) -> int:
    path = store.session_dir(session_id) / TRANSCRIPT_FILENAME
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def summary_for(store: SessionStore, session_id: str) -> SessionSummary:
    """Build a :class:`SessionSummary` for one stored session.

    Best-effort: missing/corrupt metadata degrades to empty name and an
    ``unknown`` bundle rather than raising — a listing must never crash on
    one bad session directory.
    """
    session_dir = store.session_dir(session_id)
    mtime = 0.0
    try:
        mtime = session_dir.stat().st_mtime
    except OSError:
        pass
    name = ""
    bundle = "unknown"
    if (session_dir / METADATA_FILENAME).is_file():
        try:
            metadata = store.get_metadata(session_id)
            name = str(metadata.get("name", "") or "")
            bundle = str(metadata.get("bundle", "") or "unknown")
        except (FileNotFoundError, OSError, ValueError):
            pass
    return SessionSummary(
        session_id=session_id,
        name=name,
        bundle=bundle,
        messages=_message_count(store, session_id),
        mtime=mtime,
    )


def list_summaries(store: SessionStore, *, limit: int | None = None) -> list[SessionSummary]:
    """Newest-first :class:`SessionSummary` rows for the top-level sessions."""
    ids = store.list_sessions()
    if limit is not None:
        ids = ids[:limit]
    return [summary_for(store, session_id) for session_id in ids]


def resolve(store: SessionStore, partial_id: str) -> str:
    """Resolve a partial id to one full id (raises like ``find_session``)."""
    return store.find_session(partial_id)


def rename(store: SessionStore, session_id: str, name: str) -> tuple[bool, str]:
    """Rename a stored session; returns ``(ok, message)``.

    Resolves *session_id* as a prefix, validates the name shape and clamps
    to :data:`MAX_NAME_LENGTH`, then persists ``name`` + ``name_generated_at``.
    """
    name = name.strip()
    if not name:
        return (False, "usage: rename <session> <new name>")
    if not _valid_name(name):
        return (False, "name must be letters, numbers, spaces, dot, dash or underscore")
    try:
        resolved = resolve(store, session_id)
    except FileNotFoundError:
        return (False, f"no session found matching '{session_id}'")
    except ValueError as error:
        return (False, str(error))
    clamped = name[:MAX_NAME_LENGTH]
    try:
        store.update_metadata(
            resolved,
            {"name": clamped, "name_generated_at": datetime.now(UTC).isoformat()},
        )
    except (FileNotFoundError, OSError, ValueError) as error:
        return (False, f"could not rename: {error}")
    return (True, clamped)


def delete(store: SessionStore, session_id: str) -> tuple[bool, str]:
    """Delete a stored session; returns ``(ok, resolved_id_or_reason)``."""
    try:
        resolved = resolve(store, session_id)
    except FileNotFoundError:
        return (False, f"no session found matching '{session_id}'")
    except ValueError as error:
        return (False, str(error))
    if store.delete(resolved):
        return (True, resolved)
    return (False, f"session '{resolved}' not found")


def cleanup(store: SessionStore, days: int = 30) -> int:
    """Delete top-level sessions older than *days*; returns the count."""
    return store.cleanup_old_sessions(days=days)


def branch(
    store: SessionStore,
    source_id: str,
    messages: list[dict[str, Any]],
    *,
    name: str = "",
    bundle: str = "",
) -> tuple[bool, str]:
    """Snapshot *messages* into a NEW top-level session; returns ``(ok, id_or_reason)``.

    The persisted-fork analog of the in-memory ``/rewind``: the current
    conversation is written under a fresh uuid-hex id carrying
    ``parent_id`` provenance, so it lists and resumes like any other
    session (app-cli ``core_commands._branch``). ``name`` defaults to
    ``branch-<hex8>`` and is validated when supplied.
    """
    name = name.strip()
    if name and not _valid_name(name):
        return (False, "name must be letters, numbers, spaces, dot, dash or underscore")
    branch_id = uuid.uuid4().hex
    metadata: dict[str, Any] = {
        "session_id": branch_id,
        "parent_id": source_id,
        "branched_at": datetime.now(UTC).isoformat(),
        "bundle": bundle or "unknown",
        "name": (name or f"branch-{branch_id[:8]}")[:MAX_NAME_LENGTH],
    }
    try:
        store.save(branch_id, list(messages), metadata)
    except (OSError, ValueError) as error:
        return (False, f"could not create branch: {error}")
    return (True, branch_id)


def fork(
    store: SessionStore,
    source_id: str,
    messages: list[dict[str, Any]],
    directive: str,
    *,
    name: str = "",
    bundle: str = "",
) -> tuple[bool, str]:
    """Snapshot *messages* into a NEW session PRIMED with a starting *directive*.

    The directive-seeded sibling of :func:`branch`. Like ``/branch`` it copies
    the parent conversation into a fresh top-level session carrying ``parent_id``
    provenance, but it ALSO records a starting ``directive`` in metadata under
    :data:`PENDING_DIRECTIVE_KEY` so the child is *primed*: a later
    ``amplifier-newtui resume <child>`` runs that instruction first
    (:func:`take_pending_directive` → ``RealRuntime.pending_directive`` →
    auto-submitted as the first turn).

    This re-expresses amplifier-app-cli's ``/fork <directive>`` — which folds the
    parent context into an instruction and self-delegates it to a background
    child via ``session.spawn`` — over newtui's persisted session store. True
    detached/background execution is NOT reachable from the full-screen TUI host
    (the same terminal-host seam gap deferred in #45's ``/background``); the
    in-process spawner runs children ephemerally (persist-nothing), so it cannot
    hand back a resumable child. The reachable member is therefore a primed,
    resumable child rather than a background daemon.

    Returns ``(ok, child_id_or_reason)``. An empty directive, a malformed
    ``name``, or a write failure returns ``(False, reason)``.
    """
    directive = directive.strip()
    if not directive:
        return (False, "usage: fork <directive> — a starting instruction is required")
    name = name.strip()
    if name and not _valid_name(name):
        return (False, "name must be letters, numbers, spaces, dot, dash or underscore")
    fork_id = uuid.uuid4().hex
    clamped = directive[:MAX_DIRECTIVE_LENGTH]
    metadata: dict[str, Any] = {
        "session_id": fork_id,
        "parent_id": source_id,
        "forked_at": datetime.now(UTC).isoformat(),
        "fork_directive": clamped,
        PENDING_DIRECTIVE_KEY: clamped,
        "bundle": bundle or "unknown",
        "name": (name or f"fork-{fork_id[:8]}")[:MAX_NAME_LENGTH],
    }
    try:
        store.save(fork_id, list(messages), metadata)
    except (OSError, ValueError) as error:
        return (False, f"could not create fork: {error}")
    return (True, fork_id)


def take_pending_directive(store: SessionStore, session_id: str) -> str:
    """Read and clear a resumed fork child's primed directive (consume-once).

    Returns the directive stored by :func:`fork` under
    :data:`PENDING_DIRECTIVE_KEY` (``""`` when none), then clears it so a later
    resume of the same child does not replay the instruction. ``fork_directive``
    is left in place as durable provenance. Best-effort — a missing session or
    unreadable/unwritable metadata simply yields ``""`` and changes nothing.
    """
    try:
        metadata = store.get_metadata(session_id)
    except (FileNotFoundError, OSError, ValueError):
        return ""
    directive = str(metadata.get(PENDING_DIRECTIVE_KEY) or "").strip()
    if not directive:
        return ""
    try:
        store.update_metadata(session_id, {PENDING_DIRECTIVE_KEY: ""})
    except (FileNotFoundError, OSError, ValueError):
        # Consume anyway: better to run the directive once than to loop on a
        # store we cannot clear. The caller runs it exactly once this boot.
        return directive
    return directive


__all__ = [
    "MAX_DIRECTIVE_LENGTH",
    "MAX_NAME_LENGTH",
    "NAME_PATTERN",
    "PENDING_DIRECTIVE_KEY",
    "SessionSummary",
    "branch",
    "cleanup",
    "delete",
    "fork",
    "format_time_ago",
    "list_summaries",
    "rename",
    "resolve",
    "summary_for",
    "take_pending_directive",
]
