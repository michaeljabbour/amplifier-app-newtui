"""Agent lanes: per-subagent state keyed by session id (DESIGN-SPEC §8).

Every amplifier event payload carries ``session_id`` + ``parent_id`` —
that pair is the entire routing key for lanes. The registry tolerates
events arriving before their parent lane exists (``session:start`` can
race ``task:agent_spawned`` — RESEARCH-BRIEF risk 5): a lane registered
with an unknown ``parent_id`` still routes; depth is patched when the
parent appears.

Lane line format: ``  <glyph> <name> · <activity> · <elapsed> · $<cost>``
with glyph/color per state: ``◐`` teal running, ``■`` fg working, ``✔``
dim done.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .blocks import StyleToken

LaneStateName = Literal["running", "working", "done"]

_STATE_GLYPHS: dict[LaneStateName, tuple[str, StyleToken]] = {
    "running": ("◐", "teal"),
    "working": ("■", "fg"),
    "done": ("✔", "dim"),
}

_REDACTED_SESSION_RE = re.compile(r"^\[REDACTED:[^\]]+\](?P<suffix>.+)$")


def _redacted_suffix(session_id: str) -> str | None:
    match = _REDACTED_SESSION_RE.match(session_id)
    if match is None:
        return None
    suffix = match.group("suffix")
    # Foundation sub-session suffixes are long random identifiers. Avoid
    # fuzzy-routing short redacted fragments that could match two lanes.
    return suffix if len(suffix) >= 12 else None


def _compatible_session_ids(left: str, right: str) -> bool:
    """Match a redacted spawn id to the real child ``session:start`` id."""
    left_suffix = _redacted_suffix(left)
    right_suffix = _redacted_suffix(right)
    if left_suffix is not None:
        return right.endswith(left_suffix)
    if right_suffix is not None:
        return left.endswith(right_suffix)
    return False


class LaneState(BaseModel):
    """One subagent lane's presentation state.

    - ``name``: agent name (e.g. ``test-writer``).
    - ``glyph``/``color_token``: derived from ``state`` at construction
      via :meth:`for_state` — kept as fields so a lane snapshot is fully
      renderable without lookups.
    - ``activity``: current one-line activity description.
    - ``elapsed``: seconds since spawn; ``cost``: dollars spent so far.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    glyph: str
    color_token: StyleToken
    activity: str = ""
    elapsed: float = Field(default=0.0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal("0"), ge=0)
    state: LaneStateName = "running"

    @classmethod
    def for_state(
        cls,
        *,
        name: str,
        state: LaneStateName,
        activity: str = "",
        elapsed: float = 0.0,
        tokens: int = 0,
        cost: Decimal = Decimal("0"),
    ) -> LaneState:
        """Build a lane with the spec glyph/color for *state*."""
        glyph, color = _STATE_GLYPHS[state]
        return cls(
            name=name,
            glyph=glyph,
            color_token=color,
            activity=activity,
            elapsed=elapsed,
            tokens=tokens,
            cost=cost,
            state=state,
        )


