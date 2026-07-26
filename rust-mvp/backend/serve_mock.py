#!/usr/bin/env python3
"""Protocol backend shim — the stand-in for a `serve` mode in *this repo's*
Python kernel. It owns the turn loop and speaks a bidirectional line protocol:

  submissions IN  (stdin, one JSON/line):  {"op":"submit","text":...}
                                           {"op":"approve","granted":bool}
                                           {"op":"interrupt"}
  events OUT      (stdout, one JSON/line):  the app's schema-v1 envelope —
     {"schema_version":1,"sequence":N,"timestamp":T,"type":"runtime.event",
      "event":{"kind":...}}  plus session.started.

In the real thing the turn loop body below is replaced by calls into
`amplifier_app_newtui.kernel` (RealRuntime.submit / ApprovalBroker / the
normalized UIEvent stream) — which wraps amplifier-core. **amplifier-core is
never modified**; this process is purely a client of its existing Python API,
exactly as the interactive Textual app is today. The Rust front-end, in turn, is
a pure client of *this* protocol.
"""
import json
import os
import sys
import time

_seq = 0
DELAY = float(os.environ.get("AMPLIFIER_MOCK_DELAY", "0.01"))


def _emit(obj: dict) -> None:
    global _seq
    _seq += 1
    obj = {"schema_version": 1, "sequence": _seq, "timestamp": time.time(), **obj}
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def event(kind: str, **fields) -> None:
    _emit({"type": "runtime.event", "event": {"kind": kind, **fields}})


def read_op() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def run_turn(prompt: str) -> None:
    """A scripted turn — the shape a real kernel turn produces, incl. an
    approval that PARKS until the UI answers over the protocol."""
    event("prompt_submit", text=prompt)
    time.sleep(DELAY)
    event("narration", text="Thinking…")
    time.sleep(DELAY)
    event("tool_line", summary="Read 3 files · ran 2 commands", ok=True)
    time.sleep(DELAY)

    # Park on approval: emit the request, then block for the UI's decision.
    event("approval_required", action="write_file src/health.py")
    decision = read_op() or {}
    granted = bool(decision.get("granted")) if decision.get("op") == "approve" else False

    if not granted:
        event("notice", text="Denied — continuing without the write")
        event("tool_line", summary="write_file src/health.py (denied)", ok=False)
        event("stream_start")
        for w in "Understood — I left the endpoint out.".split():
            event("stream_delta", text=w + " ")
            time.sleep(DELAY)
        event("stream_end")
        event("turn_complete", files=0, added=0, removed=0, tokens=640, cost=0.0041)
        return

    event("tool_line", summary="Changed 1 file  (+18/−0)", ok=True)
    time.sleep(DELAY)
    event("stream_start")
    answer = (
        "I've added a `/health` endpoint that returns 200 with a JSON status "
        "body, wired it into the router, and covered it with a test."
    )
    for w in answer.split():
        event("stream_delta", text=w + " ")
        time.sleep(DELAY)
    event("stream_end")
    event("turn_complete", files=1, added=18, removed=0, tokens=1240, cost=0.0123)


def main() -> None:
    _emit({
        "type": "session.started",
        "session_id": "core-01",
        "bundle": "newtui",
        "model": "claude-sonnet-4-5",
    })
    while True:
        op = read_op()
        if op is None:  # stdin closed → shut down
            break
        if op.get("op") == "submit":
            run_turn(str(op.get("text", "")))
        elif op.get("op") == "interrupt":
            event("notice", text="interrupted")
        # other ops ignored in the mock


if __name__ == "__main__":
    main()
