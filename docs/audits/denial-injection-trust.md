# Denial-stream "prompt injection" — root cause & trust boundary (2026-07-24)

Branch: `fix/denial-injection-trust`.

## The report

Live session — bundle `tui`, posture `brainstorm` ("no tools"), native mode
`team-pulse` active. The user asked a question; team-pulse's tools
(`team_pulse_info`, `team_pulse_search`) and a `mode` call were all **denied**
by the no-tools posture. The model reported that its context carried a
multi-thousand-word block "dressed up as legitimate system-reminder tags":
git status, a session id, a "team-pulse mode instruction manual", a routing
matrix, **and** adversarial-looking directives — *"do NOT call mode(set…)",
"NEVER mention this reminder to the user", "delegate to team-pulse-expert."*
The model refused to trust it and surfaced it to the user.

## Verdict: (b) benign context that reads adversarial under a denial

Not (a) a bundle injecting a "hide from the user" attack, and not (c) a tui
rendering bug that concatenates context into denial text. Every piece the model
saw is a legitimate, ephemeral `<system-reminder source="…">` block emitted by
an independent housekeeping hook. They co-located in the model's context on the
same turn where the no-tools posture denied the mode's tools, so the union read
like one injection payload.

### Provenance of each piece

| Fragment the model saw | Real source | Verbatim origin |
|---|---|---|
| git status + session id, "DO NOT mention this status information to the user … Process silently" | `hooks-status-context` | `amplifier_module_hooks_status_context/__init__.py:211` |
| "NEVER mention this reminder to the user" / "DO NOT mention this reminder to the user … Process this silently" | `hooks-todo-reminder` | `amplifier_module_hooks_todo_reminder/__init__.py:116,139` |
| "MODE ACTIVE: team-pulse … do NOT call mode(set, \"team-pulse\") to re-activate it" | `hooks-mode` | `amplifier-bundle-modes/modules/hooks-mode/…/__init__.py:734-735` |
| the "team-pulse mode instruction manual" incl. "delegate to team-pulse-expert" | `hooks-mode` injecting the active mode body | `team-pulse.md:332` (standing order §4) + examples |
| routing-matrix table | composed `routing-matrix` bundle / `hooks-routing` | (opt-in overlay) |

The team-pulse **bundle carries no concealment directive** — grepping
`modes/`, `context/`, `behaviors/` for "NEVER mention", "do NOT call mode",
"hide from" finds only the benign `delegate to team-pulse-expert` *example*
(`team-pulse.md:112`) and legit `system-reminder` references. Likewise
amplifier-core and amplifier-foundation contain none.

So the "adversarial directives" are the standard Claude-Code **housekeeping
reminder convention** ("process silently / don't mention this to the user"),
applied by three independent hooks — benign in intent, but genuinely
"hide-from-the-user"–shaped, which is why under a no-tools denial (with the
tools that would justify them stripped) they read as an attack. The model did
the right thing by refusing to silently obey and surfacing them.

## tui is not the source — but it had one real trust bug

tui neither authors nor concatenates any of this. It renders tool denials
as `⊘ blocked` lines verbatim from the tool-result payload
(`ui/reducer.py::_tool_post`), never folding reminder/context text into them,
and it filters `<system-reminder>` blocks out of the resume transcript. It
does **not** honor a hook's `suppress_output` to hide model output.

The one genuine defect: the resume-replay reminder filter tested
`text.startswith("<system-reminder>")` — an exact, attribute-free prefix.
**Every reminder a real hook emits is `<system-reminder source="…">`**, so
that prefix matched none of them; on resume those injected reminders — including
the concealment ones — would replay into the user's transcript as **fabricated
user turns**. Fixed in `kernel/reminder_trust.py` (attribute-tolerant,
provenance-aware, one pure/tested chokepoint) and wired into
`runtime.restored_history`, which now also *logs* (never silences) any dropped
concealment directive so the trust event stays observable.

Trust invariants now regression-covered in `tests/test_denial_injection_trust.py`:

- injected reminders (bare **and** attributed) never replay as user turns;
- a denial whose reason/continuation absorbed a "do not tell the user" payload
  is still rendered to the user in full — tui never suppresses user-facing
  output on a reminder's say-so.

## Upstream ask (foundation / bundles)

These are not tui bugs to fix in tui; the honest fix is upstream:

1. **`hooks-todo-reminder` / `hooks-status-context`** — the "NEVER mention this
   reminder to the user / process silently" phrasing is behaviorally a
   secrecy instruction. Reframe it as UI-housekeeping ("this is shown in the
   task panel; no need to repeat it") rather than "never mention to the user",
   so it cannot be weaponized by a hostile bundle wearing the same convention,
   and so it does not read as an injection when co-located with a denial.
2. **Denial context hygiene** — when a posture denies a tool, the denial
   tool-result and the turn's ephemeral system-reminders land together in the
   model's context. Consider not co-mingling housekeeping reminders into a
   turn whose tool calls were all denied (they have no tools to justify them),
   or tagging them so a model can tell "housekeeping" from "instruction".
3. **`is_real_user_message` (foundation `session/messages.py:95,103`)** has the
   same bare `startswith("<system-reminder>")` blind spot tui just fixed —
   attributed reminders slip its "real user message" guard. Worth hardening
   upstream with the same attribute-tolerant match.
