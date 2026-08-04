# Parity gap decision sheet — 19 gaps awaiting a product-owner ruling

**For a human. One sitting, ~20 minutes.** Every gap below has its evidence, its
user-visible consequence, and a proposed disposition with a ready-to-paste
command. Work top to bottom; the recommendations are ordered so the cheap,
obvious ones clear first.

> **Nothing here is decided.** An agent produced the evidence and the
> recommendations; an agent may not produce the decisions. The tooling enforces
> that literally — `parity_loop.py decide` refuses any owner that isn't a real
> person (see [Ground rules](#ground-rules)), so this sheet cannot sign itself.

**State as of `7030527` (2026-08-04):**

```
passes=2 clean_streak=0/3 run=open
gaps: pending=19
awaiting=19/19
```

## Ground rules

| Disposition | Means | Opens code work? |
|---|---|---|
| `accepted` | we want it; enters the transfer pipeline | **yes — the only one that does** |
| `rejected` | not worth building here (incl. "belongs below the harness / on another surface") | no |
| `deferred` | real, but not now | no |
| `already-covered` | tui reaches the capability already; not a gap | no |
| `pending` | nobody has ruled — **blocks** (the safe default) | no |

**Owner must be a real person.** `TBD`, `owner`, `team`, `unknown`, `?`, blank
and their relatives are refused at write time and read back as `unattributed` if
hand-edited into the file — a decision nobody signed is not a decision. Use your
own handle.

```sh
python3 pipelines/parity_loop.py awaiting     # what's still owed a ruling
python3 pipelines/parity_loop.py validate     # VALID | INVALID (unsigned decisions)
python3 pipelines/parity_loop.py stats
```

---

## Part 1 — the eleven baseline gaps (`100`–`110`)

**All eleven shipped.** They were filed on 2026-07-23, built between then and
now, and the run was even marked complete in `fbf7abe` — but **no disposition was
ever recorded**, so all eleven still read `pending` and still block. This part is
bookkeeping catching up with reality, not a product judgement.

Recommended disposition for every row: **`already-covered`** — present tense, the
capability is reached today. (`accepted` would be wrong: it would enqueue
already-built work into the transfer pipeline.)

| Gap | Capability | Landed in | Verify at |
|---|---|---|---|
| `100` | Prompt-injection probe on tool output (**was High**) | `4a4c865` | `kernel/governance_hook.py:38` → `model/injection.py` |
| `101` | Deferred-decision dependency blocking | `072dc5d` | `kernel/governance_hook.py:458,552-566,673-686` |
| `102` | Two-stage / provider-backed classifier seam | `8dc2047` | `kernel/governance_hook.py:193-212,769` |
| `103` | Provider key-store advisory lock | `e55af35` | `kernel/setup.py:49,430-441` |
| `104` | `sources.bundles` fed to `prepare()` | `469536a` | `kernel/config.py:499,513-518` |
| `105` | `bundle.added` resolved by name at boot | `11d38cc` | `kernel/bundle_admin.py:137-138` |
| `106` | Notification config keys + `notify` CLI (ntfy, desktop) | `7f51491` | `main.py:3447-3471`; `kernel/config.py:958-975` |
| `107` | `tool list / info / invoke` CLI | `fce458c` | `main.py:1254,1306,1379` |
| `108` | `/fork` + session fork | `4affd84` | `commands/builtin.py:202-204,539` |
| `109` | `run` per-invocation `--model/--provider/--mode` + `--resume` | `0165aef` | `main.py:335-347,553-565` |
| `110` | Data-safe `reset` | `0735127` | `main.py:1799-1897` |

**No design spike needed for any of these** — a spike informs a build decision,
and the build already happened. The one-line remediation is the disposition
itself.

Paste this block, with your handle:

```sh
OWNER=<your-handle>
for g in 100 101 102 103 104 105 106 107 108 109 110; do
  python3 pipelines/parity_loop.py decide "$g" already-covered "$OWNER" \
    "shipped before this gate existed; verified in tree at 7030527"
done
```

*(If you'd rather rule per gap — e.g. you think `102`'s provider-backed second
stage is only partially there — do them individually; the loop upserts.)*

---

## Part 2 — the eight new gaps (`111`–`118`)

Found by [pass 2](pass2-2026-08-04.md), a read-only re-audit at `7030527`. These
are **genuinely undecided** — real product calls, and the reason this sheet
exists.

---

### `111` — `/goal`: autonomous goal-driven continuation · Lane 1 · Med

**What it is.** The donor added `/goal <condition>`: the agent keeps taking turns
by itself until a stated condition is met, with stall detection, an optional
`--max-turns`, and a shipped skill that teaches the model to write a *checkable*
condition.

**Evidence.** Donor `main.py:364-390,458-461,571-579`, `goal_progress_hook.py`
(418 lines), `data/skills/goalify/SKILL.md`, `ADR-0005-goal-unlimited-by-default`.
This repo: no `/goal` verb among the 40 registered in
`commands/builtin.py:315-624`.

**Consequence of not having it.** Long autonomous runs need a human to press
enter at every turn boundary. This is the single largest capability the donor has
that this repo does not.

**Design spike:** [`docs/plans/2026-08-04-spike-goal-command-parity.md`](../plans/2026-08-04-spike-goal-command-parity.md)
— written because this is the one gap where "how would it even work here" is a
real question: an autonomous loop collides with ADR-0007's turn model, with the
3600s approval-timeout floor, and with unattended spend in a full-screen UI the
user may have walked away from.

**Recommendation: `deferred`** — real, wanted, but a multi-week feature with an
open architectural question. Accepting it today would put it in the transfer
pipeline ahead of seven one-liners.

```sh
python3 pipelines/parity_loop.py decide 111 deferred <your-handle> \
  "real capability; spike docs/plans/2026-08-04-spike-goal-command-parity.md; revisit after the one-liners"
```

---

### `112` — `/init`: scaffold `AGENTS.md` project memory · Lane 1 · Med

**Evidence.** Donor `ui/command_catalog.py:61` (`30c0a65`). This repo: absent —
the CLI `init` (`main.py:920-939`) is provider-credential setup, a different
thing with the same name.

**Consequence.** No in-session way to start project memory. A user who has never
written an `AGENTS.md` has no prompt to do so, and project memory is the single
highest-leverage thing a new user can set up.

**Spike?** No. It writes a templated file into the project root, guarded by the
existing protected-path rules (`AGENTS.md` is already in
`PROTECTED_PROJECT_PATHS`, so the scaffold needs an explicit create-if-absent
path — that's the whole design).

**Recommendation: `accepted`.** Highest value-per-line on this list.

```sh
python3 pipelines/parity_loop.py decide 112 accepted <your-handle> \
  "project memory is the highest-leverage new-user setup step; small scaffold"
```

---

### `113` — `--install-completion` (bash/zsh/fish) · Lane 1 · Low

**Evidence.** Donor `main.py:262,2591-2598`. This repo ships completion
*candidates* for resume ids (`main.py:37,61-68`) but nothing installs a
completion script.

**Consequence.** No tab-completion for commands and session ids unless the user
wires click's completion by hand.

**Spike?** No — click ships the mechanism; this is a flag plus a shell-rc writer,
and the donor's implementation is directly readable.

**Recommendation: `accepted`.**

```sh
python3 pipelines/parity_loop.py decide 113 accepted <your-handle> \
  "click provides the mechanism; small, and the resume-id completer already exists"
```

---

### `114` — `module list / show / current / validate` CLI · Lane 1 · Low

**Evidence.** Donor `commands/module.py:49` + group. This repo: no `module` group
in `amplifier-tui --help`.

**Consequence.** No CLI introspection of the module cache. This is a
module-*author* debugging surface; `doctor` already answers "is my install
healthy" for users, and `source` covers the override paths.

**Spike?** No.

**Recommendation: `rejected`** — belongs on the module-authoring surface, not in
a chat client. (The baseline's own note said "likely out of tui user scope"; this
makes that explicit rather than leaving it `pending` forever.)

```sh
python3 pipelines/parity_loop.py decide 114 rejected <your-handle> \
  "module-author debug surface, not a chat-client surface; doctor + source cover the user need"
```

---

### `115` — `agents list / show / dirs` CLI · Lane 1 · Low

**Evidence.** Donor `commands/agents.py:16,26,72,112`. This repo has in-session
`/agents` (`commands/builtin.py:415`) but no CLI group.

**Consequence.** You can't ask "what agents would this bundle give me" without
starting a session. Minor, but it's a scripting/inspection surface and `tool
list` (gap `107`) already established the pattern.

**Spike?** No — same shape as the `tool list` command that already ships.

**Recommendation: `accepted`** (low priority).

```sh
python3 pipelines/parity_loop.py decide 115 accepted <your-handle> \
  "mirrors the tool list command that already ships; low effort"
```

---

### `116` — `/btw`: context-free side question · Lane 1 · Low

**Evidence.** Donor `ui/command_catalog.py:117` (`30c0a65`). This repo: absent.

**Consequence.** Asking an unrelated question mid-task pollutes the working
context. Partially mitigated: `/fork` (gap `108`, shipped) covers the heavier
version of the same need.

**Spike?** No.

**Recommendation: `rejected`** — `/fork` and `/compact` cover the need between
them; a third context-management verb is surface area, not capability. Flip to
`accepted` if you disagree, it's a small command.

```sh
python3 pipelines/parity_loop.py decide 116 rejected <your-handle> \
  "/fork + /compact cover the need; a third context verb is surface, not capability"
```

---

### `117` — `/feedback`: prefilled GitHub issue · Lane 1 · Low

**Evidence.** Donor `ui/command_catalog.py:302` (`30c0a65`). This repo: absent.

**Consequence.** No in-app path from "this is broken" to a filed issue. Cheap,
and the app already knows its version, bundle, and session id (`/about`,
`commands/builtin.py:500`) — exactly the fields a good bug report needs.

**Spike?** No — it's a URL builder over data `/about` already assembles. Note the
one real design point: it must run the existing redaction path
(`model/redaction.py`) over anything it prefills.

**Recommendation: `accepted`.**

```sh
python3 pipelines/parity_loop.py decide 117 accepted <your-handle> \
  "cheap; /about already assembles the fields; must route prefill through model/redaction.py"
```

---

### `118` — fail-loud module activation · Lane 3 · Med

**What it is.** The donor now **aborts session start** when any declared module
fails to download/activate, with `AMPLIFIER_ALLOW_PARTIAL_BUNDLE=1` as the
explicit opt-out. This repo starts **degraded** and says so.

**Evidence.** Donor `lib/bundle_loader/prepare.py:39`,
`commands/run.py:17,185-196`. This repo `kernel/session_factory.py:101-120`
(`degraded_notice()` names every failed module), fatal only when no provider at
all mounts (`:95-98,:474`).

**Consequence — and this is a real trade, not an oversight.** The donor's case:
a session missing a declared tool fails later, further from the cause, and reads
to the user as "the model ignored me." This repo's case, already encoded in
ADR-0007: a full-screen TUI that refuses to boot is a worse failure than one that
boots and names what's missing — which is exactly why `degraded_notice()` exists.

**Spike?** No — but not because it's trivial. The design space is one flag and
two behaviours; what it needs is a **policy decision**, and a spike would just be
a longer way of writing this paragraph.

**Recommendation: `rejected` for the abort-on-boot policy.** The honesty the
donor is buying, this repo already has by a different mechanism, and that is a
PARITY verdict under the audit's own "same protection, different mechanism" rule.
*If you disagree*, the one-line remediation is a strict opt-**in**
(`AMPLIFIER_STRICT_BUNDLE=1`) rather than adopting the donor's default — same
capability, without regressing boot resilience.

```sh
python3 pipelines/parity_loop.py decide 118 rejected <your-handle> \
  "degraded_notice() already delivers the honesty without sacrificing boot resilience (ADR-0007)"
```

---

## Part 3 — recorded but deliberately not filed

Pass 2 found these and judged them below the filing threshold (the baseline's own
line: file `MISSING` and `PARTIAL`-Med, not `PARTIAL`-Low). Listed so the choice
is reviewable — **say the word and they get ids `119`+**:

- `/save`, `/strength` — aliases for `/export`, `/effort`, which already ship.
- `/review` — folded into `/plan`'s read-only posture.
- `resume --replay / --full-history / --show-thinking` — history-render niceties.
- `init`'s combined provider+routing dashboard **loop** — the linear flow reaches
  the same configuration.

**Could not determine (needs a foundation-side read, not another app-side pass):**
whether an agent's declared `agents:` sub-delegation access-control policy is
enforced here. The donor changed here in `6c3fd86`; this repo has no live-registry
propagation block for that change to break (`kernel/spawner.py:162,198,320-322`),
but enforcement below the app seam is unproven. Filing it as a gap would be a
guess, so it isn't filed. See [pass 2, lane 2](pass2-2026-08-04.md#lane-2--safeguards).

---

## After you've ruled

```sh
python3 pipelines/parity_loop.py validate        # must print VALID
python3 pipelines/parity_loop.py awaiting        # should print awaiting=0/19
python3 pipelines/parity_loop.py gaps accepted   # what enters the transfer pipeline
python3 pipelines/parity_loop.py should-continue # CONTINUE clean_streak=0/3
```

Then the next re-audit can run. Once every gap carries a disposition, a pass that
finds nothing new is **clean** — which is how the streak starts moving, and why
triage, not code, is the next action on this loop.
