"""Shared real-runtime behavior probes for CLI, TUI adapter, and serve.

This extends B9's shared cross-surface fixture beyond identity/resume.  The
same fake provider/orchestrator/tool bundle is driven through each shipped
surface, and each helper returns both its outward normalized event sequence
and the durable ``ui-events.jsonl`` sequence written by the kernel tap.

The routing probe exercises the real :class:`SessionSpawner` and
Foundation preference resolver inside that mounted runtime: an unresolvable
first model glob must fall through to ``fake-routed`` for the child.  The
cancellation probe parks the fake orchestrator on amplifier-core's real
cooperative token, then interrupts it through each *bidirectional* owner:
the threaded TUI adapter and the serve protocol.  ``run --output-format
jsonl`` is deliberately one-shot (stdin is the prompt, not an op channel),
so live cancellation is not an applicable operation on that surface; serve
is the CLI's bidirectional thin-adapter/SDK wire.

This module deliberately defines helpers, not tests; the agreement assertion
lives beside the other B9 parity assertions in ``test_cli_tui_serve_parity``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import IO, cast

import pytest
from click.testing import CliRunner

from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.kernel.runtime import RealRuntime
from amplifier_app_tui.kernel.serve import serve_loop
from amplifier_app_tui.main import main
from amplifier_app_tui.ui.runtime_adapter import RealRuntimeAdapter

from .test_cli_tui_serve_identity_fixture import OFFLINE_BUNDLE
from .test_serve_offline import _Capture, _PipeStdin, _wait_until

PROMPT = "please write hello.txt with hi"
ROUTING_FALLBACK_PROMPT = "__B9_ROUTING_FALLBACK_PROBE__"
CANCELLATION_PROMPT = "__B9_LIVE_CANCELLATION_PROBE__"


@dataclass(frozen=True, slots=True)
class LifecycleObservation:
    event_kinds: tuple[str, ...]
    logged_kinds: tuple[str, ...]
    response: str


def _logged_kinds(session_dir: Path) -> tuple[str, ...]:
    path = session_dir / "ui-events.jsonl"
    assert path.is_file(), f"normalized event log was not persisted: {path}"
    return tuple(json.loads(line)["kind"] for line in path.read_text().splitlines())


async def _cli_turn(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
) -> LifecycleObservation:
    """Drive one prompt through the real ``run --output-format jsonl`` command."""
    monkeypatch.chdir(project)

    def _invoke():
        return CliRunner().invoke(
            main,
            ["run", "--bundle", OFFLINE_BUNDLE, "--output-format", "jsonl", prompt],
        )

    result = await asyncio.wait_for(asyncio.to_thread(_invoke), timeout=15)
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    started = next(record for record in records if record.get("type") == "session.started")
    event_kinds = tuple(
        record["event"]["kind"] for record in records if record.get("type") == "runtime.event"
    )
    completed = next(record for record in records if record.get("type") == "turn.completed")
    session_dir = SessionStore(project_dir=project).session_dir(started["session_id"])
    return LifecycleObservation(
        event_kinds,
        _logged_kinds(session_dir),
        str(completed["response"]),
    )


async def _tui_turn(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    *,
    interrupt: bool = False,
) -> LifecycleObservation:
    """Drive one prompt through the real threaded adapter the TUI owns."""
    monkeypatch.chdir(project)
    adapter = RealRuntimeAdapter(bundle=OFFLINE_BUNDLE)
    await adapter.start(lambda: None)
    events = []
    submit = asyncio.create_task(adapter.submit(prompt))
    interrupted = False
    deadline = asyncio.get_running_loop().time() + 15
    try:
        while not submit.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"TUI turn did not complete (interrupt={interrupt}); "
                    f"events={[event.kind for event in events]!r}"
                )
            try:
                events.append(
                    await asyncio.wait_for(adapter.queue.get(), timeout=min(0.25, remaining))
                )
            except TimeoutError:
                continue
            if interrupt and not interrupted and events[-1].kind == "prompt_submit":
                assert await adapter.interrupt(), "real TUI adapter rejected a live interrupt"
                interrupted = True
        await submit
        if interrupt:
            assert interrupted, "turn ended before the cancellation request could be sent"
        # ``run_coroutine_threadsafe`` may resolve the submit future just
        # before the app-loop processes the final call_soon_threadsafe queue
        # callback. Two loop turns make that ordering deterministic.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while not adapter.queue.empty():
            events.append(adapter.queue.get_nowait())
        assert adapter.session_dir is not None
        closing = next(event for event in events if event.kind == "prompt_complete")
        return LifecycleObservation(
            tuple(event.kind for event in events),
            _logged_kinds(adapter.session_dir),
            str(closing.response),
        )
    finally:
        if not submit.done():
            submit.cancel()
        adapter.shutdown()


async def _serve_turn(
    project: Path,
    prompt: str,
    *,
    interrupt: bool = False,
) -> LifecycleObservation:
    """Drive one prompt through the real JSONL serve loop."""
    # Match the no-app adapter and one-shot command default: Auto does not
    # park on an approval ticket, so the protocol can complete unattended.
    runtime = RealRuntime(bundle=OFFLINE_BUNDLE, project_dir=project, mode=lambda: "auto")
    await runtime.start()
    session_id = runtime.session_id
    store = runtime._store
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))
    )
    stdin.feed({"op": "submit", "text": prompt})
    try:
        if interrupt:
            await _wait_until(
                lambda: any(
                    record.get("type") == "runtime.event"
                    and record.get("event", {}).get("kind") == "prompt_submit"
                    for record in out.lines
                ),
                timeout=10,
            )
            stdin.feed({"op": "interrupt"})
        await _wait_until(lambda: out.find("turn.completed") is not None, timeout=10)
    except AssertionError as error:
        stdin.close()
        await asyncio.wait_for(server, timeout=10)
        raise AssertionError(f"serve turn did not complete; records={out.lines!r}") from error
    stdin.close()
    assert await asyncio.wait_for(server, timeout=10) == 0
    event_kinds = tuple(
        record["event"]["kind"] for record in out.lines if record.get("type") == "runtime.event"
    )
    assert store is not None
    session_dir = store.session_dir(session_id)
    completed = out.find("turn.completed")
    assert completed is not None
    return LifecycleObservation(
        event_kinds,
        _logged_kinds(session_dir),
        str(completed["response"]),
    )


async def cli_lifecycle(project: Path, monkeypatch: pytest.MonkeyPatch) -> LifecycleObservation:
    return await _cli_turn(project, monkeypatch, PROMPT)


async def tui_lifecycle(project: Path, monkeypatch: pytest.MonkeyPatch) -> LifecycleObservation:
    return await _tui_turn(project, monkeypatch, PROMPT)


async def serve_lifecycle(project: Path) -> LifecycleObservation:
    return await _serve_turn(project, PROMPT)


async def cli_routing_fallback(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> LifecycleObservation:
    return await _cli_turn(project, monkeypatch, ROUTING_FALLBACK_PROMPT)


async def tui_routing_fallback(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> LifecycleObservation:
    return await _tui_turn(project, monkeypatch, ROUTING_FALLBACK_PROMPT)


async def serve_routing_fallback(project: Path) -> LifecycleObservation:
    return await _serve_turn(project, ROUTING_FALLBACK_PROMPT)


async def tui_live_cancellation(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> LifecycleObservation:
    return await _tui_turn(project, monkeypatch, CANCELLATION_PROMPT, interrupt=True)


async def serve_live_cancellation(project: Path) -> LifecycleObservation:
    return await _serve_turn(project, CANCELLATION_PROMPT, interrupt=True)


__all__ = [
    "LifecycleObservation",
    "CANCELLATION_PROMPT",
    "ROUTING_FALLBACK_PROMPT",
    "cli_lifecycle",
    "cli_routing_fallback",
    "serve_live_cancellation",
    "serve_lifecycle",
    "serve_routing_fallback",
    "tui_live_cancellation",
    "tui_lifecycle",
    "tui_routing_fallback",
]
