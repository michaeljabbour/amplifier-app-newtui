# Architecture

How `amplifier-app-tui` actually works: the layers, the seams, and the data flows.
This document describes the code as built. For *what the app must look and behave like*, see
[DESIGN-SPEC.md](DESIGN-SPEC.md); for *why it is shaped this way*, see
[ADR-0007](decisions/ADR-0007-tui-ground-up-architecture.md); for the research that grounded
the stack choice, see [RESEARCH-BRIEF.md](RESEARCH-BRIEF.md).

| Document | Role |
|---|---|
| [README.md](../README.md) | Quick orientation, run instructions, provider config |
| [DESIGN-SPEC.md](DESIGN-SPEC.md) | Checkbox-testable behavioral requirements (TUI v3 — Cohesive) |
| [design-v3-cohesive.html](design-v3-cohesive.html) | Executable mockup — ground truth for exact strings, colors, timing |
| [ADR-0007](decisions/ADR-0007-tui-ground-up-architecture.md) | The architecture decision record (layering, event contract, 13 resolutions) |
| **This document** | The implemented architecture, module by module |

---

## 1. System overview

The app is a **thin Textual front-end over amplifier-core**. A bundle mounts the real
capabilities (orchestrator, provider, tools, agents); the app attaches as in-process hook
handlers, normalizes everything that happens into typed events, and renders those events.
Nothing amplifier-specific leaks past the kernel package, and nothing Textual-specific leaks
into it.

```
┌────────────────────────────────────────────────────────────────────────┐
│ ui/            Textual app, widgets, reducer, runtime adapters         │
│                (imports model/; consumes UIEvents; never sees hooks)   │
├────────────────────────────────────────────────────────────────────────┤
│ commands/      slash commands as data (imports model/ + stdlib only)   │
├────────────────────────────────────────────────────────────────────────┤
│ model/         pure domain: blocks, lanes, queues, modes, trust,       │
│                turns, evidence (no Textual, no amplifier-core)         │
├────────────────────────────────────────────────────────────────────────┤
│ kernel/        the amplifier adapter: config, session factory,        │
│                event normalization, governance, approvals, steering,   │
│                spawner, rewind, cost, persistence (never imports       │
│                Textual)                                                │
├────────────────────────────────────────────────────────────────────────┤
│ amplifier-core / amplifier-foundation + the mounted bundle             │
└────────────────────────────────────────────────────────────────────────┘
```

Rendered diagrams live in [diagrams/](diagrams/):
[tui-architecture.png](diagrams/tui-architecture.png) (topology),
[tui-dataflow.png](diagrams/tui-dataflow.png) (a turn, end to end), and
[tui-amplifier-integration.png](diagrams/tui-amplifier-integration.png) (how the app
plugs into the Amplifier platform). Regeneration commands are in
[DEVELOPMENT.md](DEVELOPMENT.md).

Reading the data-flow diagram, a turn end to end (colors in the diagram):

- **Input (blue)** — keypress → Composer → `adapter.submit` → thread hop →
  `RealRuntime.submit` (synthetic `PromptSubmit` for instant echo, git snapshot) →
  `session.execute` → orchestrator ⇄ provider ⇄ tools.
- **Event stream (green)** — coordinator hooks fire (Channel A live deltas; Channel B
  durable records) → `QueueBridge.normalize()` → typed `UIEvent` → app-loop queue →
  `TranscriptReducer` → TranscriptView/LiveTail.
- **Approvals (orange)** — tool ask → `ApprovalBroker` ticket → hop to the app loop →
  ApprovalBar → answer routes back to the kernel.
- **Steering (purple)** — mid-turn composer text → `SteeringQueue` →
  `StepBoundaryBridge` injects at step boundaries.
- **Subagents (teal)** — `tool-delegate` → `SessionSpawner` (`session.spawn`) → child session
  shares the same bridge → LanesPanel.
- **Persistence (gray)** — debounced `transcript.jsonl`/`metadata.json`, append-only
  `ui-events.jsonl`; resume restores history into both the context and the transcript view.

### Load-bearing invariants

These are the rules the whole design hangs on. Every one is enforced by tests and/or review:

1. **One normalization boundary.** All raw hook payloads become a frozen pydantic `UIEvent`
   union in `kernel/events.py` — nowhere else. The UI never sees a raw hook payload.
2. **Strict layering.** `kernel/` never imports Textual; `model/` imports neither Textual nor
   amplifier-core; `commands/` imports only `model/` + stdlib.
3. **Two event channels, never cross-reconstructed.** Channel A (live streaming deltas) and
   Channel B (durable records) are consumed independently. Tool correlation is by
   `tool_call_id` only.
4. **The UI cannot tell demo from real.** `DemoRuntime` emits the same typed events into the
   same queue contract as `RealRuntime`.
5. **Rendering is pure.** `render_block(block, width) → segments` is a pure function of
   immutable block state; colors are theme-token *names*, so a theme switch is a repaint.
6. **Deny-and-continue.** Governance denials never halt a turn; they synthesize a denial
   result and keep going.
7. **Confirm-then-trim, conflict-safe files.** Conversation restore mutates transcript/UI
   state only after the context backend succeeds; workspace restore uses per-file
   compare-and-swap and skips conflicts instead of overwriting them.
8. **The runtime thread never blocks rendering** and vice versa (see §4.3).

---

## 2. Package layout

