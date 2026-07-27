# Rust Migration Tracker

> **MOVED (2026-07-27).** The Rust client no longer lives in this repo. `rust-mvp/` was
> extracted into a standalone repo — `~/dev/amplifier-app-newtui-rust`, GitHub
> [`michaeljabbour/amplifier-app-newtui-rust`](https://github.com/michaeljabbour/amplifier-app-newtui-rust)
> (private) — with full history preserved via `git subtree split`. This file is frozen at
> the split point; the ledger continues as `MIGRATION.md` in the new repo. The Rust client
> finds this Python checkout via `AMPLIFIER_PY_CHECKOUT` or as a sibling directory.

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
| model/queues | model/queues.py | test_model_turn_queues_lanes.py, test_model_lane_steering.py | verified | ValueError/KeyError → QueueError w/ exact Python messages; listener closures → ListenerId; counts() HashMap unordered |
| model/turn | model/turn.py | test_model_turn_queues_lanes.py | verified | Decimal cost arithmetic via rust_decimal (banker's rounding oracle-pinned); pydantic Field(ge/le) runtime validation not replicated; trim_to → Result |
| model/blocks | model/blocks.py | test_model_blocks.py | verified | TranscriptBlock = serde internally-tagged enum (exact `kind` literals); pydantic range validators approximated by unsigned types (upper bounds unchecked); frozen → immutability by convention; wire shape oracle-pinned vs model_dump_json |
| model/modes | model/modes.py | test_model_modes_trust.py | verified | MODE_PROFILES dict → const table in cycle order; negative-modulo cycle wrap oracle-verified |
| model/lanes | model/lanes.py | test_model_turn_queues_lanes.py, test_model_lane_steering.py | verified | kwargs → RegisterOptions/LaneUpdate structs; accessors return owned clones; Field(ge=0) via unsigned types; fuzzy routing + labels oracle-pinned (test_model_lane_steering.py targets the queues unit — now pinned 1:1 in model/queues) |
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
| kernel/demo (turn scripts) | kernel/demo.py | test_kernel_demo_turns.py (15/15) | verified | DemoScript engine + ScriptedDemoRuntime in runtime.rs: all six turn scripts (seed/build/auto/plan/brainstorm/agents) as virtual-time UIEvent scripts with pinned tick_tokens draws and approval/steer/mode/interrupt hooks; 265/265 events oracle-identical per the wave-1 verification (commit 5664a53 — oracle diff not re-run this pass); composition swap (main.rs demo_app still plays the legacy inline DemoRuntime): see ui/app row |
| kernel/serve, runtime, session_manager, session_factory, spawner, persistence, config, config_ops, mcp_config, setup, updater, bundle_*, notify_admin, routing_admin, source_admin, clipboard, tool_cli, queue_bridge, recipes, reset, rewind, session_ops, compaction, jsonl, approval broker, directory_permissions (persistence half; decision surface inline-ported into kernel/safety) | — | — | n/a | backend concerns: stay Python behind `serve`; the Rust client consumes their effects via the protocol |

**Layer 2 status: COMPLETE — 19 units verified (kernel/demo reclassified from backend-n/a to a ported engine, 2026-07-27), 1 n/a (mention_expansion); clippy clean.**

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
| ui/reducer | ui/reducer.py | test_ui_reducer_*.py (7 files) + lane_summary + lanes_telemetry reducer cases + test_needs_you_real.py::test_reducer_routes_decision_id_to_host | verified | 83 tests; owns LaneReducer<ReplayGate<H>> (one id sequence/registry); Python latent KeyError in _tool_error degraded to fallback ToolLine (commented); foundation-fork e2e case not portable (external pkg) |
| ui/lane_reducer | ui/lane_reducer.py | test_ui_lane_reducer.py | verified | all 13 cases + oracle; LaneReducer OWNS registry/allocator — sharing decision deferred to ui/reducer port |
| ui/segments | ui/segments.py | (oracle-pinned) | verified | markup emitters byte-identical to Textual escape (oracle); to_ratatui_line replaces to_rich_text; link painting → app-assembly OSC 8 |
| ui/transcript_render | ui/transcript_render.py | test_ui_transcript_render.py, test_ui_render_*.py | verified | 53 tests; answer_spans-fed cases use oracle-pinned span dumps (rewire to live_tail now that it landed — parity-pass item); unknown-kind TypeError → exhaustive enum |
| ui/transcript (view) | ui/transcript.py | test_ui_transcript_view.py | verified | all 14 cases adapted (clicks → BlockWidget::click(row), timers injected, messages → TranscriptMsg enum); archive markup oracle-pinned byte-for-byte; assembly wiring documented in module doc |
| ui/live_tail | ui/live_tail.py | test_ui_transcript_live_tail.py, test_ui_transcript_render.py::TestAnswerMarkdown (5 cases) | verified | 31 tests; span pipeline oracle-verified byte-identical on 17-case corpus; timers/paint/consolidate message → return values for app assembly; lookaround italics regex rewritten lookaround-free |
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
| ui/app (composition root) | ui/app.py | test_ui_snapshots.py + flow tests (approval, modes, interrupt, palette, steer queue, demo e2e) adapted headless; mouse cases (tool-line click toggle, approval chip click, footer badge click, mode-badge click, wheel anchor, lane row click) | verified | assembled App implements ReducerHost/CommandHost over Rc<RefCell<UiState>>; legacy demo reducer deleted; serve_mock upgraded to correlated tool_pre/post (matches real serve). NOW WIRED: mouse (crossterm capture; FrameLayout hit-testing → BlockWidget::click / ApprovalBar::click / LanesPanel::on_click / waiting-badge / mode-badge; wheel drives the follow anchor; Drag/Up drive a transcript drag-selection — REVERSED row highlight, 0.4s copy-on-settle with Python's exact "copied on select · N chars" notice, ctrl+c copies-and-clears an active selection with "copied · N chars" [+ honest empty-clipboard suffix] and short-circuits quit only then, plain click clears), boot.progress splash phases (protocol record → Python boot_progress text "action · detail", snake_case read as words; stderr BootChatter kept as fallback that never overwrites a structured phase), kitty probe at startup, OSC-0 terminal title (TitleChanged → deduped main-loop write), attention bell (\x07 on turn ≥ threshold + deferrals; focus not modeled — Python's App.bell rung only, no OSC 777 toast), launch flags → serve args + boot-failure diagnosis + backend-exit detection (see Launch surface section). NOT WIRED (recorded): per-widget shimmer timers (one global tick clock at Python cadences instead of Textual per-widget timers), needs-you decision actions inside the listing (chips render; click-to-resolve does not act), resume replay (--resume forwards to serve; no history replay into the transcript over the wire), session ops over the wire (adapter answers Python's "session still starting" until the protocol grows ops), OSC 777 notify / OSC 8 hyperlinks / OSC 52 clipboard (selection copies use the OS tool only, synchronous), image paste, composer-internal text selection, first-run provider gate (stays Python launcher-side; Rust surfaces provider-missing via the boot-failure diagnosis). WIRED IN WAVE 2 (landed): boot-time SessionBanner posting (demo exact DEMO_BANNER; protocol sessions synthesized identity detail — session.started carries no version), steer-echo transcript block w/ consume/discard sync, evidence keyboard interaction (§10: focused_evidence routes ←/→/enter/esc to BlockWidget; ExpandEvidenceClaim deep-links to the grounding tool line, CloseEvidence removes; deep-link is anchor-release + scroll offset, not animated; archived-block ScrollRequests unhandled), lane-unfocus "back to parent session" notice, demo composition = ScriptedDemoRuntime playing the real kernel/demo.py script (seed replay $0.40→$0.57, spec/lane_seed/evidence lookups, steer/mode hooks; interrupted_close bridges on cancel-event rather than Python live-read — mutex deadlock avoidance; flow test rewritten to the real script, strictly closer to Python) + 9 new flow adaptations (rewind, lanes, thinking ×2, ledger, plan panel ×2, palette zero-match, 3 frame-lock snapshots — the lane-tail one fixed a real draw bug: tail was clipped to 1 row). SCAFFOLDING PURGED (2026-07-27): the `--direct` LiveRuntime spike (src/live.rs — the documented "illustrative UI-calls-provider shortcut, not the target architecture") deleted with its main.rs arm; its only externally-valuable pin (fallback-table pricing of a 40/12 claude-sonnet-4-5 usage → $0.0003) already lives in kernel::cost's own suite (`cost_of` and `estimate_cost` cases). Silent demo fallbacks deleted: no-key/--direct → demo and backend-spawn-failure → demo are gone; spawn failure now boots into the honest diagnosis (see backend resolution row). Legacy pre-assembly `DemoRuntime` (serve-mock-shaped single turn, superseded by ScriptedDemoRuntime and referenced by nothing) deleted from runtime.rs. `--demo` (ScriptedDemoRuntime, Python-parity feature) is reachable ONLY via the explicit flag. PORTED 2026-07-27 (user-reported data loss — steer never reached the backend): mid-turn steer wire delivery via a user-authorized ADDITIVE protocol op `{"op":"steer","text":...}` in kernel/serve.py, routed into RealRuntime.steering (the SAME queue the in-process TUI shares with the StepBoundaryBridge); serve also drains leftover steers at turn end (finish_turn_queues parity) so an unconsumed steer never leaks into a later turn. Client side: on_steer keeps the local echo/badge queue AND sends the op; the backend's `Applying steer: …` narration consumes the local copy (wire-driven echo drop, root-session narration match), so no false turn-end discard notice. Queued next-turn prompts already reach the wire at prompt_complete via drain_turn_queues → submit_queued → submit op (now pinned). Pins: tests/test_serve_offline.py (steer op lands in the runtime queue + applies at the next step boundary + leftover drain), core_client::steer_over_process_boundary_applies_and_drops_echo (serve_mock now accepts steer and emits the real narration shape). BANNER DIVERGENCE 2026-07-27 (user report: duplicate boot line): protocol sessions no longer append the synthesized headline-less SessionBanner (it duplicated the footer identity verbatim; session.started carries no version headline) — demo boot + /about banners unchanged; pinned by main::test_session_started_adds_no_duplicate_banner. CHARACTER-RANGED SELECTION 2026-07-27 (user report: "can only select lines, not blocks of text" — closes the recorded whole-row gap): transcript drag-selection now anchors at a (line, column) cell and extends to another, terminal style — partial first line from the anchor column, full middle lines, partial last line to the head column (drag-direction normalized); the REVERSED highlight is per-span over exactly those cells and the copy extracts exactly that substring from the rendered plain lines (columns are terminal cells via unicode-width, matching Python's rich cell math; a cell mid-wide-glyph rounds to include the whole glyph in highlight and copy alike). Copy-on-settle (0.4s), ctrl+c copy-and-clear, and click-clears semantics unchanged. Test note (authorized rename/strengthen of the row-based pins): the `drag_select_rows` helper now anchors explicitly at column 0 and releases past line end so the three Python-adapted row pins keep their whole-line expectations, and three new pins land in main.rs — column_ranged single-line mid-selection + reverse drag, partial first/last lines with per-cell REVERSED boundaries, wide-glyph boundary (你好). Python side verified ALREADY character-ranged (Textual 8 native screen selection: char-accurate highlight + extraction incl. wrapped lines, wide glyphs, cross-block partial first/last — probed empirically); the one coarse case is framework-level (a drag endpoint on a margin/gap row falls back to whole-widget SELECT_ALL inside Textual's Screen select machinery). Pinned by tests/test_ui_composer.py::test_transcript_selection_is_character_ranged so an upgrade can never degrade granularity back to lines |
| ui/runtime_adapter | ui/runtime_adapter.py | test_runtime_adapter_*.py | verified | RuntimeAdapter trait + ClientRuntimeAdapter over Box<dyn Runtime>; serve wire carries submit/steer/approve/interrupt (steer added 2026-07-27, user-authorized additive op) — session ops answer Python "session still starting" until protocol grows ops (documented); config_ops save contract ported privately, oracle-verified; asyncio marshalling cases n/a with reasons |
| ui/term_probe | ui/term_probe.py | test_ui_term_probe.py | verified | patch_legacy_alt_named_keys n/a (Textual XTermParser surgery); crossterm alt+enter check flagged for integration |
| ui/config_view | ui/config_view.py | test_ui_config_view.py | verified | all 7 cases; spans oracle-verified incl. 'no None configured' quirk |
| ui/directory_admin | ui/directory_admin.py | test_ui_directory_admin.py | verified | all 5 cases; host trait flattens adapter/allocator; persistence stays behind protocol |
| ui/session_ops_view | ui/session_ops_view.py | test_ui_session_ops_view.py | verified | all 14 cases + format_time_ago oracle (incl. 0y quirk); input structs mirror unported kernel session_ops/session_manager types (re-export when those port) |
| ui/command_context | ui/command_context.py | test_command_context_contract.py, test_command_context_app.py | verified | contract enforced by compiler (AppCommandContext: &dyn CommandContext); CommandHost trait = the app-assembly surface |
| ui/config_admin | ui/config_admin.py | test_ui_config_admin.py | verified | all 8 cases; config_ops save contract pinned via oracle (scope-path/deep-merge stays backend) |
| ui/session_ops_controller | ui/session_ops_controller.py | test_ui_session_ops_controller.py | verified | all 26 cases; run_worker async → sync SessionOpsAdapter trait; mcp_config touchpoints live on SessionOpsHost |
| ui/demo_wiring | ui/demo_wiring.py | test_kernel_demo_data.py (data slice) + oracle tests | verified | inlines minimal kernel/demo data slice; tick_tokens RNG draws pinned as constants (CPython string-seeding not reimplemented); interrupted-close-out branch now ported in runtime.rs DemoScript (oracle_interrupted_build_turn_close_out) |

## Launch surface (main.py) & serve protocol edges

The CLI entry point (`main.py`) is a launcher plus an admin CLI. Only the TUI-launch path
migrates; everything else is `n/a (na-cli)` — CLI administration the Rust *client* never
needed (it stays available as `amplifier-newtui <cmd>` beside the Rust binary).

| Unit | Python source | Python tests pinned | Status | Caveats |
|---|---|---|---|---|
| launch flags | main.py interactive group / `serve` options | (Rust pins: main::test_launch_flags_assemble_backend_command, test_app_seeds_initial_mode, test_app_defaults_to_auto_without_initial_mode) | verified | `--bundle`/`--provider`(-p)/`--model`(-m)/`--mode`/`--resume` parsed by main.rs `parse_launch_flags` (click grammar: `--flag value` and `--flag=value`) and forwarded as backend `serve` args; `--mode` also seeds the opening posture |
| backend resolution (real session by default) | main.py `_interactive_launch` | main::test_launch_flags_assemble_backend_command (resolve_backend arms: env override / checkout / PATH) + main::test_spawn_failure_boots_into_boot_failure_diagnosis | verified | `resolve_backend`: `AMPLIFIER_SERVE_CMD` wins outright; inside the checkout spawns the REAL `uv run --project <checkout> amplifier-newtui serve`; otherwise the installed `amplifier-newtui serve` from PATH. NO scripted fallback: a spawn failure boots into announce_boot_failure's exact diagnosis (`⊘ session failed to start · backend spawn failed (…)` + doctor hint) with an `UnspawnedBackend` runtime seat that answers any submit with an honest turn-failed error; `backend/serve_mock.py` survives only as the cross-process test's fixture |
| exit resume hint | main.py `_print_resume_hint` | main::test_resume_hint_exact_text_after_session_started | verified | exact two-line farewell after a `session.started` id; demo/unstarted sessions print nothing |
| serve error records | kernel/serve.py error records | main::test_boot_error_record_dismisses_splash_with_exact_diagnosis, test_midsession_error_and_backend_exit_notices | verified | `WireEvent::Error`: during boot → `announce_boot_failure` verbatim (`⊘ session failed to start · <detail>` + doctor hint, Python app_support strings; blank message falls back to the exception type); mid-turn → `turn failed · <error>` notice matching Python `_submit_prompt`'s except-arm, including its defect of leaving `turn_active` true (notice only, no turn close-out) |
| backend-exit detection | — (Rust-only hardening) | main::test_backend_eof_before_identity_runs_boot_failure_diagnosis, test_midsession_error_and_backend_exit_notices | verified | `Msg::BackendExited` on backend stdout EOF: pre-identity runs the same boot-failure diagnosis (previously the splash hung forever; Python has no analogue — the backend is in-process); mid-session posts `backend exited · session lost — ctrl+d to quit`; a trailing EOF never clobbers an already-rendered error diagnosis |
| first-run provider gate | main.py `_first_run_gate` (+ kernel/setup) | — | n/a (na-backend) | the setup wizard is a kernel/setup concern that stays Python-side; the Rust client surfaces a provider-missing boot via the boot-failure diagnosis (serve exits nonzero before `session.started`) |
| client-side `--mode`/`--model` validation | main.py `_validate_overrides` | — | n/a | not ported: the spawned `serve` subcommand runs the same `_validate_overrides` itself and exits nonzero; the failure surfaces through the Rust boot-failure path |
| `run` | main.py | — | n/a (na-cli) | headless JSONL run CLI, not the TUI (the sdk/ clients drive it) |
| `sessions` | main.py | — | n/a (na-cli) | session-table listing for the terminal, no TUI surface |
| `resume` / `continue` | main.py | — | n/a (na-cli) | interactive relaunchers around the session picker; the TUI half is covered by the Rust `--resume <id>` flag (serve resolves partial ids); the picker/`continue` auto-select stay CLI-side |
| `session` (list/rename/delete/cleanup/fork) | main.py | — | n/a (na-cli) | store administration; in-TUI session ops ride ui/session_ops_* over the (future) wire ops |
| `tool` (list/invoke) | main.py | — | n/a (na-cli) | command-line tool invocation against a mounted bundle, no TUI surface |
| `bundle` (list/current/use/clear/update) | main.py | — | n/a (na-cli) | bundle scope administration; the TUI only consumes the resolved bundle via `serve` |
| `allowed-dirs` / `denied-dirs` | main.py | — | n/a (na-cli) | directory-permission persistence administration (decision surface already inline-ported into kernel/safety) |
| `init` | main.py | — | n/a (na-cli) | provider setup wizard (kernel/setup), the first-run gate's explicit form |
| `provider` | main.py | — | n/a (na-cli) | provider credential/routing administration |
| `notify` | main.py | — | n/a (na-cli) | notification-channel admin; the TUI's notification ladder is ported in ui/notifications |
| `update` | main.py | — | n/a (na-cli) | package self-update (kernel/updater), a distribution concern |
| `source` | main.py | — | n/a (na-cli) | source-pin administration (kernel/source_admin) |
| `routing` | main.py | — | n/a (na-cli) | routing-matrix administration (kernel/routing_admin) |
| `reset` | main.py | — | n/a (na-cli) | destructive state reset (kernel/reset), a maintenance concern |
| `doctor` | main.py | — | n/a (na-cli) | environment diagnosis CLI; the in-TUI `/doctor` command IS ported (commands/doctor) |
| `version` | main.py | — | n/a (na-cli) | prints package versions; the TUI banner carries them via `serve` |

### Non-TUI surfaces (sdk/, tests/forge/)

| Unit | Status | Caveats |
|---|---|---|
| sdk/python + sdk/typescript, tests/test_sdk_python.py | n/a (na-cli) | thin clients of the headless `amplifier-newtui run --output-format jsonl` CLI, not the TUI — nothing for the Rust client to port |
| tests/forge/ | n/a (na-harness) | real-PTY tests driving the *Python* app through the forge daemon; the Rust equivalents are the headless flow tests in main.rs, the `#[ignore]` core_client::live_serve_end_to_end live e2e, and the PERFORMANCE.md forge benchmark |

## Layer 5 — Integration

| Unit | Status | Caveats |
|---|---|---|
| Rust UI ↔ `amplifier-newtui serve` live end-to-end (real model turn; approvals by ticket id) | verified | 2026-07-26: live turn through the assembled reducer pipeline — real answer "pong", session_cost $1.1459575 from real usage records; approval round-trip proven with `serve --mode build` (ticket approval-1 answered "Allow once" over stdin; ping.txt written). Pinned as #[ignore] core_client::live_serve_end_to_end (run with --ignored; ~$1.15/turn from fresh-session cache write) |

## Layer 6 — Parity pass

| Unit | Status | Caveats |
|---|---|---|
| One test per DESIGN-SPEC behavior | verified | 48 behaviors enumerated at checkbox granularity in rust-mvp/PARITY.md: 41 covered by existing named Rust tests (§12-mouse moved from n/a to covered once the main.rs mouse wiring + tests landed), 4 added by the parity pass (activity-tail cap 3, approval-open notice, steer-then-queue client half, approval-while-lane-focused auto-return — the last also fixed an unwired assembly gap mirroring mount_approval), 3 n/a (architecture property, backend-only ×2) |

**Layers 4/5/6 status: COMPLETE.** As of 2026-07-27 (tip 5664a53): 1108 lib + 29 bin tests green (+1 ignored live test), clippy zero warnings; Python suite 2295 green as of the 2026-07-26 completion run.

## Performance validation (2026-07-26)

Forge-PTY benchmark (rust-mvp/PERFORMANCE.md, raw logs in rust-mvp/perf/): Rust first
paint 1–5 ms internal (wall measurements at the ~570 ms harness floor) vs 1.0–1.6 s for
the Python Textual app; UI RSS 4 MB vs ~90 MB; mock-protocol handshake <50 ms. Live
boots are dominated by the shared Python backend (18–34 s, network-bound) for BOTH
clients — the client swap does not change backend cost. amplifier-cli (~15 s boot,
219 MB), codex (~0.9 s to update gate, 50 MB), claude (~2.6 s, 603 MB) recorded as
environmental reference points only. Demo turn timings are not engine-comparable
(different scripted turns).

## Performance RCA (2026-07-27, validated by 4 parallel investigators + standalone reproductions)

The 51s first turn decomposed as: ~34s backend boot overlap (prompt accepted during boot),
~12s hooks-memory-interject pre-execution search, ~5.2s actual LLM, ~0.3s close-out —
"post-turn hooks" and "spawned agent" hypotheses were falsified. Fixes landed:
(1) foundation pin dc010423 → 32d4052 (activator find_spec bug reinstalled 10 packages
every boot — issue #326): warm boot 44.7s → 11.4s, zero reinstalls;
(2) memory-store search regenerate-per-hit fix on amplifier-bundle-memory branch
perf/search-no-regenerate (42s → 1.85s per query at 69k events, byte-identical results;
local branch, not pushed); (3) 33 wedged/zombie memory daemons killed (one at 103% CPU,
1.4GB, 15h); (4) working-line "1 agent" fallback → "thinking" (deliberate divergence:
Python transcript_render.py:280 still shows the mockup string "1 agent" — nothing spawns;
both codebases' comments already claimed "thinking").

## Log / caveats

- 2026-07-27: REPO SPLIT — rust-mvp/ extracted to the standalone repo
  ~/dev/amplifier-app-newtui-rust (github michaeljabbour/amplifier-app-newtui-rust,
  private), history preserved via `git subtree split` (37 commits). Cross-repo couplings
  reworked there: the cost.py drift canary, the launcher's dev-checkout detection, and
  the live serve e2e now resolve the Python checkout via AMPLIFIER_PY_CHECKOUT or the
  sibling ../amplifier-app-newtui (loud skip / PATH fallthrough when absent). This repo's
  Python suite has no rust-mvp dependency; kernel/serve.py remains the backend the Rust
  client spawns. This tracker is frozen — the ledger continues in the new repo.
- 2026-07-27: scaffolding purge (no mock/demo code on production paths). REMOVED from
  rust-mvp: src/live.rs + the `--direct` arm (the illustrative UI-calls-provider spike;
  its cost pin already covered by kernel::cost's suite), the silent fall-back-to-demo on
  backend spawn failure / missing key (replaced by the honest boot-failure diagnosis via
  a new `UnspawnedBackend` runtime seat + synthesized SpawnError record; new test
  main::test_spawn_failure_boots_into_boot_failure_diagnosis), the serve_mock production
  fallback in `resolve_backend` (outside a checkout it now spawns `amplifier-newtui
  serve` from PATH), the legacy pre-assembly `DemoRuntime` in runtime.rs (dead code —
  ScriptedDemoRuntime superseded it), and the `cargo mock` alias. KEPT deliberately:
  `--demo` + ScriptedDemoRuntime/DemoScript/demo_wiring/DemoAdapter (Python `--demo`
  feature parity, reachable ONLY via the explicit flag — the bare `demo` arg alias was
  tightened away), `backend/serve_mock.py` as the cross-process test's fixture (spawned
  only by tests / explicit AMPLIFIER_SERVE_CMD), and #[cfg(test)] fakes. Production-code
  pattern sweep (mock/stub/placeholder/dummy/fake/todo!/unimplemented!/FIXME/…): all
  remaining hits are design-mockup ground-truth references, real product features
  (composer placeholder text, paste stubs, [REDACTED] placeholder), or doc comments
  about test seams — none are scaffolding. Gates: cargo test 1160 green (1 pre-existing
  ignored live test), clippy --all-targets clean, release build clean.
- 2026-07-27: completeness audit. A 7-slice audit of the whole Python tree against this
  tracker ran; findings (high = 4, medium ≈ 8) were all fixed or reclassified this pass:
  the launch surface (main.py) got its own section (flags/backend
  resolution/resume hint ported; admin subcommands classified na-cli), serve error
  records + backend-exit detection rows added, kernel/demo reclassified from backend-n/a
  to a ported engine (DemoScript/ScriptedDemoRuntime, 15/15 pins), sdk/ + tests/forge/
  classified (na-cli / na-harness), and stale test-pin columns corrected (lane-steering →
  model/queues, TestAnswerMarkdown → ui/live_tail, decision-id routing → ui/reducer).
  Remaining gaps are protocol-bound and listed in the ui/app row (NOT WIRED).
- 2026-07-26: regression fixes (boot progress + copy). USER-AUTHORIZED additive Python change in kernel/serve.py (the only Python change): RealRuntime now boots with `on_progress` wired to emit `{"schema_version": 1, "type": "boot.progress", "action": ..., "detail": ...}` on the protocol stream before session.started (fires in-loop — resolve_config/foundation call the callback synchronously inside start(), so a plain emit is safe); pinned by tests/test_serve_offline.py::test_serve_emits_boot_progress_records_before_session_started and mirrored in rust-mvp/backend/serve_mock.py. Rust: WireEvent::BootProgress → splash status with Python boot_progress's exact text; transcript drag-selection + copy-on-settle + ctrl+c-copy ported (tests named after the Python cases in tests/test_ui_composer.py). Caveats: selection copies run the OS clipboard tool synchronously on the event loop (Python off-loads to a worker; pbcopy is fast, 5s cap) and no OSC 52 is emitted; the composer has no internal selection model, so ctrl+c copies transcript selections only.
- 2026-07-26: wave 1 (8 independent model units, 7 worktree porters) integrated: 81 unit tests ported+green, clippy clean, full suite 86 passing. deps: +regex, +serde.
- 2026-07-26: tracker created; rust-mvp baseline: 5 tests passing, clippy not yet part of gate.
