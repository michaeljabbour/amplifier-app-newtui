# NOTES from the chrome/composer/footer/notices/approval-bar agent

Files I own (all created this pass):
`amplifier_app_newtui/ui/chrome.py`, `ui/composer.py`, `ui/footer.py`,
`ui/notices.py`, `ui/approval_bar.py` and tests `tests/test_ui_chrome.py`,
`test_ui_composer.py`, `test_ui_footer.py`, `test_ui_approval.py`.

## IMPORTANT for the integrator (app.py) — theme registration timing

`ui/themes.py`'s docstring says to call `register_themes(app)` from
`App.on_mount`. That is TOO LATE for widgets whose `DEFAULT_CSS` references
the spec token variables (`$bg-chrome`, `$orange`, …) — all five of my
widgets do. Textual parses widget CSS against the *current* theme's
variables, and `on_mount` fires after the first stylesheet parse, so the
app crashes with "reference to undefined variable '$bg-chrome'".

Fix (verified, used by all my tests): register + select the theme in
`App.__init__`:

```python
class NewTuiApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
```

Suggest the contracts owner updates the `register_themes` docstring
(`ui/themes.py`) accordingly — I did not touch that file.

## Contracts my widgets expose (for app.py wiring)

- **TitleBar** (`ui/chrome.py`): reactives `state_text` / `bundle` /
  `session_short` / `running`. Spinner (✳ ✦ ✧ ✦, 260ms Textual interval)
  starts/stops on `running`. `title_text()` returns the plain rendered
  title for tests/debug. The `<state>` string (plan step, `✳ coordinating
  N agents`, …) is composed by the app; the bar only displays it.
- **Composer** (`ui/composer.py`): posts `Submit` / `Steer` /
  `QueueMessage` / `OpenPalette(filter)` / `PaletteFilterCleared` /
  `EscPressed` / `CycleModeRequested`. The app OWNS steer-vs-submit: set
  `composer.running = True/False` at turn boundaries; Enter posts `Steer`
  while running, else `Submit`. Both `shift+enter` and `alt+enter` always
  queue; ctor flag `kitty_protocol=False` only changes the advertised
  chord (`composer.queue_hint`). On mode change call
  `composer.set_mode(profile)` (badge text/color + left-edge accent;
  chat edge = `$rule` per spec §4). `focus_input()` to focus.
- **FooterBar** (`ui/footer.py`): one call — `update_state(FooterState)`.
  `FooterState.context` is a `keymap.Context`; the running hint is composed
  via `hint_label` so `kitty_protocol=False` swaps shift+enter→alt+enter.
  Badge click posts `FooterBar.WaitingBadgeClicked` (→ open needs-you).
  Pure builders `footer_left_text` / `footer_right_text` /
  `footer_waiting_text` are the testable string contract.
- **NoticeSlot** (`ui/notices.py`): `show_notice(text)` (single slot,
  restarts the 4s clock), `dismiss_notice()`, `current`. Compose it docked
  at the transcript's bottom edge; the widget only manages its own
  text/visibility/timer. Ctor `duration=` for tests.
- **ApprovalBar** (`ui/approval_bar.py`): construct with
  `(ticket_id, prompt, options=("Allow once", "Allow always", "Deny"))`,
  mount in place of the composer, then `.focus()` — it owns the keyboard
  (arrows/tab cycle, enter confirm, esc=Deny, click confirms). Emits
  `ApprovalBar.Resolved(ticket_id, choice)`; route to the kernel approval
  broker and swap the composer back.

## Minor observations

- Textual 8.2.8 `Static` exposes `.content` (there is no `.renderable`
  attribute anymore) — relevant for anyone asserting rendered strings.
- `Widget.on_mount` fires while `is_mounted` is still `False` — don't
  guard painting on `is_mounted` inside `on_mount` paths.
- `tests/test_commands_builtin.py` / `test_commands_registry.py` failed
  collection mid-run while their helper module was being written by the
  commands agent (now present); my suite runs were done with those two
  ignored. Not my area.
