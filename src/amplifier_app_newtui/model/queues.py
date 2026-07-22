"""Bounded steering, next-turn message, and needs-you queues.

Steering contract (ADR-0007): exactly ONE steering path — a bounded
:class:`SteeringQueue` (32 items / 32KB per item) consumed one-per-
``provider:request`` step boundary on the root session. Leftover steers
are discarded at turn end (mockup: a steer the runtime never consumed
must not become a turn the user never sent).

Needs-you contract (DESIGN-SPEC §7, ADR-0007 resolution 5): deferred
decisions never halt the turn. A deferred approval resolves to its
default (deny) at timeout, lands in the DenialLog AND stays retro-
answerable here — answering later injects a next-turn user instruction
(the mockup's ``Applying decision: …`` flow).

Thread-safety: both queues are mutated from TWO event loops — the UI
loop (composer enqueue / answer) and the runtime thread's loop (steer
consume at step boundary, kernel-side defer). Each queue guards its
``_pending`` / ``_items`` list and id counter with a ``threading.Lock``.
Change notification runs OUTSIDE the lock so a listener that re-reads the
queue (a common UI pattern) can never deadlock against the mutation that
woke it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict

MAX_QUEUE_ITEMS = 32
MAX_ITEM_CHARS = 32_768

Listener = Callable[[], None]


def _clean_multiline(value: object, limit: int = MAX_ITEM_CHARS) -> str:
    """Strip control characters (keeping newline/tab) and cap length."""
    return "".join(
        ch for ch in str(value) if ch in {"\n", "\t"} or ord(ch) >= 32
    )[:limit]


def _clean_line(value: object, limit: int = MAX_ITEM_CHARS) -> str:
    return " ".join(_clean_multiline(value, limit).split())


class QueuedMessage(BaseModel):
    """One queued item: a mid-turn steer or a full next-turn message.

    ``kind="steer"`` applies at the next step boundary of the running
    turn; ``kind="next_turn"`` runs as its own turn when the current one
    ends (footer ``q1`` badge, ``▹ queued next:`` strip).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str
    text: str
    kind: Literal["steer", "next_turn"] = "steer"
    created_at: float = 0.0


class _ListenerMixin:
    """Shared change-notification plumbing for the mutable queues."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        """Guards the queue's mutable list + id counter across the UI loop
        and the runtime-thread loop. Held only around list mutation and
        snapshotting — never while ``_notify`` runs listener callbacks."""

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Register a change callback; returns its removal function."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()


class SteeringQueue(_ListenerMixin):
    """Bounded FIFO of mid-turn steers and queued next-turn messages.

    Bounds: :data:`MAX_QUEUE_ITEMS` items, :data:`MAX_ITEM_CHARS` chars
    per item. ``enqueue`` raises ``ValueError`` at the limit — the UI
    surfaces that as a notice; it must never drop text silently.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        super().__init__()
        self._clock = clock
        self._next_id = 1
        self._pending: list[QueuedMessage] = []

    @property
    def pending(self) -> tuple[QueuedMessage, ...]:
        with self._lock:
            return tuple(self._pending)

    @property
    def pending_steers(self) -> tuple[QueuedMessage, ...]:
        with self._lock:
            return tuple(m for m in self._pending if m.kind == "steer")

    @property
    def pending_next_turn(self) -> tuple[QueuedMessage, ...]:
        """Queued full next-turn messages (the footer ``qN`` count)."""
        with self._lock:
            return tuple(m for m in self._pending if m.kind == "next_turn")

    def enqueue(
        self, text: object, *, kind: Literal["steer", "next_turn"] = "steer"
    ) -> QueuedMessage:
        """Queue a steer or next-turn message; raises ``ValueError`` when
        full or empty after sanitizing.

        The next-turn slot holds exactly ONE message (mockup single slot,
        ``this.queued = text``): a second ``next_turn`` enqueue REPLACES
        the queued one, so the footer badge is only ever ``· q1``.
        """
        clean = _clean_multiline(text)
        if not clean.strip():
            raise ValueError("queued text cannot be empty")
        with self._lock:
            if kind == "next_turn":
                self._pending = [m for m in self._pending if m.kind != "next_turn"]
            if len(self._pending) >= MAX_QUEUE_ITEMS:
                raise ValueError("steering queue limit reached")
            message = QueuedMessage(
                message_id=f"q-{self._next_id}",
                text=clean,
                kind=kind,
                created_at=self._clock(),
            )
            self._next_id += 1
            self._pending.append(message)
        self._notify()
        return message

    def consume_next_steer(self) -> QueuedMessage | None:
        """Pop the oldest steer (called once per ``provider:request``)."""
        popped: QueuedMessage | None = None
        with self._lock:
            for index, message in enumerate(self._pending):
                if message.kind == "steer":
                    popped = self._pending.pop(index)
                    break
        if popped is not None:
            self._notify()
        return popped

    def consume_next_turn_message(self) -> QueuedMessage | None:
        """Pop the oldest queued next-turn message (called at turn end)."""
        popped: QueuedMessage | None = None
        with self._lock:
            for index, message in enumerate(self._pending):
                if message.kind == "next_turn":
                    popped = self._pending.pop(index)
                    break
        if popped is not None:
            self._notify()
        return popped

    def drain_steers(self) -> tuple[QueuedMessage, ...]:
        """Remove and return all leftover steers (turn ended before they
        applied) — the app discards them at turn end (mockup §5)."""
        with self._lock:
            leftover = tuple(m for m in self._pending if m.kind == "steer")
            if leftover:
                self._pending = [m for m in self._pending if m.kind != "steer"]
        if leftover:
            self._notify()
        return leftover


