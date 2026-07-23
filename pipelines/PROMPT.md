# Paste-in prompt

Paste the block below into an interactive `amplifier` session started from the repo root
(`/Users/michaeljabbour/dev/amplifier-app-newtui`). It hands amplifier the objective and the
guardrails and lets it decide how to stand up and run the pipeline.

---

**Objective.** Stand up an attractor pipeline that drives the *entire* open backlog of this
repository (amplifier-app-newtui) to done — every issue in `pipelines/ledger.tsv` (the open
GitHub issues #22–#54; #21 is already fixed on a separate branch). Then run it, fully
automated, to completion. You have broad agency over *how*; the constraints below are the only
hard rules.

**Where to start (not a script — orient, then decide).**
- `pipelines/gene-transfer.dot`, `pipelines/gene-transfer.bundle.md`, `pipelines/ledger.py`,
  and `pipelines/README.md` are a working starting point I built. The `.dot` is shaped for the
  amplifier-app-cli *capability ports*; treat it as a template, not gospel.
- The backlog is heterogeneous. Read each issue (`gh issue view <n> -R
  michaeljabbour/amplifier-app-newtui`; labels categorize them) and route it to the right
  treatment: app-cli capability ports, internal reliability/security fixes, refactors,
  rendering/feature work, and a few decision/spike issues whose deliverable is a design doc in
  `docs/plans/`, not code. Extend or author whatever pipeline graph(s) fit — you own the graph
  design and the ledger.
- Set up the attractor engine however works in your environment. The `run_pipeline` tool and a
  standalone `attractor` binary are NOT available by default, and `amplifier run --bundle`
  takes a *registered name*, not a path. Do whatever it takes: register the launcher bundle
  (`amplifier bundle add ./pipelines/gene-transfer.bundle.md --app`), compose
  `attractor:bundles/attractor-interactive` to get `run_pipeline`, or orchestrate the loop
  yourself with `delegate` + `bash`. Solve the setup problems; don't stop at the first blocker
  — investigate and route around it.
- Donor repo for the port issues (read-only reference): `/Users/michaeljabbour/dev/amplifier-app-cli`.

**Hard rules (non-negotiable).**
1. **Never commit to `main`.** One issue per branch (`auto/<slug>`), one PR each. Branch
   protection re-runs the gates.
2. **ADR-0007 layering.** Kernel is the only layer that imports amplifier-core/foundation;
   ui/ and commands/ never touch amplifier-core; pure renderers are golden-tested in the same
   commit. For port issues: NEVER import, vendor, or copy amplifier-app-cli — transfer the
   capability re-expressed through this repo's seams, reusing amplifier foundation/core
   primitives (amplifier-native first).
3. **Acceptance gates.** For code issues: `uv run ruff check . && uv run pyright src/ &&
   uv run pytest -q`, PLUS a real-terminal forge check that proves the behavior
   (`python3 ~/.claude/skills/amplifier-skill-forge/tools/forge.py doctor`, then drive the
   TUI / run the new CLI and assert the issue's Acceptance criteria). For decision/spike
   issues: the deliverable is the design doc; gate it on existing + self-review. A PR opens
   only when its gates are green.
4. **Models:** use `claude-opus-4-8` for delegated work — `claude-fable-5` refuses this
   autonomous work (dual-use safety measures).
5. **Bounded.** At most 3 attempts per issue; if it won't converge, mark it `acknowledged` in
   the ledger, comment the issue with what's blocking, and move on — never stall the queue.
6. **Track state** in `pipelines/ledger.py` (`earliest` / `update <n> implemented|acknowledged`
   / `stats`) so the run is resumable.

**Done.** Every ledger row is `implemented` (green-gated PR open) or `acknowledged` (human
handoff, commented). Report: the PRs opened, the `acknowledged` rows with reasons, and how you
set the pipeline up so it's reusable next time. If anything about scope or a destructive step
is genuinely ambiguous, make the safe choice and note it — keep moving.

---

## Notes

- `pipelines/ledger.tsv` is seeded with all open issues **except #21** (already fixed on its
  own branch). `pipelines/ledger.py` keeps 3 columns (`issue`, `slug`, `state`); issue
  categories come from the GitHub labels, not the ledger.
- Not every issue is a code port. #26, #36, #53, #54 (and arguably #49/#50) are
  decisions/spikes whose deliverable is a `docs/plans/` design doc — the prompt tells amplifier
  to adapt the gate accordingly.
