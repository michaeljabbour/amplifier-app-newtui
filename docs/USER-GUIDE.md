# User Guide

How to drive the Amplifier TUI day to day: modes, steering, approvals, subagent lanes,
rewind, and every key and command. For install/provider setup see the
[README](../README.md); for how it works under the hood see
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Launching

```sh
uv run amplifier-tui              # full-screen TUI, real session
uv run amplifier-tui --demo       # scripted demo — no credentials needed
uv run amplifier-tui --bundle B   # pick a bundle by name or URI
uv run amplifier-tui sessions     # list stored sessions for this project
uv run amplifier-tui resume SESSION_ID    # resume a stored session
uv run amplifier-tui run "PROMPT" # headless one-shot, prints the answer
printf 'PROMPT\n' | uv run amplifier-tui run # stdin one-shot
uv run amplifier-tui run --output-format json "PROMPT" # machine-readable stdout
uv run amplifier-tui run --output-format jsonl "PROMPT" # live versioned JSONL events
uv run amplifier-tui doctor       # setup checkup (exit 1 when findings exist)
uv run amplifier-tui init         # set up a provider key in ~/.amplifier/keys.env
uv run amplifier-tui bundle list  # bundles from the shared registry (--all for deps)
uv run amplifier-tui bundle use B # set the active bundle (--global/--project/--local)
uv run amplifier-tui routing manage   # inspect and choose a routing matrix
uv run amplifier-tui routing use NAME # choose one directly (anthropic, runpod, ...)
uv run amplifier-tui update       # update the mounted bundles/modules (--check-only/--force)
```

`bundle` also has `show · current · clear · add · remove · update`; run
`bundle --help`. These read/write the same amplifier settings and registry
the reference CLI uses — nothing app-specific.

`routing manage` is a numbered picker. At `choice:`, type either the displayed
row number or the exact matrix name to select it. Use `v NUMBER` or `v NAME` for
details, `c` to create a custom matrix, `w` to change the settings scope, and `d`
to finish. `routing use NAME` is the non-interactive equivalent. A changed matrix
is used by the next session; it does not reroute a turn already in progress. If a
custom name is numeric or collides with a one-letter control, use `select NAME`;
colon-prefixed controls such as `:done` remain unambiguous.

`run` accepts either a prompt argument or all piped stdin. JSON modes reserve stdout for
machine-readable output and redirect setup/module diagnostics to stderr. `json-trace`
adds the normalized runtime event trace to one document. `jsonl` is live: every line has
`schema_version`, monotonic `sequence`, `timestamp`, and a discriminating `type` of
`session.started`, `runtime.event`, `turn.completed`, or `error`. Runtime records contain
the same typed event payload consumed by the TUI.

`serve` is the interactive counterpart: a bidirectional JSON-line protocol on stdio that
an out-of-process front-end (or an automated controller) drives. On top of it sits the
**session control contract** — a durable session handle, a single-writer lease so a human
and an automation can share one session without clobbering each other, deterministic
takeover, pause-and-hand-off to a person, and reattach after a dropped connection:

```sh
uv run amplifier-tui serve                       # protocol on stdio
uv run amplifier-tui serve --actor mj --actor-kind human   # stamp the default actor
uv run amplifier-tui serve --attach amplifier-session:SESSION_ID#ho-9a2  # claim a handoff
```

`--attach` takes the reference a paused controller minted: it opens the SAME session and
hands you the write lease. Full contract: [SESSION-CONTROL.md](SESSION-CONTROL.md).

