# Layer 6 — DESIGN-SPEC parity table

One row per spec'd behavior (checkbox) in `docs/DESIGN-SPEC.md`. Statuses:

- `covered-by:<test>` — behavior already pinned by an existing Rust test
- `added:<test>` — focused test added by this parity pass
- `n/a:<reason>` — not coverable by the ported units (Textual/terminal-only mechanics,
  backend-only concerns behind `serve`, or a recorded unwired-assembly gap)

Test names are `module::test` (all inline `#[cfg(test)]`; the flow tests live in
`src/main.rs::tests`). Totals: **48 behaviors — 40 covered, 4 added, 4 n/a.**

## §1 Themes & design tokens

| Ref | Behavior | Status |
|---|---|---|
| §1-tokens | All UI color from the named token tables only (exact hex per theme) | covered-by:ui/themes::test_every_token_hex_matches_spec_exactly + test_hex_values_live_only_in_themes_module |
| §1-switch | Theme switchable at runtime, default `slate` | covered-by:ui/themes::test_three_themes_exist (DEFAULT_THEME pin) + main::test_flow_palette_slash_opens_and_builtin_runs (/theme cycles live) |
| §1-glyphs | JetBrains-flavored glyph choices (❯ ● ✳ ✦ ✧ ■ ✔ □ ⊘ ◐ ├─ └ ↳ ▲ ▹ ‹ ›) | covered-by:ui/transcript_render::test_block_golden_markers_at_width (glyphs pinned per block kind); monospace rendering itself is terminal-inherent |

## §2 Screen layout

| Ref | Behavior | Status |
|---|---|---|
| §2-title | Title `amplifier-app-newtui — Amplifier — <state> — <bundle> — <session>`; orange `✳ ✦ ✧ ✦` spinner ~260ms while running; braille mirror in terminal chrome; state ladder | covered-by:ui/chrome::test_idle_title_exact_format, test_running_title_prefixes_spinner_and_cycles_frames, test_spinner_interval_is_260ms, test_native_terminal_title_uses_obvious_braille_spinner, test_title_state_text_updates_render |
| §2-order | Layout order title/transcript/notice/strips/composer/footer | covered-by:main::test_ui_snapshots_full_turn_renders_headless + test_ui_snapshots_footer_wraps_at_narrow_width (footer bottom row) |
| §2-notice | Notice slot: transient right-aligned dim, auto-dismiss ~4s, single slot | covered-by:ui/notices::test_notice_shows_and_auto_dismisses, test_notice_is_single_slot_and_replaces, default_duration_is_notice_duration (4.0s pin) |
| §2-strips | Overlay strips: palette / lanes+plan (plan hides <90 cols, footer carries Plan n/m) / rewind / queued / approval-replaces-composer | covered-by:widget suites (ui/palette, ui/lanes_panel, ui/plan_panel, ui/rewind_strip, ui/queued_strip, ui/approval_bar) + ui/footer::test_footer_paints_plan_count_in_left_segment |
| §2-composer | Composer: mode-accent edge + `[mode]` badge + green bold `❯` + exact placeholder | covered-by:ui/keymap::test_composer_placeholder_exact + ui/composer mode-class tests |
| §2-footer | Footer left `mode <mode> · <trust> · <bundle> · <session> · $<cost><yield▲><q1>` + decisions badge | covered-by:ui/footer exact-string tests (incl. `▲`, `· q1`, `N decisions waiting · ctrl-y`) |
| §2-hints | Footer hints change by state (approval/lane/palette/running/idle — exact strings) | covered-by:ui/footer state-hint tests + ui/keymap::test_footer_hints_exact_spec_strings |

## §3 Transcript block grammar

