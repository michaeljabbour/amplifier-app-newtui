//! The app's [`CommandContext`] implementation (commands ↔ app boundary).
//!
//! Port of `src/amplifier_app_newtui/ui/command_context.py`.
//!
//! Command handlers act on the app exclusively through
//! [`crate::commands::registry::CommandContext`]; [`AppCommandContext`]
//! satisfies that trait by delegating to the composition root's public
//! surface — no widget objects cross the boundary.
//!
//! Ratatui adaptation: the Python adapter holds the running Textual
//! `NewTuiApp`; here the app stands behind the [`CommandHost`] trait, whose
//! methods carry the exact names of the app members the Python adapter
//! reaches for (`action_cycle_mode`, `set_mode_by_id`, the
//! `session_ops.*` controller methods flattened onto the host, …). The
//! ratatui `App` implements [`CommandHost`] at assembly; tests drive a
//! plain fake.

use std::any::Any;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rust_decimal::Decimal;

use crate::commands::context::ContextUsage;
use crate::commands::copy::last_answer_text;
use crate::commands::export::{write_export, ExportStamp};
use crate::commands::improve::ApprovalJournal;
use crate::commands::registry::CommandContext;
use crate::model::blocks::TranscriptBlock;
use crate::model::queues::{NeedsYouQueue, SteeringQueue};
use crate::model::trust::DenialLog;
use crate::model::turn::OutcomeLedger;

/// The composition root's public surface, as the adapter consumes it.
///
/// Every method mirrors one attribute path the Python `AppCommandContext`
/// dereferences on `NewTuiApp` (noted per method) — the app-side names are
/// kept verbatim so the forwarding table in the Python behavior tests pins
/// one-to-one here. The `session_ops.*` controller methods (issue #31) are
/// flattened onto this trait; the assembled `App` forwards them itself.
pub trait CommandHost {
    // -- data surfaces (Python attribute paths in comments) ---------------

    /// `app.ledger`.
    fn ledger(&self) -> &Mutex<OutcomeLedger>;

    /// `app.adapter.denial_log`.
    fn denial_log(&self) -> &Mutex<DenialLog>;

    /// `app.adapter.steering`.
    fn steering(&self) -> &SteeringQueue;

    /// `app.adapter.needs_you`.
    fn needs_you(&self) -> &NeedsYouQueue;

    /// `app.reducer.session_cost`.
    fn session_cost(&self) -> Decimal;

    /// `app.adapter.session_short`.
    fn session_short(&self) -> String;

    /// `app.adapter.bundle_name`.
    fn bundle_name(&self) -> String;

    /// `app.allocator.next_id()`.
    fn next_block_id(&self) -> String;

    /// `app.context_usage()` — the app's clamped view of the window.
    fn context_usage(&self) -> ContextUsage;

    /// `app.journal` — the session [`ApprovalJournal`].
    fn journal(&self) -> &Mutex<ApprovalJournal>;

    /// `app.transcript.blocks` — snapshot for export/copy extraction.
    fn transcript_blocks(&self) -> Vec<TranscriptBlock>;

    /// `kernel.runtime._core_version()` — installed core version, `""`
    /// when core is absent (never errors). The Rust client has no
    /// in-process core, so the assembled app reports what it learned
    /// over the protocol.
    fn core_version(&self) -> String;

    // -- actions -----------------------------------------------------------

    /// `app.echo_user_line(text)`.
    fn echo_user_line(&self, text: &str);

    /// `app.append_block(block)`.
    fn append_block(&self, block: TranscriptBlock);

    /// `app.show_notice(text)`.
    fn show_notice(&self, text: &str);

    /// `app.action_cycle_mode()`.
    fn action_cycle_mode(&self);

    /// `app.set_mode_by_id(mode_id)`.
    fn set_mode_by_id(&self, mode_id: &str);

    /// `app.set_theme_by_name(name)`.
    fn set_theme_by_name(&self, name: &str);

    /// `app.action_toggle_lanes()`.
    fn action_toggle_lanes(&self);

    /// `app.action_open_rewind()`.
    fn action_open_rewind(&self);

    /// `app.open_permissions()`.
    fn open_permissions(&self);

    /// `app.manage_directories(kind, args)`.
    fn manage_directories(&self, kind: &str, args: &str);

    /// `app.exit()`.
    fn exit(&self);

