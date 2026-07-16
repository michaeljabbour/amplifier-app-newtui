# NOTES from the commands agent

Owner of `amplifier_app_newtui/commands/` (registry, builtin, context,
doctor, improve, permissions) + `tests/test_commands_*.py`. Entries below
are requests/contracts for files I do NOT own.

## For the integrator (main.py / ui/app.py)

1. **`amplifier-newtui doctor` subcommand**: `main.py` is currently a single
   `click.command`. Convert it to a `click.group` (default = TUI launch) and
   wire a `doctor` subcommand to
   `amplifier_app_newtui.commands.doctor.run_standalone()` — it prints the
   plain-text report and returns the exit code (0 = no findings, 1 =
   findings; opencode doctor convention). It accepts injected
   `mcp_stats` / `approval_tallies` / `settings_paths` when the app has real
   data; bare `run_standalone()` runs the environment checks only.
2. **CommandContext implementation**: the app must implement
   `commands.registry.CommandContext` (runtime-checkable Protocol). Data
   surfaces: `ledger`, `denial_log`, `steering`, `needs_you`,
   `session_short`, `bundle_name`, `next_block_id()`, `context_usage()` (a
   `commands.context.ContextUsage`), `approval_tallies()` /
   `overridden_denials()` (from a `commands.improve.ApprovalJournal`),
   `mcp_server_stats()` (`commands.doctor.McpServerStats` rows). Actions:
   `echo_user_line`, `post_block`, `show_notice`, `cycle_mode`, `set_mode`,
   `toggle_lanes`, `open_rewind`, `open_permissions` — implement these as
   Textual message posts, per ADR-0007 (widgets own their state).
3. **Dispatch**: composer/palette should call
   `registry.parse_and_run(ctx, text)` for raw `/xxx args` input, or
   `registry.run(spec.name, ctx, args)` for a selected palette row.
   `run()` echoes the user line itself (DESIGN-SPEC §6 "echoes it as a
   user line first") — do NOT echo again in the UI.
4. **Palette**: rows come from `registry.filter_rows(query)` (substring on
   the command name, mockup semantics); group headers only when
   `registry.show_group_headers(query)` (filter exactly `/`), rendered from
   `registry.grouped_rows("/")` in `GROUP_ORDER`
   (During · Parallel · Ship · Between · Repair).
5. **Keybind single-source**: `registry.keybound()` maps keymap action ids →
   specs for `cycle_mode`, `toggle_lanes`, `show_ledger`, `open_rewind`.
   `tests/test_commands_builtin.py::test_key_actions_exist_in_keymap`
   cross-checks these against `ui/keymap.py`.
6. **`tests/conftest.py`** now exists (I created it) with the
   `fake_command_context` fixture. It is shared — add fixtures, don't
   replace.

## For the kernel governance hook owner

7. **`commands.permissions.PermissionSurface`** is the editable trust
   surface behind `/permissions`. The governance hook should call
   `surface.resolve_call(tool_name, tool_input)` on `tool:pre` — precedence
   blocks → exceptions → user slot override → mode default
   (`model.trust.resolve`). "Allow always" grants should land in
   `surface.add_exception(pattern)` (tool name, or 2-token command prefix).
   The `boundary` field is data-only; within/outside-project enforcement
   stays in the kernel hook (see NOTES-contracts.md item 3).
8. **`commands.improve.ApprovalJournal`** is the recorder feeding /doctor
   and /improve: call `record_ask(action, approved=..., capability=...)` on
   every approval resolution and `record_override(action)` whenever a policy
   denial is later reversed (retro-answered needs-you decision).
   `journal.overrides(denial_log)` builds the trust-slot evidence rows.

## Contract concerns / decisions

- The markdown spec (`docs/tui-v3-cohesive.md` §Palette) mentions a
  `Setup` group and an `mcp` tag; the HTML mockup's COMMANDS array and
  DESIGN-SPEC §6 use only the five groups and `built-in`/`skill`. I
  followed DESIGN-SPEC + the HTML mockup (the compliance ground truth).
  Adding `Setup`/`mcp` later is an additive Literal change in
  `commands/registry.py`.
- `/doctor`'s description says "reports, then fixes on confirm"; the
  implemented surface is report-only (DESIGN-SPEC: "nothing changed yet").
  The confirm-and-fix flow needs a needs-you/approval interaction the
  integrator owns; `DoctorReport` keeps `CheckResult.name` per finding so a
  fixer can key off it.
- `ImproveBlock`/`DoctorBlock` carry rows only; the header lines
  (`Improve  from ledger + denial log · proposes, never applies silently`,
  `Doctor  <n> findings · nothing changed yet` via `DoctorReport.headline()`)
  are the transcript renderer's to draw with the `· ` blue glyph.
- `ContextBlock` is built with `bar_width=20` (the mockup's 20-cell bar)
  even though the model default is 10; segments always sum to `bar_width`
  and non-zero buckets keep >= 1 cell.
