//! Command palette strip (DESIGN-SPEC §6, §2 overlay strips).
//!
//! Port of `src/amplifier_app_newtui/ui/palette.py`.
//!
//! A bordered strip docked ABOVE the composer (never a modal — ADR-0007/
//! mockup): opens on `/`, live-filters by substring as the user types, and
//! shows uppercase dimmer group headers (During / Parallel / Ship / Between /
//! Repair) only when the filter is exactly `"/"`.
//!
//! Rows: teal command (min-width aligned column) + description + right-
//! aligned dimmer tag (`built-in`/`skill`). The selected row (first by
//! default) is highlighted `bg-tab` with its description brightened to `fg`.
//! `↑`/`↓` move the selection, Enter runs the selected row, click runs any
//! row. Esc closes — but is resolved by the app via `keymap.ESC_CHAIN`
//! (spec §5), never by a local binding here.
//!
//! The palette is data-driven and *controlled*: it consumes a list of
//! [`PaletteCommand`] values (provided by the commands package) and a filter
//! string (slaved to the composer text via [`PaletteStrip::apply_filter`]).
//! It never executes commands itself — it queues
//! [`PaletteMessage::CommandRun`] / [`PaletteMessage::Closed`] messages and
//! the app reacts (running a command echoes it as a user line first, per
//! spec §6).
//!
//! Ratatui adaptation: the Textual widget mechanics (mount/compose,
//! `DEFAULT_CSS`, message pump, `VerticalScroll`) do not port. What ports is
//! the pure controlled-widget state machine plus a render surface:
//! [`PaletteStrip::rows`] produces the same header/row sequence
//! `_remount_rows` mounts, [`command_row_cells`] the three text cells of one
//! row, and [`command_row_tokens`] the theme-token names `_CommandRow.render`
//! resolves at paint time (`teal` command, `fg`/`dim` description, `dimmer`
//! tag) — all color flows through theme tokens, so a theme switch is a
//! repaint. Posted messages become a drained queue
//! ([`PaletteStrip::take_messages`]) the app-assembly layer consumes.

use crate::commands::registry::CommandSpec as RegistryCommandSpec;

/// Group header order per the mockup's command table.
pub const PALETTE_GROUPS: [&str; 5] = ["During", "Parallel", "Ship", "Between", "Repair"];

/// Command column minimum width: the mockup's 150px at JetBrains Mono
/// 12.5px (7.5px/cell) is exactly 20 cells.
pub const CMD_COL_MIN_WIDTH: usize = 20;

/// The strip's local key bindings — `(key, action, description)`, exactly
/// the Python `BINDINGS` triples. No local `escape` binding: Esc must
/// bubble to the app so it resolves via `keymap.ESC_CHAIN` (spec §5 —
/// lane-focus closes before the palette even while this strip holds
/// keyboard focus). The chain calls the app's `close_palette` when the
/// palette step is reached.
pub const BINDINGS: [(&str, &str, &str); 3] = [
    ("up", "cursor_up", "↑↓ select"),
    ("down", "cursor_down", "↑↓ select"),
    ("enter", "run", "enter run"),
];

/// What the palette needs to know about one slash command.
///
/// Python is a `runtime_checkable` `Protocol`; the commands package owns
/// the registry and any type with these accessors renders as a palette row.
///
/// - `name`: the slash trigger, e.g. `/mode`.
/// - `desc`: one-line description (matches `commands::registry::CommandSpec`).
/// - `tag`: right-aligned dimmer origin tag (`built-in`, `skill`, or a
///   dynamic contribution's own label — open registry, story #2).
/// - `group`: spec §6 group header (one of [`PALETTE_GROUPS`]).
pub trait PaletteCommand {
    fn name(&self) -> &str;
    fn desc(&self) -> &str;
    fn tag(&self) -> &str;
    fn group(&self) -> &str;
}

/// The real registry spec satisfies the palette protocol unchanged
/// (Python: `isinstance(spec, palette.CommandSpec)` over the registry).
impl PaletteCommand for RegistryCommandSpec {
    fn name(&self) -> &str {
        &self.name
    }

    fn desc(&self) -> &str {
        &self.desc
    }

    fn tag(&self) -> &str {
        &self.tag
    }

    fn group(&self) -> &str {
        self.group.as_str()
    }
}

/// Substring filter over command names (mockup: `cmd.includes(filter)`).
///
/// The filter includes its leading `/` so `"/"` matches everything.
pub fn filter_commands<C: PaletteCommand + Clone>(commands: &[C], filter_text: &str) -> Vec<C> {
    commands
        .iter()
        .filter(|c| c.name().contains(filter_text))
        .cloned()
        .collect()
}