    /// `app.copy_to_clipboard(text)` (OSC 52 on the real app).
    fn copy_to_clipboard(&self, text: &str);

    /// `app.show_native_modes()`.
    fn show_native_modes(&self);

    /// `app.activate_native_mode(name)` (`None` clears all).
    fn activate_native_mode(&self, name: Option<&str>);

    /// `app.deactivate_native_mode(name)`.
    fn deactivate_native_mode(&self, name: &str);

    // -- in-session ops (`app.session_ops.*`, flattened) --------------------

    /// `app.session_ops.show_status()`.
    fn show_status(&self);

    /// `app.session_ops.show_model(arg)`.
    fn show_model(&self, arg: &str);

    /// `app.session_ops.apply_effort(arg)`.
    fn apply_effort(&self, arg: &str);

    /// `app.session_ops.compact_context(focus)`.
    fn compact_context(&self, focus: &str);

    /// `app.session_ops.clear_context()`.
    fn clear_context(&self);

    /// `app.session_ops.show_tools()`.
    fn show_tools(&self);

    /// `app.session_ops.show_agents()`.
    fn show_agents(&self);

    /// `app.session_ops.show_diff(arg)`.
    fn show_diff(&self, arg: &str);

    /// `app.session_ops.show_skills()`.
    fn show_skills(&self);

    /// `app.session_ops.load_skill(name)`.
    fn load_skill(&self, name: &str);

    /// `app.session_ops.manage_mcp(args)`.
    fn manage_mcp(&self, args: &str);

    /// `app.session_ops.load_bundle(args)`.
    fn load_bundle(&self, args: &str);

    /// `app.manage_config(args)`.
    fn manage_config(&self, args: &str);

    // -- stored-session lifecycle -------------------------------------------

    /// `app.rename_session(name)`.
    fn rename_session(&self, name: &str);

    /// `app.show_sessions()`.
    fn show_sessions(&self);

    /// `app.branch_session(name)`.
    fn branch_session(&self, name: &str);

    /// `app.fork_session(directive)`.
    fn fork_session(&self, directive: &str);
}

/// [`CommandContext`] over the running app (behind [`CommandHost`]).
pub struct AppCommandContext<'a> {
    host: &'a dyn CommandHost,
}

impl<'a> AppCommandContext<'a> {
    /// Python `AppCommandContext(app)`.
    pub fn new(host: &'a dyn CommandHost) -> Self {
        Self { host }
    }

    /// The export write with the clock and root injectable (the trait's
    /// [`CommandContext::export_transcript`] passes `now()` and the
    /// Python-hardcoded `exports` directory under the cwd). Errors are the
    /// `OSError` the Python call would propagate.
    pub fn export_transcript_with(&self, now: ExportStamp, root: &Path) -> io::Result<PathBuf> {
        let blocks = self.host.transcript_blocks();
        let session = self.host.session_short();
        // Python: `self._app.adapter.session_short or "session"`.
        let session_short = if session.is_empty() {
            "session"
        } else {
            session.as_str()
        };
        write_export(blocks.iter(), session_short, now, root)
    }
}

