//! The single command registry (DESIGN-SPEC §6, ADR-0007).
//!
//! Port of `src/amplifier_app_newtui/commands/registry.py`.
//!
//! One table of [`CommandSpec`] powers the palette rows, the keybinding
//! wiring and the help output — the opencode lesson: commands are data plus
//! callables, defined once, never inheritance hierarchies. The registry
//! knows nothing about the UI toolkit; command handlers act on the app
//! exclusively through the [`CommandContext`] trait (post messages / mutate
//! model state — never direct widget calls).
//!
//! Palette semantics (DESIGN-SPEC §6):
//!
//! - rows filter by substring of the command name (mockup:
//!   `c.name.includes(filter)`);
//! - when the filter is exactly `/`, group headers show in phase order
//!   ([`GROUP_ORDER`]);
//! - running a command echoes it as a user line first
//!   ([`CommandRegistry::run`] calls `ctx.echo_user_line` before the
//!   handler).
//!
//! Threading: like the queue models, the registry guards its state with a
//! [`Mutex`] and notifies subscribers OUTSIDE the lock, so a listener that
//! re-reads [`CommandRegistry::specs`] can never deadlock against the
//! mutation that woke it.

use std::any::Any;
use std::collections::HashMap;
use std::fmt;
use std::sync::{Arc, Mutex};

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::model::blocks::TranscriptBlock;
use crate::model::queues::{NeedsYouQueue, SteeringQueue};
use crate::model::trust::DenialLog;
use crate::model::turn::OutcomeLedger;

/// Palette group headers, exactly as the mockup COMMANDS table names them
/// (Python `CommandGroup = Literal["During", "Parallel", "Ship", "Between",
/// "Repair"]`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CommandGroup {
    During,
    Parallel,
    Ship,
    Between,
    Repair,
}

impl CommandGroup {
    /// The exact Python literal string.
    pub fn as_str(self) -> &'static str {
        match self {
            CommandGroup::During => "During",
            CommandGroup::Parallel => "Parallel",
            CommandGroup::Ship => "Ship",
            CommandGroup::Between => "Between",
            CommandGroup::Repair => "Repair",
        }
    }
}

impl fmt::Display for CommandGroup {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Group header display order when the palette filter is exactly `/`.
pub const GROUP_ORDER: [CommandGroup; 5] = [
    CommandGroup::During,
    CommandGroup::Parallel,
    CommandGroup::Ship,
    CommandGroup::Between,
    CommandGroup::Repair,
];

/// Right-aligned dimmer tag on each palette row (DESIGN-SPEC §6).
///
/// Open by design (story #2): built-ins use `built-in`; dynamic
/// contributions conventionally show their source name (`skill`, and
/// later `recipe` / `pipeline`) — new capabilities must be able to
/// register verbs without a registry change, so this is not an enum.
pub type CommandTag = String;

/// Origin label of a registration — who contributed the command.
///
/// Well-known values today: `builtin` (seeded at construction) and
/// `skill` (discovered skills + shortcuts). Future mounted capabilities
/// (`recipe`, `pipeline`, …) pick their own label; the registry needs
/// no change to accept them.
pub type CommandSource = String;

/// The seed source: collides loudly, wins collisions, never unregisters.
pub const BUILTIN_SOURCE: &str = "builtin";

/// Errors mirroring the Python `ValueError` / `KeyError` split.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RegistryError {
    /// Python `ValueError` — the message text matches the original exactly.
    Value(String),
    /// Python `KeyError(f"unknown command: {name}")` from [`CommandRegistry::run`].
    UnknownCommand(String),
}

impl fmt::Display for RegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RegistryError::Value(message) => f.write_str(message),
            RegistryError::UnknownCommand(name) => write!(f, "unknown command: {name}"),
        }
    }
}

impl std::error::Error for RegistryError {}

/// Everything a command handler may touch on the app.
///
/// Implemented by the real app shell (posting messages under the hood) and
/// by plain fakes in tests. Handlers must go through this trait only —
/// no widget imports, no direct rendering.
///
/// Python is a `Protocol`; forward references to not-yet-ported command
/// types (`ContextUsage`, `ApprovalTally`, `OverriddenDenial`,
/// `McpServerStats`) are typed `object` there and surface here as
/// `Box<dyn Any>` — later command units downcast, exactly like the Python
/// duck-typing they replace.
pub trait CommandContext {
    // --- data surfaces -------------------------------------------------

    /// The session outcome ledger (`/ledger`, `/improve`).
    fn ledger(&self) -> &Mutex<OutcomeLedger>;

    /// Deny-and-continue accounting (`/improve`).
    fn denial_log(&self) -> &Mutex<DenialLog>;

    /// The bounded steer / next-turn queue.
    fn steering(&self) -> &SteeringQueue;

    /// Deferred decisions behind the ctrl-y badge.
    fn needs_you(&self) -> &NeedsYouQueue;

    /// Cumulative session cost — the footer $ (mockup `this.cost`).
    fn session_cost(&self) -> Decimal;

    /// Short session id shown in the ledger header/footer.
    fn session_short(&self) -> String;

    /// Active bundle name shown in the ledger header/footer.
    fn bundle_name(&self) -> String;

    /// Mint the next stable transcript block id.
    fn next_block_id(&self) -> String;

    /// Current `commands::context::ContextUsage` (Python `-> object`).
    fn context_usage(&self) -> Box<dyn Any>;

