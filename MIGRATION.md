# Rust Migration Tracker

Incremental, verification-gated port of `amplifier-app-newtui` (Python/Textual) to Rust
(ratatui) under `rust-mvp/`. Architecture: the Rust app is a pure protocol CLIENT of the
existing Python `serve` backend (`amplifier-newtui serve`) over stdio JSON — the
codex-tui / codex-core split. amplifier-core / amplifier-foundation / Python app behavior
are untouched.

**Method**: one unit at a time. Each unit = idiomatic Rust + behavioral-equivalence tests
(ported from the Python unit's own tests; oracle-diffed against real Python for pure
functions where practical). DONE only when `cargo test` + `cargo clippy` pass and the
Python suite stays green. Commit + push per unit/batch. Never force-green: gaps are
recorded here, not papered over.

**Statuses**: `todo` → `ported` (code written, tests not fully verified) → `verified`
(cargo test green for its tests) | `blocked(reason)` | `n/a (reason)`.

**Resume protocol**: at session start, read this file; continue from the first non-verified
unit, top to bottom.

## Layer 1 — model/ (pure domain)

Port order respects intra-layer deps: wave 1 = independent units; then turn → blocks →
{modes, lanes} → native_modes. Rust home: `rust-mvp/src/model/<unit>.rs`, tests inline.