impl CommandContext for AppCommandContext<'_> {
    // -- data surfaces -------------------------------------------------------

    fn ledger(&self) -> &Mutex<OutcomeLedger> {
        self.host.ledger()
    }

    fn denial_log(&self) -> &Mutex<DenialLog> {
        self.host.denial_log()
    }

    fn steering(&self) -> &SteeringQueue {
        self.host.steering()
    }

    fn needs_you(&self) -> &NeedsYouQueue {
        self.host.needs_you()
    }

    fn session_cost(&self) -> Decimal {
        self.host.session_cost()
    }

    fn session_short(&self) -> String {
        self.host.session_short()
    }

    fn bundle_name(&self) -> String {
        self.host.bundle_name()
    }

    fn next_block_id(&self) -> String {
        self.host.next_block_id()
    }

    fn context_usage(&self) -> Box<dyn Any> {
        Box::new(self.host.context_usage())
    }

    fn approval_tallies(&self) -> Vec<Box<dyn Any>> {
        // Python: `tuple(self._app.journal.tallies())`.
        self.host
            .journal()
            .lock()
            .unwrap()
            .tallies()
            .into_iter()
            .map(|tally| Box::new(tally) as Box<dyn Any>)
            .collect()
    }

    fn overridden_denials(&self) -> Vec<Box<dyn Any>> {
        // Python: `tuple(self._app.journal.overrides(self._app.adapter.denial_log))`.
        let journal = self.host.journal().lock().unwrap();
        let denial_log = self.host.denial_log().lock().unwrap();
        journal
            .overrides(Some(&denial_log))
            .into_iter()
            .map(|row| Box::new(row) as Box<dyn Any>)
            .collect()
    }

    fn mcp_server_stats(&self) -> Vec<Box<dyn Any>> {
        Vec::new()
    }

    // -- actions ------------------------------------------------------------------

    fn echo_user_line(&self, text: &str) {
        self.host.echo_user_line(text);
    }

    fn post_block(&self, block: TranscriptBlock) {
        self.host.append_block(block);
    }

    fn show_notice(&self, text: &str) {
        self.host.show_notice(text);
    }

    fn cycle_mode(&self) {
        self.host.action_cycle_mode();
    }

    fn set_mode(&self, mode_id: &str) {
        self.host.set_mode_by_id(mode_id);
    }

    fn set_theme(&self, name: &str) {
        self.host.set_theme_by_name(name);
    }

    fn toggle_lanes(&self) {
        self.host.action_toggle_lanes();
    }

    fn open_rewind(&self) {
        self.host.action_open_rewind();
    }

    fn open_permissions(&self) {
        self.host.open_permissions();
    }

    fn manage_directories(&self, kind: &str, args: &str) {
        self.host.manage_directories(kind, args);
    }

    fn quit_app(&self) {
        self.host.exit();
    }

    fn export_transcript(&self) -> String {
        // Python builds `Path("exports")` under the cwd and lets any
        // OSError propagate; a panic is this port's unhandled-exception
        // analog (the write is local and the directory is created).
        let path = self
            .export_transcript_with(ExportStamp::now(), Path::new("exports"))
            .expect("transcript export write failed");
        path.to_string_lossy().into_owned()
    }

    fn copy_answer(&self) -> usize {
        let blocks = self.host.transcript_blocks();
        // Python: `if not text: return 0` — no answer, or an empty one.
        let Some(text) = last_answer_text(&blocks).filter(|text| !text.is_empty()) else {
            return 0;
        };
        self.host.copy_to_clipboard(&text);
        // Python `len(text)` counts characters, not bytes.
        text.chars().count()
    }

    fn about_info(&self) -> (String, String, String, String) {
        // Python: (`__version__`, `_core_version()`, bundle, session).
        (
            env!("CARGO_PKG_VERSION").to_string(),
            self.host.core_version(),
            self.host.bundle_name(),
            self.host.session_short(),
        )
    }

    fn show_modes(&self) {
        self.host.show_native_modes();
    }

    fn set_native_mode(&self, name: Option<&str>) {
        self.host.activate_native_mode(name);
    }

    fn remove_native_mode(&self, name: &str) {
        self.host.deactivate_native_mode(name);
    }

    fn show_status(&self) {
        self.host.show_status();
    }

    fn show_model(&self, arg: &str) {
        self.host.show_model(arg);
    }

    fn apply_effort(&self, arg: &str) {
        self.host.apply_effort(arg);
    }

    fn compact_context(&self, focus: &str) {
        self.host.compact_context(focus);
    }

    fn clear_context(&self) {
        self.host.clear_context();
    }

    fn show_tools(&self) {
        self.host.show_tools();
    }

    fn show_agents(&self) {
        self.host.show_agents();
    }

    fn show_diff(&self, arg: &str) {
        self.host.show_diff(arg);
    }

    fn show_skills(&self) {
        self.host.show_skills();
    }

    fn load_skill(&self, name: &str) {
        self.host.load_skill(name);
    }

    fn manage_mcp(&self, args: &str) {
        self.host.manage_mcp(args);
    }

    fn load_bundle(&self, args: &str) {
        self.host.load_bundle(args);
    }

    fn manage_config(&self, args: &str) {
        self.host.manage_config(args);
    }

    fn rename_session(&self, name: &str) {
        self.host.rename_session(name);
    }

    fn show_sessions(&self) {
        self.host.show_sessions();
    }

    fn branch_session(&self, name: &str) {
        self.host.branch_session(name);
    }

    fn fork_session(&self, directive: &str) {
        self.host.fork_session(directive);
    }
}

