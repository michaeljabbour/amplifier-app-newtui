"""A real, separately-spawned process that serves one session's control plane.

``tests/test_session_control_multiprocess.py`` runs this under ``sys.executable``
so two genuine OS processes contend over ONE session directory -- the situation
AC3/AC5 are actually about, and the one an in-process test can only imitate.
Everything that decides the outcome is the shipped code: the real
:func:`~amplifier_app_tui.kernel.serve.serve_loop`, the real
:class:`~amplifier_app_tui.kernel.session_control.SessionControl` over a real
``control.json`` guarded by the real ``O_EXCL`` file lock, and the real
:class:`~amplifier_app_tui.kernel.persistence.SessionStore` ledger.

Two things are stand-ins, both deliberately:

*The runtime.* A ``RealRuntime`` needs a provider, credentials and a network;
the suite is offline by contract. This fake exposes the surface ``serve_loop``
touches and appends each submission to the durable ledger exactly as the real
one does -- so "did that write actually land?" is answered by reading
``ui-events.jsonl``, not by trusting a mock.

*The clock.* ``--clock`` points the control plane's ``now()`` at a file the
test writes. Lease expiry is then something the test *causes* rather than
something it waits out, which is the difference between a deterministic CI test
and a sleep racing a real timer.

Modes:

``serve``   own the session: boot the fake runtime and run the protocol loop.
``attach``  join a session another process already owns, over its live attach
            socket -- no runtime, no writer, the Gap-3 client path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import IO, Any, cast

from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.kernel.serve import serve_loop
from amplifier_app_tui.kernel.session_attach import live_endpoint, run_attach_client
from amplifier_app_tui.kernel.session_authz import AUTHZ_FILENAME, policy_for
from amplifier_app_tui.kernel.session_control import ANONYMOUS, Actor, SessionControl
from amplifier_app_tui.model.queues import SteeringQueue


class _FileClock:
    """A clock the test advances by writing a float to a file.

    Falls back to the last value it successfully read, so a partially-written
    file (the test and this process race on an ordinary write) yields a stale
    reading rather than an exception or a jump backwards.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._last = 1_000_000.0

    def __call__(self) -> float:
        if self.path is None:
            import time

            return time.time()
        try:
            self._last = float(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
        return self._last


class _NoBroker:
    head = None

    def add_listener(self, listener: Any) -> None:
        del listener


class _FakeRuntime:
    """The slice of ``RealRuntime`` that ``serve_loop`` actually touches."""

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.bundle_name = "tui"
        self.model_name = "test-provider/test-model"
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.broker = _NoBroker()
        self.steering = SteeringQueue()
        self.interrupts = 0

    async def submit(self, text: str) -> str:
        # Append exactly as RealRuntime does, so the ledger is the honest
        # record of which process's write actually landed.
        self.store.append_event(
            self.session_id,
            {"kind": "prompt_submit", "session_id": self.session_id, "ts": 1.0, "text": text},
        )
        return f"ok:{text}"

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def cleanup(self) -> None:
        return None


async def _serve(args: argparse.Namespace) -> int:
    store = SessionStore(base_dir=Path(args.store))
    runtime = _FakeRuntime(store, args.session)
    clock = _FileClock(Path(args.clock) if args.clock else None)
    session_dir = store.session_dir(args.session)
    default_actor = Actor(id=args.actor, kind=args.kind) if args.actor else ANONYMOUS

    def _control() -> SessionControl:
        return SessionControl(
            session_dir,
            args.session,
            now=clock,
            default_actor=default_actor,
            policy=policy_for(store.base_dir / AUTHZ_FILENAME, now=clock),
        )

    return await serve_loop(
        cast("Any", runtime),
        source=sys.stdin,
        out=sys.stdout,
        default_actor=default_actor,
        attachable=args.attachable,
        control_factory=_control,
    )


async def _attach(args: argparse.Namespace) -> int:
    store = SessionStore(base_dir=Path(args.store))
    endpoint = live_endpoint(store.session_dir(args.session))
    if endpoint is None:
        sys.stdout.write(json.dumps({"type": "attach.unavailable"}) + "\n")
        sys.stdout.flush()
        return 1
    return await run_attach_client(endpoint, source=sys.stdin, out=cast("IO[str]", sys.stdout))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, help="SessionStore base dir.")
    parser.add_argument("--session", required=True, help="Session id to serve/join.")
    parser.add_argument("--mode", choices=("serve", "attach"), default="serve")
    parser.add_argument("--clock", default=None, help="File holding the current virtual time.")
    parser.add_argument("--actor", default=None, help="Default actor id.")
    parser.add_argument("--kind", default="automation", help="Default actor kind.")
    parser.add_argument("--attachable", action="store_true", help="Publish the attach endpoint.")
    args = parser.parse_args(argv)
    runner = _attach if args.mode == "attach" else _serve
    return asyncio.run(runner(args))


if __name__ == "__main__":
    raise SystemExit(main())
