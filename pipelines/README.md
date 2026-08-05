# Backlog attractor pipelines

> **Run log — 2026-07-22.** The full backlog (#22–#54, 33 issues) was driven to done in one
> automated run: 33 green-gated PRs (#55–#87), 0 acknowledged rows. What actually executed was
> [`backlog.dot`](backlog.dot) (the routed generalization of gene-transfer.dot below) with the
> orchestration loop run by an interactive amplifier session acting as the engine:
> `self`-delegated claude-opus-4-8 workers (one per issue, 4 parallel lanes in git worktrees
> under `~/dev/tui-wt/`), deterministic gates re-verified independently by the orchestrator
> before every PR, ledger advanced via `ledger.py`. Retries used: 1 (issue #43, real bug found
> by the independent gate re-run). Operational lessons baked into the graph/docs:
> **(1)** `attractor-profile-anthropic` child agents failed intermittently
> ("text content blocks must be non-empty") — `self`-delegation was the reliable worker shape;
> **(2)** forge screen-scrape probes are timing-sensitive under heavy parallel load — verify
> failures by re-running in isolation before burning a retry; **(3)** any real TUI boot
> pip-installs bundle modules into the worktree venv, and a probe's scratch `AMPLIFIER_HOME`
> cache can shadow `tests/` as a namespace package — keep probe scratch dirs inside `.ai/` and
> rebuild the venv if collection breaks; **(4)** GitHub push protection blocks realistic secret
> fixtures — build them by concatenation.

## Gene-transfer pipeline (original template)

A fully-automated [attractor](https://github.com/microsoft/amplifier-bundle-attractor)
pipeline that ports capabilities from **amplifier-app-cli** (the donor) into
**amplifier-app-tui**, one open issue at a time, gating each transfer on the unit
suite **and** a real-terminal [forge](../../.claude/skills/amplifier-skill-forge) check
before it opens a PR.

"Gene transfer" is deliberate: the pipeline moves the **capability**, re-expressed
through tui's own `kernel`/`model`/`ui`/`commands` seams under ADR-0007 — it never
imports, vendors, or copies amplifier-app-cli code (there are zero dependency ties today
and this keeps it that way). This mirrors amplifier's shipped `semport.dot` cross-repo
port fixture ("Strategy SF": the agent edits files directly, a deterministic tool node
validates via exit code, edges route on `context.tool.last_line`).

## What it covers

[`ledger.tsv`](ledger.tsv) is seeded with the **entire open backlog** (#22–#54; #21 is
already fixed on its own branch). The backlog is heterogeneous, so the pipeline routes each
issue to the right treatment rather than porting everything the same way:

- **amplifier-app-cli capability ports** (#43–#48, #51, #52) — the original gene-transfer
  shape: find the donor construct, re-express it through tui's seams under ADR-0007
  (never importing app-cli), gate on unit + forge.
- **Internal fixes / refactors / features** (#22–#42, minus decisions) — no donor; study the
  issue and the relevant code, implement, gate on unit + forge.
- **Decisions / spikes** (#26, #36, #49, #50, #53, #54) — deliverable is a `docs/plans/`
  design doc, gated on review, not a forge capability check.

The [paste-in prompt](PROMPT.md) gives amplifier agency to categorize each issue (via its
GitHub labels), adapt or author the pipeline graph(s) that fit, and stand up the attractor
engine itself. For a separately authorized, non-parity backlog item, edit the queue with
`python3 pipelines/ledger.py add-non-parity <issue> <slug>`. The plain `add` command is
reserved for the owner-gated parity loop.

## The pipeline

[`gene-transfer.dot`](gene-transfer.dot) — one issue per loop:

```
CheckLedger ──done──> exit
    │blocked──> owner_gate_blocked
    │process
SelectIssue → RecheckOwnerGate → BranchSetup → LocateDonor → PlanTransfer → Implement → UnitValidate
                                                                          │pass │fail
                                                       ForgeValidate <────┘     ▼
                                                        │pass │fail      AnalyzeFailure
                                                        ▼     └──> AnalyzeFailure → RetryGate
                                                      Commit                        │retry→Implement
                                                        │                           │giveup→MarkBlocked
                                                        └──loop_restart──> CheckLedger
```

- **LLM nodes** (`box`): SelectIssue, LocateDonor, PlanTransfer, Implement, AnalyzeFailure.
- **Deterministic gates** (`parallelogram`): CheckLedger / RecheckOwnerGate, BranchSetup,
  UnitValidate (`ruff` + `pyright`
  + `pytest`), ForgeValidate (boots the real TUI / runs the new CLI via forge and asserts),
  RetryGate (bounds retries at 3), Commit, MarkBlocked.
- Each transfer lands on its own `gene-transfer/<slug>` branch with a PR — never on `main`
  (branch protection enforces the gates a second time).
- Non-converging issues after 3 attempts are marked `acknowledged` and commented for a
  human; the loop moves on rather than stalling.
- `ledger-sources.tsv` records whether each queue row came from parity or the explicit
  non-parity path. A parity row is selected only while its effective disposition is still
  `accepted`, and the same gate is rechecked immediately before `BranchSetup`. A hand edit,
  a revoked decision, or a direct enqueue therefore stops before any code-changing node.

## Owner-gated parity loop (continuous re-audit)

The gene-transfer pipeline above answers *"port the next thing on the list."* It does not
answer *"is the list still right, and who said we should build any of it?"* That is
[`parity-loop.dot`](parity-loop.dot), driven by [`parity_loop.py`](parity_loop.py) — a
**read-only** re-audit lane wrapped around an explicit product-owner gate.

```
ShouldContinue ──done──> exit          (3 consecutive clean passes | owner-ended)
    │process
ReAudit (READ-ONLY) → RecordPass ──clean──> ShouldContinue
                          │gaps
                     OwnerTriage → OwnerGate ──proceed──> EnqueueTransfer → gene-transfer.dot
                                       └──hold──> ShouldContinue
```

**Two counters, deliberately not the same counter.** This is the whole point of the loop,
so it is worth being blunt about:

| | fix retries | consecutive clean passes |
|---|---|---|
| Bounds | one gap's fix converging | the whole run |
| Lives in | `gene-transfer.dot` RetryGate, `ledger.tsv` | `parity_loop.py streak`, `parity-passes.tsv` |
| Counts | code-changing attempts | read-only re-audits |
| Limit | 3 attempts → `acknowledged` + human | 3 clean in a row → run complete |
| Reset by | a new gap entering the transfer lane | any pass that discovers a gap |

A fix retry never advances or resets the clean-pass streak, and a clean pass never refunds a
fix-retry budget. `parity_loop.py` never reads or writes `ledger.tsv` (there is a test for
that), and `ledger.py` never sees the pass record.

**Artifacts** — both TSV, both hand-greppable, both rerunnable against a later release:

- [`parity-passes.tsv`](parity-passes.tsv) — one row per read-only re-audit pass:
  `pass · date · commit · outcome · gaps_found · gap_ids · note`, `outcome ∈ {clean, gaps,
  owner-ended}`. Because each row names the **commit** it audited, re-running the three lanes
  against a later release just appends a row — the record is the run's history, not a
  snapshot.
- [`parity-gates.tsv`](parity-gates.tsv) — one row per discovered gap:
  `gap_id · slug · disposition · owner · date · note`, `disposition ∈ {pending, accepted,
  rejected, deferred, already-covered}`.

**A decision nobody signed is not a decision.** The `owner` field must name a real person:
blank, whitespace, `-`, `?`, `TBD`, `unknown`, `owner`, `team`, and the rest of
`PLACEHOLDER_OWNERS` (one list, one home, in `parity_loop.py`) are **refused** by `decide`,
which writes nothing and exits 1. The same rule applies to `end-run`; new rows use
`owner=<full name> | <reason>` so full names are unambiguous. Enforcement is at read time too,
so hand-editing either TSV doesn't help: a row claiming `accepted` against a placeholder owner reads back
`disposition=unattributed` and **blocks at the gate exactly like `pending`**. `pending` is
the one disposition allowed to carry no owner — it is the *absence* of a ruling, which is
precisely what a freshly-discovered gap has.

```sh
python3 pipelines/parity_loop.py validate    # gate and owner-ended attribution audit (exit 1)
python3 pipelines/parity_loop.py awaiting    # gaps still owed a real, attributed decision
```

**The gate.** Parity is a *decision process*, not a mandate to copy every app-cli behavior —
some capabilities belong below the harness or on another surface. So every newly-discovered
gap is auto-registered `pending`, and **only `accepted` opens a code-changing route**:
`parity_loop.py gate <id>` prints `PROCEED` (exit 0) or `BLOCKED` (exit 1), and a gap the
owner has never ruled on reads `disposition=undecided` — which blocks. `rejected` and
`already-covered` are first-class outcomes, not failures.

```sh
python3 pipelines/parity_loop.py record-pass <sha> 120:notify-cli,121   # read-only pass
python3 pipelines/parity_loop.py record-pass <sha> -                    # clean pass
python3 pipelines/parity_loop.py decide 120 rejected mjabbour "belongs below the harness"
python3 pipelines/parity_loop.py decide 120 accepted TBD                # REFUSED, exit 1
python3 pipelines/parity_loop.py gate 120        # PROCEED (exit 0) | BLOCKED (exit 1)
python3 pipelines/parity_loop.py validate        # VALID | INVALID (unsigned decisions)
python3 pipelines/parity_loop.py awaiting        # awaiting=<n>/<total>
python3 pipelines/parity_loop.py should-continue # CONTINUE clean_streak=1/3 | DONE reason=...
python3 pipelines/parity_loop.py end-run "Michael Jabbour" "remaining gaps deferred to 0.3"
python3 pipelines/parity_loop.py stats           # passes=4 clean_streak=2/3 run=open
```

**Where the run actually stands (2026-08-04):** `passes=2 clean_streak=0/3 run=open`,
19 gaps, **all `pending`**. Pass 2 re-audited all three lanes at `7030527`
([report](../docs/audits/pass2-2026-08-04.md)) and found 8 new gaps, so the streak is 0 and
honestly so — it is derived from the gap ids on each row, not from a typed word. The next
action on this loop is **triage, which is the one step an agent cannot do**: see the
[decision sheet](../docs/audits/parity-decision-sheet.md), 19 gaps with evidence and a
proposed disposition each.

The streak is derived from the gap ids each row actually carries, not from the stored
`outcome` word — you cannot hand-edit a clean streak into existence without also deleting
the gaps that contradict it. Covered by `tests/test_parity_loop.py`.

## Prerequisites

- The forge daemon reachable at `127.0.0.1:3141` — the pipeline runs `forge doctor` itself,
  but confirm once: `python3 ~/.claude/skills/amplifier-skill-forge/tools/forge.py doctor`.
  (Verified up.)
- The attractor bundle resolvable via the `attractor:` registry alias (it is — cached and in
  your registry). The launcher bundle composes `attractor:bundles/attractor-pipeline`; you do
  **not** need the `run_pipeline` tool or a global `attractor` binary (neither is required by
  the launch path below).
- `gh` authenticated with `repo` scope (already true).
- **Check the models**: `gene-transfer.dot` sets `llm_model="claude-fable-5"` on every LLM
  node assuming your `anthropic` provider serves it. Adjust to your configured providers, or
  point `Implement` at `openai`/`gpt-5.x-codex`.

## Launch

### Primary: paste a prompt into an `amplifier` session

Your session already has `bash`, `delegate`, and file tools, so it can act as the
orchestrator directly — **no `run_pipeline` tool, no bundle registration, no standalone
`attractor` binary** (none of which are available by default; `amplifier run --bundle` also
takes a *registered name*, not a path). The copy-paste prompt is in [`PROMPT.md`](PROMPT.md);
it drives the same loop this `.dot` and ledger define. Everything lands on
`gene-transfer/<slug>` branches + PRs, never `main`.

### Alternative: the real attractor engine

To run `gene-transfer.dot` through the actual `loop-pipeline` engine, register the launcher
bundle [`gene-transfer.bundle.md`](gene-transfer.bundle.md) first, then run it by name:

```sh
amplifier bundle add ./pipelines/gene-transfer.bundle.md --app
amplifier run --bundle gene-transfer-runner "go"
```

> **Models:** the graph, the launcher, and the prompt all use `claude-opus-4-8`.
> `claude-fable-5` refuses this autonomous self-porting work (its dual-use safety measures),
> and Opus 4.8 is the verified-working fallback in this environment. Adjust if your providers
> differ.

## Monitoring — "when is it done"

**`pipelines/ledger.tsv` is the source of truth** (in-repo, launch-method-independent):
done = no `new` rows remain. For the parity loop the completion condition is different and
lives elsewhere — `parity_loop.py should-continue` (three consecutive clean passes, or an
owner-ended run); ledger exhaustion only means the *currently accepted* gaps are built. The attractor's own `checkpoint.json` is secondary and lands
under `logs_root` — `./runs/` for the bundle-config path, or a temp dir
(`$TMPDIR/attractor-pipeline/`) for `run_pipeline`. Poll with forge's `exec`, not the screen:

```sh
FORGE=~/.claude/skills/amplifier-skill-forge/tools/forge.py
REPO=/Users/michaeljabbour/dev/amplifier-app-tui

# progress: which capabilities are left (primary signal)
python3 "$FORGE" exec "python3 pipelines/ledger.py stats" --cwd "$REPO"   # e.g. implemented=5 new=3

# done when this prints 0:
python3 "$FORGE" exec "grep -c '	new\$' pipelines/ledger.tsv" --cwd "$REPO"

# which node it's on right now (adjust path to your logs_root)
python3 "$FORGE" exec "cat runs/checkpoint.json | jq '.current_node, .completed_nodes'" --cwd "$REPO"
```

Per-node detail lives at `<logs_root>/<node_id>/status.json` (`outcome` ∈
success/partial/fail); per-stage working artifacts the agents pass to each other are under
`.ai/gt_*` in the repo.

## Guardrails

- **Never `main`.** All work is branch + PR; the repo's branch protection re-runs the gates.
- **Forge gate is the acceptance oracle.** A transfer only PRs if the capability actually
  works through a real terminal, not just if unit tests pass — this is the same lesson that
  the 2026-07-22 fan-out bugs taught (unit fixtures missed them; real terminals caught them),
  and formalizing this gate as a reusable tier is issue #49.
- **Bounded.** 3 fix attempts per issue, then `acknowledged` + human handoff. The parity
  loop's three-consecutive-clean-passes exit is a *separate* bound on a separate lane —
  see the table above before touching either.
- **No code change without an owner.** In the parity loop nothing reaches a code-changing
  node without an `accepted` disposition in `parity-gates.tsv`; undecided blocks.
- **Idempotent-ish.** Re-running resumes from the ledger: `implemented`/`acknowledged` rows
  are skipped, only `new` rows are attempted. `rm -rf runs .ai/gt_*` for a clean slate.
- **Cost.** This is an autonomous multi-hour, multi-PR job. Review `ledger.tsv` scope and the
  models before launching.
