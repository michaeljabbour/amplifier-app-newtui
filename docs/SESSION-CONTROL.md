# Session Control Contract

How an **automated controller** and a **human** share one live Amplifier session
without stepping on each other: a durable session handle, a single-writer lease
with deterministic takeover, actor attribution, idempotent retries, and a
reattach path that can never corrupt the transcript or leave a session locked.

This is the *protocol* half of the story. `serve` (stdio JSONL) is one adapter
over it; a Rust client, a tmux attachment, or a phone/voice bridge are others.
The rule the design follows: **define ownership semantics first, build
click-through experiences on top.**

- Wire + ops: [`kernel/serve.py`](../src/amplifier_app_tui/kernel/serve.py)
- State machine: [`kernel/session_control.py`](../src/amplifier_app_tui/kernel/session_control.py)
- Tests: `tests/test_session_control.py` (unit, injected clock),
  `tests/test_serve_control.py` (protocol, real store)

---

## 1. Opt-in, or nothing changes

The control plane is **lazily materialized**. It comes into existence the first
time a client sends a control op (`lease.*` / `session.*` / `handoff.*` /
`audit.query`) or attaches `actor` / `lease` / `idem` to any op.

A client that never does sees the byte-identical legacy protocol — same records,
same ordering — and no control files are written. Existing front-ends need no
changes; new ones opt in per session.

## 2. Durable state

Everything lives beside the session, so it survives a dropped pipe, a process
restart, and a second client attaching from elsewhere:

```
~/.amplifier/projects/<slug>/sessions/<session-id>/
    control.json          # handle, lease, pause flag, handoffs, idempotency ring
    control-audit.jsonl    # append-only attribution trail
    ui-events.jsonl        # the existing event ledger — what history.replay streams
```

`control.json` is written atomically inside a short-lived `O_EXCL` lock (a stale
lock older than 30s is broken — a crashed holder must never wedge a session).

## 3. Session handle

```jsonc
--> {"op": "session.handle"}
<-- {"type": "session.handle", "handle": {
      "handle_id": "h-1f0c…", "session_id": "5c3e…", "created_at": 1.7e9,
      "ref": "amplifier-session:5c3e…",
      "attach_command": "amplifier-tui serve --attach amplifier-session:5c3e…"}}
```

The `handle_id` is minted once per session directory and re-read forever after,
so a reconnect observes the same handle it left. `ref` is the durable reference
a controller stores, mails, or pastes; `parse_attach_ref()` also accepts a bare
session id.

## 4. Single-writer lease

Write ops — `submit`, `steer`, `approve`, `decision`, `interrupt` — are guarded.

| Op | Meaning |
|---|---|
| `{"op":"lease.acquire","actor":{...},"ttl":120}` | take the write token (free / expired / already yours) |
| `{"op":"lease.heartbeat","lease":"l-…","ttl":120}` | extend it |
| `{"op":"lease.release","lease":"l-…"}` | give it up (retry-safe no-op if already gone) |
| `{"op":"lease.takeover","actor":{...},"force":false}` | seize it (see §5) |
| `{"op":"lease.status"}` | read-only snapshot |

Every reply is a `lease.state` record: `{lease: {...}\|null, epoch, paused, now}`.

**Authorization rule** (one rule, stated once):

1. session paused → refused (`session_paused`);
2. op presents `lease` → it must BE the current, unexpired lease, else refused
   (`lease_expired` / `not_holder`);
3. op presents no `lease` → allowed only while **no** lease is active, else
   refused (`lease_held`).

Two clients can never both land in the allowed branch, so conflicting input is
refused at the door rather than interleaved into the transcript. A refusal is a
`control.conflict` record naming the holder — nothing silently disappears.

**Expiry is the anti-lock guarantee.** A lease has a TTL; a controller that dies
without releasing it frees the session on its own at expiry (audited as
`lease.expired`). No unlock request, no operator intervention.

## 5. Deterministic takeover

Actor precedence: `human` (2) > `automation` (1) > `unknown` (0).

* no lease, or an expired one → **granted**
* requester outranks holder → **granted**, the holder's `lease_id` is dead
* equal precedence **and** `force` **and** requester is human → **granted**
* otherwise → `control.conflict` (`takeover_denied`)

So a person can always break in on a bot; a bot can never break in on a person;
two bots never fight. Every grant bumps `epoch` and mints a new `lease_id`, so
the loser's next write is refused with `not_holder` instead of interleaving.

## 6. Pause → handoff → claim (escalation)

