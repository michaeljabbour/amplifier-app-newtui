"""Store-level session lifecycle ops: rename / delete / cleanup / branch.

The interactive slash commands ``/model`` … act on the LIVE coordinator
(:mod:`~amplifier_app_tui.kernel.session_ops`). THIS module is the
sibling for the *stored* session: the operations amplifier-app-cli
exposes as ``amplifier session <verb>`` (``commands/session.py``) and the
in-session ``/rename`` / ``/branch`` family (``ui/command_sessions.py``,
``ui/core_commands.py``). Re-expressed here over tui's own
:class:`~amplifier_app_tui.kernel.persistence.SessionStore` — no
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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .persistence import (
    METADATA_FILENAME,
    TRANSCRIPT_FILENAME,
    AmbiguousSessionError,
    SessionStore,
)

MAX_NAME_LENGTH = 50
"""app-cli ``_rename_session`` clamps the stored name to 50 chars."""

MAX_DIRECTIVE_LENGTH = 2000
"""Clamp the stored fork directive: a starting instruction, not a document.
app-cli's ``/fork`` keeps only a 500-char metadata copy; tui persists the
whole directive as the child's primed first turn but bounds it so a runaway
paste never bloats ``metadata.json``."""

NAME_PATTERN = re.compile(r"[\w .-]+")
"""app-cli ``core_commands._NAME_PATTERN`` — a friendly, path-safe label."""

PENDING_DIRECTIVE_KEY = "pending_directive"
"""Metadata key holding a fork child's not-yet-run directive (consume-once)."""


def _valid_name(name: str) -> bool:
    return bool(NAME_PATTERN.fullmatch(name))


# -- session tags -----------------------------------------------------------
# The donor (opencode) has NO first-class session tags: dialog-tag.tsx is
# file-mention autocomplete and Session.Info carries only a free-form
# ``metadata`` bag (see .ai/oc_donor.md). This is the idiomatic-for-host
# re-expression: tags live in the same ``metadata.json`` the host already
# round-trips, under a ``tags`` list. Constraints mirror NAME_PATTERN's
# path-safe discipline but tighter, since a tag is an index key not a label.

TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
"""A tag: lowercase, starts alnum, then letters / digits / dash / underscore."""

MAX_TAG_LENGTH = 32
"""Longest stored tag; longer inputs are clamped before validation."""

MAX_TAGS = 20
"""Most tags one session may carry; an add that would exceed this is refused."""

TAGS_KEY = "tags"
"""``metadata.json`` key holding the session's sorted, deduped tag list."""


def normalize_tag(raw: str) -> str | None:
    """Normalize one tag or return ``None`` when it cannot be a valid tag.

    Strips, lowercases, clamps to :data:`MAX_TAG_LENGTH`, then requires a full
    :data:`TAG_PATTERN` match. Idempotent: ``normalize_tag(normalize_tag(x))``.
    """
    tag = raw.strip().lower()[:MAX_TAG_LENGTH]
    if not tag or not TAG_PATTERN.fullmatch(tag):
        return None
    return tag


def _coerce_tags(raw: object) -> tuple[str, ...]:
    """Read a persisted tag value into a sorted, deduped, valid tuple.

    Best-effort and total: a missing key, a non-list, or junk members degrade
    to a clean subset rather than raising \u2014 a listing must never crash on one
    session's malformed metadata.
    """
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            tag = normalize_tag(item)
            if tag and tag not in out:
                out.append(tag)
    return tuple(sorted(out))


