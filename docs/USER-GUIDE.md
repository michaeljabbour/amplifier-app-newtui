# User Guide

How to drive the Amplifier TUI day to day: modes, steering, approvals, subagent lanes,
rewind, and every key and command. For install/provider setup see the
[README](../README.md); for how it works under the hood see
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Launching

```sh
uv run amplifier-newtui              # full-screen TUI, real session
uv run amplifier-newtui --demo       # scripted demo — no credentials needed
uv run amplifier-newtui --bundle B   # pick a bundle by name or URI
uv run amplifier-newtui sessions     # list stored sessions for this project
uv run amplifier-newtui resume ID    # resume a stored session
uv run amplifier-newtui run "PROMPT" # headless one-shot, prints the answer
uv run amplifier-newtui doctor       # setup checkup (exit 1 when findings exist)
```

**First run:** follow the [README's Install section](../README.md#install) — it deploys
[Amplifier](https://github.com/microsoft/amplifier) first (`amplifier init` sets up your
provider and credentials in `~/.amplifier/`, which this app shares), then this app. If
anything is off, `doctor` will tell you what and how to fix it. Not sure everything's
wired? `--demo` always works and exercises the whole UI offline.

## 2. The screen

```
┌ title bar ── spinner · state — bundle — session id ─────────────────┐
│                                                                     │
│  transcript — your lines, activity digests, plans, answers,         │
│               turn rules (one per turn, clickable → rewind)         │
│                                                     ┌ notices ┐     │
├─ overlay strips appear here (palette / lanes / rewind / queued) ────┤
│ [mode] ❯ composer — type here            (swaps to approval bar)    │
├─────────────────────────────────────────────────────────────────────┤
│ mode · trust · bundle · $cost                     contextual hints  │
└─────────────────────────────────────────────────────────────────────┘
```

The footer always shows your current mode, trust posture, and session cost on the left, and
the keys that work *right now* on the right. The hints change with context — when in doubt,
read the footer.

## 3. Talking to Amplifier

| You want to… | Do this |
|---|---|
| Send a message | type, **enter** |
| Add a newline while composing | **ctrl+j** |
| **Steer** the current turn (it's still running) | just type and press **enter** — your note is injected at the next step boundary |
| Queue a **full next turn** while one runs | **shift+enter** (**alt+enter** on legacy terminals — the hint adapts) |
| Interrupt the running turn | **esc** |
| Attach an image | paste it (ctrl+v) or paste a path — it becomes an `[Image #N]` chip |

Things worth knowing:

- **Steer vs. queue.** A steer (`↳` in the transcript) nudges the *current* turn mid-flight;
  a queued message becomes the *next* turn. Steers that the turn never consumes are
  discarded — they won't fire later as a message you didn't mean to send.
- A queued message shows in an orange strip above the composer (`▹ queued next: "…" · runs
  when this turn ends`) plus a `q1` footer badge, and runs automatically when the turn
  finishes. Only **one** is held at a time — queueing again replaces it (that's also how
  you "edit" it; there's no cancel).
- **Tool digests** in the transcript (`Read 4 files · ran 6 shell commands · click to
  expand`) expand on click to show the individual calls; click again to collapse.
- **Big pastes** (>10 lines or >800 chars) collapse to a `[Pasted #N · …]` stub so the
  composer stays readable; the full text is sent verbatim on submit. Deleting the stub
  removes the paste.
- There is **no input history** on ↑/↓ — on an empty composer those keys navigate the
  agent lanes panel instead.

## 4. Modes

Modes are *postures*: they change what the agent is allowed to do, its tone of work, and
they tint the composer edge and footer. Cycle with **shift+tab**, or jump with
`/mode <name>`, `/plan`, `/brainstorm`.

| Mode | Trust | Use it for |
|---|---|---|
| chat | ask all · auto read | Q&A; every action asks first |
| plan | read-only | exploring and planning — nothing gets written |
| brainstorm | no tools | pure divergent thinking |
| build | auto read,test · ask write,net,spend | hands-on work with confirmation on writes |
| **auto** *(default)* | auto read,write · classifier-gated | amplifier's natural wide scope |

In **auto**, reads/writes/tests run freely; network, spend, and exec actions pass through a
safety classifier that only sees your messages and the proposed action. Destructive shapes
(`rm -rf`, force-push, `curl | sh`) and a `git push` you never asked for are denied and
parked for your review (§6).

Want to see where you stand? **ctrl+p** shows your current trust posture at a glance
(`trust · <summary> · edit via /permissions`), and `/permissions` prints the full
per-capability picture — each trust slot (read / test / write / net / spend / exec) with
its current decision, plus the project boundary.

Plan-mode turns that produce a plan end with a `· plan ready` rule. There's no ceremony to
hand it over: the plan is already in the conversation — shift+tab to build and say go.

## 5. Approvals

When the agent needs your sign-off, the composer is replaced by an **approval bar** showing
what it wants to run and three options — **Allow once · Allow always · Deny**.

- **arrows / tab** select · **enter** confirm · **esc** deny
- Unanswered approvals time out to **deny** — never to allow. Timed-out and deferred
  decisions land in the *needs-you* queue (§6), where you can still answer them later
- *Allow once* covers just this call; *Allow always* asks Amplifier's approval system to
  remember the decision for that same action going forward

The approval bar owns the keyboard while visible; other shortcuts pause until you decide.

## 6. Needs-you: deferred decisions

Denied-and-continued actions and deferred approvals land in the **needs-you queue**
(**ctrl+y** to open, or click the `N decisions waiting · ctrl-y` footer badge). The turn
doesn't stall — the agent routes around the blocked action and a `⊘ blocked` line marks
the spot in the transcript. To answer an item, **click one of its choice chips** (clicking
the row takes the first choice); your decision is injected into the next turn ("Applying
decision …"), so nothing is lost — just deferred. Repeated denials (three in a row, or
twenty in a session) escalate to get your attention.

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
| Parallel | `/tasks` | toggle the agent lanes panel (ctrl+t) |
| Ship | `/ledger` | session outcome ledger — spend vs. yield summary (ctrl+l) |
| | `/export` | write the transcript as markdown to `exports/` |
| | `/copy` | copy the last answer to the clipboard |
| | `/about` | app / core / bundle / session identity |
| Between | `/rewind` | open the rewind picker (ctrl+r) |
| | `/quit` | exit |
| Repair | `/permissions` | show trust slots: boundary, blocks, exceptions |
| | `/doctor` | setup checkup — reports findings and the fixes to make; changes nothing itself |
| | `/improve` | suggests allowlist/trust tweaks from your approval history — never applies silently |
| | `/theme [name]` | switch or cycle theme: slate · graphite · carbon (session-only — resets to slate on restart) |

## 8. Keys

| Key | Does | When |
|---|---|---|
| enter | send · steer · confirm | idle · running · in panels |
| shift+enter (alt+enter) | queue next-turn message | any time |
| ctrl+j | newline in composer | composing |
| shift+tab | cycle mode | any time |
| ctrl+p | show trust posture | any time |
| ctrl+t | agent lanes panel | any time |
| ctrl+l | outcome ledger | any time |
| ctrl+y | needs-you queue | any time |
| ctrl+r | rewind picker | any time |
| ↑ ↓ | select in palette/lanes (lanes from an empty composer) | panels |
| ‹ › (← →) | navigate checkpoints · evidence refs | rewind · evidence |
| ctrl+c | copy mouse-selected transcript text | after selecting |
| esc | one step "out" | see below |

**Esc does the nearest thing first:** leave a focused lane → close the palette → close
rewind → close the lanes panel → interrupt the running turn. During an approval, esc means
*deny*.

While the **approval bar** is open it owns the keyboard: the "any time" shortcuts above
pause, and tab/shift+tab move the approval selection instead.

*(shift+enter requires a modern terminal — kitty, WezTerm, foot, Ghostty, recent
iTerm2/Windows Terminal. Elsewhere use alt+enter; the app detects this and adjusts its
hints.)*

## 9. Agent lanes (subagents)

When the agent fans work out to subagents, the **lanes panel** opens automatically (or
toggle with **ctrl+t**): one live row per agent — state glyph (◐ running · ■ working ·
✔ done), current activity, elapsed time, tokens, cost.

Select a lane with ↑↓ and press **enter** to *focus* it: the transcript switches to that
subagent's own work. **esc** steps back out — first unfocusing the lane, then closing the
panel; with nothing left open, esc interrupts the whole agent tree.

## 10. Rewind

Every turn ends with a rule line and a checkpoint. To go back:

- press **ctrl+r** (or `/rewind`), or click any turn rule in the transcript
- navigate checkpoints with **‹ ›**, then **enter** to fork

Rewind is **confirm-then-trim**: the session forks from that checkpoint first, and only
after that succeeds is the transcript trimmed. A failed fork changes nothing. Cost and
ledger accounting roll back with it.

## 11. Watching cost and yield

- The **footer** shows running session cost.
- **ctrl+l / `/ledger`** shows the session ledger — turns, total spend, how many turns
  shipped changes vs. answered, cache hit rate. Per-turn cost and yield (files changed,
  `+added/−removed`, tests run) appear on each turn's rule line as it completes.
- **`/context`** shows what's occupying the context window.
- Costs come from provider-reported figures when available, otherwise a built-in pricing
  table; resumed sessions restore their prior spend.

## 12. Evidence

**Click any answer** to reveal its evidence: a block opens (and takes the keyboard)
listing each claim and the tool call that backs it — `· Evidence 1/N · ←/→ select ·
enter expand · esc close`. **enter** jumps to and expands the tool line grounding the
selected claim. Answers with no recorded evidence say so in a notice.

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
and a full event log. Saving is incremental (after every tool call), so even a crash loses
almost nothing. `sessions` lists them; `resume ID` picks one back up with history, cost,
and checkpoints intact.

## 15. When something's off

| Symptom | Try |
|---|---|
| Boot fails with a provider error | `uv run amplifier-newtui doctor` — usually missing keys in `~/.amplifier/keys.env` |
| shift+enter sends instead of queueing | legacy terminal — use **alt+enter** |
| Copy does nothing over SSH | enable the iTerm2 clipboard setting above (locally the OS clipboard tool is also used, so this mostly bites remote sessions) |
| Some tools missing at start | the banner will say so — the bundle partially mounted; doctor explains |
| Too many approval prompts | `/improve` suggests safe allowlist entries; `/permissions` to review trust |
| Want to poke around risk-free | `--demo` |
