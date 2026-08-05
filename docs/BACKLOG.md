# Backlog

**The backlog lives in [GitHub issues](https://github.com/michaeljabbour/amplifier-app-tui/issues).**
This file indexes them (background, file:line evidence, and acceptance criteria live in each
issue) alongside the shipped ledger and non-goals.

**Campaign status (verified 2026-08-05):** the 2026-07-22 audit backlog (#21–#54) and its
two follow-ups (#90, #91) are closed. The backlog-attractor campaign landed the PRs listed
below; the later hygiene closure is #99, the Forge implementation is #94, live-tail
attachment is #97, and clean lane summaries are #95. The **Status** column cites the
merging PR; GitHub remains the source of truth.

Calibrated 2026-07-22 against `main` (`ac854ef`): the 2026-07 five-specialist audit
(architecture, security, quality, tests, reliability + deterministic lint) was
**re-verified claim-by-claim against the code** before filing — corrections are noted in
the issues (label `audit-2026-07`).

Rubric: every item must stay true to the architecture rules (ADR-0007) — pure renderer
transforms, golden-tested in the same commit, kernel never imports Textual, UI never
touches amplifier-core.

---

## Audit round (2026-07-22) — blocker first

| Issue | Status | Item |
|---|---|---|
| [#21](https://github.com/michaeljabbour/amplifier-app-tui/issues/21) 🔴 | ✅ #89 | Turn exception crashes the whole TUI (run_worker `exit_on_error` + no except in the submit chain) |
| [#22](https://github.com/michaeljabbour/amplifier-app-tui/issues/22) | ✅ #77 | Hardening pass: compaction task ref, queue locks, silent cleanup swallow, empty-resume + malformed-settings notices, dead `ApprovalBroker.defer` |
| [#23](https://github.com/michaeljabbour/amplifier-app-tui/issues/23) | ✅ #61 | Secret-scrub transcripts, `/export`, `/copy` (only metadata.json is redacted today) |
| [#24](https://github.com/michaeljabbour/amplifier-app-tui/issues/24) | ✅ #62 | H1: shell write gating is command-list based — `python3 -c`/`sed -i`/`curl -o` bypass |
| [#25](https://github.com/michaeljabbour/amplifier-app-tui/issues/25) | ✅ #63 | H2: `write_boundary: "open"` default — no app-level write gate outside the project |
| [#26](https://github.com/michaeljabbour/amplifier-app-tui/issues/26) | ✅ #55 (decision doc) | Governance: classifier allows unrecognized EXEC by default — decide posture |
| [#27](https://github.com/michaeljabbour/amplifier-app-tui/issues/27) | ✅ #64 | ui-events.jsonl hot path: per-token open/write/close; deltas filtered on read not write |
| [#28](https://github.com/michaeljabbour/amplifier-app-tui/issues/28) | ✅ #65 | Behavioral test gaps: real AppCommandContext, RealRuntime op wrappers, wall-clock flake |
| [#29](https://github.com/michaeljabbour/amplifier-app-tui/issues/29) | ✅ #99 | Hygiene: `ruff format`, BLE001 lint selection, Pyright strict verdict, and token-formatter dedup |
| [#30](https://github.com/michaeljabbour/amplifier-app-tui/issues/30) | ✅ #69 | Collapse the session-op passthrough ladder (14 ops × 5 sites → one typed dispatch) |
| [#31](https://github.com/michaeljabbour/amplifier-app-tui/issues/31) | ✅ #70 | Extract SessionOpsController from ui/app.py |
| [#32](https://github.com/michaeljabbour/amplifier-app-tui/issues/32) | ✅ #71 | Extract LaneReducer from TranscriptReducer |
| [#33](https://github.com/michaeljabbour/amplifier-app-tui/issues/33) | ✅ #72 | Lift the pure `_render_*` functions out of transcript.py (zero-risk split) |

## Rendering & model contract

| Issue | Status | Item |
|---|---|---|
| [#34](https://github.com/michaeljabbour/amplifier-app-tui/issues/34) | ✅ #78 | Polish: italic, reading measure, checkbox glyphs, OSC 8 links, fence-copy, elapsed format |
| [#35](https://github.com/michaeljabbour/amplifier-app-tui/issues/35) | ✅ #67 | Width-aware surface hint at `provider:request` |

## Runtime parity & perf

| Issue | Status | Item |
|---|---|---|
| [#36](https://github.com/michaeljabbour/amplifier-app-tui/issues/36) | ✅ #56 (decision doc) | Lane/subagent todo surfacing (root-only today) |
| [#37](https://github.com/michaeljabbour/amplifier-app-tui/issues/37) | ✅ #79 | Hybrid transcript history — ADR-0007 perf escalation (5k blocks miss frame budget) |
| [#38](https://github.com/michaeljabbour/amplifier-app-tui/issues/38) | ✅ #73 | Child sessions bypass TUI posture gating; runtime skill overlays not propagated |
| [#39](https://github.com/michaeljabbour/amplifier-app-tui/issues/39) | ✅ #74 | Per-lane steering (queue a message to a running delegate) |
| [#40](https://github.com/michaeljabbour/amplifier-app-tui/issues/40) | ✅ #75 | Post-rewind ghost turns on resume |
| [#41](https://github.com/michaeljabbour/amplifier-app-tui/issues/41) | ✅ #76 | Approval bar → needs-you parking |
| [#42](https://github.com/michaeljabbour/amplifier-app-tui/issues/42) | ✅ #68 | Lane label aliasing + historical mode badges |

## CLI / session parity (Bucket B — nice-to-have; core parity done)

| Issue | Status | Item |
|---|---|---|
| [#43](https://github.com/michaeljabbour/amplifier-app-tui/issues/43) | ✅ #80 | First-run onboarding gate + provider management |
| [#44](https://github.com/michaeljabbour/amplifier-app-tui/issues/44) | ✅ #83 | `/config` live editing |
| [#45](https://github.com/michaeljabbour/amplifier-app-tui/issues/45) | ✅ #82 | Session-manager ops (delete/rename/background, resume picker) |
| [#46](https://github.com/michaeljabbour/amplifier-app-tui/issues/46) | ✅ #81 | `source` command group + `routing list/use` CLI |
| [#47](https://github.com/michaeljabbour/amplifier-app-tui/issues/47) | ✅ #84 | Desktop/OSC 777 notifications beyond the shipped bell |
| [#48](https://github.com/michaeljabbour/amplifier-app-tui/issues/48) | ✅ #85 | `@mention` expansion in the runtime path — decide + implement |

## Amplifier-team feedback round (2026-07-22)

| Issue | Status | Item |
|---|---|---|
| [#51](https://github.com/michaeljabbour/amplifier-app-tui/issues/51) | ✅ #86 | Mount `context-intelligence-logging` behavior; custom telemetry destinations |
| [#52](https://github.com/michaeljabbour/amplifier-app-tui/issues/52) | ✅ #87 | Routing-matrix: mount `hooks-routing` (settings bridge + spawner glue already shipped) |
| [#53](https://github.com/michaeljabbour/amplifier-app-tui/issues/53) | ✅ #59 (decision doc) | Anchors pin lifecycle: automate pin bumps, surface staleness |
| [#54](https://github.com/michaeljabbour/amplifier-app-tui/issues/54) | ✅ #60 (decision doc) | Evaluate `microsoft/amplifier-agent` as the runtime integration layer (spike + decision doc) |

(Provider loading needed no new issue — it already works via `config.providers` +
`keys.env` and is documented in SETTINGS.md; the UX on top is [#43](https://github.com/michaeljabbour/amplifier-app-tui/issues/43).)

## Self-improving harness

| Issue | Status | Item |
|---|---|---|
| [#49](https://github.com/michaeljabbour/amplifier-app-tui/issues/49) | ✅ #94 | Forge-driven capability test tier — validate the real TUI through a real terminal (design #57, implementation #94) |
| [#50](https://github.com/michaeljabbour/amplifier-app-tui/issues/50) | ✅ #58 (decision doc) | Self-improvement loop over skills/harness (SkillOpt discipline, AIDE² safeguards; references documented in-issue) |

## Follow-ups filed during the campaign

| Issue | Status | Item |
|---|---|---|
| [#90](https://github.com/michaeljabbour/amplifier-app-tui/issues/90) | ✅ #97 | Live tail: attach the streaming block to the lane/item it's working on (not a detached bottom strip) |
| [#91](https://github.com/michaeljabbour/amplifier-app-tui/issues/91) | ✅ #95 | Lane "done" row shows a clean summary instead of raw markdown |
| [#210](https://github.com/michaeljabbour/amplifier-app-tui/issues/210) | ✅ #216 | Light theme (`paper`) shipped: selectable via `/theme paper` (or cycling bare `/theme`), every theme's token pairs WCAG-contrast-tested (`tests/test_ui_theme_contrast.py`) -- AC4 fully met, no longer scoped out. See docs/DESIGN-SPEC.md §1 |

## Supplemental engineering ledger (working tree, 2026-08-05)

This is a compact index to the complete
[supplemental acceptance matrix](audits/feedback-status-2026-08-05.md#supplemental-engineering-work-matrix).
It is separate from the original 23-story **19 PASS · 4 PARTIAL · 0 GAP**
score. The supplemental aggregate is **17 PASS (local evidence) · 4 PARTIAL**;
the partial rows are item 4, item 13, item 20, and item 21. Item 11 is counted
among the 17 local passes while B8 remains PARTIAL overall. Every `PASS` below means
**local working-tree evidence only**; it does not mean committed, pushed,
reviewed, merged, installed, deployed, or released.

| # | Work | Engineering status | Remaining or publication boundary |
|---|---|---|---|
| 1 | `/model` | **PASS (local only)** | Providers retain model/capability authority; publish the working-tree routing and rollback changes. |
| 2 | `/effort` | **PASS (local only)** | Validate or disclose provider-specific `none`/`minimal` semantics; publish the local propagation/reporting work. |
| 3 | Manual compaction | **PASS (local only; AC1–AC5)** | Publish the serialized, bounded manual-operation contract, including rich-input preservation and stale-worker fencing; automatic rebuild behavior is item 4. |
| 4 | Repeated automatic compaction | **PARTIAL (upstream); AC1/2/3/5 pass, AC4 open** | `context-simple` still needs cached/incremental request-view maintenance and hysteresis. |
| 5 | Steering/queue recall | **PASS (local only)** | Publish the identity-owned rich-capsule admission/recall behavior. |
| 6 | Exact custom decisions | **PASS (local only)** | Publish the exact-text/exact-decision capture path. |
| 7 | Auto deny/tool-failure continuation | **PASS (local only)** | Publish the deny-and-continue and same-turn failure-recovery behavior. |
| 8 | Settings namespace [#187](https://github.com/michaeljabbour/amplifier-app-tui/issues/187) | **PASS (local only); issue open** | Commit/review/merge the `tui:` whitelist and fallback. `bundle clear` removes/prunes the canonical key when no legacy value exists, but preserves and null-masks legacy `bundle.active` when it does. |
| 9 | Real streaming [#129](https://github.com/michaeljabbour/amplifier-app-tui/issues/129) | **PASS (local Anthropic evidence); issue open** | The currently pinned Anthropic path satisfies the local runtime proof; #129's acceptance list remains unchecked and the result is neither provider-generic nor published. |
| 10 | B7 notification durability | **PASS (local only)** | Publish locked record/ack mutation and terminal-clear priority; live ntfy/mobile smoke remains release evidence. |
| 11 | B8 listener hardening | **PASS (local listener); B8 overall PARTIAL** | Ship a phone-reachable authenticated TLS/tunnel and authorized Teams/Outlook tenant integrations. |
| 12 | Locked source installer | **PASS (local installer); D1 publication PARTIAL** | Review/merge the script, then verify the raw URL from a clean shell. Inventory reproducibility is limited to the same OS/architecture/Python/marker target. |
| 13 | Cold-boot activation [#130](https://github.com/michaeljabbour/amplifier-app-tui/issues/130) | **PARTIAL (upstream)** | Pinned Foundation `dea5bd8` equals inspected upstream `main` and still needs cross-process locking, timeout/retry, signal-aware diagnostics, and lossless state merging; a deterministic probe reproduced overlap and lost state. |
| 14 | Short-ID resume (related broad wrap-up [#148](https://github.com/michaeljabbour/amplifier-app-tui/issues/148)) | **PASS (local sub-contract); issue open** | Publish the shared resolver, deterministic exit codes, completion, and canonical 8-character hints. #148 is a broad open recap, not a short-ID-specific closure issue. |
| 15 | Resume-time orphan-tool repair | **PASS (local, bounded contract)** | Publish recognized stored-shape repair, uncertainty warning, and successful-save-before-mount behavior. Persistence is best-effort on `OSError`, and no all-provider wire-format claim is made. |
| 16 | Interactive routing-matrix choice | **PASS (local only)** | Publish the intuitive number/name picker and literal shortcut help. Until then, users of the old build can enter `s1` or run `amplifier-tui routing use NAME`. |
| 17 | Full-SHA source activation compatibility | **PASS (local installed snapshot); D1 publication PARTIAL** | Publish/reinstall the application dependency containing Foundation's commit-aware clone path. Both reported SHAs are valid and cold-resolve locally; `amplifier-tui update` cannot replace the old application runtime. |
| 18 | Root model → delegate matrix synchronization | **PASS (local only)** | Publish the model/matrix synchronization work. The exact selected model remains the root/orchestrating model; the matching provider-family matrix governs delegated roles. Live resolver switching remains compatibility-scoped to the audited pinned routing runtime. |
| 19 | Live skill activation | **PASS (local only)** | Publish the context-injection path. Successful inline instructions or completed fork results affect the next model turn; missing content/context never reports a false success. |
| 20 | Live bundle and additive module loading | **PARTIAL (safe additive/content path local; singleton identity boundary open)** | Bundle instructions/context and additive providers, tools, hooks, and agent definitions load into the current session; proven live configuration propagates transactionally to child sessions, providers remain idle until selected, and failed remaps restore identity/order. Orchestrator, context-module, existing-provider, and explicit agent-module identity replacement remains a new-session boundary. |
| 21 | Live MCP reconciliation | **PARTIAL (new/owned servers live; boot-owned replacement upstream-bound)** | New and TUI-owned servers connect/reload/remove live. Boot-owned server replacement remains a restart boundary until the upstream aggregate exposes ownership-aware reconciliation. |

## Non-goals

- **Syntax highlighting in answers.** Doable, but fights the restraint aesthetic
  and churns goldens forever; calm teal verbatim reads better in a transcript
  than rainbow soup.
- **Ingested-source deletion** (corpus "Delete original" UI) — not a tui
  feature; no amplifier tool exposes a corpus-document delete.
- **Admin surface** — `source`-authoring, `tool invoke`, `reset`,
  `--install-completion`, `session cleanup`, replay: one-time/admin operations
  that belong in a small separate `amplifier-admin` CLI, not the TUI. (The
  `source` *override* group in #46 is the exception, kept for parity.) The TUI's
  `/module load` is deliberately narrower: additive provider/tool/hook modules
  mount in the current session and propagate to subsequently spawned child
  sessions; existing-provider and orchestrator/context identity changes remain
  a next-session operation.

---

## Shipped ledger (compact — details in git history and ARCHITECTURE.md)

**Audit round (2026-07-22 → 07-23)** — the full #21–#54 backlog cleared in 34 merged
PRs (#55–#89, #92): security (fail-closed EXEC scan #62, secret-scrub #61, write enforcer
#63, posture inheritance #73), reliability (crash-proof turns #89, hardening pass #77,
no post-rewind ghosts #75), the ui/app.py and transcript.py refactors (#69–#72), CLI/session
parity (onboarding #80, `/config` #83, session-manager #82, `source`/routing #81/#87,
`@mention` #85, notifications #84, telemetry bridge #86), perf (5k transcript budget #79,
no per-token delta persistence #64), and six 2026-07-22 design/decision docs (#55, #56, #57,
#58, #59, #60). The hygiene and Forge residues subsequently closed in #99 and #94;
follow-ups #90 and #91 closed in #97 and #95.

**Amplifier-native / CLI parity** — in-session commands over the live
coordinator; skills (`/skills`, `/skill`); MCP (`tool-mcp` + `/mcp` over
`~/.amplifier/mcp.json`); approvals/modes mounted anchors-identical (off by
default) + posture bridge; routing plumbing; `bundle` CLI over the shared
`BundleRegistry`; `init` (authoritative env-var, `--model`, `--from-env`);
top-level `update` over foundation `check_bundle_status`/`update_bundle`;
team-pulse read tools; needs-you queue (PR #19).

**Codex-inspired core** — `<turn_aborted>` marker + step-boundary steering;
truthful native compaction binding; progressive line-commit streaming with
fence/table holdback; two-axis safety resolution + protected paths; composer
`@file` autocomplete; `/diff` theme-token highlighting; versioned JSONL CLI +
Python/TS subprocess SDKs.

**Pricing** — Decimal estimator parity with app-cli (10/10 parity tests green),
provider-reported cost authoritative, live Helicone pricing wired at startup
(`start_live_pricing`, `kernel/runtime.py`) with 24 h on-disk cache, honest
`~$` marker for unpriced usage, `--resume` cost re-seed.

**Plan/TODO** — real `todo` tool adapter → PlanBlock (demo `plan` shape
coexists); PlanPanel bottom strip, side-by-side with lanes on wide terminals
and stacked below them on narrow terminals so expansion stays interactive.

**Streaming & inline** — committed lines use the final renderer during
streaming; `**bold**`, `` `code` ``, `[text](url)` in `_inline()`; blockquotes
as a `▌` left gutter (style token `blue` — revisit only if the mockup says dim).

**Ambient progress** — delegate summary blocks, real focused-lane transcripts
from diverted child events, lane live tail with ctrl+o cycling (PRs #13, #17).

**Runtime honesty** — thin-wrapper bundle over pinned anchors; hook suppression
(not stripping); `hooks-logging` owns `events.jsonl`, app owns `ui-events.jsonl`;
resume replays under the stored bundle (explicit `--bundle` overrides); event
canary for un-consumed engine event kinds (PRs #19, #20). v0.1.0 tagged.
