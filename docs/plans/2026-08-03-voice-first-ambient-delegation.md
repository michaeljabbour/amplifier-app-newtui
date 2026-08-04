# Design — Voice-first, ambient delegation (architecture track)

**Compliance item:** B8 — "Design for voice-first, ambient delegation"
**Status:** 🔨 **partially implemented.** The contracts this document specified are now built
(`kernel/ambient/`), together with the thin voice adapter it deliberately sequenced last. Two
items remain genuinely out of reach here and are marked as such rather than faked: a **real
Teams/Outlook connector** (E8 — needs Microsoft Graph credentials, tenant consent and network)
and a **network listener** for the reply channel (E7 — the security core is built and tested;
no reachable service ships). Speech capture and synthesis are device capabilities and are
likewise not built. See **"Implementation status"** below for the per-extension verdict.
**Built on:** B6 session-control contract (PR #203 — merged to `main`) · B7 attention
contract (PR #229 — merged to `main`) · E1 authorization (PR #230 — open; consumed, not owned)
**Author:** compliance worker · **Date:** 2026-08-03 · **Updated:** 2026-08-04
**Slug:** `voice-first-ambient-delegation`

> **Citation honesty.** Every `file:line` cite into `kernel/session_control.py`,
> `kernel/persistence.py`, `ui/notifications.py`, `docs/SESSION-CONTROL.md` and
> `docs/SETTINGS.md` was verified at `55c8f48`; B6 has since landed on **`main`**
> (PR #203, squash-merged byte-identically), so those cites carry over unchanged.
>
> **Re-verified 2026-08-04, before building.** B7 has since landed on `main` (PR #229):
> `AttentionRecord` / `AttentionCenter` / `attention_push_payload` are real, and
> `kernel/attention_store.py` provides the durable cross-process store — so **E4 and E5 were
> already delivered** and collapsed to "verify and consume", exactly as this document
> predicted for E5. E1 is being delivered by PR #230 (`kernel/session_authz.py`), which is
> **open, unmerged**, so the implementation *consumes* it optionally and degrades cleanly in
> its absence. Every other cite below was re-checked against the tree at `332ee11`.

---

## Implementation status (2026-08-04)

Everything below was **re-verified against the tree** before building; three of the eight
extensions had changed state since this document was written, and one was reassigned.

| ID | Verdict | Where |
|---|---|---|
| **E1** — authenticated principal → `Actor` | **Owned elsewhere; consumed here.** `kernel/session_authz.py` (PR #230, open) is the policy home; it deliberately chose the `session:<sid>` + `read`/`write`/`control` vocabulary this document specifies, so a grant minted by §1 maps across with no translation. This track does **not** build or edit it. | `kernel/ambient/principal.py` — consumes it if importable, degrades to an explicitly *unverified* `LocalPrincipal` if not, and enforces the security rule that matters: **an unverified `human` claim arriving over a non-local method is downgraded to `unknown`**, so it cannot outrank anything. The downgrade is recorded in the audit provenance rather than being silent. |
| **E2** — grant store + `source.*`/`grant.*` audit | **Built.** | `kernel/ambient/grants.py`. Deny-by-default; **consulted at use, never cached** (`GrantStore` holds no in-memory grants — a revoke written by another process lands on the very next call); no wildcards (a selector-less `source:*` grant is rejected at creation); `read` never implies `send`; minting restricted to first-party surfaces. Additive audit actions land in the **same** `control-audit.jsonl` via the new `SessionControl.note_ambient`, whose vocabulary is closed and separate from the control actions — an ambient caller can add to the account, never forge a `lease.granted`. |
| **E3** — structured, editable interpretation payload | **Built.** | `kernel/ambient/interpretation.py`. Typed record keyed by the B6 handoff id, with `propose`/`amend`/`confirm`/`cancel`. Gating reuses B6 **unchanged** (`pause` → `authorize()` denies every write → `claim_handoff`). `amend` mints a new id and never mutates; expiry is `cancel` **and resumes**, so a forgotten voice request cannot wedge a session; `cancel` and expiry are audited alongside `confirm`. The handoff's `note` field is left empty — the doc's "do not JSON-stuff `note`" rule is asserted by a test. |
| **E4** — push payload carries `event_id` | **Already delivered** by B7 (PR #229): `attention_push_payload()` in `ui/notifications.py`, emitted as an `attention:recorded` hook event. Nothing to build. | The ambient layer adds a **stricter** payload on top (`ambient_push_payload`): pointer-only, built from a literal allowlist, no `body` at all. |
| **E5** — durable cross-process attention records | **Already delivered** by B7 (PR #229): `kernel/attention_store.py` + `AttentionCenter.bind()`. As this document predicted, E5 collapsed to "verify, then consume". | Consumed by `kernel/ambient/reply.py`, which acknowledges an answered record cross-process. |
| **E6** — cross-project session discovery | **Built, as a read-side scan** — the cheap option this document recommended, and the recommendation holds. | `kernel/ambient/discovery.py`. See "Why a scan and not an index" in that module: the projection cannot drift because it *is* the truth re-read, an index would be a second write contract to keep in sync, and the failure mode of a stale index is the worst one available (a fleet view that confidently reports a stuck session as running). `SessionDiscovery` caches each row on its session directory's mtime, so steady-state re-reads collapse to O(changed). An unreadable session degrades to a **partial row, never an exception**. |
| **E7** — authenticated inbound reply channel | **Split, honestly.** The **security core is built and tested**; **no network listener ships, and none can be verified here.** | `kernel/ambient/reply.py`. Built: HMAC-SHA256 envelope authentication over a canonical string with constant-time compare, replay rejection (nonce + freshness window), device enrollment with `0600` secrets that never reach a log or an audit entry, correlation `event_id` → session → handoff, re-entry via `handoff.claim`, and attention acknowledgement. `accept()` is transport-agnostic on purpose, so a future HTTPS handler adds a transport without moving the security core. **v1 default is reply-on-open** (`pending_for_open`) — one-tap-to-the-right-place, zero new network surface — exactly as §3 option (c) proposed. **The ntfy reply-topic option (a) remains rejected**: a world-readable channel must never be a write path. |
| **E8** — Teams/Outlook connectors | **Genuinely external. Not built, and deliberately not faked.** | `kernel/ambient/sources.py` ships the **port** a real connector must implement, plus a **working local implementation** (`LocalFileSource`, real files on disk) and the enforcement wrapper (`GrantedSource`) that consults E2 at use and attributes every read/send. A real connector needs a Graph app registration, tenant-granted delegated scopes, an interactive consent flow, and live network access — none of which exist offline, and every one of which is a place a guess would be silently wrong. When it is built, the work is the Graph client and its consent flow; the permission check, the audit trail, the confirmation echo and the redaction policy are already done and do not move. |

**The voice adapter itself** (`kernel/ambient/voice.py`) is built, and is thin by
construction: every field on it is a collaborator, never a cache. It classifies consequence
(the five rules of §2, as one pure function), echoes an interpretation for anything
consequential, parses only the closed response vocabulary (`confirm` / `amend(field, value)` /
`cancel`, anything else re-asks rather than guessing), **refuses a spoken confirmation on an
irreversible action** and routes it to a visual surface, speaks the fleet report off E6, and
sequences follow-ons across sessions (`FollowOnPlan`) stopping on the first `control.conflict`.

**Also not built, and not buildable here:** speech capture, wake-word detection, ASR and TTS
are device capabilities. The adapter's boundary is therefore **already-transcribed text in,
speakable text out**; a real voice client owns the microphone and the speaker. There is no
mobile client either — E7's reply-on-open path and the pointer-only push payload are the seams
one would attach to.

**Open questions now answered by the implementation:** Q2 (where the ambient layer runs) — it
runs **in-process, over the filesystem**, like every other kernel contract, so a daemon remains
possible without a rewrite. Q3 (is reply-on-open acceptable for v1) — **yes, shipped as the
default**, with the authenticated core built behind it. Q4 (grant scope) — **per-user for
`source:*`**, as proposed. Q1 (Teams/Outlook API specifics) and Q5 (unattended voice mode)
remain **open and unverified**.

---

## Problem

`serve` externalized *what* can be driven. B6 added *who* may drive it. B7 added *when the
assistant needs you*. Nothing yet addresses the third question: **how a person delegates work
without a keyboard, and keeps an accurate picture of what a fleet of sessions is doing while
they are not watching.**

That is item B8, and it is deliberately a design track rather than a feature. The reason is
structural: voice and mobile are *channels*. If they are built first, they grow their own
notions of permission, confirmation, session ownership, and history — four things that must
be owned once, below the channel, or every future client re-implements (and re-breaks) them.
The rule this document follows is the one B6 already set:

> "define ownership semantics first, build click-through experiences on top."
> — `docs/SESSION-CONTROL.md:10-11`

So this specifies the four contracts that must exist **before** any UI: permission
boundaries, confirmation policy, mobile reply correlation, and cross-session activity
history. Where B6/B7 already cover a need, the capability reduces to a thin adapter over
them. Where they do not, the gap is named as a **required contract extension** (E1–E8) and is
not assumed to exist.

---

## The two framings — kept distinct, deliberately

Two different people described this capability, and they are **not** the same requirement.
Collapsing them into one "voice + notifications" story loses the part that keeps the other
honest.

**Brian's framing — conversational delegation.** The unit of work is a *request*. Say the
thing; get a faithful echo; confirm; walk away. Success is that the assistant does the right
work without you supervising it. What this framing demands of the design: a high-fidelity
interpretation echo (§2), a reply path that returns you to the exact session that asked (§3),
and very little ceremony — every extra confirmation step is a tax on the whole point.

**MJ's framing — perceptual visibility into teams of agents.** The unit is the *fleet*. Many
sessions, many agents, seen at a glance: who is running, who is stuck, who needs you, what
changed since you looked away. Success is that you can *perceive* the state of the team
without interrogating it. What this framing demands: a cross-session activity model (§4),
state that is legible without being opened, and attention as a first-class signal that
de-duplicates and can be acknowledged (B7's whole contribution).

**Why they must not be merged.** They fail in opposite directions, so each is the other's
safety net:

- Conversational delegation *without* visibility produces **confident silence** — the
  assistant did something, plausibly the wrong thing, and you find out later.
- Visibility *without* conversational delegation produces **a dashboard you have to
  operate** — you can see everything and still have to drive it all by hand.

Concretely: §2 and §3 are the *delegation spine*; §1 and §4 are the *visibility spine*. They
share exactly two things — B6's audit trail and B7's attention record — and that shared base
is what lets both be built without a fork. **Review rule:** every proposed capability on this
track is checked against both framings. If it advances only one, say which one and why that is
acceptable for that increment. Do not let "we shipped voice" stand in for "we shipped B8."

---

## What already exists (verified evidence)

### B6 — session control (merged to `main` via PR #203; verified at `55c8f48`)

| Capability | Where |
|---|---|
| Durable session handle + attach ref `amplifier-session:<sid>[#<handoff>]` + runnable `attach_command` | `kernel/session_control.py:197-223`, `:279-298`; `docs/SESSION-CONTROL.md:45-58` |
| Single-writer lease: acquire / heartbeat / release / status / takeover, TTL-expiring (`DEFAULT_LEASE_TTL = 120.0`) | `kernel/session_control.py:65-70`, `:673-836`; `docs/SESSION-CONTROL.md:60-88` |
| Deterministic takeover by precedence `human(2) > automation(1) > unknown(0)`; equal precedence needs a human `force` | `kernel/session_control.py:82-86`, `:767-837`; `docs/SESSION-CONTROL.md:90-101` |
| Write gating, one rule: paused → `session_paused`; presents a lease → must be the current unexpired one (`lease_expired` / `not_holder`); presents none → allowed only when no lease is active (`lease_held`) | `kernel/session_control.py:1018-1077` |
| Escalation: `pause()` mints a durable **one-shot** handoff with ref + attach command; `claim_handoff()` clears the pause and grants the lease; a second claim conflicts (`handoff_claimed`) | `kernel/session_control.py:838-975`; `docs/SESSION-CONTROL.md:103-132` |
| Actor attribution; every decision appends to `control-audit.jsonl` (`seq`, `at`, `action`, `actor`, `session_id`, `handle_id`, `epoch`, `lease_id`, `detail`) | `kernel/session_control.py:573-604`, `:491-512`; `docs/SESSION-CONTROL.md:134-153` |
| Idempotency: `idem` keys with durable replay, bounded ring of 128; **rejections deliberately not remembered** | `kernel/session_control.py:72-74`, `:605-632`; `docs/SESSION-CONTROL.md:160-176` |
| `history.replay` is a **read** op — streams the durable ledger, never touches the lease or the transcript | `docs/SESSION-CONTROL.md:178-195` |
| Durable state beside the session: `control.json` (atomic write under an `O_EXCL` lock, stale lock broken at 30s) + `control-audit.jsonl` | `kernel/session_control.py:59-63`, `:361-403`; `docs/SESSION-CONTROL.md:30-43` |

**And the boundary B6 states about itself** — the single most load-bearing fact in this
document:

> "`kind` drives takeover precedence and nothing else; it is a *claim* the client makes,
> recorded verbatim in the audit trail. Transport-level authentication is deliberately out of
> scope here … A networked adapter (item B8) maps its authenticated principal onto this
> record."
> — `kernel/session_control.py:108-118`; restated at `docs/SESSION-CONTROL.md:155-158`

B6 named B8 as the layer that closes that gap. This document accepts that assignment (E1).

### B7 — attention contract (PR #202; **open, not yet merged to `main`**)

Published shape, quoted from the PR:

```python
AttentionReason = Literal["completion", "awaiting_approval", "awaiting_clarification", "error"]

@dataclass(frozen=True, slots=True)
class AttentionRecord:
    session_id: str
    reason: AttentionReason
    event_id: str          # stable idempotency key: f(session_id, reason, occasion)
    detail: str = ""
    created_at: float = 0.0
    acknowledged: bool = False

class AttentionCenter:
    def note(self, session_id, reason, occasion, *, detail="", now=None) -> tuple[AttentionRecord, bool]: ...
    def acknowledge(self, session_id) -> AttentionRecord | None: ...
    def current(self, session_id) -> AttentionRecord | None: ...
```

`note()` is idempotent in `(session_id, reason, occasion)`. **The boundary B7 documented
honestly:** off-machine ntfy push is fired by an external hook off the raw kernel event, is
not routed through `AttentionRecord`, and has **no acknowledgement channel back to the TUI**.
§3 is written against that constraint rather than around it.

### Neighbouring facts on `main`

- Sessions live at `~/.amplifier/projects/<slug>/sessions/<session-id>/`
  (`kernel/persistence.py:138-142`); `SessionStore.list_sessions()` enumerates **one project**
  (`:345`); `ui-events.jsonl` is the append-only normalized event ledger (`:49`, `:301-344`).
- Local notification text is already sanitized and capped at 80/240 chars
  (`ui/notifications.py:80-81`, `:171-203`).
- Off-machine push is owned by the mounted `hooks-notify-push` module and "lives outside the
  app kernel entirely" (`ui/notifications.py:8-13`, `:45-46`). **The ntfy topic is a secret
  and public topics are world-readable** (`docs/SETTINGS.md:215`).

---

## Architecture: everything is a thin adapter over shared contracts

```
  voice client        mobile client        chat bridge          TUI
        |                   |                   |                |
        +-------------------+---------+---------+----------------+
                                      |
        ======================= adapter boundary ==================
             authenticate the principal HERE; hold no policy
                                      |
                          ambient delegation layer
       grants · interpretations · correlation table · activity projection
                                      |
        +-----------------------------+-----------------------------+
        |                                                           |
   B6 session control                                        B7 attention
   (per session, durable)                                (per session, records)
        |                                                           |
        +-----------------------------+-----------------------------+
                                      |
                    kernel: serve.py · persistence.py (ui-events.jsonl)
```

**The adapter rule.** An adapter may do exactly three things: authenticate a principal,
transport bytes, and render. It may **not** hold policy, permission state, session ownership,
or history. If a proposed capability requires an adapter to *remember* something, that memory
belongs in the ambient layer or in a contract extension — and this document says so out loud
rather than letting it settle into the client.

**The reduction test.** For every capability below: state which B6/B7 op it reduces to. If it
does not reduce, it is listed in the extension table (E1–E8). Nothing is assumed to exist.

---

## 1. Permission boundaries (AC4)

> **AC4** — "Cross-context access is explicit, permissioned, attributable, and limited to the
> sources the user enabled."

### The grant

A grant is the only thing that authorizes cross-context access. It is a record, not a setting:

```jsonc
{"grant_id": "g-…", "principal": "mj",
 "scope": "source:outlook",                            // vocabulary below
 "verb": "read",                                       // read | send | write | control
 "selector": {"folder": "Inbox", "from": "dana@…"},    // narrowing, never "everything"
 "granted_by": {"id": "mj", "kind": "human"},
 "granted_at": 1.7e9, "expires_at": 1.7e9, "revoked_at": null}
```

**Scope vocabulary** — four families, deliberately small:

| Scope | Verbs | Meaning |
|---|---|---|
| `session:<sid>` | `read` / `write` / `control` | `control` is the B6 lease/pause/handoff surface; separate from `write` on purpose — driving a session and *seizing* it are different powers |
| `project:<slug>` | `read` | list sessions and read their activity rows (§4) |
| `source:teams` | `read` / `send` | selector narrows to team + channel |
| `source:outlook` | `read` / `send` | selector narrows to folder / sender / time window |

**Granularity rule:** per-source **and** per-verb **and** per-selector. `read` never implies
`send`. A grant with no selector is not "all" — it is invalid and must be rejected at
creation. There is no wildcard grant in this design; if one is ever wanted, it is a separate
decision with its own review.

**Default deny.** No grant, no access — and the refusal is *surfaced*, never silently skipped.
An ambient assistant that quietly omits a source the user believed was connected is worse than
one that says "I can't see your mail."

### Grant and revoke

- **Minted only on a first-party, visually-confirmed surface** (the TUI or CLI). A voice
  channel may *request* a grant; it may never *create* one. Rationale: a permission escalation
  is the one action where the channel's own weakness — lossy ASR, an unattended room, a
  replayed recording — is exactly the attack. This is a hard rule, not a default.
- **Revoke is immediate and lease-independent.** A revoked grant must fail the *very next*
  read, including mid-turn. Therefore: **grants are consulted at use, never cached at session
  start.** A cached grant is a revoke that didn't happen.
- Expiry is mandatory on `source:*` grants (30 days is a starting proposal, not a verified
  number). Session/project grants may be open-ended.

### Storage

Mirror B6's proven pattern rather than inventing one: an atomic snapshot plus an append-only
trail, written under the same short-lived `O_EXCL` lock discipline
(`kernel/session_control.py:361-403`):

```
~/.amplifier/ambient/
    grants.json          # current grants (atomic write under lock)
    grants-audit.jsonl   # append-only: created / revoked / expired / denied
```

Grants are **per-user, not per-session** — a mail grant spans sessions — but every *use* is
attributed into the consuming session's own `control-audit.jsonl`, so a session's trail
remains a complete account of what was done on its behalf.

### Mapping onto B6 attribution

Every ambient action carries a B6 `Actor` (`kernel/session_control.py:105-159`). Two
extensions are required, and both are honest gaps rather than assumptions:

- **E1 — authenticated principal → actor, with provenance.** B6 records `kind` as an
  unverified claim and explicitly names B8 as the layer that maps an authenticated principal
  onto it (`kernel/session_control.py:108-118`). The ambient layer must authenticate at the
  adapter boundary and mint the `Actor`, **and** the record needs an additive `actor.auth`
  provenance field (e.g. `"auth": {"method": "device-token", "verified": true}`). Without
  provenance the audit trail cannot distinguish an authenticated human on a phone from a
  process that typed `kind:"human"` — which makes the trail non-probative exactly where AC4
  says it must be attributable.

  **This is a security blocker, not a nicety.** B6's takeover rule — `human` always beats
  `automation` (`kernel/session_control.py:82-86`) — is a *courtesy* over a local pipe whose
  peer the OS established. Over a network it becomes a privilege boundary: an unauthenticated
  adapter that can assert `kind:"human"` can seize the write lease out from under a real
  person's automation. **No networked adapter may ship before E1.**

- **E2 — audit vocabulary for cross-context access.** B6's action list is closed at thirteen
  session-control actions (`docs/SESSION-CONTROL.md:147-150`) and has no entry for reading an
  external source. Required additive actions, written through the same `_audit` path
  (`kernel/session_control.py:573-604`) so cross-context access lands in the *same* trail as
  session control: `source.read` · `source.send` · `source.denied` · `grant.created` ·
  `grant.revoked`. Each carries `grant_id` in `detail`, so "which grant authorized this" is
  answerable after the fact — that is what makes AC4's "attributable" real rather than
  aspirational.

### What reduces with no extension

Session and project scopes ride entirely on what exists: `session:<sid>:control` is B6's
lease/pause/handoff surface; `session:<sid>:write` is B6's `authorize()` gate
(`kernel/session_control.py:1018-1077`); attribution is `control-audit.jsonl`. Only the
*external source* families need new machinery.

---

## 2. Confirmation policy (AC1)

> **AC1** — a voice/conversational request is echoed back as a concise, **editable**
> interpretation before consequential work begins.

### What counts as consequential

A request is **consequential** if any of these hold:

1. **It writes outside the transcript** — file writes, shell exec, git operations.
2. **It is externally visible** — sends a Teams message or mail, creates an issue/PR, calls
   any third-party write API. This is its own class because it cannot be un-sent, and because
   the blast radius includes people who never consented to the delegation.
3. **It is irreversible or expensive to reverse** — delete, force-push, spend.
4. **It spans sessions** — any follow-on fan-out across multiple sessions (§4, AC2).
5. **It consumes a `source:*` grant** — *including reads.* Reading someone's mail is itself a
   privacy act, and it is the step where a misheard selector does quiet damage.

**Not** consequential: read-only inspection of the user's own sessions, status questions, and
`history.replay` — which B6 already guarantees is a read op that never touches the lease or
the transcript (`docs/SESSION-CONTROL.md:186-192`).

**Voice raises the bar with no exemption.** ASR is lossy and the channel is eyes-free, so in
v1 *every* voice-initiated consequential request is echoed. There is no trusted-speaker
bypass. If one is ever proposed it is a separate decision with its own review — Open
question 5.

### What the echo contains

An **InterpretationRecord**, ordered for speech (most decision-relevant first) and capped so
it can be spoken in one breath:

1. **Verb + object, one line** — "Reply to Dana's thread confirming Thursday."
2. **Target session(s)** — the B6 attach ref (`kernel/session_control.py:197-206`) plus a
   human-readable title, so "which session" is never ambiguous.
3. **Grants it will consume** — source + verb + selector, named ("your Outlook inbox, read
   only, Dana's thread").
4. **Reversibility class** — reversible · externally visible · irreversible.
5. **Explicit negative scope** — what it will *not* do. The cheapest guard against silent
   over-reach, and the field an ASR error is most likely to expose.
6. **The editable fields, enumerated** — so "change the …" has a closed vocabulary to hit.

### How the edit round-trips

Three responses only: `confirm` · `amend(field, value)` · `cancel`.

- **`amend` returns a NEW interpretation with a new id.** It never mutates in place. An
  interpretation you can mutate is an interpretation you cannot audit — the record the user
  agreed to must be exactly the record that executes.
- **Interpretations expire** (10 minutes proposed; unverified). Expiry is `cancel`.
- **`cancel` and expiry are both audited**, not just `confirm`. What the assistant *didn't* do
  on your behalf is part of the account.

### What it reduces to, and what it doesn't

The *gating* is already built. B6's `pause()` parks the write lane and mints a durable
one-shot handoff with a ref and a runnable attach command
(`kernel/session_control.py:838-903`); while paused, `authorize()` denies **every** write with
`session_paused` (`:1047-1051`); `claim_handoff()` is one-shot and races safely, so two people
answering the same prompt cannot both believe they own it (`:905-975`). So: an interpretation
awaiting confirmation **parks the session via `session.pause`**, and confirming is
`handoff.claim` + `submit`. Nothing can slip through while the human is deciding.

**E3 — the payload is the gap.** B6's handoff carries free-text `reason` and `note`
(`kernel/session_control.py:868-875`). A structured proposal with enumerated editable fields
is not expressible. **Do not JSON-stuff the `note` field** — that turns a human-readable field
into an untyped side channel and guarantees drift. Required extension: an `interpretation`
record in the ambient layer, keyed by the handoff id, with `interpretation.propose / amend /
confirm / cancel`. Gating reuses B6 unchanged; only the payload is new.

**Exactly-once confirm** rides B6's `idem` keys (`kernel/session_control.py:605-632`), so a
"yes" repeated over a flaky mobile link cannot double-execute. Honest bound: the ring holds
128 entries and rejections are deliberately not remembered (`:72-74`;
`docs/SESSION-CONTROL.md:174-176`) — see Risks.

### Redaction in ambient notifications

The local ladder already sanitizes and caps notification text (`ui/notifications.py:80-81`,
`:171-203`). Off-machine push is a *different device's* tray, owned by a module outside the
app kernel (`ui/notifications.py:8-13`, `:45-46`), delivered over a topic that is a shared
secret and world-readable if public (`docs/SETTINGS.md:215`).

**Policy: ambient notifications carry a pointer, not content.** By default an off-machine
notification contains only the attention `reason`, the session title, and the attach ref.
Never message bodies, file contents, diffs, credentials, or model output. Content appears only
after the user opens the session on an authenticated surface. Treat push as an **untrusted
broadcast channel** and design the payload as if a stranger will read it — because on a public
topic one can.

---

## 3. Mobile reply correlation (AC3)

> **AC3** — a mobile notification or quick reply can answer a pending clarification and return
> control to the same session.

### The two keys

- **Correlation key: B7's `event_id`** — stable, derived from `(session_id, reason,
  occasion)`, idempotent by construction, so a re-render or a reconnect cannot mint a second
  identity for the same question.
- **Re-entry key: B6's handoff ref** — `amplifier-session:<sid>#<handoff>`
  (`kernel/session_control.py:197-206`), with its runnable `attach_command` (`:220-223`).

### The flow

1. Session needs a human → `AttentionCenter.note(session_id, "awaiting_clarification",
   occasion)` → record with `event_id`.
2. If the question blocks work → also `session.pause(...)` → handoff id + ref
   (`kernel/session_control.py:838-903`). The ambient layer binds `(event_id ↔ handoff_id)` in
   its correlation table.
3. Push payload = `event_id` + attach ref + a redacted one-liner (§2). Nothing else.
4. Reply arrives → the adapter **authenticates the principal** (E1) → resolves `event_id` →
   finds the handoff → `handoff.claim(handoff_id, actor)`, which clears the pause and grants
   the lease in one step (`kernel/session_control.py:957-975`) → `submit` carrying that lease
   and an `idem` key → `AttentionCenter.acknowledge(session_id)`.
5. Result: control is back in **the same session**, held by **the same authenticated human**,
   with the reply attributed in `control-audit.jsonl`. A second reply to the same notification
   conflicts with `handoff_claimed` rather than double-answering — B6 already guarantees this
   (`:930-941`).

### The ntfy boundary, stated plainly

B7 documented that push fires from an external hook off the raw kernel event and has no
acknowledgement channel back. Two distinct consequences follow, and they need different
answers.

**(a) The notification cannot name what it is about.** Because the hook fires off the raw
kernel event rather than the `AttentionRecord`, the payload carries no `event_id`. Without
that, a reply has nothing to correlate *to*. → **E4: route push through the attention record
so the payload carries `event_id` + attach ref.** Until E4 lands, mobile reply correlation is
not implementable — not "harder", *not implementable*. This is the cheapest unblocking change
on the whole track.

**(b) There is no ingress at all.** ntfy is publish-only from this system's perspective. A
"quick reply" from a phone would publish to a topic nothing here subscribes to.

| Option | Verdict |
|---|---|
| **(a) Subscribe to an ntfy reply topic from a local ambient daemon** | **Rejected.** The topic is a shared secret and a public topic is world-readable (`docs/SETTINGS.md:215`). That makes it an unauthenticated write path into a live session. A world-readable channel must never be a write path — full stop. |
| **(b) An authenticated HTTPS ingress the mobile adapter posts to** | **The target.** Correct and attributable, but the largest new surface in this document: a reachable service with its own auth and its own operational burden. → **E7**, explicitly out of v1. |
| **(c) No ingress: push stays notify-only; the reply happens when the user opens the authenticated app**, which reads the pending attention record and offers the interpretation | **Recommended for v1.** Delivers "the notification takes you to the right pending question in the right session" — the whole correlation value — with zero new network surface. It is not *quick* reply; it is *one-tap-to-the-right-place* reply. Say that honestly rather than claiming AC3 in full. |

**E5 — durability.** As published in PR #202, `AttentionCenter` is a UI-layer object in
`ui/notifications.py` with no documented durable store. A reply arriving in a **different
process** — a daemon, a re-launched TUI, a bridge — cannot see it. Required: per-session
durable attention records (an `attention.jsonl` beside `control.json`) so `current(session_id)`
is answerable cross-process. This is not new invention: it is exactly the move B6 already made
for control state (`docs/SESSION-CONTROL.md:30-43`), so the pattern is proven in-tree. *If B7
already persists records and the PR body simply didn't say so, E5 collapses to "confirm and
document the durability guarantee" — verify before building.*

---

## 4. Cross-session activity history (AC2, AC5)

> **AC2** — sequence approved follow-on actions and report status across multiple sessions.
> **AC5** — the user can open the underlying session and inspect what the assistant did, why
> it paused, and what remains.

### The activity model is a projection, not a new write path

Everything needed is already written to disk by B6, B7 and the ledger. The activity model is a
**read-side projection** over those files — which means it cannot drift from the truth, because
it *is* the truth, re-read:

| Field of an `ActivityRow` | Source |
|---|---|
| session ref, project, title | `kernel/session_control.py:197-206`; `kernel/persistence.py:138-142` |
| state: running / paused-awaiting-you / idle / failed | `control.json` `paused` flag + lease presence (`docs/SESSION-CONTROL.md:36-40`) |
| who holds the pen | `control.json` lease actor |
| **why it paused** | the last `session.paused` audit entry's `detail.why` (`kernel/session_control.py:886`) |
| what the assistant *did* | `ui-events.jsonl` (`kernel/persistence.py:49`, `:301-344`) |
| what remains | last plan/todo state in `ui-events.jsonl` |
| whether it needs you | B7 `AttentionRecord` for that session |
| the full account | `control-audit.jsonl` via `audit_entries()` (`kernel/session_control.py:491-512`) |

**E6 — discovery is the one gap.** `SessionStore.list_sessions()` enumerates a *single* project
(`kernel/persistence.py:345`); there is no cross-project registry. The fix is deliberately the
cheap one: **walk `~/.amplifier/projects/*/sessions/*/`** and read each `control.json` plus the
tail of each audit file. That is O(sessions) per refresh, cached on directory mtime. *No new
write contract, therefore nothing to keep in sync.* Add a write-side index only if the scan is
**measured** too slow — not before.

Scope note: this projection is per-user and local. It reads what that user's own filesystem
permissions already allow, and grants nothing new.

### AC5 reduces to zero extensions

The strongest thin-adapter case in the document — it works over B6 **today**:

1. Take `attach_command` straight from the row (`kernel/session_control.py:220-223`).
2. `history.replay` as an **observer** — read-only, streams the durable ledger, never touches
   the lease, every record flagged `replay` so nothing is double-counted
   (`docs/SESSION-CONTROL.md:178-195`). You can inspect a session someone else is driving
   without disturbing them.
3. *Then*, if you want the pen: `lease.acquire`, or `handoff.claim` if it is parked, or
   `lease.takeover` — where being human always wins (`kernel/session_control.py:767-837`).

"Open the session and see what it did, why it paused, and what remains" is already a supported
operation. The ambient layer only has to *route* to it.

### AC2 — sequencing follow-on actions across sessions

A **FollowOnPlan** is an ordered list of steps, each `(session_ref, interpretation_id,
payload)`. The coordinator executes one step at a time:

```
lease.acquire(actor, ttl)  →  submit(lease, idem=step_id)  →  await turn end  →  lease.release
```

Every step is individually gated, attributed and idempotent by ops that already exist. **On any
`control.conflict` the plan stops** — it does not retry blindly — and raises an attention record
naming the step and the holder. B6's own guidance says exactly this: "treat `control.conflict`
as authoritative — re-read `lease.status`, never retry blindly"
(`docs/SESSION-CONTROL.md:209-210`). A human who grabbed the pen mid-plan is a *signal*, not an
obstacle to route around.

The only new state is the plan object itself — a queue in the ambient layer, no B6 contract
change. Note this is a genuinely multi-session capability, which is why §2 classes it as
consequential and requires the echo to name **every** target session before the first step runs.

---

## Required contract extensions (the honest gap list)

Nothing below is assumed to exist. Each is a real gap between what B6/B7 provide today and what
this design needs.

*(Status column added 2026-08-04 — see "Implementation status" above for detail.)*

| ID | Gap | Owner | Blocking | Status |
|---|---|---|---|---|
| **E1** | Authenticated principal → `Actor`, plus an additive `actor.auth` provenance field. B6 records `kind` as an unverified claim and names B8 as the mapper (`kernel/session_control.py:108-118`; `docs/SESSION-CONTROL.md:155-158`). | new adapter boundary + additive B6 field | **Hard blocker for any networked adapter.** Without it `kind:"human"` is spoofable and B6's human>automation takeover becomes a privilege-escalation path. | **consumed, not owned** (PR #230) |
| **E2** | Permission-grant store + `source.*` / `grant.*` audit actions. B6's action vocabulary is closed at thirteen session-control actions (`docs/SESSION-CONTROL.md:147-150`). | new ambient store; additive to B6's vocabulary | AC4 | **built** |
| **E3** | Structured, editable interpretation record + `interpretation.propose/amend/confirm/cancel`. B6's handoff payload is free-text `reason`/`note` (`kernel/session_control.py:868-875`). | ambient layer (gating reuses B6 pause/claim unchanged) | AC1 | **built** |
| **E4** | Push payload must carry B7's `event_id` + attach ref. Today ntfy fires from an external hook off the raw kernel event, outside the record (PR #202). | `hooks-notify-push` wiring / B7 routing | AC3 — **nothing in §3 works without it** | **delivered by B7** |
| **E5** | Durable, cross-process attention records (`attention.jsonl` beside `control.json`). As published, `AttentionCenter` is a UI-layer object; another process cannot read it. **Verify against B7 before building** — may collapse to a documentation fix. | B7 (mirrors B6's proven durability pattern) | AC3 | **delivered by B7** |
| **E6** | Cross-project session discovery. `SessionStore` enumerates one project (`kernel/persistence.py:345`). Cheap fix: read-side scan, no new write contract. | read-side projection | AC2/AC5 *breadth* (single-project already works) | **built** (scan, as recommended) |
| **E7** | An authenticated inbound reply channel. ntfy is publish-only; its topic is a shared secret and world-readable when public (`docs/SETTINGS.md:215`). | new service — largest new surface, **explicitly out of v1** | *quick* reply only; reply-on-open needs nothing | **security core built; no listener ships** |
| **E8** | Teams/Outlook connectors. Shape and permission requirements are specified in §1; **concrete API surfaces, delegated permission scopes and consent flows are deliberately unverified and OPEN** — Open question 1. | new source modules | AC4's Teams/Outlook instance | **port + local impl only — real connector genuinely external** |

### What needs **no** extension (the thin-adapter proof)

These already reduce to B6/B7 as they stand today:

- Observer reattach and inspection — `history.replay`, read-only, lease untouched.
- A human interrupting or taking over anything — `lease.takeover`, human always wins.
- Sequencing follow-ons across sessions — per step: `acquire` → `submit(idem)` → `release`.
- Gating a session while a human decides — `session.pause` → `authorize()` denies all writes.
- Exactly-once confirmation — `idem` keys with durable replay.
- Full attribution of every session write — `control-audit.jsonl`.
- De-duplicated, acknowledgeable "needs you" — B7 `note()` / `acknowledge()`.

Six of the eight extensions are additive records or wiring. Only **E7** (ingress service) and
**E8** (connectors) are genuinely new surfaces — and both are sequenced last.

---

## Phasing (direction, not a commitment)

Ordered so each phase is independently useful and nothing is built on an assumption:

| Phase | Content | Why here |
|---|---|---|
| **0** | This document; review gate | Contracts before clients |
| **1** | Identity + durability: **E1**, **E5** | No user-visible feature, unblocks everything, and E1 is a security precondition |
| **2** | Activity model, read-side: **E6** | First real user value — the fleet view (MJ's framing) — and it needs no voice at all |
| **3** | Interpretation / confirmation loop: **E3**, **text-first in the TUI** | Prove the echo/edit loop where it is cheap to observe and cheap to fix |
| **4** | Permissions + **one** source connector: **E2**, **E8** | One connector proves the permission model; two prove nothing extra |
| **5** | Notification correlation: **E4** → reply-on-open | Most of AC3 with zero new network surface |
| **6** | Voice adapter; then **E7** quick-reply ingress **last** | Biggest surfaces, gated on everything below being real |

**Ordering principle: voice is last, because voice is an adapter.** Build it first and it will
grow the permission, confirmation and history logic that belongs underneath it — precisely the
failure this document exists to prevent.

---

## Test & validation strategy (for whenever this is built)

Matching the house discipline — offline, pure-logic where possible, injected clocks, tested at
the right layer (`docs/DEVELOPMENT.md`, test-suite map):

- **Grants** — a pure `authorize_source(grants, principal, scope, verb, selector, now) ->
  Decision`, table-driven. Must include an **empty-grants case proving deny-by-default**, an
  expired-grant case, and a revoked-mid-turn case.
- **Interpretation state machine** — `propose → amend → confirm | cancel | expire` with an
  injected clock, the way B6's own state machine is unit-tested
  (`docs/SESSION-CONTROL.md:15-16`). Assert `amend` mints a new id and never mutates.
- **Correlation** — `event_id` → handoff → claim → submit returns the lease to the replying
  principal; a **second** reply on the same event conflicts with `handoff_claimed`.
- **Redaction** — a golden test asserting the push payload contains **only** an allowlist of
  fields (allowlist, never a denylist — a denylist passes every field you forgot).
- **Activity projection** — build a `tmp_path` tree of fake session dirs and assert row states;
  an unreadable session dir must degrade to a **partial view, never an exception** — the same
  posture B6 takes on an unwritable audit file (`kernel/session_control.py:597-603`).
- **Offline** — all of the above runs with no network and no credentials; source connectors are
  faked at the module seam, the way `tests/test_runtime_offline.py` fakes providers.

---

## Risks & mitigations

| Risk | Evidence | Mitigation |
|---|---|---|
| ASR mishears a destructive verb; the echo is *also* spoken, so the same channel confirms its own error | design | Class-3 (irreversible) actions require confirmation on a **visual** surface, never voice alone |
| A networked adapter asserts `kind:"human"` and seizes the lease from a human's automation | `kernel/session_control.py:82-86`, `:108-118` | **E1 is a hard gate** — no networked adapter ships before it |
| ntfy topic leaks / a public topic is world-readable | `docs/SETTINGS.md:215` | Push carries pointers only; ntfy is never a write path (option (a) rejected in §3) |
| Idem ring holds 128 entries and rejections are not remembered — a confirm retried after 128 intervening keyed ops re-executes | `kernel/session_control.py:72-74`; `docs/SESSION-CONTROL.md:174-176` | Keep the confirm window short (interpretation expiry ≪ ring turnover); measure before ambient volume grows; only then consider a larger/partitioned ring |
| Grants cached at session start survive a revoke | design | Consult grants **at use**, never at start — stated as a rule in §1 |
| Activity scan cost grows with session count | `kernel/persistence.py:345` | mtime-keyed cache; add a write-side index only if measured too slow |
| A pause parks the session while the human never answers | `kernel/session_control.py:1047-1051` (paused denies all writes) | Interpretations expire; expiry cancels **and resumes**, so a forgotten voice request cannot wedge a session |
| This doc predates any implementation and cites two very fresh contracts | B6 has landed on `main` (PR #203); B7 (PR #202) is still open, not yet merged | Re-verify every B6/B7 cite before Phase 1; treat E5 as "verify first, then build" |
| Teams/Outlook specifics get invented under delivery pressure | — | They are marked OPEN below and sequenced into Phase 4, behind the permission model that constrains them |

---

## Acceptance mapping

| AC | Where specified | Reduces to | Extension needed |
|---|---|---|---|
| **AC1** — editable interpretation echo before consequential work | §2 | `session.pause` → `authorize()` denies all writes → `handoff.claim` + `submit(idem)` | **E3** (structured payload) |
| **AC2** — sequence approved follow-on actions, report status across multiple sessions | §4 | per step: `lease.acquire` → `submit(idem)` → `lease.release`; stop on `control.conflict` | **E6** (cross-project discovery) |
| **AC3** — mobile notification / quick reply answers a pending clarification and returns control to the same session | §3 | B7 `event_id` as correlation key + B6 handoff ref for re-entry + `acknowledge()` | **E4** (payload carries `event_id`), **E5** (durable records); **E7** only for *quick* reply — v1 ships reply-on-open and says so |
| **AC4** — cross-context access explicit, permissioned, attributable, limited to enabled sources | §1 | B6 `Actor` + `control-audit.jsonl` via `_audit()` | **E1** (authenticated principal), **E2** (grants + audit vocabulary), **E8** (connectors) |
| **AC5** — open the underlying session; inspect what it did, why it paused, what remains | §4 | `attach_command` → `history.replay` (read-only observer) → optional `lease.acquire` / `handoff.claim` | **none** — works over B6 today |
| **Design note** — preserve Brian's conversational-delegation and MJ's perceptual-visibility framings, un-collapsed | "The two framings" | §2/§3 are the delegation spine; §1/§4 the visibility spine; both share the B6 audit trail and the B7 attention record | — |
| **Design note** — voice/mobile clients remain thin adapters | "Architecture" + extension table | The adapter rule plus the reduction test on every capability | 6 of 8 extensions are additive records/wiring; only E7 and E8 are new surfaces, both sequenced last |

---

## Open questions for review

1. **Teams / Outlook API specifics are deliberately unverified.** Which API surface, which
   delegated permission scopes, which consent flow, and whether tenant admin consent is
   required — none of that is asserted here, because none of it was verified. §1 specifies the
   integration **shape** and its permission/attribution requirements; the concrete API work is
   E8 and needs its own investigation before any estimate is believed.
2. **Where does the ambient layer run?** In-TUI worker vs a local daemon. This decides the shape
   of E5, E6 and E7, and it is the largest unstated architectural fork in this document.
3. **Is reply-on-open (§3 option (c)) acceptable for AC3's "quick reply"**, or must the
   authenticated ingress (E7) land in the same increment? That is the difference between a small
   increment and a new service.
4. **Grant scope: per-user global or per-project?** This document proposes per-user for
   `source:*` (a mail grant naturally spans sessions), which is the more permissive choice and
   deserves explicit sign-off.
5. **Does voice ever get an unattended mode** — a narrow allowlist with no echo — or is the
   confirmation echo unconditional forever? v1 says unconditional. Changing that is a separate
   decision with its own review.