```
src/amplifier_app_tui/
├── main.py            click entry point: TUI launch, --demo, run/sessions/resume/doctor,
│                       init, update, and the bundle group (list/show/use/…)
├── kernel/            amplifier adapter layer (no Textual)
│   ├── config.py          resolve_config(): keys.env → settings merge → bundle lifecycle
│   │                       (+ mode search-path & routing-config injection)
│   ├── session_factory.py create_initialized_session(): canonical session bring-up
│   ├── runtime.py         RealRuntime: boot, submit, hook registration, turn close-out
│   ├── session_ops.py     live-session ops for /model /effort /compact /clear /status
│   │                       /tools /agents /skills (over the coordinator)
│   ├── skill_activation.py palette skill result → next-turn hook-origin context
│   ├── bundle_compose.py  safe live additive provider/tool/hook/agent composition
│   ├── bundle_content.py  prepared bundle instruction/context → next-turn context
│   ├── live_mcp.py        ownership-aware current-session MCP reconciliation
│   ├── bundle_admin.py    bundle CLI logic (list/show/use/… over the shared registry)
│   ├── setup.py           init: provider discovery + keys.env writer
│   ├── updater.py         update: foundation-backed bundle/module refresh (backs `update`)
│   ├── mcp_config.py      effective MCP scopes + atomic user-config persistence
│   ├── events.py          UIEvent union + normalize() — THE normalization boundary
│   ├── queue_bridge.py    hooks → asyncio.Queue[UIEvent]
│   ├── governance_hook.py app-side tool:pre approval + path-policy gate (§7.2)
│   ├── safety.py          typed two-axis approval / execution-policy resolution
│   ├── approval.py        ApprovalBroker: tickets, timeout→deny, defer to needs-you
│   ├── steering.py        StepBoundaryBridge: mid-turn context injection
│   ├── spawner.py         SessionSpawner: in-process subagents + routing preference apply
│   ├── rewind.py          RewindController: pre-prompt conversation restore/fork helpers
│   ├── checkpoints.py     private structured-file preimages + CAS workspace restore
│   ├── git_yield.py       bounded git diff snapshots → turn yield labels (+ /diff patch)
│   ├── turn_yield.py      TurnYieldTracker (tests-ran heuristic)
│   ├── cost.py            CostTracker: Decimal pricing, resume re-seed
│   ├── persistence.py     SessionStore + IncrementalSaver (transcript/metadata/events)
│   ├── evidence.py        EvidenceCollector: answer claims ↔ tool calls
│   ├── clipboard.py       clipboard image ingestion
│   ├── display.py         DisplaySystem: kernel messages → Notification UIEvents
│   ├── demo.py            DemoRuntime: scripted offline event producer
│   ├── directory_permissions.py  shared path policy + protected defaults
│   ├── file_mentions.py   bounded workspace-file discovery and ranking
│   └── trackers/          task_status, stream_status, runtime_status
├── model/             pure domain state (no Textual, no amplifier-core)
│   ├── blocks.py          TranscriptBlock discriminated union (19 kinds) + id allocator
│   ├── lanes.py           LaneRegistry / LaneRecord / LaneState
│   ├── queues.py          SteeringQueue + NeedsYouQueue (bounded)
│   ├── modes.py           five interaction modes (chat/plan/brainstorm/build/auto)
│   ├── trust.py           CapabilityClass, classify_tool(), resolve(), DenialLog
│   ├── turn.py            TurnTelemetry, TurnOutcome, Checkpoint, OutcomeLedger
│   └── evidence.py        EvidenceLink
├── commands/          slash commands as data + callables
│   ├── registry.py        CommandSpec, CommandRegistry, CommandContext protocol
│   ├── builtin.py         registration of all built-ins (thin glue)
│   ├── skills.py          discovered skills registered as first-class commands (+ shortcut aliases)
│   └── copy/export/improve/doctor/permissions/context.py   pure command logic
├── data/bundles/tui.md   packaged default bundle (byte-identical to repo bundle.md)
└── ui/                Textual layer
    ├── app.py             TuiApp composition root
    ├── app_support.py     esc-chain, approval bar mount, restore confirm, footer state
    ├── command_context.py AppCommandContext: CommandContext protocol → running app
    ├── runtime_adapter.py RuntimeAdapter seam; RealRuntimeAdapter (thread marshalling)
    ├── demo_wiring.py     DemoRuntimeAdapter
    ├── session_ops_view.py rendering for /status /tools /agents /skills output
    ├── directory_admin.py session directory-command controller
    ├── reducer.py         TranscriptReducer: UIEvent → transcript mutations
    ├── transcript.py      TranscriptView + pure render_block() per block kind
    ├── live_tail.py       the single mutable streaming region
    ├── composer.py        input TextArea: submit/steer/queue, paste, @file mentions
    ├── file_mentions.py   controlled workspace-path autocomplete strip
    ├── keymap.py          keymap-as-data + ESC_CHAIN + validation
    ├── palette.py         command palette strip (substring filter)
    ├── lanes_panel.py     live subagent panel
    ├── plan_panel.py      ambient Plan N/M checklist strip (todo tool)
    ├── motion.py          shared presentation-only motion primitives for active labels
    └── footer.py / chrome.py / approval_bar.py / rewind_strip.py / needs_you.py /
        queued_strip.py / decision_capture.py / notices.py / themes.py / segments.py /
        term_probe.py
```

---

## 3. Boot

### 3.1 Entry point (`main.py`)

`main()` is a click group. With no subcommand it runs `asyncio.run(_launch_tui(...))` — one
`asyncio.run` for the whole app. Flags: `--demo` (scripted offline runtime), `--bundle`
(name or URI). Subcommands: `run [PROMPT]` (headless one-shot; stdin plus
`text|json|json-trace|jsonl` output), `sessions`, `resume SESSION_ID`, `doctor`, `init` (provider setup),
`update` (bundle/module refresh), `allowed-dirs`, `denied-dirs`, and the `bundle` group
(`list/show/use/clear/current/add/remove/update`). JSON modes redirect all runtime chatter
to stderr. Document modes keep stdout to one parseable object; JSONL adds a versioned,
sequenced envelope around the normalized queue the TUI consumes and flushes each event
before the turn completes.

