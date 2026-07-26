"""Bidirectional protocol server — the one new seam a Rust (or any external)
front-end needs.

``run --output-format jsonl`` already externalizes the normalized ``UIEvent``
stream (events OUT). ``serve`` adds the input direction (submissions IN:
``submit`` / ``approve`` / ``interrupt``) so an out-of-process UI can drive a
full *interactive* session — approvals answered across the boundary included.

It wraps :class:`~amplifier_app_newtui.kernel.runtime.RealRuntime` exactly as the
one-shot ``run`` path does (``start`` → ``submit`` → drain ``queue`` → ``cleanup``)
plus the runtime's :class:`~amplifier_app_newtui.kernel.approval.ApprovalBroker`
for the answer path. **amplifier-core is never touched** — this is a pure client
of the same Python API the interactive Textual app uses today; the only thing
that changes versus ``run`` is that stdin carries submissions back.

Wire (one JSON object per line):

  IN  (stdin)   {"op": "submit",    "text": "..."}
                {"op": "approve",   "ticket_id": "approval-3", "choice": "Allow once"}
                {"op": "interrupt"}
  OUT (stdout)  {"schema_version": 1, "sequence": N, "timestamp": T,
                 "type": "session.started" | "runtime.event" | "turn.completed"}
                {"schema_version": 1, "type": "approval.required",
                 "ticket_id": "approval-3", "prompt": "...", "options": [...]}

The ``runtime.event`` envelope is byte-identical to the ``run`` JSONL contract
(``JsonlRecords``); ``approval.required`` is the one record ``run`` cannot emit,
because a one-shot has no way to answer it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import redirect_stdout
from typing import IO, Any

from .jsonl import JsonlRecords
from .runtime import RealRuntime


def _emit_raw(out: IO[str], obj: dict[str, Any]) -> None:
    out.write(json.dumps(obj, default=str) + "\n")
    out.flush()


async def serve(
    bundle: str | None,
    *,
    mode: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    resume_id: str | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Run the interactive protocol loop until stdin closes. Returns an exit code."""
    runtime_kwargs: dict[str, Any] = {"bundle": bundle}
    if resume_id is not None:
        runtime_kwargs["resume_id"] = resume_id
    if model is not None:
        runtime_kwargs["model_override"] = model
    if provider is not None:
        runtime_kwargs["provider_override"] = provider
    if mode is not None:
        mode_value = mode
        runtime_kwargs["mode"] = lambda: mode_value
    runtime = RealRuntime(**runtime_kwargs)

    records = JsonlRecords()
    # Capture the real stdout BEFORE redirecting stray module prints to stderr —
    # exactly the discipline the ``run`` JSONL path uses so the protocol stream
    # stays clean while boot/module chatter still goes somewhere visible.
    out = stdout or sys.stdout
    source = stdin or sys.stdin
    loop = asyncio.get_running_loop()

    # stdin is blocking; read it on a thread and marshal ops onto the loop.
    ops: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def _read_stdin() -> None:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                op = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(op, dict):
                loop.call_soon_threadsafe(ops.put_nowait, op)
        loop.call_soon_threadsafe(ops.put_nowait, {"op": "__eof__"})

    threading.Thread(target=_read_stdin, daemon=True, name="serve-stdin").start()

    with redirect_stdout(sys.stderr):
        try:
            await runtime.start()
        except Exception as caught:  # noqa: BLE001 — boot failure is a structured terminal record
            _emit_raw(out, {"schema_version": 1, "type": "error",
                            "error": str(caught), "error_type": type(caught).__name__})
            return 1

        _emit_raw(out, records.session_started(
            session_id=runtime.session_id,
            bundle=runtime.bundle_name,
            model=runtime.model_name,
        ).model_dump(mode="json"))

        # Approvals: the broker owns the ticket id the UIEvent lacks. On every
        # queue change, surface the head ticket once (id + prompt + options) so
        # the UI can answer it by id. Fires in-loop (RealRuntime runs here, not
        # on a separate thread), so writing to stdout from the listener is safe.
        announced: set[str] = set()

        def _on_broker_change() -> None:
            head = runtime.broker.head
            if head is not None and head.ticket_id not in announced:
                announced.add(head.ticket_id)
                _emit_raw(out, {
                    "schema_version": 1,
                    "type": "approval.required",
                    "ticket_id": head.ticket_id,
                    "prompt": head.prompt,
                    "options": list(head.options),
                })

        runtime.broker.add_listener(_on_broker_change)

        # One pump drains normalized events for the whole session. The broker
        # listener owns approval_required (with its id), so it is filtered here.
        async def _pump() -> None:
            while True:
                event = await runtime.queue.get()
                if getattr(event, "kind", "") == "approval_required":
                    continue
                _emit_raw(out, records.runtime_event(event).model_dump(mode="json"))

        pump = asyncio.create_task(_pump())
        turn: asyncio.Task[str] | None = None

        try:
            while True:
                op = await ops.get()
                kind = op.get("op")
                if kind in ("__eof__", "quit"):
                    break
                if kind == "submit":
                    if turn is not None and not turn.done():
                        continue  # a turn is already running; ignore re-submit
                    text = str(op.get("text", ""))
                    turn = asyncio.create_task(_run_turn(runtime, out, text))
                elif kind == "approve":
                    ticket = op.get("ticket_id") or (
                        runtime.broker.head.ticket_id if runtime.broker.head else None
                    )
                    choice = str(op.get("choice", "Deny"))
                    if ticket:
                        try:
                            runtime.broker.answer(ticket, choice)
                        except KeyError:
                            pass  # already resolved / timed out
                elif kind == "interrupt":
                    asyncio.create_task(runtime.interrupt())  # noqa: RUF006 — fire-and-forget
        finally:
            pump.cancel()
            if turn is not None and not turn.done():
                turn.cancel()
            try:
                await runtime.cleanup()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
    return 0


async def _run_turn(runtime: RealRuntime, out: IO[str], text: str) -> str:
    """Execute one turn and emit its terminal record. Events stream via _pump."""
    try:
        response = await runtime.submit(text)
    except Exception as caught:  # noqa: BLE001 — a failed turn is a structured record, not a crash
        _emit_raw(out, {"schema_version": 1, "type": "error",
                        "session_id": runtime.session_id,
                        "error": str(caught), "error_type": type(caught).__name__})
        return ""
    _emit_raw(out, {"schema_version": 1, "type": "turn.completed",
                    "session_id": runtime.session_id, "response": response})
    return response
