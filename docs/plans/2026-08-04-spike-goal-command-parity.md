# Design spike — gap `111`: `/goal` autonomous continuation in a Textual TUI

**Status:** spike, not a plan. Written to make gap `111` decidable in one sitting.
**Owner decision required.** Nothing here is approved; see
[parity-decision-sheet.md](../audits/parity-decision-sheet.md).

## Why this gap gets a spike and the other seven don't

Gaps `112`–`117` are one-line-remediation shaped: a scaffold writer, a click
completion flag, two read-only introspection groups, a prompt prefix, a URL
deep-link. Their design question is "do we want it?", not "how would it work?" —
a spike would be ceremony.

Gap `111` is different. `/goal <condition>` is not a command; it is **a loop that
takes the turn away from the user until a condition is met**, and it lands in an
event-driven TUI whose entire interaction model assumes a human closes each turn.
That is a design question, and answering it wrong is expensive in a way a missing
`/feedback` link is not.

## What the donor actually built

`microsoft/amplifier-app-cli@f1fcb66`:

| Piece | Cite | What it does |
|---|---|---|
| `/goal` verb, both entry points | `main.py:360-390,458-461,571-579` | parses an optional `--max-turns N`, then a free-text goal condition |
| Unlimited by default | `docs/decisions/ADR-0005-goal-unlimited-by-default.md` | the donor deliberately chose no turn cap as the default |
| Progress hook | `goal_progress_hook.py` (418 lines) | watches turns for progress; fires **stall detection while the agent is busy** (`00a49ad`) |
| Condition authoring skill | `data/skills/goalify/SKILL.md` + two known-bad examples | teaches the model to write a *checkable* condition (the L1-ordering and L2-quantifier failure modes are shipped as fixtures) |
| Condition written to file | `f1fcb66` | the condition is persisted, not held in chat scrollback |

The shipped shape is telling: roughly one-third of the donor's investment is in
*making the goal condition checkable at all*. A naive port that adds a verb and a
`while not done: continue` loop reproduces the command and none of the value.

## The four questions this repo has to answer before implementing

1. **Who owns the turn?** Every seam here — composer, live status band, queued
   strip, needs-you badge — assumes a turn ends and the human types next. An
   autonomous continuation loop has to either (a) synthesize turns through the
   existing reducer path so the transcript, cost footer, and rewind checkpoints
   stay truthful, or (b) run beside it and lie to all three. Only (a) is
   acceptable under ADR-0007; the reducer never touches widgets, so the loop
   belongs in `kernel/`, driven by typed events, with `ui/` merely rendering a
   "goal active" state.

2. **How does it interact with governance?** An unattended loop that hits an
   approval gate stalls forever; one that runs in `auto` posture spends money
   unattended. The donor's answer is stall detection plus an unlimited default.
   This repo already floors approval timeouts at 3600s (`kernel/runtime.py:495`)
   precisely so a human reading a plan isn't auto-denied — that floor and an
   unattended loop are in direct tension and the interaction has to be designed,
   not discovered in production.

3. **What bounds it?** The donor chose unlimited-by-default and wrote an ADR to
   defend it. This repo has a visible per-session cost footer and a `stats`
   command, i.e. it has already decided that spend is a thing the user watches.
   An unlimited loop in a full-screen TUI where the user may have walked away is
   a different risk than the same loop in a scrolling terminal they are watching.
   **Recommendation if implemented: bounded by default, `--max-turns` to raise it,
   and a hard cost ceiling — deliberately diverging from the donor's ADR-0005.**
   Parity is a decision process; this is exactly the kind of place to diverge.

4. **Is the condition checkable?** Without the `goalify` skill's discipline, "the
   tests pass" and "the feature is done" are indistinguishable to the loop, and
   the second one never terminates. Porting `/goal` without porting condition
   authoring ships the failure mode, not the feature.

## Smallest honest increment (if the owner accepts)

1. `kernel/` goal state: condition text, turn budget, spend budget, `active`
   flag, persisted with the session — no UI, no loop. Testable in isolation.
2. A typed `GoalProgress` event emitted per turn + a stall predicate over it.
   Still no loop; `ui/` renders "goal active · turn 3/20" in the status band.
3. The continuation itself, gated on: budget remaining, no pending approval, no
   parked decision, no stall. First real end-to-end run through `DemoRuntime`
   (the demo is a contract — `DemoRuntime` must emit the same events).
4. `/goal` verb + `/goal stop`, palette entry, footer hint.
5. Condition-authoring guidance, ported or re-expressed.

Steps 1–2 are worth doing even if the loop is later rejected: "how much progress
did this session actually make" is useful on its own.

## Recommendation to the owner

**`deferred`, not `accepted` or `rejected`.** The capability is real and the donor
has shipped it, so `rejected` would be premature. But it is the only gap in this
pass that is a multi-week feature with an open architectural question against
ADR-0007's turn model, and accepting it today would put it in the transfer
pipeline ahead of seven one-line items. Defer it with this spike attached, decide
it deliberately, and do not let it block the other seventeen dispositions.