// ---------------------------------------------------------------------------
// Tests — port of tests/test_command_context_contract.py (the surface
// contract, which the compiler enforces) plus the pure-enough behavior
// cases from tests/test_command_context_app.py over a fake host.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::improve::{ApprovalTally, OverriddenDenial};
    use crate::model::blocks::{Answer, BlockIdAllocator, Segment};
    use crate::model::trust::CapabilityClass;

    /// Recording stand-in for the composition root — the fake host mirrors
    /// the `NewTuiApp` surface the Python behavior tests spy on.
    struct FakeHost {
        ledger: Mutex<OutcomeLedger>,
        denial_log: Mutex<DenialLog>,
        steering: SteeringQueue,
        needs_you: NeedsYouQueue,
        journal: Mutex<ApprovalJournal>,
        ids: Mutex<BlockIdAllocator>,
        session_cost: Decimal,
        session_short: String,
        bundle_name: String,
        core_version: String,
        blocks: Mutex<Vec<TranscriptBlock>>,
        user_lines: Mutex<Vec<String>>,
        notices: Mutex<Vec<String>>,
        copied: Mutex<Vec<String>>,
        calls: Mutex<Vec<String>>,
    }

    impl FakeHost {
        fn new() -> Self {
            Self {
                ledger: Mutex::new(OutcomeLedger::default()),
                denial_log: Mutex::new(DenialLog::new()),
                steering: SteeringQueue::new(),
                needs_you: NeedsYouQueue::new(),
                journal: Mutex::new(ApprovalJournal::new()),
                ids: Mutex::new(BlockIdAllocator::new()),
                session_cost: Decimal::new(42, 3), // 0.042
                session_short: "a1b2c3".to_string(),
                bundle_name: "dev-bundle".to_string(),
                core_version: String::new(),
                blocks: Mutex::new(Vec::new()),
                user_lines: Mutex::new(Vec::new()),
                notices: Mutex::new(Vec::new()),
                copied: Mutex::new(Vec::new()),
                calls: Mutex::new(Vec::new()),
            }
        }

        fn record(&self, entry: impl Into<String>) {
            self.calls.lock().unwrap().push(entry.into());
        }

        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl CommandHost for FakeHost {
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
            self.session_short.clone()
        }

        fn bundle_name(&self) -> String {
            self.bundle_name.clone()
        }

        fn next_block_id(&self) -> String {
            self.ids.lock().unwrap().next_id()
        }

        fn context_usage(&self) -> ContextUsage {
            ContextUsage::new(52_000, 18_000, 8_000).expect("fixture usage is valid")
        }

        fn journal(&self) -> &Mutex<ApprovalJournal> {
            &self.journal
        }

        fn transcript_blocks(&self) -> Vec<TranscriptBlock> {
            self.blocks.lock().unwrap().clone()
        }

        fn core_version(&self) -> String {
            self.core_version.clone()
        }

        fn echo_user_line(&self, text: &str) {
            self.user_lines.lock().unwrap().push(text.to_string());
        }

        fn append_block(&self, block: TranscriptBlock) {
            self.blocks.lock().unwrap().push(block);
        }

        fn show_notice(&self, text: &str) {
            self.notices.lock().unwrap().push(text.to_string());
        }

        fn action_cycle_mode(&self) {
            self.record("action_cycle_mode");
        }

        fn set_mode_by_id(&self, mode_id: &str) {
            self.record(format!("set_mode_by_id:{mode_id}"));
        }

        fn set_theme_by_name(&self, name: &str) {
            self.record(format!("set_theme_by_name:{name}"));
        }

        fn action_toggle_lanes(&self) {
            self.record("action_toggle_lanes");
        }

        fn action_open_rewind(&self) {
            self.record("action_open_rewind");
        }

        fn open_permissions(&self) {
            self.record("open_permissions");
        }

        fn manage_directories(&self, kind: &str, args: &str) {
            self.record(format!("manage_directories:{kind}:{args}"));
        }

        fn exit(&self) {
            self.record("exit");
        }

        fn copy_to_clipboard(&self, text: &str) {
            self.copied.lock().unwrap().push(text.to_string());
        }

        fn show_native_modes(&self) {
            self.record("show_native_modes");
        }

        fn activate_native_mode(&self, name: Option<&str>) {
            self.record(format!(
                "activate_native_mode:{}",
                name.unwrap_or("None")
            ));
        }

        fn deactivate_native_mode(&self, name: &str) {
            self.record(format!("deactivate_native_mode:{name}"));
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

    /// Pins `test_command_context_contract.py::test_real_context_covers_the_protocol`
    /// (and `test_protocol_surface_is_nonempty`): the coercion to
    /// `&dyn CommandContext` compiles only when every trait member —
    /// exact names and parameter lists — is implemented, which is the
    /// whole point of the Python inspect-based contract test.
    #[test]
    fn test_real_context_covers_the_protocol() {
        let host = FakeHost::new();
        let ctx = AppCommandContext::new(&host);
        let dyn_ctx: &dyn CommandContext = &ctx;
        assert_eq!(dyn_ctx.session_short(), "a1b2c3");
    }

    /// Pins `test_command_context_app.py::test_data_surfaces_delegate_to_the_composition_root`.
    #[test]
    fn test_data_surfaces_delegate_to_the_composition_root() {
        let host = FakeHost::new();
        let ctx = AppCommandContext::new(&host);

        // Identity: the adapter hands back the app's live objects.
        assert!(std::ptr::eq(ctx.ledger(), &host.ledger));
        assert!(std::ptr::eq(ctx.denial_log(), &host.denial_log));
        assert!(std::ptr::eq(ctx.steering(), &host.steering));
        assert!(std::ptr::eq(ctx.needs_you(), &host.needs_you));

        // Scalars mirror the composition root.
        assert_eq!(ctx.session_cost(), host.session_cost);
        assert_eq!(ctx.session_short(), host.session_short);
        assert_eq!(ctx.bundle_name(), host.bundle_name);

        // context_usage() is the app's clamped view; tallies are tuples.
        let usage = ctx
            .context_usage()
            .downcast::<ContextUsage>()
            .expect("context_usage() must return a ContextUsage");
        assert!(usage.window > 0);
        assert!(ctx.approval_tallies().is_empty());
        assert!(ctx.overridden_denials().is_empty());
        assert!(ctx.mcp_server_stats().is_empty());

        // next_block_id() draws from the app allocator (unique, monotone).
        let ids: Vec<String> = (0..3).map(|_| ctx.next_block_id()).collect();
        assert_eq!(ids, vec!["b1", "b2", "b3"]);
    }

    /// No direct Python counterpart (the Textual test only checks tuple-ness):
    /// the tally/override surfaces delegate to the live journal + denial log.
    #[test]
    fn test_tallies_and_overrides_come_from_the_live_journal() {
        let host = FakeHost::new();
        host.journal
            .lock()
            .unwrap()
            .record_ask("rm -rf build", true, "exec")
            .unwrap();
        host.journal
            .lock()
            .unwrap()
            .record_override("rm -rf build")
            .unwrap();
        host.denial_log
            .lock()
            .unwrap()
            .record_denial(CapabilityClass::Exec, "rm -rf build", "mode denies exec")
            .unwrap();
        let ctx = AppCommandContext::new(&host);

        let tallies: Vec<ApprovalTally> = ctx
            .approval_tallies()
            .into_iter()
            .map(|row| *row.downcast::<ApprovalTally>().unwrap())
            .collect();
        assert_eq!(tallies.len(), 1);
        assert_eq!(tallies[0].action, "rm -rf build");
        assert_eq!(tallies[0].approved, 1);

        let overrides: Vec<OverriddenDenial> = ctx
            .overridden_denials()
            .into_iter()
            .map(|row| *row.downcast::<OverriddenDenial>().unwrap())
            .collect();
        assert_eq!(overrides.len(), 1);
        assert_eq!(overrides[0].action, "rm -rf build");
        assert_eq!(overrides[0].denied, 1);
    }

    /// Pins `test_echo_and_post_block_reach_the_transcript`.
    #[test]
    fn test_echo_and_post_block_reach_the_transcript() {
        let host = FakeHost::new();
        let ctx = AppCommandContext::new(&host);

        ctx.echo_user_line("drive the real adapter");
        assert_eq!(
            *host.user_lines.lock().unwrap(),
            vec!["drive the real adapter"]
        );

        let answer = Answer::new(
            ctx.next_block_id(),
            vec![Segment::new("posted through the boundary")],
        );
        let answer_id = answer.id.clone();
        ctx.post_block(answer.into());
        assert!(host
            .transcript_blocks()
            .iter()
            .any(|block| block.id() == answer_id));
    }

    /// Pins `test_show_notice_lands_on_the_notice_slot` (message → host).
    #[test]
    fn test_show_notice_lands_on_the_notice_slot() {
        let host = FakeHost::new();
        let ctx = AppCommandContext::new(&host);
        ctx.show_notice("boundary notice");
        assert_eq!(*host.notices.lock().unwrap(), vec!["boundary notice"]);
    }

    /// Pins the forwarding half of `test_set_theme_switches_the_running_app_theme`
    /// (the unknown-theme rejection + listing notice is app logic, tested
    /// with the assembled app).
    #[test]
    fn test_set_theme_forwards_to_the_app() {
        let host = FakeHost::new();
        let ctx = AppCommandContext::new(&host);
        ctx.set_theme("carbon");
        assert_eq!(host.calls(), vec!["set_theme_by_name:carbon"]);
    }

    /// Pins `test_copy_answer_copies_the_last_real_answer`.
    #[test]
    fn test_copy_answer_copies_the_last_real_answer() {
        let host = FakeHost::new();
        let ctx = AppCommandContext::new(&host);

        // Nothing to copy yet on a bare session.
        assert_eq!(ctx.copy_answer(), 0);
        assert!(host.copied.lock().unwrap().is_empty());

        let text = "the final answer text";
        ctx.post_block(Answer::new(ctx.next_block_id(), vec![Segment::new(text)]).into());
        assert_eq!(ctx.copy_answer(), text.len());
        assert_eq!(*host.copied.lock().unwrap(), vec![text]);
    }

    /// Pins `test_about_info_reports_live_session_identity`.
    #[test]
    fn test_about_info_reports_live_session_identity() {
        let host = FakeHost::new();
        let ctx = AppCommandContext::new(&host);
        let (version, core_version, bundle, session) = ctx.about_info();
        assert!(!version.is_empty());
        assert_eq!(core_version, ""); // "" when core absent, never errors
        assert_eq!(bundle, host.bundle_name);
        assert_eq!(session, host.session_short);
    }

    /// Pins `test_action_forwards_to_the_app` — the Python `_FORWARDING`
    /// table verbatim: (ctx method + args, app method it must invoke).
    #[test]
    fn test_action_forwards_to_the_app() {
        #[allow(clippy::type_complexity)]
        let forwarding: Vec<(&str, Box<dyn Fn(&dyn CommandContext)>, String)> = vec![
            (
                "cycle_mode",
                Box::new(|c| c.cycle_mode()),
                "action_cycle_mode".into(),
            ),
            (
                "set_mode",
                Box::new(|c| c.set_mode("plan")),
                "set_mode_by_id:plan".into(),
            ),
            (
                "toggle_lanes",
                Box::new(|c| c.toggle_lanes()),
                "action_toggle_lanes".into(),
            ),
            (
                "open_rewind",
                Box::new(|c| c.open_rewind()),
                "action_open_rewind".into(),
            ),
            (
                "open_permissions",
                Box::new(|c| c.open_permissions()),
                "open_permissions".into(),
            ),
            (
                "manage_directories",
                Box::new(|c| c.manage_directories("add", "src")),
                "manage_directories:add:src".into(),
            ),
            ("quit_app", Box::new(|c| c.quit_app()), "exit".into()),
            (
                "show_modes",
                Box::new(|c| c.show_modes()),
                "show_native_modes".into(),
            ),
            (
                "set_native_mode",
                Box::new(|c| c.set_native_mode(Some("debug"))),
                "activate_native_mode:debug".into(),
            ),
            (
                "remove_native_mode",
                Box::new(|c| c.remove_native_mode("team-pulse")),
                "deactivate_native_mode:team-pulse".into(),
            ),
            (
                "show_model",
                Box::new(|c| c.show_model("gpt")),
                "show_model:gpt".into(),
            ),
            (
                "apply_effort",
                Box::new(|c| c.apply_effort("high")),
                "apply_effort:high".into(),
            ),
            (
                "compact_context",
                Box::new(|c| c.compact_context("focus")),
                "compact_context:focus".into(),
            ),
            (
                "clear_context",
                Box::new(|c| c.clear_context()),
                "clear_context".into(),
            ),
            (
                "show_tools",
                Box::new(|c| c.show_tools()),
                "show_tools".into(),
            ),
            (
                "show_agents",
                Box::new(|c| c.show_agents()),
                "show_agents".into(),
            ),
            (
                "show_diff",
                Box::new(|c| c.show_diff("staged")),
                "show_diff:staged".into(),
            ),
            (
                "show_skills",
                Box::new(|c| c.show_skills()),
                "show_skills".into(),
            ),
            (
                "load_skill",
                Box::new(|c| c.load_skill("brainstorming")),
                "load_skill:brainstorming".into(),
            ),
            (
                "manage_mcp",
                Box::new(|c| c.manage_mcp("list")),
                "manage_mcp:list".into(),
            ),
        ];
        for (name, invoke, expected) in forwarding {
            let host = FakeHost::new();
            let ctx = AppCommandContext::new(&host);
            invoke(&ctx);
            assert_eq!(host.calls(), vec![expected], "forwarding for {name}");
        }
    }

    /// Extra coverage beyond the Python `_FORWARDING` table: the members it
    /// omits (`show_status` is Python's worker round-trip test; the session
    /// lifecycle rows postdate the table) forward the same way, including
    /// the `None` arm of `set_native_mode`.
    #[test]
    fn test_remaining_actions_forward_to_the_app() {
        #[allow(clippy::type_complexity)]
        let forwarding: Vec<(&str, Box<dyn Fn(&dyn CommandContext)>, String)> = vec![
            (
                "show_status",
                Box::new(|c| c.show_status()),
                "show_status".into(),
            ),
            (
                "set_native_mode(None)",
                Box::new(|c| c.set_native_mode(None)),
                "activate_native_mode:None".into(),
            ),
            (
                "load_bundle",
                Box::new(|c| c.load_bundle("load dev")),
                "load_bundle:load dev".into(),
            ),
            (
                "manage_config",
                Box::new(|c| c.manage_config("show")),
                "manage_config:show".into(),
            ),
            (
                "rename_session",
                Box::new(|c| c.rename_session("spike")),
                "rename_session:spike".into(),
            ),
            (
                "show_sessions",
                Box::new(|c| c.show_sessions()),
                "show_sessions".into(),
            ),
            (
                "branch_session",
                Box::new(|c| c.branch_session("alt")),
                "branch_session:alt".into(),
            ),
            (
                "fork_session",
                Box::new(|c| c.fork_session("try the other fix")),
                "fork_session:try the other fix".into(),
            ),
        ];
        for (name, invoke, expected) in forwarding {
            let host = FakeHost::new();
            let ctx = AppCommandContext::new(&host);
            invoke(&ctx);
            assert_eq!(host.calls(), vec![expected], "forwarding for {name}");
        }
    }

    /// Pins `test_export_transcript_writes_under_the_cwd`, adapted: the
    /// root is injected (a tempdir) instead of monkeypatched cwd — the
    /// trait method is the same write with `Path::new("exports")`.
    #[test]
    fn test_export_transcript_writes_under_the_root() {
        let dir = tempfile::tempdir().unwrap();
        let host = FakeHost::new();
        host.append_block(Answer::new("b1", vec![Segment::new("hello")]).into());
        let ctx = AppCommandContext::new(&host);
        let path = ctx
            .export_transcript_with(ExportStamp::new(2026, 1, 1, 12, 34, 56), dir.path())
            .unwrap();
        assert!(path.is_file());
        assert_eq!(
            path.file_name().unwrap().to_str().unwrap(),
            "a1b2c3-20260101-123456.md"
        );
    }

    /// Python `session_short or "session"`: an unnamed session exports
    /// under the fallback stem.
    #[test]
    fn test_export_falls_back_to_session_stem_when_short_id_empty() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new();
        host.session_short = String::new();
        let ctx = AppCommandContext::new(&host);
        let path = ctx
            .export_transcript_with(ExportStamp::new(2026, 1, 1, 0, 0, 0), dir.path())
            .unwrap();
        assert_eq!(
            path.file_name().unwrap().to_str().unwrap(),
            "session-20260101-000000.md"
        );
    }
}
