"""Attaching to a LIVE runtime -- the endpoint, the fan-out, the stale break.

``serve --attach <ref>`` used to *resume* a session: it booted a second
``RealRuntime`` over the same session id. If the first process was still
running, that was two live runtimes appending to one ``ui-events.jsonl``. The
lease stops two *clients* interleaving input; nothing stopped two *processes*
interleaving transcript.

:mod:`~amplifier_app_tui.kernel.session_attach` makes the owner advertise a
socket and the newcomer join it instead. The cross-process proof lives in
``tests/test_session_control_multiprocess.py``; this file pins the mechanism
underneath it, and in particular the two ways it could quietly fail: believing
a *stale* advert (a hard-killed owner would make a session look permanently
occupied) and *not* believing a live one (which is the double-writer).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.session_attach import (
    ENDPOINT_FILENAME,
    AttachEndpoint,
    AttachServer,
    FanoutWriter,
    endpoint_live,
    endpoint_path,
    live_endpoint,
    read_endpoint,
    socket_path_for,
    unix_sockets_available,
)

pytestmark = pytest.mark.skipif(
    not unix_sockets_available(), reason="live attachment needs AF_UNIX"
)

SESSION_ID = "a" * 32


class _Sink:
    def __init__(self) -> None:
        self.text = ""

    def write(self, s: str) -> int:
        self.text += s
        return len(s)

    def flush(self) -> None:
        pass


async def _server(session_dir: Path, ops: list[dict[str, object]]) -> AttachServer:
    server = AttachServer(session_dir, SESSION_ID, on_op=ops.append)
    assert await server.start() is not None
    return server


# -- the advert ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_owner_advertises_a_durable_endpoint(tmp_path: Path) -> None:
    """Durable on purpose: another process finds it with only the session path."""
    server = await _server(tmp_path, [])
    try:
        endpoint = read_endpoint(tmp_path)
        assert endpoint is not None
        assert endpoint.pid == os.getpid()
        assert endpoint.session_id == SESSION_ID
        assert endpoint_live(endpoint)
        assert live_endpoint(tmp_path) is not None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stopping_retracts_the_advert(tmp_path: Path) -> None:
    """Clean detach from the owner's side: the next process sees a free session."""
    server = await _server(tmp_path, [])
    socket_path = Path(read_endpoint(tmp_path).socket_path)  # type: ignore[union-attr]
    await server.stop()

    assert not endpoint_path(tmp_path).exists()
    assert not socket_path.exists()
    assert live_endpoint(tmp_path) is None


def test_a_stale_advert_never_wedges_a_session(tmp_path: Path) -> None:
    """A hard-killed owner leaves the file behind. Believing it would make the
    session look permanently occupied -- the exact failure lease expiry exists
    to prevent, one level up."""
    endpoint_path(tmp_path).write_text(
        json.dumps(
            AttachEndpoint(
                session_id=SESSION_ID,
                pid=2**30,  # a pid that cannot be running
                socket_path=str(tmp_path / "gone.sock"),
                started_at=1.0,
            ).as_dict()
        ),
        encoding="utf-8",
    )

    assert read_endpoint(tmp_path) is not None, "the advert is there..."
    assert live_endpoint(tmp_path) is None, "...but it is not live, and is cleared"
    assert not endpoint_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_an_advert_whose_socket_died_is_also_stale(tmp_path: Path) -> None:
    """A live pid is not enough: the socket file survives a kill -9, so both
    checks have to agree before a newcomer stands down."""
    endpoint = AttachEndpoint(
        session_id=SESSION_ID,
        pid=os.getpid(),  # alive
        socket_path=str(tmp_path / "never-bound.sock"),  # but nothing listens
        started_at=1.0,
    )
    assert not endpoint_live(endpoint)


@pytest.mark.asyncio
async def test_a_second_owner_stands_down_rather_than_double_writing(
    tmp_path: Path,
) -> None:
    """The double-writer defence, at its narrowest point.

    Starting a second server over a live one must fail *cleanly* -- ``None``
    means "attach instead", and any other answer is two runtimes on one ledger.
    """
    first = await _server(tmp_path, [])
    try:
        second = AttachServer(tmp_path, SESSION_ID, on_op=lambda _op: None)
        assert await second.start() is None
        assert not second.listening
        # The original owner is untouched.
        assert read_endpoint(tmp_path).pid == os.getpid()  # type: ignore[union-attr]
    finally:
        await first.stop()