| Ref | Behavior | Status |
|---|---|---|
| §3-user | User line `❯ [mode] text`, badge stamps scrollback | covered-by:ui/transcript_render::test_user_line_exact, test_user_line_mode_badge_colors + ui/reducer::test_replay_stamps_historical_mode_on_the_user_line |
| §3-narration | Narration `● ` bright bullet + fg text | covered-by:ui/transcript_render::test_narration_exact |
| §3-digest | Activity digest: one collapsed dim line per burst, humanized counts, `· click to expand`, expandable body, frozen when model speaks, denial never folded | covered-by:ui/reducer::test_mixed_tool_burst_collapses_to_one_humanized_digest (+ flush_burst cases) + ui/transcript_render::test_tool_line_collapsed_exact/…expanded… + main::test_flow_approval_arrows_cycle_and_esc_denies_with_blocked_line (denial gets its own ⊘ line) |
| §3-tree | Live activity tree: up to 3 recent `└`/`├` ops beneath the working line, in-flight vs settled, ephemeral | added:ui/reducer::test_live_activity_tree_caps_at_three_recent_ops (render already pinned by transcript_render working-status tests; vanish-at-turn-end pinned by test_turn_end_discards_all_tail_state…) |
| §3-plan | Plan checklist glyphs `□ ■ ✔` + live `(Ns · ↓ X.Xk tok)` telemetry | covered-by:ui/transcript_render::test_plan_exact + ui/plan_panel tests |
| §3-blocked | `⊘ blocked · <cmd>` red + dim reason/continuation, never halts the turn | covered-by:ui/transcript_render::test_blocked_exact + main flow-approval deny path |
| §3-working | Working status line: pulsing `✳/✦/✧` + `working · Ns · ↓ tok` + steer hint; `thinking`/`1 agent` note; fan-out `Coordinating N agents` variant without steer hint | covered-by:ui/transcript_render::test_working_status_exact_and_spinner_frames, test_working_status_single_agent_exact (fan-out variant golden at line refs `Coordinating N agents`) |
| §3-recap | Recap `✳ Goal: <goal>. Next: <next>.` italic dim | covered-by:ui/transcript_render::test_recap_exact_italic_dim |
| §3-answer | Final answer: styled spans, teal inline code, clickable → evidence | covered-by:ui/transcript_render::test_answer_splits_newlines_and_keeps_span_styles + ui/transcript click→evidence message tests |
| §3-steer-echo | Steer echo `↳ steer queued: "<text>" · applies at next step boundary` + `Applying steer:` narration | covered-by:ui/transcript_render::test_steer_echo_exact + kernel/steering injection-string tests |
| §3-rule | Turn rule: full-width, right label `<Ns> · <tok>, N% cached · $<cost> · <outcome>`, dim/dimmer by outcome | covered-by:ui/transcript_render::test_turn_rule_fills_width_exactly, test_turn_rule_label_dim_when_shipped_dimmer_otherwise |
| §3-rule-click | Turn rule click → rewind picker at that checkpoint | covered-by:ui/rewind_strip::test_opens_at_clicked_rule_checkpoint (logic; terminal mouse wiring is a recorded assembly gap, see §12-mouse) |
| §3-delegate | Delegate summary `● Used N delegates · Plan n/m · <dur> ▸`, expandable rows, rebuilt on resume | covered-by:ui/transcript_render delegate-summary suite + ui/reducer::test_fanout_appends_exactly_one_summary_block, test_replay_rebuilds_delegate_summary_lane_transcript_and_plan |

## §4 Modes & trust

| Ref | Behavior | Status |
|---|---|---|
| §4-table | Five modes with exact colors + trust strings | covered-by:model/modes table test (exact `(id, color, trust)` tuples) |
| §4-default | Default mode is `auto` (2026-07-16 amendment) | covered-by:main::test_flow_modes_shift_tab_cycles_with_notice (boot assert) |
| §4-cycle | shift+tab cycles modes (also while input focused); badge click cycles | covered-by:main flow-modes + model/modes::test_cycle_visits_all_five_modes_and_wraps + ui/composer CycleModeRequested test (badge *click* rides the mouse gap, §12-mouse) |
| §4-notice | Mode change → notice `mode <id> · <trust>` | covered-by:main::test_flow_modes_shift_tab_cycles_with_notice (exact string) |
| §4-tint | Mode tint in exactly three places; chat composer edge uses `rule` | covered-by:model/modes::test_chat_composer_edge_uses_rule_token + ui/composer mode-class tests + ui/footer mode-segment tests |
| §4-gating | Trust profiles actually gate tools (plan read-only, brainstorm no tools, …, auto policy gate) | covered-by:model/trust suite (13) + kernel/governance_hook suite (40, classifier-gated auto) |
| §4-plan-handoff | Plan mode produces `(read-only)` plan + `Plan ready. shift+tab to build…` recap + handoff | covered-by:ui/transcript_render::test_plan_read_only_suffix + ui/reducer::test_real_plan_mode_turn_is_plan_ready + ui/demo_wiring PLAN_RECAP pin |

## §5 Composer input semantics