The packages under `sdk/python` and `sdk/typescript` deliberately contain no runtime
implementation. Each spawns this command, sends the prompt over stdin, validates schema v1
plus sequence/terminal invariants, and exposes the normalized runtime records as a typed
iterator. “The CLI is the API” keeps TUI, automation, and SDK behavior on one surface.

`_launch_tui` picks the adapter — `DemoRuntimeAdapter` for `--demo`, otherwise
`RealRuntimeAdapter(bundle, resume_id)` — and hands it to `TuiApp`. That adapter choice is
the *only* place demo and real diverge.

### 3.2 Real boot (`kernel/runtime.py` → `RealRuntime.start()`)

1. **`resolve_config()`** (`kernel/config.py`) — the single configuration golden path:
   - load `~/.amplifier/keys.env` into the environment (existing env wins);
   - deep-merge three settings scopes: `~/.amplifier/settings.yaml` →
     `<project>/.amplifier/settings.yaml` → `.amplifier/settings.local.yaml`;
   - discover the bundle: CLI `--bundle` → settings `bundle.active` → the packaged default
     (`tui`, byte-identical copy of the repo's `bundle.md`);
   - foundation lifecycle: `load_bundle` → compose settings overlays → `prepare()` exactly
     once; then apply module overrides and expand `${VAR}` / `${VAR:default}` placeholders
     into the mount plan.
2. **Suppress noisy hooks.** Any line-mode stdout hook (e.g. `hooks-streaming-ui`) would
   corrupt the Textual screen; `_apply_hook_suppression()` strips them (plus `hooks-notify`,
   whose raw OSC-777/BEL stdout escapes corrupt the full-screen TUI — the app rings Textual's
   driver-safe bell instead via the `ui/app_support` attention-bell policy) from the mount
   plan at boot, emits a notice naming what was removed, and honours the `hooks.suppress`
   settings key for user extensions. `hooks-logging` mounts natively: it owns the canonical
   `events.jsonl` (ISO-timestamped hook records), while the app's normalized UIEvent log
   lives in `ui-events.jsonl` — two writers, two files, no schema mixing.
   `hooks-insight-blocks`/`hooks-inline-blocks` are *not* suppressed: they inject
   instructions (no stdout), and the transcript renders their blockquote callouts natively
   behind a `▌` gutter.
3. **`create_initialized_session()`** (`kernel/session_factory.py`) — the canonical order:
   mint/accept a session id → stamp root metadata into the mount plan (fill-only, so child
   sessions inherit) → `create_session` (foundation mounts modules) → register
   `session.spawn` / `session.resume` capabilities (*after create, before execute*) →
   `verify_mounts` (zero providers = fatal `ProviderMountError` with a doctor pointer;
   partial tool failure = degraded-mode notice) → on resume, restore the transcript and
   re-inject the fresh system prompt.
4. **Register app hooks** on the session's hook registry: `QueueBridge` (events out),
   the `ApprovalBroker` provider (approvals in), `StepBoundaryBridge` (steering),
   `IncrementalSaver` (persistence), and a `provider:request` injector for pasted clipboard
   images.

### 3.3 Demo boot

`DemoRuntime` (`kernel/demo.py`) is a fully offline scripted producer — no bundle, no
network, no credentials. It replays choreographed turns from the design mockup on a virtual
clock (injectable sleep, seeded RNG, fixed costs), emitting the same `UIEvent`s into the same
`asyncio.Queue`. It exists so compliance/Pilot/golden tests and demos run deterministically.

---

## 4. The event pipeline

### 4.1 Normalization (`kernel/events.py`)

`normalize(event_name, payload)` converts every raw hook payload into one member of the
`UIEvent` frozen-pydantic discriminated union, tolerating provider/payload variance
(`delta|text|content`, `result|tool_response`, legacy event names) *at this boundary only*.
Every event carries the envelope `{event_id, session_id, parent_id, ts}` — `parent_id` is
what routes subagent events into lanes.

Two channels are consumed independently:

| Channel | Events | Purpose |
|---|---|---|
| **A — live** | `StreamBlockStart/Delta/End`, `StreamAborted` | streaming text/thinking into the live tail |
| **B — durable** | `ToolPre/Post/Error`, `ContentBlockStart/End`, `OrchestratorComplete` | the permanent record: tool lines, answers |

Plus lifecycle (`PromptSubmit/Complete`, `ExecutionStart/End`), telemetry
(`ProviderResponseUsage`, `ProviderNotice`), session (`SessionStart/End/Fork/Resume`),
approvals/cancel (`ApprovalRequired/Granted/Denied`, `CancelRequested/Completed`), subagents
(`AgentSpawned/Completed`), `Notification`, `ContextInjected`, and `ContextCompacted`.

### 4.2 Bridge (`kernel/queue_bridge.py`)

`QueueBridge` registers one fast handler per consumed hook name; each handler normalizes and
`put_nowait`s onto an `asyncio.Queue[UIEvent]` — it never blocks the engine. A synchronous
`tap` observes every event for the evidence collector, turn-yield tracker, and the
`ui-events.jsonl` log. The bridge also synthesizes `ProviderResponseUsage` from
`content_block:end` usage data, because the streaming orchestrator does not fire
`provider:response`. Providers repeat that usage on every block in a response, so the
bridge emits telemetry only for the final block. Resume replay applies the same
exactly-once rule and repairs logs written by older TUI builds that recorded every
repeated block usage.

An **event-drift canary** guards `CONSUMED_EVENTS` against upstream renames/additions.
The Rust hook registry matches exact names only (no wildcard subscription), so the bridge
observes drift the way native `hooks-logging` does: it subscribes per-name to the installed
core's `ALL_EVENTS` plus `observability.events` module contributions, minus the consumed
set and an explicit `IGNORED_EVENTS` allowlist (deliberately-unbridged kinds like
`provider:request` and the high-volume canonical delta streams). The first occurrence of an
unknown kind — or a subscribed name `normalize()` no longer recognizes — surfaces once per
session as a debug-level `Notification` plus a logger line, instead of silently vanishing.

Two events are deliberately **app-synthesized** rather than hook-driven:

- `PromptSubmit` is emitted *before* `session.execute` so the user's line echoes instantly;
- the enriched `PromptComplete` is emitted after the end-of-turn git snapshot, carrying
  `files_changed` / diffstat / `tests_ok` (see §7.4).

### 4.3 The thread boundary (`ui/runtime_adapter.py`)

`RealRuntime` runs on a dedicated **`real-runtime` daemon thread with its own asyncio loop**,
so slow hooks and provider I/O can never starve Textual's render loop. Calls *in* (submit,
interrupt, checkpoint restore, fork, approval answers) marshal via
`run_coroutine_threadsafe`; events *out* marshal via `call_soon_threadsafe` onto the app-loop
queue. `RuntimeAdapter` is the seam that owns the queue plus the shared interaction state
(steering queue, needs-you queue, denial log) and the call surface (`submit / interrupt /
restore_checkpoint / fork / turn_spec / evidence_links / …`). `DemoRuntimeAdapter`
implements the identical surface in-process.

