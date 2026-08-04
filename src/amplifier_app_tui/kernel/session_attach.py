"""Attach a SECOND process to a LIVE running session, safely.

``serve --attach <ref>`` used to *resume* the referenced session: it booted a
brand-new :class:`~amplifier_app_tui.kernel.runtime.RealRuntime` over the same
session id and claimed the handoff. That attaches to the same session **state**
-- and if the original process was still running, it also produced two live
runtimes appending to one ``ui-events.jsonl``. The lease stops two *clients*
interleaving input; it never stopped two *processes* interleaving transcript.

This module attaches to the live **runtime** instead. The process that owns the
session listens on a Unix domain socket beside the session directory and
advertises it in a durable ``attach.json``; a second process finds that
endpoint and joins as a peer rather than booting a rival runtime. Every record
the owner emits is fanned out to every peer, and every op a peer sends lands in
the owner's own op queue -- so an attached client drives the *same* live
session, with the same lease gate deciding who may write.

The four safety properties, and what each rests on:

**No double-writer.** Exactly one process owns the runtime. Ownership is
claimed under the shared ``O_EXCL`` file lock, and a would-be second owner that
finds a *live* endpoint refuses to boot and attaches instead. One writer to the
ledger, by construction.

**No transcript corruption.** A peer never touches the store. It speaks the
protocol; only the owner's runtime appends events. Follows from the above.

**Deterministic conflict resolution.** Two rules, no timing: *at the process
level*, a live endpoint wins and the newcomer becomes a client; a **stale**
endpoint (owner gone, or socket refusing connections) is broken and the
newcomer becomes the owner -- the same stale-break spirit as the file lock and
lease expiry, so a hard-killed owner can never wedge a session. *At the
participant level*, nothing changes: the existing lease and its takeover
precedence decide who may write.

**Clean detach.** A peer closing its socket removes it from the fan-out and
touches nothing else; the owner keeps running. The owner shutting down unlinks
both the socket and the endpoint file, so the next process sees a clean slate.

Layering (ADR-0007): pure ``kernel/`` -- stdlib asyncio over the filesystem, no
Textual, no amplifier-core, no runtime import. ``AF_UNIX`` is required; where
the platform lacks it (Windows) advertisement is skipped and ``--attach``
degrades to the previous resume-the-session behaviour rather than pretending.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .file_lock import locked

ENDPOINT_FILENAME = "attach.json"
"""Durable advert: which process currently owns this session's live runtime."""

SOCKET_FILENAME = "attach.sock"

PROTOCOL_VERSION = 1

PROBE_TIMEOUT = 1.0
"""Seconds to wait when probing whether an advertised endpoint still answers."""

_SUN_PATH_MAX = 100
"""Conservative ``sockaddr_un`` limit (macOS is 104, Linux 108, minus a margin).

Session directories under a deep project path can exceed it, which is why
:func:`socket_path_for` falls back to a short path in the system temp dir.
"""


def unix_sockets_available() -> bool:
    """Does this platform have ``AF_UNIX``? (Everything but Windows, in practice.)"""
    return hasattr(socket, "AF_UNIX")


@dataclass(frozen=True)
class AttachEndpoint:
    """The advert a session owner publishes: where and who."""

    session_id: str
    pid: int
    socket_path: str
    started_at: float
    protocol: int = PROTOCOL_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "socket_path": self.socket_path,
            "started_at": self.started_at,
            "protocol": self.protocol,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AttachEndpoint:
        return cls(
            session_id=str(raw.get("session_id", "")),
            pid=int(raw.get("pid", 0)),
            socket_path=str(raw.get("socket_path", "")),
            started_at=float(raw.get("started_at", 0.0)),
            protocol=int(raw.get("protocol", PROTOCOL_VERSION)),
        )


def endpoint_path(session_dir: Path) -> Path:
    return Path(session_dir) / ENDPOINT_FILENAME


def socket_path_for(session_dir: Path, session_id: str) -> str:
    """Where to bind. Beside the session when the path fits, else in temp.

    ``sockaddr_un`` truncates silently past ~100 bytes, which would produce a
    socket nobody can find. Falling back to a short, session-id-derived temp
    path keeps deep project trees working instead of failing mysteriously.
    """
    preferred = Path(session_dir) / SOCKET_FILENAME
    if len(str(preferred).encode("utf-8")) <= _SUN_PATH_MAX:
        return str(preferred)
    import tempfile

    return str(Path(tempfile.gettempdir()) / f"amp-attach-{session_id[:16]}.sock")


