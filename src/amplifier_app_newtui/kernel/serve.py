"""Bidirectional protocol server — the one new seam a Rust (or any external)
front-end needs.

``run --output-format jsonl`` already externalizes the normalized ``UIEvent``
stream (events OUT). ``serve`` adds the input direction (submissions IN:
``submit`` / ``steer`` / ``approve`` / ``interrupt``) so an out-of-process UI
can drive a full *interactive* session — approvals answered across the
boundary and mid-turn steering included.

It wraps :class:`~amplifier_app_newtui.kernel.runtime.RealRuntime` exactly as the
one-shot ``run`` path does (``start`` → ``submit`` → drain ``queue`` → ``cleanup``)
plus the runtime's :class:`~amplifier_app_newtui.kernel.approval.ApprovalBroker`
for the answer path. **amplifier-core is never touched** — this is a pure client
of the same Python API the interactive Textual app uses today; the only thing
that changes versus ``run`` is that stdin carries submissions back.

Wire (one JSON object per line):

  IN  (stdin)   {"op": "submit",    "text": "..."}
                {"op": "steer",     "text": "..."}   (mid-turn course correction)
                {"op": "approve",   "ticket_id": "approval-3", "choice": "Allow once"}
                {"op": "decision",  "decision_id": "decision-1", "answer": "Allow once"}
                {"op": "interrupt"}
                {"op": "effort.get"}                  (read the reasoning-effort tier)
                {"op": "effort.set", "effort": "high"} (set it; accepts "max"->"xhigh")
                {"op": "effort.cycle"}                 (advance one tier, wraps xhigh->none)
                {"op": "tag.add",   "session_id": "<id?>", "tags": ["urgent"]}   (session tags; additive)
                {"op": "tag.remove","session_id": "<id?>", "tags": ["urgent"]}
                {"op": "tag.list",  "session_id": "<id?>"}
                {"op": "tag.sessions", "tag": "urgent"}
  OUT (stdout)  {"schema_version": 1, "type": "boot.progress",
                 "action": "preparing", "detail": "newtui"}   (before session.started)
                {"schema_version": 1, "sequence": N, "timestamp": T,
                 "type": "session.started" | "runtime.event" | "turn.completed"}
                {"schema_version": 1, "type": "approval.required",
                 "ticket_id": "approval-3", "prompt": "...", "options": [...]}
                {"schema_version": 1, "type": "effort.state",
                 "effort": "high" | null, "levels": ["none", ..., "xhigh"]}
                 (reply to every effort.* op; set/cycle add "ok"/"detail")
                {"schema_version": 1, "type": "tag.updated", "op": "tag.add",
                 "ok": true, "session_id": "...", "tags": [...], "changed": [...], "rejected": [...]}
                {"schema_version": 1, "type": "tag.list", "op": "tag.list",
                 "ok": true, "session_id": "...", "tags": [...]}
                {"schema_version": 1, "type": "tag.sessions", "op": "tag.sessions",
                 "ok": true, "tag": "urgent", "sessions": [{"session_id": "...", "name": "...", "tags": [...]}]}

The ``runtime.event`` envelope is byte-identical to the ``run`` JSONL contract
(``JsonlRecords``); ``approval.required`` is the one record ``run`` cannot emit,
because a one-shot has no way to answer it. The ``effort.*`` ops expose the
in-session reasoning-effort tier (the ``/effort`` command's plumbing:
``RealRuntime.get_effort`` / ``set_effort`` -> ``session_ops``) so an
out-of-process UI can read, set, and cycle a dimension orthogonal to the model
mid-session. The post-op ``effort.state`` IS the change notification (serve is
single-client, so the echoed state is authoritative). Cycle lives server-side
to keep the canonical ring order in one home; a client may equally compose it
from ``effort.get`` + ``effort.set``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import redirect_stdout
from typing import IO, Any

from . import session_manager
from .jsonl import JsonlRecords
from .prompt_history import PromptHistoryStore
from .runtime import RealRuntime
from .session_ops import EFFORT_LEVELS


def _emit_raw(out: IO[str], obj: dict[str, Any]) -> None:
    out.write(json.dumps(obj, default=str) + "\n")
    out.flush()


def _next_effort(current: str | None) -> str:
    """The next reasoning-effort tier in the canonical ring, wrapping ``xhigh`` ->
    ``none``.

    Mirrors the donor ``variant.cycle`` entry/advance rules within the tiers
    amplifier's existing ``set_effort`` can actually reach: an unset/unknown
    current enters the ring at the first tier; otherwise advance one and wrap.
    There is no Default(unset) slot because ``session_ops.set_effort`` has no
    clear path (documented divergence -- see ``.ai/oc_donor.md``)."""
    if current is None or current not in EFFORT_LEVELS:
        return EFFORT_LEVELS[0]
    return EFFORT_LEVELS[(EFFORT_LEVELS.index(current) + 1) % len(EFFORT_LEVELS)]


async def _emit_effort_state(
    runtime: RealRuntime,
    out: IO[str],
    *,
    ok: bool | None = None,
    detail: str | None = None,
) -> None:
    """Emit the current reasoning-effort tier as an ``effort.state`` record.

    The reply to every ``effort.*`` op and the change notification itself
    (serve is single-client, so the post-op state is authoritative). ``levels``
    is the canonical ring order the client cycles through; ``ok``/``detail`` are
    attached only for mutating ops (set/cycle) so a client can surface the same
    success/error notice the in-process ``/effort`` command shows."""
    record: dict[str, Any] = {
        "schema_version": 1,
        "type": "effort.state",
        "effort": await runtime.get_effort(),
        "levels": list(EFFORT_LEVELS),
    }
    if ok is not None:
        record["ok"] = ok
    if detail is not None:
        record["detail"] = detail
    _emit_raw(out, record)


# -- session tags (additive metadata ops) -----------------------------------
# tag CRUD is pure session *metadata* (kernel/session_manager), never
# amplifier-core, so each op is one synchronous request->response over the
# SessionStore with no turn involved. Strictly additive to the wire.

_TAG_OPS = frozenset({"tag.add", "tag.remove", "tag.list", "tag.sessions"})


def _serve_store(runtime: Any) -> Any:
    """The SessionStore the tag ops read/write.

    Prefer the runtime's own store (bound to the right project); fall back to a
    default-constructed store from its project_dir. Built lazily so runtimes
    that never receive a tag op (every existing serve test) construct nothing.
    """
    store = getattr(runtime, "store", None)
    if store is not None:
        return store
    from .persistence import SessionStore

    return SessionStore(project_dir=getattr(runtime, "project_dir", None))


def _tag_inputs(op: dict[str, Any]) -> list[str]:
    """Read the ``tags`` list (or a singular ``tag``) from a tag-op request."""
    raw = op.get("tags")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    single = op.get("tag")
    if isinstance(single, str):
        return [single]
    return []


def _handle_tag_op(runtime: Any, op: dict[str, Any]) -> dict[str, Any]:
    """Service one synchronous tag op; return the response record to emit.

    ``tag.sessions`` filters the whole store by one tag. ``tag.add`` /
    ``tag.remove`` / ``tag.list`` target a single session, defaulting to the
    LIVE session (``runtime.session_id``) when the client omits ``session_id``;
    the live session persists lazily, so it is materialized first (mirroring
    ``/rename``). An explicitly-supplied id is resolved as a prefix and is
    NEVER created — an unknown id round-trips ``ok:false`` with an error.
    """
    kind = str(op.get("op", ""))
    store = _serve_store(runtime)

    if kind == "tag.sessions":
        tag = str(op.get("tag", ""))
        summaries = session_manager.sessions_by_tag(store, tag)
        return {
            "schema_version": 1,
            "type": "tag.sessions",
            "op": "tag.sessions",
            "ok": True,
            "tag": session_manager.normalize_tag(tag) or tag,
            "sessions": [
                {"session_id": s.session_id, "name": s.name, "tags": list(s.tags)}
                for s in summaries
            ],
        }

    supplied = op.get("session_id")
    session_id = str(supplied or getattr(runtime, "session_id", ""))
    if not supplied:
        bundle = str(getattr(runtime, "bundle_name", "") or "unknown")
        session_manager.ensure_session_dir(store, session_id, bundle=bundle)

    if kind == "tag.list":
        listed = session_manager.get_tags(store, session_id)
        record: dict[str, Any] = {
            "schema_version": 1,
            "type": "tag.list",
            "op": "tag.list",
            "ok": listed.ok,
            "session_id": listed.session_id,
            "tags": list(listed.tags),
        }
        if not listed.ok:
            record["error"] = listed.error
        return record

    if kind == "tag.add":
        outcome = session_manager.add_tags(store, session_id, _tag_inputs(op))
    else:  # tag.remove
        outcome = session_manager.remove_tags(store, session_id, _tag_inputs(op))
    record = {
        "schema_version": 1,
        "type": "tag.updated",
        "op": kind,
        "ok": outcome.ok,
        "session_id": outcome.session_id,
        "tags": list(outcome.tags),
        "changed": list(outcome.changed),
        "rejected": list(outcome.rejected),
    }
    if not outcome.ok:
        record["error"] = outcome.error
    return record


DEFAULT_HISTORY_QUERY_LIMIT = 10
"""Default cap for a ``history.query`` with no explicit ``limit``."""


def _history_list_record(runtime: RealRuntime, op: dict[str, Any]) -> dict[str, Any]:
    """Build the ``history.list`` reply to a ``history.query`` op.

    Additive READ path: frecency-ranks THIS project's prompt history
    (``kernel/frecency.py`` over ``PromptHistoryStore``) for an
    out-of-process autocomplete/recall UI -- a prompt used often *and*
    recently outranks a once-used more recent one. It needs no live turn,
    so it answers even mid-turn. Best-effort: any failure returns an empty
    list rather than breaking the protocol loop -- prompt history is never
    load-bearing (mirrors the store's own swallow-and-continue contract).
    It does NOT touch the composer up-ring default (that stays chronological
    for the client lane to build on).
    """
    prefix = str(op.get("prefix", ""))
    try:
        limit = int(op.get("limit", DEFAULT_HISTORY_QUERY_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_HISTORY_QUERY_LIMIT
    entries: list[dict[str, Any]] = []
    try:
        store = PromptHistoryStore(project_dir=getattr(runtime, "project_dir", None))
        entries = [
            {
                "text": ranked.text,
                "score": round(ranked.score, 6),
                "frequency": ranked.frequency,
                "age": ranked.age,
            }
            for ranked in store.ranked_history(prefix, limit=limit)
        ]
    except Exception:  # noqa: BLE001 -- history recall is best-effort, never fatal
        entries = []
    return {
        "schema_version": 1,
        "type": "history.list",
        "prefix": prefix,
        "entries": entries,
    }


async def serve(
    bundle: str | None,
    *,
    mode: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    resume_id: str | None = None,
    project_dir: Any = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Boot a RealRuntime and run the interactive protocol loop on stdio.

    Returns an exit code. Construction lives here; the loop lives in
    :func:`serve_loop`, which a test can drive against a pre-started runtime.
    """
    # Capture the real stdout BEFORE redirecting stray module prints to stderr —
    # exactly the discipline the ``run`` JSONL path uses so the protocol stream
    # stays clean while boot/module chatter still goes somewhere visible.
    out = stdout or sys.stdout
    source = stdin or sys.stdin

    def _boot_progress(action: str, detail: str) -> None:
        # Boot-phase feedback on the protocol stream: module prepare can run
        # for minutes and ``session.started`` is the first record otherwise —
        # a protocol client would show a blank splash the whole time. Same
        # ``(action, detail)`` phases the Textual app paints via
        # ``RealRuntime(on_progress=...)``. Fires in-loop (resolve_config /
        # foundation's prepare call the callback synchronously inside
        # ``runtime.start()``), so a plain emit is safe.
        _emit_raw(
            out, {"schema_version": 1, "type": "boot.progress", "action": action, "detail": detail}
        )

    runtime_kwargs: dict[str, Any] = {"bundle": bundle, "on_progress": _boot_progress}
    if resume_id is not None:
        runtime_kwargs["resume_id"] = resume_id
    if model is not None:
        runtime_kwargs["model_override"] = model
    if provider is not None:
        runtime_kwargs["provider_override"] = provider
    if project_dir is not None:
        runtime_kwargs["project_dir"] = project_dir
    if mode is not None:
        mode_value = mode
        runtime_kwargs["mode"] = lambda: mode_value
    runtime = RealRuntime(**runtime_kwargs)

    with redirect_stdout(sys.stderr):
        try:
            await runtime.start()
        except Exception as caught:  # noqa: BLE001 — boot failure is a structured terminal record
            _emit_raw(
                out,
                {
                    "schema_version": 1,
                    "type": "error",
                    "error": str(caught),
                    "error_type": type(caught).__name__,
                },
            )
            return 1
        return await serve_loop(runtime, source=source, out=out)


async def serve_loop(runtime: RealRuntime, *, source: IO[str], out: IO[str]) -> int:
    """The protocol loop over an already-started ``runtime``: emit session start,
    stream events, and service ``submit``/``steer``/``approve``/``interrupt``
    submissions until ``source`` closes. Split out so tests drive it with a
    fake-module runtime (real broker, no key/network)."""
    records = JsonlRecords()
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

    _emit_raw(
        out,
        records.session_started(
            session_id=runtime.session_id,
            bundle=runtime.bundle_name,
            model=runtime.model_name,
        ).model_dump(mode="json"),
    )

    # Approvals: the broker owns the ticket id the UIEvent lacks. On every queue
    # change, surface the head ticket once (id + prompt + options) so the UI can
    # answer it by id. Fires in-loop (RealRuntime runs here, not on a separate
    # thread), so writing to stdout from the listener is safe.
    announced: set[str] = set()

    def _on_broker_change() -> None:
        head = runtime.broker.head
        if head is not None and head.ticket_id not in announced:
            announced.add(head.ticket_id)
            _emit_raw(
                out,
                {
                    "schema_version": 1,
                    "type": "approval.required",
                    "ticket_id": head.ticket_id,
                    "prompt": head.prompt,
                    "options": list(head.options),
                },
            )

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
            elif kind == "steer":
                # Mid-turn course correction (additive op). Lands in the SAME
                # bounded queue the in-process TUI shares with the runtime
                # (RealRuntime.steering): the StepBoundaryBridge consumes one
                # steer per provider:request and the runtime itself narrates
                # the application as a durable "Applying steer: …" block
                # (kernel/runtime.py _steer_applied) — nothing new is emitted
                # on stdout here. Bound/empty violations are dropped silently:
                # a protocol client enforces the same SteeringQueue limits
                # locally, so a ValueError here is a client already told.
                try:
                    runtime.steering.enqueue(str(op.get("text", "")))
                except ValueError:
                    pass
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
            elif kind == "decision":
                # Answer a DEFERRED needs-you decision (additive op). A
                # deferral has NO live broker ticket — governance parked the
                # item straight into NeedsYouQueue and deny-and-continued,
                # so {"op":"approve"} can never reach it. This mirrors the
                # in-process TUI's apply_decision: answer the SAME kernel
                # queue; the StepBoundaryBridge injects the answer at the
                # next provider:request (kernel/steering.py). Unknown ids /
                # already-answered decisions are a client already told —
                # dropped silently like the steer arm's bound violations.
                decision_id = str(op.get("decision_id", ""))
                answer = str(op.get("answer", ""))
                if decision_id and answer:
                    try:
                        runtime.needs_you.answer(decision_id, answer)
                    except (KeyError, ValueError):
                        pass
            elif kind in _TAG_OPS:
                # Additive synchronous metadata ops (session tag CRUD): one
                # request -> one response record, no turn, no amplifier-core.
                _emit_raw(out, _handle_tag_op(runtime, op))
            elif kind == "interrupt":
                asyncio.create_task(runtime.interrupt())  # noqa: RUF006 — fire-and-forget
            elif kind == "effort.get":
                # Read-only: reply with the current tier + canonical ring order.
                await _emit_effort_state(runtime, out)
            elif kind == "effort.set":
                # Set an explicit tier (accepts the "max"->"xhigh" alias). The
                # echoed effort.state carries ok/detail so the client can show the
                # same notice /effort does; an invalid level reports ok:false and
                # leaves the tier unchanged (session_ops.set_effort).
                ok, detail = await runtime.set_effort(str(op.get("effort", "")))
                await _emit_effort_state(runtime, out, ok=ok, detail=detail)
            elif kind == "effort.cycle":
                # The donor's headline op, re-expressed server-side so the
                # canonical ring order lives in ONE home; a client may equally
                # compose get+set. Advances one tier, wrapping xhigh->none.
                nxt = _next_effort(await runtime.get_effort())
                ok, detail = await runtime.set_effort(nxt)
                await _emit_effort_state(runtime, out, ok=ok, detail=detail)
            elif kind == "history.query":
                # Additive READ op (no turn needed): frecency-ranked prompt
                # recall. Serviced inline off the ops queue so it answers
                # even while a turn runs; emit is on the loop thread (safe).
                # Merge note: other in-flight lanes append effort.*/tag.*
                # arms to THIS ladder -- each arm is independent, so the
                # only adjacency is textual (self-contained additive elif).
                _emit_raw(out, _history_list_record(runtime, op))
    finally:
        # Let an in-flight turn finish (the pump keeps draining its events) so a
        # piped one-shot `submit` completes cleanly on stdin EOF; only then stop
        # the pump and tear down. An interactive client that wants to abort sends
        # `interrupt` rather than closing the pipe.
        if turn is not None and not turn.done():
            try:
                await turn
            except Exception:  # noqa: BLE001 — a failed turn already emitted its record
                pass
        pump.cancel()
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
        _emit_raw(
            out,
            {
                "schema_version": 1,
                "type": "error",
                "session_id": runtime.session_id,
                "error": str(caught),
                "error_type": type(caught).__name__,
            },
        )
        return ""
    finally:
        # Turn-end queue duty (ui/app_support.finish_turn_queues parity):
        # leftover steers are discarded — an unconsumed steer must never
        # inject into a later turn the user never aimed it at (ADR-0007
        # §Steering). The protocol client drains its own mirror queue and
        # shows the discard notice; serve only keeps the runtime honest.
        runtime.steering.drain_steers()
    _emit_raw(
        out,
        {
            "schema_version": 1,
            "type": "turn.completed",
            "session_id": runtime.session_id,
            "response": response,
        },
    )
    return response
