# Adoption runbook — how to actually run the five stages

Everything a human needs to execute the staged adoption program, so the only missing input
is real usage by real people. [README.md](README.md) is the *policy* — owners, criteria,
rollback, messaging. This is the *procedure*.

**Nothing in this file has been executed.** No stage has started, no seat is filled, and no
decision has been recorded. The commands below are ready to run; the names and evidence are
blanks for the people who do the running.

---

## Before anything: what the tooling will and will not accept

`scripts/adoption_gate.py` refuses a ledger that *looks* filled in. Two rules bite hardest
and are worth knowing before you type:

1. **A placeholder is not a person.** `TBD`, `-`, `?`, `unknown`, `N/A`, `<name>`, an empty
   cell, and a cell containing only whitespace are all the same answer: nobody. They can
   never fill a stage-3 seat, own a stage, or stand in for entry/exit evidence. The full
   list is `PLACEHOLDERS` in the gate script — one list, checked everywhere.
2. **`tested_commit` must be a real commit.** 7–40 hex characters, and — when git can be
   consulted — an object that actually exists in this clone. `latest main` and a typo are
   both refused.

Run `python3 scripts/adoption_gate.py check` after any edit. It prints every problem at
once, and it never edits your file.

---

## Stage 1 and 2 — the daily-driver stages

**Stage 1 owner: MJ Jabbour. Stage 2 owner: Brian Krabach.** The owner *is* the daily
driver, so the owner runs all of this.

### 1. Install the candidate build and record it

```sh
uv tool install --reinstall git+https://github.com/michaeljabbour/amplifier-app-tui
amplifier-tui version                       # confirm what you actually got
```

From a clone of this repo, get the exact commit you are about to run:

```sh
git rev-parse --short HEAD                  # this is your tested_commit
```

### 2. Prove the build before you start the window

```sh
scripts/adoption_smoke.sh                   # full: lint, format, types, tests, forge, ledger, rollback
scripts/adoption_smoke.sh --no-forge        # same without the real-PTY tier
```

A green smoke at commit `<sha>` is your **entry evidence**. A red smoke is evidence too —
file it and stop; do not start the window on a build you could not prove.

### 3. Open the stage

Edit `docs/adoption/stages.tsv`, on your row:

| column | what to put |
|---|---|
| `tested_commit` | the short sha from step 1 |
| `start_date` | today, `YYYY-MM-DD` |
| `entry_evidence` | `smoke green @ <sha>` — cite the run, not the intent |
| `decision` | leave `pending` |

Commit it in a PR. `python3 scripts/adoption_gate.py check` must pass.

### 4. Daily-drive it for at least one full working day

Use `amplifier-tui` as your **primary** Amplifier interface. Not a trial alongside the CLI —
the primary one. The window is a floor, not a target: one day is the minimum, and finishing
the day does not by itself clear the gate.

**Every defect you hit gets filed**, immediately, while you remember it. Append a row to
`docs/adoption/blockers.tsv`:

```
BL-1	1	release-blocking	open	2026-08-05	-	resume drops accumulated cost after reconnect
```

| column | value |
|---|---|
| `id` | `BL-<n>`, next free number |
| `stage` | the stage you were in when you hit it |
| `severity` | `release-blocking` if you would not ask a teammate to live with it; otherwise `friction` |
| `status` | `open` |
| `opened` | today |
| `resolution` | `-` while open; the PR / issue / commit that closed it |
| `summary` | what broke, in one line, specific enough to reproduce |

**Severity is the honest call, and it is the whole point.** An open `release-blocking` row
blocks *every* stage promotion no matter how much time has elapsed. `friction` is tracked
and does not gate — which is exactly why burying a real blocker as friction to keep the gate
green would be the failure this program exists to prevent.

### 5. Close the stage and decide

```sh
python3 scripts/adoption_gate.py status
python3 scripts/adoption_gate.py promote 1     # or 2
```