class LaneSteeringQueue(_ListenerMixin):
    """Per-lane steering: a bounded steer FIFO per running delegate.

    The root :class:`SteeringQueue` steers the coordinator; this steers a
    *child* session (issue #39). It mirrors the same next-boundary
    semantics — each queued message is delivered at that delegate's next
    ``provider:request`` step boundary (kernel/steering.py) — but keys the
    FIFOs by child ``session_id`` so every live lane gets its own queue.

    Bounds match :class:`SteeringQueue`: :data:`MAX_QUEUE_ITEMS` items /
    :data:`MAX_ITEM_CHARS` chars per item, per lane. ``enqueue`` raises
    ``ValueError`` when full or empty — the UI surfaces that as a notice;
    typed text is never dropped silently.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        super().__init__()
        self._clock = clock
        self._next_id = 1
        self._pending: dict[str, list[QueuedMessage]] = {}

    def enqueue(self, session_id: str, text: object) -> QueuedMessage:
        """Queue a steer for the delegate *session_id*; raises ``ValueError``
        when that lane's queue is full or the text is empty."""
        if not session_id:
            raise ValueError("lane steering needs a session id")
        clean = _clean_multiline(text)
        if not clean.strip():
            raise ValueError("queued text cannot be empty")
        queue = self._pending.setdefault(session_id, [])
        if len(queue) >= MAX_QUEUE_ITEMS:
            raise ValueError("lane steering queue limit reached")
        message = QueuedMessage(
            message_id=f"lane-{self._next_id}",
            text=clean,
            kind="steer",
            created_at=self._clock(),
        )
        self._next_id += 1
        queue.append(message)
        self._notify()
        return message

    def pending_for(self, session_id: str) -> tuple[QueuedMessage, ...]:
        """The lane's queued steers, oldest first."""
        return tuple(self._pending.get(session_id, ()))

    def queued_count(self, session_id: str) -> int:
        """Depth of one lane's queue — the ``N queued`` lane-row badge."""
        return len(self._pending.get(session_id, ()))

    def counts(self) -> dict[str, int]:
        """``{session_id: depth}`` for every lane with queued steers."""
        return {sid: len(queue) for sid, queue in self._pending.items() if queue}

    @property
    def total_pending(self) -> int:
        return sum(len(queue) for queue in self._pending.values())

    def consume_next(self, session_id: str) -> QueuedMessage | None:
        """Pop the lane's oldest steer (once per child ``provider:request``)."""
        queue = self._pending.get(session_id)
        if not queue:
            return None
        message = queue.pop(0)
        if not queue:
            del self._pending[session_id]
        self._notify()
        return message

    def drain(self, session_id: str) -> tuple[QueuedMessage, ...]:
        """Drop a finished lane's undelivered steers (it will never reach
        another step boundary) — the lane analogue of
        :meth:`SteeringQueue.drain_steers`."""
        leftover = tuple(self._pending.pop(session_id, ()))
        if leftover:
            self._notify()
        return leftover


NeedsYouStatus = Literal["pending", "answered", "consumed", "dismissed"]