    /// Recorded `commands::improve::ApprovalTally` rows (Python `-> tuple[object, ...]`).
    fn approval_tallies(&self) -> Vec<Box<dyn Any>>;

    /// Recorded `commands::improve::OverriddenDenial` rows (Python `-> tuple[object, ...]`).
    fn overridden_denials(&self) -> Vec<Box<dyn Any>>;

    /// `commands::doctor::McpServerStats` rows for /doctor (Python `-> tuple[object, ...]`).
    fn mcp_server_stats(&self) -> Vec<Box<dyn Any>>;

    // --- actions (message posts on the real app) -----------------------

    /// Echo a command invocation as a `❯ [mode]` user line.
    fn echo_user_line(&self, text: &str);

    /// Append a [`TranscriptBlock`] to the transcript.
    fn post_block(&self, block: TranscriptBlock);

    /// Show a transient right-aligned dim notice.
    fn show_notice(&self, text: &str);

    /// Advance the shift+tab mode cycle by one.
    fn cycle_mode(&self);

    /// Jump directly to a mode by id.
    fn set_mode(&self, mode_id: &str);

    /// Switch the UI theme (`/theme`); empty name cycles (spec §1).
    fn set_theme(&self, name: &str);

    /// Toggle the agent-lanes panel (`/tasks` / ctrl-t).
    fn toggle_lanes(&self);

    /// Open the rewind picker strip (`/rewind` / ctrl-r).
    fn open_rewind(&self);

    /// Open the trust-slot editor (`/permissions`).
    fn open_permissions(&self);

    /// List/add/remove allowed or denied session directories.
    fn manage_directories(&self, kind: &str, args: &str);

    /// Exit the app (`/quit` — ctrl-d and ctrl-q are the key paths).
    fn quit_app(&self);

    /// Write the transcript markdown export; returns the written path
    /// (the `/export` handler surfaces it in the notice).
    fn export_transcript(&self) -> String;

    /// Copy the last assistant answer to the clipboard (OSC 52);
    /// returns the number of chars copied (0 = no answer yet).
    fn copy_answer(&self) -> usize;

    /// The identity data the session banner shows —
    /// `(app_version, core_version, bundle_name, session_short)`;
    /// the `/about` handler posts it as a transcript block.
    fn about_info(&self) -> (String, String, String, String);

    /// Print the bundle-composed native mode catalog (`/modes`).
    fn show_modes(&self);

    /// ADD a bundle-provided mode to the active set (`None` clears all) —
    /// actioned through the mounted mode tool, never an app-local list. Only
    /// the newest (primary) is enforced upstream (single-slot mode tool).
    fn set_native_mode(&self, name: Option<&str>);

    /// Remove ONE native mode from the active set (`/mode -<name>`),
    /// promoting the next-newest back into the enforced upstream slot.
    fn remove_native_mode(&self, name: &str);

    // -- in-session ops over the live amplifier coordinator -----------------

    /// Post the live session status block (`/status`).
    fn show_status(&self);

    /// `/model`: list models (empty arg) or switch to `arg`.
    fn show_model(&self, arg: &str);

    /// `/effort`: show current level (empty arg) or set to `arg`.
    fn apply_effort(&self, arg: &str);

    /// `/compact`: compact context, optionally focused on `focus`.
    fn compact_context(&self, focus: &str);

    /// `/clear`: clear the conversation context.
    fn clear_context(&self);

    /// `/tools`: post the mounted-tools roster.
    fn show_tools(&self);

    /// `/agents`: post the delegatable-agents roster.
    fn show_agents(&self);

    /// `/diff`: post the working-tree (or `staged`) patch.
    fn show_diff(&self, arg: &str);

    /// `/skills`: post the available-skills roster.
    fn show_skills(&self);

    /// `/skill <name>`: load a skill via the mounted skills tool.
    fn load_skill(&self, name: &str);

    /// `/mcp`: list / add / remove MCP servers (mcp.json).
    fn manage_mcp(&self, args: &str);

    /// `/bundle`: list deferred overlays, or `load <name>` composes one
    /// into the running session on demand (fast-boot deferral).
    fn load_bundle(&self, args: &str);

    /// `/config`: show/toggle/set/diff/save live session config.
    fn manage_config(&self, args: &str);

    // -- stored-session lifecycle (rename / list / branch) ------------------

    /// `/rename <name>`: label the live session (resume-picker name).
    fn rename_session(&self, name: &str);

    /// `/sessions`: post the stored-session roster for this project.
    fn show_sessions(&self);

    /// `/branch [name]`: snapshot this conversation into a new session.
    fn branch_session(&self, name: &str);

    /// `/fork <directive>`: snapshot into a new session primed to run it.
    fn fork_session(&self, directive: &str);
}

/// Handler signature: `(ctx, args)` where `args` is the text after the
/// command name (may be empty). Handlers post messages via ctx and return.
pub type CommandHandler = Arc<dyn Fn(&dyn CommandContext, &str) + Send + Sync>;

