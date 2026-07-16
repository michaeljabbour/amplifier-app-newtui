# Architecture Briefing: amplifier-app-newtui (Ground-Up Full-Screen TUI Rebuild)

**Synthesis of 10 deep-reader findings across amplifier-core, amplifier-foundation, amplifier-app-cli (current + flagship + wave2), amplifier-tui, codex-rs TUI, opencode, and the UI-bridge module family.**

Target repo: `~/dev/amplifier-app-newtui`. Source-of-truth design docs to carry over unchanged:
- `/Users/michaeljabbour/dev/amplifier-app-cli/docs/designs/tui-v3-cohesive.md` (presentation spec)
- `/Users/michaeljabbour/dev/amplifier-app-cli/docs/decisions/ADR-0005-interaction-modes-and-trust-postures.md`
- `/Users/michaeljabbour/dev/amplifier-app-cli/docs/decisions/ADR-0006-full-screen-pinned-interactive-shell.md`
- `/Users/michaeljabbour/dev/amplifier-foundation/docs/APPLICATION_INTEGRATION_GUIDE.md` (7-step lifecycle + protocol boundary)

---

## 1) Chosen Stack Recommendation

### Framework: **Textual (pin `textual~=8.2`, ≥8.2.6)** on Python 3.12+, fully async entrypoint

**Rationale (from the framework and prior-art findings):**

| Requirement | Current prompt_toolkit cost | Textual answer |
|---|---|---|
| Full-screen app-owned viewport (ADR-0006) | Hand-rolled 512-line window + RLock (`layered_transcript.py`) | `ScrollView` + Line API (`render_line(y) -> Strip`) |
| Rich block rendering | 550-line ANSI re-parser (`terminal_transcript.py`) round-tripping Rich→ANSI→pt fragments — the #1 architectural wart | Rich renderables are Textual-native; **zero** escape-code round-trip |
| Collapsible tool lines | Re-emit blocks (no in-place toggle) | Built-in `Collapsible` widget → true in-place expand/collapse |
| 3 themes, runtime switch | `TOKENS` frozen at import; "no runtime theme-selection mechanism yet" | `textual.theme.Theme` + `App.theme` setter; port the exact slate/graphite/carbon hex tokens from `ui/layered_repl_style.py` |
| Shift+Enter / Shift+Tab | `Keys.F21` carrier hack (`keyboard_protocol.py`) | Kitty keyboard protocol native since 8.2.6; keep alt+enter fallback |
| Palette / approval bar / lanes / overlays | 15 hand-wired `ConditionalContainer`s | `ModalScreen`, layers, dock CSS, built-in command palette |
| Clickable spans | Heuristic row registry + string matching | `[@click=action]` content markup / per-widget `on_click` with stable block IDs |
| Testing | PTY tests + goldens | `Pilot` headless testing + snapshot testing; keep SVG-parse UX verification from amplifier-tui `tools/` |

Peer precedent: codex chose ratatui (Rust's dominant TUI), opencode chose Bubble Tea (Go's), Claude Code chose Ink. Textual is the Python analogue, and **amplifier-tui already shipped a full working product on it** (streaming markdown, collapsibles, agent tree, 11 themes) — the framework is proven for this exact workload; only its architecture (13.7k-line god file) failed.

### Key dependencies

```toml
[project]
dependencies = [
  "textual~=8.2",
  "amplifier-core>=1.6.0",          # top-level imports ONLY (Rust-backed types)
  "amplifier-foundation",            # git-pinned in [tool.uv.sources]
  "rich>=13",                        # renderables inside Textual
  "pydantic>=2",                     # typed event/block schemas
  "click>=8.1",                      # thin CLI shell only; async body
  "pyyaml", "httpx", "filelock",
]
[project.scripts]
amplifier-newtui = "amplifier_app_newtui.main:main"
```

Build: hatchling + uv, `package=true` — mirror `/Users/michaeljabbour/dev/amplifier-app-cli/pyproject.toml`. Ship `AGENTS.md`, `PRINCIPLES.md`, `SMOKE_TESTS.md` per foundation `PER_REPO_CONVENTIONS.md`.

**Explicitly rejected:** prompt_toolkit full-screen (this repo *is* the evidence of its cost), rich Live + raw stdin (no input stack, flickers at transcript scale), urwid (pre-asyncio, no kitty protocol), OpenAI-compatible facade à la amplifier-app-opencode (lossy: no tools/approvals/lanes/telemetry on the wire — that repo is the proof).

