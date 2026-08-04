# Session Control Contract

How an **automated controller** and a **human** share one live Amplifier session
without stepping on each other: an authenticated identity with real permissions,
a durable session handle, a single-writer lease with deterministic takeover, a
complete status read, universal attribution, live attachment to a running
session, and a reattach path that can never corrupt the transcript or leave a
session locked.

This is the *protocol* half of the story. `serve` (stdio JSONL) is one adapter
over it; a Rust client, a tmux attachment, or a phone/voice bridge are others.
The rule the design follows: **define ownership semantics first, build
click-through experiences on top.**

- Wire + ops: [`kernel/serve.py`](../src/amplifier_app_tui/kernel/serve.py)
- State machine: [`kernel/session_control.py`](../src/amplifier_app_tui/kernel/session_control.py)
- Authorization: [`kernel/session_authz.py`](../src/amplifier_app_tui/kernel/session_authz.py)
- Live attachment: [`kernel/session_attach.py`](../src/amplifier_app_tui/kernel/session_attach.py)
- Tests: `tests/test_session_control.py` (unit, injected clock),
  `tests/test_serve_control.py` (protocol, real store),
  `tests/test_session_authz.py` (identity + permissions),
  `tests/test_serve_status.py` (status completeness),
  `tests/test_serve_audit_registry.py` (no unaudited mutation can be added),
  `tests/test_session_attach.py` (endpoint, fan-out, stale break),
  `tests/test_session_control_multiprocess.py` (**two real processes**)

---

## 1. Opt-in, or nothing changes

The control plane is **lazily materialized**. It comes into existence the first
time a client sends a control op (`lease.*` / `session.*` / `handoff.*` /
`audit.query`) or attaches `actor` / `lease` / `idem` / `auth` to any op.

A client that never does sees the byte-identical legacy protocol — same records,
same ordering — and no control files are written. Existing front-ends need no
changes; new ones opt in per session.

**The one exception, and it is deliberate:** once an operator issues a control
token for a project (§4), *every* classified op on that project is
authenticated, whether or not the client asked. An authorization scheme you can
skip by sending fewer fields is not an authorization scheme. Projects with no
token issued are unaffected.

## 2. Durable state

Everything lives beside the session, so it survives a dropped pipe, a process
restart, and a second client attaching from elsewhere:

```
~/.amplifier/projects/<slug>/sessions/
    control-authz.json            # hashed capability tokens (per PROJECT)
    <session-id>/
        control.json              # handle, lease, pause flag, handoffs, idempotency ring
        control-audit.jsonl       # append-only attribution trail
        attach.json               # live-attach endpoint of the owning process
        attach.sock               # the socket it listens on
        ui-events.jsonl           # the existing event ledger — what history.replay streams
```

`control.json` is written atomically inside a short-lived `O_EXCL` lock
(`kernel/file_lock.py`, shared with every other durable kernel writer). A stale
lock older than 30s is broken — a crashed holder must never wedge a session.

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

## 4. Authorization: who you are, and what you may do

`kind` used to be an unverified claim. Since a `human` always outranks an
`automation` for the lease (§6), any client could send `{"kind": "human"}` and
seize the pen from a real person's controller. Over a local pipe whose peer the
OS established, that was a defensible courtesy. Over anything else it is
privilege escalation — and it is the security prerequisite the ambient
delegation track names as blocking ("E1").

Two ideas, and deliberately no more.

**Principal — who the connecting party is.** Established by an
`AuthorizationPolicy` from a credential the client presents. It carries the
identity, the **verified** kind, the permissions held, and its own provenance.

**Permission — what that principal may do.** Three verbs, matching the
`session:<sid>` scope vocabulary the downstream grant model uses, so a grant
minted there maps across with no translation:

| Permission | Covers |
|---|---|
| `read` | `session.status` · `lease.status` · `session.handle` · `handoff.list` · `audit.query` · `history.replay` · `history.query` · `context.get` · `effort.get` · `tag.list` · `tag.sessions` |
| `write` | every mutation: `submit` · `steer` · `approve` · `decision` · `interrupt` · `tag.add` · `tag.remove` · `effort.set` · `effort.cycle` |
| `control` | ownership: `lease.acquire` · `lease.heartbeat` · `lease.release` · `lease.takeover` · `session.pause` · `session.resume` · `handoff.claim` |

`read` never implies `write`; `write` never implies `control`. An observer bot
can watch a session it may not drive, and drive one it may not seize.

**Turning it on.** Authorization is opt-in per project, exactly as the control
plane is opt-in per session:

```sh
amplifier-tui control-token issue mj  --kind human
amplifier-tui control-token issue bot --kind automation --permission read --permission write
amplifier-tui control-token list
amplifier-tui control-token revoke tok-…
```

Tokens are stored **hashed** — the plaintext is printed once and never written
to disk — and are resolved from disk on every op, so a revoke takes effect on
the very next request. A cached grant is a revoke that didn't happen. Minting
lives on this first-party surface on purpose: a channel that can mint its own
credential is not a credential.

**Presenting one.** Any op may carry `auth`:

```jsonc
--> {"op":"lease.acquire","actor":{"id":"mj","kind":"human"},
     "auth":{"token":"amp-ctl-…"},"ttl":120}
```

**The three refusals**, in order, each a surfaced `control.conflict` and an
`auth.denied` audit entry — never a silent downgrade:

1. no principal for the presented credential → `unauthenticated`;
2. the message claims an identity the principal is not entitled to — a
   *different* id, or a `kind` that **outranks** the verified one →
   `identity_unverified`. (A principal may always act *below* itself: a verified
   human driving a bot lane may present `automation` and take the weaker
   precedence that comes with it.)
3. the principal holds no permission for that op class → `permission_denied`.

**Provenance.** A verified identity carries an additive `auth` block:

```jsonc
"actor": {"id":"mj","kind":"human",
          "auth":{"method":"token","verified":true,"principal":"mj"}}
```

Its **absence is meaningful**: it says "established by the OS pipe peer and
nothing stronger", which is the honest claim for a local pipe. That is what
lets the trail distinguish an authenticated human from a process that typed the
word. A client-supplied `auth` block on the wire is ignored — provenance is
minted from the policy's verdict, never accepted from the wire.

**Mapping a networked adapter on.** An adapter that authenticated its peer some
other way (OIDC, mTLS, device token, platform SSO) builds the `Principal`
itself and wraps it in `StaticPolicy`:

```python
control = SessionControl(session_dir, session_id, policy=StaticPolicy(
    Principal(principal_id="mj@contoso", kind="human",
              permissions=frozenset({"read", "write", "control"}),
              method="oidc", verified=True)))
```

Every semantic below then holds unchanged, with real provenance in the trail.
An adapter authenticates, transports and renders; it holds no policy and
invents no lease semantics.

## 5. Single-writer lease

Write ops are guarded — all of them (§9).

| Op | Meaning |
|---|---|
| `{"op":"lease.acquire","actor":{...},"ttl":120}` | take the write token (free / expired / already yours) |
| `{"op":"lease.heartbeat","lease":"l-…","ttl":120}` | extend it |
| `{"op":"lease.release","lease":"l-…"}` | give it up (retry-safe no-op if already gone) |
| `{"op":"lease.takeover","actor":{...},"force":false}` | seize it (see §6) |
| `{"op":"lease.status"}` | read-only lease snapshot (unchanged shape; see §8 for the full picture) |

Every reply is a `lease.state` record: `{lease: {...}\|null, epoch, paused, now}`.

**The gate rule** (one rule, stated once):

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

## 6. Deterministic takeover

Actor precedence: `human` (2) > `automation` (1) > `unknown` (0).

* no lease, or an expired one → **granted**
* requester outranks holder → **granted**, the holder's `lease_id` is dead
* equal precedence **and** `force` **and** requester is human → **granted**
* otherwise → `control.conflict` (`takeover_denied`)

So a person can always break in on a bot; a bot can never break in on a person;
two bots never fight. Every grant bumps `epoch` and mints a new `lease_id`, so
the loser's next write is refused with `not_holder` instead of interleaving.

Under a token policy the `kind` driving this is a **verified** one (§4), which
is what turns the courtesy into a boundary.

## 7. Pause → handoff → claim (escalation)

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
joins that session (live if it is running — §11) and claims the handoff, so the
arriving human holds the lease before their first keystroke.

## 8. Complete status

`lease.status` answers one question — who holds the pen — and is deliberately
left byte-identical, because clients branch on it. It is not enough to decide
anything: a controller also has to know whether a turn is running (else its
`submit` is silently dropped as a re-submit), whether an approval or a deferred
decision is blocking (else it waits forever for a turn that cannot finish),
which model and reasoning tier are actually in force after a mid-session change,
what it has queued, how much context and budget remain, and where the ledger
stands.

`session.status` is that record, and it is a pure read — no audit entry, no
epoch bump, safe to poll:

```jsonc
--> {"op":"session.status"}
<-- {"type":"session.status","ok":true,"session_id":"5c3e…",
     "state":"awaiting_approval",              // paused|awaiting_approval|awaiting_decision|busy|idle
     "turn":    {"active":true,"queued_steers":1,"queued_next_turn":0},
     "session": {"bundle":"tui","provider":"anthropic","model":"claude-sonnet-4","effort":"high"},
     "pending": {"approval":{"ticket_id":"approval-3","prompt":"…","options":["Allow once","Deny"]},
                 "decisions":[{"decision_id":"decision-1","question":"Which region?"}],
                 "decision_count":1},
     "context": {"context_tokens":48120,"context_window":200000,"context_pct":24,"cost_usd":"0.83"},
     "history": {"events":42,"cursor":42,"last":{"kind":"assistant_message","ts":"…"}},
     "control": {"epoch":3,"paused":false,"paused_by":null,
                 "lease":{"lease_id":"l-…","actor":{…},"expires_at":1.7e9,"expires_in":94.2},
                 "holder":{"id":"bot-1","kind":"automation"},
                 "handoffs":{"total":2,"open":1,"pending":[{…}]},
                 "authz":{"policy":"token","requires_credential":true,"verified":true},
                 "audit":{"seq":17,"last":{…}}}}
```

`state` is the one word to branch on, in strict priority order: a paused session
is paused whatever else is true; an approval blocks the turn it is inside; a
deferred decision waits on a person without blocking; otherwise the session is
working or free. `control` is `null` before anyone opts in — reading status is
not itself an opt-in, so "check before you commit" is not a commitment.

## 9. Attribution and audit — universal

Every mutating op carries `actor` (a bare string id, or
`{"id","kind","display"}`). Every decision appends to `control-audit.jsonl` and
is mirrored on the wire as `control.audit`:

```jsonc
{"type":"control.audit","entry":{
  "seq":7,"at":1.7e9,"action":"handoff.claimed",
  "actor":{"id":"mj","kind":"human","auth":{"method":"token","verified":true,"principal":"mj"}},
  "session_id":"5c3e…","handle_id":"h-1f0c…","epoch":3,"lease_id":"l-…"}}
```

Actions: `lease.granted` · `lease.renewed` · `lease.released` · `lease.revoked`
· `lease.expired` · `lease.denied` · `lease.takeover` · `session.paused` ·
`session.resumed` · `handoff.created` · `handoff.claimed` · `write.accepted` ·
`write.rejected` · `auth.denied`.

**"Universal" is a property of the routing, not a discipline.** `serve.OP_PERMISSIONS`
is the single table the dispatch loop reads to classify every op. An op
classified `write` is routed through `SessionControl.authorize()`, which cannot
accept it without appending `write.accepted` (or `write.rejected`). So the trail
is not something an implementer has to remember to call — and
`tests/test_serve_audit_registry.py` fails the build if an op the loop handles
is missing from the table, or if the table names one the loop never dispatches.

**What this fixed.** Before it, `tag.add` · `tag.remove` · `effort.set` ·
`effort.cycle` mutated the session with **no lease check and no attribution at
all**: a controller could retag and re-tier a session it did not hold the pen
for and the trail would show nothing. They are now `write` ops like any other.

**What is honestly out of reach**, and why:

- **The in-process Textual TUI.** Rewind/fork, context clear, and settings
  changes made by a human sitting at the app are not protocol ops. That surface
  has exactly one participant, whom the OS already established, and routing it
  through the control plane would materialize control state for every ordinary
  TUI session — breaking §1's opt-in promise to buy attribution nobody is
  contesting. Named, not silently ignored. Note the *reachable* consequence:
  because those paths are not ops, they cannot be driven by a remote controller
  either, so there is no unaudited remote mutation path.
- **Tool-side effects inside a turn** (file writes, shell, git). These are
  governed by the approval/governance system and recorded in `ui-events.jsonl`,
  not `control-audit.jsonl`. The control trail attributes *who caused the turn*;
  the event ledger records *what the turn did*. Joining them is the `session_id`
  and the timestamps.

The ambient delegation layer (item B8, `kernel/ambient/`) appends to the **same**
trail through `SessionControl.note_ambient()`, using a separate, closed
vocabulary: `source.read` · `source.send` · `source.denied` · `grant.created` ·
`grant.revoked` · `grant.expired` · `interpretation.proposed` ·
`interpretation.amended` · `interpretation.confirmed` ·
`interpretation.cancelled` · `interpretation.expired` · `reply.accepted` ·
`reply.rejected`. One trail is the point — "which grant authorized this read"
and "who answered the notification" are answerable in `seq` order alongside the
lease decisions they interleave with. The split is the guard: an ambient caller
can add to the account of what happened, but **cannot forge a control action**
(`note_ambient` rejects anything outside its own set).