/// Group headers appear only when the filter is exactly `/` (spec §6).
pub fn show_group_headers(filter_text: &str) -> bool {
    filter_text == "/"
}

/// The three text cells of one palette row: (command, description, tag).
pub fn command_row_cells<C: PaletteCommand + ?Sized>(spec: &C) -> (String, String, String) {
    (
        spec.name().to_string(),
        spec.desc().to_string(),
        spec.tag().to_string(),
    )
}

/// Displayed header text — uppercase dimmer per the mockup CSS.
pub fn group_header_text(group: &str) -> String {
    group.to_uppercase()
}

/// Theme-token names for the three cells of one row, as
/// `_CommandRow.render` resolves them at paint time: teal command,
/// `fg` (selected) / `dim` description, `dimmer` tag. The selected row's
/// background is `bg-tab` (the `-selected` class in the Python CSS).
pub fn command_row_tokens(selected: bool) -> (&'static str, &'static str, &'static str) {
    ("teal", if selected { "fg" } else { "dim" }, "dimmer")
}

/// One message the strip queues instead of acting (Textual `post_message`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PaletteMessage<C> {
    /// The user ran a palette row (Enter on selection or click).
    CommandRun(C),
    /// [`PaletteStrip::action_close`] ran while the palette was open.
    Closed,
}

/// One mounted row of the open strip — the `_remount_rows` sequence:
/// an uppercase dimmer group header, or a command row (with its filtered
/// index and whether it carries the `-selected` highlight).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PaletteRow<C> {
    GroupHeader { group: String },
    Command { index: usize, spec: C, selected: bool },
}

/// The command palette overlay strip (DESIGN-SPEC §6).
///
/// Controlled widget: the host calls [`Self::set_commands`] once and
/// [`Self::apply_filter`] on every composer change (`None` or a non-`/`
/// string closes it). It queues messages instead of acting:
///
/// - [`PaletteMessage::CommandRun`] — Enter on the selection or click on
///   any row ([`Self::run_row`]).
/// - [`PaletteMessage::Closed`] — [`Self::action_close`] ran (Esc itself is
///   resolved by the app via `keymap.ESC_CHAIN`, spec §5).
#[derive(Debug, Default)]
pub struct PaletteStrip<C: PaletteCommand + Clone> {
    commands: Vec<C>,
    filter: Option<String>,
    filtered: Vec<C>,
    selected: usize,
    display: bool,
    messages: Vec<PaletteMessage<C>>,
}

impl<C: PaletteCommand + Clone> PaletteStrip<C> {
    pub fn new(commands: impl IntoIterator<Item = C>) -> Self {
        Self {
            commands: commands.into_iter().collect(),
            filter: None,
            filtered: Vec::new(),
            selected: 0,
            display: false,
            messages: Vec::new(),
        }
    }

    // -- public API ----------------------------------------------------

    pub fn is_open(&self) -> bool {
        self.display
    }

    pub fn filter_text(&self) -> Option<&str> {
        self.filter.as_deref()
    }

    /// Currently displayed commands, in row order.
    pub fn filtered_commands(&self) -> &[C] {
        &self.filtered
    }

    pub fn selected_command(&self) -> Option<&C> {
        self.filtered.get(self.selected)
    }

    /// Index of the highlighted row within [`Self::filtered_commands`].
    pub fn selected_index(&self) -> usize {
        self.selected
    }

    /// Replace the command list (re-applies the current filter).
    pub fn set_commands(&mut self, commands: impl IntoIterator<Item = C>) {
        self.commands = commands.into_iter().collect();
        let filter = self.filter.clone();
        self.apply_filter(filter.as_deref());
    }

    /// Slave the palette to the composer text.
    ///
    /// `None` (or text not starting with `/`) closes the strip; a `/…`
    /// filter rebuilds the rows. Zero matches also hide the strip
    /// (mockup: `paletteOpen = filter != null && entries.length`).
    pub fn apply_filter(&mut self, filter_text: Option<&str>) {
        let Some(filter_text) = filter_text.filter(|text| text.starts_with('/')) else {
            self.filter = None;
            self.filtered = Vec::new();
            self.selected = 0;
            self.display = false;
            return;
        };
        self.filter = Some(filter_text.to_string());
        self.filtered = filter_commands(&self.commands, filter_text);
        self.selected = 0;
        // `_rebuild`: zero matches hide the strip (the live filter stays).
        self.display = !self.filtered.is_empty();
    }