If it prints `BLOCKED`, it lists every unmet reason. Fix the record or fix the software.

When it prints `PROMOTE stage N: gate clear`, **the owner still has to decide.** The tool
says the gate is clear; it does not say the software is good. See
[what the owner decides](#what-each-stage-owner-must-decide) below. Then edit your row:

| column | what to put |
|---|---|
| `end_date` | today |
| `exit_evidence` | the real tasks you completed on it — named, not counted |
| `decision` | `promoted`, `held`, or `rolled-back` |

`exit_evidence` is prose and stays prose: "shipped PR #231, debugged the ntfy routing
regression, ran three delegate fan-outs" is evidence. "12 sessions" is not — session counts
measure that you opened the app, not that it worked.

---

## Stage 3 — the three additional daily drivers

**Owner: MJ Jabbour** (consolidating). The seat holders are the daily drivers.

### The participant sheet

Three seats are reserved in `docs/adoption/feedback.tsv` and **all three are unfilled**. No
name has been invented to make the file look complete. Filling one is a one-line edit in a
PR.

Copy this row per participant, replace every `<…>`, and paste it over the matching `seat-N`
line. Columns are **tab-separated**.

```
seat-1	<full name>	3	<short sha they ran>	<YYYY-MM-DD>	<real tasks they completed>	<what got in their way>	untriaged	-
seat-2	<full name>	3	<short sha they ran>	<YYYY-MM-DD>	<real tasks they completed>	<what got in their way>	untriaged	-
seat-3	<full name>	3	<short sha they ran>	<YYYY-MM-DD>	<real tasks they completed>	<what got in their way>	untriaged	-
```

| column | who fills it | notes |
|---|---|---|
| `participant` | owner, once the person agrees | a real name. `TBD` is refused by name, and the gate will tell you which seat is still empty |
| `tested_commit` | participant | the commit **they** ran — not the owner's. A report is only reproducible against a known build |
| `date` | participant | the day they drove it |
| `completion_evidence` | participant | real tasks finished, named |
| `friction` | participant | what got in the way, however small. This is the column the program exists to collect |
| `disposition` | owner | starts `untriaged`; must move to `fixed` / `deferred` / `wont-fix` / `duplicate` before the stage can promote |
| `disposition_ref` | owner | the PR / issue / commit / doc recording the call |

`deferred` is a legitimate, recorded answer. Silence is not.

### What each participant does

Send them exactly this:

```sh
# 1. install the build you are being asked to try
uv tool install --reinstall git+https://github.com/michaeljabbour/amplifier-app-tui

# 2. confirm what you got, and tell the owner this sha
amplifier-tui version

# 3. use it as your primary Amplifier interface for at least one full working day
amplifier-tui

# 4. if you need your transcript before switching back, export it first
#    (in-app: /export writes markdown to exports/)

# 5. to go back to the CLI at any time — nothing to uninstall, settings carry over
amplifier
```

Then report three things to the stage owner: **the sha they ran**, **the real tasks they
completed**, and **everything that got in their way**. Participants do not run the smoke and
do not edit the ledger — the owner records the stage.

### Consolidating

Every seat needs a name, a commit, completion evidence, and a disposition off `untriaged`.
Release-blocking findings also get their own `blockers.tsv` row — that is what actually
gates.

```sh
python3 scripts/adoption_gate.py promote 3
```

The gate names each unfilled seat individually, so "who is still missing?" is never a
guess.

---

## Stage 4 — team-wide default

**Owner: MJ Jabbour.**

S4-entry requires the rollback path to have been **walked**, not read. The mechanical half
is automated:

```sh
python3 scripts/adoption_gate.py rollback
```

It verifies the documented commands are well-formed, that the pinned-build command points at
this app's own repository and pins the ledger's `tested_commit` column, that every recorded
`tested_commit` is a real commit, that `pyproject.toml` declares only `amplifier-tui` (so
both executables install side by side — ADR-0008), that there is no dependency tie between
the two apps, and that nothing in `src/` or `scripts/` executes a `uv` command that would
disturb the CLI's install.

It then prints, every time, the part it did **not** do. A human still has to:

- run `amplifier` after `amplifier-tui` on a real machine and confirm both launch;
- confirm `~/.amplifier/` settings and keys carry over in both directions;
- confirm amplifier-app-cli stayed installed and usable for the **whole** stage-4 window.

Cite that drill — the date and who ran it — in `entry_evidence`. A green `rollback` run is
not a drill.

---

## Stage 5 — replacement

**Owner: MJ Jabbour.**

```sh
python3 scripts/adoption_gate.py promote 4
```

That command *is* the replacement gate. Exit 0 is the only thing that authorizes retiring
amplifier-app-cli, and it cannot return 0 while any `release-blocking` row is open.

Promotion through stage 5 marks app-cli **deprecated**, not removed. Removal is a separate,
separately-announced decision — see [Rollout messaging](README.md#rollout-messaging).

---

## What each stage owner must decide

The gate answers "may this be promoted?" It never answers "should it be?" That judgment is
the owner's, it is recorded in the `decision` column, and it is the reason the tool is
read-only.

| Stage | Owner | The decision, stated plainly |
|---|---|---|
| 1 | MJ Jabbour | *Would I keep using this tomorrow if nobody were watching?* Promote only if the answer is yes on real work. `held` if the day was fine but you are not convinced. |
| 2 | Brian Krabach | Same question, independently. Stage 2 is not a rubber stamp on stage 1 — a second daily driver exists precisely to catch what the first one had learned to work around. |
| 3 | MJ Jabbour | *Is every seat's friction genuinely dispositioned?* Each report gets `fixed`, `deferred`, `wont-fix`, or `duplicate` with a reference. Deferring is allowed; leaving it `untriaged` is not. |
| 4 | MJ Jabbour | *Can the whole team default to this and still get out?* Requires the rollback drill walked and amplifier-app-cli demonstrably usable throughout. If anyone was told "just use the TUI, the CLI is gone," stage 4 failed and re-runs. |
| 5 | MJ Jabbour | *Is deprecating amplifier-app-cli the right call now?* `promote 4` clearing is necessary, not sufficient. Deprecated is not removed; do not let the promotion imply it. |

Any owner may record `held` or `rolled-back` on a clear gate. That is the point of having a
person in the loop.

---

## When something goes wrong mid-stage

```sh
# roll back to the CLI — separate binaries, shared ~/.amplifier/, nothing to uninstall
amplifier

# roll back to the last known-good TUI build
uv tool install --force git+https://github.com/michaeljabbour/amplifier-app-tui@<tested_commit>
```

Then **record it**. File the cause in `blockers.tsv` as `release-blocking`/`open` and set the
stage's `decision` to `rolled-back`. The gate will refuse to promote anything until that row
is resolved — which is the correct behavior, not an obstacle.

---

## The state today

```
stage  owner                              decision  window  tested_commit
1      MJ Jabbour                         pending   0/1d    -
2      Brian Krabach                      pending   0/1d    -
3      stage-3 seats (see feedback.tsv)   pending   0/1d    -
4      MJ Jabbour                         pending   0/1d    -
5      MJ Jabbour                         pending   0/0d    -
stage-3 seats unfilled: seat-1, seat-2, seat-3
open release-blockers: none
```

**What is missing before stage 1 can be promoted** — and it is only this:

1. MJ installs a build and records its `tested_commit` and `start_date`.
2. A green `scripts/adoption_smoke.sh` at that commit, cited as `entry_evidence`.
3. At least one full working day of MJ using it as his primary interface.
4. Every defect found filed in `blockers.tsv`, with an honest severity.
5. `exit_evidence` naming the real tasks completed, and MJ's recorded `decision`.

None of that can be produced by tooling, and none of it has been invented here.