`{"op":"audit.query","limit":50}` reads the trail back over the protocol, so a
human client and an automated one can inspect the same history.

## 10. Idempotency

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

## 11. Reattach: replay, and live attachment

### Replay (read-only history)

```jsonc
--> {"op":"history.replay","since":0,"limit":0}
<-- {"type":"history.begin","since":0}
<-- {"type":"runtime.event","replay":true,"sequence":1,"event":{...}}   # ledger order
<-- {"type":"history.end","count":42,"cursor":42}
```

Replay is **read-only** — it streams the durable UIEvent ledger and never writes
the transcript or touches the lease. `sequence` is the ledger index (not the
live connection's counter) and every record is flagged `replay`, so a client can
resume from `cursor` without double-counting cost or confusing replayed events
with live ones.

### Live attachment (joining a running runtime)

`--attach` used to *resume*: it booted a second runtime over the same session
id. That attaches to the same session **state** — and if the first process was
still running, it also meant two live runtimes appending to one
`ui-events.jsonl`. The lease stops two *clients* interleaving input; nothing
stopped two *processes* interleaving transcript.

The process that owns a session now listens on a Unix socket beside the session
directory and advertises it in `attach.json`. `--attach` resolves in two stages:

1. **A live owner exists** → join it as a peer. No runtime is booted here. Every
   record the owner emits is fanned out to this client, and every op sent lands
   in the owner's own queue — so an attached participant drives the *same* live
   session, gated by the *same* lease.
2. **No live owner** → resume the session state here, claim the handoff if the
   ref carries one, and become the live owner.

```jsonc
<-- {"type":"attach.listening","session_id":"5c3e…","pid":4711,"socket_path":"…/attach.sock"}
<-- {"type":"session.attached","session_id":"5c3e…","pid":4711,"mode":"live"}
```

The endpoint is published automatically as soon as a session uses the control
plane, or up front with `serve --attachable` (for a long-running automated
session that never opens the control plane itself).

The four safety properties, and what each rests on:

- **No double-writer** — exactly one process owns the runtime. Ownership is
  claimed under the shared `O_EXCL` lock, and a would-be second owner that finds
  a live endpoint stands down and attaches instead.
- **No transcript corruption** — a peer never touches the store. Only the
  owner's runtime appends events; follows from the above.
- **Deterministic conflict resolution** — two rules, no timing. *At the process
  level*: a live endpoint wins; a **stale** one (owner gone, or socket refusing
  connections — both checked, because a pid can be recycled and a socket file
  survives `kill -9`) is broken, so a hard-killed owner can never make a session
  look permanently occupied. *At the participant level*: nothing changes — the
  lease and its takeover precedence decide who may write.
- **Clean detach** — a peer closing its socket leaves the fan-out and touches
  nothing else; the owner keeps serving. An owner shutting down unlinks both the
  socket and the advert.

`AF_UNIX` is required. Where the platform lacks it, advertisement is skipped and
`--attach` degrades to stage 2 rather than pretending.

Combined with §5's expiry, a disconnected participant can always come back, and
can never leave the session permanently locked.

## 12. Conflict reasons (stable strings)

`no_actor` · `lease_held` · `not_holder` · `lease_expired` · `takeover_denied` ·
`session_paused` · `unknown_handoff` · `handoff_claimed` · `unauthenticated` ·
`identity_unverified` · `permission_denied`

## 13. Building on this

A higher-level client (voice, mobile, chat bridge) should:

1. authenticate its principal at its own boundary and map it on (§4) — over a
   network this is not optional;
2. `session.handle` on connect → keep the `ref` as its durable pointer;
3. `lease.acquire` with its own actor identity, and heartbeat at ~TTL/3;
4. send every write with `lease` + `idem`;
5. poll `session.status` rather than `lease.status` when deciding whether to act;
6. treat `control.conflict` as authoritative — re-read status, never retry blindly;
7. `session.pause` to escalate, and hand the returned `attach_command` / `ref`
   to a person, who joins the *live* session (§11);
8. on reconnect: `history.replay` from its last `cursor`, then re-acquire.

The contract is the seam. Nothing above requires the TUI, and the TUI is just
another adapter over it.