    /// Move the highlighted row by *delta*, clamped to the list.
    pub fn move_selection(&mut self, delta: isize) {
        if self.filtered.is_empty() {
            return;
        }
        let max = self.filtered.len() as isize - 1;
        self.selected = (self.selected as isize + delta).clamp(0, max) as usize;
    }

    /// Queue [`PaletteMessage::CommandRun`] for the highlighted row.
    pub fn run_selected(&mut self) {
        if let Some(command) = self.selected_command().cloned() {
            self.messages.push(PaletteMessage::CommandRun(command));
        }
    }

    /// Queue [`PaletteMessage::CommandRun`] for row *index* — the
    /// `_CommandRow.on_click` path (click runs any row, not the selection).
    pub fn run_row(&mut self, index: usize) {
        if let Some(command) = self.filtered.get(index).cloned() {
            self.messages.push(PaletteMessage::CommandRun(command));
        }
    }

    /// Drain the queued messages, oldest first (the host's message pump).
    pub fn take_messages(&mut self) -> Vec<PaletteMessage<C>> {
        std::mem::take(&mut self.messages)
    }

    // -- key actions ----------------------------------------------------

    /// Dispatch one key through [`BINDINGS`]; returns whether it was
    /// consumed. Unbound keys (notably `escape`) bubble to the app.
    pub fn handle_key(&mut self, key: &str) -> bool {
        let Some((_, action, _)) = BINDINGS.iter().find(|(bound, _, _)| *bound == key) else {
            return false;
        };
        match *action {
            "cursor_up" => self.action_cursor_up(),
            "cursor_down" => self.action_cursor_down(),
            "run" => self.action_run(),
            _ => unreachable!("BINDINGS actions are exhaustive"),
        }
        true
    }

    pub fn action_cursor_up(&mut self) {
        self.move_selection(-1);
    }

    pub fn action_cursor_down(&mut self) {
        self.move_selection(1);
    }

    pub fn action_run(&mut self) {
        self.run_selected();
    }

    pub fn action_close(&mut self) {
        self.messages.push(PaletteMessage::Closed);
    }

    // -- render surface ---------------------------------------------------

    /// The mounted rows of the open strip, in `_remount_rows` order:
    /// group headers (only when the filter is exactly `/`) interleaved
    /// before each group's first command row. Hidden strip → no rows.
    pub fn rows(&self) -> Vec<PaletteRow<C>> {
        if !self.display {
            return Vec::new();
        }
        let mut rows = Vec::new();
        let headers = show_group_headers(self.filter.as_deref().unwrap_or(""));
        let mut last_group: Option<String> = None;
        for (index, spec) in self.filtered.iter().enumerate() {
            if headers && last_group.as_deref() != Some(spec.group()) {
                last_group = Some(spec.group().to_string());
                rows.push(PaletteRow::GroupHeader {
                    group: spec.group().to_string(),
                });
            }
            rows.push(PaletteRow::Command {
                index,
                spec: spec.clone(),
                selected: index == self.selected,
            });
        }
        rows
    }
}