### 4.4 End-to-end flow

```
composer keypress
  → Textual message → TuiApp handler → adapter.submit()      (thread hop, real mode)
    → RealRuntime.submit: emit PromptSubmit, snapshot git, session.execute()
      → orchestrator ⇄ provider ⇄ tools; coordinator hooks fire
        → QueueBridge.normalize() → UIEvent → asyncio.Queue     (thread hop back)
          → TuiApp._consume_events() → TranscriptReducer.handle(event)
            → ReducerHost calls (append/replace/remove block, notices, lanes…)
              → TranscriptView / LiveTail widget updates → Textual paints
```

---

## 5. The UI layer

### 5.1 Widget tree

Composed in `ui/app.py`, the composition root. ADR-0007 prescribes a <500-line budget for
this file; as built it has grown to roughly double that — helper logic lives in
`app_support.py` and the widgets, and further extraction is the standing direction:

```
TuiApp
├── TitleBar #title-bar             spinner · "<state> — <bundle> — <session>"
├── Container #transcript-region    (1fr; layered)
│   ├── TranscriptView #transcript  durable history: selectable archive + widget tail
│   ├── LiveTail #live-tail         the ONE mutable streaming region (~30 Hz throttle)
│   └── NoticeSlot #notice-slot     floating transient notices (own layer)
├── PaletteStrip / LanesPanel + PlanPanel / RewindStrip / SessionsStrip /
│   QueuedStrip                       overlay strips,
│                                   display:none until opened, docked above composer
├── DecisionCaptureStrip              persistent custom-answer context band
├── Container #composer-slot
│   └── Composer                    swapped for ApprovalBar while an approval is pending
└── FooterBar #footer-bar           mode · trust · model · session · context · $cost
                                    │ context-sensitive hints (bundle stays in title)
```

`TitleBar` owns the only 260 ms active-turn spinner timer. Each changed frame
is emitted as a Textual message; `TuiApp` mirrors a higher-motion braille
frame to the native terminal window/tab title with a sanitized, bounded OSC 0
write while the in-app chrome keeps the product's star pulse.
The timer is stopped at idle, and unmount restores a static application title.

### 5.2 State management (`ui/reducer.py`)

`TranscriptReducer` is redux-*adjacent*: a stateful UIEvent→mutation translator, not a pure
`(state, action) → state` function. It owns per-turn state (tool-call correlation by
`tool_call_id`, plan-block ids, burst digest counters, the live activity ring, spinner frame)
and session tallies (tokens, cost). Events dispatch through a `match` table.

Crucially, the reducer **never touches widgets**. It acts through the narrow `ReducerHost`
protocol (`append_block / replace_block / remove_block / show_notice / turn_started /
turn_finished / lanes_changed / stream_opened / stream_delta / stream_closed / …`),
implemented by `TuiApp`. Widgets talk *back* only via Textual messages
(`Composer.Submit`, `ApprovalBar.Resolved`, `LanesPanel.FocusLane`, …). The result is a
unidirectional loop: events flow down through the reducer; intents flow up as messages.

### 5.3 Transcript rendering (`ui/transcript.py`, `ui/live_tail.py`)

**Two-region model.** `TranscriptView` holds the durable, immutable history;
`LiveTail` is the single mutable region that renders streaming deltas and consolidates into
an immutable `Answer` block at `stream_block_end`. Completed streaming lines already use
the final answer renderer while only the trailing partial line stays mutable; pipe tables
remain held until their widths are stable, and half-open fences retain code styling.
The reducer supplies the root tail's `main · tN` identity from the same active `_Turn`
that owns checkpoint/ledger numbering. Child tails take identity from their containing
`LaneRecord` row and focused banner. These labels are projection metadata: consolidation
persists model text only, so identity cannot duplicate or reorder stream content.

**Infinite-history layout.** The newest ~1000 transcript blocks remain independent widgets.
When history grows beyond that tail, finalized older blocks consolidate into one
`HistoryArchive`. The archive keeps all text selectable and retains tool expansion,
evidence, rewind, and deferred-decision actions, while bounding the number of children the
Textual compositor arranges each frame. The 5k-block layout test therefore remains below
the 16ms streaming frame budget without truncating the conversation.

**Pure rendering.** Each block kind has a `_render_*` function; `render_block(block, width)`
returns lines of segments referencing theme variables (via `ui/segments.py`) — which makes
the renderer golden-testable as plain text at multiple widths, and makes theme switching a
repaint (themes: slate / graphite / carbon / paper in `ui/themes.py`, the only module
containing hex values).