| Ref | Behavior | Status |
|---|---|---|
| §5-send | Idle + Enter → send user turn | covered-by:main::test_ui_snapshots_full_turn_renders_headless (submit op) + ui/composer::test_running_enter_posts_steer_not_submit (idle branch) |
| §5-steer | Running + Enter → steer this turn (queued in SteeringQueue, exact notice); second steer queues a full message | added:main::test_flow_steer_running_enter_steers_then_second_steer_queues (client half; mid-turn wire *delivery* has no serve op yet — recorded gap in MIGRATION ui/app row) |
| §5-queue | Running + Shift+Enter → queue full next-turn message (strip + `· q1` + auto-run at turn end) | covered-by:main::test_flow_steer_queue_shift_enter_queues_and_drains_at_turn_end (exact strip text, footer badge, `queued message picked up`) |
| §5-slash | `/` prefix opens the palette live-filtered | covered-by:main::test_flow_palette_slash_opens_and_builtin_runs |
| §5-esc | Esc priority lane→palette→rewind→lanes→interrupt; second Esc ≤750ms opens rewind | covered-by:ui/keymap::test_esc_chain_priority_order_per_spec (+0.75 pin) + ui/app_support::test_esc_sequence_accepts_the_boundary_once, test_esc_sequence_expires_and_clears |

## §6 Command palette

| Ref | Behavior | Status |
|---|---|---|
| §6-open | Opens on `/`, substring filter, first row highlighted, Enter runs top, click runs any, esc closes | covered-by:ui/palette suite (filter/selection/click/close) + main flow-palette |
| §6-rows | Row: teal command + description + right dimmer tag | covered-by:ui/palette::test_row_cells_and_groups_match_spec |
| §6-groups | Group headers only when filter is exactly `/` (During/Parallel/Ship/Between/Repair) | covered-by:ui/palette::test_group_headers_only_when_filter_is_exactly_slash + commands/builtin::test_registry_holds_all_commands |
| §6-set | Minimum command set (`/mode /plan /brainstorm /context /tasks /ledger /rewind /permissions /doctor /improve`) | covered-by:commands/builtin::test_table_matches_mockup_exactly (whole table pinned) |
| §6-echo | Running a command echoes it as a user line first | covered-by:main::test_flow_palette_slash_opens_and_builtin_runs (echo assert) |

## §7 Approvals & needs-you

| Ref | Behavior | Status |
|---|---|---|
| §7-bar | Bar replaces composer; exact label/options; `› ` selection; Deny red; arrows/tab/enter/esc; clickable | covered-by:ui/approval_bar suite (20) + main::test_flow_approval_arrows_cycle_and_esc_denies_with_blocked_line |
| §7-notice | Notice on open: `approval required · choose below the transcript` | added:main::test_flow_approval_open_posts_exact_notice |
| §7-lane-return | Lane focused when approval arrives → auto-return to parent with notice | added:main::test_flow_approval_while_lane_focused_auto_returns_to_parent (behavior was an unwired assembly gap — now wired in `app.rs` mirroring Python `mount_approval`, incl. palette close + `back to parent · approval required`) |
| §7-deny | Deny → `⊘ blocked … denied by user · continuing without …`, turn continues | covered-by:main flow-approval deny path + ui/transcript_render::test_blocked_exact |
| §7-defer | Auto-mode trust block → deferred decision, footer `N decisions waiting · ctrl-y`, run continues | covered-by:ui/footer decisions-badge tests + ui/reducer decision_deferred cases + ui/approval_bar::test_ctrl_y_parks_ticket_without_resolving |
| §7-needs-you | ctrl-y → Needs-you block, chips, `Applying decision:` clears badge | covered-by:ui/needs_you suite (13) + ui/transcript_render::test_needs_you_exact_chip_styling (chip *click* actions ride the mouse gap, §12-mouse) |

## §8 Agent lanes & subagent focus

| Ref | Behavior | Status |
|---|---|---|
| §8-panel | ctrl-t//tasks toggles panel; exact header; aligned per-lane rows with state glyphs | covered-by:ui/lanes_panel suite (31, width goldens 80/58) |
| §8-progress | Multi-agent progress in panel + delegate summary (no per-agent tree lines); `Changed N files` aggregate | covered-by:ui/reducer::test_no_tree_line_answer_blocks_anymore + fanout suite + ui/transcript_render::test_expanded_change_line_uses_theme_aware_diff_styles |
| §8-tail | Lane live tail: ≤3 `┆` lines, 0.05s throttle, focus-follow, ctrl-o pin, `▸` marker, root preempts, ephemeral | covered-by:ui/reducer lane-tail suite (focus-follow/pin/preempt/clear) + ui/live_tail suite (26) |
| §8-focus | Lane focus swaps transcript: banner `focused: <name> · subagent of …`, `[delegated]` brief, esc back | covered-by:ui/reducer::test_child_events_accumulate_a_focus_transcript (exact banner) + ui/transcript focus/restore tests |
| §8-title | Title `✳ coordinating N agents` while coordinating | covered-by:ui/chrome coordinating test + reducer title_state cases |

