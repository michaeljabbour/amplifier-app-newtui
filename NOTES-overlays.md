# NOTES from the overlays agent (palette / lanes / rewind / queued / needs-you)

Owner of: `ui/palette.py`, `ui/lanes_panel.py`, `ui/rewind_strip.py`,
`ui/queued_strip.py`, `ui/needs_you.py` and tests
`test_ui_palette*.py` / `test_ui_lanes*.py` / `test_ui_rewind*.py`.

## Integration contract for app.py

1. **Theme prerequisite.** Every strip's `DEFAULT_CSS` uses spec-token theme
   variables (`$rule`, `$bg-tab`, `$bg-page`, `$orange`, `$dimmer`, `$green`).
   The app MUST `register_themes(app)` and set
   `app.theme = theme_id(DEFAULT_THEME)` before these widgets mount (the test
   host apps do both in `App.__init__` — that works and is the recommended
   pattern).
2. **Palette is a controlled component** (slaved to composer text, like the
   mockup): call `PaletteStrip.apply_filter(text)` on every composer change
   (`None`/non-`/` closes; zero matches auto-hide). It only posts messages:
   - `PaletteStrip.CommandRun(command)` → app echoes the command as a user
     line (spec §6), clears the composer, calls `apply_filter(None)`, then
     dispatches to the commands registry handler.
   - `PaletteStrip.Closed` (esc) → app clears the composer `/` text and calls
     `apply_filter(None)`.
3. **LanesPanel / RewindStrip self-hide** on esc (and on fork). Host re-shows
   via `show_panel()` / `show_checkpoints(ledger.checkpoints[, index])`.
   Messages: `LanesPanel.FocusLane(name, session_id=…)`, `LanesPanel.Closed`,
   `RewindStrip.ForkRequested(checkpoint_id)`, `RewindStrip.Closed`.
   Fork is request-only — app must do the backend fork first, then trim
   (confirm-then-trim, ADR-0007).
4. **Key routing.** Each strip carries its own `BINDINGS`
   (up/down/enter/escape, left/right for rewind) active when the strip has
   focus; `show_panel`/`show_checkpoints` focus the strip. If the app instead
   keeps composer focus while a strip is open, dispatch keymap actions to the
   public methods: `move_selection(±1)` / `run_selected()` (palette),
   `move_selection(±1)` / `focus_selected()` (lanes), `nav(±1)` / `fork()` /
   `close_strip()` (rewind). Esc precedence stays with `keymap.ESC_CHAIN`.
5. **QueuedStrip** is display-only: `show_queued(text)` / `clear_queued()`;
   `SteeringQueue` owns the state; footer `· q1` badge is the footer's job.
6. **NeedsYouList** renders a `model.blocks.NeedsYouBlock` (map queue
   `NeedsYouItem`s → `NeedsYouEntry`/`NeedsYouChoice` per NOTES-contracts #4).
   Chip clicks post `NeedsYouList.DecisionTaken(item_id, choice)`; the app
   answers the queue and logs `applying_decision_line(...)` narration.
   `focused_lane_banner(_parts)` in the same module is the spec §8 banner
   helper for the focused-lane transcript swap.

## Contract alignment done here

- `ui/palette.py`'s `CommandSpec` Protocol uses the field name **`desc`**
  (not `description`) to structurally match
  `commands.registry.CommandSpec`. Guard test:
  `test_ui_palette.py::test_real_command_registry_satisfies_palette_protocol`.

## Test-file placement (ownership globs)

- needs-you tests live in `tests/test_ui_lanes_needs_you.py`; queued-strip
  tests in `tests/test_ui_rewind_queued.py` — both to stay inside the
  `test_ui_lanes*` / `test_ui_rewind*` ownership globs.

## Issue in someone else's files (not touched)

- `tests/test_commands_builtin.py` fails **collection** for the whole suite:
  `from test_commands_helpers import FakeContext` — no such module exists
  (conftest.py provides `FakeCommandContext` instead). Commands owner /
  integrator should fix; everything else passes
  (`uv run pytest -q --ignore=tests/test_commands_builtin.py` → 392 passed).