**Block vocabulary** (`model/blocks.py`, discriminated union on `kind`): `SessionBanner`,
`UserLine`, `Narration`, `ToolLine` (the collapsible burst digest: *"Read 4 files · ran 6
commands"*), `PlanBlock`, `Blocked`, `WorkingStatus` (spinner pulse + live activity tree),
`Recap`, `Answer` (with `evidence_refs`), `SteerEcho`, `TurnRule` (carries `checkpoint_id` —
clickable → rewind), `EvidenceBlock`, `NeedsYouBlock`, `LedgerBlock`, `ContextBlock`,
`DoctorBlock`, `ImproveBlock`, `BrainstormIdea`, `LiveCommand`. Every block has a monotonic
string id (`BlockIdAllocator`) used for in-place mutation, click routing, and rewind
trimming.

**Lane focus.** Selecting a subagent lane swaps the transcript to that agent's blocks; the
parent block list is stashed, and appends that arrive during focus land in the stash, so
Esc restores an up-to-date parent view.

---

## 6. Input, commands, keys

### 6.1 Composer (`ui/composer.py`)

An auto-growing TextArea (6-line cap) that intercepts keys ahead of TextArea's own bindings.
It **only posts messages; it never executes anything**:

| Input | Behavior |
|---|---|
| Enter (idle) | `Submit` (with staged image attachments) |
| Enter (turn running) | `Steer` — text-only mid-turn steering |
| Shift+Enter (Alt+Enter fallback) | `QueueMessage` — a full next turn |
| Enter (decision capture) | `DecisionAnswer` — outranks submit, steer, queue, and slash commands |
| Ctrl+J / Ctrl+Enter | literal newline (app-cli parity + terminal alternate) |
| Large paste (>10 lines / 800 chars) | collapses to a `[Pasted #N · …]` stub, expanded verbatim at submit |
| Image path paste / Ctrl+V | `[Image #N]` attachment (off-thread clipboard read) |
| Text starting with `/` | posts `OpenPalette(filter=…)` on every edit |
| `@query` after whitespace | posts a ranked workspace-file filter; arrows/enter/tab stay in the composer |
| ↑/↓ on single-line composer | bounded prompt history; ↓ restores the current draft |
| ↑/↓ on empty composer with no history | lane navigation |
| Esc | `EscPressed` → resolved by the app's Esc chain |

### 6.2 Commands (`commands/`)

Commands are **data + callables** (the opencode pattern): a frozen `CommandSpec` (`group`,
`name`, `desc`, `tag`, `handler`, optional `key_action` linking a command to a keybinding) in
one ordered `CommandRegistry` that powers the palette rows, slash parsing, keybind wiring,
and help output. Handlers are synchronous, receive a `CommandContext` protocol (data
surfaces + actions), and the real implementation (`ui/command_context.py`) is a thin adapter
over the running app — tests substitute a plain fake. Dedicated modules (`doctor.py`,
`improve.py`, `permissions.py`, …) keep command logic pure; `builtin.py` is glue.

Groups and built-ins: **During** `/mode /modes /plan /brainstorm /context /status /model
/effort /compact /clear /tools /agents /skills /skill /mcp` — plus every discovered skill,
which `commands/skills.py` registers additively into the During group as its own command
(and `shortcut:` alias) · **Parallel** `/tasks` ·
**Ship** `/ledger /export /copy /diff /about` · **Between** `/rewind /quit` ·
**Repair** `/permissions /allowed-dirs /denied-dirs /doctor /improve /theme`.

### 6.3 Keymap (`ui/keymap.py`)

Keymap-as-data: one `Binding` table feeds both Textual bindings and the footer hints, so the
advertised keys can never drift from the real ones; `validate()` rejects duplicate
key+context claims. Esc resolves through an explicit `ESC_CHAIN`:
`lane_unfocus → close_palette → close_rewind → close_lanes → interrupt_running`.
The app-level `EscSequence` adds the temporal chord: with an empty composer, double-Esc
opens checkpoints; during a turn the first Esc interrupts and the second opens restore;
an idle draft is cleared into recall history rather than overwritten by the picker.
The palette (`ui/palette.py`) is a strip docked above the composer (never a modal),
substring-filtered (not fuzzy), slaved to composer text, and posts `CommandRun` messages
rather than executing anything itself.

The file mention strip follows the same controlled-widget pattern. Filesystem discovery
lives in `kernel/file_mentions.py`, is bounded, prunes dependency/build trees, and never
follows symlinks; the UI only presents relative paths.

---

## 7. Governance, approvals, and turn machinery

### 7.1 Trust model (`model/trust.py`)

Tools are classified into `CapabilityClass` — READ / WRITE / NET / TEST / SPEND / EXEC /
OUTSIDE_PROJECT — via
an explicit table, then test-command sniffing, then name heuristics, with **EXEC as the
fail-safe default** for anything unknown. `resolve(mode, tool, input)` yields a
`TrustDecision`: `allow`, `ask`, or `deny` (plus `classifier_gated` in auto mode). The
`DenialLog` counts denials and escalates at 3 consecutive or 20 total. `PermissionSurface`
adds explicit slot overrides, command exceptions and blocks; `GovernanceHook` consults it
live through adapter callables, so the pure model never imports kernel code.

`kernel/safety.py` keeps approval and execution path policy as a typed two-axis result.
An allow decision can therefore remain valid while a recognizable target is still blocked
by configured directory policy. `DirectoryPolicy` supplies the hard path check to
filesystem tools and protects `.git`, `.agents`, `.codex`, and `AGENTS.md` by default;
recognizable shell paths use the same resolution. This is policy enforcement, not an OS
sandbox for opaque interpreter code.

**The two-layer shell-write contract (audit H1).** Shell (`bash`/`exec`) writes are
governed by two independent layers, and neither is a sandbox:

1. **The mounted bash tool's own validator** (`amplifier-module-tool-bash`,
   `safety.py`) is a *dangerous-command blocklist* — profile-based patterns such as
   `rm -rf /`, `sudo`, `mkfs`, `dd if=/dev/*`. It enforces **no** allowed/denied
   write-path list; the audited `tool-filesystem` allowlist (`is_path_allowed`) gates
   only the filesystem *tools* (`write_file`/`edit_file`), never a shell subprocess. A
   write through `bash` therefore reaches the OS with no path enforcement from either
   external module.
