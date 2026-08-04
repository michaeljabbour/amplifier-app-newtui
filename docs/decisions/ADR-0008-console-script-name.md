# ADR-0008: The console script stays `amplifier-tui`

Status: accepted · 2026-08-03
Supersedes: the "temporary differentiation from the reference `amplifier` CLI" language in
merged PR #179. That deferral is now a decision with a rationale; the name is not provisional.
Compliance: closes the open piece of audit item **D1 — one global installation and update
path** (the executable-rename question David flagged), and satisfies D1's **AC4** (§AC4).

## Context

`amplifier-app-tui` installs one executable, `amplifier-tui`
(`pyproject.toml:28` → `[project.scripts] amplifier-tui = "amplifier_app_tui.main:main"`).
The recurring question is whether it should instead be plain `amplifier`, since PR #179
branded everything *inside* the app as plain "amplifier" and called the hyphenated command
a temporary stopgap.

That question conflates two different changes with completely different consequences. This
ADR separates them, measures the one that matters, and settles the name.

### `amplifier` is already owned — twice

| repo | declares |
| --- | --- |
| `microsoft/amplifier` (entry point, `pyproject.toml:17`) | `amplifier = "amplifier_app_cli.main:main"` |
| `microsoft/amplifier-app-cli` (`pyproject.toml:24`) | `amplifier = "amplifier_app_cli.main:main"` |

The entry-point repo states the intent plainly (`amplifier:docs/MODULES.md:40`): "When you
install `amplifier`, you get the amplifier-app-cli as the executable application."
`amplifier:docs/REPOSITORY_RULES.md` designates `microsoft/amplifier` as the **Entry Point**
repository — the one that references everything. The bare `amplifier` command is that repo's
product surface, not an unclaimed name.

The collision is live, not theoretical. On a developer machine with the ecosystem installed,
`uv tool list` shows **both** `amplifier v0.1.0` and `amplifier-app-cli v0.1.0` claiming the
`amplifier` executable, and `~/.local/bin/amplifier` symlinks into the meta-package's tool
directory. Two installed tools already contend for the name; a third would land in that fight.

### What uv actually does on a name collision (measured)

Re-measured 2026-08-03 against **uv 0.10.2** in an isolated sandbox (`UV_TOOL_DIR` and
`UV_TOOL_BIN_DIR` redirected to a scratch tree, real installs untouched) with two throwaway
packages, `pkg-a` and `pkg-b`, both declaring `[project.scripts] collide`. Run, not inferred
from documentation:

| step | result |
| --- | --- |
| install `pkg-a`, then `pkg-b` | **`error: Executable already exists: collide (use --force to overwrite)`**, exit **2**. `collide` still runs `pkg-a`; `uv tool list` shows only `pkg-a` — `pkg-b` is not registered. |
| `uv tool install --reinstall pkg-b` | same error, exit **2** |
| `uv tool install --upgrade pkg-b` | same error, exit **2** |
| `uv tool install --force pkg-b` | exit **0**, silently takes the name. `uv tool list` now shows **both** `pkg-a` and `pkg-b` claiming `collide`. |
| then `uv tool uninstall pkg-a` — the tool that **no longer owns** the name | `Uninstalled 1 executable: collide` — the shared symlink is **gone**. `pkg-b`'s command is destroyed by uninstalling `pkg-a`. |

That last row is the load-bearing one. After a forced overwrite the two tools share one
symlink with no ownership record, so uninstalling *either* tool silently breaks the *other*.
There is no clean recovery short of reinstalling by hand.

*(One correction to the earlier informal write-up of this experiment: it reported that the
losing install leaves a venv behind. On uv 0.10.2 the failed install is rolled back
completely — the tool directory contains only the winner. The user-visible outcome is
unchanged: hard failure, no command.)*

### Two consequences that make this decision load-bearing

1. **It would break the ecosystem's own documented onboarding.** The entry-point repo's
   README and this repo's README both tell users to run
   `uv tool install git+https://github.com/microsoft/amplifier`. If the TUI declared
   `amplifier`, that one-liner would fail with exit 2 for anyone who installed the TUI
   first — the entry point's front door, broken by a downstream app.
2. **`amplifier update` could not force its way out.** app-cli's self-update delegates to
   `uv tool install --upgrade --reinstall`
   (`amplifier-app-cli:amplifier_app_cli/utils/update_executor.py`, `execute_self_update`),
   and a regression test pins the absence of `--force`
   (`tests/test_update_executor.py::test_execute_self_update_uses_upgrade_reinstall_not_force`).
   Both of those flags hit the collision error, as measured above — so self-update would fail
   and stay failed.

   *Honest nuance:* that test's stated rationale is cost, not collisions — "`--force`
   destroys the entire tool virtualenv and rebuilds from scratch, which is unnecessarily
   slow." It was not written to guard against this. The effect is the same either way: the
   supported update path cannot force past a collision, and changing that would mean deleting
   a deliberate, tested constraint in a repo this one does not own.

