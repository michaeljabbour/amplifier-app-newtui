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
            cost=cost,
            state=state,
        )


class LaneRecord(BaseModel):
    """A lane plus its routing identity in the session tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    parent_id: str | None
    depth: int = Field(default=1, ge=0)
    lane: LaneState


class LaneRegistry:
    """All live/finished lanes keyed by ``session_id``, routed by ``parent_id``.

    Mutable by design (one per app). ``register`` opens a lane on
    ``task:agent_spawned``/``session:start``; ``update`` patches activity/
    telemetry from any child-stamped event; ``complete`` closes it on
    ``task:agent_completed``. Unknown-parent registration is tolerated and
    depth is retro-patched when the parent lane appears.
    """

    def __init__(self) -> None:
        self._records: dict[str, LaneRecord] = {}
        self._order: list[str] = []

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
        return self._records.get(session_id)

    def children_of(self, parent_id: str) -> tuple[LaneRecord, ...]:
        return tuple(r for r in self.lanes if r.parent_id == parent_id)

    def register(
        self,
        session_id: str,
        *,
        parent_id: str | None,
        name: str,
        activity: str = "",
    ) -> LaneRecord:
        """Open (or re-open idempotently) a lane for a spawned subagent."""
        existing = self._records.get(session_id)
        if existing is not None:
            return existing
        parent = self._records.get(parent_id) if parent_id else None
        record = LaneRecord(
            session_id=session_id,
            parent_id=parent_id,
            depth=(parent.depth + 1) if parent else 1,
            lane=LaneState.for_state(name=name, state="running", activity=activity),
        )
        self._records[session_id] = record
        self._order.append(session_id)
        self._patch_child_depths(session_id)
        return record

    def update(
        self,
        session_id: str,
        *,
        activity: str | None = None,
        elapsed: float | None = None,
        cost: Decimal | None = None,
        state: LaneStateName | None = None,
    ) -> LaneRecord | None:
        """Patch a lane's live fields; returns None for unknown lanes
        (events for sessions we never saw spawn are dropped, not fatal)."""
        record = self._records.get(session_id)
        if record is None:
            return None
        lane = record.lane
        new_state = state or lane.state
        updated = LaneState.for_state(
            name=lane.name,
            state=new_state,
            activity=lane.activity if activity is None else activity,
            elapsed=lane.elapsed if elapsed is None else elapsed,
            cost=lane.cost if cost is None else cost,
        )
        patched = record.model_copy(update={"lane": updated})
        self._records[session_id] = patched
        return patched

    def complete(self, session_id: str, *, result: str = "") -> LaneRecord | None:
        """Mark a lane done (``✔`` dim), recording its result summary."""
        activity = f"done · {result}" if result else "done"
        return self.update(session_id, state="done", activity=activity)

    def _patch_child_depths(self, parent_id: str) -> None:
        """Fix depths of children registered before their parent (spawn race)."""
        parent = self._records[parent_id]
        for child in self.children_of(parent_id):
            expected = parent.depth + 1
            if child.depth != expected:
                self._records[child.session_id] = child.model_copy(
                    update={"depth": expected}
                )
                self._patch_child_depths(child.session_id)


__all__ = ["LaneRecord", "LaneRegistry", "LaneState", "LaneStateName"]