2. **The app governance layer** (`DirectoryPolicy.shell_outside_target`) is the only
   place a shell write meets the protected/denied lists, and it is a *governance
   signal*, not a shell sandbox. Its token pass recognises write-command heads and
   redirects; a concrete protected target token (`> .git/config`, `rm .git/x`) hard
   **blocks**. Because that pass is command-list based, a protected path buried inside a
   quoted interpreter script (`python3 -c "...open('.git/config')..."`), a `sed -i`
   expression, or behind a directory prefix (`vendored/.git/config`) escapes it — and
   layer 1 will not catch it either. To fail closed, a substring scan flags any
   protected path referenced **anywhere** in the command and escalates it to **ask**
   (the human adjudicates; deny-and-continue in restrictive modes). Glob *filters* that
   name a protected dir to exclude it (`find … -not -path './.git/*'`) stay exempt, and
   `.gitignore`/`.github` never match `.git`.

Residual (accepted): a write to a *merely-outside* path (not protected/denied) via an
interpreter — `python3 -c "open('/etc/hosts','w')"` — is not path-confined in the default
`open` posture, matching amplifier-CLI's unconfined bash; the filesystem tool cannot see
interpreter code, so no layer inspects the argument. Protected paths, which are enforced
in both postures, are the crown jewels this fail-closed scan defends.

### 7.2 Gating: app postures + native modes

Two policies share Amplifier's hook/approval mechanism:

- **`GovernanceHook`** (ephemeral `tool:pre`, priority 1000) enforces the app's five
  postures and `outside-project` slot. Direct writes are first checked against the mutable
  `DirectoryPolicy`; reads are denylist-bounded (they roam anywhere outside denied
  directories). The write boundary posture is `permissions.write_boundary`: `open`
  (default, amplifier-CLI parity) adds no app-level gate outside the project — the
  filesystem tool's own allowlist is the sole write enforcement and shell writes roam;
  `guarded` blocks outside writes pre-flight and classifies write-shaped shell escapes
  (write-command heads, redirection targets) as outside-project while read-shaped
  commands roam. Denied and protected paths are enforced in both postures. Denials are
  deny-and-continue and classifier boundary denials enter needs-you.
- **`tool-filesystem`** receives the same effective allowed/denied lists. It remains the
  hard write-path enforcement point, with deny taking precedence. The resolved project
  root is always injected into `allowed_write_paths`; configured lists union rather than
  replacing it.
- The bundle-native stack arrives FROM the composed `anchors` bundle (the packaged tui
  bundle is a thin wrapper that tracks the only fetchable anchors source, Foundation
  `@main`; every other app-owned dependency is pinned and the exception is guarded):

- **`hooks-mode`** (`tool:pre`, pri −20) reads `session_state["active_mode"]` and, per the
  mode's YAML, allows / warns / confirms / blocks a tool (and sets
  `require_approval_tools` from the mode's `confirm` list). With **no active mode it does
  nothing**.
- **`hooks-approval`** (`tool:pre`, pri −10) prompts only for tools in
  `require_approval_tools`; mounted with `policy_driven_only: true` + `default_action:
  continue` + `rules: []`, so its built-in high-risk checks are skipped and a timeout falls
  through to continue.
- **`tool-mode`** lets the app switch the active mode; the app's `_sync_native_mode`
  (`ui/app.py`) bridges the shift+tab postures (`plan`/`brainstorm`) to same-named native
  modes, and `data/modes/{plan,brainstorm,careful}.md` ship self-contained definitions
  (injected via `config.py::inject_mode_search_paths`).

With no active native mode, `hooks-mode`/`hooks-approval` add no policy; the app posture
still governs. Activating `plan`, `brainstorm`, `careful`, or a composed mode adds native
policy. Both ask through the same `ApprovalBroker`, so presentation and allow-always
semantics remain one path.

### 7.3 Approvals (`kernel/approval.py`)

`ApprovalBroker` implements the kernel `request_approval` contract as a FIFO of structured
`ApprovalDetail` tickets. The inline `ApprovalBar` (which replaces the composer, and owns
the keyboard — arrows/tab select, enter confirms, esc denies) answers the head; timeout
resolves to the default (deny) and lands in the `DenialLog`. Ctrl-y on the bar calls
`ApprovalBroker.defer`: it parks a retro-answerable `NeedsYouQueue` item and immediately
resolves the current ticket to Deny, so the failed tool result returns to the model and
work continues. Classifier denials use the same queue without opening a live approval.
A `min_timeout` floor exists because the kernel's

300-second default was silently denying while users were still reading.

### 7.4 Steering, queueing, and turn identity

- **Steering** (`kernel/steering.py` + `model/queues.py`): mid-turn composer text lands in
  the bounded `SteeringQueue` (32 items / 32 KB each). `StepBoundaryBridge` hooks
  `provider:request` on the root session and consumes at most one steer (plus any answered
  needs-you decisions) per step boundary, returning
  `HookResult(action="inject_context", context_injection_role="user")`. Leftover steers are discarded at turn
  end — an unconsumed steer must never become a turn the user didn't send.
- **Queued messages** (shift+enter) occupy a single next-turn slot (a second enqueue
  replaces the first) and *do* increment `turn_id`; steers do not. `turn_id` is app-assigned
  and monotonic, stamped at prompt submit. Alt+Up or a click on the queued strip consumes
  that slot into an empty composer, where Enter can steer it immediately; recall refuses
  when doing so would overwrite a draft or collide with a pending steer.
