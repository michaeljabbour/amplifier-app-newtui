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

**To actually run this program, go to [RUNBOOK.md](RUNBOOK.md)** — the ready-to-fill
participant sheet, the exact commands each participant runs, and what each stage owner must
decide. This file is the policy; that one is the procedure.

A stage can move in as little as **one day** — but only after its gate is actually
cleared. Elapsed time is a floor, never a reason.

## Files

| File | What it holds |
|---|---|
| `stages.tsv` | one row per stage: owner, window, criteria ids, tested commit, dates, entry/exit evidence, decision |
| `blockers.tsv` | defects found while daily-driving. `release-blocking` rows gate promotion; `friction` rows are tracked, not gating |
| `feedback.tsv` | stage-3 daily-driver seats, each tied to the exact commit that participant ran, each with a tracked disposition |
| `RUNBOOK.md` | how to actually run it: the participant sheet, the exact commands, what each owner must decide |
| `../../scripts/adoption_gate.py` | read-only checker: validates the ledger, answers "may stage N be promoted?", and verifies the mechanical half of the rollback path |
| `../../scripts/adoption_smoke.sh` | the compatibility smoke run at every gate |

The TSVs are hand-edited in a reviewed PR. Nothing writes them automatically — **git
history is the audit trail**, and a promotion is a commit somebody signed off on.

## The five stages

| Stage | Owner | Min window | Gate |
|---|---|---|---|
| 1 | MJ Jabbour | ≥ 1 day | MJ daily-driver approval |
| 2 | Brian Krabach | ≥ 1 day | Brian daily-driver approval |
| 3 | MJ Jabbour (consolidating three `feedback.tsv` seats) | ≥ 1 day | consolidated feedback, every seat dispositioned |
| 4 | MJ Jabbour (team-wide default) | ≥ 1 day | documented rollback path + amplifier-app-cli still available |
| 5 | MJ Jabbour (replacement) | ≥ 1 day | final observation, then zero-blocker replacement decision |

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