---

## 2) Event/Hook Contract Between amplifier-core and the New TUI

### Attachment mechanism

The UI attaches as **plain in-process hook handlers** — no hook module needed:

```python
unregister = session.coordinator.hooks.register(
    event: str,
    handler: async (event: str, data: dict) -> HookResult,  # return HookResult(action="continue")
    priority: int = ...,
    name: str = "newtui.<concern>",
) 
```
(`/Users/michaeljabbour/dev/amplifier-core/python/amplifier_core/_engine.pyi` — the real API surface. Always import from top-level `amplifier_core`; submodule imports give divergent pure-Python types.)

Every payload auto-carries `session_id` + `parent_id` via `hooks.set_default_fields` — this is the entire routing key for agent lanes.

Adopt the proven **hook-tracker pattern** (`ui/stream_status.py`, `ui/runtime_status.py`, `ui/task_status.py`): small class with `EVENTS` tuple, `async handle_event(event, data) -> HookResult`, `register_hooks(hooks, *, priority) -> unregister`, `add_listener(cb) -> remove`. Trackers are pure state; the app wires listeners to Textual message posting / `refresh()`. Hook callbacks must return fast — push into an `asyncio.Queue` consumed by the render loop (pattern: `hooks-ui-bridge` `create_queue_adapter`, but **do not** use the module for streaming: its `ui_friendly` mode drops all deltas).

### Exact events to consume (two-channel streaming — critical)

**Channel A — live deltas (from `amplifier-module-provider-anthropic`, ~lines 2760–2925; NOT in kernel `ALL_EVENTS`):**

| Event | Payload | Drives |
|---|---|---|
| `llm:stream_block_start` | `{request_id, block_index, block_type, name?}` | open streaming region |
| `llm:stream_block_delta` | `{request_id, block_index, block_type: "text"\|"thinking", sequence, text}` | live text/thinking tail (throttle notify ≥50ms) |
| `llm:stream_block_end` | `{request_id, block_index, block_type}` | close region |
| `llm:stream_aborted` | `{request_id, error:{type,msg}}` | abort marker |

**Channel B — durable block records (from `amplifier-module-loop-streaming` orchestrator; atomic, not incremental):**

| Event | Payload | Drives |
|---|---|---|
| `tool:pre` | `{tool_name, tool_call_id, tool_input, parallel_group_id?}` | collapsed tool line (open, spinner) |
| `tool:post` | `{tool_name, tool_call_id, tool_input, result: ToolResult.model_dump()}` | tool line finalize + expandable output |
| `tool:error` | `{tool_name, tool_call_id, error:{type,msg}}` | tool error line |
| `content_block:start/end` | `{block_type, block_index, total_blocks, block, usage}` | durable answer/thinking record |
| `orchestrator:complete` | `{orchestrator, turn_count, status: success\|cancelled\|incomplete}` | turn outcome |