- **Interrupt context** (`kernel/runtime.py`): after an accepted graceful cancel reaches
  the turn boundary, the runtime appends an assistant `<turn_aborted>` marker before the
  end-of-turn save. It persists and remains model-visible on the next request, but resume
  replay filters it because the reducer already renders the interrupted recap.
- **Turn yield** (`kernel/git_yield.py`, `kernel/turn_yield.py`): bounded git snapshots
  (`diff --numstat` + untracked files, no shell) taken before and after each turn produce
  the `files N · +A/−D` shipped-outcome label and the tests-ran heuristic that enrich
  `PromptComplete`.

### 7.5 Structured questions (`kernel/question.py`)

The host `question` tool uses the shared `NeedsYouQueue`. In interactive postures it waits
for its own answer, returns that answer as the tool result, and consumes it so the bridge
cannot inject it twice. In Auto it returns a successful deferred result immediately and
leaves the item pending; the loop keeps working, and `StepBoundaryBridge` injects a later
answer exactly once at the next provider request.

### 7.6 Rewind and workspace checkpoints (`kernel/rewind.py`, `kernel/checkpoints.py`)

`OutcomeLedger.begin_turn` cuts and exposes a checkpoint before `session.execute`; the
reducer finalizes that same object onto the turn's `TurnRule`. Each target records the
conversation boundary *before* the prompt (`restore_turn_id`), its full prompt label, and an
opaque `workspace_id`. This makes the first and current in-flight prompts restorable and
keeps display ids (`t1`, `t2`, …) separate from never-reused workspace identities. The
picker resolves strictly by checkpoint id, retains the newest 100 targets, navigates prompts
with ←/→ and the scopes below with ↑/↓:

| Scope | Conversation path | Workspace path |
|---|---|---|
| `both` | restore to before the selected prompt | undo selected-and-later tracked edits |
| `conversation` | restore to before the selected prompt | no change |
| `code` | no change | undo selected-and-later tracked edits |

Conversation restore uses Foundation's native message slicing and commits through
`context.set_messages`; the turn-one boundary explicitly sets an empty message list. Only
after that succeeds do `OutcomeLedger.trim_before`, transcript trimming, and prompt return
to the composer occur. A durable `RewindMarker`, including the zero-turn case, prevents
removed turns from reappearing during event replay; `IncrementalSaver.force_save` persists
the shortened context. If confirmation happens while a turn is active, `app_support`
requests the normal graceful interrupt and waits for close-out before calling the adapter.
Opening or navigating the picker has no turn-side effect.

Foundation does not retain filesystem preimages, so `WorkspaceCheckpointStore` supplies a
separate, TUI-owned boundary. It is registered only on the root session's hook bus and
watches direct structured tools (`write_file`, `edit_file`, `create_file`, `delete_file`,
`apply_patch`). At `tool:pre` it resolves known targets and durably writes preimages before
execution continues; `tool:post`/`tool:error` records the resulting state. Restore builds a
per-path chain and applies a strict compare-and-swap: current bytes/mode must still equal the
last tracked after-state. Divergence, later manual edits, or unsafe targets become explicit
per-file skips/warnings; independent safe files may still restore, so workspace restore is
best-effort and not an all-files transaction.

Shell/interpreter calls, child sessions/subagents, MCP/external tools, editors and manual
changes are outside the capture contract. So are outside-workspace and `.git` paths,
symlinked or hard-linked files, non-regular files, files over 8 MiB, and files with extended
attributes/ACLs, non-default flags, or unsafe ownership. The store never follows those paths
or overwrites them during restore. On supported POSIX hosts, capture and mutation walk from
the canonical workspace root with `O_DIRECTORY|O_NOFOLLOW`, retain the verified parent
descriptor through leaf stat/open/rename/unlink and directory fsync, and fail closed per path
when those descriptor-relative primitives are unavailable. A prompt is also bounded to 512
snapshots / 64 MiB.

Every session store is private, but its lease is keyed by canonical workspace root and
shared across sessions. A structured root turn holds that lease from the pre-prompt cut
through finalization; a restore takes the same lease. Contention or a failed durable begin
rejects the new message before `session.execute`, returning its rich draft to the composer.
Manifest/index updates, per-file restore journals, conversation rewind intents, and visible
branch intents make interrupted work restart-completable. A later owner replays them before
new checkpoint work, and unresolved recovery keeps the send gate closed. The store keeps
private manifests, content-addressed blobs, pending records, and journals under the
session's `workspace-checkpoints/` tree, prunes to 100 manifests, and garbage-collects
unreferenced blobs. There is deliberately no redo graph: this is a conflict-safe convenience
over known direct edits, not a Git replacement.

### 7.7 Cost & evidence

- **Cost** (`kernel/cost.py`): Decimal end-to-end; provider-reported `cost_usd` is
  authoritative when present, else the live Helicone table (settings `pricing.live`,
  default on: fresh 24 h `~/.amplifier/pricing_cache.json` applies at startup, otherwise a
  daemon background fetch atomically swaps the module-level table — `CostTracker`
  snapshots it at `start_turn`, so new turns only), else the offline fallback table.
  Usage that cannot be priced increments an `unpriced` counter and the footer/turn-rule
  `$` figures render with a `~` prefix instead of silently recording $0. On resume, prior
  spend is re-seeded by replaying usage events from `ui-events.jsonl` (plus the legacy
  `events.jsonl` for sessions written before the rename).
- **Evidence** (`kernel/evidence.py`, `model/evidence.py`): a tap on the event stream pairs
  answer claims with the tool calls that support them (`EvidenceLink`), skipping denied
  calls; `EvidenceBlock`s render the links as keyboard-navigable superscripts.

---

## 8. Subagents and lanes