/// Python `repr(str)` for the validator message: single-quoted unless the
/// value contains a single quote (and no double quote), matching CPython.
fn py_repr(value: &str) -> String {
    if value.contains('\'') && !value.contains('"') {
        format!("\"{value}\"")
    } else {
        format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// One palette command: group + name + description + tag + handler.
///
/// - `name`: the slash trigger including the leading `/`.
/// - `desc`: palette row description — EXACT mockup strings for the
///   built-in set (DESIGN-SPEC §6).
/// - `tag`: right-aligned dimmer tag — `built-in`, `skill`, or a
///   future contribution's own label (open string, story #2).
/// - `key_action`: optional keymap action id this command duplicates
///   (e.g. `/tasks` ↔ `toggle_lanes`) so keybinds and palette stay a
///   single source.
///
/// Frozen in Python — treated as immutable here (no mutation after
/// construction; [`CommandSpec::with_key_action`] is a pre-registration
/// builder mirroring the Python keyword argument).
#[derive(Clone)]
pub struct CommandSpec {
    pub group: CommandGroup,
    pub name: String,
    pub desc: String,
    pub tag: CommandTag,
    pub handler: CommandHandler,
    pub key_action: Option<String>,
}

impl CommandSpec {
    /// Construct with the pydantic field validators applied; errors carry
    /// the exact Python `ValueError` message strings.
    pub fn new(
        group: CommandGroup,
        name: &str,
        desc: &str,
        tag: &str,
        handler: CommandHandler,
    ) -> Result<Self, RegistryError> {
        if !name.starts_with('/') || name.chars().count() < 2 || name.contains(' ') {
            return Err(RegistryError::Value(format!(
                "command name must be a single /trigger, got {}",
                py_repr(name)
            )));
        }
        if desc.trim().is_empty() {
            return Err(RegistryError::Value(
                "command description is required".to_string(),
            ));
        }
        if tag.trim().is_empty() {
            return Err(RegistryError::Value("command tag is required".to_string()));
        }
        Ok(Self {
            group,
            name: name.to_string(),
            desc: desc.to_string(),
            tag: tag.to_string(),
            handler,
            key_action: None,
        })
    }

    /// Python keyword argument `key_action=`.
    pub fn with_key_action(mut self, key_action: &str) -> Self {
        self.key_action = Some(key_action.to_string());
        self
    }
}

impl fmt::Debug for CommandSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CommandSpec")
            .field("group", &self.group)
            .field("name", &self.name)
            .field("desc", &self.desc)
            .field("tag", &self.tag)
            .field("key_action", &self.key_action)
            .finish_non_exhaustive() // handler is opaque
    }
}

/// Two specs are equal when every data field matches AND they share the
/// same handler `Arc` — so a clone of a registered spec compares equal to
/// the original (the Rust stand-in for Python `is` identity in the tests)
/// while an independently-built spec with the same fields does not.
impl PartialEq for CommandSpec {
    fn eq(&self, other: &Self) -> bool {
        self.group == other.group
            && self.name == other.name
            && self.desc == other.desc
            && self.tag == other.tag
            && self.key_action == other.key_action
            && Arc::ptr_eq(&self.handler, &other.handler)
    }
}

struct RegistryState {
    specs: Vec<CommandSpec>,
    by_name: HashMap<String, CommandSpec>,
    sources: HashMap<String, CommandSource>,
}

/// Ordered registry of [`CommandSpec`] — the palette's row source.
///
/// Registration order is display order within the full list (the mockup
/// table is already in phase order); [`CommandRegistry::grouped_rows`]
/// regroups by [`GROUP_ORDER`] for the headers-visible state.
///
/// Open registry (story #2): built-ins seed at construction; any mounted
/// capability may [`CommandRegistry::register_with_source`] verbs at
/// runtime under its own `source` label (`skill` today; `recipe` /
/// `pipeline` later) and [`CommandRegistry::unregister`] them when it
/// unmounts. Collision policy: built-ins win (a duplicate *built-in* is a
/// programming error and errors); a dynamic registration whose name is
/// taken is skipped (Python logs a warning; the crate has no logger — the
/// skip is observable via the `false` return), first registration wins.
/// [`CommandRegistry::subscribe`] observers hear every successful change
/// so the palette/help stay a live reflection.
pub struct CommandRegistry {
    state: Mutex<RegistryState>,
    listeners: Mutex<Vec<Arc<dyn Fn() + Send + Sync>>>,
}

impl Default for CommandRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl CommandRegistry {
    /// Python `CommandRegistry()` (empty seed tuple).
    pub fn new() -> Self {
        Self {
            state: Mutex::new(RegistryState {
                specs: Vec::new(),
                by_name: HashMap::new(),
                sources: HashMap::new(),
            }),
            listeners: Mutex::new(Vec::new()),
        }
    }

    /// Python `CommandRegistry(specs)`: seed built-ins in order; a
    /// duplicate seed name propagates the `ValueError`.
    pub fn with_specs(specs: impl IntoIterator<Item = CommandSpec>) -> Result<Self, RegistryError> {
        let registry = Self::new();
        for spec in specs {
            registry.register(spec)?;
        }
        Ok(registry)
    }

    /// Snapshot of every command in registration order (Python `.specs`).
    pub fn specs(&self) -> Vec<CommandSpec> {
        self.state.lock().unwrap().specs.clone()
    }

    /// Command names in registration order (Python `.names`).
    pub fn names(&self) -> Vec<String> {
        self.state
            .lock()
            .unwrap()
            .specs
            .iter()
            .map(|spec| spec.name.clone())
            .collect()
    }

    /// `register(spec)` with the default `source=BUILTIN_SOURCE`.
    pub fn register(&self, spec: CommandSpec) -> Result<bool, RegistryError> {
        self.register_with_source(spec, BUILTIN_SOURCE)
    }

