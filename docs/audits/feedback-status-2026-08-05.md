# Consolidated feedback status — 2026-08-05

This is the current repository source of truth for the 23 feedback stories from
Brian Krabach, Samuel Lee, and David Koleczek. It supersedes the dated status
counts embedded in earlier review documents; those older sections remain useful
only as chronological evidence.

## State boundary

- **Merged baseline:** local `main` and `origin/main` both point to
  `118b796f5b8ed2c33b4390d4ef793304b3eb8a30` at this audit.
- **Verified working tree:** the fixes and tests described below are local,
  uncommitted, and unpushed. This audit does not call them merged or released.
- **Scoring rule:** a story is `PASS` only when all five written acceptance
  criteria pass. One partial criterion makes the whole story `PARTIAL`.
- **Current result:** **19 PASS · 4 PARTIAL · 0 GAP**.

Literal `[ ]` entries in `docs/DESIGN-SPEC.md` are normative requirement
bullets, not implementation-status markers. The AC map below is the dated status
ledger.

## Complete acceptance map

| ID | Current verdict | AC1–AC5 | Primary inspection evidence |
|---|---|---|---|
| B1 | PASS | AC1–AC5 pass | `ui/reducer.py`, `ui/transcript_render.py`, `ui/themes.py`, `tests/test_flow_return_to_answer.py`, `tests/test_ui_theme_contrast.py` |
| B2 | PASS | AC1–AC5 pass | `tests/test_skill_alias_fixture.py`, `tests/test_skill_alias_external_cli_resolver.py`, `tests/test_skill_alias_parity.py`, `tests/test_flow_skill_aliases.py` |
| B3 | PARTIAL | AC1, AC2, AC3, AC5 pass; AC4 partial | `pipelines/parity_loop.py`, `pipelines/ledger.py`, `pipelines/gene-transfer.dot`, `pipelines/parity-gates.tsv`, `pipelines/parity-passes.tsv`, `tests/test_parity_loop.py` |
| B4 | PASS | AC1–AC5 pass | `ui/footer.py`, `kernel/session_ops.py`, `tests/test_ui_footer.py`, `tests/test_kernel_session_ops.py` |
| B5 | PARTIAL | AC2, AC4 pass; AC1, AC3, AC5 partial | `scripts/adoption_gate.py`, `scripts/adoption_smoke.sh`, `docs/adoption/`, `tests/test_adoption_gate.py` (named-owner/seat evidence and blocker-date gates now enforced) |
| B6 | PASS | AC1–AC5 pass | `kernel/session_control.py`, `kernel/session_authz.py`, `kernel/session_attach.py`, `tests/test_session_control_multiprocess.py`, `tests/test_serve_control.py` |
| B7 | PASS | AC1–AC5 pass | `kernel/attention_store.py`, `kernel/attention_push.py`, `ui/notifications.py`, `kernel/runtime.py::publish_attention*`, `bundle.md`, `tests/test_attention_push.py`, `tests/test_kernel_attention_store.py`, `tests/test_ui_notifications.py`, `tests/test_runtime_offline_lane_attention.py`, `docs/audits/b7-b9-boundary-2026-08-05.md` |
| B8 | PARTIAL | AC1, AC2, AC5 pass; AC3, AC4 partial | `kernel/ambient/reply.py`, `kernel/ambient/reply_listener.py`, `ui/runtime_adapter.py`, `ui/app.py::_notify_attention`, `tests/test_ambient_reply.py`, `tests/test_ambient_reply_listener.py`, `docs/plans/2026-08-03-voice-first-ambient-delegation.md`, and the external blockers below |
| B9 | PASS | AC1–AC5 pass | `data/anchors-source-lock.json`, `kernel/source_lock.py`, `kernel/config.py`, `kernel/setup.py::PROVIDER_SOURCES`, `scripts/verify_anchors_source_lock.py`, `scripts/bump_anchors_ref.py`, `tests/test_source_lock.py`, `tests/test_no_floating_dependencies.py`, `tests/test_cli_tui_serve_lifecycle_fixture.py`, `tests/test_cli_tui_serve_parity.py`, `tests/test_sdk_python.py` |
| S1 | PASS | AC1–AC5 pass | `ui/rewind_strip.py`, `ui/app_support.py`, `docs/USER-GUIDE.md`, `tests/test_flow_rewind.py`, `tests/test_ui_rewind.py` |
| S2 | PASS | AC1–AC5 pass | `ui/sessions_strip.py`, `ui/app.py`, `main.py`, `tests/test_flow_sessions.py`, `tests/test_ui_sessions_strip.py` |
| S3 | PASS | AC1–AC5 pass | `commands/builtin.py`, `kernel/session_manager.py`, `tests/test_session_cli.py`, `tests/test_kernel_session_manager.py` |
| S4 | PASS | AC1–AC5 pass | `kernel/preflight.py`, `kernel/preflight_verify.py`, `main.py`, `tests/test_kernel_preflight.py`, `tests/test_interactive_launch.py` |
| S5 | PASS | AC1–AC5 pass | `model/blocks.py`, `kernel/events.py`, `ui/transcript_render.py`, `tests/test_ui_reducer_replay.py` |
| S6 | PASS | AC1–AC5 pass | `ui/lanes_panel.py`, `tests/test_flow_lanes.py`, `tests/test_ui_lanes.py` |
| S7 | PASS | AC1–AC5 pass | `ui/plan_panel.py`, `ui/app.py::action_toggle_plan_overflow`, `tests/test_flow_plan_panel.py`, `tests/test_ui_plan_panel_expand.py` |
| D1 | PARTIAL | AC2–AC5 pass; AC1 partial | `scripts/install.sh`, `docs/INSTALL.md`, `README.md`, `tests/test_source_installer.py`, and the isolated clean-install evidence below |
| D2 | PASS | AC1–AC5 pass | `ui/footer.py`, `ui/composer.py`, `tests/test_ui_composer_status_seam.py` |
| D3 | PASS | AC1–AC5 pass | `ui/session_ops_controller.py`, `ui/app.py`, `ui/reducer.py`, `tests/test_flow_clear_transcript.py`, `tests/test_ui_reducer_clear_fencing.py`, `tests/test_ui_session_ops_controller.py` |
| D4 | PASS | AC1–AC5 pass | `ui/chrome.py`, `tests/test_ui_chrome.py`, `tests/test_ui_chrome_snapshots.py`, `tests/test_ui_footer.py` |
| D5 | PASS | AC1–AC5 pass | `model/lanes.py`, child event routing in `ui/reducer.py`, `tests/test_ui_reducer_delegates.py`, `tests/test_ui_reducer_lane_transcripts.py` |
| D6 | PASS | AC1–AC5 pass | `ui/live_tail.py`, root identity in `ui/reducer.py`, `tests/test_ui_transcript_live_tail.py`, `tests/test_flow_main_chat_tail.py` |
| D7 | PASS | AC1–AC5 pass | `tests/test_kernel_evidence.py`, `tests/test_model_evidence.py`, `tests/test_ui_evidence_panel.py`, `tests/test_ui_evidence_click_flow.py`, `tests/test_ui_evidence_detail_flow.py` |