`SessionSpawner` (`kernel/spawner.py`) implements the `session.spawn` capability for
in-process subagents: it enforces recursion depth *before* creating anything, creates the
child `AmplifierSession` from the parent config plus the agent overlay, **re-attaches the
shared QueueBridge/trackers to the child's hooks** (so subagent events flow into the same
queue, distinguished by `session_id`/`parent_id`), links child cancellation to the parent
(Esc reaches the whole tree), registers itself on the child for grandchildren, and always
unwinds in `finally`.

On the model side, `LaneRegistry` (`model/lanes.py`) keys lanes by `session_id` and routes by
`parent_id`, tolerating spawn/start races (idempotent registration; retro-patched depths).
The parent view shows a stable delegate lifecycle summary plus the `WorkingStatus` pulse
("Coordinating N agents…"). The `LanesPanel` overlay is the one live child surface (one row
per agent: state glyph · activity · elapsed · tokens · cost), and it auto-opens on fan-out.
Child tool/stream events update their exact lane and focused lane chronology; the parent does
not replay that content. Pending child approvals and failures place only the affected lane in
the shared `attention` state, while final/error/attention events bypass progress coalescing.
Successful native file writes also feed one bounded, expandable, diff-styled `Changed N files`
ToolLine in the owning chronology.

---

## 9. Persistence and resume

`SessionStore` (`kernel/persistence.py`) and `WorkspaceCheckpointStore` write to
`~/.amplifier/projects/<project-slug>/sessions/<session-id>/`:

| Artifact | Contents | Writing discipline |
|---|---|---|
| `transcript.jsonl` | user/assistant messages (system/developer skipped) | atomic write + `.backup` recovery |
| `metadata.json` | session metadata, secrets redacted | atomic write + `.backup` recovery |
| `ui-events.jsonl` | append-only normalized `UIEvent`s | best-effort, never raises; readers fall back to the pre-rename `events.jsonl`, now owned by `hooks-logging` |
| `workspace-checkpoints/` | structured-file manifests, content-addressed preimages, pending records, restore journals | directory mode 0700, files 0600, atomic writes; newest 100 manifests retained |

`IncrementalSaver` debounce-saves on `tool:post`, so a crash loses at most one tool call —
not a turn. Workspace preimages have a stronger local timing guarantee: each is fsynced to
private session storage before the matching structured file tool proceeds. What survives
restarts: the full conversation, metadata, event log, and retained workspace checkpoints —
which together power cost re-seeding, evidence links, lane replay, and pre-prompt restore.

On resume the stored events replay through `TranscriptReducer.replay` (behind a
side-effect-suppressing host proxy), rebuilding the full transcript — tool digests,
delegate summaries, lane focus transcripts, plan state, turn rules with checkpoints —
with a prose-only fallback for sessions that have no usable event log. Resume also
reattaches under the session's **stored** bundle by default (resolved through the normal
`resolve_config` path); settings `resume.use_active_bundle: true` or an explicit
`--bundle` attach under the current one instead, and every divergent outcome is announced.

Before that context is mounted, `kernel/session_integrity.py` completes any assistant tool
call whose result was not durably recorded. It recognizes both top-level `tool_calls` and
content `tool_call`/`tool_use` blocks, preserves every real result, and inserts an immediate
`role: tool` uncertainty result only for a genuine orphan. `RealRuntime.start()` atomically
persists the repaired transcript before model execution and emits a visible warning that
the tool may have executed and real state must be inspected before retry. This application
boundary is intentional: provider-side request repair is ephemeral and can make one request
succeed while the next request rejects the same still-invalid stored transcript.

---

## 10. Testing architecture

The suite (~60+ files under `tests/`) is **fully offline** — no credentials, no network:

| Layer | Approach |
|---|---|
| `model/`, trackers, kernel logic | pure-logic tests consuming events/dataclasses directly |
| `commands/` | tested against a `FakeCommandContext` protocol fake — no Textual involved |
| UI widgets & reducer | per-widget tests + Textual **Pilot** headless driving |
| Flows (approval, interrupt, lanes, rewind, steer/queue, …) | end-to-end scripted turns via `DemoRuntime` |
| Real lifecycle | `test_runtime_offline.py`: a genuine foundation lifecycle with fake provider/context/tool/orchestrator modules mounted from temp dirs via `file://` bundle sources — asserts both event channels, the real approval path, steering injection, persistence output, and durable orphan-tool resume repair; `test_session_integrity.py` covers provider transcript shapes and idempotence |
| Renderer | golden width-matrix (40/80/97/120) plain-text files in `tests/goldens/` (regen via `tests/goldens/regen.py`) |
| Performance | `test_perf_spike.py` enforces ADR-0007's pure-renderer, live-tail-throttle, and hybrid-history 5k frame budgets |

CI (`.github/workflows/ci.yml`): `uv sync --frozen` → `ruff check .` → `pyright src/` →
`pytest -q`.

---

## 11. Where to make changes

| If you're changing… | Touch | Keep in mind |
|---|---|---|
| How a hook payload becomes UI state | `kernel/events.py` only | the single-boundary rule (invariant 1, §1) |
| What a block looks like | `model/blocks.py` + its `_render_*` in `ui/transcript.py` | update goldens; colors are token names only |
| Turn/transcript behavior | `ui/reducer.py` | act through `ReducerHost`, never widgets |
| A keybinding or hint | `ui/keymap.py` | one table drives bindings *and* footer hints |
| A slash command | `commands/` (pure logic) + `builtin.py` (registration) | sync handlers; `CommandContext` only; no Textual imports |
| Trust policy / tool gating | `model/trust.py` + `kernel/governance_hook.py` | deny-and-continue; classifier fails closed |
| Session bring-up order | `kernel/session_factory.py` | capabilities register after create, before execute |
| Anything crossing the thread boundary | `ui/runtime_adapter.py` | marshal in via `run_coroutine_threadsafe`, out via `call_soon_threadsafe` |
| Bundle/module defaults | `bundle.md` | keep the packaged copy byte-identical; never mount printing hooks |
