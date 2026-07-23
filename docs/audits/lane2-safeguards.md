# Lane 2 — Safeguards Parity Audit

**Scope:** every security / trust / safety mechanism in Microsoft's `amplifier-app-cli`
(donor/reference) matched against `michaeljabbour/amplifier-app-newtui` (target, clean main
@ e6b50cd). Question answered per row: **is the same protection achieved** (possibly by a
different mechanism)? A missing safeguard is the highest-severity gap class.

Verdict legend: **PARITY** (same protection) · **PARTIAL** (protection weaker/narrower) ·
**MISSING** (absent in newtui) · **NEWTUI-BETTER** (newtui hardens beyond app-cli) ·
**N/A-BY-DESIGN** (mechanism not applicable to newtui's architecture).

Read-only on both repos. All rows cite `file:line` on **both** sides.

---

## Verdict counts

| Verdict | Count |
|---|---|
| PARITY | 20 |
| NEWTUI-BETTER | 6 |
| PARTIAL | 3 |
| MISSING | 2 |
| N/A-BY-DESIGN | 3 |
| **Total rows** | **34** |

---

## Safeguard-by-safeguard matrix

| # | Safeguard | app-cli cite | newtui cite / status | Verdict | Sev | Recommendation |
|---|---|---|---|---|---|---|
| 1 | Trust postures / interaction modes + gating | `ui/interaction_state.py:46-120` (TrustPreset/TrustState, 6 presets incl. `bypass`); `ui/governance.py:83-132` (resolve_trust) | `model/trust.py:156-218` (5 spec modes plan/brainstorm/chat/build/auto; resolve/resolve_capability) | PARITY | — | Same allow/ask/deny semantics; newtui omits full `bypass` (see #34). |
| 2 | Static per-capability policy table (missing key ⇒ ask, never widen) | `ui/governance.py:121-132` (BLOCK>ASK>AUTO; missing⇒ASK) | `model/trust.py:211-212` (`policy.get(cap,"ask")`, unknown mode⇒chat) | PARITY | — | — |
| 3 | Auto-mode reasoning-blind classifier gating, fail-closed | `ui/safety_classifier.py:335-461` (TwoStageActionClassifier, offline `ReasoningBlindStageEvaluator` default **+ provider-backed async seam**); fail-closed `423-436` | `kernel/governance_hook.py:75-184` (OfflineAutoClassifier, single-stage); fail-closed `311-312` | PARTIAL | Low-Med | Offline deterministic reasoning-blind path is at parity; newtui lacks the 2-stage + provider-backed deliberative evaluator. Wire a provider evaluator behind the existing `classifier` injection point. |
| 4 | Offline classifier posture for unrecognized ops (fail-safe default) | `ui/safety_classifier.py` + `ui/governance.py` (unknown⇒SHELL, most restrictive) | `model/trust.py:117-149` (unknown tool ⇒ EXEC, most restrictive; test-cmd sniff) | PARITY | — | — |
| 5 | NET / SPEND (/SUBAGENT) capability classes | `ui/safety_classifier.py:48-56` (NET, SPEND, SUBAGENT) | `model/trust.py:32-41` (NET, SPEND; subagent folded into SPEND via `task/spawn`) | PARITY | — | Subagent-as-spend is a defensible collapse. |
| 6 | EXEC/shell gating | `ui/governance_hooks.py:264-285` (`bash`⇒SHELL, aggregate-slot gate) | `model/trust.py:91-94,142-148` (`bash`⇒EXEC); `kernel/safety.py:74-91` | PARITY | — | Both ask/classify shell in non-auto modes; newtui adds path-scan (#9). |
| 7 | `allowed_write_paths` policy + cwd-in-writable | `runtime/config_policies.py:109-127` (`_ensure_cwd_in_write_paths` injects `.`) | `kernel/directory_permissions.py:280-368,482-486` (DirectoryPolicy, project_dir always in `_base_allowed:293`; merged_tool_config) | PARITY | — | newtui unifies the effective boundary across fs-tool + governance + CLI admin. |
| 8 | Protected-path rules (`.git`/`.agents`/`.codex`/`AGENTS.md`) | *none found* — no protected-path concept | `kernel/directory_permissions.py:104-109,355-356` (PROTECTED_PROJECT_PATHS; check_write hard-deny) | NEWTUI-BETTER | Med | app-cli should adopt a protected-path denylist for instruction/repo-control files. |
| 9 | Fail-closed embedded protected-path scan for interpreter writes | *none found* — governance only inspects the single `target` path (`ui/governance_hooks.py:110,288-307`) | `kernel/directory_permissions.py:399-480` (`shell_outside_target` + `_embedded_protected_reference`, precompiled fail-closed matchers) | NEWTUI-BETTER | Med-High | Confirms newtui audit-H1 hardening; app-cli cannot see `.git/config` buried in `python3 -c "…"`. |
| 10 | Merely-outside-project interpreter writes (non-protected) | roam in auto/shell (no per-token outside scan) | roam under default `open` posture; `#24` scoped fix to protected only (`kernel/directory_permissions.py:361-368,430-458`) | PARITY | Low | **Known deferral (classified, not rediscovered):** both apps let non-protected outside interpreter writes roam; the mounted fs-tool remains the write enforcer. |
| 11 | Write-boundary enforcer assertion (`open`→`guarded` degrade if no fs-tool) | *none* — no write_boundary concept (app-cli app-gates outside writes via OUTSIDE_PROJECT slot, `ui/governance.py:111-120`) | `kernel/directory_permissions.py:79-101` + `kernel/runtime.py:668-672` (assert enforcer present, degrade + boot notice) | NEWTUI-BETTER | — | newtui refuses to silently delegate to a non-existent enforcer. |
| 12 | Approval UX (Allow once/always/Deny, deny-default, ctrl-a detail) | `approval_provider.py:74-93`; `ui/inline_approval.py` (STANDARD_APPROVAL_OPTIONS, stage_approval_detail) | `kernel/approval.py:46-49,147-150,256-264` (STANDARD_OPTIONS verbatim, stage_detail, deny default) | PARITY | — | Both keep the verbatim "Allow"-family strings the gate string-matches. |
| 13 | Approval timeout → default-deny (+ premature-timeout guard) | `approval_provider.py:77,114-127` (300s default, then TimeoutError⇒raise/deny) | `kernel/approval.py:184-195` (timeout⇒default); floor `kernel/runtime.py:495` `min_timeout=3600.0` | NEWTUI-BETTER | — | newtui floors the timeout so long human reads don't auto-deny (a live app-cli footgun; broker docstring `approval.py:113-117`). |
| 14 | Needs-you / deferred-decision queue + retro-answer | `ui/interaction_state.py:302-462` (NeedsYouQueue) | `model/queues.py:290-413` (NeedsYouQueue, defer/answer/consume) | PARITY | — | — |
| 15 | Denial escalation (3 consecutive / 20 total ⇒ needs-you) | `ui/governance.py:152-208,419-434` (DenialLog) | `model/trust.py:242-312` + `kernel/governance_hook.py:361-371` | PARITY | — | Identical thresholds. |
| 16 | Deferred-decision **dependency blocking** (block dependent tool until answered) | `ui/interaction_state.py:299,371-386` (`blocking_decisions`) + `ui/governance_hooks.py:166-197` (`_blocked_dependencies`) | *absent* — `NeedsYouItem.action` exists (`model/queues.py:282-284`) but no `dependencies`/`blocking_decisions`; governance_hook has no dependency gate | MISSING | Low-Med | Add dependency-keyed blocking so a step declaring `depends_on` a parked decision is deny-and-continue until answered. Auto-classifier still denies unauthorized ops, limiting exposure. |
| 17 | Prompt-injection input probe (scan tool RESULT ⇒ inject security note; feed classifier deny) | `ui/safety_classifier.py:131-221` (InjectionInputProbe, 5 shapes) + `ui/governance_hooks.py:39,73-77,199-228` (tool:post/tool:error probe⇒`inject_context`) | **absent** — `GovernanceHook.EVENTS=("prompt:submit","tool:pre")` only (`kernel/governance_hook.py:194`); no tool-output inspection anywhere in kernel | MISSING | **High** | Highest-risk gap. Untrusted tool output (web_fetch, file reads) carries instruction-shaped text into model context with zero flagging. Port InjectionInputProbe onto `tool:post`/`tool:error` and inject a system data-only note. |
| 18 | Provider key store: atomic write, chmod 600, concurrency lock | `key_manager.py:87-135` (FileLock advisory lock + tmp-replace + chmod600) | `kernel/setup.py:189-218` (tmp-replace + chmod600, **no FileLock**) | PARTIAL | Low | Atomic replace prevents corruption, but concurrent `write_key` is last-writer-wins (a racing terminal can drop a key). Add an advisory lock around read-modify-write. |
| 19 | Provider key config as `${VAR}` placeholders (never literals) | `provider_config_utils.py` / `lib/settings.py` | `kernel/setup.py:320-337` (`provider_config_entry` writes `${VAR}`) | PARITY | — | Secrets never serialized into settings scope files. |
| 20 | Key-based metadata redaction on persist | `session_store.py:19,178` (`redact_secrets`, key-based only) | `kernel/persistence.py:102-117` (`redact_secrets` + `scrub_value`) | PARITY | — | newtui is a superset (see #21). |
| 21 | **Value-pattern** secret scrubbing in transcript / export / copy | *none* — app-cli only key-redacts **metadata**; transcript bodies are JSON-sanitized, never scrubbed (`session_store.py:178` only) | `model/redaction.py:39-116` applied to transcript `kernel/persistence.py:213`, metadata `:117`, export `commands/export.py:41`, copy `commands/copy.py:23` | NEWTUI-BETTER | Med | Confirms newtui issue #23 hardening. app-cli persists AWS keys/bearer tokens/PEM blocks in transcript.jsonl plaintext — recommend porting value-pattern scrub. |
| 22 | Child/subagent governance: app trust-hook re-registration | `runtime/session_spawn_inprocess.py:49-52,189-201` propagates **trust_state capability + approval provider** but does **not** re-register the app `GovernanceHook` on child hooks; hook registered root-only `runtime/interactive_resources.py:365-374` | `kernel/spawner.py:130-147,242-243` + `kernel/runtime.py:738` (same GovernanceHook re-registered on every child coordinator) | NEWTUI-BETTER | Med | Confirms newtui issue #38. In app-cli, careful/plan posture gating never reaches subagent lanes (only native approval inheritance does). |
| 23 | Subagent recursion depth cap | `runtime/session_spawn_inprocess.py:140-142` (self_delegation_depth via tool-delegate) | `kernel/spawner.py:110-111,183-195` (explicit `DEFAULT_MAX_DEPTH=2`, enforced pre-spawn) | PARITY | — | newtui enforces app-side before creating anything. |
| 24 | Skill overlay + tool/hook inheritance to children | `runtime/session_spawn_inprocess.py:104-117` (RUNTIME_SKILL_OVERLAY); `runtime/session_spawn_config.py` (filter_tools/hooks) | `kernel/spawner.py:379-403,479-507` (`_apply_inheritance_filter`, `_inherit_skill_overlays`) | PARITY | — | Exclusions apply to inheritance only; agent-declared modules always kept — same rule both sides. |
| 25 | Cancellation propagation to child tree (esc-interrupt) | `runtime/session_spawn_inprocess.py:119-121,302-307` | `kernel/spawner.py:262-267,285-289` (register/unregister child cancellation) | PARITY | — | — |
| 26 | Session store atomic write + backup + crash recovery | `session_store.py` (atomic write + `.backup`) | `kernel/persistence.py:73-84,248-297` (`_write_with_backup`, `.backup` recovery, `transcript_recovery_failed` surfaced) | PARITY | — | — |
| 27 | Incremental save (tool:post crash recovery between calls) | `incremental_save.py:64-187` (tool:post, priority 900, debounced) | `kernel/persistence.py:437-507` (IncrementalSaver, tool:post p900, debounced) | PARITY | — | — |
| 28 | Path-traversal guard on session id | `session_store.py` (validated id, `find_session` resolution) | `kernel/persistence.py:65-70` (`_validate_session_id` rejects `/ \ . ..`) | PARITY | — | — |
| 29 | Destructive-op safety: delete / cleanup (no prefix auto-resolve) | `session_store.py` (delete/cleanup_old_sessions) | `kernel/persistence.py:388-434` (delete validates id, no prefix resolve; cleanup `days>=0` guard, sub-sessions/dotfiles skipped) | PARITY | — | — |
| 30 | Config scope-file writes atomic | `lib/settings.py` writers | `kernel/bundle_admin.py:85-94` (`write_scope` tmp+replace); `kernel/setup.py:210`, `kernel/mcp_config.py:46`, `kernel/cost.py:525` | PARITY | — | — |
| 31 | env-var provider detection hygiene (entry-point gated) | `provider_env_detect.py:19-50` (installed-provider gated) | `kernel/setup.py:310-317,380-385` (PROVIDER_CREDENTIAL_VARS, detect_provider_from_env) | PARITY | — | Both only read env for detection; no leak into logs. |
| 32 | Trust posture persistence downgrade-safety (missing value ≠ broaden; `bypass` not restored unless versioned) | `ui/interaction_state.py:193-223` (`restore_persisted` + TRUST_POLICY_VERSION); persisted in spawn metadata `runtime/session_spawn_inprocess.py:284-294` | *not found* — mode is a live callable (`kernel/runtime.py:724`); no per-session posture persist/restore with a downgrade guard | PARTIAL | Low | Could not confirm newtui persists/restores a per-session trust posture. Resume defaults to a safe live mode (fine); risk only exists if a future restore path broadens silently — add a version-guarded restore if posture becomes persisted. |
| 33 | stdin arbiter (approval vs steering exclusive raw stdin) | `stdin_arbiter.py:9-21` + `approval_provider.py:64-72` (approval claims stdin) | Textual event-driven UI: approval bar + composer are widgets on one input loop; no shared raw stdin to arbitrate | N/A-BY-DESIGN | — | Mechanism obviated by the UI framework, not a missing protection. |
| 34 | stdout offload (protect UI from blocking library stdout) | `stdout_offload.py:1-205` (prompt_toolkit `patch_stdout` event-loop-freeze fix) | prompt_toolkit-specific bug; Textual owns the screen and its own render loop | N/A-BY-DESIGN | — | Not applicable to Textual; verify stray-stdout capture separately if desired. |
| 35 | `bypass` / fully-unrestricted posture | `ui/interaction_state.py:119` (`bypass` preset, all-auto) | no `bypass` posture in the 5 spec modes (`model/modes.py:22`, `model/trust.py`) | N/A-BY-DESIGN | — | newtui omitting a full-bypass posture is *safer*, not a gap. |

---

## Top gaps (ranked by risk)

1. **[MISSING · High] Prompt-injection input probe (row 17).** app-cli scans every tool
   result on `tool:post`/`tool:error` for injection-shaped text (authority-override,
   role-impersonation, secret-extraction, concealed-action, tool-directive) and injects a
   "treat as data only" system note; the classifier also hard-denies on injection shapes.
   newtui's governance hook never inspects tool output at all. **Risk:** untrusted content
   from `web_fetch`/file reads can smuggle instructions into model context with zero
   detection. This is the single highest-severity safeguard gap in the lane.

2. **[MISSING · Low-Med] Deferred-decision dependency blocking (row 16).** app-cli blocks a
   tool call that declares a dependency on an unanswered parked decision (deny-and-continue
   until the human answers). newtui tracks the denied `action` but not dependencies, so a
   dependent step can execute before its decision is resolved. Mitigated because the
   auto-classifier independently denies unauthorized ops.

3. **[PARTIAL · Low-Med] Single-stage vs two-stage/provider classifier (row 3).** Offline
   deterministic reasoning-blind gating is at parity and fail-closed on both sides, but
   newtui lacks app-cli's deliberative second stage and provider-backed evaluator seam →
   coarser verdicts at the margin. Injection point already exists (`classifier=` arg).

4. **[PARTIAL · Low] Provider key store concurrency (row 18).** newtui `write_key` is atomic
   (tmp-replace + chmod600) but has no advisory FileLock, so two concurrent CLI invocations
   are last-writer-wins and can drop a saved key. No corruption risk, only loss.

5. **[PARTIAL · Low / unresolved] Trust-posture persistence downgrade-safety (row 32).**
   Could not confirm newtui persists/restores a per-session trust posture at all; app-cli
   does, with a version guard that refuses to silently broaden (esp. `bypass`). Safe today
   (resume falls back to a safe live mode); becomes relevant only if posture persistence is
   added later.

### Where newtui is stronger than app-cli (verified against the donor)

- **Protected paths (row 8)** — app-cli has *no* protected-path concept; newtui hard-denies
  writes to `.git`/`.agents`/`.codex`/`AGENTS.md`.
- **Embedded interpreter-write scan (row 9)** — app-cli only inspects the single explicit
  path arg; newtui fail-closed-scans the raw command string (`python3 -c`, `sed -i`, …).
- **Write-boundary enforcer assertion (row 11)** — app-cli has no such assertion; newtui
  degrades `open`→`guarded` with a boot notice when no fs-tool backs the boundary.
- **Value-pattern secret scrubbing (row 21)** — app-cli key-redacts metadata only; newtui
  scrubs secret-shaped values from transcript bodies, exports, and clipboard copies.
- **Child governance re-registration (row 22)** — app-cli propagates trust_state + native
  approval to children but *not* its app-level GovernanceHook; newtui re-registers the same
  posture gate on every child lane.
- **Approval-timeout floor (row 13)** — newtui floors approval timeouts (3600s) so a human
  reading a plan doesn't get auto-denied; app-cli's 300s default auto-denies.

## Could-not-determine

- **Row 32** — whether newtui persists a per-session trust posture (no evidence found; likely
  intentional, mode being a live callable). Flagged PARTIAL pending confirmation.
- **Row 34** — newtui's stray-stdout capture path (Textual-owned) was not traced; the app-cli
  mechanism is prompt_toolkit-specific and N/A, but newtui's own screen-integrity handling
  was out of scope to verify.
- Internal enforcement inside the mounted `tool-filesystem` (audit H1 in newtui's own notes)
  is a module concern, not an app-seam concern; both apps delegate the hard write-block there
  under the `open`/outside-project path and neither app's code can assert it here.
