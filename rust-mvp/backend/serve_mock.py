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


def run_turn(prompt: str) -> str:
    """A scripted turn in the REAL event vocabulary (kernel/events.py kinds),
    incl. an approval that PARKS until the UI answers over the protocol. This is
    exactly what kernel/serve.py emits from a live RealRuntime turn."""
    event("prompt_submit", prompt=prompt, mode="chat")
    time.sleep(DELAY)
    event("notification", message="Thinking…", level="info")
    time.sleep(DELAY)
    event("tool_post", tool_name="read_files", result={"summary": "Read 3 files · ran 2 commands"})
    time.sleep(DELAY)

    # Park on approval: emit the ticket-bearing record (the one `run` can't), then
    # block for the UI's decision routed back by ticket id.
    _emit({"schema_version": 1, "type": "approval.required",
           "ticket_id": "approval-1", "prompt": "write_file src/health.py",
           "options": ["Allow once", "Allow always", "Deny"]})
    decision = read_op() or {}
    granted = str(decision.get("choice", "")).startswith("Allow") if decision.get("op") == "approve" else False

    if not granted:
        event("notification", message="Denied — continuing without the write", level="warn")
        event("tool_error", tool_name="write_file", error_type="denied")
        response = "Understood — I left the endpoint out."
        event("stream_block_start", block_type="text")
        for w in response.split():
            event("stream_block_delta", text=w + " ")
            time.sleep(DELAY)
        event("stream_block_end")
        event("prompt_complete", response=response, files_changed=0, diffstat="")
        return response

    event("tool_post", tool_name="write_file", result={"summary": "Changed 1 file  (+18/−0)"})
    time.sleep(DELAY)
    response = (
        "I've added a `/health` endpoint that returns 200 with a JSON status "
        "body, wired it into the router, and covered it with a test."
    )
    event("stream_block_start", block_type="text")
    for w in response.split():
        event("stream_block_delta", text=w + " ")
        time.sleep(DELAY)
    event("stream_block_end")
    event("prompt_complete", response=response, files_changed=1, diffstat="+18/−0")
    return response


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
            response = run_turn(str(op.get("text", "")))
            _emit({"schema_version": 1, "type": "turn.completed",
                   "session_id": "core-01", "response": response})
        elif op.get("op") == "interrupt":
            event("notice", text="interrupted")
        # other ops ignored in the mock


if __name__ == "__main__":
    main()