## Supplemental engineering-work matrix

This matrix is deliberately separate from the 23-story acceptance map above.
It does not add stories to, or change, the **19 PASS · 4 PARTIAL · 0 GAP**
score. Its own aggregate is **17 PASS (local evidence) · 4 PARTIAL**;
the partial rows are item 4 (automatic-compaction rebuilding/hysteresis), item
13 (Foundation cold-boot activation), item 20 (singleton bundle/module identity),
and item 21 (boot-owned MCP reconciliation). Item 11 counts as a local-listener
PASS in this supplemental matrix even though the broader B8 story remains
PARTIAL. `PASS (local only)` means the contract is implemented and has the cited
working-tree evidence; it does **not** mean committed, pushed, reviewed, merged,
installed, deployed, or released. `PARTIAL (upstream)` means the TUI-side
mitigation or presentation is present but the owning upstream implementation is
not complete. Open or closed GitHub issue state is coordination metadata, not
release proof.

| # | Engineering work | Supplemental acceptance criteria | Status | Code and test evidence | Development and publication notes |
|---|---|---|---|---|---|
| 1 | `/model` | AC1 bare `/model` reports the serving provider, current model, and advertised choices.<br>AC2 `/model [provider] <model>` targets an explicit mounted provider while the bare form can resolve an advertised model.<br>AC3 a cross-provider choice is promoted to serving priority and remains the sticky target for the session.<br>AC4 an immutable or stale routing-priority mutation fails closed and rolls model/config/session state back atomically.<br>AC5 success refreshes status/footer state and every failure is actionable. | **PASS (local only)** | `src/amplifier_app_tui/commands/builtin.py::_cmd_model`; `src/amplifier_app_tui/kernel/session_ops.py::list_models` and `set_model`; `src/amplifier_app_tui/ui/session_ops_controller.py::_show_model`; `tests/test_kernel_session_ops.py` model cases; `tests/test_ui_session_ops_controller.py`; `tests/forge/test_capability_demo.py`. | Providers remain authoritative for which model IDs and capabilities they actually support. The routing/presentation contract is local and uncommitted. |
| 2 | `/effort` | AC1 bare `/effort` reads the effective effort from the serving provider.<br>AC2 `/effort <none…max>` validates and writes the canonical session/provider setting.<br>AC3 the footer and `/status` reflect the same effective value.<br>AC4 keyboard cycling, interactive command handling, and serve `effort.get`/`effort.set` use the same contract.<br>AC5 invalid values and unavailable providers return actionable errors without corrupting state. | **PASS (local only)** | `src/amplifier_app_tui/commands/builtin.py::_cmd_effort`; `src/amplifier_app_tui/kernel/session_ops.py::get_effort` and `set_effort`; `src/amplifier_app_tui/ui/session_ops_controller.py`; `tests/test_kernel_session_ops.py` effort cases; `tests/test_flow_effort.py`; `tests/test_serve_effort.py`. | Propagation and reporting pass locally. Provider/model-specific meaning for `none`, `minimal`, or restricted models remains provider-owned and must be validated or disclosed separately. |
| 3 | Manual compaction | AC1 `/compact` accepts an optional focus and invokes the native context compactor.<br>AC2 compaction is mutually serialized with prompt admission, steering, queued turns, clears, restores, and deferred-decision answers.<br>AC3 active-turn interruption/waiting is bounded and the pending fence is released on success, failure, or timeout.<br>AC4 the UI reports a truthful visible result only after the backend outcome is known.<br>AC5 rejected or concurrent rich input remains recoverable and a stale worker cannot commit across the fence. | **PASS (local only)** — AC1–AC5 pass | `src/amplifier_app_tui/commands/builtin.py::_cmd_compact`; `src/amplifier_app_tui/kernel/session_ops.py::compact_context`; `src/amplifier_app_tui/ui/session_ops_controller.py::_compact_context`; the context-operation fence in `src/amplifier_app_tui/ui/app.py`; `tests/test_kernel_session_ops.py`; `tests/test_ui_session_ops_controller.py`; `tests/test_flow_clear_transcript.py`; `tests/test_ui_reducer_outcomes.py`. | This is the user-invoked operation, including rich-input recovery and stale-worker fencing. It does not claim to cure the separate upstream automatic-compaction rebuild loop in item 4. |
| 4 | Repeated automatic-compaction boundary | AC1 the TUI adopts the provider-derived request budget instead of inventing a conflicting threshold.<br>AC2 repeated root compactions coalesce into one updating transcript row.<br>AC3 child-session compactions stay out of the parent transcript.<br>AC4 the owning context module caches or incrementally maintains the compacted request view and applies hysteresis so it does not rebuild repeatedly at the same boundary.<br>AC5 compaction remains inspectable through normalized events/accounting. | **PARTIAL (upstream)** — AC1, AC2, AC3, and AC5 pass; AC4 is open | `src/amplifier_app_tui/ui/reducer.py` compaction outcome handling; runtime context-budget binding in `src/amplifier_app_tui/kernel/config.py`; `tests/test_ui_reducer_outcomes.py`; `tests/test_serve_offline.py`; `tests/test_context_meter.py`. | The remaining fix belongs in upstream `context-simple`; the TUI now presents the boundary correctly but does not stop that module from repeatedly rebuilding its ephemeral request view. No merge/release claim. |
| 5 | Steering and queued-message recall | AC1 running root and child turns accept steering only at the intended step boundary.<br>AC2 the queue chord creates a visible ordered queued turn instead of silently submitting it.<br>AC3 queue state exposes the pending item and its identity.<br>AC4 recall removes exactly the selected pending item atomically and never overwrites non-empty composer text.<br>AC5 paste/image sidecars, requeue behavior, rejection recovery, and admission ownership preserve the exact rich capsule without duplication. | **PASS (local only)** | `src/amplifier_app_tui/kernel/steering.py`; steering/queue/recall paths in `src/amplifier_app_tui/ui/app.py` and `ui/app_support.py`; `tests/test_flow_steer_queue.py`; `tests/test_ui_submit_errors.py`; `tests/forge/test_capability_demo.py::test_next_turn_queue_can_be_recalled_and_steered`. | The result is a local interaction contract; it is not evidence that this working tree is in the published package. |
| 6 | Exact custom decisions | AC1 arbitrary typed text is delivered verbatim to the exact pending decision ID.<br>AC2 decision capture takes precedence over slash-command, steering, and queue dispatch.<br>AC3 the context-operation fence rejects safely without consuming the answer.<br>AC4 Escape cancels capture without answering a different decision.<br>AC5 the applied or cleared state is visibly and durably attributable to that decision. | **PASS (local only)** | Decision capture in `src/amplifier_app_tui/ui/app_support.py` and `src/amplifier_app_tui/ui/app.py`; `tests/test_flow_decision_capture.py`, including the real-adapter path; `tests/forge/test_capability_demo.py::test_custom_decision_accepts_exact_free_text`. | Exact user text is not normalized or reinterpreted by the TUI. Local tests and Forge evidence do not imply publication. |
| 7 | Auto deny and tool-failure continuation | AC1 risky Auto-mode actions are classified before execution and an offline deny can never be loosened by a provider stage.<br>AC2 a denial returns a deny result rather than halting the turn.<br>AC3 the unresolved action is parked as a recoverable decision with its reason/context.<br>AC4 ordinary tool failures render as failures and the same model turn can continue to a fallback answer.<br>AC5 Plan/manual/strict boundaries remain fail-closed and do not inherit a broader Auto allowance. | **PASS (local only)** | `src/amplifier_app_tui/kernel/governance_hook.py`; `src/amplifier_app_tui/kernel/approval.py`; failure/decision rendering in `src/amplifier_app_tui/ui/reducer.py`; `tests/test_kernel_approval_governance.py`; `tests/test_decision_flow_ux.py`; `tests/test_ui_reducer_tool_failures.py`; `tests/forge/test_capability_demo.py::test_auto_tool_denial_continues_and_leaves_decision_waiting`. | “Continue” means the denied/failed tool becomes model-visible evidence; it never means the denied action was executed. This is uncommitted local evidence. |
| 8 | Settings namespace [#187](https://github.com/michaeljabbour/amplifier-app-tui/issues/187) | AC1 TUI-only preferences project from a strict `tui:` whitelist.<br>AC2 projection occurs per scope before global → project → local merge, so same-scope namespaced values win without defeating a more-specific legacy scope.<br>AC3 shared platform keys cannot be shadowed under `tui:`.<br>AC4 legacy app keys remain read fallbacks and malformed namespaces degrade safely.<br>AC5 `bundle use` writes only `tui.bundle.active`; without a legacy fallback, `bundle clear` removes that canonical key and prunes empty containers; with legacy `bundle.active`, it preserves the legacy value and writes a canonical `null` tombstone so the value does not reappear. | **PASS (local only); issue remains open** | `src/amplifier_app_tui/kernel/config.py::project_tui_preferences`; `src/amplifier_app_tui/kernel/bundle_admin.py` active-bundle writers; `tests/test_kernel_session_config.py`; `tests/test_kernel_bundle_admin.py`; `tests/test_bundle_cli.py`; `docs/SETTINGS.md`. Focused gate: **74 passed**; Ruff and Pyright passed. | Implemented in the dirty checkout at `118b796`; repeated clear of an already-cleared canonical state is a no-op. No commit, push, PR, merge, installed-copy update, or release is claimed. |
| 9 | Real streaming [#129](https://github.com/michaeljabbour/amplifier-app-tui/issues/129) | AC1 a real Anthropic turn emits progressive thinking start/delta/end events while submission is still in flight.<br>AC2 real answer text also streams before final consolidation.<br>AC3 stream end precedes durable `content_block_end`, which precedes `prompt_complete`.<br>AC4 concatenated streamed text equals the durable answer; `QueueBridge` consumes the event family while per-token events remain intentionally absent from `ui-events.jsonl`.<br>AC5 a packaged real-provider Forge lane proves the contract. | **PASS (local Anthropic evidence); #129 remains open** | Existing production path: `src/amplifier_app_tui/kernel/queue_bridge.py`, `kernel/events.py`, and `kernel/runtime.py`; new proof: `tests/forge/test_capability_real.py::test_real_anthropic_streams_thinking_and_text_before_durable_close`. Real lane: **1 passed in 51.52s**; related unit slices: **178 passed**. | The issue’s historical premise is stale for the currently pinned Anthropic path: the current SDK emits `llm:stream_block_*`. This local proof does not claim provider-generic coverage, update #129's unchecked acceptance list, or establish merge/package/release state; the issue remains open. |
| 10 | B7 durable notification state | AC1 concurrent record writers preserve both events while deterministic IDs still deduplicate the same transition.<br>AC2 acknowledgements use a locked reload/mutate/save and are monotonic.<br>AC3 acknowledgement marks an event terminal, removes any queued publish, and suppresses late `attention:recorded` replay.<br>AC4 terminal clears use a separate coalesced lane and are not displaced by a saturated advisory FIFO.<br>AC5 hook/HTTP failures remain content-free and cannot erase durable local truth or block the runtime. | **PASS (local only)** | `src/amplifier_app_tui/kernel/file_lock.py`; `kernel/attention_store.py`; `kernel/attention_push.py`; `ui/notifications.py`; `tests/test_kernel_attention_store.py`; `tests/test_attention_push.py`; `tests/test_ui_notifications.py`. Latest focused slices: **74 passed**, plus **50 passed** compatibility/static slice. | The deterministic local ntfy transport contract passes; no live ntfy/mobile tray smoke was performed. See the [B7/B9 boundary audit](b7-b9-boundary-2026-08-05.md). No merge/release claim. |
| 11 | B8 listener hardening | AC1 one ephemeral loopback listener starts only after session identity is known.<br>AC2 discovery is private (`0700` directory/`0600` records) and supports multiple live owners.<br>AC3 discovery requires both a live PID and a bounded socket challenge, pruning a live/reused PID whose listener is dead.<br>AC4 teardown removes only the current owner and closes undiscoverable partial starts.<br>AC5 a signed, nonce-protected reply reaches the exact originating decision before acknowledgement. | **PASS (local listener); B8 overall PARTIAL** | `src/amplifier_app_tui/kernel/ambient/reply_listener.py`; `kernel/ambient/reply.py`; runtime adapter lifecycle wiring; `tests/test_ambient_reply_listener.py`; `tests/test_ambient_reply.py`. | Same-host lifecycle is hardened locally. B8 AC3 still lacks a phone-reachable authenticated TLS/tunnel and real mobile enrollment; B8 AC4 still lacks authorized Teams/Outlook tenant integrations and attributable end-to-end proof. |
| 12 | Locked source installer | AC1 a requested branch/tag/ref is resolved to one full application commit SHA.<br>AC2 the installer fetches and verifies that exact detached checkout and refuses a source tree without committed `uv.lock`.<br>AC3 `uv export --frozen` produces the runtime constraint set from that checkout.<br>AC4 the isolated tool install is constrained to that set and failures remain actionable.<br>AC5 a clean fresh shell verifies version/help and reinstalling the same SHA yields the same package inventory for the same resolved target (OS, architecture, Python version/implementation, and environment-marker evaluation). | **PASS (local installer); D1 publication PARTIAL** | `scripts/install.sh`; `tests/test_source_installer.py`; recorded isolated empty-home install/reinstall/version/help/doctor/uninstall evidence. | The raw `main/scripts/install.sh` channel does not expose these local changes until review and merge, so D1 AC1 remains partial. Environment-marker-selected wheels can legitimately differ across targets; this is a same-target inventory claim, not byte-for-byte or cross-platform reproducibility. |
| 13 | Cold-boot dependency activation [#130](https://github.com/michaeljabbour/amplifier-app-tui/issues/130) | AC1 upstream dependency activation is serialized by a cross-process lock.<br>AC2 every external install has a hard timeout.<br>AC3 transient failures receive a bounded retry policy.<br>AC4 failure diagnostics preserve return code, bounded stdout/stderr, and signal-vs-exit distinction.<br>AC5 concurrent successful activators merge install state without losing entries. | **PARTIAL (upstream)** | `pyproject.toml`, `uv.lock`, and the installed distribution's `direct_url.json` all identify Foundation `dea5bd8fe11a7617dbcfc61c47f9f4f2fdc0b134`, which matched upstream `main` at inspection time. Installed `amplifier_foundation/modules/activator.py` still calls `subprocess.run` without timeout/retry or a cross-process lock; `install_state.py` has no locked merge. A deterministic two-activator probe reached `max_simultaneous_installs=2`, retained only one of two successful state entries, and a simulated `returncode=-9` received one attempt. | `bundle warm` and deferred activation reduce exposure but do not satisfy any missing upstream AC. Updating the pin alone cannot help while it already equals inspected upstream `main`; Foundation must add the lock, timeout/retry, signal-aware diagnostics, and lossless state transaction. Issue #130 remains open. |
| 14 | Short-ID resume (related broad wrap-up [#148](https://github.com/michaeljabbour/amplifier-app-tui/issues/148)) | AC1 exact IDs and unique prefixes resolve to one full resumable session.<br>AC2 ambiguous prefixes list all matching candidates with actionable 8-character commands and exit 3.<br>AC3 not-found and corrupt targets fail distinctly with exit 2 and 4 guidance.<br>AC4 `resume`, `session resume`, `run --resume`, and `serve --resume` share resolver/completion semantics.<br>AC5 exit, cross-project, import/fork, and completion hints use the same copy-pasteable short-ID form. | **PASS (local sub-contract); #148 remains open** | `src/amplifier_app_tui/main.py::_resolve_resume_target` and `_print_resume_hint`; `kernel/persistence.py::find_session`; `kernel/session_manager.py::resolve_for_resume`; `tests/test_session_cli.py`; `tests/test_kernel_session_manager.py`; `tests/test_kernel_persistence.py`. | #148 is an open, broad session-wrap-up recap rather than a short-ID-specific acceptance issue. This row proves only the listed local short-ID sub-contract; it does not close #148 or claim commit/push/merge/release. |
| 15 | Resume-time orphan-tool repair | AC1 resume detects unmatched assistant calls in the stored top-level `tool_calls` and content-block `tool_call`/`tool_use` shapes, and recognizes existing top-level tool results and `tool_result` blocks.<br>AC2 every recognized real result is preserved and only a missing call ID receives a placeholder.<br>AC3 a generic uncertainty placeholder is inserted immediately after the originating assistant message and warns that the tool may have executed; no cross-provider native-wire-format claim is made.<br>AC4 repair is idempotent for those recognized shapes.<br>AC5 on a successful `SessionStore.save`, the repaired transcript is written before context mount and the first resumed model request; an `OSError` is logged and resume continues, so persistence is best-effort rather than fail-closed.<br>AC6 the runtime emits a warning notification to inspect actual state before retrying. | **PASS (local, bounded contract)** | `src/amplifier_app_tui/kernel/session_integrity.py`; resume integration in `src/amplifier_app_tui/kernel/runtime.py`; `tests/test_session_integrity.py`; `tests/test_runtime_offline.py::test_offline_resume_persists_interrupted_tool_result_repairs`. Focused gate: **4 passed in 0.26s**. | Coverage proves stored-shape detection, the generic placeholder, idempotence, successful local persistence ordering, and runtime notification under the offline fake-provider fixture. It does not prove every provider's native message schema or durable writeback after a storage error. No publication state is claimed. |
| 16 | Interactive routing-matrix choice | AC1 the visible picker accepts the displayed row number as a complete selection.<br>AC2 it accepts an exact matrix name case-insensitively, preserving exact spelling and rejecting an ambiguous case-only collision.<br>AC3 the older `sN`/`vN` shortcuts remain supported and the help line renders literally instead of being swallowed as Rich markup.<br>AC4 invalid input does not write settings and reports the accepted range and names.<br>AC5 the same picker behavior is available from `routing manage` and first-run `init`, while control-key and numeric-name collisions retain explicit, unambiguous forms. | **PASS (local only)** | `src/amplifier_app_tui/main.py::_manage_matrix_target`, `_manage_select`, `_manage_view`, and `_routing_console`; `tests/test_routing_cli.py`; `tests/test_init_cli.py`. Focused routing/source gate: **75 passed in 6.38s**. Forge PTY session `5588381d` selected `anthropic` with bare `1`, selected `runpod` by name, and showed each persisted active marker. | The reported `1`/`anthropic`/`runpod` rejection is fixed in the working tree. On the still-published build, `s1` or `amplifier-tui routing use anthropic` is the immediate workaround. No commit, merge, install, or release is claimed. |
| 17 | Full-SHA source activation compatibility | AC1 the exact provider-anthropic and team-pulse commit hashes in the packaged bundle are reachable Git commits.<br>AC2 the installed application dependency contains Foundation's commit-aware clone path instead of treating a SHA as a branch.<br>AC3 the source installer carries that fixed Foundation revision through the frozen application lock.<br>AC4 an isolated fresh tool install cold-resolves both packaged sources to their exact requested HEAD without a `Remote branch … not found` error.<br>AC5 the audit distinguishes application reinstall from `amplifier-tui update`, which only refreshes mounted content. | **PASS (local installed snapshot); D1 publication PARTIAL** | `pyproject.toml` and `uv.lock` pin Foundation `dea5bd8fe11a7617dbcfc61c47f9f4f2fdc0b134`; bundled sources pin provider-anthropic `94a435482a879a1c506b2ea9076a951875e89c9d` and team-pulse `e89574d2b90814a0c10a2164aa7d5c9cc43bd3ce`; `tests/test_no_floating_dependencies.py::test_foundation_resolves_a_non_tip_full_sha_from_a_cold_cache`; `tests/test_source_installer.py`. Isolated Forge install emitted `COLD_PIN_OK` for both exact SHAs and `CLEAN_INSTALL_OK`. | Ken's field failure identifies the published app's older Foundation path; it does not show that either SHA is stale. The local repair is not user-installable until reviewed and merged, and `amplifier-tui update` cannot replace the application dependency. |

| 18 | Root model → delegate matrix synchronization | AC1 setup persists the matching provider-family matrix beside the selected provider model.<br>AC2 explicit launch provider/model selection enables the matching in-memory routing overlay without changing the saved matrix.<br>AC3 `/model [provider] <model>` preserves the exact root model and selects a unique advertising provider; ambiguous/unadvertised bare models fail with explicit-provider guidance.<br>AC4 the pinned live resolver, agent preferences, routing context, capability, and session ledger switch before the next delegated turn.<br>AC5 unavailable/custom matrices surface root/delegate divergence honestly and never claim an ephemeral restart will apply it. | **PASS (local only)** | `src/amplifier_app_tui/kernel/model_routing.py`; `kernel/session_ops.py::set_model`; `kernel/config.py`; setup/provider paths in `main.py`; `tests/test_kernel_model_routing.py`; model-routing cases in `tests/test_kernel_session_ops.py` and `tests/test_kernel_session_config.py`. | The selected provider's exact `default_model` remains the orchestrator/root model. Live matrix mutation is compatibility-scoped to the audited pinned routing resolver until upstream publishes a public matrix-switch API. No commit/merge/release claim. |
| 19 | Live skill activation | AC1 `/skill NAME [ARGS]` preserves the argument tail and omits an empty argument field.<br>AC2 inline instructions enter live context exactly once as a hook-origin system message that survives the dynamic root prompt factory.<br>AC3 fork skills expose a nonblank completed result.<br>AC4 the session ledger records name, arguments, and inline/fork kind.<br>AC5 absent tools/content/context or insertion failure never falsely reports next-turn activation. | **PASS (local only)** | `src/amplifier_app_tui/kernel/skill_activation.py`; `kernel/session_ops.py::load_skill`; `tests/test_kernel_skill_activation.py`; skill activation cases in `tests/test_kernel_session_ops.py`. | This fixes the prior display-only path: a successful palette load is now observable by the very next model request. Local and unmerged. |
| 20 | Live bundle and additive module loading | AC1 `/bundle` resolves deferred, registered, registry, local, and direct URI/path targets on a worker.<br>AC2 providers, tools, hooks, agent definitions, bundle instruction, and bundle context enter the current session and register cleanup; proven entries propagate transactionally to the parent configuration used by future child sessions.<br>AC3 a shared lock and canonical identity ledger makes boot-active, aliased, repeated, and concurrent loads idempotent; provider remap/failure preserves the exact existing identity and order.<br>AC4 `/module load <provider-or-tool-or-hook> [source]` mounts safe additive modules and refuses suppressed/unknown/singleton kinds; a new provider remains behind the serving route, including in inherited child configuration, until explicitly selected.<br>AC5 existing-provider, orchestrator, context-module, and explicit agent-module identity replacements require a new session because the upstream lifecycle exposes no safe hot-swap contract. | **PARTIAL (safe additive/content path local; singleton identity boundary open)** | `src/amplifier_app_tui/kernel/bundle_compose.py`; `kernel/bundle_content.py`; `kernel/runtime.py::load_deferred_bundle` and `load_module`; adapter/controller/command wiring; `tests/test_kernel_bundle_compose.py`; `tests/test_kernel_bundle_content.py`; `tests/test_runtime_live_loading.py`. | AC1–AC4 pass. Bundle instructions/context affect the next turn; live entries propagate to future child sessions; provider mounts are transactional and never take over implicitly. AC5 remains an explicit lifecycle boundary rather than an unsafe identity replacement. |
| 21 | Live MCP reconciliation | AC1 effective config follows user < project < environment < inline precedence.<br>AC2 a proven-new server connects/discovers before mounting, and collision/partial failure rolls back tools and connection.<br>AC3 TUI-owned servers reload, remove, and clean up live without touching another manager.<br>AC4 upstream `mcp.reconcile` is preferred; the targeted fallback is restricted to the audited pinned `tool-mcp` and does not duplicate its visibility hook.<br>AC5 boot-owned servers reload/remove live only through an ownership-aware upstream API; otherwise configured-vs-connected state and restart boundary are explicit. | **PARTIAL (new/owned servers live; boot-owned replacement upstream-bound)** | `src/amplifier_app_tui/kernel/live_mcp.py`; `kernel/mcp_config.py`; runtime lifecycle and adapter/controller wiring; `tests/test_kernel_live_mcp.py`; `tests/test_kernel_mcp_config.py`; `tests/test_runtime_live_mcp_ops.py`; controller MCP tests. | AC1–AC4 pass locally. AC5 preserves the aggregate boot manager instead of duplicating or orphaning its connections. No full arbitrary hot-reload claim. |

## Complete outstanding list

The four partial stories have explicit, visible boundaries rather than silent
gaps. B3/B5 need genuine human evidence, B8 needs external mobile/tenant
deployment, and D1 needs publication of the reviewed one-line channel.

### B3 — parity ownership and clean streak

- Obtain authorized owner dispositions for the 19 recorded findings.
- Implement the accepted outcomes.
- Record three consecutive audits with no new relevant gap. The current
  `clean_streak` is not three, so AC4 remains partial even though the fail-closed
  gate and versioned artifacts work.

### B5 — real adoption evidence

- Execute the five stages with the required one-day usage windows.
- Fill the three named additional daily-driver seats with genuine task/friction
  evidence and tracked dispositions.
- Rehearse rollback and record the replacement decision only with zero release
  blockers. The repository tooling intentionally rejects placeholder evidence.

### B8 — live inbound and tenant integrations

- The signed loopback HTTP path now proves exact reply text reaches the
  originating `NeedsYouQueue` decision before acknowledgement, with durable
  nonce replay protection and content-free delivery outcomes. New TUI
  clarification records are bound to that exact decision ID.
- Each live runtime now owns one ephemeral loopback listener after session
  identity is known. Private per-session discovery records use `0700`/`0600`
  permissions, multiple owners coexist, and shutdown removes only the current
  owner before runtime teardown. Discovery checks both process liveness and a
  bounded loopback socket challenge, so a reused/live PID cannot preserve a
  dead-listener record.
- Deploy a phone-reachable authenticated TLS/tunnel surface and prove the same
  quick-reply path, including remote device enrollment, from a real mobile
  client.
- Authorize real Teams and Outlook connectors against an actual tenant with
  explicit consent scopes and attributable actions.
- **AC3 remains partial:** signed loopback submission passes, but listener
  lifecycle and same-host discovery now pass; phone-reachable TLS/tunnel
  deployment, remote enrollment, and a real mobile quick-reply test remain.
- **AC4 remains partial:** local authorization/audit primitives pass; Teams and
  Outlook still require approved connector implementations, tenant consent
  scopes, and attributable end-to-end tests.

### D1 — publish and prove the one-line channel

The intended source-channel interface is:

```sh
bash -o pipefail -c "curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/michaeljabbour/amplifier-app-tui/main/scripts/install.sh | bash -s -- --launch"
```

- Review and merge `scripts/install.sh`; until then the raw URL does not expose
  this local installer.
- An isolated empty-home macOS run installed the reviewed working-tree snapshot,
  verified `amplifier-tui --help` and `version` from a fresh shell, exercised
  doctor, reinstalled, and uninstalled. A separate empty-home real launch
  cold-mounted the full pinned Foundation/Anchors graph and reached the expected
  provider authentication boundary with an intentionally invalid key.
- After merge, verify the documented raw URL itself from a clean shell. Linux
  and WSL runs remain broader release-matrix hardening rather than an AC2 blocker.
- A signed native/PyPI/package-manager release and background self-updater remain
  future release infrastructure. The docs label the current path
  **latest-source** and do not claim those capabilities.
- The installer checks out that exact application SHA, requires its committed
  `uv.lock`, exports the frozen runtime resolution, and constrains the isolated
  tool install to those versions. Reinstalling the same SHA produced an
  identical package inventory in the isolated Forge environment. Python and
  platform-specific wheels can still differ where the lock intentionally
  carries environment markers.

## Additional known upstream/release follow-ups

These do not change the 23-story count but remain explicit engineering work:

1. `context-simple` should cache or incrementally maintain its compacted request
   view with hysteresis. The TUI now adopts the provider-derived request budget,
   coalesces repeated root compactions into one updating row, and keeps child
   compactions out of the parent transcript; it does not stop `context-simple`
   from repeatedly rebuilding its ephemeral request view.
2. Provider/model reasoning-effort capabilities should be validated or disclosed
   for `none`/`minimal` and restricted models. Propagation and reporting are now
   correct; providers retain semantic authority.
3. `/model <provider> <model>` now promotes the selected provider to serving
   priority, keeps a sticky provider override, and rolls back atomically if
   routing priority is read-only. Bare `/effort`, `/status`, and the footer now
   report the serving provider's effective configured effort.
4. Pin or explicitly govern the floating operational sources in
   `pipelines/backlog.bundle.md` and `pipelines/gene-transfer.bundle.md`; they are
   intentionally outside the packaged-app anti-float guard today.
5. Publication requires an intentional scope review, commit/branch, safe rebase,
   complete post-rebase gate, push, review, and merge. None of those states is
   implied by local verification.
6. Live bundle/module AC5 remains open by design: existing-provider,
   orchestrator, context-module, and explicit agent-module identity replacement needs an
   upstream lifecycle contract before it can be made safe in an already-running
   coordinator. Instructions, bundle context, additive providers/tools/hooks,
   and agent definitions take effect in the current session and are inherited
   by child sessions spawned afterward.
7. Live MCP AC5 remains upstream-bound: newly added and TUI-owned servers can be
   connected, reloaded, removed, and cleaned up immediately, but a boot-owned
   server cannot be replaced without an ownership-aware reconcile API. The TUI
   reports persisted configuration and live connection state separately instead
   of claiming an unsafe hot swap.

## Validation record

The final shared dirty tree was recomputed as one combined gate after the
supplemental changes. The exact CI contract passed: frozen dependency sync,
Ruff lint and formatting, production Pyright, **4,385 passed / 12 opt-in Forge
tests deselected** with **89.23%** source coverage, and the separate **8-passed**
performance/snapshot tier. The final strict Forge tier passed all **9**
deterministic PTY scenarios; its **3** paid/network real-provider scenarios were
intentionally skipped because `AMPLIFIER_FORGE_REAL=1` was not enabled.

Recorded closure gates:

- Full ordinary suite on the prior integrated snapshot: **4,228 passed · 11
  intentionally deselected · 356.01s**.
- Recorded B7/B8/B9/D1 closure gate: **351 passed · 12.00s**, covering the
  attention destination, saturated-clear priority, legacy-producer suppression,
  ambient listener lifecycle, recursive source lock, cold-SHA guards, source
  installer, and CLI/TUI/serve/SDK parity fixtures. The TypeScript SDK contract
  separately passed **3/3**.
- Recorded strict Forge PTY acceptance with the paid real-provider lane explicitly
  enabled: **11/11 passed · 110.90s**. This covers exact custom decision capture,
  queued-message recall/interjection, Auto deny-and-continue, narrow Plan
  interaction, real boot, exact-session persistence, transcript rebuild, and
  cost reseeding.
- Recorded static and repository integrity: **Ruff check passed · Ruff format passed ·
  Pyright `src` passed · bundle copies byte-identical · `git diff --check`
  passed · GitHub workflow YAML parsed · installer passed both `sh -n` and
  `bash -n`**. The cold source-lock verifier also passed with the expected
  outer hash, **18 recursive repositories, and zero floating sources**.
- Installer: the deterministic contract is included in the focused gate. An
  isolated clean macOS run installed the reviewed snapshot, verified a fresh
  shell, version/doctor, reinstall, and uninstall. A separate empty-home real
  launch cold-mounted the pinned graph and reached only the expected fake-key
  authentication failure. The documented raw bootstrap URL remains unavailable
  until `scripts/install.sh` is merged.

The final reliability closure specifically proves:

- `/clear` installs its pending fence before worker scheduling, interrupts and
  waits for an active turn, retains new manual/queued/decision input, clears
  backend and view only after success, resets checkpoint lineage, and prevents
  a late close-out from recreating stale checkpoints while still accounting
  completed cost. Interrupt, clear, compact, and branch/fork snapshot waits are
  bounded so a broken operation cannot fence the UI indefinitely.
- Clear, manual compact, checkpoint restore, and branch/fork snapshot operations
  are mutually serialized with prompt admission, steering, queued turns, and
  deferred-decision answers. Rejected work remains editable instead of being
  consumed against a context that is about to change.
- Manual and queued submissions share an identity-owned admission token.
  Pre-admission failures restore the exact rich capsule; post-admission failures
  are contained without duplicating the accepted turn; an older worker cannot
  clear a successor's admission fence.
- Rich checkpoint capsules retain only paste and image sidecars still referenced
  by visible composer text. Their aggregate cache is capped at 64 MiB with
  oldest-first eviction; evicted capsules fall back to ledger/context restore.
