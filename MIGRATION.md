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
| model/native_modes | model/native_modes.py | test_model_native_modes.py | todo | needs modes, trust |

## Layer 2 — kernel pure logic

Only client-side-relevant pure logic is ported; process/IO orchestration stays in the
Python backend behind `serve`.

| Unit | Python source | Python tests pinned | Status | Caveats |
|---|---|---|---|---|
| kernel/events (normalization) | kernel/events.py | test_kernel_events_normalize.py, test_kernel_event_canary.py | todo | |
| kernel/cost | kernel/cost.py | test_kernel_cost.py, test_cost_parity_appcli.py | todo | includes wiring `provider_response_usage` → live token/cost tallies in the Rust client (known $0.0000 gap) |
| kernel/git_yield | kernel/git_yield.py | (covered via turn_yield tests) | todo | |
| kernel/turn_yield | kernel/turn_yield.py | test_kernel_turn_yield.py | todo | |
| kernel/approval (decision logic) | kernel/approval.py | test_kernel_approval.py, test_kernel_approval_governance.py | todo | decision logic only; broker IO stays Python |
| kernel/reminder_trust | kernel/reminder_trust.py | test_denial_injection_trust.py | todo | |
| kernel/safety | kernel/safety.py | test_kernel_safety.py | todo | |
| kernel/evidence | kernel/evidence.py | test_kernel_evidence.py | todo | |
| kernel/surface_hint | kernel/surface_hint.py | test_kernel_surface_hint.py | todo | |
| kernel/steering | kernel/steering.py | test_kernel_steering.py, test_kernel_lane_steering.py | todo | |
| kernel/trackers/runtime_status | kernel/trackers/runtime_status.py | test_kernel_trackers.py | todo | |
| kernel/trackers/stream_status | kernel/trackers/stream_status.py | test_kernel_trackers.py | todo | |
| kernel/trackers/task_status | kernel/trackers/task_status.py | test_kernel_trackers.py, test_kernel_trackers_spawner.py | todo | |
| kernel/display | kernel/display.py | (inline uses) | todo | |
| kernel/file_mentions | kernel/file_mentions.py | test_kernel_file_mentions.py | todo | |
| kernel/mention_expansion | kernel/mention_expansion.py | test_kernel_mention_expansion.py | todo | |
| kernel/prompt_history | kernel/prompt_history.py | test_kernel_prompt_history.py | todo | |
| kernel/serve, runtime, session_manager, session_factory, spawner, persistence, config, config_ops, mcp_config, setup, updater, bundle_*, notify_admin, routing_admin, source_admin, clipboard, demo, tool_cli, queue_bridge, recipes, reset, rewind, session_ops, compaction, jsonl, approval broker, governance_hook, directory_permissions | — | — | n/a | backend concerns: stay Python behind `serve`; the Rust client consumes their effects via the protocol |

## Layer 3 — commands/

| Unit | Python source | Python tests pinned | Status | Caveats |
|---|---|---|---|---|
| commands/registry | commands/registry.py | test_commands_registry.py | todo | |
| commands/builtin | commands/builtin.py | test_commands_builtin.py | todo | |
| commands/context | commands/context.py | test_commands_context.py | todo | |
| commands/copy | commands/copy.py | test_commands_copy.py | todo | |
| commands/doctor | commands/doctor.py | test_commands_doctor.py | todo | |
| commands/export | commands/export.py | test_commands_export.py | todo | |
| commands/improve | commands/improve.py | test_commands_improve.py | todo | |
| commands/permissions | commands/permissions.py | test_commands_permissions.py | todo | |
| commands/skills | commands/skills.py | test_commands_skills.py | todo | |

## Layer 4 — ui/ (ratatui rebuild)

| Unit | Python source | Python tests pinned | Status | Caveats |
|---|---|---|---|---|
| ui/reducer | ui/reducer.py | test_ui_reducer_*.py | todo | grow existing rust-mvp app.rs/event.rs |
| ui/lane_reducer | ui/lane_reducer.py | test_ui_lane_reducer.py | todo | |
| ui/segments | ui/segments.py | (inline uses) | todo | |
| ui/transcript_render | ui/transcript_render.py | test_ui_transcript_render.py, test_ui_render_*.py | todo | |
| ui/transcript (view) | ui/transcript.py | test_ui_transcript_view.py | todo | |
| ui/live_tail | ui/live_tail.py | test_ui_transcript_live_tail.py | todo | |
| ui/composer | ui/composer.py | test_ui_composer.py | todo | |
| ui/approval_bar | ui/approval_bar.py | test_ui_approval.py, test_ui_approval_wrap.py | todo | |
| ui/footer | ui/footer.py | test_ui_footer.py | todo | |
| ui/keymap | ui/keymap.py | test_ui_keymap.py | todo | |
| ui/lanes_panel | ui/lanes_panel.py | test_ui_lanes.py, test_ui_lanes_needs_you.py | todo | |
| ui/needs_you | ui/needs_you.py | test_needs_you_real.py | todo | |
| ui/palette | ui/palette.py | test_ui_palette.py | todo | |
| ui/plan_panel | ui/plan_panel.py | test_ui_plan_panel.py | todo | |
| ui/queued_strip | ui/queued_strip.py | test_ui_rewind_queued.py | todo | |
| ui/rewind_strip | ui/rewind_strip.py | test_ui_rewind.py | todo | |
| ui/notices | ui/notices.py | (inline uses) | todo | |
| ui/notifications | ui/notifications.py | test_ui_notifications.py | todo | |
| ui/themes | ui/themes.py | test_ui_themes.py | todo | |
| ui/splash | ui/splash.py | test_ui_splash.py | todo | |
| ui/motion | ui/motion.py | (inline uses) | todo | |
| ui/chrome | ui/chrome.py | test_ui_chrome.py | todo | |
| ui/file_mentions | ui/file_mentions.py | test_ui_file_mentions.py | todo | |
| ui/app_support | ui/app_support.py | test_ui_app_support.py | todo | |
| ui/app (composition root) | ui/app.py | test_ui_snapshots.py, flow tests | todo | maps onto rust-mvp main.rs/app.rs |
| ui/runtime_adapter | ui/runtime_adapter.py | test_runtime_adapter_*.py | todo | Rust side = CoreClientRuntime; Textual-thread specifics n/a |
| ui/term_probe, config_view, config_admin, directory_admin, session_ops_view, session_ops_controller, command_context, demo_wiring | various | various | todo | assess during layer 4; some may be n/a (Textual-specific) |

## Layer 5 — Integration

| Unit | Status | Caveats |
|---|---|---|
| Rust UI ↔ `amplifier-newtui serve` live end-to-end (real model turn; approvals by ticket id) | todo | flow previously demoed with rust-mvp CoreClientRuntime |

## Layer 6 — Parity pass

| Unit | Status | Caveats |
|---|---|---|
| One test per DESIGN-SPEC behavior | todo | enumerate from docs/DESIGN-SPEC at layer start |

## Log / caveats

- 2026-07-26: wave 1 (8 independent model units, 7 worktree porters) integrated: 81 unit tests ported+green, clippy clean, full suite 86 passing. deps: +regex, +serde.
- 2026-07-26: tracker created; rust-mvp baseline: 5 tests passing, clippy not yet part of gate.
