#!/usr/bin/env python3
"""Protocol backend shim — the stand-in for a `serve` mode in *this repo's*
Python kernel. It owns the turn loop and speaks a bidirectional line protocol:

  submissions IN  (stdin, one JSON/line):  {"op":"submit","text":...}
                                           {"op":"steer","text":...}
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


def boot_progress(action: str, detail: str) -> None:
    """The exact boot-phase record kernel/serve.py emits from RealRuntime's
    ``on_progress`` (no sequence/timestamp — it precedes the session)."""
    sys.stdout.write(json.dumps({"schema_version": 1, "type": "boot.progress",
                                 "action": action, "detail": detail}) + "\n")
    sys.stdout.flush()


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


def usage_event(event_id: str, **fields) -> None:
    """One ``provider_response_usage`` in the exact shape kernel/serve.py puts
    on the wire (pydantic ``model_dump(mode="json")`` of ProviderResponseUsage:
    full envelope, ``cost_usd`` null unless the provider reported one)."""
    event(
        "provider_response_usage",
        event_id=event_id,
        session_id="core-01",
        parent_id=None,
        ts=time.time(),
        input_tokens=fields.get("input_tokens", 0),
        output_tokens=fields.get("output_tokens", 0),
        cache_read=fields.get("cache_read", 0),
        cache_write=fields.get("cache_write", 0),
        model=fields.get("model", "claude-sonnet-4-5"),
        cost_usd=fields.get("cost_usd"),
    )


_steers: list[str] = []
"""Mid-turn ``steer`` ops received while the turn was parked — the mock's
stand-in for RealRuntime's SteeringQueue (kernel/serve.py routes the op
into ``runtime.steering``)."""


def apply_steers() -> list[str]:
    """Step boundary: apply queued mid-turn steers with the exact narration
    shape kernel/runtime.py ``RealRuntime._steer_applied`` puts on the wire
    (a durable ContentBlockEnd text block with the narration role marker)."""
    applied: list[str] = []
    while _steers:
        text = _steers.pop(0)
        applied.append(text)
        event("content_block_end", session_id="core-01", block_type="text",
              block={"type": "text", "text": f"Applying steer: {text}",
                     "demo_role": "narration"})
    return applied


def tool_call(index: int, tool: str, tool_input: dict, result: dict | None = None) -> None:
    """One tool:pre/tool:post pair, correlated by tool_call_id exactly as the
    real runtime emits them (the reducer folds them into its burst digest)."""
    call_id = f"call-{index}"
    event("tool_pre", tool_name=tool, tool_call_id=call_id, tool_input=tool_input)
    time.sleep(DELAY)
    event("tool_post", tool_name=tool, tool_call_id=call_id,
          tool_input=tool_input, result=result or {"status": "ok"})


def run_turn(prompt: str) -> str:
    """A scripted turn in the REAL event vocabulary (kernel/events.py kinds),
    incl. an approval that PARKS until the UI answers over the protocol. This is
    exactly what kernel/serve.py emits from a live RealRuntime turn."""
    event("prompt_submit", prompt=prompt, mode="chat")
    time.sleep(DELAY)
    event("notification", message="Thinking…", level="info")
    time.sleep(DELAY)
    # 3 file reads + 2 shell commands → digest "Read 3 files · ran 2 shell commands".
    tool_call(1, "read_file", {"path": "src/app.py"})
    tool_call(2, "read_file", {"path": "src/router.py"})
    tool_call(3, "read_file", {"path": "tests/test_app.py"})
    tool_call(4, "bash", {"command": "pytest -q"})
    tool_call(5, "bash", {"command": "ruff check ."})
    time.sleep(DELAY)
    # First provider response of the turn (the planning/tool round).
    usage_event("ev-usage-1", input_tokens=1200, output_tokens=340,
                cache_read=800, cache_write=100)
    time.sleep(DELAY)

    # Park on approval: emit the ticket-bearing record (the one `run` can't), then
    # block for the UI's decision routed back by ticket id. A `steer` op that
    # arrives while parked queues, exactly as kernel/serve.py enqueues it into
    # RealRuntime.steering while a turn is in flight.
    _emit({"schema_version": 1, "type": "approval.required",
           "ticket_id": "approval-1", "prompt": "write_file src/health.py",
           "options": ["Allow once", "Allow always", "Deny"]})
    granted = False
    while True:
        decision = read_op()
        if decision is None:
            break  # stdin closed mid-park — fail closed (deny)
        if decision.get("op") == "steer":
            _steers.append(str(decision.get("text", "")))
            continue
        if decision.get("op") == "approve":
            granted = str(decision.get("choice", "")).startswith("Allow")
        break

    # The next step boundary after the park: queued steers apply here (the
    # StepBoundaryBridge consumes one per provider:request in the real thing).
    steered = apply_steers()
    steer_suffix = (
        " I also applied your steer: " + "; ".join(steered) + "." if steered else ""
    )

    if not granted:
        event("notification", message="Denied — continuing without the write", level="warn")
        # The denied write is a tool:pre/post pair whose post carries the
        # denial (status/reason/continuation) — the durable ⊘ blocked line.
        tool_call(6, "write_file", {"path": "src/health.py"},
                  {"status": "denied", "reason": "denied by user",
                   "continuation": "continuing without the write"})
        response = "Understood — I left the endpoint out." + steer_suffix
        event("stream_block_start", block_type="text")
        for w in response.split():
            event("stream_block_delta", text=w + " ")
            time.sleep(DELAY)
        event("stream_block_end")
        usage_event("ev-usage-denied", input_tokens=600, output_tokens=80)
        event("prompt_complete", response=response, files_changed=0, diffstat="")
        return response

    tool_call(6, "write_file",
              {"path": "src/health.py",
               "content": "def health():\n    return {\"status\": \"ok\"}\n"})
    time.sleep(DELAY)
    response = (
        "I've added a `/health` endpoint that returns 200 with a JSON status "
        "body, wired it into the router, and covered it with a test." + steer_suffix
    )
    event("stream_block_start", block_type="text")
    for w in response.split():
        event("stream_block_delta", text=w + " ")
        time.sleep(DELAY)
    event("stream_block_end")
    # Final provider response (the streamed answer round).
    usage_event("ev-usage-2", input_tokens=900, output_tokens=120)
    event("prompt_complete", response=response, files_changed=1, diffstat="+18/−0")
    return response


def main() -> None:
    # Boot phases land BEFORE session.started, exactly as kernel/serve.py's
    # on_progress reports RealRuntime's start (module names on the splash
    # while amplifier loads, instead of a blank screen).
    boot_progress("loading", "newtui")
    boot_progress("installing_package", "tool-bash")
    boot_progress("creating", "session")
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
