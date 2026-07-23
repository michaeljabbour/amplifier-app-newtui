# Backlog

**The backlog lives in [GitHub issues](https://github.com/michaeljabbour/amplifier-app-newtui/issues).**
This file indexes them (background, file:line evidence, and acceptance criteria live in each
issue) alongside the shipped ledger and non-goals.

**Campaign status (2026-07-23):** the 2026-07-22 audit backlog (#21–#54) has been driven to
done — 34 PRs merged: the backlog attractor's 33/33-green run (PRs in the #55–#89 range, per
`54d8a1d`) plus the fresh `ruff format` cut #92. Every issue below is **closed** except
**#29** (hygiene — format shipped in #92; residual lint/pyright/dedup items remain) and
**#49** (forge capability tier — design merged in #57, implementation underway on
`auto/forge-capability-tier-impl`). Two follow-ups filed during the campaign are also open
(**#90**, **#91**). The **Status** column cites the merging PR; treat GitHub as the source of
truth.

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
| [#21](https://github.com/michaeljabbour/amplifier-app-newtui/issues/21) 🔴 | ✅ #89 | Turn exception crashes the whole TUI (run_worker `exit_on_error` + no except in the submit chain) |
| [#22](https://github.com/michaeljabbour/amplifier-app-newtui/issues/22) | ✅ #77 | Hardening pass: compaction task ref, queue locks, silent cleanup swallow, empty-resume + malformed-settings notices, dead `ApprovalBroker.defer` |
| [#23](https://github.com/michaeljabbour/amplifier-app-newtui/issues/23) | ✅ #61 | Secret-scrub transcripts, `/export`, `/copy` (only metadata.json is redacted today) |
| [#24](https://github.com/michaeljabbour/amplifier-app-newtui/issues/24) | ✅ #62 | H1: shell write gating is command-list based — `python3 -c`/`sed -i`/`curl -o` bypass |
| [#25](https://github.com/michaeljabbour/amplifier-app-newtui/issues/25) | ✅ #63 | H2: `write_boundary: "open"` default — no app-level write gate outside the project |
| [#26](https://github.com/michaeljabbour/amplifier-app-newtui/issues/26) | ✅ #55 (decision doc) | Governance: classifier allows unrecognized EXEC by default — decide posture |
| [#27](https://github.com/michaeljabbour/amplifier-app-newtui/issues/27) | ✅ #64 | ui-events.jsonl hot path: per-token open/write/close; deltas filtered on read not write |
| [#28](https://github.com/michaeljabbour/amplifier-app-newtui/issues/28) | ✅ #65 | Behavioral test gaps: real AppCommandContext, RealRuntime op wrappers, wall-clock flake |
| [#29](https://github.com/michaeljabbour/amplifier-app-newtui/issues/29) | 🟢 open | Hygiene: `ruff format` landed via #92 (superseded #66, closed unmerged). **Open:** BLE001 lint select, pyright strict verdict docs, token-formatter dedup |
| [#30](https://github.com/michaeljabbour/amplifier-app-newtui/issues/30) | ✅ #69 | Collapse the session-op passthrough ladder (14 ops × 5 sites → one typed dispatch) |
| [#31](https://github.com/michaeljabbour/amplifier-app-newtui/issues/31) | ✅ #70 | Extract SessionOpsController from ui/app.py |
| [#32](https://github.com/michaeljabbour/amplifier-app-newtui/issues/32) | ✅ #71 | Extract LaneReducer from TranscriptReducer |
| [#33](https://github.com/michaeljabbour/amplifier-app-newtui/issues/33) | ✅ #72 | Lift the pure `_render_*` functions out of transcript.py (zero-risk split) |

## Rendering & model contract

| Issue | Status | Item |
|---|---|---|
| [#34](https://github.com/michaeljabbour/amplifier-app-newtui/issues/34) | ✅ #78 | Polish: italic, reading measure, checkbox glyphs, OSC 8 links, fence-copy, elapsed format |
| [#35](https://github.com/michaeljabbour/amplifier-app-newtui/issues/35) | ✅ #67 | Width-aware surface hint at `provider:request` |

## Runtime parity & perf

| Issue | Status | Item |
|---|---|---|
| [#36](https://github.com/michaeljabbour/amplifier-app-newtui/issues/36) | ✅ #56 (decision doc) | Lane/subagent todo surfacing (root-only today) |
| [#37](https://github.com/michaeljabbour/amplifier-app-newtui/issues/37) | ✅ #79 | Hybrid transcript history — ADR-0007 perf escalation (5k blocks miss frame budget) |
| [#38](https://github.com/michaeljabbour/amplifier-app-newtui/issues/38) | ✅ #73 | Child sessions bypass TUI posture gating; runtime skill overlays not propagated |
| [#39](https://github.com/michaeljabbour/amplifier-app-newtui/issues/39) | ✅ #74 | Per-lane steering (queue a message to a running delegate) |
| [#40](https://github.com/michaeljabbour/amplifier-app-newtui/issues/40) | ✅ #75 | Post-rewind ghost turns on resume |
| [#41](https://github.com/michaeljabbour/amplifier-app-newtui/issues/41) | ✅ #76 | Approval bar → needs-you parking |
| [#42](https://github.com/michaeljabbour/amplifier-app-newtui/issues/42) | ✅ #68 | Lane label aliasing + historical mode badges |

## CLI / session parity (Bucket B — nice-to-have; core parity done)

| Issue | Status | Item |
|---|---|---|
| [#43](https://github.com/michaeljabbour/amplifier-app-newtui/issues/43) | ✅ #80 | First-run onboarding gate + provider management |
| [#44](https://github.com/michaeljabbour/amplifier-app-newtui/issues/44) | ✅ #83 | `/config` live editing |
| [#45](https://github.com/michaeljabbour/amplifier-app-newtui/issues/45) | ✅ #82 | Session-manager ops (delete/rename/background, resume picker) |
| [#46](https://github.com/michaeljabbour/amplifier-app-newtui/issues/46) | ✅ #81 | `source` command group + `routing list/use` CLI |
| [#47](https://github.com/michaeljabbour/amplifier-app-newtui/issues/47) | ✅ #84 | Desktop/OSC 777 notifications beyond the shipped bell |
| [#48](https://github.com/michaeljabbour/amplifier-app-newtui/issues/48) | ✅ #85 | `@mention` expansion in the runtime path — decide + implement |

## Amplifier-team feedback round (2026-07-22)

| Issue | Status | Item |
|---|---|---|
| [#51](https://github.com/michaeljabbour/amplifier-app-newtui/issues/51) | ✅ #86 | Mount `context-intelligence-logging` behavior; custom telemetry destinations |
| [#52](https://github.com/michaeljabbour/amplifier-app-newtui/issues/52) | ✅ #87 | Routing-matrix: mount `hooks-routing` (settings bridge + spawner glue already shipped) |
| [#53](https://github.com/michaeljabbour/amplifier-app-newtui/issues/53) | ✅ #59 (decision doc) | Anchors pin lifecycle: automate pin bumps, surface staleness |
| [#54](https://github.com/michaeljabbour/amplifier-app-newtui/issues/54) | ✅ #60 (decision doc) | Evaluate `microsoft/amplifier-agent` as the runtime integration layer (spike + decision doc) |

(Provider loading needed no new issue — it already works via `config.providers` +
`keys.env` and is documented in SETTINGS.md; the UX on top is [#43](https://github.com/michaeljabbour/amplifier-app-newtui/issues/43).)

## Self-improving harness

| Issue | Status | Item |
|---|---|---|
| [#49](https://github.com/michaeljabbour/amplifier-app-newtui/issues/49) | 🟢 open | Forge-driven capability test tier — validate the real TUI through a real terminal. Design/decision merged (#57); **implementation in progress** on `auto/forge-capability-tier-impl` |
| [#50](https://github.com/michaeljabbour/amplifier-app-newtui/issues/50) | ✅ #58 (decision doc) | Self-improvement loop over skills/harness (SkillOpt discipline, AIDE² safeguards; references documented in-issue) |

## Follow-ups filed during the campaign (open)

| Issue | Status | Item |
|---|---|---|
| [#90](https://github.com/michaeljabbour/amplifier-app-newtui/issues/90) | 🟢 open | Live tail: attach the streaming block to the lane/item it's working on (not a detached bottom strip) |
| [#91](https://github.com/michaeljabbour/amplifier-app-newtui/issues/91) | 🟢 open | Lane "done" row shows raw markdown result instead of a clean summary |

## Non-goals

- **Syntax highlighting in answers.** Doable, but fights the restraint aesthetic
  and churns goldens forever; calm teal verbatim reads better in a transcript
  than rainbow soup.
- **Ingested-source deletion** (corpus "Delete original" UI) — not a newtui
  feature; no amplifier tool exposes a corpus-document delete.
- **Admin surface** — `module`, `source`-authoring, `tool invoke`, `reset`,
  `--install-completion`, `session cleanup`, replay: one-time/admin operations
  that belong in a small separate `amplifier-admin` CLI, not the TUI. (The
  `source` *override* group in #46 is the exception, kept for parity.)

---

## Shipped ledger (compact — details in git history and ARCHITECTURE.md)

**Audit round (2026-07-22 → 07-23)** — the full #21–#54 backlog cleared in 34 merged
PRs (#55–#89, #92): security (fail-closed EXEC scan #62, secret-scrub #61, write enforcer
#63, posture inheritance #73), reliability (crash-proof turns #89, hardening pass #77,
no post-rewind ghosts #75), the ui/app.py and transcript.py refactors (#69–#72), CLI/session
parity (onboarding #80, `/config` #83, session-manager #82, `source`/routing #81/#87,
`@mention` #85, notifications #84, telemetry bridge #86), perf (5k transcript budget #79,
no per-token delta persistence #64), and six 2026-07-22 design/decision docs (#55, #56, #57,
#58, #59, #60). Open residue: #29 (hygiene) and #49 (forge tier, in progress).

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
coexists); PlanPanel bottom strip + `Plan N/M` narrow-footer fallback
(responsive either/or, PR #13).

**Streaming & inline** — committed lines use the final renderer during
streaming; `**bold**`, `` `code` ``, `[text](url)` in `_inline()`; blockquotes
as a `▌` left gutter (style token `blue` — revisit only if the mockup says dim).

**Ambient progress** — delegate summary blocks, real focused-lane transcripts
from diverted child events, lane live tail with ctrl+o cycling (PRs #13, #17).

**Runtime honesty** — thin-wrapper bundle over pinned anchors; hook suppression
(not stripping); `hooks-logging` owns `events.jsonl`, app owns `ui-events.jsonl`;
resume replays under the stored bundle (explicit `--bundle` overrides); event
canary for un-consumed engine event kinds (PRs #19, #20). v0.1.0 tagged.
