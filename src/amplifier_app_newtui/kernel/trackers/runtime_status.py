"""Runtime status tracker: turn boundaries + provider usage/notices.

Hook-tracker pattern. Kernel ``SessionStatus`` counters are NOT populated
(RESEARCH-BRIEF §2), so this tracker accumulates provider usage itself
from ``provider:response`` payloads (normalized through
:func:`kernel.events.normalize`, absorbing flat/nested usage shapes).

Turn boundaries follow the ROOT session's ``prompt:submit`` /
``prompt:complete`` / ``execution:end``; usage from child sessions still
counts toward the running turn and the session totals (the parent pays
for its agents). Cost is computed per usage event by an injectable
``cost_fn`` (kept out of this module so pricing tables live in one place).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from time import monotonic
from typing import Any

from amplifier_core import HookResult
from pydantic import BaseModel, ConfigDict, Field

from ..events import ProviderNotice, ProviderResponseUsage, normalize

logger = logging.getLogger(__name__)

Listener = Callable[[], None]
CostFn = Callable[[ProviderResponseUsage], Decimal]


class UsageTotals(BaseModel):
    """Immutable snapshot of accumulated provider usage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_write: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal("0"), ge=0)

    @property
    def cache_hit_pct(self) -> int:
        """Percent of prompt tokens served from cache (the ``NN% cached`` figure)."""
        prompt_total = self.input_tokens + self.cache_read
        if prompt_total <= 0:
            return 0
        return round(100 * self.cache_read / prompt_total)

    def adding(self, usage: ProviderResponseUsage, cost: Decimal) -> UsageTotals:
        return UsageTotals(
            requests=self.requests + 1,
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_read=self.cache_read + usage.cache_read,
            cache_write=self.cache_write + usage.cache_write,
            cost=self.cost + max(Decimal("0"), cost),
        )


class RuntimeSnapshot(BaseModel):
    """One coherent view for the working line, footer, and turn rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    running: bool = False
    turn_elapsed: float = Field(default=0.0, ge=0)
    turn: UsageTotals = Field(default_factory=UsageTotals)
    session: UsageTotals = Field(default_factory=UsageTotals)
    last_notice: ProviderNotice | None = None


class RuntimeStatusTracker:
    """Track turn lifecycle and telemetry; state only, listener-driven."""

    EVENTS = (
        "prompt:submit",
        "prompt:complete",
        "execution:start",
        "execution:end",
        "provider:response",
        "provider:error",
        "provider:retry",
        "provider:throttle",
    )

    def __init__(
        self,
        root_session_id: str,
        *,
        cost_fn: CostFn | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.root_session_id = root_session_id
        self._cost_fn = cost_fn
        self._clock = clock
        self._listeners: list[Listener] = []
        self._running = False
        self._turn_started_at: float | None = None
        self._turn = UsageTotals()
        self._session = UsageTotals()
        self._last_notice: ProviderNotice | None = None

    # -- state ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def turn_elapsed(self) -> float:
        if self._turn_started_at is None:
            return 0.0
        return max(0.0, self._clock() - self._turn_started_at)

    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            running=self._running,
            turn_elapsed=self.turn_elapsed,
            turn=self._turn,
            session=self._session,
            last_notice=self._last_notice,
        )

    def seed_session_cost(self, prior_cost: Decimal) -> None:
        """Re-seed restored spend on resume (ui-events.jsonl replay)."""
        if prior_cost <= 0:
            return
        self._session = self._session.model_copy(update={"cost": self._session.cost + prior_cost})
        self._notify()

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # -- hook plumbing ---------------------------------------------------------

    async def handle_event(self, event: str, data: dict[str, Any]) -> HookResult:
        self.consume(event, data)
        return HookResult(action="continue")

    def register_hooks(self, hooks: Any, *, priority: int = 55) -> Callable[[], None]:
        unregister_callbacks: list[Callable[..., object]] = []
        for event in self.EVENTS:
            unregister = hooks.register(
                event,
                self.handle_event,
                priority=priority,
                name=f"newtui-runtime-status-{event.replace(':', '-')}",
            )
            if callable(unregister):
                unregister_callbacks.append(unregister)

        def unregister_all() -> None:
            for unregister in reversed(unregister_callbacks):
                unregister()

        return unregister_all

    # -- consumption -----------------------------------------------------------

    def consume(self, event: str, data: dict[str, Any]) -> None:
        payload = data or {}
        session_id = str(payload.get("session_id") or self.root_session_id)
        is_root = session_id == self.root_session_id
        if event == "prompt:submit" and is_root:
            self._running = True
            self._turn_started_at = self._clock()
            self._turn = UsageTotals()
            self._last_notice = None
            self._notify()
            return
        if event in {"prompt:complete", "execution:end"} and is_root:
            self._running = False
            self._notify()
            return
        if event == "execution:start" and is_root:
            self._running = True
            if self._turn_started_at is None:
                self._turn_started_at = self._clock()
            self._notify()
            return
        if event == "provider:response":
            usage = normalize(event, payload)
            if isinstance(usage, ProviderResponseUsage):
                self._add_usage(usage)
            return
        if event in {"provider:error", "provider:retry", "provider:throttle"}:
            notice = normalize(event, payload)
            if isinstance(notice, ProviderNotice):
                self._last_notice = notice
                self._notify()

    def _add_usage(self, usage: ProviderResponseUsage) -> None:
        cost = Decimal("0")
        if self._cost_fn is not None:
            try:
                cost = self._cost_fn(usage)
            except Exception:  # noqa: BLE001 — cost function is best-effort
                logger.debug("Cost function failed", exc_info=True)
        self._turn = self._turn.adding(usage, cost)
        self._session = self._session.adding(usage, cost)
        self._notify()

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 — a bad listener must not break notify
                logger.debug("Runtime status listener failed", exc_info=True)


__all__ = ["RuntimeSnapshot", "RuntimeStatusTracker", "UsageTotals"]