def read_endpoint(session_dir: Path) -> AttachEndpoint | None:
    """The advertised endpoint, or ``None`` when there is none / it is junk."""
    try:
        raw = json.loads(endpoint_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    endpoint = AttachEndpoint.from_dict(raw)
    return endpoint if endpoint.socket_path else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def endpoint_live(endpoint: AttachEndpoint | None) -> bool:
    """Does this endpoint still answer?

    Two independent checks, because either alone lies: a pid can be recycled,
    and a socket file survives a ``kill -9``. Only a process that is alive AND
    a socket that accepts a connection counts as live -- anything else is
    stale and may be broken by the next owner.
    """
    if endpoint is None or not unix_sockets_available():
        return False
    if not _pid_alive(endpoint.pid):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(PROBE_TIMEOUT)
    try:
        probe.connect(endpoint.socket_path)
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            probe.close()
    return True


def clear_endpoint(session_dir: Path, endpoint: AttachEndpoint | None = None) -> None:
    """Remove a (presumed stale) advert and its socket file."""
    with contextlib.suppress(OSError):
        endpoint_path(session_dir).unlink()
    if endpoint is not None:
        with contextlib.suppress(OSError):
            Path(endpoint.socket_path).unlink()


def live_endpoint(session_dir: Path) -> AttachEndpoint | None:
    """The endpoint if a live owner holds this session, else ``None``.

    Breaking the stale advert here (rather than leaving it) is what stops a
    hard-killed owner from making a session look permanently occupied.
    """
    endpoint = read_endpoint(session_dir)
    if endpoint is None:
        return None
    if endpoint_live(endpoint):
        return endpoint
    clear_endpoint(session_dir, endpoint)
    return None


class AttachServer:
    """The owning process's listener: fan records out, take ops in.

    Records are pushed with :meth:`broadcast` from the event loop thread (the
    same thread ``serve_loop`` emits on), so writes are buffered by asyncio and
    never block the session on a slow or vanished peer.
    """

    def __init__(
        self,
        session_dir: Path,
        session_id: str,
        *,
        on_op: Callable[[dict[str, Any]], None],
        now: Callable[[], float] = time.time,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.session_id = session_id
        self._on_op = on_op
        self._now = now
        self._server: asyncio.AbstractServer | None = None
        self._peers: set[asyncio.StreamWriter] = set()
        self._socket_path = ""
        self.endpoint: AttachEndpoint | None = None

    @property
    def peer_count(self) -> int:
        return len(self._peers)

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def start(self) -> AttachEndpoint | None:
        """Bind, advertise, and start accepting peers. ``None`` if unsupported.

        Claiming is done under the shared file lock and re-checks for a live
        owner inside it, so two processes racing to own the same session
        cannot both win.
        """
        if not unix_sockets_available() or self._server is not None:
            return None
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with locked(endpoint_path(self.session_dir)):
            existing = read_endpoint(self.session_dir)
            if existing is not None and endpoint_live(existing):
                return None  # someone else owns this session; caller should attach
            if existing is not None:
                clear_endpoint(self.session_dir, existing)
            self._socket_path = socket_path_for(self.session_dir, self.session_id)
            with contextlib.suppress(OSError):
                Path(self._socket_path).unlink()
            try:
                self._server = await asyncio.start_unix_server(
                    self._handle_peer, path=self._socket_path
                )
            except (OSError, NotImplementedError):
                self._server = None
                return None
            endpoint = AttachEndpoint(
                session_id=self.session_id,
                pid=os.getpid(),
                socket_path=self._socket_path,
                started_at=self._now(),
            )
            self._write_endpoint(endpoint)
            self.endpoint = endpoint
        return endpoint

    def _write_endpoint(self, endpoint: AttachEndpoint) -> None:
        path = endpoint_path(self.session_dir)
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(endpoint.as_dict()), encoding="utf-8")
        os.replace(tmp, path)

    async def _handle_peer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._peers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # clean detach: the peer closed its end
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    op = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(op, dict):
                    self._on_op(op)
        except (ConnectionError, OSError):
            pass  # an abrupt peer death is a detach, not a session failure
        finally:
            self._peers.discard(writer)
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()

    def broadcast(self, payload: str) -> None:
        """Send one already-serialized line to every attached peer.

        A peer whose socket has gone is dropped rather than raised: the live
        session must never fail because an observer walked away.
        """
        if not self._peers:
            return
        data = payload.encode("utf-8")
        for writer in list(self._peers):
            try:
                writer.write(data)
            except (ConnectionError, OSError, RuntimeError):
                self._peers.discard(writer)

    async def stop(self) -> None:
        """Drop every peer, unbind, and retract the advert."""
        for writer in list(self._peers):
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
        self._peers.clear()
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        if self.endpoint is not None:
            clear_endpoint(self.session_dir, self.endpoint)
            self.endpoint = None


class FanoutWriter:
    """A file-like ``out`` that writes to stdout AND every attached peer.

    ``serve_loop`` emits through a single ``out`` handle, so making attachment
    visible is a matter of substituting this for ``sys.stdout`` -- no emit site
    changes, and no way for a record to reach the primary client but silently
    skip the attached ones. Everyone attached to a session sees the same
    stream, which is the property a shared session needs.
    """

    def __init__(self, primary: IO[str], server: AttachServer | None = None) -> None:
        self.primary = primary
        self.server = server

    def write(self, text: str) -> int:
        written = self.primary.write(text)
        if self.server is not None:
            self.server.broadcast(text)
        return written

    def flush(self) -> None:
        with contextlib.suppress(Exception):
            self.primary.flush()


async def run_attach_client(
    endpoint: AttachEndpoint,
    *,
    source: IO[str],
    out: IO[str],
    hello: dict[str, Any] | None = None,
) -> int:
    """Be a peer of a live session: pipe stdin in, stream records out.

    The attaching process runs THIS instead of booting a runtime. It owns no
    session state, so there is nothing for it to corrupt; it is a protocol
    client that happens to be talking to a local process rather than starting
    one. Returns 0 on a clean detach (either side closing).
    """
    reader, writer = await asyncio.open_unix_connection(endpoint.socket_path)
    loop = asyncio.get_running_loop()
    out.write(
        json.dumps(
            {
                "schema_version": 1,
                "type": "session.attached",
                "session_id": endpoint.session_id,
                "pid": endpoint.pid,
                "socket_path": endpoint.socket_path,
                "mode": "live",
            }
        )
        + "\n"
    )
    out.flush()
    if hello:
        writer.write((json.dumps(hello) + "\n").encode("utf-8"))
        await writer.drain()

    done = asyncio.Event()

    def _pump_stdin() -> None:
        # stdin is blocking; read it on a thread and marshal lines onto the
        # loop, the same idiom serve_loop uses for its own stdin.
        try:
            for line in source:
                text = line.strip()
                if not text:
                    continue
                loop.call_soon_threadsafe(_send, text)
        finally:
            loop.call_soon_threadsafe(done.set)

    def _send(text: str) -> None:
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            writer.write((text + "\n").encode("utf-8"))

    threading.Thread(target=_pump_stdin, daemon=True, name="attach-stdin").start()

    async def _pump_records() -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            out.write(line.decode("utf-8", "replace"))
            out.flush()
        done.set()

    records = asyncio.create_task(_pump_records())
    try:
        await done.wait()
    finally:
        records.cancel()
        with contextlib.suppress(Exception):
            await records
        with contextlib.suppress(ConnectionError, OSError):
            writer.close()
            await writer.wait_closed()
    return 0


def main() -> int:  # pragma: no cover -- convenience entry for manual probing
    """``python -m amplifier_app_tui.kernel.session_attach <session-dir>``."""
    if len(sys.argv) < 2:
        print("usage: session_attach <session-dir>", file=sys.stderr)
        return 2
    endpoint = live_endpoint(Path(sys.argv[1]))
    if endpoint is None:
        print("no live session at that path", file=sys.stderr)
        return 1
    return asyncio.run(run_attach_client(endpoint, source=sys.stdin, out=sys.stdout))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ENDPOINT_FILENAME",
    "PROTOCOL_VERSION",
    "SOCKET_FILENAME",
    "AttachEndpoint",
    "AttachServer",
    "FanoutWriter",
    "clear_endpoint",
    "endpoint_live",
    "endpoint_path",
    "live_endpoint",
    "read_endpoint",
    "run_attach_client",
    "socket_path_for",
    "unix_sockets_available",
]
