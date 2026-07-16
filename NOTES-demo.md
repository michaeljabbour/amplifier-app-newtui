# NOTES from the demo-runtime agent (kernel/demo.py)

For the integrator and the UI builders. I own `amplifier_app_newtui/kernel/demo.py`
and `tests/test_kernel_demo_*.py` only.

## What DemoRuntime emits (conventions the UI must key on in --demo mode)

1. **Assistant text** arrives on BOTH channels per ADR-0007: full Channel-A
   stream (`stream_block_start/delta/end`) + durable `content_block_end`. The
   presentation role travels in `StreamBlockStart.name` and
   `ContentBlockEnd.block["demo_role"]`: `narration` | `answer` | `recap` |
   `idea`. The real runtime leaves `name` empty — the transcript builder should
   treat the role marker as a hint with a heuristic fallback (text before tools
   = narration, last text = answer).
2. **Plan checklists** are `update_plan` tool calls:
   `tool_input = {"title", "read_only", "steps": [{"step", "status"}]}` with
   statuses `pending|active|done`. Each state change is a fresh pre/post pair.
   The plan turn's head lands with zero steps, then steps stream in (mockup's
   500ms cadence).
3. **Shell commands** are `bash` tool calls (`tool_input["command"]`). The live
   `└ $ cmd` line spans ToolPre→ToolPost; successful posts carry
   `result={"output": "(output collapsed)"}`. The seed turn's two commands
   share one `parallel_group_id` → render as one `Ran 2 shell commands` batch.
4. **Blocks (⊘)** are deny-and-continue: `ToolPre` → `ToolPost(result=
   {"status": "denied", "reason", "continuation"})` + `ApprovalDenied`. The
   chat-mode pytest deny has NO ToolPre (approval precedes the call, as in the
   mockup); render the blocked line from `ApprovalDenied` there.
5. **Deferred decision (auto turn)**: after the force-push block, a
   `Notification(level="decision", source="needs_you", message="decision
   deferred to queue · run continues")` fires. The needs-you block content
   (entry text, `[yes · push to fork]` chip, "Applying decision: …" narration)
   is exported as `DEMO_DEFERRED_DECISION`.
6. **Mode switches** emit `Notification(source="mode")` with the exact
   `mode <id> · <trust>` text BEFORE the turn's `prompt_submit`. The UI should
   set its mode posture from these in demo mode.
7. **Turn-end order**: answer text → recap text → `orchestrator_complete` →
   `execution_end` → `prompt_complete` → optional `Notification(source="turn")`
   end notice. The auto (blocked) turn deliberately has NO end notice
   (mockup parity); build = `agents 1 done`, agents = `agents 3 done · click a
   lane to inspect its transcript`.
8. **Telemetry ticks**: one `provider_response_usage` per virtual second while
   a turn is "working" (`output_tokens` per the mockup formulas — see
   `tick_tokens`). Plan/brainstorm/seed emit a single usage event with the
   headline token count instead (no live ticking in the mockup for those).

## Rule labels / cost: use the exported specs, not derivation

The mockup's turn-rule labels hardcode cached% and cost (e.g. `$0.13` from
`0.04 + secs*0.01`) — they are NOT derivable from the usage events alone. In
demo mode, close out each turn rule from `DEMO_TURNS` / `DEMO_TURN_BY_KEY`
(`rule_label`, `outcome`, `shipped`, `checkpoint_id`, `checkpoint_label`,
`cost`, `cost_after`). `build_denied_spec()` is the alternate close-out when
the user denies the pytest approval. Footer cost = `cost_after` (session
starts at `DEMO_SESSION_COST_START = $0.57`, seed's $0.17 pre-baked).

## Lane focus transcripts come from DEMO_LANES (not child events)

`AgentSpawned/AgentCompleted` are emitted with hierarchical
`sub_session_id`s, but per-lane logs are NOT replayed as child-session
events — the mockup shows them as static focus data. `DEMO_LANES` carries
everything: `panel_line` (spacing verbatim), `brief` (the `[delegated]` line),
`log` rows (narration/tool/command/answer), `state_recap`, `tree_spawn`/
`tree_done` labels, `done_at_ms`. `color_token` is the theme-token NAME
(teal/fg/dim) — resolve via ui/themes, never hex.

## Approvals

`DemoRuntime(approver=...)` is the interactive seam: the app's approval bar
should be wired as the approver (`async (prompt, options) -> choice`). Default
is auto-`Allow once` so `--demo` runs unattended. Options tuple is the
verbatim `("Allow once", "Allow always", "Deny")`.

## Not scripted (out of scope for the five turns)

- **Interrupt (esc)** and **steer/queue mid-turn** are interactive behaviors,
  not part of the mockup's five scripted turns. If the demo app wants them,
  extend DemoRuntime with an `interrupt()`/`steer()` seam — the virtual-clock
  `_wait` loop is the place to check flags. The `· interrupted` rule label and
  steer strings are in DESIGN-SPEC §3.
- The "approval required · choose below the transcript" notice on approval
  open is UI behavior (spec §7) — emit it app-side when `approval_required`
  arrives; DemoRuntime does not send it.
- `DEMO_BANNER` is the mockup-verbatim session banner for demo mode; real mode
  should build the banner from actual version/bundle info.

## Contract concerns for the contracts agent

- None blocking. The 28-event union covered everything; I did not need to
  extend it. If a first-class `plan:update` or needs-you event is ever added,
  demo.py conventions 2 and 5 above are the migration points.
