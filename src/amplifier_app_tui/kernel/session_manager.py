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

import logging
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

logger = logging.getLogger(__name__)

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


SessionState = Literal["ok", "recovered", "corrupt", "transcript_lost", "indexing"]
"""Health of one session row in the listing (S2 compliance: a damaged
session must be labeled, never dropped or shown as if it were healthy).

Two independent axes are folded into one field: whether ``metadata.json``
parses, and whether the transcript/index backing it is intact. Only
``ok``/``recovered``/``corrupt`` were surfaced originally; ``transcript_lost``
and ``indexing`` (S2 gap 3, "explicit indexing states") close the gap
between those two extremes for shapes that are genuinely detectable from
the files ``SessionStore`` already reads for a listing -- no state here is
inferred or guessed at:

- ``"ok"`` -- metadata parsed normally AND (when metadata exists) the
  transcript is readable. This also covers a brand-new session with no
  ``metadata.json`` yet and nothing written -- that is a normal, not a
  damaged, state.
- ``"recovered"`` -- ``metadata.json`` (and its ``.backup``) existed but
  neither could be parsed; :meth:`~amplifier_app_tui.kernel.persistence.
  SessionStore._load_metadata` already substitutes a synthetic
  ``{"recovered": True, ...}`` shell rather than raising, but nothing
  upstream surfaced that marker until now.
- ``"corrupt"`` -- building the summary itself raised past that recovery;
  :func:`list_summaries` catches it at the per-session boundary so one bad
  directory cannot take down the whole listing.
- ``"transcript_lost"`` -- metadata parsed cleanly (name/bundle/turns are
  all trustworthy), but ``transcript.jsonl`` (and its ``.backup``, if any)
  EXISTED and neither parsed -- :meth:`~amplifier_app_tui.kernel.
  persistence.SessionStore.transcript_ok` reuses the exact recovery probe
  a real resume runs. The conversation history is gone; the session's
  identity is not. Unlike ``recovered``/``corrupt``, this state is still
  resumable (see :data:`RESUMABLE_STATES`) -- the runtime already resumes
  it today with an empty restored history plus a loud warning
  (``kernel/runtime.py``'s ``transcript_recovery_failed`` notice); this
  just stops the listing from hiding that fact behind a plain ``ok``.
- ``"indexing"`` -- the OPPOSITE asymmetry: ``transcript.jsonl`` has real
  content (``_message_count`` > 0) but ``metadata.json`` does not exist as
  a file at all -- not "recovered" (which requires a file that failed to
  parse), genuinely absent. Every code path that writes a session
  (:meth:`~amplifier_app_tui.kernel.persistence.SessionStore.save`) writes
  the transcript then the metadata in the same call, so this shape is the
  detectable fingerprint of a save interrupted mid-way (a session still
  being indexed/cataloged) or a directory populated by something other
  than this app. Name/bundle/turn count are genuinely unknown, not merely
  unparsed -- there is nothing to resume into (:data:`RESUMABLE_STATES`
  excludes it, matching :func:`resolve_for_resume`'s pre-existing
  no-metadata-at-all refusal).
"""

RESUMABLE_STATES: frozenset[SessionState] = frozenset({"ok", "transcript_lost"})
"""States :func:`resolve_for_resume` treats as launchable (S2 gap 3).

``ok`` and ``transcript_lost`` both carry a fully-readable ``metadata.json``
(bundle/name intact) -- enough for ``RealRuntime`` to boot the same
session; ``transcript_lost`` merely resumes with an empty restored history
plus the existing ``transcript was unreadable`` warning, exactly as it did
before this state existed. ``recovered``, ``corrupt`` and ``indexing`` all
lack a trustworthy bundle/identity to relaunch into, so they stay refused."""


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
    a fabricated zero. ``state`` is :data:`SessionState` -- ``"ok"`` unless
    the session's own files were damaged (S2 compliance).
    """

    session_id: str
    name: str = ""
    bundle: str = "unknown"
    messages: int = 0
    mtime: float = 0.0
    turns: int | None = None
    tags: tuple[str, ...] = ()
    state: SessionState = "ok"

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
    except (OSError, ValueError):
        # ValueError also catches UnicodeDecodeError from a binary/corrupt
        # transcript file -- one session's bad bytes must not raise past
        # this point (list_summaries' "never crash the listing" contract).
        return 0


def summary_for(store: SessionStore, session_id: str) -> SessionSummary:
    """Build a :class:`SessionSummary` for one stored session.

    Best-effort: missing/corrupt metadata degrades to empty name and an
    ``unknown`` bundle rather than raising — a listing must never crash on
    one bad session directory.

    Two damage axes are probed independently (S2 gap 3), in priority
    order: a metadata parse failure (``"recovered"``) always wins over the
    transcript-side checks below, since a synthetic metadata shell already
    means the row is degraded and the finer-grained states assume metadata
    IS trustworthy. See :data:`SessionState` for exactly what each value
    means and how it is detected.
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
    state: SessionState = "ok"
    has_metadata = (session_dir / METADATA_FILENAME).is_file()
    if has_metadata:
        try:
            metadata = store.get_metadata(session_id)
        except (FileNotFoundError, OSError, ValueError):
            # Belt-and-suspenders: _load_metadata degrades a parse failure
            # to a "recovered" dict instead of raising, so this branch is a
            # defensive backstop (e.g. a file removed mid-read) rather than
            # the common path -- either way it is damage, not health.
            state = "recovered"
        else:
            if metadata.get("recovered") is True:
                # persistence.py's own corruption fallback: metadata.json
                # (and its .backup) existed but neither parsed as JSON.
                state = "recovered"
            else:
                name = str(metadata.get("name", "") or "")
                bundle = str(metadata.get("bundle", "") or "unknown")
                raw_turns = metadata.get("turn_count")
                if isinstance(raw_turns, int) and not isinstance(raw_turns, bool):
                    turns = raw_turns
                tags = _coerce_tags(metadata.get(TAGS_KEY))
                # Metadata is genuinely healthy -- the ONLY branch where the
                # transcript-side probe applies (a "recovered" shell already
                # means the row is damaged; no need to compound it).
                if not store.transcript_ok(session_id):
                    state = "transcript_lost"
    messages = _message_count(store, session_id)
    if not has_metadata and messages > 0:
        # The opposite asymmetry: real transcript content but no catalog
        # entry at all -- every save() writes both files together, so this
        # is the fingerprint of an interrupted write (S2 gap 3), not a
        # brand-new/empty session (which has messages == 0 here too and
        # correctly stays "ok").
        state = "indexing"
    return SessionSummary(
        session_id=session_id,
        name=name,
        bundle=bundle,
        messages=messages,
        mtime=mtime,
        turns=turns,
        tags=tags,
        state=state,
    )