class NeedsYouItem(BaseModel):
    """One deferred decision awaiting the human (DESIGN-SPEC §7).

    ``choices`` are the inline actionable chip labels (e.g.
    ``yes · push to fork``); ``answer`` is filled when the human acts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    question: str
    reason: str = ""
    choices: tuple[str, ...] = ()
    highlight: str = ""
    """Substring of ``question`` the UI accents teal (mockup ``mj/waypoint``)."""
    action: str = ""
    """The denied action this decision defers (joins override records to
    the DenialLog for /improve trust-slot evidence)."""
    status: NeedsYouStatus = "pending"
    answer: str = ""
    created_at: float = 0.0


class NeedsYouQueue(_ListenerMixin):
    """Deferred-decision queue behind the footer ``N decisions waiting ·
    ctrl-y`` badge and the Needs-you block.

    Lifecycle: ``defer`` → ``answer`` (human acts; logs ``Applying
    decision: …``) → ``consume_answered`` (the answer became a next-turn
    instruction). ``dismiss`` drops a decision without acting.
    """

    _MAX_DECISIONS = 100

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        super().__init__()
        self._clock = clock
        self._next_id = 1
        self._items: list[NeedsYouItem] = []
        self._defer_listeners: list[Callable[[NeedsYouItem], None]] = []

    def add_defer_listener(
        self, listener: Callable[[NeedsYouItem], None]
    ) -> Callable[[], None]:
        """Register a per-item deferral callback; returns its removal.

        Plain change listeners can't tell WHAT changed; the real runtime
        needs the created item (decision_id) to surface each kernel-side
        deferral as one UI event without re-deriving it from text."""
        self._defer_listeners.append(listener)

        def remove() -> None:
            if listener in self._defer_listeners:
                self._defer_listeners.remove(listener)

        return remove

    @property
    def items(self) -> tuple[NeedsYouItem, ...]:
        with self._lock:
            return tuple(self._items)

    @property
    def pending(self) -> tuple[NeedsYouItem, ...]:
        with self._lock:
            return tuple(item for item in self._items if item.status == "pending")

    @property
    def pending_count(self) -> int:
        """The footer badge count (``N decisions waiting · ctrl-y``)."""
        return len(self.pending)

    @property
    def answered(self) -> tuple[NeedsYouItem, ...]:
        with self._lock:
            return tuple(item for item in self._items if item.status == "answered")

    def defer(
        self,
        question: object,
        reason: object = "",
        *,
        choices: tuple[str, ...] = (),
        highlight: object = "",
        action: object = "",
    ) -> NeedsYouItem:
        """Park a decision for later; raises ``ValueError`` when full/empty."""
        with self._lock:
            active = [i for i in self._items if i.status in {"pending", "answered"}]
            if len(active) >= self._MAX_DECISIONS:
                raise ValueError("deferred decision limit reached")
            clean_question = _clean_line(question, 4_096)
            if not clean_question:
                raise ValueError("decision question cannot be empty")
            item = NeedsYouItem(
                decision_id=f"decision-{self._next_id}",
                question=clean_question,
                reason=_clean_line(reason, 4_096),
                choices=tuple(_clean_line(c, 200) for c in choices if _clean_line(c, 200)),
                highlight=_clean_line(highlight, 200),
                action=_clean_line(action, 4_096),
                created_at=self._clock(),
            )
            self._next_id += 1
            self._items.append(item)
        self._notify()
        for listener in tuple(self._defer_listeners):
            listener(item)
        return item

    def answer(self, decision_id: str, answer: object) -> NeedsYouItem:
        """Record the human's answer (drives ``Applying decision: …``)."""
        clean_answer = _clean_line(answer, 4_096)
        if not clean_answer:
            raise ValueError("decision answer cannot be empty")
        return self._transition(decision_id, "answered", clean_answer)

    def dismiss(self, decision_id: str) -> NeedsYouItem:
        return self._transition(decision_id, "dismissed", "")

    def consume_answered(self) -> tuple[NeedsYouItem, ...]:
        """Mark all answered decisions consumed (their answers were
        injected as next-turn instructions); returns what was consumed."""
        consumed: list[NeedsYouItem] = []
        with self._lock:
            for index, item in enumerate(self._items):
                if item.status == "answered":
                    updated = item.model_copy(update={"status": "consumed"})
                    self._items[index] = updated
                    consumed.append(updated)
        if consumed:
            self._notify()
        return tuple(consumed)

    def _transition(
        self, decision_id: str, status: NeedsYouStatus, answer: str
    ) -> NeedsYouItem:
        updated: NeedsYouItem | None = None
        with self._lock:
            for index, item in enumerate(self._items):
                if item.decision_id != decision_id:
                    continue
                if item.status != "pending":
                    raise ValueError(f"decision is already {item.status}")
                updated = item.model_copy(update={"status": status, "answer": answer})
                self._items[index] = updated
                break
            else:
                raise KeyError(f"unknown decision: {decision_id}")
        self._notify()
        return updated


__all__ = [
    "MAX_ITEM_CHARS",
    "MAX_QUEUE_ITEMS",
    "LaneSteeringQueue",
    "NeedsYouItem",
    "NeedsYouQueue",
    "NeedsYouStatus",
    "QueuedMessage",
    "SteeringQueue",
]
