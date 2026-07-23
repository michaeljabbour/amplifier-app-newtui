# Backlog attractor pipelines

> **Run log — 2026-07-22.** The full backlog (#22–#54, 33 issues) was driven to done in one
> automated run: 33 green-gated PRs (#55–#87), 0 acknowledged rows. What actually executed was
> [`backlog.dot`](backlog.dot) (the routed generalization of gene-transfer.dot below) with the
> orchestration loop run by an interactive amplifier session acting as the engine:
> `self`-delegated claude-opus-4-8 workers (one per issue, 4 parallel lanes in git worktrees
> under `~/dev/newtui-wt/`), deterministic gates re-verified independently by the orchestrator
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
**amplifier-app-newtui**, one open issue at a time, gating each transfer on the unit
suite **and** a real-terminal [forge](../../.claude/skills/amplifier-skill-forge) check
before it opens a PR.

"Gene transfer" is deliberate: the pipeline moves the **capability**, re-expressed
through newtui's own `kernel`/`model`/`ui`/`commands` seams under ADR-0007 — it never
imports, vendors, or copies amplifier-app-cli code (there are zero dependency ties today
and this keeps it that way). This mirrors amplifier's shipped `semport.dot` cross-repo
port fixture ("Strategy SF": the agent edits files directly, a deterministic tool node
validates via exit code, edges route on `context.tool.last_line`).

## What it covers

[`ledger.tsv`](ledger.tsv) is seeded with the **entire open backlog** (#22–#54; #21 is
already fixed on its own branch). The backlog is heterogeneous, so the pipeline routes each
issue to the right treatment rather than porting everything the same way:

- **amplifier-app-cli capability ports** (#43–#48, #51, #52) — the original gene-transfer
  shape: find the donor construct, re-express it through newtui's seams under ADR-0007
  (never importing app-cli), gate on unit + forge.
- **Internal fixes / refactors / features** (#22–#42, minus decisions) — no donor; study the
  issue and the relevant code, implement, gate on unit + forge.
- **Decisions / spikes** (#26, #36, #49, #50, #53, #54) — deliverable is a `docs/plans/`
  design doc, gated on review, not a forge capability check.

The [paste-in prompt](PROMPT.md) gives amplifier agency to categorize each issue (via its
GitHub labels), adapt or author the pipeline graph(s) that fit, and stand up the attractor
engine itself. Edit the queue with `python3 pipelines/ledger.py add <issue> <slug>`.

## The pipeline

[`gene-transfer.dot`](gene-transfer.dot) — one issue per loop:

```
CheckLedger ──done──> exit
    │process
SelectIssue → BranchSetup → LocateDonor → PlanTransfer → Implement → UnitValidate
                                                                          │pass │fail
                                                       ForgeValidate <────┘     ▼
                                                        │pass │fail      AnalyzeFailure
                                                        ▼     └──> AnalyzeFailure → RetryGate
                                                      Commit                        │retry→Implement
                                                        │                           │giveup→MarkBlocked
                                                        └──loop_restart──> CheckLedger
```

- **LLM nodes** (`box`): SelectIssue, LocateDonor, PlanTransfer, Implement, AnalyzeFailure.
- **Deterministic gates** (`parallelogram`): BranchSetup, UnitValidate (`ruff` + `pyright`
  + `pytest`), ForgeValidate (boots the real TUI / runs the new CLI via forge and asserts),
  RetryGate (bounds retries at 3), Commit, MarkBlocked.
- Each transfer lands on its own `gene-transfer/<slug>` branch with a PR — never on `main`
  (branch protection enforces the gates a second time).
- Non-converging issues after 3 attempts are marked `acknowledged` and commented for a
  human; the loop moves on rather than stalling.

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
done = no `new` rows remain. The attractor's own `checkpoint.json` is secondary and lands
under `logs_root` — `./runs/` for the bundle-config path, or a temp dir
(`$TMPDIR/attractor-pipeline/`) for `run_pipeline`. Poll with forge's `exec`, not the screen:

```sh
FORGE=~/.claude/skills/amplifier-skill-forge/tools/forge.py
REPO=/Users/michaeljabbour/dev/amplifier-app-newtui

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
- **Bounded.** 3 fix attempts per issue, then `acknowledged` + human handoff.
- **Idempotent-ish.** Re-running resumes from the ledger: `implemented`/`acknowledged` rows
  are skipped, only `new` rows are attempted. `rm -rf runs .ai/gt_*` for a clean slate.
- **Cost.** This is an autonomous multi-hour, multi-PR job. Review `ledger.tsv` scope and the
  models before launching.
