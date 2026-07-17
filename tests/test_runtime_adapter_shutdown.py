"""RealRuntimeAdapter.shutdown() must survive an already-closed runtime loop.

A boot failure returns the runtime thread body early, so ``asyncio.run``
closes the runtime loop before the app's ``on_unmount`` fires. shutdown()
used to call ``call_soon_threadsafe`` on that closed loop and raise
``RuntimeError: Event loop is closed`` — which masked the real boot error
behind a teardown traceback (user report). It must now be a no-op.
"""

from __future__ import annotations

import asyncio

from amplifier_app_newtui.ui.runtime_adapter import RealRuntimeAdapter


def test_shutdown_is_noop_when_runtime_loop_already_closed() -> None:
    adapter = RealRuntimeAdapter(bundle="offline")
    loop = asyncio.new_event_loop()
    adapter._stop = asyncio.Event()  # its real loop is irrelevant; loop is closed
    adapter._runtime_loop = loop
    loop.close()
    assert loop.is_closed()

    adapter.shutdown()  # must not raise RuntimeError('Event loop is closed')


def test_shutdown_is_noop_before_boot() -> None:
    # No thread/loop yet (adapter constructed but start() never ran).
    RealRuntimeAdapter(bundle="offline").shutdown()