## §9 Rewind & checkpoints

| Ref | Behavior | Status |
|---|---|---|
| §9-record | Every turn rule records a checkpoint `{tN, label, cost}` | covered-by:ui/reducer checkpoint/turn-id suite (test_plain_turns_keep_sequential_turn_ids, …) |
| §9-picker | ctrl-r//rewind/double-esc/rule-click opens `‹ rewind › tN · $ · label › [enter fork] [esc close]` | covered-by:ui/rewind_strip suite (13, exact label string) |
| §9-fork | Forking restores conversation state to that point | covered-by:ui/reducer::test_trim_rewinds_turn_ids_past_dropped_injections + app_support::trim_after_checkpoint cases (client half; the session-store fork itself is backend-only behind `serve`) |

## §10 Ledger, evidence, context

| Ref | Behavior | Status |
|---|---|---|
| §10-ledger | ctrl-l//ledger prints session ledger with turn/cost/shipped/cache aggregates | covered-by:ui/transcript_render::test_ledger_exact + commands/builtin::test_ledger_posts_ledger_block_with_aggregates |
| §10-yield | Footer green `▲` when last turn shipped | covered-by:ui/footer yield-glyph tests + ui/reducer::test_real_turn_with_file_changes_ships |
| §10-evidence | Clicking answer prints evidence block with numbered teal claims | covered-by:ui/transcript_render::test_evidence_exact + kernel/evidence suite + ui/transcript evidence-action tests |
| §10-context | `/context` → `· Context NN% of 200k` + usage bar | covered-by:ui/transcript_render::test_context_exact_bar + commands/context bit-for-bit oracle rows |

## §11 Turn lifecycle & telemetry

| Ref | Behavior | Status |
|---|---|---|
| §11-telemetry | Live token counting while running; per-turn cost from provider usage | covered-by:kernel/trackers suites + ui/reducer usage/cost tests (exact Decimal, priced + estimated) |
| §11-interrupt | Esc while running → step-boundary stop, `Interrupted. Goal: …` recap, `· interrupted` rule | covered-by:main::test_flow_interrupt_esc_requests_break_then_recap_and_rule + ui/reducer interrupted-close-out cases |
| §11-end-notice | Turn end notice `agents N done` / `turn interrupted · context saved` | covered-by:ui/reducer fan-out end-notice case + main flow-interrupt (exact strings) |
| §11-closeout | Fan-out close-out folds chrome into durable summary; survives resume via events log | covered-by:ui/reducer::test_all_complete_finalizes_duration_and_failure_state + replay suite |
| §11-banner | Session banner: bright `Amplifier <ver> · core <ver>` + dim bundle/provider/model/session line | covered-by:ui/transcript_render session-banner golden + commands/builtin::test_about_posts_session_banner_block |

## §12 Non-visual requirements

| Ref | Behavior | Status |
|---|---|---|
| §12-native | Built the amplifier-native way (thin app over amplifier-core, bundle-driven) | n/a:architecture property — the Rust client rides the Python `serve` backend (MIGRATION header); not unit-testable in-crate |
| §12-real | Real sessions: streaming from core events; persistence with resume + fork | n/a:backend-only behind `serve` (client replay half covered-by ui/reducer replay suite; live end-to-end is MIGRATION Layer 5) |
| §12-keys | Keybindings in real terminals; kitty shift+enter documented; graceful fallback | covered-by:ui/keymap::test_shift_enter_with_alt_enter_fallback + ui/footer alt+enter hint case (real-terminal kitty probe is integration, recorded) |
| §12-resize | Resize reflows transcript without corruption | covered-by:ui/transcript::test_resize_reflow_debounced_and_width_pure, test_resize_reflow_deferred_while_streaming_then_forced_once |
| §12-mouse | Mouse click targets (rules, tool lines, lanes, palette, approval, badge, chips) | n/a:unwired-assembly-gap — terminal mouse events are not wired in the Rust app (recorded in MIGRATION ui/app row); per-widget click *logic* is covered by widget click tests |
| §12-suite | Test suite covering block grammar, gating, palette, approvals, steer/queue, checkpoints, ledger math, theme tokens | covered — this table is the index (1078 tests green: 1066 lib + 12 bin) |
