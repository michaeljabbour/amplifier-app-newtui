# Spike + Decision: `microsoft/amplifier-agent` as the runtime integration layer

- **Issue:** [#54](https://github.com/michaeljabbour/amplifier-app-newtui/issues/54) — *Evaluate microsoft/amplifier-agent as the runtime integration layer (spike + decision doc)*
- **Status:** Decided — **Stay core-native (amplifier-foundation direct); do NOT adopt now.** Keep a scoped door open for a headless-only `run` adapter, gated on three concrete upstream capabilities.
- **Date:** 2026-07-22
- **Type:** Spike / decision doc (no code changes — this doc is the deliverable). Intended durable home when merged: `docs/plans/2026-07-22-amplifier-agent-eval.md`. **No ADR** is cut, because the recommendation is to *not* change the runtime layer.
- **Evidence base:** read-only checkout of `origin/main` at `/Users/michaeljabbour/dev/newtui-wt/base`; shallow read-only clone of `microsoft/amplifier-agent` at `/tmp/amplifier-agent-eval-clone` (default branch, protocol `0.3.0`). Every `file:line` cite below was opened and verified during this spike.

---

## 1. Problem

The amplifier team asked (2026-07-22): *"Is there a reason not to use `microsoft/amplifier-agent` as the primary way to leverage the Amplifier layer(s)? That is where we're putting all of our investment … thin adapters to allow usage in other harnesses. Today you can call it like a chat completion endpoint for a quick wire-up."*

newtui today builds **directly on amplifier-core / amplifier-foundation**: it composes a wrapper bundle, attaches **in-process hook handlers** to the live coordinator, and normalizes the entire engine firehose into one typed `UIEvent` queue. Its differentiating features — token streaming with fence holdback, subagent lanes + live tail, a structured approval broker, step-boundary steering, rewind, truthful compaction/cost accounting, and an upstream-drift event canary — **all live on that firehose**.

The question is narrow and answerable: **which `amplifier-agent` consumption surface, if any, can carry newtui's event contract without regressing those features — and is the investment-sharing upside worth the integration cost?**

`amplifier-agent` exposes three surfaces (README; `docs/LAYERS_AND_RELEASES.md`):
1. **chat-completions HTTP** (`serve chat-completions` → `POST /v1/chat/completions`, SSE) — the "quick wire-up."
2. **stdio ndjson** (`run --output json --display ndjson`) — one turn per subprocess, JSON envelope on stdout, ndjson diagnostics on stderr.
3. **`amplifier_agent_lib` in-process** — transport-free Python the host can embed.

This spike assesses all three against newtui's `CONSUMED_EVENTS`, plus four cross-cutting concerns the issue calls out: **bundle-composition control**, **session-dir compatibility**, **cost/usage fidelity**, and a **prototype behind the existing `RuntimeAdapter` seam** ("count what breaks").

> **Method note (read-only constraint).** The task forbids code changes, so the "prototype behind `RuntimeAdapter`" was executed as a **static integration trace** — reading both public APIs end-to-end and enumerating each load-bearing subsystem that would break — rather than a running spike branch. That is sufficient here because the decisive finding is structural (an absent seam), which a static trace establishes definitively; it does not depend on runtime behavior. §7 lists what breaks.

---

## 2. Evidence

### 2.1 What newtui consumes today (amplifier-app-newtui @ `origin/main`)

- **The event contract is wide.** `kernel/queue_bridge.py:30` — `CONSUMED_EVENTS` enumerates **39** raw hook kinds (the issue's "~32 + delegate/*"): both streaming channels, tool lifecycle, content-block boundaries, `orchestrator:complete`, execution/turn lifecycle, provider telemetry (`provider:response/error/retry/throttle`), `context:compaction`, session lifecycle (`session:start/end/fork/resume`), approvals (`approval:required/granted/denied`), cancellation (`cancel:requested/completed`), the **subagent-lane family** (`task:*` **and** `delegate:agent_spawned/completed/resumed/cancelled/error`), `user:notification`, and `recipe:approval`.
- **One normalization boundary → 31 typed events.** `kernel/events.py` defines the frozen `UIEvent` union (31 `_Envelope` subclasses). ADR-0007 §"Event architecture": *"All amplifier-core events are normalized at exactly ONE boundary … Both channels consumed … Never reconstruct one from the other."* (`docs/decisions/ADR-0007-newtui-ground-up-architecture.md`).
- **A drift canary treats missing kinds as a defect.** `kernel/queue_bridge.py:110` — *"Anything else the engine publishes but `CONSUMED_EVENTS` does not name is upstream drift and must surface, not vanish."* newtui's contract is *maximalist by design*; a lossy upstream is a regression, not a convenience.
- **Hooks are attached in-process to the live coordinator.** `kernel/runtime.py:380` `class RealRuntime`; boot registers on the coordinator's `hooks`: the `QueueBridge` across all events (`runtime.py:622`), the drift canary (`:628`), `GovernanceHook` on `tool:pre` (`:634/:645`), the recipes bridge (`:661`), `StepBoundaryBridge` on `provider:request` (`:662/:673`), the `IncrementalSaver` (`:680`), and a `ClipboardImageInjector` on `provider:request` (`:691‑693`). It passes `approval_system`/`display_system` **into** `prepared.create_session(...)` (`kernel/session_factory.py:283‑285`).
- **Steering is a `provider:request` hook.** `kernel/steering.py:28` `EVENTS = ("provider:request",)`; `:6` returns `HookResult(action="inject_context", context_injection_role="user")`. Mid-turn injection has no equivalent across a subprocess or an Engine boundary.
- **Governance is two-axis on `tool:pre`.** `kernel/governance_hook.py:194` `EVENTS = ("prompt:submit", "tool:pre")`; maps trust decisions to `HookResult` `continue` / `ask_user` (structured approval) / `deny` (deny-and-continue). ADR-0007 §Approvals: the `ApprovalTicket` carries *"unique id, command, cwd, rule, capability class"* and verbatim "Allow once / Allow always / Deny" scoping — richer than a boolean accept/decline.
- **Rewind calls foundation directly.** `kernel/rewind.py:136` `fork_session`, `:184` `fork_session_in_memory` — confirm-then-trim forking with no surface on any `amplifier-agent` layer.
- **Session dir is `~/.amplifier/projects/...`.** `kernel/persistence.py:5‑8` and `:117` — `~/.amplifier/projects/<slug>/sessions/<id>/` with `transcript.jsonl` + `metadata.json` + **`ui-events.jsonl`** (append-only normalized UIEvents, ADR-0007 §9); cost re-seeds from that log on resume.
- **The insertion seam already exists.** `ui/runtime_adapter.py:41` `class RuntimeAdapter` (base contract; owns `queue`, `steering`, `needs_you`, `denial_log`), `:245` `class RealRuntimeAdapter`. ADR-0007 §Runtimes: *"the UI cannot tell demo from real"* — any runtime that can fill `asyncio.Queue[UIEvent]` and honor the adapter methods drops in here.
- **Bundle control is a hard requirement.** `bundle.md:12‑15` / `:101‑106` — newtui is *"a THIN WRAPPER"* that **suppresses printing hooks and OSC/BEL `hooks-notify` at boot** (built-in suppression list + `hooks.suppress`), keeps `hooks-logging` native, and adds its own stdout-free `hooks-notify-push`. `AGENTS.md:26` restates the non-negotiable: *"Never mount printing hooks … they write ANSI to stdout and corrupt the Textual screen."* Overlay + suppression + roster control are load-bearing, not incidental.

### 2.2 What `amplifier-agent` actually exposes (microsoft/amplifier-agent, protocol `0.3.0`)

- **The wire taxonomy is nine types, fixed.** `src/amplifier_agent_lib/protocol/notifications.py:29` `CANONICAL_DISPLAY_EVENTS = (result/delta, result/final, tool/started, tool/completed, progress, thinking/delta, thinking/final, usage, error)`, with the docstring: *"Adapters translate; they do NOT invent new types."* Plus two approval notifications (`ApprovalRequest*`/`ApprovalTimeout*`, same file).
- **The engine is transport-shaped.** `src/amplifier_agent_lib/engine.py:78` `class Engine` with `boot()` (`:113`), `submit_turn()` (`:185`), `dispatch()` (`:238`). `submit_turn` **returns only `TurnSubmitResult{reply, turnId, sessionId}`** — a final string; all streaming leaves through the injected `DisplaySystem`.
- **The only injectable protocol points are Display + Approval.** `src/amplifier_agent_lib/protocol_points/base.py:28` `DisplaySystem.emit(DisplayEvent)` (`:35`, one-way, 9-type), `:62` `ApprovalSystem.request(...)` (`:69`) returning `{accept|decline|cancel}` (`:40`). The base module states plainly: *"Spawn is NOT a protocol point. It is library-internal."*
- **The lib→wire translation is lossy (11 kernel hooks → 9 wire types).** `src/amplifier_agent_lib/bundle/hook_streaming.py` `mount()` registers **11** handlers (`tool:pre/post/error`, `content_block:start/delta/end`, `llm:response`, `llm:stream_block_delta`, `thinking:delta/final`, `orchestrator:complete`) and collapses them into the 9 wire types. Concretely it **drops**: streaming block **start/end** boundaries (only deltas survive), the entire **`delegate:*` subagent-lane lifecycle** (subagents survive only as an `agentName` *string* parsed from a `session_id` suffix — `_parse_agent_name`), `context:compaction`, `session:*`, `provider:retry/throttle`, `cancel:*`, `execution:*`, and `recipe:approval`. `orchestrator:complete` is reduced to a `usage` cost rollup.
- **The real in-process path is a private, bundle-hardwired factory.** `src/amplifier_agent_lib/_runtime.py` `make_turn_handler` (underscore-private module): it `load_and_prepare_cached()` **their** vendored bundle, creates a **fresh session per turn**, then on the coordinator registers `display.emit` (`:360`), `approval.request` (`:362`), **mounts their streaming hook** (`:368`), and registers `session.spawn` (`:429`). This is the only place arbitrary hooks get attached — and it is not public API; it is welded to their bundle, their streaming hook, and their session-per-turn model.
- **Their bundle is fixed and cache-keyed.** `LAYERS_AND_RELEASES.md` §2 — bundle `amplifier-agent-behavioral-anchor`, a fixed module roster (providers, `loop-streaming`, `context-simple`, a tool set, seven hooks, six vendored agents). `src/amplifier_agent_lib/bundle/cache.py:71` `load_and_prepare_cached(aaa_version)` keys on `sha256(bundle.md)`. `bundle/loader.py:23` accepts an override path, **but the Engine / `_runtime` / cache path uses the vendored bundle**; host input is limited to per-module config overlays via `merge_config` (D5, `_runtime.py:100‑116`). There is **no per-hook suppression seam** and no roster-replacement seam on the embed path.
- **Their state dir differs.** `src/amplifier_agent_lib/persistence.py:100` `amplifier_agent_home()` → `~/.amplifier-agent/` (override `$AMPLIFIER_AGENT_HOME`); `:130` `state_root()` → `<home>/state/`; sessions bucket under `state/workspaces/<slug>/sessions/<id>/`. No `ui-events.jsonl` equivalent.
- **Cost fidelity on the `usage` event is actually good but boundary-only.** `hook_streaming.py` `on_llm_response` / `on_orchestrator_complete` emit `usage` with `inputTokens/outputTokens`, optional `cost` **as a Decimal-string** (precision preserved), `cacheRead/WriteTokens`, and `sessionCostTotal` via `collect_contributions("session.cost")`. Per-subagent cost is tagged only by `agentName` string — coarser than newtui's per-lane Decimal ledger + resume re-seed.
- **Steering / rewind / cancel are absent from the lib surface.** A repo-wide grep of `src/amplifier_agent_lib/` for `provider:request`, `inject_context`, `fork_session`, `rewind`, and turn-level `interrupt`/`cancel` returns only approval-timeout `cancel` and crash-recovery transcript repair — **no mid-turn context injection, no fork/rewind, no turn interrupt**.

---

## 3. Event-contract gap table

Legend: **✓** carried faithfully · **~** partial / lossy · **✗** dropped. "ndjson" and "lib Engine" columns are identical in fidelity because the lib's Engine emits through the **same** 9-type `DisplaySystem` taxonomy the ndjson face serializes (`hook_streaming.py` is the shared translator).

| newtui `CONSUMED_EVENTS` (grouped) | chat-completions | ndjson `run` | `amplifier_agent_lib` (Engine) | Notes |
|---|:--:|:--:|:--:|---|
| `llm:stream_block_start` | ✗ | ✗ | ✗ | No open boundary on the wire |
| `llm:stream_block_delta` | ~ | ✓ | ✓ | Text ok; block index/identity lost |
| `llm:stream_block_end` | ✗ | ✗ | ✗ | **Fence-holdback consolidation depends on this** (ADR-0007 §Event) |
| `llm:stream_aborted` | ✗ | ✗ | ✗ | — |
| `tool:pre` → `tool/started` | ~ | ✓ | ✓ | `toolCallId` preserved |
| `tool:post` → `tool/completed` | ~ | ✓ | ✓ | — |
| `tool:error` → `error` | ~ | ~ | ~ | Collapsed into generic `error` |
| `content_block:start` / `:end` | ✗ | ~ | ~ | `end` → `result/delta` text only; `start` dropped |
| `orchestrator:complete` | ✗ | ~ | ~ | Reduced to a `usage` cost rollup |
| `execution:start` / `:end` | ✗ | ✗ | ✗ | — |
| `prompt:submit` / `:complete` | n/a | n/a | n/a | newtui synthesizes these itself |
| `provider:response` | ✗ | ~ | ~ | Surfaces as `usage` |
| `provider:error` | ✗ | ~ | ~ | Generic `error` |
| `provider:retry` / `:throttle` | ✗ | ✗ | ✗ | Lost provider-health telemetry |
| `context:compaction` | ✗ | ✗ | ✗ | **Truthful compaction accounting depends on it** |
| `session:start/end/fork/resume` | ✗ | ✗ | ✗ | — |
| `approval:required/granted/denied` | ✗ | ~ | ~ | `ApprovalSystem` = `{accept/decline/cancel}`; no `ApprovalTicket` rule/capability/cwd; no "Allow once/always" scoping |
| `cancel:requested` / `:completed` | ✗ | ✗ | ✗ | **No turn interrupt** (Esc / `TURN_ABORTED_MARKER`) |
| `task:*` (spawn/complete) | ✗ | ~ | ~ | Only an `agentName` string tag |
| `delegate:agent_spawned/completed/resumed/cancelled/error` | ✗ | ✗ | ✗ | **The subagent-lanes firehose — entirely absent** |
| `user:notification` | ✗ | ~ | ~ | Loosely → `progress` |
| `recipe:approval` | ✗ | ✗ | ✗ | No recipe approval gate on the wire |

**Tally:** of 39 consumed kinds, the 9-type surface carries ~5 faithfully, ~9 partially, and **drops ~25** — including every load-bearing differentiator: streaming boundaries, the `delegate:*` lane lifecycle, steering's `provider:request` seam, compaction, session lifecycle, cancel, and recipes. The gap list **decides feasibility**: no `amplifier-agent` surface can carry newtui's contract without regressing the product.

---

## 4. Options considered

### Option A — Adopt `amplifier-agent` as the primary runtime layer
Replace `kernel/runtime.py`'s foundation-direct boot with `amplifier_agent_lib.Engine` (or the ndjson subprocess) behind a new `RuntimeAdapter`.

- **Pros:** inherits upstream investment (credential handling, protocol evolution, bundle caching, state migration/repair); the amplifier team's stated direction; smaller kernel surface long-term.
- **Cons (decisive):** the Engine funnels everything through the **9-type `DisplaySystem`** (`notifications.py:29`), which is strictly narrower than `CONSUMED_EVENTS`. The one place arbitrary hooks attach (`_runtime.make_turn_handler`) is **private and welded to their bundle + streaming hook + session-per-turn**. You cannot get the raw firehose *and* keep upstream's convenience — the transport-free part of the lib (`prepared.create_session` + your own hooks) is **exactly what newtui already does**, so adopting it adds their bundle/state/taxonomy constraints while removing nothing we hand-roll. Steering, rewind, cancel, structured approvals, lanes, and compaction all regress (§3, §7). **Rejected.**

### Option B — Partial adopt: `amplifier-agent` for the headless `run` path only
Keep foundation-direct for the interactive TUI; add an `amplifier-agent`-backed adapter for the non-interactive `run`/`resume` CLI (`main.py`), where the full firehose isn't rendered.

- **Pros:** consumes upstream investment where fidelity matters least; a genuine, low-risk place to "call it like a chat completion endpoint"; keeps a live integration point warm so protocol drift is caught early; a friendly signal to the amplifier team.
- **Cons:** it forks reality — the headless path would run **their** bundle (different tool/agent roster than the TUI wrapper; no suppression/overlay control, `bundle/cache.py:71` + `_runtime.py`), persist to **their** state dir (`~/.amplifier-agent/state/workspaces/...`, `persistence.py:100/130`) instead of `~/.amplifier/projects/...` (`persistence.py:117`), and produce no `ui-events.jsonl`. Net: **a `run` session and a TUI session could not share history or resume each other**, and cost re-seed diverges. Real cost, thin benefit **today**. **Deferred, not taken now** (see §5 gating conditions).

### Option C — Stay core-native (amplifier-foundation direct), share findings upstream
Keep `kernel/` on amplifier-core/foundation. Treat `amplifier-agent` as a **peer consumer** of the same kernel, not a layer above newtui. Hand the amplifier team the §3 gap table and §8 asks so their "thin adapter for other harnesses" investment can eventually cover a firehose consumer like this TUI.

- **Pros:** zero regression to lanes/approvals/steering/rewind/cost/compaction/canary; keeps the single normalization boundary and drift canary intact; matches the standing "amplifier-native first" directive and the sanctioned APPLICATION_INTEGRATION_GUIDE app-side pattern; the `RuntimeAdapter` seam (`ui/runtime_adapter.py:41`) remains the clean future insertion point if/when upstream grows the surface.
- **Cons:** newtui keeps hand-rolling prepare/mount and does **not** inherit upstream's credential/state/protocol investment yet; ongoing responsibility to track core hook-event drift (already mitigated by the canary, `queue_bridge.py:110`); requires a good-faith written hand-back so the divergence is a documented, revisited decision rather than a silent fork.

---

## 5. Decision / Recommendation

**Adopt Option C: stay core-native (amplifier-foundation direct) as the primary and interactive runtime layer. Do not adopt `amplifier-agent` now.**

Rationale in one line: **newtui is a firehose consumer; every `amplifier-agent` surface is a 9-type funnel, and the lib exposes no public seam to attach our own hooks or inject our own bundle without reimplementing foundation-direct anyway.** The three surfaces resolve cleanly:

- **chat-completions face → non-fit** (confirmed): OpenAI-shaped assistant text + reasoning only; no hooks, lanes, approvals, or steering on the wire. This is the right tool for "quick wire-ups," not for this TUI. It becomes viable only if the wire grows those event families.
- **stdio ndjson face → non-fit for the contract**: the ndjson diagnostics stream *is* the 9-type taxonomy (`notifications.py:29`); the §3 gap table shows it drops ~25 of 39 kinds, including the entire `delegate:*` lane lifecycle. Fine for headless one-shot text; cannot drive the transcript.
- **`amplifier_agent_lib` in-process → non-fit today, and the reason is structural**: the Engine emits through the same 9-type `DisplaySystem`; the only hook-attachment site (`_runtime.make_turn_handler`) is private and welded to their bundle/streaming-hook/session-per-turn. Embedding it either accepts the lossy taxonomy or bypasses the Engine to call `prepared.create_session()` with our own hooks — which is precisely what `kernel/runtime.py` already does against foundation.

**Keep Option B on the shelf, explicitly gated.** Revisit a headless-only `run` adapter when `amplifier_agent_lib` ships **all three** of:
1. a **public** session-construction API that accepts a **caller-supplied prepared bundle** (roster + per-hook suppression), not just their vendored `bundle.md`;
2. a **host-configurable state root** so sessions can live under `~/.amplifier/projects/...` and interoperate with TUI resume; and
3. either raw hook pass-through **or** an expanded display taxonomy covering `delegate:*`, block boundaries, and `context:compaction`.
Until then, Option B forks bundle + state + history for marginal benefit and is not worth the split.

This is the honest "stay core-native with rationale to share back" outcome the issue lists as a valid recommendation — chosen on evidence, not inertia.

---

## 6. Implementation plan (recommended option)

Option C is a **no-runtime-change** decision, so the "implementation" is (a) recording the decision and its gate, and (b) a concrete, phased hand-back to the amplifier team. Concrete paths:

**Phase 0 — Land the decision (this repo).**
- Promote this file to `docs/plans/2026-07-22-amplifier-agent-eval.md` (dated-plan convention).
- Add a one-line pointer under "Runtimes" in `docs/decisions/ADR-0007-newtui-ground-up-architecture.md` — *"amplifier-agent evaluated 2026-07-22; stay core-native, see docs/plans/2026-07-22-amplifier-agent-eval.md"* — so the choice is discoverable without reopening the spike. **No ADR supersession** (ADR-0007 §Runtimes stands unchanged).
- Note in `docs/ARCHITECTURE.md §1` that `ui/runtime_adapter.py:RuntimeAdapter` is the sanctioned insertion point for any future amplifier-agent-backed runtime.

**Phase 1 — Keep the seam honest (cheap insurance, already mostly present).**
- Confirm the drift canary (`kernel/queue_bridge.py:110`, `:628`) remains green against the installed core; it is our early-warning that upstream event families changed. No new code expected.

**Phase 2 — Hand-back to the amplifier team (the actual deliverable of adopting C).**
- File an upstream issue on `microsoft/amplifier-agent` containing the §3 gap table and the §8 asks, framed as "what the wire/lib must grow to carry a firehose TUI." Reference `notifications.py:29`, `protocol_points/base.py:28`, and `_runtime.py:make_turn_handler` so the asks are concrete.

**Phase 3 — Gated, optional headless adapter (only if §5 gates clear).**
- New file `src/amplifier_app_newtui/ui/runtime_adapter_agent.py` — `class AmplifierAgentRuntimeAdapter(RuntimeAdapter)` used **only** by the `run`/`resume` CLI in `src/amplifier_app_newtui/main.py`, never by the interactive TUI.
- New file `src/amplifier_app_newtui/kernel/agent_bridge.py` — translate the (by-then-expanded) display taxonomy back into `kernel/events.py` `UIEvent`s at the one boundary; wire the caller-supplied bundle from `kernel/config.py` and point state at `~/.amplifier/projects/...`.
- Contract test replaying a captured session through both runtimes to prove headless↔TUI resume parity **before** shipping.

---

## 7. Test & validation strategy

**What this spike ran (read-only static trace behind the seam — "count what breaks").** Tracing an `AmplifierAgentRuntimeAdapter(RuntimeAdapter)` onto `Engine.boot`/`submit_turn`, these **eight** load-bearing subsystems break, each tied to a concrete cite:

1. **Transcript fidelity** — `submit_turn` returns only `reply` (`engine.py:185`); the `UIEvent` queue would be fed solely by 9-type `DisplayEvent`s, so the reducer loses `StreamBlock*` boundaries, `delegate:*` lanes, `ContextCompaction`, and session-lifecycle blocks.
2. **Steering** — no `provider:request` seam across the Engine boundary; `kernel/steering.py:28` `inject_context` cannot fire. (ADR-0007 §Steering violated.)
3. **Approvals** — `ApprovalSystem.request` yields `{accept/decline/cancel}` (`protocol_points/base.py:40`); the `ApprovalTicket` (rule, capability class, cwd, "Allow once/always") from `governance_hook.py` has nowhere to ride.
4. **Rewind** — no fork surface on the Engine; `kernel/rewind.py:136/184` calls foundation directly and would be unavailable.
5. **Interrupt** — no turn cancel across `submit_turn`; Esc / `TURN_ABORTED_MARKER` (`kernel/runtime.py`) unsupported.
6. **Bundle composition** — Engine loads their bundle (`bundle/cache.py:71`); our wrapper roster + suppression + overlays (`bundle.md:12‑15`, `AGENTS.md:26`) don't apply.
7. **Session dir / persistence** — state lands in `~/.amplifier-agent/state/...` (`persistence.py:100/130`), diverging from `~/.amplifier/projects/...` (`persistence.py:117`); cost re-seed from `ui-events.jsonl` breaks.
8. **In-session ops** — `/model`, `/effort`, `/compact`, `/tools`, `/agents` run over the **live coordinator** (`kernel/session_ops.py`); the Engine exposes no coordinator handle, so these commands break.

**Validation for the recommended path (Option C):**
- **No regression by construction** — no runtime change ships, so the existing suite (`uv run pytest -q`, ruff, pyright — `AGENTS.md`) is the guardrail; golden transcript files stay untouched.
- **Drift canary** remains the standing contract test that upstream core hasn't silently changed the firehose (`queue_bridge.py:110`).

**Validation that would gate Phase 3 (if pursued):**
- A recorded-session **contract test** replaying one captured `ui-events.jsonl` through both `RealRuntimeAdapter` and `AmplifierAgentRuntimeAdapter`, asserting transcript + cost parity and **cross-runtime resume**. Ship Phase 3 only when it passes.

---

## 8. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Staying core-native diverges from the amplifier team's investment direction | High | Med | Explicit written hand-back (§6 Phase 2) with the §3 gap table + asks below; re-evaluate on each `amplifier-agent` minor that touches the display taxonomy or exposes a bundle/hook seam. |
| Upstream core changes a hook event newtui consumes | Med | Med | Drift canary already fails loudly (`queue_bridge.py:110`); pinned `amplifier-core>=1.6.0` (ADR-0007 §Stack); lockfile committed. |
| "Quick wire-up" pressure to ship chat-completions anyway | Med | High | This doc records the concrete non-fit (§3); revisit only if the wire grows hook/lane/approval/steering events. |
| Missed a private/undocumented lib seam for raw hooks | Low | Med | Traced `engine.py`, `_runtime.py`, `protocol_points/base.py`, `hook_streaming.py`, `spawn.py`, `wire_approval_provider.py`; the sole hook-attach site is the private, bundle-welded `make_turn_handler`. If upstream later publicizes an equivalent, Phase 3 becomes viable. |
| Headless-only adapter (if built) forks history/state | Med (only in Phase 3) | High | §5 gate #2 (host-configurable state root) is a hard precondition; Phase 3 blocked on the cross-runtime resume parity test. |

**Concrete asks to hand back to the amplifier team (so a firehose TUI can adopt later):**
1. Expose the **raw kernel hook firehose** (or a display taxonomy covering `delegate:agent_*`, `content_block:start/end`, `context:compaction`, `session:*`, `cancel:*`, `provider:retry/throttle`, `recipe:approval`) — not just the 9 canonical types.
2. Provide a **public** session-construction API on `amplifier_agent_lib` that accepts a **caller-supplied prepared bundle** (roster + per-hook suppression), decoupled from the vendored `bundle.md`.
3. Add wire/lib support for **mid-turn steering** (`provider:request` inject), **fork/rewind**, and **turn interrupt/cancel**.
4. Make the **state root host-configurable** so sessions can live under a host's chosen path and interoperate with the host's resume/cost model.
5. Carry **structured approvals** (a ticket with rule/capability/cwd and once/always scoping), not just `{accept/decline/cancel}`.

---

## 9. Acceptance mapping

The issue has no explicit "Acceptance" header; the contract is derived from **"The evaluation to run (spike)"** and **"Deliverable."** Each bullet, mapped to where it is satisfied:

| Acceptance bullet (from issue #54) | Where addressed |
|---|---|
| **chat-completions face** — document as non-fit unless the wire grows hook/lane/approval/steering | §3 (row-level), §4 A, §5 bullet 1 — non-fit confirmed with cites (`notifications.py:29`). |
| **stdio ndjson face** — enumerate what ndjson carries vs `CONSUMED_EVENTS` (~32 + delegate/*); gap list decides feasibility | §3 full gap table (39 kinds) + §2.2 (`hook_streaming.py` 11→9 lossy translation, `_parse_agent_name`); feasibility verdict in §5. |
| **`amplifier_agent_lib` in-process** — assess whether the lib exposes session construction with hook attachment; **prototype behind `RuntimeAdapter`** and **count what breaks** | §2.2 (`engine.py:78/185`, `_runtime.py:make_turn_handler` private + bundle-welded), §7 (static prototype trace enumerating **8** broken subsystems). Method note in §1 explains the static-trace approach under the read-only constraint. |
| **Bundle composition control** (wrapper bundle + overlays + suppression) | §2.1 (`bundle.md:12‑15`, `AGENTS.md:26`) vs §2.2 (`bundle/cache.py:71`, `loader.py:23`, `merge_config` D5) — conflict documented; §5 gate #1. |
| **Session-dir compatibility** (`~/.amplifier/projects/...` vs `$AMPLIFIER_AGENT_HOME/state/`) | §2.1 (`persistence.py:5‑8/117`) vs §2.2 (`persistence.py:100/130`) — incompatible; §4 B, §5 gate #2, §8 risk row. |
| **Cost/usage telemetry fidelity** | §2.2 (`hook_streaming.py` `on_llm_response`/`on_orchestrator_complete`, `UsageNotification`) vs §2.1 (CostTracker + `ui-events.jsonl` re-seed) — decent-but-coarser, boundary-only. |
| **Event-contract gap table** | §3. |
| **A prototype behind `RuntimeAdapter` if lib-embedding looks viable** | §7 — lib-embedding is **not** viable (structural absent seam), so a running spike-branch prototype is unwarranted; a static integration trace establishes the verdict. This is the one bullet where I substituted a static trace for a running prototype — justified in §1 and §7. |
| **A recommendation** (adopt / adopt for headless `run` only / stay core-native with rationale to share back) | §5 — **stay core-native**, with a gated headless-only door (Option B) and a written hand-back (§6 Phase 2, §8 asks). |
| **Decision doc** (docs/plans dated; ADR if adopted) | This doc; §6 Phase 0 (promote to `docs/plans/2026-07-22-…`); **no ADR** since the runtime layer is unchanged. |

**Self-review result:** every acceptance bullet is addressed with verified `file:line` evidence. The single deviation is the "prototype" bullet — executed as a static integration trace rather than a running branch — which is disclosed explicitly (§1 method note, §7, and this table) and is dispositive because the blocking finding is the absence of a hook-attachment seam, not a runtime behavior.
