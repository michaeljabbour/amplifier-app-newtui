"""Thin, dependency-free client for ``amplifier-newtui`` JSONL runs."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

SCHEMA_VERSION = 1


class RecordEnvelope(TypedDict):
    schema_version: Literal[1]
    sequence: int
    timestamp: str


class SessionStartedRecord(RecordEnvelope):
    type: Literal["session.started"]
    session_id: str
    bundle: str
    model: str


class RuntimeEventRecord(RecordEnvelope):
    type: Literal["runtime.event"]
    event: dict[str, Any]


class TurnCompletedRecord(RecordEnvelope):
    type: Literal["turn.completed"]
    session_id: str
    response: str
    duration_ms: float


class ErrorRecord(RecordEnvelope):
    type: Literal["error"]
    session_id: str
    error: str
    error_type: str
    duration_ms: float


JsonlRecord = (
    SessionStartedRecord | RuntimeEventRecord | TurnCompletedRecord | ErrorRecord
)


class AmplifierSdkError(RuntimeError):
    """Base SDK error."""


class ProtocolError(AmplifierSdkError):
    """The CLI emitted malformed or incompatible JSONL."""


class ProcessError(AmplifierSdkError):
    """The CLI exited without a valid terminal record."""

    def __init__(self, message: str, *, returncode: int | None, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class AmplifierRunError(AmplifierSdkError):
    """A valid terminal ``error`` record from Amplifier."""

    def __init__(self, record: ErrorRecord) -> None:
        super().__init__(f"{record['error_type']}: {record['error']}")
        self.record = record


@dataclass(frozen=True)
class RunResult:
    session_id: str
    bundle: str
    model: str
    response: str
    duration_ms: float
    events: tuple[RuntimeEventRecord, ...]


def _require(
    record: dict[str, Any], name: str, kind: type | tuple[type, ...]
) -> None:
    if not isinstance(record.get(name), kind):
        names = (
            "/".join(item.__name__ for item in kind)
            if isinstance(kind, tuple)
            else kind.__name__
        )
        raise ProtocolError(f"JSONL field {name!r} must be {names}")


def _validate_record(value: Any, expected_sequence: int) -> JsonlRecord:
    if not isinstance(value, dict):
        raise ProtocolError("each JSONL line must be an object")
    record = cast(dict[str, Any], value)
    schema_version = record.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise ProtocolError(
            f"unsupported JSONL schema_version {schema_version!r}; "
            f"expected {SCHEMA_VERSION}"
        )
    sequence = record.get("sequence")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence != expected_sequence
    ):
        raise ProtocolError(
            f"expected JSONL sequence {expected_sequence}, got {sequence!r}"
        )
    _require(record, "timestamp", str)
    record_type = record.get("type")
    if record_type == "session.started":
        for name in ("session_id", "bundle", "model"):
            _require(record, name, str)
    elif record_type == "runtime.event":
        _require(record, "event", dict)
        _require(record["event"], "kind", str)
    elif record_type == "turn.completed":
        for name in ("session_id", "response"):
            _require(record, name, str)
        _require(record, "duration_ms", (int, float))
    elif record_type == "error":
        for name in ("session_id", "error", "error_type"):
            _require(record, name, str)
        _require(record, "duration_ms", (int, float))
    else:
        raise ProtocolError(f"unknown JSONL record type {record_type!r}")
    return cast(JsonlRecord, record)


class AmplifierClient:
    """Spawn one CLI process per run; stdout JSONL is the only API surface."""

    def __init__(
        self,
        command: Sequence[str] = ("amplifier-newtui",),
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = tuple(command)
        self.cwd = Path(cwd) if cwd is not None else None
        self.env = dict(env or {})

    def stream(self, prompt: str, *, bundle: str | None = None) -> Iterator[JsonlRecord]:
        """Yield validated records as the CLI flushes them."""
        args = [*self.command, "run", "--output-format", "jsonl"]
        if bundle:
            args.extend(("--bundle", bundle))
        environment = {**os.environ, **self.env}
        try:
            process = subprocess.Popen(  # noqa: S603 - argv only; shell=False
                args,
                cwd=self.cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            raise ProcessError(
                f"could not start {self.command[0]!r}: {error}",
                returncode=None,
                stderr="",
            ) from error

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_stream = process.stderr
        stderr_parts: list[str] = []

        def drain_stderr() -> None:
            stderr_parts.append(stderr_stream.read())

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        terminal_type: str | None = None
        expected_sequence = 1
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            for line_number, line in enumerate(process.stdout, start=1):
                if not line.strip():
                    continue
                if terminal_type is not None:
                    raise ProtocolError(
                        f"record emitted after terminal {terminal_type!r} at line {line_number}"
                    )
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ProtocolError(f"invalid JSONL at line {line_number}: {error}") from error
                record = _validate_record(decoded, expected_sequence)
                expected_sequence += 1
                if record["type"] in ("turn.completed", "error"):
                    terminal_type = record["type"]
                yield record

            returncode = process.wait()
            stderr_thread.join(timeout=1)
            stderr = "".join(stderr_parts)
            if terminal_type is None:
                raise ProcessError(
                    "CLI exited without a terminal JSONL record",
                    returncode=returncode,
                    stderr=stderr,
                )
            if terminal_type == "turn.completed" and returncode != 0:
                raise ProcessError(
                    "CLI returned non-zero after turn.completed",
                    returncode=returncode,
                    stderr=stderr,
                )
            if terminal_type == "error" and returncode == 0:
                raise ProtocolError("CLI returned zero after terminal error record")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            stderr_thread.join(timeout=1)

    def run(self, prompt: str, *, bundle: str | None = None) -> RunResult:
        """Run to completion, returning the response and normalized events."""
        started: SessionStartedRecord | None = None
        events: list[RuntimeEventRecord] = []
        completed: TurnCompletedRecord | None = None
        failed: ErrorRecord | None = None
        for record in self.stream(prompt, bundle=bundle):
            if record["type"] == "session.started":
                started = record
            elif record["type"] == "runtime.event":
                events.append(record)
            elif record["type"] == "turn.completed":
                completed = record
            else:
                failed = record
        if failed is not None:
            raise AmplifierRunError(failed)
        if started is None or completed is None:
            raise ProtocolError("successful run needs session.started and turn.completed")
        return RunResult(
            session_id=completed["session_id"],
            bundle=started["bundle"],
            model=started["model"],
            response=completed["response"],
            duration_ms=float(completed["duration_ms"]),
            events=tuple(events),
        )


__all__ = [
    "SCHEMA_VERSION",
    "AmplifierClient",
    "AmplifierRunError",
    "AmplifierSdkError",
    "ErrorRecord",
    "JsonlRecord",
    "ProcessError",
    "ProtocolError",
    "RunResult",
    "RuntimeEventRecord",
    "SessionStartedRecord",
    "TurnCompletedRecord",
]