### The naming convention already covers this

Every app-layer executable in the ecosystem is a flat `amplifier-*` name. Verified on this
machine via `uv tool list`: `amplifier-agent`, `amplifier-opencode`, `amplifier-tui`,
`amplifier-digital-twin`, `amplifier-gitea`, `amplifier-shadow`, `amplifier-online`,
`amplifier-resolve`, `amplifier-workspace`, `amplifierd`. The single exception is `amplifier`
itself, reserved for the reference CLI per `REPOSITORY_RULES.md`. **`amplifier-tui` already
conforms** — it is not an anomaly awaiting correction.

### Governance state

No ADR, issue, or CODEOWNERS entry anywhere in the ecosystem settles who owns the `amplifier`
command. The only written artifact was PR #179's parenthetical calling `amplifier-tui` a
"temporary differentiation." This ADR replaces that parenthetical.

## Decision

**Keep `amplifier-tui` as this repo's console script. Do not rename.**

The rename question splits into two changes that are routinely confused:

**(a) The TUI declares `amplifier` in its own `pyproject.toml`. — REJECTED.**
Produces a third claimant on a contended name. Measured consequences: install fails with
exit 2 for anyone who already has the platform (and vice versa), the entry-point repo's
documented onboarding one-liner breaks, `amplifier update` cannot recover, and any `--force`
workaround creates a shared symlink where uninstalling either tool destroys the other's
command. This repo cannot unilaterally take a name the entry-point repo ships.

**(b) `microsoft/amplifier` repoints its existing `amplifier` script at the TUI.** One line in
`[project.scripts]` plus a dependency swap, in the repo that already owns the name. No
collision — there is still exactly one declaring package. The onboarding one-liner keeps
working; `amplifier` simply launches a different app. **This is the only viable path to the
underlying want**, and it is a governance decision belonging to the entry-point repo's owner,
not to this repo. Appendix A drafts the issue that asks it.

Anyone re-opening "should the TUI be called `amplifier`?" should be asked which change they
mean. If (a), this ADR is the answer. If (b), the venue is `microsoft/amplifier`.

### AC4 — compatibility and competing defaults

D1's AC4 reads: *"If the command is renamed, the former command has a documented
compatibility or deprecation path and users are not shown two competing defaults."*

**AC4 is satisfied by the decision not to rename.** Its precondition never fires — there is
no former command, so there is nothing to deprecate and no compatibility shim to document.
Users see exactly one default today: `amplifier-tui` — the sole entry in `[project.scripts]`,
the name in every README install and run line, and the `prog_name` reported by `--version`
(`src/amplifier_app_tui/main.py:391`). The in-app branding is plain "amplifier"
(`src/amplifier_app_tui/ui/chrome.py:37`, `APP_TITLE_NAME`), which is a *display* string, not
a second command — nothing on `PATH` competes with `amplifier-tui`.

If path (b) is ever taken upstream, AC4 becomes the entry-point repo's obligation on its own
`amplifier` command, and this repo's `amplifier-tui` continues to work unchanged — which is
itself the compatibility path.

### #187 (app-scoped settings namespace) does not block this

Confirmed independently: nothing in the config resolution chain derives any path, key, or
identity from the executable name. This repo already carries **three different identity
strings for the same app**, and they have never been coupled:

| string | where | role |
| --- | --- | --- |
| `amplifier-tui` | `pyproject.toml:28` | console script |
| `"amplifier-tui"` | `src/amplifier_app_tui/main.py:391` | `--version` prog name |
| `"tui"` | `src/amplifier_app_tui/kernel/config.py:39` (`DEFAULT_BUNDLE`) | default bundle / `bundle.name` |

Three names, one app, zero coupling — direct proof that settings identity does not follow the
executable.

The dependency runs **D1 → #187**, not the reverse. #187's premise — that cross-app interop
is a feature — presupposes two distinct commands coexisting on `PATH`. Settling that there
*are* two distinct commands is what gives #187 its problem statement. #187 can proceed without
reopening this ADR, and this ADR does not wait on #187.

## Consequences

- **Zero code change.** `pyproject.toml`, `main.py`, and `kernel/config.py` are untouched.
  This ADR is the deliverable; the gate is a no-op for behavior.
- **The name is settled, not deferred.** PR #179's "temporary" framing is superseded. Future
  PRs should cite this ADR rather than re-argue the name.
- **The real want stays alive, in the right venue.** People who want to type `amplifier` and
  get the TUI are asking for (b). Appendix A is ready to file against `microsoft/amplifier`.