class LaneRecord(BaseModel):
    """A lane plus its routing identity in the session tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    parent_id: str | None
    depth: int = Field(default=1, ge=0)
    started_at: float = Field(default=0.0, ge=0)
    lane: LaneState


class LaneRegistry:
    """All live/finished lanes keyed by ``session_id``, routed by ``parent_id``.

    Mutable by design (one per app). ``register`` opens a lane on
    ``task:agent_spawned``/``session:start``; ``update`` patches activity/
    telemetry from any child-stamped event; ``complete`` closes it on
    ``task:agent_completed``. Unknown-parent registration is tolerated and
    depth is retro-patched when the parent lane appears.

    Concurrency invariant: every writer — the reducer (event consumer,
    heartbeat ``advance``) and the app (``cycle_tail_focus``) — runs on the
    single UI event loop; the runtime thread never touches this registry
    (events are marshalled via the adapter's call_soon_threadsafe queue).
    Methods are synchronous with no awaits, so mutations are atomic under
    cooperative scheduling. Do not call from other threads.
    """

    def __init__(self) -> None:
        self._records: dict[str, LaneRecord] = {}
        self._order: list[str] = []
        self._aliases: dict[str, str] = {}
        self._pending_sessions: dict[str, str | None] = {}
        self._tail_focus: str | None = None
        self._tail_recent: str | None = None

    @property
    def lanes(self) -> tuple[LaneRecord, ...]:
        """All lanes in registration order (the lanes panel listing)."""
        return tuple(self._records[sid] for sid in self._order)

    @property
    def active(self) -> tuple[LaneRecord, ...]:
        return tuple(r for r in self.lanes if r.lane.state != "done")

    @property
    def active_count(self) -> int:
        """Drives ``N agent(s)`` in the working line and the coordinating title."""
        return len(self.active)

    def get(self, session_id: str) -> LaneRecord | None:
        key = self._resolve_id(session_id)
        return self._records.get(key) if key is not None else None

    def children_of(self, parent_id: str) -> tuple[LaneRecord, ...]:
        return tuple(r for r in self.lanes if r.parent_id == parent_id)

    def register(
        self,
        session_id: str,
        *,
        parent_id: str | None,
        name: str,
        activity: str = "",
        state: LaneStateName = "running",
        reopen: bool = False,
        now: float = 0.0,
    ) -> LaneRecord:
        """Open a lane for a spawned subagent.

        Idempotent for known session ids by default (``session:start`` can
        race ``task:agent_spawned``, and a completion that raced ahead of
        its spawn must stay done). With ``reopen=True`` a *finished* lane
        spawned again (a replayed demo turn reuses its sub-session ids) is
        reset to a fresh spawned state so the panel shows the live
        tri-state glyphs instead of a stale ``✔ done``.
        """
        existing_key = self._resolve_id(session_id)
        existing = self._records.get(existing_key) if existing_key is not None else None
        if existing is not None:
            if reopen and existing.lane.state == "done" and state != "done":
                fresh = existing.model_copy(
                    update={
                        "started_at": now,
                        "lane": LaneState.for_state(name=name, state=state, activity=activity),
                    }
                )
                self._records[session_id] = fresh
                return fresh
            return existing
        parent = self._records.get(parent_id) if parent_id else None
        record = LaneRecord(
            session_id=session_id,
            parent_id=parent_id,
            depth=(parent.depth + 1) if parent else 1,
            started_at=now,
            lane=LaneState.for_state(name=name, state=state, activity=activity),
        )
        self._records[session_id] = record
        self._order.append(session_id)
        self._patch_child_depths(session_id)
        for actual_id, actual_parent in tuple(self._pending_sessions.items()):
            if _compatible_session_ids(session_id, actual_id) and (
                actual_parent is None or actual_parent == parent_id
            ):
                rebound = self.bind_session(actual_id, parent_id=actual_parent)
                if rebound is not None:
                    return rebound
        return record

    def bind_session(self, session_id: str, *, parent_id: str | None) -> LaneRecord | None:
        """Bind a real child session id to its possibly-redacted spawn lane.

        Foundation governance can redact the leading portion of
        ``task:agent_spawned.sub_session_id`` while the child's later
        ``session:start`` and usage events carry the usable id. Re-keying
        here restores exact telemetry routing and makes lane focus open the
        real child transcript. The redacted id remains an alias so the
        corresponding ``task:agent_completed`` still closes the lane.
        """
        key = self._resolve_id(session_id, parent_id=parent_id)
        if key is None:
            self._pending_sessions[session_id] = parent_id
            return None
        self._pending_sessions.pop(session_id, None)
        if key == session_id:
            return self._records[key]
        if _redacted_suffix(key) is None or _redacted_suffix(session_id) is not None:
            self._aliases[session_id] = key
            return self._records[key]
        return self._rekey(key, session_id, parent_id=parent_id)

    def update(
        self,
        session_id: str,
        *,
        activity: str | None = None,
        elapsed: float | None = None,
        tokens: int | None = None,
        cost: Decimal | None = None,
        state: LaneStateName | None = None,
    ) -> LaneRecord | None:
        """Patch a lane's live fields; returns None for unknown lanes
        (events for sessions we never saw spawn are dropped, not fatal)."""
        key = self._resolve_id(session_id)
        record = self._records.get(key) if key is not None else None
        if record is None:
            return None
        lane = record.lane
        new_state = state or lane.state
        updated = LaneState.for_state(
            name=lane.name,
            state=new_state,
            activity=lane.activity if activity is None else activity,
            elapsed=lane.elapsed if elapsed is None else elapsed,
            tokens=lane.tokens if tokens is None else tokens,
            cost=lane.cost if cost is None else cost,
        )
        patched = record.model_copy(update={"lane": updated})
        assert key is not None
        self._records[key] = patched
        return patched

    def advance(self, now: float) -> bool:
        """Bump each running lane's ``elapsed`` to ``now - started_at``.

        Driven by the app's 1s heartbeat (via ``reducer.tick``) so a
        subagent's per-lane clock ticks live between the sparse usage
        events. Done lanes are frozen; lanes with no ``started_at`` (never
        stamped at spawn) are left alone. Returns True if any lane moved.
        """
        changed = False
        for session_id, record in self._records.items():
            if record.lane.state == "done" or record.started_at <= 0:
                continue
            elapsed = now - record.started_at
            if elapsed < 0 or elapsed == record.lane.elapsed:
                continue
            updated = record.lane.model_copy(update={"elapsed": elapsed})
            self._records[session_id] = record.model_copy(update={"lane": updated})
            changed = True
        return changed

    def complete(self, session_id: str, *, result: str = "") -> LaneRecord | None:
        """Mark a lane done (``✔`` dim), recording its result summary."""
        activity = f"done · {result}" if result else "done"
        return self.update(session_id, state="done", activity=activity)

    # -- lane tail focus (DESIGN-SPEC §8: live tail) ------------------------

    @property
    def tail_lane(self) -> LaneRecord | None:
        """The lane whose stream feeds the live tail.

        An explicit ctrl-o choice wins while that lane still runs; then the
        most-recently-streaming running lane; then the first running lane.
        None when nothing is running (the tail goes dark).
        """
        for candidate in (self._tail_focus, self._tail_recent):
            if candidate is None:
                continue
            key = self._resolve_id(candidate)
            record = self._records.get(key) if key is not None else None
            if record is not None and record.lane.state != "done":
                return record
        active = self.active
        return active[0] if active else None

    def note_stream_activity(self, session_id: str) -> None:
        """Record *session_id* as the most-recently-streaming lane.

        Unknown or finished lanes are dropped, not fatal (same tolerance
        as :meth:`update`).
        """
        key = self._resolve_id(session_id)
        record = self._records.get(key) if key is not None else None
        if record is not None and record.lane.state != "done":
            self._tail_recent = key

    def cycle_tail_focus(self) -> LaneRecord | None:
        """Pin the tail to the next running lane (ctrl-o), in lane order."""
        active = self.active
        if not active:
            self._tail_focus = None
            return None
        ids = [record.session_id for record in active]
        current = self.tail_lane
        if current is not None and current.session_id in ids:
            index = (ids.index(current.session_id) + 1) % len(ids)
        else:
            index = 0
        self._tail_focus = ids[index]
        return self._records[ids[index]]

    def _patch_child_depths(self, parent_id: str) -> None:
        """Fix depths of children registered before their parent (spawn race)."""
        parent = self._records[parent_id]
        for child in self.children_of(parent_id):
            expected = parent.depth + 1
            if child.depth != expected:
                self._records[child.session_id] = child.model_copy(update={"depth": expected})
                self._patch_child_depths(child.session_id)

    def _resolve_id(self, session_id: str, *, parent_id: str | None = None) -> str | None:
        if session_id in self._records:
            return session_id
        alias = self._aliases.get(session_id)
        if alias in self._records:
            return alias
        matches = [
            key
            for key, record in self._records.items()
            if _compatible_session_ids(key, session_id)
            and (parent_id is None or record.parent_id == parent_id)
        ]
        return matches[0] if len(matches) == 1 else None

    def _rekey(self, old_id: str, new_id: str, *, parent_id: str | None) -> LaneRecord:
        record = self._records.pop(old_id)
        rebound = record.model_copy(
            update={
                "session_id": new_id,
                "parent_id": parent_id if parent_id is not None else record.parent_id,
            }
        )
        self._records[new_id] = rebound
        self._order[self._order.index(old_id)] = new_id
        self._aliases[old_id] = new_id
        for alias, target in tuple(self._aliases.items()):
            if target == old_id:
                self._aliases[alias] = new_id
        for child_id, child in tuple(self._records.items()):
            if child.parent_id == old_id:
                self._records[child_id] = child.model_copy(update={"parent_id": new_id})
        self._patch_child_depths(new_id)
        return rebound


__all__ = ["LaneRecord", "LaneRegistry", "LaneState", "LaneStateName"]