| Unit | Python source | Python tests pinned | Status | Caveats |
|---|---|---|---|---|
| model/evidence | model/evidence.py | test_model_blocks.py (EvidenceLink uses) | verified | pydantic frozen/extra=forbid → serde deny_unknown_fields; direct construction cannot fail |
| model/formatting | model/formatting.py | test_model_formatting.py | verified | round-half-even edges (9950, 999500, 1050) oracle-checked vs real Python |
| model/trust | model/trust.py | test_model_modes_trust.py | verified | ValueError → Result<_, TrustValueError> (exact messages); casefold→to_lowercase (ASCII-identical); strings/thresholds oracle-checked |
| model/config | model/config.py | test_model_config.py | verified | py_repr diverges from Python repr at extreme float magnitudes; typed diff equality (no True==1); underscore/unicode numeric literals parse as Str |
| model/injection | model/injection.py | test_model_injection.py | verified | regex crate (no backtracking) oracle-diffed byte-for-byte on all pinned payloads; str/bytes/Display typed entry points; +regex dep |
| model/redaction | model/redaction.py | test_model_redaction.py | verified | serde_json::Value instead of arbitrary objects; 23-case differential oracle matched byte-for-byte |
| model/terminal | model/terminal.py | test_model_terminal.py | verified | duck-typed clamp → set_cols(i64)/set_cols_str; junk input falls back to 80 |
| model/queues | model/queues.py | test_model_turn_queues_lanes.py | verified | ValueError/KeyError → QueueError w/ exact Python messages; listener closures → ListenerId; counts() HashMap unordered |
| model/turn | model/turn.py | test_model_turn_queues_lanes.py | verified | Decimal cost arithmetic via rust_decimal (banker's rounding oracle-pinned); pydantic Field(ge/le) runtime validation not replicated; trim_to → Result |
| model/blocks | model/blocks.py | test_model_blocks.py | verified | TranscriptBlock = serde internally-tagged enum (exact `kind` literals); pydantic range validators approximated by unsigned types (upper bounds unchecked); frozen → immutability by convention; wire shape oracle-pinned vs model_dump_json |
| model/modes | model/modes.py | test_model_modes_trust.py | verified | MODE_PROFILES dict → const table in cycle order; negative-modulo cycle wrap oracle-verified |
| model/lanes | model/lanes.py | test_model_turn_queues_lanes.py, test_model_lane_steering.py | verified | kwargs → RegisterOptions/LaneUpdate structs; accessors return owned clones; Field(ge=0) via unsigned types; fuzzy routing + labels oracle-pinned (test_model_lane_steering.py targets queues unit, already ported) |
| model/native_modes | model/native_modes.py | test_model_native_modes.py | verified | add/remove/clear return new instances (identity pin → value-equality pin); notice strings oracle-pinned verbatim |

**Layer 1 status: COMPLETE — 13/13 units verified, 140 lib tests + 3 bin tests green, clippy clean.**

## Layer 2 — kernel pure logic

Only client-side-relevant pure logic is ported; process/IO orchestration stays in the
Python backend behind `serve`.

| Unit | Python source | Python tests pinned | Status | Caveats |
|---|---|---|---|---|
| kernel/events (normalization) | kernel/events.py | test_kernel_events_normalize.py, test_kernel_event_canary.py | verified | UIEvent = internally-tagged serde enum; wire shape oracle-checked vs model_dump(mode="json") incl. Decimal-as-string; canary/QueueBridge cases deferred to queue_bridge seam (backend) |
| kernel/cost | kernel/cost.py | test_kernel_cost.py, test_cost_parity_appcli.py | verified | FALLBACK_PRICING embedded as JSON w/ drift-canary test vs Python source literals; module-global table → RwLock<Arc<PricingTable>>; live-fetch via ureq untested-network; usage→tallies client wiring tracked as its own unit below |
| kernel/git_yield | kernel/git_yield.py | test_kernel_turn_yield.py (git cases) | verified | asyncio subprocess → blocking Command w/ kill-on-timeout; capture_git_patch/_line_count oracle-backed; +tempfile dev-dep |
| kernel/turn_yield | kernel/turn_yield.py | test_kernel_turn_yield.py | verified | tracker cases ported; exit-code coercion (bool/float/None) oracle-checked; RealRuntime close-out cases stay backend |
| kernel/approval (decision logic) | kernel/approval.py | test_kernel_approval.py | verified | async broker → sync ticket-id API (request_approval returns ticket_id; answer/resolve_timeout); all 8 approval cases ported; governance-file cases belong to kernel/governance_hook below |
| kernel/reminder_trust | kernel/reminder_trust.py | test_denial_injection_trust.py | verified | 2 cases skipped (need kernel/runtime replay + ui render layers); regex edges oracle-checked |
| kernel/safety | kernel/safety.py | test_kernel_safety.py | verified | minimal DirectoryPolicy surface inline-ported (check_write/read, within_allowed, shell_outside_target incl. Python ';'-strip quirk, oracle-pinned); resolve(strict=False) approximated lexically (no symlink resolution); hand-rolled shlex subset |
| kernel/evidence | kernel/evidence.py | test_kernel_evidence.py | verified | lookbehind sentence-split emulated by hand-rolled scanner (oracle-checked); is_top_level_session inline-ported from persistence.py |
| kernel/surface_hint | kernel/surface_hint.py | test_kernel_surface_hint.py | verified | all 11 cases; hint text oracle-exact (incl. U+2264); duck-typed context → SurfaceHintContext trait |
| kernel/steering | kernel/steering.py | test_kernel_steering.py, test_kernel_lane_steering.py | verified | 15 non-runtime cases + exact injection strings oracle-pinned; bridge owns queues (Python borrows) — revisit at app wiring; RealRuntime cases stay backend |
| kernel/trackers/runtime_status | kernel/trackers/runtime_status.py | test_kernel_trackers.py | verified | u64 counters saturate-at-0 where pydantic would raise (pathological only); CostFn → Result<Decimal,String>; panicking listener propagates (crate convention) |
| kernel/trackers/stream_status | kernel/trackers/stream_status.py | test_kernel_trackers.py | verified | all 6 cases + hook-entrypoint test; register_hooks records interest only (no bound handlers); listener crash isolation via catch_unwind |
| kernel/trackers/task_status | kernel/trackers/task_status.py | test_kernel_trackers.py | verified | 10 cases + oracle test; spawner test file pins kernel/spawner.py (backend) entirely; register_hooks not ported (no in-crate hooks registry) |
| kernel/display | kernel/display.py | test_kernel_trackers.py (DisplaySystem cases) | verified | emit is Box<dyn FnMut(Notification)>; QueueBridge stand-in = VecDeque in tests (bridge is backend) |
| kernel/governance_hook (decision logic) | kernel/governance_hook.py | test_kernel_approval_governance.py (40 cases — brief said 35, file has 40, all ported) | verified | async hooks → sync; wait_for cancellation → wall-clock elapsed check (late verdict discarded, same offline-floor degradation); classifier edge cases oracle-checked |
| kernel/usage→tallies client wiring | (rust-mvp core_client/app) | wire shape + costs oracle-pinned vs serve/cost_of | verified | $0.0000 gap closed: Tallies.cost now Decimal via kernel::cost::CostTracker (session + per-turn); tokens = output tokens (Python ↓ tok figure); serve_mock emits realistic usage events; headless test adapted to exact-Decimal assertions (strictly stronger) |
| kernel/file_mentions | kernel/file_mentions.py | test_kernel_file_mentions.py | verified | casefold→to_lowercase; non-UTF-8 names via to_string_lossy (unreachable in pinned tests) |
| kernel/mention_expansion | kernel/mention_expansion.py | test_kernel_mention_expansion.py | n/a | thin wrapper over external amplifier_foundation.mentions (engine upstream, ~658 lines); serve backend already expands on submit (runtime.py _expand_mentions) and bound-skips surface as Notification events — Rust client sends raw prompt; reimplementing upstream would violate the no-reimplementation rule |
| kernel/prompt_history | kernel/prompt_history.py | test_kernel_prompt_history.py | verified | all 15 cases + get_project_slug inline-ported from kernel/config.py; timestamp comment UTC not local; negative-limit escape hatch unrepresentable (usize) |
| kernel/serve, runtime, session_manager, session_factory, spawner, persistence, config, config_ops, mcp_config, setup, updater, bundle_*, notify_admin, routing_admin, source_admin, clipboard, demo, tool_cli, queue_bridge, recipes, reset, rewind, session_ops, compaction, jsonl, approval broker, directory_permissions (persistence half; decision surface inline-ported into kernel/safety) | — | — | n/a | backend concerns: stay Python behind `serve`; the Rust client consumes their effects via the protocol |

**Layer 2 status: COMPLETE — 18 units verified, 1 n/a (mention_expansion); 384 lib + 3 bin tests green, clippy clean.**

## Layer 3 — commands/

| Unit | Python source | Python tests pinned | Status | Caveats |
|---|---|---|---|---|
| commands/registry | commands/registry.py | test_commands_registry.py | verified | all 21 cases; CommandContext Protocol → trait (forward-ref members Box<dyn Any> until doctor/improve integrate); errors carry exact CPython/pydantic-validator messages |
| commands/builtin | commands/builtin.py | test_commands_builtin.py | verified | all 22 cases; BUILTIN_COMMANDS tuple → builtin_commands() fn; /doctor install probe = current_exe (no importlib.metadata analogue); skills.rs tests now use real build_registry |
| commands/context | commands/context.py | test_commands_context.py | verified | segment math f64 op-order identical, 9 rows oracle-pinned bit-for-bit; app-level context tests belong to ui layer |
| commands/copy | commands/copy.py | test_commands_copy.py | verified | all 7 cases, exact redaction strings |
| commands/doctor | commands/doctor.py | test_commands_doctor.py | verified | all 19 cases; importlib probe → injected probe fn; AnchorsPinStatus → trait (kernel/updater not ported — test-local mirror pins exact strings); +serde_yaml dep |
| commands/export | commands/export.py | test_commands_export.py | verified | injectable datetime → ExportStamp struct; now() stamps UTC vs Python local (filename only) |
| commands/improve | commands/improve.py | test_commands_improve.py | verified | all 10 cases; Counter insertion order preserved via ask_order Vec |
| commands/permissions | commands/permissions.py | test_commands_permissions.py | verified | all 11 cases; exact CPython list.remove message; frozen-mutation pin is compile-time |
| commands/skills | commands/skills.py | test_commands_skills.py | verified | all 8 cases; tests use minimal build_registry stand-in until commands/builtin lands (then switch to real one) |

**Layer 3 status: COMPLETE — 9/9 commands units verified.**

## Layer 4 — ui/ (ratatui rebuild)

| Unit | Python source | Python tests pinned | Status | Caveats |
|---|---|---|---|---|
| ui/reducer | ui/reducer.py | test_ui_reducer_*.py (7 files) + lane_summary + lanes_telemetry reducer cases | verified | 81 tests; owns LaneReducer<ReplayGate<H>> (one id sequence/registry); Python latent KeyError in _tool_error degraded to fallback ToolLine (commented); foundation-fork e2e case not portable (external pkg) |
| ui/lane_reducer | ui/lane_reducer.py | test_ui_lane_reducer.py | verified | all 13 cases + oracle; LaneReducer OWNS registry/allocator — sharing decision deferred to ui/reducer port |
| ui/segments | ui/segments.py | (oracle-pinned) | verified | markup emitters byte-identical to Textual escape (oracle); to_ratatui_line replaces to_rich_text; link painting → app-assembly OSC 8 |
| ui/transcript_render | ui/transcript_render.py | test_ui_transcript_render.py, test_ui_render_*.py | verified | 53 tests; answer_spans-fed cases use oracle-pinned span dumps (rewire to live_tail now that it landed — parity-pass item); unknown-kind TypeError → exhaustive enum |
| ui/transcript (view) | ui/transcript.py | test_ui_transcript_view.py | verified | all 14 cases adapted (clicks → BlockWidget::click(row), timers injected, messages → TranscriptMsg enum); archive markup oracle-pinned byte-for-byte; assembly wiring documented in module doc |
| ui/live_tail | ui/live_tail.py | test_ui_transcript_live_tail.py | verified | 26 tests; span pipeline oracle-verified byte-identical on 17-case corpus; timers/paint/consolidate message → return values for app assembly; lookaround italics regex rewritten lookaround-free |
| ui/composer | ui/composer.py | test_ui_composer.py, test_ui_prompt_history.py | verified | 28 tests; ImageAttachment local stand-in (kernel/clipboard unported); mention regex lookbehind-free, oracle-verified; selection/clipboard model is app-assembly |
| ui/approval_bar | ui/approval_bar.py | test_ui_approval.py, test_ui_approval_wrap.py | verified | 20 tests; messages → KeyOutcome/ApprovalMsg return values; wrap decision at width 80 oracle-checked; colors await themes wiring at assembly |
| ui/footer | ui/footer.py | test_ui_footer.py | verified | 24 tests; fit-ladder + wrap thresholds oracle-checked at 5 widths; pass content width (terminal-2) at assembly |
| ui/keymap | ui/keymap.py | test_ui_keymap.py | verified | all 15 cases 1:1 |
| ui/lanes_panel | ui/lanes_panel.py | test_ui_lanes.py, test_ui_lanes_telemetry.py (panel cases) | verified | 31 tests; width-budget goldens at 80/58 oracle-pinned; reducer-owned telemetry cases deferred to ui/reducer |
| ui/needs_you | ui/needs_you.py | test_needs_you_real.py | verified | 13 tests; Textual pilot cases skipped w/ reasons |
| ui/palette | ui/palette.py | test_ui_palette.py | verified | 13 tests; widget/pilot cases skipped w/ reasons |
| ui/plan_panel | ui/plan_panel.py | test_ui_plan_panel.py | verified | all 7 cases, exact glyphs/widths |
| ui/queued_strip | ui/queued_strip.py | test_ui_rewind_queued.py (queued cases) | verified | 4 tests |
| ui/rewind_strip | ui/rewind_strip.py | test_ui_rewind.py | verified | 13 tests |
| ui/notices | ui/notices.py | test_ui_chrome.py (NoticeSlot cases) | verified | 9 tests |
| ui/notifications | ui/notifications.py | test_ui_notifications.py | verified | 15 tests, pure port |
| ui/themes | ui/themes.py | test_ui_themes.py | verified | exact hex tables |
| ui/splash | ui/splash.py | test_ui_splash.py | verified | 13 tests; CPython MT19937 replicated — dissolve frames byte-identical |
| ui/motion | ui/motion.py | (oracle-pinned) | verified | shimmer_band pinned vs live Python for 5 lengths; timers are app-assembly |
| ui/chrome | ui/chrome.py | test_ui_chrome.py | verified | 12 tests; TitleBar reactive watchers → setters + terminal-title dedupe; spinner timer is app-assembly |
| ui/file_mentions | ui/file_mentions.py | test_ui_file_mentions.py | verified | 4 tests; MentionHost trait replaces Textual widget plumbing |
| ui/app_support | ui/app_support.py | test_ui_app_support.py + 2 deferred outcome cases | verified | pure helpers ported (esc chain → resolve_esc, plan ladder → plan_surface, clipboard via std::process); orchestration fns (echo_steer, finish_turn_queues, footer_state, announce_*) are app-assembly items |
| ui/app (composition root) | ui/app.py | test_ui_snapshots.py + flow tests (approval, modes, interrupt, palette, steer queue, demo e2e) adapted headless | verified | assembled App implements ReducerHost/CommandHost over Rc<RefCell<UiState>>; legacy demo reducer deleted; serve_mock upgraded to correlated tool_pre/post (matches real serve). NOT WIRED (recorded): mouse events, per-widget shimmer timers, steer delivery (no wire op), needs-you decision actions, resume replay, session ops over the wire, OSC title/notify/hyperlink/clipboard, kitty probe, image paste |
| ui/runtime_adapter | ui/runtime_adapter.py | test_runtime_adapter_*.py | verified | RuntimeAdapter trait + ClientRuntimeAdapter over Box<dyn Runtime>; serve wire carries only submit/approve/interrupt — session ops answer Python "session still starting" until protocol grows ops (documented); config_ops save contract ported privately, oracle-verified; asyncio marshalling cases n/a with reasons |
| ui/term_probe | ui/term_probe.py | test_ui_term_probe.py | verified | patch_legacy_alt_named_keys n/a (Textual XTermParser surgery); crossterm alt+enter check flagged for integration |
| ui/config_view | ui/config_view.py | test_ui_config_view.py | verified | all 7 cases; spans oracle-verified incl. 'no None configured' quirk |
| ui/directory_admin | ui/directory_admin.py | test_ui_directory_admin.py | verified | all 5 cases; host trait flattens adapter/allocator; persistence stays behind protocol |
| ui/session_ops_view | ui/session_ops_view.py | test_ui_session_ops_view.py | verified | all 14 cases + format_time_ago oracle (incl. 0y quirk); input structs mirror unported kernel session_ops/session_manager types (re-export when those port) |
| ui/command_context | ui/command_context.py | test_command_context_contract.py, test_command_context_app.py | verified | contract enforced by compiler (AppCommandContext: &dyn CommandContext); CommandHost trait = the app-assembly surface |
| ui/config_admin | ui/config_admin.py | test_ui_config_admin.py | verified | all 8 cases; config_ops save contract pinned via oracle (scope-path/deep-merge stays backend) |
| ui/session_ops_controller | ui/session_ops_controller.py | test_ui_session_ops_controller.py | verified | all 26 cases; run_worker async → sync SessionOpsAdapter trait; mcp_config touchpoints live on SessionOpsHost |
| ui/demo_wiring | ui/demo_wiring.py | test_kernel_demo_data.py (data slice) + oracle tests | verified | inlines minimal kernel/demo data slice; tick_tokens RNG draws pinned as constants (CPython string-seeding not reimplemented); interrupted-close-out branch stays with legacy DemoRuntime |

## Layer 5 — Integration

| Unit | Status | Caveats |
|---|---|---|
| Rust UI ↔ `amplifier-newtui serve` live end-to-end (real model turn; approvals by ticket id) | verified | 2026-07-26: live turn through the assembled reducer pipeline — real answer "pong", session_cost $1.1459575 from real usage records; approval round-trip proven with `serve --mode build` (ticket approval-1 answered "Allow once" over stdin; ping.txt written). Pinned as #[ignore] core_client::live_serve_end_to_end (run with --ignored; ~$1.15/turn from fresh-session cache write) |

## Layer 6 — Parity pass

| Unit | Status | Caveats |
|---|---|---|
| One test per DESIGN-SPEC behavior | verified | 48 behaviors enumerated at checkbox granularity in rust-mvp/PARITY.md: 40 covered by existing named Rust tests, 4 added this pass (activity-tail cap 3, approval-open notice, steer-then-queue client half, approval-while-lane-focused auto-return — the last also fixed an unwired assembly gap mirroring mount_approval), 4 n/a (architecture property, backend-only, mouse unwired) |

**Layers 4/5/6 status: COMPLETE.** Final: 1066 lib + 12 bin tests green (+1 ignored live test), clippy zero warnings, Python suite 2295 green.

## Log / caveats

- 2026-07-26: wave 1 (8 independent model units, 7 worktree porters) integrated: 81 unit tests ported+green, clippy clean, full suite 86 passing. deps: +regex, +serde.
- 2026-07-26: tracker created; rust-mvp baseline: 5 tests passing, clippy not yet part of gate.