def test_the_socket_always_lands_somewhere_bindable(tmp_path: Path) -> None:
    """``sockaddr_un`` truncates silently past ~100 bytes, which would produce a
    socket nobody can find. Deep project trees are ordinary (and a pytest
    ``tmp_path`` on macOS is already one), so the path degrades to a short temp
    one rather than failing mysteriously -- while a session dir that fits keeps
    its socket beside the session where it belongs."""
    deep = tmp_path.joinpath(*["a-fairly-long-directory-name"] * 6)
    fallback = socket_path_for(deep, SESSION_ID)
    assert not fallback.startswith(str(deep))
    assert len(fallback.encode()) <= 100

    beside = socket_path_for(Path("/tmp/amp-b6"), SESSION_ID)  # noqa: S108 -- short by construction
    assert beside == "/tmp/amp-b6/attach.sock"  # noqa: S108


# -- the fan-out --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_sees_the_same_records_and_can_drive_the_session(
    tmp_path: Path,
) -> None:
    """One session, one stream. Every participant sees what the others see, and
    a peer's op lands in the owner's queue -- so the owner's lease gate decides
    who may write, no matter which side asked."""
    ops: list[dict[str, object]] = []
    server = await _server(tmp_path, ops)
    try:
        endpoint = read_endpoint(tmp_path)
        assert endpoint is not None
        reader, writer = await asyncio.open_unix_connection(endpoint.socket_path)

        writer.write(json.dumps({"op": "submit", "text": "from the peer"}).encode() + b"\n")
        await writer.drain()
        await _eventually(lambda: ops == [{"op": "submit", "text": "from the peer"}])

        server.broadcast(json.dumps({"type": "turn.completed"}) + "\n")
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert json.loads(line)["type"] == "turn.completed"

        writer.close()  # clean detach
        await _eventually(lambda: server.peer_count == 0)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_owner_survives_a_peer_that_vanishes(tmp_path: Path) -> None:
    """A live session must never fail because an observer walked away."""
    server = await _server(tmp_path, [])
    try:
        endpoint = read_endpoint(tmp_path)
        assert endpoint is not None
        _reader, writer = await asyncio.open_unix_connection(endpoint.socket_path)
        writer.close()
        await _eventually(lambda: server.peer_count == 0)

        server.broadcast(json.dumps({"type": "runtime.event"}) + "\n")  # no raise
        assert server.listening
    finally:
        await server.stop()


def test_the_fanout_writer_is_transparent_without_peers() -> None:
    """No attachment, no behaviour change: exactly the bytes stdout had before."""
    sink = _Sink()
    writer = FanoutWriter(sink)
    writer.write('{"type":"session.started"}\n')
    writer.flush()
    assert sink.text == '{"type":"session.started"}\n'


@pytest.mark.asyncio
async def test_the_fanout_writer_never_skips_the_primary_client(
    tmp_path: Path,
) -> None:
    """Substituting one ``out`` handle is what makes attachment total: there is
    no emit site that could reach stdout but silently skip the peers, or the
    reverse."""
    sink = _Sink()
    server = await _server(tmp_path, [])
    try:
        endpoint = read_endpoint(tmp_path)
        assert endpoint is not None
        reader, writer = await asyncio.open_unix_connection(endpoint.socket_path)
        fanout = FanoutWriter(sink, server)
        await _eventually(lambda: server.peer_count == 1)

        fanout.write('{"type":"runtime.event"}\n')
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert json.loads(line)["type"] == "runtime.event"
        assert sink.text == '{"type":"runtime.event"}\n'
        writer.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_garbage_from_a_peer_is_ignored_not_fatal(tmp_path: Path) -> None:
    """A malformed line is a client bug, not a reason to drop a live session."""
    ops: list[dict[str, object]] = []
    server = await _server(tmp_path, ops)
    try:
        endpoint = read_endpoint(tmp_path)
        assert endpoint is not None
        _reader, writer = await asyncio.open_unix_connection(endpoint.socket_path)
        writer.write(b"not json\n[]\n" + json.dumps({"op": "interrupt"}).encode() + b"\n")
        await writer.drain()
        await _eventually(lambda: ops == [{"op": "interrupt"}])
        writer.close()
    finally:
        await server.stop()


def test_the_endpoint_filename_lives_beside_the_session(tmp_path: Path) -> None:
    assert endpoint_path(tmp_path) == tmp_path / ENDPOINT_FILENAME


async def _eventually(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    """Poll a condition to a deadline. The timeout is a failure bound; a passing
    run returns as soon as the condition holds."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")
