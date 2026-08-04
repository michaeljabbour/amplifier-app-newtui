# Staged adoption — amplifier-app-tui replacing amplifier-app-cli

**Ledger version 1 · opened 2026-08-03 · no stage has started.**

amplifier-app-tui does not replace [amplifier-app-cli](https://github.com/microsoft/amplifier)
by declaration. It advances through five gates, in order, and each one has a named owner,
a minimum usage window, written entry and exit criteria, and a decision recorded in a
file you can diff. This directory is that record.

The shape comes from Brian Krabach's proposal (Teams, 2026-07-21) and MJ Jabbour's
agreement in the same thread:

> Advance through five gates: MJ daily-driver approval, Brian daily-driver approval,
> feedback from at least three more users, team-wide default use, and only then
> replacement of amplifier-app-cli.

A stage can move in as little as **one day** — but only after its gate is actually
cleared. Elapsed time is a floor, never a reason.

## Files

| File | What it holds |
|---|---|
| `stages.tsv` | one row per stage: owner, window, criteria ids, tested commit, dates, entry/exit evidence, decision |
| `blockers.tsv` | defects found while daily-driving. `release-blocking` rows gate promotion; `friction` rows are tracked, not gating |
| `feedback.tsv` | stage-3 daily-driver seats, each tied to the exact commit that participant ran, each with a tracked disposition |
| `../../scripts/adoption_gate.py` | read-only checker: validates the ledger and answers "may stage N be promoted?" |
| `../../scripts/adoption_smoke.sh` | the compatibility smoke run at every gate |

The TSVs are hand-edited in a reviewed PR. Nothing writes them automatically — **git
history is the audit trail**, and a promotion is a commit somebody signed off on.

## The five stages

| Stage | Owner | Min window | Gate |
|---|---|---|---|
| 1 | MJ Jabbour | ≥ 1 day | MJ daily-driver approval |
| 2 | Brian Krabach | ≥ 1 day | Brian daily-driver approval |
| 3 | three additional daily drivers (`feedback.tsv` seats) | ≥ 1 day | consolidated feedback, every seat dispositioned |
| 4 | MJ Jabbour (team-wide default) | ≥ 1 day | documented rollback path + amplifier-app-cli still available |
| 5 | MJ Jabbour (replacement) | — | ledger shows zero unresolved release-blockers |

Owner semantics: the stage owner is accountable for recording the stage — running the
smoke, filing blockers, and writing the decision. For stages 1 and 2 the owner is also
the daily driver whose approval *is* the gate. Stage 3's daily drivers are the seat
holders; MJ owns consolidating their feedback. Stages 4 and 5 are team-level actions, so
the repo maintainer owns the record.

## Entry and exit criteria

`stages.tsv` stores criteria **ids**; the prose lives here so it is versioned once.

**S1-entry** — the candidate build passes `scripts/adoption_smoke.sh`; its commit is
recorded as `tested_commit`; `start_date` is set.
**S1-exit** — MJ has used that build as his primary Amplifier interface for at least one
full working day, with completed real tasks named in `exit_evidence` (not session counts);
every defect found is filed in `blockers.tsv`; no `release-blocking` row is open.

**S2-entry** — S1 is `promoted`; Brian is on a recorded `tested_commit` (the same build,
or a newer one, in which case the newer commit is what gets recorded).
**S2-exit** — same bar as S1-exit, for Brian.

**S3-entry** — S2 is `promoted`; all three seats in `feedback.tsv` are filled with named
participants, each with the commit they ran.
**S3-exit** — each seat has task-completion evidence, a friction report, and a
disposition that is no longer `untriaged`; release-blocking findings are filed in
`blockers.tsv` and closed.

**S4-entry** — S3 is `promoted`; the rollback path below has been *walked*, not just
read, and the drill is cited in `entry_evidence`.
**S4-exit** — the team has used amplifier-app-tui as its default for at least one working
day; amplifier-app-cli remained installed and usable throughout (this is a hard
requirement of stage 4, not a courtesy); no `release-blocking` row is open.

**S5-entry** — `python3 scripts/adoption_gate.py promote 4` exits 0. That command *is*
the replacement gate.
**S5-exit** — amplifier-app-cli is marked deprecated in the team's docs and
amplifier-app-tui is the documented default. Deprecated is not removed; see
[Rollout messaging](#rollout-messaging) below.

## Blocking defects — why a window is never enough

The failure this governance exists to prevent is a promotion that happens because a week
went by, not because the software got good. So the gate has two independent conditions
and both must hold:

1. the usage window has elapsed, **and**
2. no `release-blocking` row in `blockers.tsv` is `open`.

An open release-blocking defect blocks **every** stage promotion, at any stage,
regardless of elapsed time — including stage 4, which is the replacement gate. It is
deliberately repo-wide rather than stage-scoped: a reliability defect found by a stage-3
seat does not stop mattering because the calendar moved on.

`friction` severity is the other half. Not every complaint is a blocker, and burying
qualitative friction to keep the gate green would be its own dishonesty — so friction
rows are recorded and triaged alongside, and simply do not gate.

## Stage 3 — the three seats

AC3 needs at least three additional daily drivers. The brief defines the seats but does
**not** name the people; they have not been chosen. So `feedback.tsv` ships three real,
reserved seats with `participant = TBD`. Filling a name is a one-line edit in a PR — this
is a blank to fill, not a blocker, and no name has been invented to make the file look
complete.

Each seat records the **exact commit that participant ran**. That is what makes a report
reproducible: "it hung on resume" is only actionable if you know which build hung. A seat
without a `tested_commit` cannot clear the stage-3 gate.

Dispositions are `untriaged → fixed | deferred | wont-fix | duplicate`. "Tracked" means
moved off `untriaged` with a reference — `deferred` is a legitimate, recorded answer;
silence is not.

## Rollback path

amplifier-app-tui (`amplifier-tui`) and amplifier-app-cli (`amplifier`) are **separate
tool installs with separate binaries and no dependency ties**, sharing the same
`~/.amplifier/` configuration in both directions. That is what makes rollback cheap:
there is nothing to uninstall and no configuration to restore.

**Roll back to amplifier-app-cli** — run `amplifier` instead of `amplifier-tui`. Keys and
settings in `~/.amplifier/` carry over as-is. If it is not installed:

```sh
uv tool install git+https://github.com/microsoft/amplifier
amplifier init      # only if ~/.amplifier/keys.env is not already set up
```

**Roll back to the last known-good TUI build** — every stage records a `tested_commit`
precisely so this is possible:

```sh
uv tool install --force git+https://github.com/michaeljabbour/amplifier-app-tui@<tested_commit>
```

**Then record it.** A rollback is evidence, not an embarrassment: file the cause in
`blockers.tsv` as `release-blocking`/`open`, and set the stage's `decision` to
`rolled-back`. The gate will refuse to promote anything until that row is resolved.

**Known limitation, stated plainly:** an in-flight TUI session transcript does not
transfer to amplifier-app-cli — the two keep their own session stores. Use `/export`
(writes the transcript as markdown to `exports/`) before switching if you need the
history.

**Continued access through stage 4 (AC4).** Nothing in this repo installs, upgrades, or
removes `amplifier`. Stage 4's exit criterion requires amplifier-app-cli to have remained
installed and usable for the whole team-wide window; if anyone had to be told "just use
the TUI, the CLI is gone," stage 4 has failed and must be re-run.

## Rollout messaging

Three states, and only these three, so nobody has to guess what a stage means for them:

| State | When | What it means for a user |
|---|---|---|
| **default** | stage 4 promotes | amplifier-app-tui is what you get and what we support first. `amplifier` still works, unchanged. |
| **deprecated** | stage 5 promotes | amplifier-app-cli still installs and runs; it stops being the recommended path and stops getting parity work. |
| **removed** | never implied by a stage | a separate, separately-announced decision. Promotion through stage 5 does not authorize removal. |

## The compatibility smoke

One command, run at every gate:

```sh
scripts/adoption_smoke.sh              # full
scripts/adoption_smoke.sh --no-forge   # without the real-PTY tier
```

It adds **no new test suite**. It composes the gates this repo already documents in
[DEVELOPMENT.md](../DEVELOPMENT.md) — `ruff check` → `ruff format --check` →
`pyright src/` → `pytest -q` → the forge real-PTY capability tier (which skips cleanly
when forge is unavailable) — and finishes with `adoption_gate.py check` so a malformed
ledger is itself a smoke failure. Steps 1–4 are exactly what a PR must pass, so a red
smoke is never a smoke-only artifact.

Participants do not run the smoke; the stage owner does, at the gate, and records the
commit. Participants report what broke while they were working.

## Recording a promotion

```sh
python3 scripts/adoption_gate.py status         # where everything stands
scripts/adoption_smoke.sh                       # prove the build
python3 scripts/adoption_gate.py promote 1      # may stage 1 be promoted?
```

`promote` prints `PROMOTE stage N: gate clear` (exit 0) or `BLOCKED stage N` with one
line per unmet reason (exit 1). When it is clear, edit `stages.tsv` — set `end_date`,
`exit_evidence`, and `decision = promoted` — and open a PR. The tool never edits the
ledger; refusing and recording are different jobs, and only a human does the second.

## Why this shape

A plain document was the first option considered, and it covers most of this: owners,
windows, criteria, rollback, and messaging are all prose, and prose is where they belong.
What prose cannot do is enforce a **negative**. AC2 and AC5 are both of the form "this
must not happen even though it looks ready," and a sentence saying so is exactly the kind
of rule that gets waved through at 5pm on a Friday. So there is one small tool —
~330 lines of stdlib Python, read-only, no dependencies — whose entire job is to say no.

Everything the tool does not need is not in it: no state machine, no promotion command,
no dashboard, no service. It cannot modify a ledger file. AC4 (rollback path documented,
CLI access continued) stays **document-enforced on purpose** — the honest check for
"is the rollback path real" is walking it, and a substring match on an evidence column
would be theater dressed as verification.

The ledger format follows the repo's existing convention (`pipelines/ledger.tsv` +
`pipelines/ledger.py`): TSV rows, `#` comments, stdlib-only tooling that never raises.

## Acceptance criteria map

| AC | Where it is satisfied | Enforced by |
|---|---|---|
| AC1 owner, ≥1-day window, entry/exit criteria, recorded decision per stage | `stages.tsv` + [criteria](#entry-and-exit-criteria) | `adoption_gate.py check` (a `promoted` row missing evidence is a hard error) |
| AC2 blocking defects prevent promotion even after the window elapses | [Blocking defects](#blocking-defects--why-a-window-is-never-enough) | `adoption_gate.py promote` — window and blockers are independent conditions |
| AC3 ≥3 additional daily drivers, feedback consolidated into tracked dispositions | `feedback.tsv` | `adoption_gate.py promote 3` — 3 named seats, each with a commit and a non-`untriaged` disposition |
| AC4 team-wide stage has a documented rollback path and continued CLI access | [Rollback path](#rollback-path), S4-exit | document + review (deliberately not machine-checked) |
| AC5 replacement only after zero unresolved release-blockers | S5-entry | `adoption_gate.py promote 4` — the replacement gate |
