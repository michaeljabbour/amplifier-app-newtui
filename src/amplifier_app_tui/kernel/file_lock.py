"""Shared inter-process file-lock idiom.

Originally written for :mod:`kernel.session_control` (the session control
plane's ``control.json``); extracted here (B7 gap-closure pass) so a SECOND
durable-state writer (:mod:`kernel.attention_store`) reuses the exact same
lock mechanism instead of inventing its own -- one idiom, every durable
kernel-side writer builds on it.

An ``O_EXCL`` create IS the lock -- atomic on every filesystem this app
targets, no third-party dependency. A lock file older than ``stale_after``
is broken (a crashed holder must never wedge a session, or a durable-state
write, forever -- the same spirit as session_control's lease expiry). A
caller that cannot acquire within ``timeout`` proceeds anyway rather than
hang: a write hanging forever is a worse failure than a rare
last-writer-wins on the state file. Callers with tighter latency budgets
(a UI-thread write that must never be felt as lag) should pass a much
shorter ``timeout`` than the control plane's own default.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path


@contextmanager
def locked(target: Path, *, timeout: float = 5.0, stale_after: float = 30.0) -> Iterator[bool]:
    """Best-effort inter-process critical section around *target*.

    ``target`` need not exist; only a sibling ``<name>.lock`` marker file is
    created/removed. See the module docstring for the stale-break /
    timeout-proceeds-anyway contract -- the yielded boolean reports whether
    the marker was actually acquired. Callers whose own writes are already
    safe under a last-writer-wins race may ignore it and treat the lock as a
    contention reducer. Read-modify-write callers must check it and fail
    closed rather than execute an unlocked mutation.
    """
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue
            if age > stale_after:
                with suppress(OSError):
                    lock.unlink()
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)
            continue
        except OSError:
            break
        os.close(handle)
        acquired = True
        break
    try:
        yield acquired
    finally:
        if acquired:
            with suppress(OSError):
                lock.unlink()


__all__ = ["locked"]