// ---------------------------------------------------------------------------
// Tests — port of tests/test_ui_palette.py (pure helpers + the widget-state
// cases, re-expressed over the controlled state machine).
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Minimal PaletteCommand-conforming record (the protocol contract) —
    /// port of the test module's frozen `Cmd` dataclass.
    #[derive(Clone, Debug, PartialEq, Eq)]
    struct Cmd {
        group: &'static str,
        name: &'static str,
        desc: &'static str,
        tag: &'static str,
    }

    impl PaletteCommand for Cmd {
        fn name(&self) -> &str {
            self.name
        }

        fn desc(&self) -> &str {
            self.desc
        }

        fn tag(&self) -> &str {
            self.tag
        }

        fn group(&self) -> &str {
            self.group
        }
    }

    const fn cmd(
        group: &'static str,
        name: &'static str,
        desc: &'static str,
        tag: &'static str,
    ) -> Cmd {
        Cmd {
            group,
            name,
            desc,
            tag,
        }
    }

    /// The mockup's command table, verbatim (DESIGN-SPEC §6 minimum set).
    const COMMANDS: [Cmd; 10] = [
        cmd(
            "During",
            "/mode",
            "cycle or jump posture: chat, plan, brainstorm, build, auto",
            "built-in",
        ),
        cmd(
            "During",
            "/plan",
            "read-only planning; hands the plan to build",
            "built-in",
        ),
        cmd(
            "During",
            "/brainstorm",
            "no tools, divergent output; /plan to converge",
            "built-in",
        ),
        cmd(
            "During",
            "/context",
            "context usage grid + suggestions",
            "built-in",
        ),
        cmd(
            "Parallel",
            "/tasks",
            "agent lanes: one line per subagent",
            "built-in",
        ),
        cmd(
            "Ship",
            "/ledger",
            "session outcome ledger: spend vs yield",
            "built-in",
        ),
        cmd(
            "Between",
            "/rewind",
            "fork from any turn-rule checkpoint",
            "built-in",
        ),
        cmd(
            "Repair",
            "/permissions",
            "edit trust slots: boundary, blocks, exceptions",
            "built-in",
        ),
        cmd(
            "Repair",
            "/doctor",
            "setup checkup; reports, then fixes on confirm",
            "skill",
        ),
        cmd(
            "Repair",
            "/improve",
            "tune config from ledger + denial log",
            "skill",
        ),
    ];

    fn strip() -> PaletteStrip<Cmd> {
        PaletteStrip::new(COMMANDS)
    }

    fn names(commands: &[Cmd]) -> Vec<&str> {
        commands.iter().map(|c| c.name).collect()
    }

    fn header_groups(rows: &[PaletteRow<Cmd>]) -> Vec<String> {
        rows.iter()
            .filter_map(|row| match row {
                PaletteRow::GroupHeader { group } => Some(group.clone()),
                PaletteRow::Command { .. } => None,
            })
            .collect()
    }

    // -- pure helpers -------------------------------------------------------

    /// Python `test_real_command_registry_satisfies_palette_protocol`
    /// pins `commands.builtin.BUILTIN_COMMANDS`; that module is not yet
    /// ported, so this pins the protocol half: a real
    /// `commands::registry::CommandSpec` renders as a palette row unchanged.
    #[test]
    fn test_registry_command_spec_satisfies_palette_protocol() {
        use crate::commands::registry::{CommandGroup, CommandHandler, CommandSpec};
        use std::sync::Arc;

        let handler: CommandHandler = Arc::new(|_, _| {});
        let spec = CommandSpec::new(
            CommandGroup::During,
            "/mode",
            "cycle or jump posture: chat, plan, brainstorm, build, auto",
            "built-in",
            handler,
        )
        .unwrap();
        let row: &dyn PaletteCommand = &spec;
        assert_eq!(
            command_row_cells(row),
            (
                spec.name.clone(),
                spec.desc.clone(),
                spec.tag.clone()
            )
        );
        assert!(PALETTE_GROUPS.contains(&row.group()));
    }

    #[test]
    fn test_filter_is_substring_on_command_name() {
        assert_eq!(
            names(&filter_commands(&COMMANDS, "/")),
            names(&COMMANDS)
        );
        assert_eq!(names(&filter_commands(&COMMANDS, "/mo")), vec!["/mode"]);
        assert_eq!(names(&filter_commands(&COMMANDS, "/re")), vec!["/rewind"]);
        assert!(filter_commands(&COMMANDS, "/nope").is_empty());
    }

    #[test]
    fn test_group_headers_only_when_filter_is_exactly_slash() {
        assert!(show_group_headers("/"));
        assert!(!show_group_headers("/m"));
        assert!(!show_group_headers(""));
    }

    #[test]
    fn test_row_cells_and_groups_match_spec() {
        assert_eq!(
            PALETTE_GROUPS,
            ["During", "Parallel", "Ship", "Between", "Repair"]
        );
        assert_eq!(
            command_row_cells(&COMMANDS[0]),
            (
                "/mode".to_string(),
                "cycle or jump posture: chat, plan, brainstorm, build, auto".to_string(),
                "built-in".to_string(),
            )
        );
        assert_eq!(group_header_text("During"), "DURING");
        // 150px at JetBrains Mono 12.5px (7.5px/cell) == 20 cells.
        assert_eq!(CMD_COL_MIN_WIDTH, 20);
    }

    // -- widget behavior (controlled state machine, no pilot) ---------------

    #[test]
    fn test_open_on_slash_shows_group_headers_and_selects_first() {
        let mut strip = strip();
        assert!(!strip.is_open());
        strip.apply_filter(Some("/"));
        assert!(strip.is_open());
        let selected = strip.selected_command().expect("first row selected");
        assert_eq!(selected.name, "/mode");
        // Group headers present, in mockup order, displayed uppercase.
        let rows = strip.rows();
        assert_eq!(header_groups(&rows), PALETTE_GROUPS.to_vec());
        for group in header_groups(&rows) {
            assert_eq!(group_header_text(&group), group.to_uppercase());
        }
    }

    #[test]
    fn test_narrow_filter_hides_group_headers() {
        let mut strip = strip();
        strip.apply_filter(Some("/do"));
        assert_eq!(names(strip.filtered_commands()), vec!["/doctor"]);
        assert!(header_groups(&strip.rows()).is_empty());
    }

    #[test]
    fn test_zero_matches_hides_strip() {
        let mut strip = strip();
        strip.apply_filter(Some("/"));
        assert!(strip.is_open());
        strip.apply_filter(Some("/zzz"));
        assert!(!strip.is_open());
        // Flow pin (test_esc_with_zero_match_filter_clears_filter_not_the_turn):
        // zero matches hide the strip but the filter stays live…
        assert_eq!(strip.filter_text(), Some("/zzz"));
        assert!(strip.rows().is_empty());
        // …and clearing it (the app's Esc) drops the filter entirely.
        strip.apply_filter(None);
        assert_eq!(strip.filter_text(), None);
    }

    #[test]
    fn test_arrows_move_selection_and_enter_runs_selected() {
        let mut strip = strip();
        strip.apply_filter(Some("/"));
        assert!(strip.handle_key("down"));
        assert!(strip.handle_key("down"));
        assert_eq!(
            strip.selected_command().map(|c| c.name),
            Some("/brainstorm")
        );
        assert!(strip.handle_key("up"));
        assert_eq!(strip.selected_command().map(|c| c.name), Some("/plan"));
        // Clamped at the top.
        assert!(strip.handle_key("up"));
        assert!(strip.handle_key("up"));
        assert!(strip.handle_key("up"));
        assert_eq!(strip.selected_command().map(|c| c.name), Some("/mode"));
        assert!(strip.handle_key("enter"));
        let runs: Vec<_> = strip
            .take_messages()
            .into_iter()
            .filter_map(|message| match message {
                PaletteMessage::CommandRun(command) => Some(command.name),
                PaletteMessage::Closed => None,
            })
            .collect();
        assert_eq!(runs, vec!["/mode"]);
    }

    #[test]
    fn test_selection_highlight_tracks_selected_row() {
        let mut strip = strip();
        strip.apply_filter(Some("/"));
        let selected_flags = |rows: &[PaletteRow<Cmd>]| -> Vec<bool> {
            rows.iter()
                .filter_map(|row| match row {
                    PaletteRow::Command { selected, .. } => Some(*selected),
                    PaletteRow::GroupHeader { .. } => None,
                })
                .collect()
        };
        let rows = strip.rows();
        assert!(selected_flags(&rows)[0]);
        strip.move_selection(1);
        let rows = strip.rows();
        assert!(!selected_flags(&rows)[0]);
        assert!(selected_flags(&rows)[1]);
    }

    #[test]
    fn test_click_runs_that_row() {
        let mut strip = strip();
        strip.apply_filter(Some("/le"));
        assert_eq!(names(strip.filtered_commands()), vec!["/ledger"]);
        // `_CommandRow.on_click` on #palette-row-0.
        strip.run_row(0);
        let runs: Vec<_> = strip
            .take_messages()
            .into_iter()
            .filter_map(|message| match message {
                PaletteMessage::CommandRun(command) => Some(command.name),
                PaletteMessage::Closed => None,
            })
            .collect();
        assert_eq!(runs, vec!["/ledger"]);
    }

    #[test]
    fn test_close_action_posts_closed_and_escape_is_not_bound_locally() {
        // Esc is resolved by the app via keymap.ESC_CHAIN (spec §5) — the
        // strip has no local escape binding, so Esc bubbles even while it
        // holds focus.
        let mut strip = strip();
        strip.apply_filter(Some("/"));
        assert!(!strip.handle_key("escape")); // bubbled: no local handling
        assert!(strip.take_messages().is_empty());
        strip.action_close();
        assert_eq!(strip.take_messages(), vec![PaletteMessage::Closed]);
    }

    // -- ratatui-side conventions the Python render pins implicitly ---------

    #[test]
    fn test_command_row_tokens_brighten_selected_description() {
        // `_CommandRow.render`: teal command, dim description (fg when
        // selected), dimmer tag — resolved from theme tokens at paint time.
        assert_eq!(command_row_tokens(false), ("teal", "dim", "dimmer"));
        assert_eq!(command_row_tokens(true), ("teal", "fg", "dimmer"));
    }

    #[test]
    fn test_set_commands_reapplies_current_filter() {
        // `set_commands` re-applies the live filter (PaletteStrip.set_commands).
        let mut strip = strip();
        strip.apply_filter(Some("/do"));
        assert_eq!(names(strip.filtered_commands()), vec!["/doctor"]);
        strip.set_commands([cmd("Repair", "/doom", "d", "skill")]);
        assert_eq!(names(strip.filtered_commands()), vec!["/doom"]);
        assert_eq!(strip.filter_text(), Some("/do"));
    }
}