**First run:** follow the [README's Install section](../README.md#install). Its single
source-install command verifies `amplifier-tui` and opens the built-in provider setup; the
full [Amplifier](https://github.com/microsoft/amplifier) CLI is optional. Existing
`~/.amplifier/` providers and credentials are reused automatically. If anything is off,
`doctor` will tell you what and how to fix it. Not sure everything's wired? `--demo` always
works and exercises the whole UI offline.

## 2. The screen

```
┌ title bar ── spinner · state — bundle — session id ─────────────────┐
│                                                                     │
│  transcript — your lines, activity digests, plans, answers,         │
│               turn rules (one pre-prompt checkpoint each)           │
│                                                     ┌ notices ┐     │
├─ overlay strips appear here (palette / lanes / rewind / queued) ────┤
│ [mode] ❯ composer — type here            (swaps to approval bar)    │
├─────────────────────────────────────────────────────────────────────┤
│ mode · trust · model · session · $cost         contextual hints     │
└─────────────────────────────────────────────────────────────────────┘
```

While Amplifier is working, the title bar pulses in-app and an unmistakable
braille spinner is mirrored into your terminal window or tab title. Both use
the same active-turn timer, so they stop immediately when the turn finishes and
consume no idle redraw loop. The title bar is also the one place the
active **bundle** shows — it never repeats in the footer below. It shows the
actual resolved bundle path/URI (not just its short name), fitted to your
terminal's live width so it never wraps onto the composer — wide terminals
show more of it, narrow ones truncate with a trailing `…`; `/status` always
prints the complete, untruncated value.

The footer's left side always shows your current mode, trust posture, model, session id,
and session cost (`~$…` when any usage couldn't be priced — the total is a floor). A green
`▲` appears after a turn that shipped, and an orange `q1` badge while a next-turn message is
queued. On narrow terminals the decorations drop one by one (trust, then session, then
model) — mode and cost never drop. The right side is empty at rest — it isn't trying to
teach you the whole keyboard on every frame — and only fills in with the keys that work
*right now* once something needs them (a running turn, an open overlay). For the full
shortcut list any time, run **/keys**.

**Plan panel.** When the agent keeps a live checklist (the `todo` tool), a compact
**`Plan N/M`** panel appears in the bottom strip's right column: `✔` done, `▶` in
progress, `○` pending — windowed around the in-progress item, with a `⋮ +N more`
control when the plan is long. **Click** it, or press **ctrl-h** — which works from
anywhere, no need to focus it first — to expand the full list in place (**ctrl-n**
is a different chord: it instead widens the row window default → +2 → +3 rows →
back). The control is also focusable, so **enter**/**space** re-toggle it once it
has focus (matching ctrl-h/click). Either path flips the control to
**`▾ Show less`** to collapse it again; at a short
terminal height the expanded list scrolls inside the panel rather than covering the
composer. Once every item completes it collapses to the header line (done stays
visible). On narrow terminals the panel stacks below the agent lanes at full width;
the same **ctrl-h**, enter/space, and click controls remain available, and long content
continues to scroll inside the bounded panel without covering the composer.

## 3. Talking to Amplifier

| You want to… | Do this |
|---|---|
| Send a message | type, **enter** |
| Recall an earlier prompt | **↑** for older, **↓** for newer/current draft |
| Add a newline while composing | **ctrl+j** or **ctrl+enter** |
| **Steer** the current turn (it's still running) | just type and press **enter** — your note is injected at the next step boundary |
| Queue a **full next turn** while one runs | **shift+enter** (**alt+enter** on legacy terminals — the hint adapts) |
| Pull the queued turn back to steer now | **alt+↑**, or click its orange `queued next` strip, then press **enter** |
| Interrupt the running turn | **esc** |
| Attach an image | paste it (ctrl+v) or paste a path — it becomes an `[Image #N]` chip |
| Mention a workspace file | type `@` after whitespace, then **↑/↓** and **enter** (or **tab**) to insert |

Things worth knowing:

- **The full shortcut list** is one command away: run **/keys** any time. The footer's
  right side stays empty at rest on purpose (item D4) — it only fills in with hints once
  something needs them (a running turn, an open overlay).
- **Steer vs. queue.** A steer (`↳` in the transcript) nudges the *current* turn mid-flight;
  a queued message becomes the *next* turn. Steers that the turn never consumes are
  discarded — they won't fire later as a message you didn't mean to send.
- A queued message shows in an orange strip above the composer (`▹ queued next: "…" · runs
  when this turn ends · alt+↑ recall to steer`) plus a `q1` footer badge, and runs
  automatically when the turn finishes. Only **one** is held at a time. Press **alt+↑**
  or click the strip to recall its exact text into an empty composer; **enter** then steers
  the active turn, while shift/alt+enter queues it again. Recall refuses to overwrite an
  existing draft or a steer that is already waiting.
- **Tool failures do not end an Auto turn.** A failed tool is retained as a red failed row
  with its error detail; the failure result goes back to the model, which can try a fallback
  tool (for example, a denied file edit followed by a shell-based alternative).
- **Tool digests** in the transcript (`Read 4 files · ran 6 shell commands · click to
  expand`) expand on click to show the individual calls; click again to collapse.
- **Final answer marker.** The turn's one authoritative answer opens with a bright/bold
  `● Final answer` heading (never a color-only cue, so it stays legible in any theme) so
  its start is always identifiable, even after you've scrolled away to reread earlier
  context — press **ctrl+f** to jump straight back to it.
- **Big pastes** (>10 lines or >800 chars) collapse to a `[Pasted #N · …]` stub so the
  composer stays readable; the full text is sent verbatim on submit. Deleting the stub
  removes the paste.
- **File mentions** autocomplete bounded, relative workspace paths. They insert an `@path`
  reference into your message; paths containing whitespace are quoted. **Esc** closes the
  suggestions without interrupting a running turn.
- **Prompt history** keeps submitted, steered and queued text for this app session; resumed
  user turns seed it too. While browsing, **↓** eventually restores the draft you were
  typing. A multi-line draft keeps normal vertical cursor movement.

## 4. Modes

Modes are *postures*: they set the agent's working style, tint the composer edge and footer,
and — for the gating postures below — restrict which tools can run via Amplifier's **native
mode system** (`hooks-mode` + `tool-mode`, the same modules the reference bundle mounts).
Cycle with **shift+tab**, or jump with `/mode <name>`, `/plan`, `/brainstorm`.

| Mode | Gating | Use it for |
|---|---|---|
| chat | auto read; ask for other capabilities | Q&A and light work |
| plan | **read-only** — non-read tools blocked | exploring and planning |
| brainstorm | **no tools** — pure text | divergent thinking |
| build | auto read/test; ask write/net/spend/exec/outside-project | hands-on work |
| **auto** *(default)* | auto read/write/test; asks if risky at boundaries | Amplifier's natural wide scope |

The app's posture gate is an Amplifier `tool:pre` hook: it resolves the trust slots shown by
`/permissions`, denies-and-continues when a capability is blocked, and sends asks through
the same `ApprovalBroker` used by mounted modules. Bundle-native modes remain independent:
**plan** and **brainstorm** also activate their matching `hooks-mode` definitions, while
`/mode careful` adds native confirmation rules. The two layers share Amplifier's hook and
approval contracts rather than bypassing the kernel.

`ctrl+p` shows the current posture and `/permissions` prints the effective trust view,
including the `outside-project` slot.

Plan-mode turns that produce a plan end with a `· plan ready` rule. There's no ceremony to
hand it over: the plan is already in the conversation — shift+tab to build and say go.

## 5. Approvals

In the default `auto` posture, read/test calls proceed silently anywhere outside denied
directories — reads are denylist-bounded, not confined to the project — and writes
proceed silently too. Outside the project, the write tools defer to the filesystem
tool's own boundary (a graceful tool error, never an approval) and shell writes roam —
the same defaults as the amplifier CLI. Network, spend and shell actions are
reasoning-blind classifier gates: explicit, safe user requests proceed; destructive or
unrequested boundary crossings deny and defer. Set `permissions.write_boundary: guarded`
in settings to restore app-level gating of outside-project writes.
`chat` and `build` can ask more often, and `/mode careful` or another bundle mode can add
native confirmations. An ask replaces the composer with **Allow once · Allow always ·
Deny**.

- **arrows / tab** select · **enter** confirm · **esc** deny
- **ctrl+y** defers: the current call is denied immediately so work continues, while the
  decision remains in the needs-you queue for a later answer
- Deferred decisions land in the *needs-you* queue (§6), where you can still answer later
- *Allow once* covers just this call; *Allow always* asks Amplifier's approval system to
  remember the decision for that same action going forward

The approval bar owns the keyboard while visible; other shortcuts pause until you decide
or defer it.

## 6. Needs-you: deferred decisions

Denied-and-continued actions and deferred approvals land in the **needs-you queue**
(**ctrl+y** to open, or click the `N decisions waiting · ctrl-y` footer badge). The turn
doesn't stall — the agent routes around the blocked action and a `⊘ blocked` line marks
the spot in the transcript. To answer an item, **click one of its choice chips** (clicking
the row takes the first choice); your decision is injected into the next turn ("Applying
decision …"), so nothing is lost — just deferred. Repeated denials (three in a row, or
twenty in a session) escalate to get your attention.

For a free-text choice, click **Type your own**. A persistent decision band opens above
the composer and temporarily parks your existing draft. **Enter** submits the answer even
while a turn is running, **ctrl+j** adds a newline, and **esc** cancels without interrupting
the turn; submit or cancel restores the original draft exactly. Slash-leading answers are
literal answers, not slash commands.

The structured `question` tool follows the active posture: interactive modes wait for the
answer that the current step needs; **Auto** parks the question and returns control to the
model immediately so independent work continues. If you answer later, the answer is
injected once at the next model boundary.

## 7. Commands

Type `/` to open the command palette (↑↓ select, enter run, esc close — filtering is by
substring as you type). The same commands work typed in full, e.g. `/mode plan`.

| Group | Command | What it does |
|---|---|---|
| During | `/mode [name\|off]` | cycle or jump interaction mode (also activates bundle-native modes) |
| | `/modes` | list available modes and postures |
| | `/plan` | jump to read-only planning |
| | `/brainstorm` | jump to no-tools brainstorming |
| | `/context` | context-window usage grid (conversation / tools / memory / free) |
| | `/status` | live session snapshot — model, mode, messages, tools, cost |
| | `/model [[provider] name]` | list the provider's models, or switch the live model (naming a provider also reroutes turns to it) |
| | `/effort [none…max]` | show or set reasoning effort |
| | `/compact [focus]` | compact the conversation context, optionally focused |
| | `/clear` | clear the transcript view + conversation context (not persisted history) |
| | `/tools` | list the mounted tools |
| | `/agents` | list the delegatable agents |
| | `/skills` | list available skills |
| | `/skill <name> [arguments]` | load a skill into the next live model turn |
| | `/<skill-name>` | every discovered skill is also its own command (plus its `shortcut:` alias) |
| | `/mcp [add\|reload\|remove]` | list effective MCP servers + live connection state; reconcile changes now when safely owned |
| | `/bundle [load NAME_OR_URI]` | list/load registered, deferred, local, or direct bundles into this session |
| | `/module load ID [SOURCE]` | mount one additive provider/tool/hook module into this session |
| Parallel | `/tasks` | toggle the agent lanes panel (ctrl+t) |
| Ship | `/ledger` | session outcome ledger — spend vs. yield summary (ctrl+l) |
| | `/diff [staged]` | working-tree (or staged) git patch with theme-aware highlighting |
| | `/export` | write the transcript as markdown to `exports/` |
| | `/copy` | copy the last answer to the clipboard |
| | `/about` | app / core / bundle / session identity |
| Between | `/rewind` | open the pre-prompt restore picker (ctrl+r) |
| | `/quit` | exit |
| Repair | `/permissions` | show trust slots: boundary, blocks, exceptions |
| | `/allowed-dirs [list\|add PATH\|remove PATH]` | edit allowed write paths for this session |
| | `/denied-dirs [list\|add PATH\|remove PATH]` | edit denied write paths for this session |
| | `/doctor` | setup checkup — install, PATH, platform, Python/uv, permissions, settings; each finding names the exact fix, changes nothing itself |
| | `/improve` | suggests allowlist/trust tweaks from your approval history — never applies silently |
| | `/theme [name]` | switch or cycle theme: slate · graphite · carbon · paper (session-only — resets to slate on restart) |
| | `/keys` | list every keyboard shortcut and what it does |

**Model, effort, compact, clear, status, tools, agents, diff** act on the live
Amplifier session through the coordinator (the same calls the reference CLI
makes). **`/model`** switches the mounted provider's model in place —
`/model <name>` chooses the unique provider advertising that model (and uses
the last-switched provider only to disambiguate a model advertised by several);
`/model <provider> <name>` targets explicitly. When the target is not the provider
currently answering, its routing priority is lowered below the others'
so root turns actually move to it, while delegated roles switch to the matching
provider-family routing matrix. The exact chosen model remains the root model;
the matrix never substitutes one of its role defaults. **`/effort`** sets the orchestrator's
per-turn effort; with no override the provider's own configured effort
applies and is what bare `/effort` and the footer show.
**`/bundle load`** prepares a registered/deferred/local bundle and mounts its
additive providers, tools, hooks, agents, instruction, and context immediately.
**`/module load`** mounts one
additive provider, tool, or hook module, optionally from an explicit source URI.
A provider loaded this way does not silently take over; select its exact root
model with `/model <provider> <model>`. Both live-load paths use a canonical
session ledger and transactional provider remapping, so repeating an aliased
load is a no-op, existing provider identities/order are preserved, and a failed
load is rolled back. Successfully loaded provider/tool/hook entries and new
agent definitions are mirrored into the parent session configuration, so child
lanes created afterward inherit them; cleanup restores both the live mounts and
the inherited configuration. Orchestrator and context *module replacements*
plus explicit agent modules remain next-session-only and are reported as such;
swapping those singleton identities mid-conversation is not additive bundle
composition. Bundle instruction/context prose is rendered through Foundation's
prepared-bundle prompt factory and enters the very next turn as an additive,
hook-origin system message.
**`/compact`** and **`/clear`** drive the context module directly. **`/clear`**
additionally empties the visible transcript in the same action — both the
rendered rows and the live context reset together, immediately, with a brief
confirmation. Neither touches the persisted session log on disk: resume and
`/export` still see every prior turn.
The packaged tui bundle compacts automatically at 80% of the serving
provider's effective request budget. `context.max_tokens` is the 300k fallback
only when the provider exposes no model limit. Override `context.auto_compact`,
`context.compact_threshold`, or that fallback in settings; `/status` labels the
configured fallback and whether accounting is provider-observed or estimated.
After native compaction reveals the provider-derived budget, `/context` and the
footer use it rather than the fallback. Root compaction bursts update one
summary row in place; child-agent maintenance stays in its own event log instead
of flooding the parent conversation.

`/compact` asks the mounted context implementation for a persistent compaction.
The bundled `context-simple` compacts ephemeral request views automatically, so
its explicit protocol method makes no persistent change; the notice says so
rather than claiming messages were removed.

**MCP & skills.** `/mcp` reads the same effective scopes as `tool-mcp` (user
`~/.amplifier/mcp.json`, project `./.amplifier/mcp.json`, environment override,
then inline bundle config). A genuinely new `/mcp add` connects and mounts its
`mcp_<server>_<tool>` capabilities into the current session transactionally;
servers added by this live reconciler can also reload/remove immediately. A server
owned by the aggregate boot-time `tool-mcp` remains connected until restart unless
the mounted module exposes the upstream per-server reconciliation capability; the
notice distinguishes "configuration saved/removed" from "connected" instead of
claiming a live change. `/skills` and `/skill` drive the mounted skills tool and add
the returned inline instructions or fork result to live next-turn context exactly
once — the agent also loads skills on its own when relevant.
Discovered skills additionally register as first-class commands: `/cranky-old-sam`
(and its declared `shortcut:` alias, e.g. `/cosam`) resolves exactly like a built-in —
in the palette, in the help listing, and at the prompt — and loads that skill. Text
after the name or alias forwards to the skill exactly like `/skill <name> <rest>` does.
A shortcut that collides — shared with another skill, or shadowing a built-in — is
reported in the transcript at boot instead of silently dropped, and an unrecognized
`/command` offers the nearest registered match (“did you mean …?”) rather than a bare
rejection.

**Directory capabilities.** The project root is always an implicit allowed write path.
Top-level `amplifier-tui allowed-dirs` / `denied-dirs` commands persist global, project,
or local settings; the slash commands change the current session immediately and persist
under that session for resume. Permission lists union across scopes, denied paths win, and
the mounted filesystem tool is the hard enforcement point. `.git`, `.agents`, `.codex`,
and `AGENTS.md` beneath the project are protected defaults and cannot be reopened by an
approval. The kernel resolves two independent axes for each recognized action: whether it
needs approval and whether its recognizable target satisfies the configured path policy.
Reads are denylist-bounded: the AI may read anywhere outside denied directories. Write
*tools* stay confined to allowed paths — enforced by the mounted filesystem tool itself,
which fails gracefully outside them. By default (`permissions.write_boundary: open`,
amplifier-CLI parity) there is no additional app-level gate: shell writes roam, and only
denied or protected paths are stopped. Setting `permissions.write_boundary: guarded`
restores the app-level gate: outside writes are blocked pre-flight, and shell calls are
checked for recognizable absolute, home-relative, parent-relative and redirection paths —
write-shaped commands (write-command heads, redirection targets) get gated outside the
project while read-shaped commands still roam. Neither posture is an operating-system
sandbox around arbitrary interpreter code.

## 8. Keys

| Key | Does | When |
|---|---|---|
| enter | send · steer · confirm | idle · running · in panels |
| shift+enter (alt+enter) | queue next-turn message | any time |
| alt+↑ | recall queued next-turn text so Enter can steer it now (or send it if the prior turn already ended) | queued message visible |
| ctrl+j (ctrl+enter) | newline in composer | composing |
| ↑ / ↓ | older/newer prompt; restore draft | single-line composer |
| ↑ / ↓ | move file suggestion | `@file` suggestions open |
| tab | insert selected file path | `@file` suggestions open |
| shift+tab | cycle mode | any time |
| ctrl+p | show trust posture | any time |
| ctrl+t | agent lanes panel | any time |
| ctrl+o | cycle which running agent the live tail follows | agents fanned out |
| ctrl+l | outcome ledger | any time |
| ctrl+y | needs-you queue | any time |
| ctrl+r | checkpoint restore picker | any time |
| ctrl+f | jump back to the current turn's final-answer start | any time |
| esc esc | open restore picker; while running, interrupt first | running; idle with empty composer |
| ↑ ↓ | select in palette/lanes (lanes from an empty composer) | panels |
| ‹ › (← →) | navigate checkpoints · evidence refs | restore picker · evidence |
| d | open/refresh/close evidence detail panel | evidence |
| ctrl+c | copy mouse-selected transcript text | after selecting |
| ctrl+d | quit | any time |
| esc | one step "out" | see below |

**Esc does the nearest thing first:** leave a focused lane → close the palette → close the
restore picker → close the lanes panel → interrupt the running turn. During an approval,
esc means *deny*. With an empty composer, press Esc twice within 750ms to open the same
picker used by ctrl+r. During a turn the first Esc requests an interrupt and the second
opens the picker while close-out finishes. With a draft at idle, double-Esc clears it but
keeps it recoverable with **↑**, rather than opening a restore over text you may still need.

An accepted turn interrupt is also recorded in model context as a hidden
`<turn_aborted>` boundary. The next turn therefore knows the prior response was cut off and
is told to verify any possibly partial tool effects before retrying; the transcript keeps
the human-facing interrupted recap instead of exposing the marker.

While the **approval bar** is open it owns the keyboard: the "any time" shortcuts above
pause, and tab/shift+tab move the approval selection instead.

*(shift+enter requires a modern terminal — kitty, WezTerm, foot, Ghostty, recent
iTerm2/Windows Terminal. Elsewhere use alt+enter; the app detects this and adjusts its
hints.)*

## 9. Agent lanes (subagents)

When the agent fans work out to subagents, the **lanes panel** opens automatically (or
toggle with **ctrl+t**): one live row per agent — state glyph (◐ running · ■ working ·
✔ done · `!` attention · ✖ failed · ⊘ cancelled), producing turn, current
activity, elapsed time, tokens, cost. A child waiting for approval or continuing after a
denied/blocked action turns its own row orange and names that need; the same global
approval bar remains where you answer it.

Child tool and stream events update that row and its compact transcript-tree row in place
(`reading README.md`, `editing reducer.py`, `writing response`) without accumulating status
lines. Successful native file edits roll into one `Changed N files · click to expand` row;
click it (or focus it and press **enter**) for bounded, theme-aware red/green details.

While agents run and the root model is quiet, the area under the transcript shows a live
**tail** of one agent's stream — up to three dim `┆`-prefixed lines, so you can always see
the work happening. It follows whichever running agent spoke most recently; press
**ctrl+o** to pin it to a different one (the `▸` after a lane's name marks who you're
tailing — also shown in the panel header hint). The moment the root model speaks, the tail
switches back to it. Every live stream names its source and turn: child identity sits on
the containing lane row/focus banner, while the root peek and revealed box say
`main · tN`. Tail text is a live preview only: the agent's full prose lives in its own
transcript (focus the lane to read it), and neither the preview nor its identity label lands
in your durable answer.

Select a lane with ↑↓ and press **enter** (or click its row) to *focus* it: the transcript
switches to that subagent's own work, with a **‹ Back to parent** control at the top — click it,
or press **esc**, to return; nothing left open, esc interrupts the whole agent tree instead.
Focusing is pure navigation: it never ends the subagent's turn or the session, the parent
transcript keeps accumulating underneath, and returning — to the same lane or a different one —
restores exactly where you left off (scroll position included) rather than snapping to the
latest line. The first time you ever focus a lane, a one-off notice calls out the esc/Back exit
path; it does not repeat and never sits onscreen as a permanent overlay.

The transcript itself keeps one **delegate summary** line per fan-out: `● 2 delegates
running…` while work is in flight, then `● Used 2 delegates · Plan 3/4 · 1m 12s ▸` when
it settles. Click it (or focus it and press **enter**) to expand the agent tree in
place — each agent's outcome glyph, elapsed time, and a snippet of its final answer,
plus the final plan on one line. Click again to collapse.

## 10. Rewind

A checkpoint is cut **before every prompt starts**, then attached to that turn's rule line
when it closes. The pending checkpoint is already selectable while its turn is running,
including for the first prompt in a session. Selecting `before turn 3`, for example, means
“undo turn 3 and everything after it,” not “keep turn 3.”

Open the bottom restore picker with **ctrl+r**, `/rewind`, a click on a turn rule, or
**esc esc** while the composer is empty. Use **←/→** (or click **‹/›**) to choose the
prompt, **↑/↓** to choose a scope, and **enter** to restore:

| Scope | Conversation | Workspace files |
|---|---|---|
| **code + conversation** *(default)* | restored to immediately before the selected prompt | safely restores tracked direct edits from that prompt onward |
| **conversation only** | restored to immediately before the selected prompt | left exactly as they are |
| **code only** | left exactly as it is | safely restores tracked direct edits from that prompt onward |

When conversation is included, the selected prompt returns to the composer for editing and
resubmission; the selected turn and all later transcript/ledger entries disappear only after
the context restore succeeds. Code-only restore does not rewrite the conversation or
composer. If a turn is active when you confirm, the app requests the normal graceful
interrupt, waits for close-out, and then restores. Opening and browsing the picker alone
never interrupts anything. A queued next-turn message remains queued; because the restored
prompt now occupies the composer, it is not silently run ahead of your revision.

If a **code + conversation** restore can safely restore only some files, those successful
file restores stay applied, but the conversation remains in place so the same visible
checkpoint can be retried after you resolve the skipped paths. The notice says **partial
restore** and includes the first concrete warning; it never presents a mixed result as a
complete rewind.

### What code undo covers

The code checkpoint store watches only **root-session structured file tools** whose target
is known before execution: `write_file`, `edit_file`, `create_file`, `delete_file`, and
`apply_patch`. It records private preimages before the tool runs, then uses a strict
compare-and-swap check at restore time. If a current file no longer matches the recorded
after-state — because you, a shell command, another process, or another agent changed it —
that file is **skipped**, never overwritten. Other independent files can still restore.
The completion notice reports restored and skipped counts, and unsafe/conflicting paths are
surfaced as warnings rather than being presented as successful undo.

The following are deliberately not checkpointed:

- `bash`/shell or arbitrary interpreter changes;
- subagent/child-session writes;
- MCP, external-tool, editor, or other manual changes;
- paths outside the workspace, anything under `.git`, and symlinked paths;
- hard-linked files, directories/devices/other non-regular files, and files over 8 MiB;
- files with extended attributes, extended ACLs, non-default file flags, or ownership the
  current process cannot reproduce safely.

On supported POSIX systems (including macOS), each parent directory is held by a no-follow
descriptor through capture or restore, so swapping an intermediate path to a symlink cannot
redirect the operation. A host without the required descriptor-relative primitives skips
code restore paths rather than falling back to an unsafe path-based write.

Checkpoint data lives inside the session's private `workspace-checkpoints/` directory
(0700 directories and 0600 files), survives `resume`, and retains the newest **100**
restore points. It is removed with the containing session. Rewind has **no redo stack**,
cannot reconstruct untracked mutation sources above, and is not a replacement for commits,
branches, or a clean Git working tree. Use Git when you need durable, inspectable history.
One prompt can retain at most 512 preimages / 64 MiB in total, in addition to the 8 MiB
per-file limit; anything beyond those bounds is reported as skipped.

Structured turns and restores targeting the same workspace share an exclusive checkpoint
lease, even across two TUI sessions. If another turn owns that lease, or the pre-prompt
checkpoint cannot be made durable, the new message is returned to the composer and is
**not sent**. Restore journals and branch intents are replayed on resume before another
checkpoint operation is allowed, so a process exit cannot silently bless a half-finished
undo.

## 11. Watching cost and yield

- The **footer** shows running session cost.
- **ctrl+l / `/ledger`** shows the session ledger — turns, total spend, how many turns
  shipped changes vs. answered, cache hit rate. Per-turn cost and yield (files changed,
  `+added/−removed`, tests run) appear on each turn's rule line as it completes.
- **`/context`** shows what's occupying the context window.
- Costs come from provider-reported figures when available, otherwise a live pricing
  table (fetched and cached at `~/.amplifier/pricing_cache.json`, on by default),
  otherwise a built-in offline table; resumed sessions restore their prior spend.

## 12. Evidence

**Click any answer** to reveal its evidence: a block opens (and takes the keyboard)
listing each claim and the tool call that backs it — `· Evidence 1/N · ←/→ select ·
enter expand · d detail · esc close`. **enter** jumps to and expands the tool line
grounding the selected claim in the transcript itself. Answers with no recorded
evidence say so in a notice.

**Press d** to open the **evidence detail panel**, docked beside the transcript, for
the currently-selected claim: the producing tool call and its input/query summary,
when it ran, its source/output, and the originating agent. **←/→** while the panel is
open re-targets it to a different claim; **d** again on the same claim closes it and
restores your scroll position and keyboard focus to that evidence row. On a narrow
terminal (under 80 columns) `d` shows a notice instead of squeezing the transcript
unreadably thin, and an already-open panel collapses (without losing its content) if
you resize below that width, reappearing when you widen back out. If a claim's
grounding tool call has no correlation id, is no longer resolvable, or its output is
too large to show inline, the panel says so explicitly — never a blank or dead panel.

## 13. Copying and exporting

- **`/copy`** — last answer to clipboard.
- **Mouse-select** transcript text, then **ctrl+c** — a `copied · N chars` notice confirms.
- **`/export`** — the whole transcript as markdown in `exports/`.

Copies are written two ways at once — through your OS clipboard tool (pbcopy / wl-copy /
xclip) *and* OSC 52 — so a local copy nearly always lands. The OSC 52 path is what matters
over SSH: there, on iTerm2, enable *Settings → General → Selection → "Applications in
terminal may access clipboard"*. On terminals with the kitty keyboard protocol ⌘C reaches
the app and copies too; elsewhere use ctrl+c, or hold ⌥/Shift while dragging to use the
terminal's native selection.

## 14. Sessions

Sessions persist under `~/.amplifier/projects/<project>/sessions/` — transcript, metadata,
a full event log, and private workspace-checkpoint data. Saving is incremental (after every
tool call), so even a crash loses almost nothing. `sessions` lists them; `resume SESSION_ID`
picks one back up with history, cost, and the newest 100 restore points intact. A prefix
works too (`resume abc123`); an ambiguous prefix lists every match instead of guessing, and
`resume`/`session resume`/`run --resume`/`serve --resume` all use distinct, stable exit codes
(0 ok, 2 no match, 3 ambiguous, 4 corrupt) instead of a blanket 1 — see the table below.

| Exit code | Meaning |
|---|---|
| `0` | Resumed (or launched) fine; the session's own exit status then applies |
| `2` | No stored session matches the given id/prefix |
| `3` | The prefix matches more than one stored session (candidates are listed) |
| `4` | The match is unambiguous but its metadata is corrupt, even from backup |

If an interruption lands after the assistant emitted tool calls but before their results
were saved, resume repairs that transcript boundary before the first new model request. It
persists an uncertainty result for each orphaned call and shows a warning such as
`Resume repaired 2 interrupted tool results`. The repair deliberately does **not** claim
whether the tools executed: inspect the actual disk or external state before retrying any
side effect. Re-resuming is idempotent, and a real stored result is never replaced.

In-session, `/sessions` opens an interactive picker over this project's stored roster
(never just a wall of ids): **↑/↓** clearly moves the highlighted row; **enter** (or a row
click) opens its detail, while **r** (or the trailing **⟳**) actually resumes it. Resume
cleanly closes the current runtime, then reopens the selected stored session through the
same fresh-runtime path as `amplifier-tui resume SESSION_ID`; the equivalent command is
copied to the clipboard as a fallback, but there is nothing else to paste or run.

Opening detail shows the full id on its own line (the table itself only shows a short
8-character id) plus name, bundle, message/turn counts and age; the full id is copied where
the terminal allows it and is always safely mouse-selectable. A session whose stored data
could not be read is never dropped or shown as healthy: it lists with an explicit
**recovered**, **transcript lost**, **indexing**, or **corrupt** state. Rows without enough
trusted identity to resume remain in the picker for inspection, but **r** refuses them with
a clear notice instead of closing into a predictable boot failure.

## 15. When something's off

| Symptom | Try |
|---|---|
| Boot fails with a provider error | `uv run amplifier-tui doctor` — usually missing keys in `~/.amplifier/keys.env` |
| shift+enter sends instead of queueing | legacy terminal — use **alt+enter** |
| Copy does nothing over SSH | enable the iTerm2 clipboard setting above (locally the OS clipboard tool is also used, so this mostly bites remote sessions) |
| Some tools missing at start | the banner will say so — the bundle partially mounted; doctor explains |
| Too many approval prompts | `/improve` suggests safe allowlist entries; `/permissions` to review trust |
| Want to poke around risk-free | `--demo` |