    /// Add a command under *source*; returns whether it was added.
    ///
    /// Duplicate built-ins fail loudly (a bug in the seed table); a
    /// dynamic contribution whose name is already taken is skipped (the
    /// Python original logs a warning line here) — the existing command,
    /// built-in or earlier dynamic registration, always wins.
    pub fn register_with_source(
        &self,
        spec: CommandSpec,
        source: &str,
    ) -> Result<bool, RegistryError> {
        {
            let mut state = self.state.lock().unwrap();
            if state.by_name.contains_key(&spec.name) {
                if source == BUILTIN_SOURCE {
                    return Err(RegistryError::Value(format!(
                        "command already registered: {}",
                        spec.name
                    )));
                }
                // Python: _log.warning("command %s from %r skipped: already
                // registered by %r", …) — no logger in this crate.
                return Ok(false);
            }
            state.by_name.insert(spec.name.clone(), spec.clone());
            state.sources.insert(spec.name.clone(), source.to_string());
            state.specs.push(spec);
        }
        self.notify();
        Ok(true)
    }

    /// Remove a dynamic command by name; returns whether it existed.
    ///
    /// Built-ins are permanent — trying to unregister one errors
    /// (Python raises `ValueError`).
    pub fn unregister(&self, name: &str) -> Result<bool, RegistryError> {
        let key = name.trim();
        {
            let mut state = self.state.lock().unwrap();
            if !state.by_name.contains_key(key) {
                return Ok(false);
            }
            if state.sources.get(key).map(String::as_str) == Some(BUILTIN_SOURCE) {
                return Err(RegistryError::Value(format!(
                    "built-in command cannot be unregistered: {key}"
                )));
            }
            let position = state
                .specs
                .iter()
                .position(|spec| spec.name == key)
                .expect("by_name and specs stay in sync");
            state.specs.remove(position);
            state.by_name.remove(key);
            state.sources.remove(key);
        }
        self.notify();
        Ok(true)
    }

    /// Who registered *name* — `None` when unknown.
    pub fn source_of(&self, name: &str) -> Option<CommandSource> {
        self.state.lock().unwrap().sources.get(name.trim()).cloned()
    }

    /// All commands registered under *source*, in registration order.
    pub fn contributions(&self, source: &str) -> Vec<CommandSpec> {
        let state = self.state.lock().unwrap();
        state
            .specs
            .iter()
            .filter(|spec| state.sources[&spec.name] == source)
            .cloned()
            .collect()
    }

    /// Call *listener* after every successful register/unregister
    /// (skipped collisions and no-op unregisters stay silent) — the
    /// palette re-reads [`Self::specs`] on each change.
    pub fn subscribe(&self, listener: impl Fn() + Send + Sync + 'static) {
        self.listeners.lock().unwrap().push(Arc::new(listener));
    }

    /// Snapshot then call OUTSIDE the lock (Python `tuple(self._listeners)`).
    fn notify(&self) {
        let snapshot: Vec<_> = self.listeners.lock().unwrap().iter().cloned().collect();
        for listener in snapshot {
            listener();
        }
    }

    pub fn get(&self, name: &str) -> Option<CommandSpec> {
        self.state.lock().unwrap().by_name.get(name.trim()).cloned()
    }

    // --- palette -------------------------------------------------------

    /// Rows whose name contains *query* (mockup substring semantics).
    ///
    /// `"/"` (or empty) matches everything. Matching is on the command
    /// name only, exactly like the mockup's `c[1].includes(filter)`.
    pub fn filter_rows(&self, query: &str) -> Vec<CommandSpec> {
        let needle = query.trim();
        if needle.is_empty() || needle == "/" {
            return self.specs();
        }
        self.state
            .lock()
            .unwrap()
            .specs
            .iter()
            .filter(|spec| spec.name.contains(needle))
            .cloned()
            .collect()
    }

    /// Group headers show only when the filter is exactly `/`.
    pub fn show_group_headers(query: &str) -> bool {
        query.trim() == "/"
    }

    /// Matching rows grouped in [`GROUP_ORDER`]; empty groups omitted.
    ///
    /// Also serves as the help listing (same single source). Python
    /// default `query="/"`.
    pub fn grouped_rows(&self, query: &str) -> Vec<(CommandGroup, Vec<CommandSpec>)> {
        let rows = self.filter_rows(query);
        let mut grouped: Vec<(CommandGroup, Vec<CommandSpec>)> = Vec::new();
        for group in GROUP_ORDER {
            let members: Vec<CommandSpec> = rows
                .iter()
                .filter(|spec| spec.group == group)
                .cloned()
                .collect();
            if !members.is_empty() {
                grouped.push((group, members));
            }
        }
        grouped
    }

    // --- keybinds ------------------------------------------------------

    /// Keymap action id → command, for wiring key chords to handlers.
    pub fn keybound(&self) -> HashMap<String, CommandSpec> {
        self.state
            .lock()
            .unwrap()
            .specs
            .iter()
            .filter_map(|spec| {
                spec.key_action
                    .as_ref()
                    .map(|action| (action.clone(), spec.clone()))
            })
            .collect()
    }

    // --- execution -----------------------------------------------------

    /// Run a command by name: echo it as a user line, then dispatch.
    ///
    /// DESIGN-SPEC §6: running a command echoes it as a user line first.
    /// Unknown names error (Python `KeyError` — the palette only offers
    /// real rows; a typo reaching here is a bug).
    pub fn run(
        &self,
        name: &str,
        ctx: &dyn CommandContext,
        args: &str,
    ) -> Result<(), RegistryError> {
        let Some(spec) = self.get(name) else {
            return Err(RegistryError::UnknownCommand(name.to_string()));
        };
        let trimmed = args.trim();
        let invocation = if trimmed.is_empty() {
            spec.name.clone()
        } else {
            format!("{} {}", spec.name, trimmed)
        };
        ctx.echo_user_line(&invocation);
        (spec.handler)(ctx, trimmed);
        Ok(())
    }

