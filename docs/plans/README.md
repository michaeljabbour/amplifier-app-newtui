# Implementation plans

Dated, execution-ready plans and design/decision docs. Each carries a **Status**
banner at the top; once a plan lands (or a decision is recorded) it stays here as the
historical record of what was decided and why — deviations are noted in the banner. The
live list of open work is [../BACKLOG.md](../BACKLOG.md).

## Implementation plans

| Plan | Status |
|---|---|
| [2026-07-20 anchors migration](2026-07-20-anchors-migration-implementation.md) | ✅ Implemented (one noted deviation: logging split reversed by PR #19) |
| [2026-07-21 ambient progress — design](2026-07-21-ambient-progress-design.md) | ✅ Implemented (PR #13) |
| [2026-07-21 phase 1 — plan panel](2026-07-21-ambient-progress-phase1-plan-panel.md) | ✅ Implemented (PR #13) |
| [2026-07-21 phase 2 — delegate summary](2026-07-21-ambient-progress-phase2-delegate-summary.md) | ✅ Implemented (PR #13) |
| [2026-07-21 phase 3 — lane live tail](2026-07-21-ambient-progress-phase3-live-tail.md) | ✅ Implemented (PR #13, deepened in PR #17) |

## Decision / design docs (2026-07-22 audit round)

Doc-only deliverables from the backlog campaign — the design or decision *is* the
artifact; any follow-on implementation is tracked in the linked issue.

| Doc | Status |
|---|---|
| [governance — default EXEC posture](2026-07-22-governance-exec-default-posture.md) | 🧭 Decision recorded (PR #55; issue #26 closed) |
| [lane / subagent todo surfacing](2026-07-22-lane-todo-surfacing.md) | 🧭 Decision recorded (PR #56; issue #36 closed) |
| [forge-driven capability tier](2026-07-22-forge-capability-tier.md) | 📐 Design merged (PR #57); implementation in progress on `auto/forge-capability-tier-impl` (issue #49 open) |
| [self-improvement loop over skills/harness](2026-07-22-self-improvement-loop.md) | 🧭 Decision recorded (PR #58; issue #50 closed) |
| [anchors pin lifecycle](2026-07-22-anchors-pin-lifecycle.md) | 🧭 Decision recorded (PR #59; issue #53 closed) |
| [amplifier-agent eval (spike + decision)](2026-07-22-amplifier-agent-eval.md) | 🧭 Decided — stay core-native (PR #60; issue #54 closed) |