def list_summaries(store: SessionStore, *, limit: int | None = None) -> list[SessionSummary]:
    """Newest-first :class:`SessionSummary` rows for the top-level sessions.

    Each row is built independently and defensively: if one session's files
    are damaged beyond even ``summary_for``'s own recovery, that ONE session
    becomes a bare ``state="corrupt"`` row instead of raising past this
    point -- the listing itself must never crash on one bad directory (S2
    compliance).
    """
    ids = store.list_sessions()
    if limit is not None:
        ids = ids[:limit]
    summaries: list[SessionSummary] = []
    for session_id in ids:
        try:
            summaries.append(summary_for(store, session_id))
        except Exception:  # noqa: BLE001 -- one bad session must not crash the listing
            logger.warning("Could not summarize session %s", session_id, exc_info=True)
            summaries.append(SessionSummary(session_id=session_id, state="corrupt"))
    return summaries


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
    - ``"corrupt"`` -- ``session_id`` resolved to exactly one session, but it
      is not resumable: :func:`summary_for` (S2's own per-session probe, the
      SAME one :func:`list_summaries` uses) reports a :data:`SessionState`
      outside :data:`RESUMABLE_STATES`, or it has no ``metadata.json`` at all
      (see :func:`resolve_for_resume` for why that extra case is
      resume-specific rather than a second corruption probe). Note this is
      NOT simply "state != ok": ``"transcript_lost"`` still resolves ``"ok"``
      here -- its metadata is intact, so the runtime can still boot it (with
      an empty restored history and a loud warning), unlike ``"recovered"``,
      ``"corrupt"`` and ``"indexing"``.
    """

    status: Literal["ok", "not_found", "ambiguous", "corrupt"]
    session_id: str = ""
    candidates: tuple[SessionSummary, ...] = ()
    partial_id: str = ""


def resolve_for_resume(store: SessionStore, partial_id: str) -> ResumeResolution:
    """Resolve *partial_id* for a resume-family command; never raises.

    Thin wrapper over :meth:`SessionStore.find_session` that turns its two
    exception types (``FileNotFoundError``, :class:`AmbiguousSessionError`)
    plus a post-resolve health check into one :class:`ResumeResolution`, so
    CLI callers map status -> exit code / guidance text with no try/except
    of their own (S3).

    The health check reuses :func:`summary_for` -- the SAME per-session
    probe :func:`list_summaries` uses (S2) -- rather than a second,
    independent reading of ``metadata.json``: any :data:`SessionState`
    outside :data:`RESUMABLE_STATES` maps to this function's ``"corrupt"``
    status (S2 gap 3: ``"transcript_lost"`` is the one non-``"ok"`` state
    that still counts as resumable -- its metadata is fully intact, so
    ``RealRuntime`` boots it exactly as it always has, just with an empty
    restored history and the pre-existing loud warning; ``"recovered"``,
    ``"corrupt"`` and ``"indexing"`` all still refuse). ``summary_for``
    itself never raises, but the call is still guarded here so this
    function's own "never raises" contract cannot be broken by a future
    change to it.

    One extra, resume-specific rule sits on top of that shared probe: a
    session directory with NO ``metadata.json`` at all is refused here
    even on the rare chance :func:`summary_for` reported ``"ok"`` for it
    (a still-being-written, message-less brand-new session lists cleanly
    as ``"ok"`` -- see :data:`SessionState`) -- there is no bundle/identity
    to relaunch a resume into, so this refuses it too rather than failing
    deeper and less clearly inside the runtime.
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
    try:
        healthy = summary_for(store, resolved).state in RESUMABLE_STATES
    except Exception:  # noqa: BLE001 -- resolve_for_resume must never raise (S3)
        healthy = False
    has_metadata = (store.session_dir(resolved) / METADATA_FILENAME).is_file()
    if not healthy or not has_metadata:
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
    "RESUMABLE_STATES",
    "TAGS_KEY",
    "TAG_PATTERN",
    "SessionState",
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
