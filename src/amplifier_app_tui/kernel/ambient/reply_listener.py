"""Session-owned lifecycle and local discovery for E7's reply listener.

The authenticated reply channel and its loopback HTTP transport live in
``reply.py``.  This module owns the missing runtime concerns around that
transport:

* one live TUI session starts one ephemeral loopback listener;
* the chosen port is published beside that session as a private, atomic
  discovery record;
* shutdown removes only the record owned by that listener and closes the
  socket; and
* every startup/cleanup failure degrades to an explicit status instead of
  preventing the Amplifier session from running.

Discovery records contain no credential and grant no authority.  A caller
still has to submit a signed :class:`~.reply.ReplyEnvelope`, and the channel
still resolves its ``event_id`` to the exact session and decision before it
touches the live ``NeedsYouQueue``.  Records are intentionally session-local:
a local adapter first resolves the event correlation (which includes the
session directory), then discovers the endpoint owned by that session.  This
also lets multiple sessions -- or even multiple views of one session -- own
independent ports without a process-global "last writer wins" file.

This is a same-host transport only.  It does not turn loopback HTTP into a
phone-reachable service, provide TLS/tunnelling, or perform device enrollment.
Those deployment boundaries stay outside this lifecycle.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .reply import (
    CorrelationTable,
    LoopbackReplyListener,
    NeedsYouReplySubmissionPort,
    ReplyChannel,
)

logger = logging.getLogger(__name__)

LISTENERS_DIRNAME = "reply-listeners"
LISTENER_SCHEMA_VERSION = 1
REPLY_PATH = "/reply"

STATUS_STARTED = "started"
STATUS_STOPPED = "stopped"
STATUS_SESSION_UNAVAILABLE = "session_unavailable"
STATUS_BIND_FAILED = "bind_failed"
STATUS_DISCOVERY_FAILED = "discovery_failed"


@dataclass(frozen=True)
class ReplyListenerEndpoint:
    """One discoverable, process-owned loopback endpoint.

    ``owner_id`` is an unguessable cleanup token, not an authentication
    secret.  Authentication remains entirely in :mod:`.reply`.
    """

    session_id: str
    owner_id: str
    host: str
    port: int
    pid: int
    started_at: float

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{REPLY_PATH}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LISTENER_SCHEMA_VERSION,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "host": self.host,
            "port": self.port,
            "pid": self.pid,
            "started_at": self.started_at,
            "reply_path": REPLY_PATH,
        }


@dataclass(frozen=True)
class ReplyListenerStatus:
    """Non-throwing lifecycle result surfaced to the runtime owner."""

    active: bool
    reason: str
    endpoint: ReplyListenerEndpoint | None = None


class _Listener(Protocol):
    @property
    def address(self) -> tuple[str, int]: ...

    def start(self) -> _Listener: ...

    def close(self) -> None: ...


ListenerFactory = Callable[..., _Listener]
ProcessAlive = Callable[[int], bool]
EndpointAlive = Callable[[ReplyListenerEndpoint], bool]


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _endpoint_alive(endpoint: ReplyListenerEndpoint) -> bool:
    """Return whether the recorded loopback socket still accepts connections.

    A live PID alone is insufficient because PIDs can be reused and a running
    TUI process can outlive a failed listener thread.  Discovery is local-only,
    so a short TCP challenge is enough to reject those stale records without
    exposing or exercising the authenticated reply route.
    """

    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=0.1):
            return True
    except OSError:
        return False


def _owner_filename(owner_id: str) -> str:
    digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _parse_endpoint(raw: object) -> ReplyListenerEndpoint | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        if int(raw.get("schema_version", 0)) != LISTENER_SCHEMA_VERSION:
            return None
        session_id = str(raw.get("session_id", "")).strip()
        owner_id = str(raw.get("owner_id", "")).strip()
        host = str(raw.get("host", "")).strip()
        port = int(raw.get("port", 0))
        pid = int(raw.get("pid", 0))
        started_at = float(raw.get("started_at", 0.0))
        address = ipaddress.ip_address(host)
    except (TypeError, ValueError):
        return None
    if (
        not session_id
        or not owner_id
        or not address.is_loopback
        or address.version != 4
        or not 1 <= port <= 65_535
        or pid <= 0
        or started_at <= 0.0
        or raw.get("reply_path") != REPLY_PATH
    ):
        return None
    return ReplyListenerEndpoint(session_id, owner_id, host, port, pid, started_at)


class ReplyListenerRegistry:
    """Private per-session endpoint discovery records.

    Each owner gets its own file, so two live views of the same session do not
    overwrite or delete one another.  Atomic replacement means a discovering
    process sees either the complete old record or the complete new record.
    Dead-PID and malformed records are pruned opportunistically.
    """

    def __init__(
        self,
        session_dir: Path,
        session_id: str,
        *,
        process_alive: ProcessAlive = _process_alive,
        endpoint_alive: EndpointAlive = _endpoint_alive,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.session_id = str(session_id)
        self.root = self.session_dir / LISTENERS_DIRNAME
        self._process_alive = process_alive
        self._endpoint_alive = endpoint_alive

    def registration_path(self, owner_id: str) -> Path:
        return self.root / _owner_filename(owner_id)

    def publish(self, endpoint: ReplyListenerEndpoint) -> Path:
        if endpoint.session_id != self.session_id or not self.session_id:
            raise ValueError("reply listener endpoint belongs to a different session")
        if _parse_endpoint(endpoint.as_dict()) != endpoint:
            raise ValueError("invalid reply listener endpoint")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.discover(prune_stale=True)
        path = self.registration_path(endpoint.owner_id)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(endpoint.as_dict(), stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return path

    def remove(self, owner_id: str) -> None:
        path = self.registration_path(owner_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            raw = None
        endpoint = _parse_endpoint(raw)
        # The hashed filename already scopes this to ``owner_id``.  The row
        # check additionally prevents a corrupted/misplaced file from making
        # one lifecycle delete another owner's discoverable endpoint.
        if endpoint is not None and endpoint.owner_id != owner_id:
            return
        path.unlink(missing_ok=True)
        try:
            self.root.rmdir()
        except OSError:
            pass  # another live listener (or a concurrent publisher) remains

    def discover(self, *, prune_stale: bool = False) -> tuple[ReplyListenerEndpoint, ...]:
        try:
            paths = tuple(self.root.glob("*.json"))
        except OSError:
            return ()
        live: list[ReplyListenerEndpoint] = []
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            endpoint = _parse_endpoint(raw)
            if (
                endpoint is None
                or endpoint.session_id != self.session_id
                or not self._process_alive(endpoint.pid)
                or not self._endpoint_alive(endpoint)
            ):
                if prune_stale:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.debug("could not prune stale reply listener record", exc_info=True)
                continue
            live.append(endpoint)
        # A bridge should try the newest view first; deterministic owner id is
        # the tie-breaker for injected-clock tests.
        return tuple(sorted(live, key=lambda row: (row.started_at, row.owner_id), reverse=True))


def discover_reply_endpoints(
    session_dir: Path,
    session_id: str,
    *,
    prune_stale: bool = False,
) -> tuple[ReplyListenerEndpoint, ...]:
    """Find same-host reply endpoints for an already-resolved session."""

    return ReplyListenerRegistry(session_dir, session_id).discover(prune_stale=prune_stale)


def discover_reply_endpoints_for_event(
    event_id: str,
    *,
    ambient_root: Path | None = None,
    prune_stale: bool = False,
) -> tuple[ReplyListenerEndpoint, ...]:
    """Resolve an attention event directly to its same-session endpoints.

    This is the narrow bridge-facing discovery API: the notification carries
    only ``event_id``; the durable correlation supplies the exact session and
    session directory; the private registry supplies the currently-live
    loopback port.  No caller has to reimplement either binding.
    """

    row = CorrelationTable(ambient_root).resolve(str(event_id))
    if row is None:
        return ()
    session_id = str(row.get("session_id", "")).strip()
    session_dir = str(row.get("session_dir", "")).strip()
    if not session_id or not session_dir:
        return ()
    return discover_reply_endpoints(
        Path(session_dir),
        session_id,
        prune_stale=prune_stale,
    )


class ReplyListenerLifecycle:
    """Own one loopback listener from session boot through session teardown."""

    def __init__(
        self,
        session_id: str,
        session_dir: Path,
        needs_you: Any,
        *,
        ambient_root: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        now: Callable[[], float] = time.time,
        pid: Callable[[], int] = os.getpid,
        listener_factory: ListenerFactory = LoopbackReplyListener,
        registry: ReplyListenerRegistry | None = None,
    ) -> None:
        self.session_id = str(session_id)
        self.session_dir = Path(session_dir)
        self.owner_id = secrets.token_urlsafe(24)
        self._host = host
        self._port = int(port)
        self._now = now
        self._pid = pid
        self._listener_factory = listener_factory
        self._registry = registry or ReplyListenerRegistry(self.session_dir, self.session_id)
        self._channel = ReplyChannel(
            ambient_root,
            now=now,
            submitter=NeedsYouReplySubmissionPort(self.session_id, needs_you),
        )
        self._listener: _Listener | None = None
        self._status = ReplyListenerStatus(False, STATUS_STOPPED)

    @property
    def status(self) -> ReplyListenerStatus:
        return self._status

    def start(self) -> ReplyListenerStatus:
        if self._listener is not None and self._status.active:
            return self._status
        if not self.session_id or not self.session_dir.is_dir():
            self._status = ReplyListenerStatus(False, STATUS_SESSION_UNAVAILABLE)
            return self._status

        listener: _Listener | None = None
        try:
            listener = self._listener_factory(self._channel, host=self._host, port=self._port)
            listener.start()
            host, port = listener.address
        except Exception:  # noqa: BLE001 -- ambient ingress must never block session boot
            if listener is not None:
                self._close_listener(listener)
            logger.warning("ambient reply listener could not bind", exc_info=True)
            self._status = ReplyListenerStatus(False, STATUS_BIND_FAILED)
            return self._status

        try:
            endpoint = ReplyListenerEndpoint(
                session_id=self.session_id,
                owner_id=self.owner_id,
                host=host,
                port=port,
                pid=self._pid(),
                started_at=self._now(),
            )
            self._registry.publish(endpoint)
        except Exception:  # noqa: BLE001 -- no undiscoverable listener may survive startup
            self._close_listener(listener)
            logger.warning("ambient reply listener discovery could not be published", exc_info=True)
            self._status = ReplyListenerStatus(False, STATUS_DISCOVERY_FAILED)
            return self._status

        self._listener = listener
        self._status = ReplyListenerStatus(True, STATUS_STARTED, endpoint)
        return self._status

    def close(self) -> None:
        listener = self._listener
        self._listener = None
        try:
            self._registry.remove(self.owner_id)
        except Exception:  # noqa: BLE001 -- cleanup remains best-effort during process exit
            logger.warning("ambient reply listener discovery cleanup failed", exc_info=True)
        if listener is not None:
            self._close_listener(listener)
        self._status = ReplyListenerStatus(False, STATUS_STOPPED)

    @staticmethod
    def _close_listener(listener: _Listener) -> None:
        try:
            listener.close()
        except Exception:  # noqa: BLE001 -- cleanup must not mask shutdown/boot failures
            logger.warning("ambient reply listener cleanup failed", exc_info=True)

    def __enter__(self) -> ReplyListenerLifecycle:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


__all__ = [
    "LISTENERS_DIRNAME",
    "LISTENER_SCHEMA_VERSION",
    "REPLY_PATH",
    "STATUS_BIND_FAILED",
    "STATUS_DISCOVERY_FAILED",
    "STATUS_SESSION_UNAVAILABLE",
    "STATUS_STARTED",
    "STATUS_STOPPED",
    "ReplyListenerEndpoint",
    "ReplyListenerLifecycle",
    "ReplyListenerRegistry",
    "ReplyListenerStatus",
    "discover_reply_endpoints",
    "discover_reply_endpoints_for_event",
]