- **The install/update docs stay correct as written.** README's
  `uv tool install --reinstall git+…/amplifier-app-tui` is the right form precisely because it
  does *not* pass `--force`; with a unique name it never reaches the collision path.
- **If (b) is ever accepted upstream,** this repo's `amplifier-tui` keeps working with no
  change, and the README's "optional: the full Amplifier platform" section becomes the thing
  that needs an edit — not `[project.scripts]`.

## Non-Goals

This ADR does not decide what `amplifier` *should* point at — that is the entry-point repo's
call. It does not change in-app branding (plain "amplifier" stays, per PR #179). It does not
add an alias, shim, or second console script: two names for one app is precisely the
"competing defaults" AC4 forbids.

---

## Appendix A — drafted upstream issue (NOT filed)

Ready to file against **`microsoft/amplifier`**. Drafted here so the decision has a written
next step; filing is the entry-point owner's call, not this repo's.

**Title:** `Which app should [project.scripts] amplifier launch — app-cli or app-tui?`

**Body:**

````markdown
## The question

`microsoft/amplifier` owns the `amplifier` command:

```toml
[project.scripts]
amplifier = "amplifier_app_cli.main:main"
```

`docs/MODULES.md` states the intent plainly: "When you install `amplifier`, you get the
amplifier-app-cli as the executable application."

Now that `amplifier-app-tui` exists and is the full-screen interactive surface, the question
is whether the entry point should keep launching app-cli or switch to app-tui. **Only this
repo can answer it** — no downstream app can take the name (see below), so the decision gets
made here or not at all.

## Why downstream can't just do it

`amplifier-app-tui` deliberately ships `amplifier-tui`, not `amplifier`, and has recorded that
as a decision (ADR-0008 in that repo). The reason is measured, not hypothetical.

Measured on **uv 0.10.2**, isolated sandbox (`UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` redirected),
two throwaway packages both declaring the same script name:

| step | result |
| --- | --- |
| install second package | `error: Executable already exists: collide (use --force to overwrite)`, exit 2. Not linked, not registered. |
| retry with `--reinstall` | same error, exit 2 |
| retry with `--upgrade` | same error, exit 2 |
| retry with `--force` | exit 0, silently takes the name; **both** tools then claim it in `uv tool list` |
| then uninstall the tool that no longer owns the name | `Uninstalled 1 executable` — **the shared symlink is removed, destroying the other tool's command** |

Consequences if a second package declared `amplifier`:

1. **This repo's documented onboarding would break.**
   `uv tool install git+https://github.com/microsoft/amplifier` fails with exit 2 for any user
   who installed the other app first.
2. **Self-update could not recover.** `amplifier-app-cli`'s `execute_self_update` runs
   `uv tool install --upgrade --reinstall`, and a regression test pins the absence of
   `--force` (`tests/test_update_executor.py::test_execute_self_update_uses_upgrade_reinstall_not_force`).
   Both flags hit the collision error.
3. **`--force` is worse than the disease** — it creates the shared-symlink state above, where
   uninstalling either tool silently breaks the other.

The collision is already live: `uv tool list` on a developer machine with the ecosystem
installed shows both `amplifier` and `amplifier-app-cli` claiming the `amplifier` executable.

## What a change here would actually cost

Repointing the entry point is **one line plus a dependency swap** in this repo:

```toml
[project.scripts]
amplifier = "amplifier_app_tui.main:main"   # was amplifier_app_cli.main:main
```

No collision — there is still exactly one declaring package, and the onboarding one-liner
keeps working unchanged. Users who type `amplifier` simply get a different app.

## Decision requested

Pick one:

- **(A) Status quo** — `amplifier` stays app-cli; `amplifier-tui` remains the TUI's command.
  Please say so explicitly so it stops being re-litigated downstream.
- **(B) Repoint** — `amplifier` launches app-tui. Then this repo needs: the script +
  dependency swap, a `docs/MODULES.md` update (the sentence quoted above), a decision on
  whether app-cli keeps a command of its own (it declares `amplifier` in its *own*
  `pyproject.toml` too, so that needs resolving in the same change), and a migration note for
  existing installs.
- **(C) Neither** — a different arrangement, e.g. a dispatcher that picks a surface. Worth
  naming the cost: it adds a layer between the user and both apps.

If (B) or (C), an ADR here would be the right home, since `docs/REPOSITORY_RULES.md` makes
this repo the Entry Point and the only legitimate owner of the bare `amplifier` name.

## Non-goal

This is **not** a request for `amplifier-app-tui` to be renamed to `amplifier`. That has been
rejected downstream for the measured reasons above. This asks only what the name this repo
already owns should point at.
````