```jsonc
--> {"op":"session.pause","actor":{"id":"bot-1","kind":"automation"},
     "lease":"l-…","reason":"needs human judgment","interrupt":false}
<-- {"type":"handoff.created","handoff":{
      "handoff_id":"ho-9a2…", "reason":"needs human judgment",
      "ref":"amplifier-session:5c3e…#ho-9a2…",
      "attach_command":"amplifier-tui serve --attach amplifier-session:5c3e…#ho-9a2…",
      "claimed":false}}
```

Pausing parks the write lane (the pauser's own lease is dropped) and blocks
**every** write until someone claims or resumes. `"interrupt": true` also
cancels the running turn; by default a pause lets the turn finish.

The human claims it — from any process, with only the ref:

```jsonc
--> {"op":"handoff.claim","handoff":"ho-9a2…","actor":{"id":"mj","kind":"human"}}
<-- {"type":"handoff.claimed", ...}   +   {"type":"lease.state","lease":{"actor":{"id":"mj"}}}
```

A handoff is **one-shot**: a second claim conflicts (`handoff_claimed`), an
unknown ref conflicts (`unknown_handoff`). `session.resume` lifts a pause
without a handoff. `handoff.list` enumerates them.

CLI adapter: `amplifier-tui serve --attach <ref> --actor mj --actor-kind human`
resumes that session and claims the handoff on boot, so the arriving human holds
the lease before their first keystroke.

## 7. Attribution and audit

Every mutating op carries `actor` (a bare string id, or
`{"id","kind","display"}`). Every decision appends to `control-audit.jsonl` and
is mirrored on the wire as `control.audit`:

```jsonc
{"type":"control.audit","entry":{
  "seq":7,"at":1.7e9,"action":"handoff.claimed",
  "actor":{"id":"mj","kind":"human"},
  "session_id":"5c3e…","handle_id":"h-1f0c…","epoch":3,"lease_id":"l-…"}}
```

Actions: `lease.granted` · `lease.renewed` · `lease.released` · `lease.revoked`
· `lease.expired` · `lease.denied` · `lease.takeover` · `session.paused` ·
`session.resumed` · `handoff.created` · `handoff.claimed` · `write.accepted` ·
`write.rejected`.

`{"op":"audit.query","limit":50}` reads the trail back over the protocol, so a
human client and an automated one can inspect the same history.

`kind` is a *claim* the client makes, recorded verbatim. Transport-level
authentication is deliberately out of scope: `serve` speaks over a pipe whose
peer the OS already established. A networked adapter maps its authenticated
principal onto this field; every semantic above holds unchanged.

## 8. Idempotency

Any control or write op may carry `"idem": "<key>"`. The records that answered
it are remembered durably (bounded ring of 128), so a retry after a dropped
connection **replays** them — flagged `"replay": true` — instead of acting twice.

```jsonc
--> {"op":"submit","text":"deploy","actor":{"id":"bot-1"},"idem":"req-42"}
<-- {"type":"control.ack","op":"submit","idem":"req-42","actor":{...}}
    … turn runs …
    (connection drops; controller reconnects and retries the same key)
--> {"op":"submit","text":"deploy","actor":{"id":"bot-1"},"idem":"req-42"}
<-- {"type":"control.ack","op":"submit","idem":"req-42","replay":true}   # no second turn
```

Rejections are deliberately **not** remembered: a retry must re-evaluate against
the lease as it stands then.

## 9. Reconnect / reattach

```jsonc
--> {"op":"history.replay","since":0,"limit":0}
<-- {"type":"history.begin","since":0}
<-- {"type":"runtime.event","replay":true,"sequence":1,"event":{...}}   # ledger order
<-- {"type":"history.end","count":42,"cursor":42}
```

Replay is **read-only** — it streams the durable UIEvent ledger and never writes
the transcript. `sequence` is the ledger index (not the live connection's
counter) and every record is flagged `replay`, so a client can resume from
`cursor` without double-counting cost or confusing replayed events with live
ones. A human and an automated participant reattaching to the same session
observe the same history.

Combined with §4's expiry, a disconnected participant can always come back, and
can never leave the session permanently locked.

## 10. Conflict reasons (stable strings)

`no_actor` · `lease_held` · `not_holder` · `lease_expired` · `takeover_denied` ·
`session_paused` · `unknown_handoff` · `handoff_claimed`

## 11. Building on this

A higher-level client (voice, mobile, chat bridge) should:

1. `session.handle` on connect → keep the `ref` as its durable pointer;
2. `lease.acquire` with its own actor identity, and heartbeat at ~TTL/3;
3. send every write with `lease` + `idem`;
4. treat `control.conflict` as authoritative — re-read `lease.status`, never
   retry blindly;
5. `session.pause` to escalate, and hand the returned `attach_command` /
   `ref` to a person;
6. on reconnect: `history.replay` from its last `cursor`, then re-acquire.

The contract is the seam. Nothing above requires the TUI, and the TUI is just
another adapter over it.