@dataclass(frozen=True)
class SessionSummary:
    """One row of the resume picker / ``session list`` table.

    ``messages`` is the transcript line count (fast: one ``wc``-style pass,
    matching app-cli's ``_get_session_display_info``); ``mtime`` is the
    directory modification time used for newest-first ordering and the
    human ``time_ago`` label. ``turns`` is the user-turn count the
    incremental saver records as ``turn_count`` in ``metadata.json``
    (see :class:`~amplifier_app_tui.kernel.persistence.SessionSaver`);
    it is ``None`` when the stored metadata predates that field rather than
    a fabricated zero.
    """

    session_id: str
    name: str = ""
    bundle: str = "unknown"
    messages: int = 0
    mtime: float = 0.0
    turns: int | None = None
    tags: tuple[str, ...] = ()

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
    turns: int | None = None
    tags: tuple[str, ...] = ()
    if (session_dir / METADATA_FILENAME).is_file():
        try:
            metadata = store.get_metadata(session_id)
            name = str(metadata.get("name", "") or "")
            bundle = str(metadata.get("bundle", "") or "unknown")
            raw_turns = metadata.get("turn_count")
            if isinstance(raw_turns, int) and not isinstance(raw_turns, bool):
                turns = raw_turns
            tags = _coerce_tags(metadata.get(TAGS_KEY))
        except (FileNotFoundError, OSError, ValueError):
            pass
    return SessionSummary(
        session_id=session_id,
        name=name,
        bundle=bundle,
        messages=_message_count(store, session_id),
        mtime=mtime,
        turns=turns,
        tags=tags,
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


@dataclass(frozen=True)
class ResumeResolution:
    """Outcome of resolving one resume target -- the resume path's one decision
    point (S3), shared by ``resume`` / ``session resume`` / ``run --resume`` /
    ``serve --resume`` so all four commands report the same deterministic
    outcome from a single, kernel-tested function instead of four hand-rolled
    try/excepts that can (and did) drift apart.

    Exactly one status applies:

    - ``"ok"`` -- ``session_id`` is the resolved, readable, unambiguous id.
    - ``"not_found"`` -- no stored session matches ``partial_id``.
    - ``"ambiguous"`` -- ``partial_id`` matches every session in ``candidates``
      (newest-first, full :class:`SessionSummary` rows -- enough to render an
      actionable table, not just a truncated id preview).
    - ``"corrupt"`` -- ``session_id`` resolved to exactly one session, but its
      metadata (and its ``.backup``) could not be read. ``SessionStore``
      already degrades this to a synthesized ``recovered`` stub rather than
      raising (:meth:`SessionStore._load_metadata`); this status is the
      resume path's own probe of that stub so it can refuse to launch into a
      session with no known bundle/identity instead of failing deeper and
      less clearly inside the runtime.
    """

    status: Literal["ok", "not_found", "ambiguous", "corrupt"]
    session_id: str = ""
    candidates: tuple[SessionSummary, ...] = ()
    partial_id: str = ""


def resolve_for_resume(store: SessionStore, partial_id: str) -> ResumeResolution:
    """Resolve *partial_id* for a resume-family command; never raises.

    Thin wrapper over :meth:`SessionStore.find_session` that turns its two
    exception types (``FileNotFoundError``, :class:`AmbiguousSessionError`)
    plus a post-resolve corruption probe into one :class:`ResumeResolution`,
    so CLI callers map status -> exit code / guidance text with no
    try/except of their own (S3).
    """
    try:
        resolved = store.find_session(partial_id)
    except FileNotFoundError:
        return ResumeResolution(status="not_found", partial_id=partial_id)
    except AmbiguousSessionError as error:
        candidates = tuple(summary_for(store, sid) for sid in error.matches)
        return ResumeResolution(status="ambiguous", candidates=candidates, partial_id=partial_id)
    except ValueError:
        # e.g. an empty/whitespace id: nothing to resolve, and not a
        # candidate-bearing ambiguity -- the same user-facing outcome as
        # "not found" rather than a fifth status the CLI brief never asked for.
        return ResumeResolution(status="not_found", partial_id=partial_id)
    metadata = store.get_metadata(resolved)
    if not metadata or metadata.get("recovered"):
        return ResumeResolution(status="corrupt", session_id=resolved, partial_id=partial_id)
    return ResumeResolution(status="ok", session_id=resolved, partial_id=partial_id)


def find_across_projects(
    partial_id: str, amplifier_home: Path | None = None
) -> list[tuple[str, str]]:
    """Search EVERY project's session store for a (prefix) id match.

    Sessions live per working directory (``~/.amplifier/projects/<slug>/
    sessions/``), so a bare ``resume SESSION_ID`` only sees the current dir's
    project — a user who ``cd``'d elsewhere gets a bare "no session found"
    even though the session exists. This backstops that error with an
    actionable cross-project hint. Returns ``(full_id, working_dir)`` pairs
    (working_dir ``""`` when the metadata predates the field). Pure/offline —
    best-effort, never raises on a malformed store."""
    import json

    partial = partial_id.strip()
    root = (amplifier_home or (Path.home() / ".amplifier")) / "projects"
    out: list[tuple[str, str]] = []
    if not partial or not root.is_dir():
        return out
    for project in sorted(root.iterdir()):
        sessions = project / "sessions"
        if not sessions.is_dir():
            continue
        for entry in sessions.iterdir():
            if not (entry.is_dir() and entry.name.startswith(partial)):
                continue
            working_dir = ""
            meta = entry / METADATA_FILENAME
            if meta.is_file():
                try:
                    working_dir = str(json.loads(meta.read_text()).get("working_dir") or "")
                except (OSError, ValueError):
                    working_dir = ""
            out.append((entry.name, working_dir))
    return out


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
    ``amplifier-tui resume <child>`` runs that instruction first
    (:func:`take_pending_directive` → ``RealRuntime.pending_directive`` →
    auto-submitted as the first turn).

    This re-expresses amplifier-app-cli's ``/fork <directive>`` — which folds the
    parent context into an instruction and self-delegates it to a background
    child via ``session.spawn`` — over tui's persisted session store. True
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


@dataclass(frozen=True)
class TagOutcome:
    """Result of one tag read or mutation over a stored session.

    ``tags`` is always the session's full resulting set (sorted); ``changed``
    is the subset actually added/removed this call; ``rejected`` echoes inputs
    that could not be a valid tag. ``ok`` is False only on resolve/IO failure
    or a cap breach, with ``error`` set.
    """

    ok: bool
    session_id: str
    tags: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    error: str = ""


def _normalize_inputs(raw_tags: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split raw inputs into (valid normalized, deduped) and (rejected)."""
    valid: list[str] = []
    rejected: list[str] = []
    for raw in raw_tags:
        text = raw if isinstance(raw, str) else str(raw)
        tag = normalize_tag(text)
        if tag is None:
            stripped = text.strip()
            if stripped and stripped not in rejected:
                rejected.append(stripped)
        elif tag not in valid:
            valid.append(tag)
    return valid, rejected


def ensure_session_dir(store: SessionStore, session_id: str, *, bundle: str = "unknown") -> bool:
    """Persist a minimal metadata shell for a not-yet-saved session.

    A fresh live session persists lazily; tagging it (like ``/rename``) must
    still land, so write a stub ``metadata.json`` first when the dir is absent.
    Returns True when the session dir exists afterwards.
    """
    if store.exists(session_id):
        return True
    try:
        store.save(session_id, [], {"session_id": session_id, "bundle": bundle})
    except (OSError, ValueError):
        return False
    return True


def read_tags(store: SessionStore, session_id: str) -> tuple[str, ...]:
    """Best-effort sorted tag tuple for one session ( () on any read error )."""
    try:
        metadata = store.get_metadata(session_id)
    except (FileNotFoundError, OSError, ValueError):
        return ()
    return _coerce_tags(metadata.get(TAGS_KEY))


def get_tags(store: SessionStore, session_id: str) -> TagOutcome:
    """Read a session's tags; resolves *session_id* as a prefix."""
    try:
        resolved = resolve(store, session_id)
    except FileNotFoundError:
        return TagOutcome(False, session_id, error=f"no session found matching '{session_id}'")
    except ValueError as error:
        return TagOutcome(False, session_id, error=str(error))
    return TagOutcome(True, resolved, tags=read_tags(store, resolved))


def add_tags(store: SessionStore, session_id: str, tags: Iterable[str]) -> TagOutcome:
    """Attach one or more tags to a session (deduped, sorted, capped).

    Invalid inputs are reported in ``rejected`` and skipped. An add that would
    push the session past :data:`MAX_TAGS` is refused whole (``ok=False``, no
    write) so the caller can prune first.
    """
    try:
        resolved = resolve(store, session_id)
    except FileNotFoundError:
        return TagOutcome(False, session_id, error=f"no session found matching '{session_id}'")
    except ValueError as error:
        return TagOutcome(False, session_id, error=str(error))
    valid, rejected = _normalize_inputs(tags)
    current = read_tags(store, resolved)
    changed = tuple(tag for tag in valid if tag not in current)
    union = tuple(sorted(set(current) | set(valid)))
    if len(union) > MAX_TAGS:
        return TagOutcome(
            False,
            resolved,
            tags=current,
            rejected=tuple(rejected),
            error=f"too many tags (max {MAX_TAGS}); remove some first",
        )
    if changed:
        try:
            store.update_metadata(resolved, {TAGS_KEY: list(union)})
        except (FileNotFoundError, OSError, ValueError) as error:
            return TagOutcome(False, resolved, tags=current, error=f"could not save tags: {error}")
    return TagOutcome(True, resolved, tags=union, changed=changed, rejected=tuple(rejected))


def remove_tags(store: SessionStore, session_id: str, tags: Iterable[str]) -> TagOutcome:
    """Detach one or more tags from a session (absent tags are a silent no-op)."""
    try:
        resolved = resolve(store, session_id)
    except FileNotFoundError:
        return TagOutcome(False, session_id, error=f"no session found matching '{session_id}'")
    except ValueError as error:
        return TagOutcome(False, session_id, error=str(error))
    valid, rejected = _normalize_inputs(tags)
    current = read_tags(store, resolved)
    remove = set(valid)
    changed = tuple(tag for tag in current if tag in remove)
    remaining = tuple(tag for tag in current if tag not in remove)
    if changed:
        try:
            store.update_metadata(resolved, {TAGS_KEY: list(remaining)})
        except (FileNotFoundError, OSError, ValueError) as error:
            return TagOutcome(False, resolved, tags=current, error=f"could not save tags: {error}")
    return TagOutcome(True, resolved, tags=remaining, changed=changed, rejected=tuple(rejected))


def sessions_by_tag(store: SessionStore, tag: str) -> list[SessionSummary]:
    """Newest-first summaries of sessions carrying *tag* ( [] if tag invalid )."""
    needle = normalize_tag(tag)
    if needle is None:
        return []
    return [summary for summary in list_summaries(store) if needle in summary.tags]


__all__ = [
    "MAX_DIRECTIVE_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_TAGS",
    "MAX_TAG_LENGTH",
    "NAME_PATTERN",
    "PENDING_DIRECTIVE_KEY",
    "TAGS_KEY",
    "TAG_PATTERN",
    "SessionSummary",
    "TagOutcome",
    "add_tags",
    "branch",
    "cleanup",
    "delete",
    "ensure_session_dir",
    "fork",
    "format_time_ago",
    "get_tags",
    "list_summaries",
    "normalize_tag",
    "read_tags",
    "remove_tags",
    "rename",
    "resolve",
    "sessions_by_tag",
    "summary_for",
    "take_pending_directive",
]
