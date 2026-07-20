"""Versioned JSONL records for headless ``run`` consumers.

The normalized :class:`~amplifier_app_newtui.kernel.events.UIEvent` queue is
the behavior surface shared with the TUI.  This module only adds a stable,
sequenced wire envelope around those events; it never reconstructs events or
reaches into amplifier-core.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from .events import UIEvent

JSONL_SCHEMA_VERSION = 1


class _EnvelopeArgs(TypedDict):
    sequence: int
    timestamp: str


class _Record(BaseModel):
    """Fields common to every JSONL line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = JSONL_SCHEMA_VERSION
    sequence: int = Field(ge=1)
    timestamp: str


class SessionStarted(_Record):
    """The runtime is mounted and its session identity is known."""

    type: Literal["session.started"] = "session.started"
    session_id: str
    bundle: str
    model: str


class RuntimeEvent(_Record):
    """One normalized event, emitted without waiting for turn completion."""

    type: Literal["runtime.event"] = "runtime.event"
    event: UIEvent


class TurnCompleted(_Record):
    """Terminal success record for the one-shot turn."""

    type: Literal["turn.completed"] = "turn.completed"
    session_id: str
    response: str
    duration_ms: float = Field(ge=0)


class RunError(_Record):
    """Terminal failure record; the process also exits non-zero."""

    type: Literal["error"] = "error"
    session_id: str
    error: str
    error_type: str
    duration_ms: float = Field(ge=0)


JsonlRecord = Annotated[
    SessionStarted | RuntimeEvent | TurnCompleted | RunError,
    Field(discriminator="type"),
]


class JsonlRecords:
    """Monotonic record factory for one invocation."""

    def __init__(self) -> None:
        self._sequence = 0

    def _envelope(self) -> _EnvelopeArgs:
        self._sequence += 1
        return {
            "sequence": self._sequence,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def session_started(
        self, *, session_id: str, bundle: str, model: str
    ) -> SessionStarted:
        return SessionStarted(
            **self._envelope(),
            session_id=session_id,
            bundle=bundle,
            model=model,
        )

    def runtime_event(self, event: UIEvent) -> RuntimeEvent:
        return RuntimeEvent(**self._envelope(), event=event)

    def turn_completed(
        self, *, session_id: str, response: str, duration_ms: float
    ) -> TurnCompleted:
        return TurnCompleted(
            **self._envelope(),
            session_id=session_id,
            response=response,
            duration_ms=duration_ms,
        )

    def error(
        self,
        *,
        session_id: str,
        error: Exception,
        duration_ms: float,
    ) -> RunError:
        return RunError(
            **self._envelope(),
            session_id=session_id,
            error=str(error),
            error_type=type(error).__name__,
            duration_ms=duration_ms,
        )


__all__ = [
    "JSONL_SCHEMA_VERSION",
    "JsonlRecord",
    "JsonlRecords",
    "RunError",
    "RuntimeEvent",
    "SessionStarted",
    "TurnCompleted",
]