**S5-entry** — `python3 scripts/adoption_gate.py promote 4` exits 0; the stage-5 row records
the candidate commit, start date, and the clear stage-4 gate in `entry_evidence`. Both tools
remain available during a final observation window of at least one full day.
**S5-exit** — no `release-blocking` row is open after that window; the observation is cited
in `exit_evidence`; amplifier-app-cli is marked deprecated in the team's docs and
amplifier-app-tui is the documented default. `python3 scripts/adoption_gate.py promote 5`
is the final replacement gate. Deprecated is not removed; see [Rollout messaging](#rollout-messaging).

## Blocking defects — why a window is never enough

The failure this governance exists to prevent is a promotion that happens because a week
went by, not because the software got good. So the gate has two independent conditions
and both must hold:

1. the usage window has elapsed, **and**
2. no `release-blocking` row in `blockers.tsv` is `open`.

An open release-blocking defect blocks **every** stage promotion, at any stage,
regardless of elapsed time — including stage 5, which is the replacement gate. It is
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

`TBD` is a **refused value, not a counted one.** The gate does not merely fail to count it;
it names the seat and says so: `seat-1 is unfilled: participant 'TBD' is a placeholder, not
a named person`. See [Placeholders are refused](#placeholders-are-refused).

Each seat records the **exact commit that participant ran**. That is what makes a report
reproducible: "it hung on resume" is only actionable if you know which build hung. A seat
without a `tested_commit` cannot clear the stage-3 gate.

Dispositions are `untriaged → fixed | deferred | wont-fix | duplicate`. "Tracked" means
moved off `untriaged` with a reference — `deferred` is a legitimate, recorded answer;
silence is not.

## Placeholders are refused

A ledger is only worth something if the names in it are names. A cell that merely *looks*
filled is worse than an empty one: it survives review, it satisfies a word count, and it
quietly counts as a daily driver. So the tool refuses stand-ins by name rather than merely
failing to count them.

**What counts as a placeholder** — enumerated once, in `PLACEHOLDERS` in
`scripts/adoption_gate.py`, and used everywhere a cell is supposed to hold a real person or
a real piece of evidence:

`-`, `--`, `.`, an empty cell, whitespace only, `?` / `??` / `???`, `n/a`, `na`, `nil`,
`none`, `null`, `tba`, `tbc`, `tbd`, `todo`, `to-do`, `unassigned`, `unfilled`, `unknown`,
`xx`, `xxx`, `fixme`, `pending`, `placeholder`, `someone`, `somebody`, `anyone`, `name`,
`name here`, `your name`, `participant`, `owner`.

Comparison is case-folded, and the value is normalized the way a human actually types a
blank first: non-breaking spaces become spaces, internal whitespace is collapsed, and
wrapping punctuation is stripped — so `<name>`, `[ TBD ]`, `` `?` `` and `"N/A"` are all the
same answer: nobody. `-` is deliberately in the list: "not recorded" and "recorded as
nothing" are the same fact, and splitting them would just create a second, weaker code path.

Person fields have one additional, deliberately separate rule: a role is not a name.
`PERSON_ROLE_PLACEHOLDERS` and the stage-seat pattern reject values such as `team`, `daily
drivers`, and `stage-3 seats (see feedback.tsv)` as owners or participants. They remain valid
inside ordinary evidence text, where phrases such as "team-wide smoke" are meaningful.

**Where it is enforced:**

| Field | Rule |
|---|---|
| stage `owner` | a placeholder is a **hard validation error**. Every stage needs someone accountable for the record, always — there is no legitimate state in which a stage has no owner |
| stage-3 `participant` | a **reserved, untouched** seat may hold `TBD` (the people genuinely have not been chosen). It can never satisfy the gate: `promote 3` names each unfilled seat individually |
| stage-3 `participant` **with evidence** | a **hard validation error**. A seat carrying a commit, a date, a report or a disposition is *claiming somebody sat in it*, and an anonymous claim is not evidence |
| `tested_commit` | a placeholder means "not recorded" and blocks promotion; a non-placeholder must be a real commit (below) |
| `entry_evidence` / `exit_evidence` | a placeholder does not count as evidence, at promote time **or** on a row already marked `promoted` |
| blocker `resolution` | a `resolved` row whose resolution is a placeholder is a validation error |

The distinction that matters: a **reserved blank** is legitimate and expected — nobody has
been asked yet. A **filled-in blank** is a fabrication with the serial numbers filed off,
and it is a hard error, so it cannot ride along in a ledger nobody happened to re-gate.

## Hand-editing `promoted` does not bypass the gate

`promote` is advisory: it says yes or no, and a human still hand-edits `decision = promoted`
in a reviewed PR. That is deliberate — refusing and recording are different jobs. But it
would be a hole if `check` only verified that the evidence columns were non-empty, because
then editing one word in the decision column would be the entire bypass.

So `check` **re-derives every promotion condition against every row that claims to be
promoted**, every time it runs: real tested commit, real entry and exit evidence, start and
end dates recorded, the minimum window actually elapsed between them, every earlier stage
already promoted, no release-blocker already open at the recorded promotion date, and — for
stage 3 — three named seats with dates, completion evidence, friction reports, dispositions,
and disposition references. A promotion that was never gated fails `check`, which fails the
smoke, which fails the next PR. A blocker opened later does not rewrite history, but it still
blocks every future promotion.

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
bash -o pipefail -c "curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/michaeljabbour/amplifier-app-tui/main/scripts/install.sh | bash -s -- --ref <tested_commit>"
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

### The rollback path is verified, not just written down

```sh
python3 scripts/adoption_gate.py rollback
```

A previous round left this section wholly document-enforced, on the grounds that a
substring match on an evidence column would be theater. That reasoning is right about
*qualitative* evidence and wrong about the mechanical half — every claim above that is a
claim about **files in this repository** can be checked, and if it is not, a one-line
`pyproject.toml` edit could quietly falsify the whole story with nothing to catch it.

So `rollback` checks exactly that half, and nothing more:

| Check | What would break it |
|---|---|
| the rollback path is documented | this section removed or renamed |
| the amplifier-app-cli restore command is well-formed | the `uv tool install git+…` line malformed, or pointed somewhere other than `microsoft/amplifier` |
| the pinned-build command is well-formed | the `uv tool install --force git+…@<tested_commit>` line malformed, pointed at a repo other than this app's own `APP_REPO_URL` (read from `kernel/updater.py`), or pinning a placeholder that is not the ledger's `tested_commit` column |
| both executables install side by side | `pyproject.toml` declaring `amplifier`, or any second console script — the exact collision ADR-0008 measured on uv 0.10.2 |
| the coexistence decision is recorded | ADR-0008 deleted or reversed |
| no dependency tie between the two apps | this app taking a dependency on `amplifier` / `amplifier-app-cli`, which would make rolling one back drag the other |
| nothing here installs, upgrades, or removes amplifier-app-cli | any executed `uv` argv in `src/` or `scripts/` containing `uninstall`, or naming `amplifier` as its target. This reads **argv literals via the AST**, not the file text — `kernel/reset.py` has a docstring saying it deliberately does *not* do this, and a substring scan cannot tell a sentence from a subprocess call |
| every recorded `tested_commit` is real | a fabricated or mistyped sha in the ledger — the pin you would actually roll back to |

**And it prints what it did not do, every time.** These stay human, because the honest
check for "is the rollback path real" is a person walking it:

- run `amplifier` after `amplifier-tui` on a real machine and confirm both launch;
- confirm `~/.amplifier/` settings and keys carry over in both directions;
- confirm amplifier-app-cli stayed installed and usable for the **whole** stage-4 window.

`rollback` runs as part of [the compatibility smoke](#the-compatibility-smoke), so it is
re-checked at every gate. A green run is a precondition for S4-entry, not a substitute for
the drill — cite the drill (date, who walked it) in `entry_evidence`.

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

## What is machine-checked — and what is deliberately not

The line is not "whatever was easy." It is: **a claim with one right answer gets checked; a
judgment gets a named human.** Automating a judgment does not make it rigorous, it makes it
look rigorous, which is worse.

**Machine-checked** (`adoption_gate.py check` / `promote` / `rollback`):

| Claim | How |
|---|---|
| every stage has a real, named owner | placeholder or role-only owner is a hard validation error |
| stage-3 seats hold real, named people | placeholders refused by name, per seat |
| a seat carrying evidence has a name on it | hard validation error |
| `tested_commit` is a real build | 7–40 hex, and — when git can be consulted — an object that exists in this clone |
| dates are well-formed | `YYYY-MM-DD` or `-`, everywhere a date appears |
| dates are ordered | `end_date` ≥ `start_date`; no `end_date` without a `start_date`; no stage starting before the previous one ended |
| the minimum window actually elapsed | on promotion **and** on any row already marked `promoted` |
| stages promoted in order | checked both at promote time and on the recorded row |
| a `promoted` row carries the evidence its criteria demand | `check` re-derives the whole gate for every promoted row |
| no open release-blocking defect | repo-wide, independent of elapsed time (AC2/AC5) |
| every seat has a complete feedback record | missing date, completion evidence, friction, disposition, or disposition reference blocks stage 3 |
| the rollback mechanics hold | see [above](#the-rollback-path-is-verified-not-just-written-down) |

**Left to human judgment, on purpose:**

- **Whether the friction reported was acceptable.** `friction` vs `release-blocking` is the
  single most consequential call in this program, and it is a judgment about whether you
  would ask a teammate to live with something. No string check can make it, and a tool that
  pretended to would just teach people to phrase around it.
- **Whether `exit_evidence` describes real work.** The tool can insist the cell is not a
  placeholder. It cannot know whether "shipped PR #231" happened. Reviewers can — that is
  what the PR is for.
- **Whether the software is actually good.** `promote` says the gate is clear. The owner
  says whether to promote. Those are different sentences, and the `decision` column is where
  the second one lives.
- **Whether the rollback drill was walked.** A person running the commands on a real
  machine is the only honest check, and the tool says so out loud rather than accepting a
  substring in an evidence column as proof.

Two things follow from that line. `check` never guesses: when git cannot answer honestly —
no git binary, not a work tree, or a **shallow clone** where a correctly-recorded commit is
genuinely absent — it says it cannot tell rather than reporting a real commit as fabricated.
And the tool is still read-only: it validates, refuses, and explains, and a human writes
every row.

## Why this shape

A plain document was the first option considered, and it covers most of this: owners,
windows, criteria, rollback, and messaging are all prose, and prose is where they belong.
What prose cannot do is enforce a **negative**. AC2 and AC5 are both of the form "this
must not happen even though it looks ready," and a sentence saying so is exactly the kind
of rule that gets waved through at 5pm on a Friday. So there is one tool — a single
stdlib-only file, read-only, no dependencies — whose entire job is to say no.

Everything the tool does not need is not in it: no state machine, no promotion command,
no dashboard, no service. It cannot modify a ledger file.

AC4 was originally left wholly document-enforced, on the argument that the honest check for
"is the rollback path real" is walking it and a substring match on an evidence column would
be theater. Half of that still stands — and is now printed by the tool itself every time
`rollback` runs. The other half was a mistake: the *mechanical* claims (command shapes, the
pinned commit, side-by-side installability, no dependency tie) are claims about files in
this repository with exactly one right answer, so `rollback` checks them and the smoke runs
it at every gate. Verifying a fact is not theater; verifying a judgment would be.

The ledger format follows the repo's existing convention (`pipelines/ledger.tsv` +
`pipelines/ledger.py`): TSV rows, `#` comments, stdlib-only tooling that never raises.

## Acceptance criteria map

| AC | Where it is satisfied | Enforced by |
|---|---|---|
| AC1 owner, ≥1-day window, entry/exit criteria, recorded decision per stage | `stages.tsv` + [criteria](#entry-and-exit-criteria) | `adoption_gate.py check` — named owner (no placeholders), real `tested_commit`, ordered dates, and the **whole gate re-derived** for any row marked `promoted` |
| AC2 blocking defects prevent promotion even after the window elapses | [Blocking defects](#blocking-defects--why-a-window-is-never-enough) | `adoption_gate.py promote` — window and blockers are independent conditions |
| AC3 ≥3 additional daily drivers, feedback consolidated into tracked dispositions | `feedback.tsv`, [participant sheet](RUNBOOK.md#the-participant-sheet) | `adoption_gate.py promote 3` — 3 **named** seats (placeholders and role labels refused), each with a real commit, date, completion evidence, friction report, non-`untriaged` disposition, and disposition reference |
| AC4 team-wide stage has a documented rollback path and continued CLI access | [Rollback path](#rollback-path), S4-exit | `adoption_gate.py rollback` for the mechanical half (command shapes, pinned commit, side-by-side install, no dependency tie); the drill itself stays human and the tool prints which half it skipped |
| AC5 replacement only after zero unresolved release-blockers | S5 entry/exit | `adoption_gate.py promote 4` opens the final ≥1-day observation window; `adoption_gate.py promote 5` is the replacement gate and independently rechecks blockers |