    /// Dispatch raw composer text like `/mode plan`.
    ///
    /// Returns `false` when the text is not a known command (the composer
    /// treats it as a normal message).
    pub fn parse_and_run(&self, ctx: &dyn CommandContext, input_text: &str) -> bool {
        let text = input_text.trim();
        if !text.starts_with('/') {
            return false;
        }
        let (name, args) = text.split_once(' ').unwrap_or((text, ""));
        if self.get(name).is_none() {
            return false;
        }
        self.run(name, ctx, args)
            .expect("spec was just looked up; run cannot miss");
        true
    }
}

// ---------------------------------------------------------------------------
// Tests — port of tests/test_commands_registry.py (all cases). The fake
// context mirrors tests/conftest.py's FakeCommandContext.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::blocks::BlockIdAllocator;

    /// Stand-in for `commands.context.ContextUsage` (not yet ported); the
    /// conftest fake seeds `conversation=52_000, tools=18_000, memory=8_000`.
    #[derive(Debug, PartialEq, Eq)]
    struct FakeContextUsage {
        conversation: u64,
        tools: u64,
        memory: u64,
    }

    /// Records every action a command handler takes (CommandContext fake) —
    /// port of `tests/conftest.py::FakeCommandContext`. The recording sinks
    /// are `Arc<Mutex<…>>` so test handler closures can share `calls` the
    /// way Python handlers reach `ctx.calls`.
    struct FakeCommandContext {
        ledger: Mutex<OutcomeLedger>,
        denial_log: Mutex<DenialLog>,
        steering: SteeringQueue,
        needs_you: NeedsYouQueue,
        ids: Mutex<BlockIdAllocator>,
        session_cost: Decimal,
        answer_chars: usize,
        user_lines: Arc<Mutex<Vec<String>>>,
        blocks: Mutex<Vec<TranscriptBlock>>,
        notices: Mutex<Vec<String>>,
        calls: Arc<Mutex<Vec<String>>>,
    }

    impl FakeCommandContext {
        fn new() -> Self {
            Self {
                ledger: Mutex::new(OutcomeLedger::new()),
                denial_log: Mutex::new(DenialLog::new()),
                steering: SteeringQueue::new(),
                needs_you: NeedsYouQueue::new(),
                ids: Mutex::new(BlockIdAllocator::new()),
                session_cost: Decimal::ZERO,
                answer_chars: 42,
                user_lines: Arc::new(Mutex::new(Vec::new())),
                blocks: Mutex::new(Vec::new()),
                notices: Mutex::new(Vec::new()),
                calls: Arc::new(Mutex::new(Vec::new())),
            }
        }

        fn record(&self, entry: impl Into<String>) {
            self.calls.lock().unwrap().push(entry.into());
        }

        fn user_lines(&self) -> Vec<String> {
            self.user_lines.lock().unwrap().clone()
        }

        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl CommandContext for FakeCommandContext {
        fn ledger(&self) -> &Mutex<OutcomeLedger> {
            &self.ledger
        }

        fn denial_log(&self) -> &Mutex<DenialLog> {
            &self.denial_log
        }

        fn steering(&self) -> &SteeringQueue {
            &self.steering
        }

        fn needs_you(&self) -> &NeedsYouQueue {
            &self.needs_you
        }

        fn session_cost(&self) -> Decimal {
            self.session_cost
        }

        fn session_short(&self) -> String {
            "a1b2c3".to_string()
        }

        fn bundle_name(&self) -> String {
            "dev-bundle".to_string()
        }

        fn next_block_id(&self) -> String {
            self.ids.lock().unwrap().next_id()
        }

        fn context_usage(&self) -> Box<dyn Any> {
            Box::new(FakeContextUsage {
                conversation: 52_000,
                tools: 18_000,
                memory: 8_000,
            })
        }

        fn approval_tallies(&self) -> Vec<Box<dyn Any>> {
            Vec::new()
        }

        fn overridden_denials(&self) -> Vec<Box<dyn Any>> {
            Vec::new()
        }

        fn mcp_server_stats(&self) -> Vec<Box<dyn Any>> {
            Vec::new()
        }

        fn echo_user_line(&self, text: &str) {
            self.user_lines.lock().unwrap().push(text.to_string());
        }

        fn post_block(&self, block: TranscriptBlock) {
            self.blocks.lock().unwrap().push(block);
        }

        fn show_notice(&self, text: &str) {
            self.notices.lock().unwrap().push(text.to_string());
        }

        fn cycle_mode(&self) {
            self.record("cycle_mode");
        }

        fn set_mode(&self, mode_id: &str) {
            self.record(format!("set_mode:{mode_id}"));
        }

        fn set_theme(&self, name: &str) {
            self.record(format!("set_theme:{name}"));
        }

        fn toggle_lanes(&self) {
            self.record("toggle_lanes");
        }

        fn open_rewind(&self) {
            self.record("open_rewind");
        }

        fn open_permissions(&self) {
            self.record("open_permissions");
        }

        fn manage_directories(&self, kind: &str, args: &str) {
            self.record(format!("manage_directories:{kind}:{args}"));
        }

        fn quit_app(&self) {
            self.record("quit_app");
        }

        fn export_transcript(&self) -> String {
            self.record("export_transcript");
            "exports/a1b2c3-20260101-000000.md".to_string()
        }

        fn copy_answer(&self) -> usize {
            self.record("copy_answer");
            self.answer_chars
        }

        fn about_info(&self) -> (String, String, String, String) {
            self.record("about_info");
            (
                "0.1.0".to_string(),
                "1.2.3".to_string(),
                self.bundle_name(),
                self.session_short(),
            )
        }

        fn show_modes(&self) {
            self.record("show_modes");
        }

        fn set_native_mode(&self, name: Option<&str>) {
            // Python records f"set_native_mode:{name}" — None prints "None".
            self.record(format!("set_native_mode:{}", name.unwrap_or("None")));
        }

        fn remove_native_mode(&self, name: &str) {
            self.record(format!("remove_native_mode:{name}"));
        }

        fn show_status(&self) {
            self.record("show_status");
        }

        fn show_model(&self, arg: &str) {
            self.record(format!("show_model:{arg}"));
        }

        fn apply_effort(&self, arg: &str) {
            self.record(format!("apply_effort:{arg}"));
        }

        fn compact_context(&self, focus: &str) {
            self.record(format!("compact_context:{focus}"));
        }

        fn clear_context(&self) {
            self.record("clear_context");
        }

        fn show_tools(&self) {
            self.record("show_tools");
        }

        fn show_agents(&self) {
            self.record("show_agents");
        }

        fn show_diff(&self, arg: &str) {
            self.record(format!("show_diff:{arg}"));
        }

        fn show_skills(&self) {
            self.record("show_skills");
        }

        fn load_skill(&self, name: &str) {
            self.record(format!("load_skill:{name}"));
        }

        fn manage_mcp(&self, args: &str) {
            self.record(format!("manage_mcp:{args}"));
        }

        fn load_bundle(&self, args: &str) {
            self.record(format!("load_bundle:{args}"));
        }

        fn manage_config(&self, args: &str) {
            self.record(format!("manage_config:{args}"));
        }

        fn rename_session(&self, name: &str) {
            self.record(format!("rename_session:{name}"));
        }

        fn show_sessions(&self) {
            self.record("show_sessions");
        }

        fn branch_session(&self, name: &str) {
            self.record(format!("branch_session:{name}"));
        }

        fn fork_session(&self, directive: &str) {
            self.record(format!("fork_session:{directive}"));
        }
    }

    /// Python `_spec(name, group, key_action, tag)`: the handler appends
    /// `ran:{name}:{args}` to the context's `calls`. Rust handlers cannot
    /// duck-type into the fake, so the sink is shared by `Arc` instead —
    /// pass `fake.calls.clone()` to observe runs through that fake.
    fn spec_with(
        calls: Arc<Mutex<Vec<String>>>,
        name: &str,
        group: CommandGroup,
        key_action: Option<&str>,
        tag: &str,
    ) -> CommandSpec {
        let spec_name = name.to_string();
        let handler: CommandHandler = Arc::new(move |_ctx, args| {
            calls
                .lock()
                .unwrap()
                .push(format!("ran:{spec_name}:{args}"));
        });
        let spec = CommandSpec::new(group, name, &format!("desc for {name}"), tag, handler)
            .expect("valid test spec");
        match key_action {
            Some(action) => spec.with_key_action(action),
            None => spec,
        }
    }

    /// `_spec(name)` — defaults: group="During", key_action=None,
    /// tag="built-in", detached calls sink.
    fn spec(name: &str) -> CommandSpec {
        spec_with(
            Arc::new(Mutex::new(Vec::new())),
            name,
            CommandGroup::During,
            None,
            "built-in",
        )
    }

    fn spec_grouped(name: &str, group: CommandGroup) -> CommandSpec {
        spec_with(
            Arc::new(Mutex::new(Vec::new())),
            name,
            group,
            None,
            "built-in",
        )
    }

    fn spec_tagged(name: &str, tag: &str) -> CommandSpec {
        spec_with(
            Arc::new(Mutex::new(Vec::new())),
            name,
            CommandGroup::During,
            None,
            tag,
        )
    }

    #[test]
    fn test_fake_context_satisfies_protocol() {
        // Python: isinstance(fake_command_context, CommandContext).
        let fake = FakeCommandContext::new();
        let _ctx: &dyn CommandContext = &fake;
    }

    #[test]
    fn test_register_and_lookup() {
        let registry = CommandRegistry::new();
        registry.register(spec("/mode")).unwrap();
        assert!(registry.get("/mode").is_some());
        assert!(registry.get(" /mode ").is_some());
        assert!(registry.get("/nope").is_none());
        assert_eq!(registry.names(), vec!["/mode"]);
    }

    #[test]
    fn test_duplicate_name_rejected() {
        let registry = CommandRegistry::with_specs([spec("/mode")]).unwrap();
        let err = registry.register(spec("/mode")).unwrap_err();
        assert_eq!(
            err,
            RegistryError::Value("command already registered: /mode".to_string())
        );
    }

    #[test]
    fn test_name_must_be_slash_trigger() {
        let noop: CommandHandler = Arc::new(|_, _| {});
        for bad in ["mode", "/", "/two words"] {
            let err = CommandSpec::new(CommandGroup::During, bad, "desc", "built-in", noop.clone())
                .unwrap_err();
            assert_eq!(
                err,
                RegistryError::Value(format!(
                    "command name must be a single /trigger, got '{bad}'"
                ))
            );
        }
    }

    #[test]
    fn test_filter_rows_substring_semantics() {
        let registry = CommandRegistry::with_specs([
            spec_grouped("/rewind", CommandGroup::Between),
            spec("/brainstorm"),
            spec("/mode"),
        ])
        .unwrap();
        // "/" and empty show everything, in registration order.
        assert_eq!(registry.filter_rows("/"), registry.specs());
        assert_eq!(registry.filter_rows(""), registry.specs());
        // Substring of the command name, mockup semantics.
        assert_eq!(
            registry
                .filter_rows("/re")
                .iter()
                .map(|s| s.name.as_str())
                .collect::<Vec<_>>(),
            vec!["/rewind"]
        );
        assert_eq!(
            registry
                .filter_rows("rain")
                .iter()
                .map(|s| s.name.as_str())
                .collect::<Vec<_>>(),
            vec!["/brainstorm"]
        );
        assert!(registry.filter_rows("/zzz").is_empty());
    }

    #[test]
    fn test_group_headers_only_for_bare_slash() {
        assert!(CommandRegistry::show_group_headers("/"));
        assert!(CommandRegistry::show_group_headers(" / "));
        assert!(!CommandRegistry::show_group_headers("/re"));
        assert!(!CommandRegistry::show_group_headers(""));
    }

    #[test]
    fn test_grouped_rows_follow_group_order_and_skip_empty() {
        let registry = CommandRegistry::with_specs([
            spec_grouped("/rewind", CommandGroup::Between),
            spec_grouped("/mode", CommandGroup::During),
            spec_grouped("/doctor", CommandGroup::Repair),
        ])
        .unwrap();
        let grouped = registry.grouped_rows("/");
        assert_eq!(
            grouped.iter().map(|(group, _)| *group).collect::<Vec<_>>(),
            vec![
                CommandGroup::During,
                CommandGroup::Between,
                CommandGroup::Repair
            ]
        );
        assert_eq!(
            GROUP_ORDER.iter().map(|g| g.as_str()).collect::<Vec<_>>(),
            vec!["During", "Parallel", "Ship", "Between", "Repair"]
        );
    }

    #[test]
    fn test_run_echoes_user_line_then_dispatches() {
        let fake = FakeCommandContext::new();
        let registry = CommandRegistry::with_specs([spec_with(
            fake.calls.clone(),
            "/mode",
            CommandGroup::During,
            None,
            "built-in",
        )])
        .unwrap();
        registry.run("/mode", &fake, "plan").unwrap();
        // DESIGN-SPEC §6: running a command echoes it as a user line first.
        assert_eq!(fake.user_lines(), vec!["/mode plan"]);
        assert_eq!(fake.calls(), vec!["ran:/mode:plan"]);
    }

    #[test]
    fn test_run_without_args_echoes_bare_command() {
        let fake = FakeCommandContext::new();
        let registry = CommandRegistry::with_specs([spec_with(
            fake.calls.clone(),
            "/tasks",
            CommandGroup::Parallel,
            None,
            "built-in",
        )])
        .unwrap();
        registry.run("/tasks", &fake, "").unwrap();
        assert_eq!(fake.user_lines(), vec!["/tasks"]);
    }

    #[test]
    fn test_run_unknown_raises() {
        let fake = FakeCommandContext::new();
        let registry = CommandRegistry::new();
        let err = registry.run("/nope", &fake, "").unwrap_err();
        assert_eq!(err, RegistryError::UnknownCommand("/nope".to_string()));
        assert_eq!(err.to_string(), "unknown command: /nope");
    }

    #[test]
    fn test_parse_and_run() {
        let fake = FakeCommandContext::new();
        let registry = CommandRegistry::with_specs([spec_with(
            fake.calls.clone(),
            "/mode",
            CommandGroup::During,
            None,
            "built-in",
        )])
        .unwrap();
        assert!(registry.parse_and_run(&fake, "/mode build"));
        assert_eq!(fake.calls(), vec!["ran:/mode:build"]);
        assert!(!registry.parse_and_run(&fake, "hello world"));
        assert!(!registry.parse_and_run(&fake, "/unknown"));
    }

    #[test]
    fn test_keybound_maps_key_actions_to_specs() {
        let tasks = spec_with(
            Arc::new(Mutex::new(Vec::new())),
            "/tasks",
            CommandGroup::Parallel,
            Some("toggle_lanes"),
            "built-in",
        );
        let registry = CommandRegistry::with_specs([spec("/mode"), tasks.clone()]).unwrap();
        let keybound = registry.keybound();
        assert_eq!(keybound.len(), 1);
        assert_eq!(keybound.get("toggle_lanes"), Some(&tasks));
    }

    // --- open registry: dynamic contributions tagged by source (story #2) ---

    #[test]
    fn test_register_returns_true_and_records_source() {
        let registry = CommandRegistry::with_specs([spec("/mode")]).unwrap();
        assert!(registry
            .register_with_source(spec_tagged("/review", "skill"), "skill")
            .unwrap());
        assert_eq!(registry.source_of("/review").as_deref(), Some("skill"));
        // Seeded specs are built-ins.
        assert_eq!(registry.source_of("/mode").as_deref(), Some("builtin"));
        assert_eq!(registry.source_of("/nope"), None);
    }

    #[test]
    fn test_dynamic_collision_skips_with_log_and_builtin_wins() {
        // Python asserts the warning via caplog; this crate has no logger,
        // so the skip is pinned through the `false` return + unchanged state.
        let builtin = spec("/status");
        let registry = CommandRegistry::with_specs([builtin.clone()]).unwrap();
        assert!(!registry
            .register_with_source(spec_tagged("/status", "skill"), "skill")
            .unwrap());
        // The built-in survives untouched; order and lookup unchanged.
        assert_eq!(registry.get("/status"), Some(builtin));
        assert_eq!(registry.names(), vec!["/status"]);
        assert_eq!(registry.source_of("/status").as_deref(), Some("builtin"));
    }

    #[test]
    fn test_first_dynamic_registration_wins_over_later_ones() {
        let registry = CommandRegistry::new();
        let first = spec_tagged("/approve", "skill");
        assert!(registry
            .register_with_source(first.clone(), "skill")
            .unwrap());
        assert!(!registry
            .register_with_source(spec_tagged("/approve", "recipe"), "recipe")
            .unwrap());
        assert_eq!(registry.get("/approve"), Some(first));
        assert_eq!(registry.source_of("/approve").as_deref(), Some("skill"));
    }

    #[test]
    fn test_builtin_duplicate_still_raises() {
        let registry = CommandRegistry::with_specs([spec("/mode")]).unwrap();
        // Default source is builtin.
        let err = registry.register(spec("/mode")).unwrap_err();
        assert!(err.to_string().contains("already registered"));
    }

    #[test]
    fn test_future_sources_register_without_registry_changes() {
        // Acceptance: recipe/pipeline verbs must be registerable later with no
        // further registry changes — open source label, open display tag.
        let fake = FakeCommandContext::new();
        let registry = CommandRegistry::with_specs([spec("/mode")]).unwrap();
        assert!(registry
            .register_with_source(
                spec_with(
                    fake.calls.clone(),
                    "/recipe-approve",
                    CommandGroup::Parallel,
                    None,
                    "recipe",
                ),
                "recipe",
            )
            .unwrap());
        assert!(registry
            .register_with_source(
                spec_with(
                    Arc::new(Mutex::new(Vec::new())),
                    "/pipeline-status",
                    CommandGroup::Parallel,
                    None,
                    "pipeline",
                ),
                "pipeline",
            )
            .unwrap());
        assert!(registry.parse_and_run(&fake, "/recipe-approve now"));
        assert_eq!(fake.calls(), vec!["ran:/recipe-approve:now"]);
        assert!(registry.get("/pipeline-status").is_some());
    }

    #[test]
    fn test_contributions_filter_by_source_in_registration_order() {
        let registry = CommandRegistry::with_specs([spec("/mode")]).unwrap();
        let a = spec_tagged("/aa", "skill");
        let b = spec_tagged("/bb", "recipe");
        let c = spec_tagged("/cc", "skill");
        registry.register_with_source(a.clone(), "skill").unwrap();
        registry.register_with_source(b.clone(), "recipe").unwrap();
        registry.register_with_source(c.clone(), "skill").unwrap();
        assert_eq!(registry.contributions("skill"), vec![a, c]);
        assert_eq!(registry.contributions("recipe"), vec![b]);
        assert_eq!(
            registry.contributions("builtin"),
            vec![registry.get("/mode").unwrap()]
        );
        assert_eq!(
            registry.contributions("pipeline"),
            Vec::<CommandSpec>::new()
        );
    }

    #[test]
    fn test_unregister_removes_dynamic_command_and_keeps_order() {
        let registry = CommandRegistry::with_specs([spec("/mode")]).unwrap();
        registry
            .register_with_source(spec_tagged("/aa", "skill"), "skill")
            .unwrap();
        registry
            .register_with_source(spec_tagged("/bb", "skill"), "skill")
            .unwrap();
        assert!(registry.unregister("/aa").unwrap());
        // Stable order for palette/help.
        assert_eq!(registry.names(), vec!["/mode", "/bb"]);
        assert!(registry.get("/aa").is_none());
        assert_eq!(registry.source_of("/aa"), None);
        // Already gone → False, no raise.
        assert!(!registry.unregister("/aa").unwrap());
    }

    #[test]
    fn test_unregister_builtin_is_refused() {
        let registry = CommandRegistry::with_specs([spec("/mode")]).unwrap();
        let err = registry.unregister("/mode").unwrap_err();
        assert_eq!(
            err,
            RegistryError::Value("built-in command cannot be unregistered: /mode".to_string())
        );
        assert!(registry.get("/mode").is_some());
    }

    #[test]
    fn test_subscribers_hear_successful_changes_only() {
        let registry = Arc::new(CommandRegistry::new());
        registry.register(spec("/mode")).unwrap();
        let pings: Arc<Mutex<Vec<usize>>> = Arc::new(Mutex::new(Vec::new()));
        let (registry_view, sink) = (Arc::clone(&registry), Arc::clone(&pings));
        registry.subscribe(move || sink.lock().unwrap().push(registry_view.specs().len()));
        registry
            .register_with_source(spec_tagged("/aa", "skill"), "skill")
            .unwrap();
        // Skipped: silent.
        registry
            .register_with_source(spec_tagged("/aa", "skill"), "skill")
            .unwrap();
        registry.register(spec("/mode2")).unwrap();
        registry.unregister("/aa").unwrap();
        // No-op: silent.
        assert!(!registry.unregister("/zz").unwrap());
        assert_eq!(*pings.lock().unwrap(), vec![2, 3, 2]);
    }
}
