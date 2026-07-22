# Design / Decision — Self-improvement loop over skills & harness (SkillOpt discipline, AIDE² safeguards)

**Issue:** [#50](https://github.com/michaeljabbour/amplifier-app-newtui/issues/50) — *Self-improvement loop over skills/harness*
**Companion:** [#49](https://github.com/michaeljabbour/amplifier-app-newtui/issues/49) — *Forge-driven capability test tier* (the rollout/eval substrate)
**Status:** Proposed — design only, no code lands with this doc.
**Evidence rev:** `ac854ef` (read-only checkout of `origin/main` at `/Users/michaeljabbour/dev/newtui-wt/base`).

> When this doc is accepted it should be promoted to the repo plans convention:
> `docs/plans/2026-07-22-self-improvement-loop.md`, with a **Status** banner, and
> registered in `docs/plans/README.md` (see `docs/plans/README.md:1-14`). It is
> written here first per the backlog-attractor pipeline contract.

---

## Problem

Issue #50 asks us to design the **rollout → reflect → validate → deploy** loop that lets
amplifier-app-newtui improve *its own* skills and harness, using the forge capability tier
(#49) as the evaluation substrate. Concretely the deliverable must pin down four things:

1. **What is trainable** — skill docs (SkillOpt-style), harness/prompt scaffolding
   (AIDE²-style), or both — and *in what order*.
2. **The eval** — forge capability tier as rollout environment; scoring; public/private
   split; budget cap.
3. **The gate** — held-out validation, never-regress acceptance, and a human checkpoint for
   harness-level edits.
4. **The first concrete target** — one real amplifier skill through a SkillOpt-style loop with
   the forge tier as judge.

Two facts shape the whole design and are easy to get wrong:

- **This is NOT the existing `/improve` command.** The repo already ships an `/improve`
  surface, but it is an *in-session config proposer* that mines the approval ledger + denial
  log and **"proposes and never applies silently"** — it does not optimize skill text
  (`src/amplifier_app_newtui/commands/improve.py:1-19`). The self-improvement loop in #50 is a
  distinct, *offline* optimizer that rewrites a skill document and validates the rewrite on
  held-out tasks. The doc must keep these separate so nobody wires them together.
- **The thing we optimize (a skill doc) lives outside the app's import graph**, and the
  optimizer must too — the architecture forbids leaking optimizer machinery into `kernel/` or
  `ui/` (ADR-0007 layering, `docs/decisions/ADR-0007-newtui-ground-up-architecture.md:15-19`).

---

## Evidence (verified `file:line`, rev `ac854ef`)

**The backlog driver and its architectural rubric**
- `docs/BACKLOG.md:7-9` — the rubric every item must honor: *"pure renderer transforms,
  golden-tested in the same commit, kernel never imports Textual, UI never touches
  amplifier-core."* The loop's tooling must not violate this.
- **Discrepancy to flag honestly:** both #50 and #49 cite *"docs/BACKLOG.md §7"* as the home of
  the rollout→reflect→validate→deploy item, but on `origin/main` §7 is
  **"## 7. Smaller wins"** (`docs/BACKLOG.md:115-120`) — markdown-rendering polish, not a
  self-improvement loop. The loop is therefore **net-new**: there is no backlog line item to
  build against beyond the two issues. This design *is* the missing specification.

**Architecture constraints the optimizer must respect**
- `docs/decisions/ADR-0007-newtui-ground-up-architecture.md:15-19` — enforced layering
  `ui/ → model/ → kernel/ → amplifier-core/foundation`; *"kernel/ never imports Textual.
  model/ imports neither Textual nor amplifier-core."* An import-linter contract enforces it.
- `docs/decisions/ADR-0007-newtui-ground-up-architecture.md:19` — `ui/app.py` is a
  composition root with a hard <500-line budget. Any harness-level (AIDE²-style) edit that
  touches app code is high-blast-radius and contract-governed.

**The existing `/improve` command — what it is and is NOT**
- `src/amplifier_app_newtui/commands/improve.py:1-19` — `/improve` mines two evidence streams
  (allowlist candidates from `N/N` approvals; trust-slot suggestions from overridden denials)
  and *"proposes and never applies silently."*
- `src/amplifier_app_newtui/commands/improve.py:196-213` — `improve_proposals(...)`; the
  `ledger` arg is *"reserved: spend-vs-yield proposals are not spec'd yet"* — i.e. no
  optimization loop exists today.
- `src/amplifier_app_newtui/commands/builtin.py:442-447` — the `CommandSpec` registers
  `/improve` as *"tune config from ledger + denial log"*, tag `skill`.
- `tests/test_commands_improve.py:1-64` — the command is tested as **pure data-in/data-out**
  (tallies/overrides → proposals). This is the testing discipline the loop's pure functions
  should copy.

**Where skills actually come from (the trainable artifact is external)**
- `bundle.md:56-61` — `tool-skills` is mounted with sources
  `git+https://…/amplifier-foundation@main#subdirectory=skills` **and** `~/.amplifier/skills`.
  So the skill docs newtui loads are (a) upstream foundation skills and (b) user-dir skills —
  **none are vendored in this repo tree** (`find . -iname SKILL.md` under `src/` returns
  nothing). Consequence: a `deploy` step writes to `~/.amplifier/skills/<skill>/SKILL.md`,
  which wins over the foundation copy by mount priority — an override, not an upstream edit.

**The eval substrate (#49) and how scores get produced**
- Issue #49 body — a **forge-driven capability tier** drives the real TUI through a PTY via
  `amplifier-skill-forge`, asserting boot→composer, `/status`, `/model`, a full `--demo` turn,
  fan-out lanes, and resume re-seed; *"Every capability assertion here becomes a fitness signal
  there [#50]."* Runnable via `uv run pytest -m forge` or a script; **flake-resistant with
  bounded waits on ledger/screen state, not sleeps**; kept out of the default CI gate initially.
- The `amplifier-skill-forge` skill (mounted this session) is the PTY daemon that boots and
  drives amplifier itself — the mechanism #49 wraps.

**Budget accounting already exists (wire the cap to it)**
- `docs/BACKLOG.md:53-67` — `kernel/cost.py` ships `estimate_cost()`, `CostTracker`,
  `cost_of()` (provider-reported `cost_usd` authoritative), and `sum_prior_cost()` re-seed from
  `ui-events.jsonl`. Real per-run dollar cost is observable, so an AIDE²-style **fixed dollar
  budget cap** has a real signal to read; it does not need to be invented.

**Test & CI conventions the loop must fit**
- `pyproject.toml:50-53` — `[tool.pytest.ini_options]` defines `testpaths`, `--import-mode`,
  `asyncio_mode`, but **registers no custom markers** — so `-m forge` / `-m optimize` markers
  (#49 and this loop) must be added here.
- `docs/DEVELOPMENT.md:22-25` — CI runs `uv sync --frozen → ruff check → pyright src/ →
  pytest -q` with a **coverage floor of 85%**; perf/snapshot run uninstrumented. The loop must
  stay marker-gated and out of the default gate so it neither flakes CI nor dents coverage.
- `docs/DEVELOPMENT.md:50-59` — renderer output is locked by plain-text goldens at widths
  **40/80/97/120** (`tests/goldens/`); *"A golden diff you can't explain is a regression."* This
  is a ready-made, deterministic fitness signal for the first target (below).

**Repo convention for self-improving workflows (where the loop should live)**
- `pipelines/` (untracked working tree) already holds attractor pipelines as `*.dot` + a
  `*.bundle.md` companion + a `ledger.py`/`ledger.tsv` (e.g. `pipelines/backlog.dot`,
  `pipelines/gene-transfer.dot`). The `amplifier-tool-leverage-patterns` skill names this the
  standard "attractor-only" home. The rollout→reflect→validate→deploy loop is itself a DAG —
  it belongs here as `pipelines/self-improve.dot`, **not** inside the app.

---

## Prior art (summarized from the issue body only — no external citations fabricated)

Per the task constraint, these are summarized strictly from what issue #50's *References*
section itself states:

- **Weco AIDE²** — described in the issue as the *"first claimed net-positive recursive
  self-improvement"*: an outer-loop agent rewrites the inner agent's code across ~100 unattended
  iterations, producing successive improved versions that beat a hand-tuned baseline on external
  benchmarks and cut reward hacking 63%→34%. The issue calls out four **safeguards to copy**:
  **(1)** public/private score split (optimize on visible, survive on hidden), **(2)** a fixed
  dollar budget, **(3)** a heterogeneous task collection, and **(4)** first/second-order
  generalization tests. The issue also notes it did *not* reach "ignition."
- **microsoft/SkillOpt** — described in the issue as treating *the skill document as the
  trainable parameter for frozen models*: **rollout** (scored trajectories) → **reflect**
  (bounded add/delete/replace edits by an optimizer model) → **validate** (*accept only on
  strict held-out improvement*) → **deploy** a static `best_skill.md`. The issue notes it
  supports a Claude Code CLI harness — i.e. it is directly applicable to our skills.
- **"Self-Improvements in Modern Agentic Systems: A Survey"** — the issue positions this as the
  *vocabulary / design-space map* (prompt/framework refinement, in-operation skill libraries,
  autonomous heuristic optimization). Used here only as taxonomy.

Design consequence: **SkillOpt gives us the loop shape** (rollout→reflect→validate→deploy on a
skill doc); **AIDE² gives us the four safeguards** we bolt on so the loop can't fool itself.

---

## Options considered

### Option A — SkillOpt-first: optimize skill docs only, harness frozen (recommended core)
Run the loop over a single skill *document* as the trainable parameter; the inner agent
(harness, app code, prompts) is **frozen**. Deploy is a static `best_skill.md` written to
`~/.amplifier/skills/<skill>/SKILL.md` (override by mount priority, `bundle.md:56-61`).

- **Pros:** Directly mirrors the SkillOpt discipline the issue endorses. **Zero edits to
  app/kernel/ui** — respects ADR-0007's contract with nothing to enforce
  (`ADR-0007…:15-19`). Deploy is a single reversible file write; rollback = delete the override.
  Low blast radius; auto-deployable without a human in the loop once the gate is green.
- **Cons:** Bounded ceiling — you can only improve what a skill doc can express; wins on the
  scaffolding/harness itself are out of reach. Requires #49 to exist to produce fitness.

### Option B — AIDE²-first: optimize harness/prompt scaffolding with an outer-loop agent
Let an outer agent rewrite the *harness* — system-prompt overlays, governance posture, the
model-side rendering contract, even renderer code — as AIDE² does to its inner agent.

- **Pros:** Higher ceiling; can attack the scaffolding that skill text can't reach; closest to
  the AIDE² result the issue cites.
- **Cons:** **High blast radius and contract-governed.** Edits land in import-linter-contracted
  code with a golden matrix and a <500-line app budget (`ADR-0007…:15-19`;
  `docs/DEVELOPMENT.md:50-59`). Auto-merging model-authored harness edits is unsafe; each needs
  a human checkpoint, so throughput collapses. Larger budget, more reward-hacking surface. Wrong
  place to *start*.

### Option C — Hybrid, phased: skills-first now, harness-scaffolding later behind a human gate
Do Option A first (auto-deployable, low risk), then unlock Option B **later**, strictly behind a
mandatory human checkpoint, once the eval substrate and gate have earned trust on skills.

- **Pros:** Answers the issue's *"both — and in what order"* explicitly: **skills first, harness
  second.** Front-loads the safe, measurable wins; defers the dangerous surface until the
  scoring/gate machinery is proven. Each phase is independently shippable.
- **Cons:** Two loop modes to maintain long-term; the harness phase stays human-bottlenecked by
  design (that's the point, not a defect).

---

## Decision / Recommendation

**Adopt Option C (phased hybrid), starting with Option A.**

1. **What is trainable, and in what order:** **skill docs first** (SkillOpt-style, frozen
   harness, auto-deployable), then **harness/prompt scaffolding second** (AIDE²-style,
   human-gated). Never harness-first.
2. **The eval:** the **forge capability tier (#49)** is the rollout environment. Each capability
   assertion is a fitness signal, aggregated to a scalar `score = w_pass·(checks passed / total)
   − w_cost·(dollars / budget) − w_latency·(turns over budget)`. The task collection is split
   **public / private**: the reflect step sees only public scores; the private held-out set is
   revealed *only* at the validate gate (AIDE² safeguard #1). A **fixed dollar budget cap** is
   read from real `CostTracker` spend (`docs/BACKLOG.md:53-67`) and halts the run (safeguard #2).
   The task set is deliberately **heterogeneous** (safeguard #3).
3. **The gate:** accept a candidate **only** on strict improvement on the *private held-out* set
   **and** a **never-regress** invariant — no held-out capability that passed before may fail
   after (SkillOpt's "accept only on held-out improvement," hardened with AIDE² generalization
   tests, safeguard #4). **Harness-level edits require an explicit human checkpoint before
   deploy**, on top of passing the same gate.
4. **First concrete target:** the **terminal-output-contract skill** — the model-side rendering
   contract described in `docs/BACKLOG.md:103-113` (answer-first, terminal-friendly markdown
   subset: no images, tables ≤4 columns, shallow lists, fenced code with language tags). It is
   **newtui-owned** (not a shared foundation skill, so optimizing it can't leak into other
   harnesses), and its fitness is **deterministically scoreable** by the forge tier plus the
   existing golden width matrix (`docs/DEVELOPMENT.md:50-59`) — the ideal low-risk pilot.

> ★ **Insight:** the safest first target isn't the most impressive skill — it's the one whose
> "did it get better?" question already has a deterministic answer in the repo. The golden width
> matrix (40/80/97/120) turns "renders well in a terminal" into a diff you can score without a
> judge model, so the pilot's fitness can't be gamed by a persuasive rewrite.

---

## Implementation plan (phased, concrete file paths)

All new machinery lives **outside** `src/amplifier_app_newtui/` (ADR-0007) — in `pipelines/`,
matching the existing attractor convention (`pipelines/backlog.dot`, `pipelines/gene-transfer.dot`).

### Phase 0 — Land the spec + wiring (no dependency on #49)
- Promote this doc to `docs/plans/2026-07-22-self-improvement-loop.md` with a Status banner; add
  its row to `docs/plans/README.md`.
- Register pytest markers in `pyproject.toml:50-53`:
  ```toml
  markers = [
      "forge: end-to-end capability tier via the forge PTY daemon (issue #49)",
      "optimize: self-improvement loop runs; live agents + budget; never in the default gate",
  ]
  ```
- Add `pipelines/self-improve.dot` + `pipelines/self-improve.bundle.md` skeletons (nodes:
  `rollout → reflect → validate → deploy`) and a `pipelines/self-improve/ledger.tsv` for run
  provenance, mirroring `pipelines/ledger.py`/`ledger.tsv`.

### Phase 1 — Rollout harness (**blocked-by #49**)
- `pipelines/self_improve/rollout.py` — pure orchestration: run the candidate skill through the
  forge suite (#49), collect per-capability pass/fail + `CostTracker` dollars + turn counts,
  aggregate to the scalar `score`. **No app imports**; consumes forge results as data.
- `pipelines/self_improve/tasks.py` — the heterogeneous task collection with an explicit,
  version-pinned **public/private split** (e.g. `tasks_public.tsv` / `tasks_private.tsv`); the
  private file is never passed to `reflect`.
- `pipelines/self_improve/budget.py` — reads real spend via the same accounting `CostTracker`
  exposes; raises `BudgetExhausted` at the cap.

### Phase 2 — Reflect step
- `pipelines/self_improve/reflect.py` — call an optimizer model with **only public scores +
  rollout transcripts**; constrain it to **bounded add/delete/replace edits** to the skill doc
  (SkillOpt discipline); emit a candidate `SKILL.md`. Diff size is capped per iteration.

### Phase 3 — Validate gate + deploy
- `pipelines/self_improve/validate.py` — pure function
  `accept(before_scores, after_scores) -> bool`: require **strict improvement on the private
  held-out set** AND **never-regress** (no previously-passing held-out check now failing). Unit-
  tested exactly like `improve.py` is (`tests/test_commands_improve.py:1-64`).
- `pipelines/self_improve/deploy.py` — on accept, atomically write the winner to
  `~/.amplifier/skills/<skill>/SKILL.md` (override per `bundle.md:56-61`), keep the prior version
  for one-command rollback, and append `{skill, iter, before, after, dollars, git_sha}` to
  `pipelines/self-improve/ledger.tsv`. **This ledger append is where "before/after scores
  recorded" (issue Acceptance) is satisfied.**

### Phase 4 — First optimization run (the acceptance-closing run)
- Target the terminal-output-contract skill (`docs/BACKLOG.md:103-113`). Seed = current contract
  text; fitness = forge rendering checks + golden width matrix. Execute one bounded run under a
  fixed budget; record before/after in the ledger; write a short results note back into this plan
  ("Run 1" appendix).

### Phase 5 — Harness scaffolding loop (later; **human-gated**)
- `pipelines/self-improve-harness.dot` — same shape, but the reflect target is prompt/harness
  scaffolding, and `deploy` is replaced by **`propose`**: open a PR / print a diff for a human,
  never auto-write. Gate additionally requires import-linter + `pyright src/` + the golden matrix
  to pass (`docs/DEVELOPMENT.md:22-25,50-59`).

---

## Test & validation strategy

- **Pure functions, offline, in the default gate.** `validate.accept()`, the public/private
  splitter, the score aggregator, and `budget` accounting are pure data-in/data-out and unit-
  tested with synthetic score fixtures — the exact discipline `tests/test_commands_improve.py`
  uses. These *do* run in `pytest -q` and count toward the 85% floor (`docs/DEVELOPMENT.md:22-25`).
- **The loop itself is marker-gated (`-m optimize`) and excluded from the default gate**, like
  the forge tier (#49), because it needs live agents, a PTY, and real budget. This keeps CI fast,
  green, and coverage-stable.
- **Gate self-tests (the safety-critical ones):**
  - a candidate that improves the *public* set but regresses the *private* set is **rejected**;
  - a candidate that adds a new held-out failure is **rejected** even if total score rises
    (never-regress);
  - the run **halts** at the dollar cap with a partial, recorded result (no silent overrun).
- **Generalization tests** (AIDE² safeguard #4): after accept, re-score on a *third*, untouched
  task slice; a large public↔held-out gap flags overfitting and blocks deploy.
- **Determinism for the pilot:** the terminal-output-contract target is validated against the
  frozen goldens at 40/80/97/120 — an intentional change must land its golden update in the same
  step (`docs/DEVELOPMENT.md:50-59`), so a "win" that mangles rendering is caught mechanically.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| **Reward hacking / overfitting to visible tasks** (the AIDE² failure mode). | Public/private split — reflect never sees held-out scores; validate gates on held-out only; plus a third-slice generalization check. |
| **Optimizing a shared foundation skill leaks into other harnesses.** | First target is **newtui-owned** and deploy writes a **user-dir override** (`~/.amplifier/skills/…`, `bundle.md:56-61`); foundation upstream is never edited in place. |
| **Harness edits violate the import-linter / renderer-purity contract** (`ADR-0007…:15-19`, `docs/BACKLOG.md:7-9`). | Harness loop (Phase 5) is human-gated and cannot deploy; its gate additionally requires import-linter, pyright, and the golden matrix to pass. |
| **Cost runaway from unattended iteration.** | Fixed dollar budget read from real `CostTracker` spend (`docs/BACKLOG.md:53-67`); `BudgetExhausted` halts and records a partial run. |
| **Confusion with the in-session `/improve` command.** | Explicitly scoped as separate surfaces: `/improve` proposes config from the ledger and never applies (`improve.py:1-19`); this loop is offline skill-doc optimization. Different artifact, different code home (`pipelines/`). |
| **Flaky fitness from a live PTY.** | Inherit #49's discipline: bounded waits on ledger/screen state, not sleeps; run N rollouts per candidate and take the median. |
| **#49 not done yet.** | Phases 0 (spec + markers + skeleton) are independent and land now; Phases 1–4 are `blocked-by #49`. Stated in the plan; no false readiness. |

---

## Acceptance mapping

Issue #50's explicit **Acceptance** is terse — *"Design doc merged; first optimization run
executed with before/after scores recorded"* — so the real contract is that line **plus** the
four numbered **Deliverable** items. Each mapped to where it is satisfied:

| Contract item (issue #50) | Where addressed |
|---|---|
| **Deliverable 1 — what is trainable (skills / harness / both) and in what order** | *Decision* §1 + *Options* A/B/C: **skill docs first (frozen harness), harness scaffolding second, human-gated.** |
| **Deliverable 2 — the eval: forge tier as rollout env; scoring; public/private split; budget cap** | *Decision* §2 + *Impl* Phase 1: forge (#49) as rollout, scalar `score` formula, versioned public/private split (`tasks.py`), dollar cap wired to `CostTracker` (`docs/BACKLOG.md:53-67`). |
| **Deliverable 3 — the gate: held-out validation, never-regress, human checkpoint for harness edits** | *Decision* §3 + *Impl* Phase 3/5: `validate.accept()` = strict held-out improvement **and** never-regress; harness edits gated by an explicit human checkpoint + contract checks. |
| **Deliverable 4 — first concrete target (a skill via SkillOpt-style loop, forge as judge)** | *Decision* §4 + *Impl* Phase 4: the **terminal-output-contract skill** (`docs/BACKLOG.md:103-113`), scored by forge + the golden width matrix. |
| **Acceptance — "Design doc merged"** | This document; Phase 0 promotes it to `docs/plans/2026-07-22-self-improvement-loop.md` and registers it in `docs/plans/README.md:1-14`. |
| **Acceptance — "first optimization run executed with before/after scores recorded"** | *Specified but not executed here.* Phase 4 runs it; Phase 3 `deploy.py` records before/after to `pipelines/self-improve/ledger.tsv`. **The run itself cannot be performed by this read-only, code-free deliverable and is blocked-by #49** (no forge tier = no fitness signal). This is the one acceptance clause the doc cannot itself close; it is closed by executing Phase 4 after #49 lands. |

**Self-review result:** every Deliverable item and the "design doc merged" clause are addressed
with verified `file:line` evidence. The single clause this doc cannot itself satisfy — *executing*
the first run — is disclosed explicitly (it requires #49 and running code, both out of scope for a
design deliverable), and the doc pins down exactly where and how those before/after scores get
recorded so the run is turnkey once #49 exists.