Never reconstruct one channel from the other — the provider explicitly designs them as independent. Correlate tool pre/post by **`tool_call_id`, never `tool_name`** (parallel calls of the same tool run via `asyncio.gather`; ui-bridge's tool_name keying is a documented bug).

**Lifecycle/telemetry/policy events (kernel canonical, `events.py` / `crates/amplifier-core/src/events.rs`):**
- `prompt:submit`, `prompt:complete`, `execution:start/end` — turn boundaries (turn rules)
- `provider:request` — the **step-boundary steer injection point** (StepBoundaryBridge hooks here, priority ~950)
- `provider:response` — usage payloads → tokens/cache%/cost telemetry (kernel `SessionStatus` counters are NOT populated; compute yourself, port `estimate_cost()` from `amplifier-module-hooks-streaming-ui/cost.py`)
- `provider:error/retry/throttle` — footer notices
- `session:start/end/fork/resume` — session header, lane creation
- `approval:required/granted/denied` — needs-you queue + ledger evidence
- `cancel:requested/completed` — interrupt UX
- `user:notification`, `context:pre_compact/post_compact/compaction`

**App-layer events (from `amplifier-module-tool-task`):**
- `task:agent_spawned` `{agent, sub_session_id, parent_session_id}`, `task:agent_completed` `{+success}` — agent lane open/close. Sub-session IDs are hierarchical: `{parent}-{16hex}_{agent_name}`. Note the taxonomy mismatch pitfall: streaming-ui listens for `task:spawned`; **pick the `task:agent_*` names and adapt at one edge**.

### Injected protocol objects (the 4-point boundary)

Per the integration guide, TUI↔kernel meet at exactly four points, all injected at `PreparedBundle.create_session(...)` or registered on the coordinator:

1. **ApprovalSystem** — `async request_approval(prompt: str, options: list[str], timeout: float, default: Literal["allow","deny"]) -> str`. Fail-closed, string-matched in Rust: options MUST contain an "Allow"-family string (`Allow` / `Allow once` / `Allow always`) or grants are silently denied. This backs the inline approval bar; `approval:*` events back the needs-you queue.
2. **DisplaySystem** — `show_message(message, level, source)` → transient notices.
3. **Streaming hooks** — ephemeral, registered per-execution with named handlers, unregistered in `finally`. NOTE: ephemeral hooks do **not** propagate to spawned children — lane instrumentation must be attached explicitly at spawn time (see §5).
4. **`session.spawn` / `session.resume` capabilities** — registered on `session.coordinator` **after `create_session`, before `execute`** (timing bug documented in the integration guide). Reference impl: `/Users/michaeljabbour/dev/amplifier-app-cli/amplifier_app_cli/session_spawner.py` + `runtime/session_spawn_inprocess.py` — child gets parent's approval/display systems, `parent_cancellation.register_child(child_cancellation)`, and shared UI trackers re-attached to the child coordinator's hooks.

**Approvals as data:** governance returns `HookResult(action="ask_user", approval_prompt, approval_options, approval_timeout, approval_default)` from `tool:pre` (register the gate at negative/high-precedence priority so it runs before display hooks). Denials return `HookResult(action="deny", reason=...)` → orchestrator synthesizes a "Denied by hook" tool result (deny-and-continue). **Fix the detail-smuggling hack:** current app passes rich `ApprovalDetail` through a module-global keyed by prompt string (race-prone). In the rebuild, carry a structured detail payload alongside the request through your own ApprovalSystem implementation (you own both ends), keyed by a request ID.

---

## 3) Proposed Package Layout

```
amplifier-app-newtui/
├── pyproject.toml                    # hatchling+uv; amplifier-core>=1.6, foundation git-pinned
├── AGENTS.md  PRINCIPLES.md  SMOKE_TESTS.md
├── bundle.md                         # the app's REAL bundle — load_bundle()'d, never decorative
├── docs/
│   ├── designs/tui-v3-cohesive.md    # copied forward; stays source of truth
│   └── decisions/                    # ADR-0005, ADR-0006 carried over + new ADRs
├── amplifier_app_newtui/
│   ├── main.py                       # thin async click entry; no sync/async bridging
│   ├── kernel/                       # ALL amplifier-core/foundation touchpoints (no Textual imports)
│   │   ├── config.py                 # single resolve_config(): discover→load_bundle→compose→prepare (ONCE)
│   │   ├── session_factory.py        # single create_initialized_session(SessionConfig)->InitializedSession
│   │   ├── spawner.py               # session.spawn/resume capability (ONE small module — no facade sprawl)
│   │   ├── approval.py               # ApprovalSystem impl → routes to UI via typed queue; needs-you parking
│   │   ├── display.py                # DisplaySystem impl → notices
│   │   ├── events.py                 # ONE event-name taxonomy + payload normalization (tool result shapes, delta key variants)
│   │   ├── trackers/                 # stream_status, runtime_status, task_status, governance_hook, step_boundary
│   │   ├── persistence.py            # SessionStore: transcript.jsonl/metadata.json/events.jsonl (foundation layout)
│   │   ├── cost.py                   # provider:response usage → Decimal cost; resume re-seed from events.jsonl
│   │   └── rewind.py                 # fork_session(turn=N) / fork_session_in_memory + context.set_messages
│   ├── model/                        # framework-agnostic typed state (no Textual, no kernel imports)
│   │   ├── blocks.py                 # 13-type frozen TranscriptBlock union w/ STABLE block IDs (port transcript_blocks.py)
│   │   ├── turn.py                   # TurnOutcome, Telemetry, OutcomeLedger (checkpoints)
│   │   ├── trust.py                  # TrustPreset/TrustState/PermissionSlots, resolve_trust, ActionGovernor, DenialLog
│   │   ├── modes.py                  # ModeProfile registry (chat/plan/brainstorm/build/auto), ModeRuntimeBinding
│   │   ├── queues.py                 # SteeringQueue (bounded 32/32KB), NeedsYouQueue, turn drain deque
│   │   ├── lanes.py                  # AgentLaneViewModel keyed by session_id/parent_id
│   │   └── evidence.py               # EvidenceLinkModel
│   ├── ui/                           # Textual only; widgets own their state (NO mixin god-object)
│   │   ├── app.py                    # App subclass; composition root wiring model↔widgets; <500 lines budget
│   │   ├── transcript.py             # virtualized ScrollView (Line API) + live-tail widgets; tail-follow anchor
│   │   ├── composer.py               # TextArea; Enter=steer/submit, Shift+Enter=queue (alt+enter fallback)
│   │   ├── approval_bar.py           # inline allow-once/always/deny; ctrl-a detail screen
│   │   ├── footer.py                 # tiered responsive footer (CSS layout, not string surgery)
│   │   ├── palette.py  lanes.py  ledger.py  needs_you.py  rewind_picker.py   # ModalScreens
│   │   ├── themes.py                 # slate/graphite/carbon as textual.theme.Theme (runtime-switchable)
│   │   └── keymap.py                 # KEYMAP-as-data + validate() + hint_label(); feeds BINDINGS and footer
│   ├── commands/                     # slash commands + minimal CLI subcommands (run/resume/session/doctor)
│   └── data/bundles/                 # packaged default bundles
└── tests/
    ├── goldens/                      # width-matrix goldens (40/80/97/120) + regen script
    ├── test_trackers_*.py            # pure-logic tracker tests (consume() directly)
    ├── test_snapshot_*.py            # Textual snapshot tests via Pilot
    └── tools/                        # SVG-capture UX verification (port from amplifier-tui/tools)
```

**Layering rule (enforce with import-linter):** `ui/` → `model/` → `kernel/` → amplifier-core/foundation. `kernel/` never imports Textual; `model/` imports neither. This is the engine/frontend split that let amplifier-tui host both TUI and web frontends, done from day 1.

---

## 4) Ten Riskiest Technical Problems, Ranked

**1. Streaming render pipeline correctness + performance (deltas → blocks → paint).**
Highest-frequency code path; every prior implementation fought it (codex needed table-holdback + two-region model; opencode re-renders the whole transcript per delta; current app has 50ms throttles + double-render).
*Mitigation:* codex's two-region model in Textual terms — durable history as a virtualized Line-API ScrollView (pure function of `(blocks, width)`), one mutable live-tail widget re-rendered from accumulated raw source per committed delta; throttle invalidations to 30–60Hz batches; hold tables in the tail until stream end; consolidate to one source-backed block on `llm:stream_block_end`.

**2. Two-channel / non-canonical event dependency.**
The real deltas (`llm:stream_block_*`) are ad-hoc provider events not in `ALL_EVENTS`; canonical `content_block:delta` is barely emitted; `loop-streaming`'s `provider.stream()` branch is vestigial. Payload shapes vary (`delta`/`text`/`content` keys; `tool_response` vs `result`).
*Mitigation:* subscribe to **both** families; normalize every payload at exactly one ingestion boundary (`kernel/events.py`); ground-truth against the provider source (`amplifier-module-provider-anthropic/__init__.py` ~2760–2925) and `StreamStatusTracker`'s EVENTS list; write contract tests that replay captured `events.jsonl` files.

**3. Resize reflow during active streaming.**
Both codex (dedicated deferral state machine, forced post-consolidation reflow) and the current app (commits 925e418 etc., 23 tests) got bitten repeatedly.
*Mitigation:* width-parametric blocks (HistoryCell model) make rendering a pure function of width; 75ms trailing debounce; defer only while a stream is painting, force one reflow after consolidation; **port `tests/test_resize_reflow.py` cases, not just the code**; Textual eliminates the scrollback-rewrite half of the problem.

**4. Approval flow: fail-closed routing, parallel asks, deferred queue.**
Rust string-matches "Allow"-family options; missing approval_system = silent deny-everything; parallel tool calls mean concurrent in-flight approvals; needs-you requires parking a blocking `await`.
*Mitigation:* build `kernel/approval.py` as a request broker: typed `ApprovalTicket` with unique ID, FIFO of pending tickets (opencode's Permissions queue), inline bar answers head, ctrl-y defers a ticket into NeedsYouQueue (answer later resolves the still-awaited future or times out to default); always include "Allow once"/"Allow always"/"Deny" verbatim; adopt hooks-approval's remember-key scoping (file tools by parent dir, bash by 2-token prefix) for "always".

**5. Subagent lanes going dark / event routing.**
Kernel gives no child transcript access — only events; ephemeral hooks don't propagate to children; `session:start` can race `task:agent_spawned`; subprocess spawn loses in-process visibility entirely; task-tool recursion depth limiting is documented but **not implemented**.
*Mitigation:* one small `kernel/spawner.py` that on every spawn (a) re-attaches the shared tracker set to the child coordinator (`propagate_*` pattern), (b) registers child cancellation, (c) inherits approval/display systems; route all lane state by `session_id`/`parent_id` from event payloads; tolerate parent-unknown-yet with depth patching (StateManager pattern); enforce recursion depth in the spawn capability; in-process spawn only for v1.

**6. Steer vs queue semantics + terminal key reality.**
Shift+Enter is undetectable on legacy terminals/tmux<3.4; steers have inherent races; old app has two parallel steering systems and a 613-line stdin-gymnastics legacy path.
*Mitigation:* exactly ONE path — bounded `SteeringQueue` consumed one-per-`provider:request` hook returning `HookResult(action="inject_context", context_injection_role="user")` (StepBoundaryBridge, root session only); leftover steers roll forward as follow-up turns with a visible notice (make this the contract); Textual 8.2.6 kitty support + startup capability probe → alt+enter fallback hint swap; the full-screen single-input-owner design deletes stdin arbitration entirely.

**7. Rewind/turn-checkpoint consistency.**
Trimming the UI transcript before the backend confirms diverges state; orphaned tool_use pairs corrupt provider requests; resumed sessions lose cost accumulators and can hit provider drift.
*Mitigation:* codex's confirm-then-trim state machine over foundation's `fork_session(parent_dir, turn=N, handle_orphaned_tools="complete")` / `fork_session_in_memory` + `context.set_messages()`; stamp `OutcomeLedger.checkpoint_id` onto turn-rule blocks at emit time (stable IDs, no reverse string matching); re-seed cost from `events.jsonl` on resume (`cost_history.restore_session_cost` pattern); add a resume provider-guard early (wave2 lesson).

**8. UI-object sprawl (the god-object failure mode).**
Every prior attempt collapsed: `LayeredReplApp` 8 mixins/~60 shared attrs, amplifier-tui `app.py` 13.7k lines/23 mixins, codex ChatWidget ~90 fields, opencode 1,600-line Update.
*Mitigation:* structural, not disciplinary — widgets own their state and communicate via Textual messages; per-concern model objects; Request/Dependencies dataclass seams for the runtime; hard line-count budget on `ui/app.py`; slash commands as a registry, never inheritance; typing + lint gates from commit 1.

**9. Config/bundle dual-representation and mount-plan drift.**
`PreparedBundle.mount_plan` vs `.bundle` must both receive overrides or children get zero providers ("CRITICAL" comments in current repo); kernel swallows provider/tool load failures (session runs with missing tools); config shape-shifting (dict-or-string orchestrator) plagued `run.py`.
*Mitigation:* one `resolve_config()` golden path; treat the composed `Bundle` as the single source and derive the mount plan; normalize config shape once at load; after `initialize()`, verify `coordinator.get('tools')`/`get('providers')` against the plan and fail loudly in-UI.

**10. Governance classification fragility + classifier fail modes.**
Capability class from tool-name substrings misclassifies; hooks-approval's default dangerous-pattern heuristics are wrong (`'rm'` anywhere in a command); auto-mode LLM classifier must never leak reasoning or fail open.
*Mitigation:* port `resolve_trust`/`ActionGovernor`/`DenialLog` (deny-and-continue, 3-consecutive/20-total escalation) and the reasoning-blind two-stage classifier (`authorization_stage.py`, strict JSON schema, fail-closed, deterministic offline fallback) mostly as-is; declare capability class in explicit config with the substring heuristic only as fallback; propose tool-metadata capability declaration upstream as a module contract.

---

## 5) Reuse Map

### Port nearly as-is (from current amplifier-app-cli)
| Asset | File | Notes |
|---|---|---|
| 13-block TranscriptBlock grammar + renderer | `ui/transcript_blocks.py` | Cleanest module in the repo; add stable block IDs; render to Textual/Rich natively |
| Keymap-as-data + validate() + hint_label() | `ui/key_bindings_table.py` | Feed Textual BINDINGS + footer from one table |
| Theme token palettes (slate/graphite/carbon) | `ui/layered_repl_style.py` | Hex values verbatim → `textual.theme.Theme` |
| Trust/governance stack | `ui/governance.py`, `governance_hooks.py`, `authorization_stage.py`, `interaction_state.py` | resolve_trust, ActionGovernor, DenialLog, NeedsYouQueue, 7 PermissionSlots |
| Mode system (2 independent 5-state cycles) | `ui/mode_profiles.py`, `interaction_controller.py` | ADR-0005 amendment: shift+tab ≠ ctrl-p, never share |
| Hook trackers | `ui/stream_status.py`, `runtime_status.py`, `task_status.py`, `agent_lanes.py` | The core↔UI bridge pattern; pure-state, listener-driven |
| Steering | `ui/steering.py` + `ui/step_boundaries.py` | Bounded queue + provider:request injection; drop the legacy path |
| Turn/ledger | `ui/outcome_ledger.py`, `runtime/interactive_turn.py` | TurnOutcome telemetry, git-diff capture, checkpoint IDs |
| Session factory + capability registration order | `session_runner.py` (flagship version is the cleanest) | Single `create_initialized_session`; capability-name contract from `runtime/interactive_resource_setup.py` |
| Persistence | `session_store.py`, `incremental_save.py`, `cost_history.py` | Atomic write+backup; tool:post debounced save; events.jsonl cost re-seed |
| Terminal probe + shift+enter sequences | `ui/terminal_probe.py`, `keyboard_protocol.py` | Probe pattern + hint overrides survive; F21 hack dies |
| Golden width-matrix + PTY/reflow tests | `tests/test_transcript_golden_widths.py`, `test_resize_reflow.py`, `test_tui_pty.py` | Port the tests even where code is rewritten |

### Lift conceptually from codex-rs
- **HistoryCell width-parametric block model** (`history_cell/mod.rs`) — rendering as pure fn of (cells, width); gives reflow, pager, copy, rewind-trim for free.
- **Two-region streaming + table holdback + consolidation** (`streaming/controller.rs` — the module doc is a spec).
- **AppEvent bus + demand-driven FrameRequester** (coalesced, rate-capped repaints; animations schedule their own next frame) — maps to Textual messages + timers.
- **Bottom-pane view stack** (composer persists under modals; approval deferral queue) (`bottom_pane/mod.rs`).
- **Backtrack confirm-then-trim rewind state machine** (`app_backtrack.rs`).
- **Esc precedence chain documented as a table** (`chatwidget/interaction.rs`) — spec it, don't let it emerge.
- **Event vocabulary discipline**: paired Begin/End per tool, *Delta for streams, TurnStarted/Completed with telemetry, structured error payloads for steer races (they parse error strings — don't).

### Lift conceptually from opencode
- **Idempotent part upsert** (full state + advisory delta) — apply to internal event normalization so replay/reconnect is free.
- **Busy state derived from data, not flags** (`IsBusy()` = last assistant msg incomplete).
- **Permission-as-promise + wildcard "always" patterns + FIFO permission queue** (`permission/index.ts`).
- **Subagents as child sessions with parent lane mirroring** (`tool/task.ts`) — validates the amplifier spawn model.
- **Single command registry** powering keybinds + slash triggers + palette + help (`commands/command.go`).
- **Semantic theme tokens as flat data with adaptive pairs + override directories**; **persisted lightweight UI state file** (theme, MRU models, prompt history) separate from server config.
- **Revert/unrevert with file-diffstat markers** — matches the rewind mockup.
- **Double-press debounce** for Esc/Ctrl-C destructive actions.

### From existing amplifier modules
- **Mount/consume, don't fork**: `hooks-approval` (approval interception + remember keys), `tool-task` (spawn contract), `loop-streaming`, `provider-anthropic`.
- **Port state, not printing** from `hooks-streaming-ui`: `state.py` (Phase enum, SessionMetrics, breadcrumb, depth-race patching) and `cost.py` (`estimate_cost`). Never mount it — it prints to stdout.
- **`hooks-ui-bridge`**: reuse the `UIEvent` envelope idea (event_id/parent_event_id/session_id/agent_name) and `register_on_coordinator(is_child=True)` stamping concept; skip the module itself (global singletons, drops deltas in ui_friendly mode, tool_name correlation bug).
- **From amplifier-tui**: `SharedAppBase` streaming-callback seam, per-block-index block_type tracking, worker-based session cleanup on exit (the lost-last-turn bug fix), SVG-capture UX test tooling, `TodoPanel`/`AgentTreePanel` as prior art.
- **From amplifier-app-opencode**: the `doctor` subcommand pattern (named checks, OK/FAIL/INFO/WARN, CI exit codes), `KNOWN_PROVIDER_ENV_VARS` single source, self-update via PEP 610 detection, first-run progress UI for 60–90s cold module installs.

### Foundation "native way" skeleton
7-step lifecycle verbatim: `load_bundle` → `compose` overlays (modes as behavior bundles composed **before** prepare) → `prepare()` **once** → `create_session` per conversation → `coordinator.mount` runtime tools → ephemeral `hooks.register` → `execute`. Modes/trust as bundle overlays + `SessionConfigurator`/`RuntimeOverlay` for live transitions; three-scope settings (global/project/local deep merge); bundle search paths project → user → packaged.

---

## 6) Open Questions Implementation Must Resolve

1. **Live mode transition mechanism**: `SessionConfigurator`/`RuntimeOverlay` vs teardown-and-recreate with same `session_id` vs current app's `setattr(provider, 'default_model', ...)` mutation? The setattr path is untyped and silently no-ops on drift — decide, and possibly propose a typed kernel contract for model_role/reasoning_effort changes.
2. **Approval detail contract**: keep kernel's minimal `(prompt, options)` and carry detail entirely inside the app's own ApprovalSystem (feasible since we own both ends), or push an upstream amplifier-core widening (structured `ApprovalRequest` in the ask_user path)? Affects whether the needs-you queue can show command/cwd/rule without any staging channel.
3. **Delta channel canonicalization**: propose upstreaming `llm:stream_block_*` into `ALL_EVENTS` (they're the de-facto contract), or wrap them behind our normalization layer and accept per-provider variance? Which other provider modules emit them at all (non-Anthropic support)?
4. **Turn identity across the stack**: no kernel turn ID exists; is checkpoint_id derived from `count_turns` over transcript.jsonl stable across steers-rolled-forward, compaction (`context:compaction`), and forks? Define the turn-numbering invariant before building the ledger/rewind UI.
5. **Needs-you deferral semantics vs blocking approvals**: when a parked approval's `approval_timeout` (default 300s) fires, does the ticket resolve to default-deny and become a retro-answerable DenialLog entry, or do we hold the turn indefinitely? What happens to a deferred approval whose tool the orchestrator has already moved past?
6. **Virtualization threshold + in-place collapse**: pure Line-API history (fast, but collapse re-renders) vs widget-per-block (true Collapsible, but perf risk at 10k blocks) vs hybrid (widgets for last N blocks + Line API beyond)? Needs an early spike with a 5k-block synthetic transcript at 60fps streaming.
7. **Subprocess spawn support**: v1 in-process only is clean, but long/parallel agent trees may need process isolation — if so, a lane-event IPC protocol (opencode-style SSE?) must be designed; defer or design now?
8. **Native scrollback/copy compensation** (ADR-0006 trade-off): Textual text-selection + explicit copy affordances + plain-transcript handoff on exit — is that sufficient, or do we need a Ctrl+T-style alt-screen pager with raw copy mode (codex) as well?
9. **Session events.jsonl as replay source**: do we commit to full event logging such that the TUI can cold-render any historical session purely from events (enabling lane replay and evidence links), or is transcript.jsonl + metadata the only durable contract?
10. **Version/packaging hygiene**: amplifier-core pyproject says 1.3.3 while `__version__` reads 1.0.7, and app-cli pins `>=1.6.0` — establish the actual compatible version range and a pinned-lockfile policy; also decide whether the new app registers any of its own entry-point hook modules (`amplifier.modules`) or stays 100% in-process handlers.
11. **Theme-change reflow**: runtime theme switch requires re-rendering retained blocks (colors are baked at render time). Is theme switch a full transcript rebuild (reuse the reflow path) — acceptable cost on a 10k-block session?
12. **Loud-failure policy for partial mounts**: kernel warns-and-swallows provider/tool load failures; do we hard-fail session start, or start degraded with a blocking notice + doctor pointer? (Current app's self-healing Counter-arithmetic detection is the anti-pattern to avoid.)