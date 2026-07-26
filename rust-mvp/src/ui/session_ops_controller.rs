//! SessionOpsController: the live in-session op surface (ADR-0007 seam).
//!
//! Port of `src/amplifier_app_newtui/ui/session_ops_controller.py`.
//!
//! The `/status /model /effort /compact /clear /tools /agents /diff /skills
//! /skill /mcp /bundle` handlers used to live directly on the app; this
//! controller owns them as a single-purpose unit so the composition root
//! stays a thin shell (ADR-0007's <500-line budget). The controller touches
//! the app only through the narrow [`SessionOpsHost`] trait, so it is
//! unit-testable without any widget host — a plain fake satisfies it
//! (mirrors how the command tests drive `FakeCommandContext`).
//!
//! Ratatui adaptation: Python pairs each public trigger with an async body
//! scheduled via `host.run_worker(...)` so the coordinator call marshals to
//! the runtime loop without blocking the UI. This client has no in-process
//! coordinator; the adapter surface the async bodies awaited becomes the
//! synchronous [`SessionOpsAdapter`] trait and each trigger runs its body
//! inline. App assembly is responsible for keeping adapter implementations
//! non-blocking (or for invoking the controller off the render loop) — the
//! worker-scheduling half of the Python seam does not port. Likewise the
//! `kernel.mcp_config` file store the Python `/mcp` handler reads directly
//! is an unported server-side unit here, so its four touchpoints
//! (path+read+describe collapse into one listing call, add, remove) live on
//! [`SessionOpsHost`] for assembly to wire over the real `mcp.json`.

use rust_decimal::Decimal;

use crate::model::blocks::{Answer, TranscriptBlock};

use super::session_ops_view::{
    diff_spans, mcp_spans, model_listing_spans, names_spans, skill_loaded_spans, skills_spans,
    status_spans, CompactionConfig, ModelListing, SkillInfo, StatusInfo,
};

/// The RuntimeAdapter surface the controller touches (Python
/// `host.adapter.*`, awaited there; synchronous here — see module docs).
///
/// Tuple results keep Python's `(ok, detail)` shapes verbatim.
pub trait SessionOpsAdapter {
    /// `adapter.bundle_name`.
    fn bundle_name(&self) -> String;

    /// `adapter.session_short`.
    fn session_short(&self) -> String;

    /// `adapter.compaction`.
    fn compaction(&self) -> CompactionConfig;

    /// `await adapter.status()`.
    fn status(&self) -> StatusInfo;

    /// `await adapter.set_model(model)`.
    fn set_model(&self, model: &str) -> (bool, String);

    /// `await adapter.list_models()`.
    fn list_models(&self) -> ModelListing;

    /// `await adapter.set_effort(level)`.
    fn set_effort(&self, level: &str) -> (bool, String);

    /// `await adapter.get_effort()` — Python `str | None`.
    fn get_effort(&self) -> Option<String>;

    /// `await adapter.compact(focus)`.
    fn compact(&self, focus: &str) -> (bool, String);

    /// `await adapter.clear_context()`.
    fn clear_context(&self) -> (bool, u64);

    /// `await adapter.list_tools()`.
    fn list_tools(&self) -> Vec<String>;

    /// `await adapter.list_agents()`.
    fn list_agents(&self) -> Vec<String>;

    /// `await adapter.diff(staged)` — Python `str | None`.
    fn diff(&self, staged: bool) -> Option<String>;

    /// `await adapter.list_skills()`.
    fn list_skills(&self) -> Vec<SkillInfo>;

    /// `await adapter.load_skill(name)` — `(ok, body-or-error)`.
    fn load_skill(&self, name: &str) -> (bool, String);

    /// `await adapter.mcp_tools()` — live-connected MCP tool names.
    fn mcp_tools(&self) -> Vec<String>;

    /// `await adapter.deferred_bundles()`.
    fn deferred_bundles(&self) -> Vec<String>;

    /// `await adapter.load_deferred_bundle(name)`.
    fn load_deferred_bundle(&self, name: &str) -> (bool, String);
}

/// The narrow app surface [`SessionOpsController`] drives.
///
/// Implemented by the composition root (the real host) and by plain fakes
/// in tests — no widget objects cross the boundary.
pub trait SessionOpsHost {
    /// `host.adapter`.
    fn adapter(&self) -> &dyn SessionOpsAdapter;

    /// `host.allocator.next_id()`.
    fn next_block_id(&self) -> String;

    /// Current interaction-mode id (status/footer field).
    fn mode_id(&self) -> String;

    /// Cumulative session cost shown in `/status`.
    fn session_cost(&self) -> Decimal;

    /// True while the boot splash is up (session not ready yet).
    fn splash_active(&self) -> bool;

    /// Append a transcript block.
    fn append_block(&self, block: TranscriptBlock);

    /// Show a transient right-aligned dim notice.
    fn show_notice(&self, text: &str);

    /// Repaint the title/footer after adapter-derived state changes.
    fn refresh_status(&self);

    // -- kernel.mcp_config touchpoints (unported server-side unit) ----------

    /// Python `{name: describe_server(spec) for name, spec in
    /// read_servers(mcp_config_path()).items()}` — insertion order is
    /// render order.
    fn mcp_servers(&self) -> Vec<(String, String)>;

    /// `mcp_config.add_stdio_server(path, name, command, args)`.
    fn add_mcp_stdio_server(&self, name: &str, command: &str, args: &[String]);

    /// `mcp_config.remove_server(path, name)`.
    fn remove_mcp_server(&self, name: &str) -> bool;
}

/// Python `SessionOpsController._DIFF_STAGED_ARGS`.
const DIFF_STAGED_ARGS: [&str; 4] = ["staged", "cached", "--staged", "--cached"];

/// In-session ops over the live amplifier coordinator (ADR-0007 seam).
///
/// Owns `/status /model /effort /compact /clear /tools /agents /diff
/// /skills /skill /mcp /bundle`. Behavior is identical to the app's prior
/// inline handlers; only the host reference is indirected.
pub struct SessionOpsController<'a> {
    host: &'a dyn SessionOpsHost,
}

impl<'a> SessionOpsController<'a> {
    /// Python `SessionOpsController(host)`.
    pub fn new(host: &'a dyn SessionOpsHost) -> Self {
        Self { host }
    }

    /// True (and notices) when the session banner has not landed yet.
    fn ops_starting(&self) -> bool {
        if self.host.splash_active() {
            self.host
                .show_notice("session still starting · try again once the banner lands");
            return true;
        }
        false
    }

    /// `/status` — coordinator snapshot joined with app-side mode/cost.
    pub fn show_status(&self) {
        let info = self.host.adapter().status();
        let id = self.host.next_block_id();
        let spans = status_spans(
            &info,
            &self.host.mode_id(),
            &self.host.adapter().bundle_name(),
            &self.host.adapter().session_short(),
            self.host.session_cost(),
            &self.host.adapter().compaction(),
        );
        self.host.append_block(Answer::new(id, spans).into());
    }

    /// `/model [name]` — switch models, or list the provider's set.
    pub fn show_model(&self, arg: &str) {
        if !arg.is_empty() && self.ops_starting() {
            return;
        }
        if !arg.is_empty() {
            let (ok, detail) = self.host.adapter().set_model(arg);
            if ok {
                self.host.refresh_status(); // footer model field is adapter-derived
            }
            let notice = if ok { format!("model · {detail}") } else { detail };
            self.host.show_notice(&notice);
            return;
        }
        let listing = self.host.adapter().list_models();
        let id = self.host.next_block_id();
        self.host
            .append_block(Answer::new(id, model_listing_spans(&listing)).into());
    }

    /// `/effort [level]` — set the reasoning effort, or show the current one.
    pub fn apply_effort(&self, arg: &str) {
        if !arg.is_empty() && self.ops_starting() {
            return;
        }
        if !arg.is_empty() {
            let (ok, detail) = self.host.adapter().set_effort(arg);
            let notice = if ok { format!("effort · {detail}") } else { detail };
            self.host.show_notice(&notice);
            return;
        }
        // Python `current or '(default)'` — None and "" both read default.
        let current = self
            .host
            .adapter()
            .get_effort()
            .filter(|level| !level.is_empty())
            .unwrap_or_else(|| "(default)".to_string());
        self.host
            .show_notice(&format!("effort · {current} · /effort <level> to set"));
    }

    /// `/compact [focus]` — compact the context window.
    pub fn compact_context(&self, focus: &str) {
        if self.ops_starting() {
            return;
        }
        let (ok, detail) = self.host.adapter().compact(focus);
        let notice = if ok { format!("compacted · {detail}") } else { detail };
        self.host.show_notice(&notice);
    }

    /// `/clear` — drop the conversation context.
    pub fn clear_context(&self) {
        if self.ops_starting() {
            return;
        }
        let (ok, count) = self.host.adapter().clear_context();
        let notice = if ok {
            format!("context cleared · {count} messages dropped")
        } else {
            "clear unavailable in this session".to_string()
        };
        self.host.show_notice(&notice);
    }

    /// `/tools` — the mounted-tool roster.
    pub fn show_tools(&self) {
        let names = self.host.adapter().list_tools();
        let id = self.host.next_block_id();
        self.host.append_block(
            Answer::new(id, names_spans("Tools", &names, "no tools mounted")).into(),
        );
    }

    /// `/agents` — the mounted-agent roster.
    pub fn show_agents(&self) {
        let names = self.host.adapter().list_agents();
        let id = self.host.next_block_id();
        self.host.append_block(
            Answer::new(
                id,
                names_spans(
                    "Agents",
                    &names,
                    "no agents · bundle has no agents: include: block",
                ),
            )
            .into(),
        );
    }

    /// `/diff [staged]` — the working-tree (or staged) git patch.
    pub fn show_diff(&self, arg: &str) {
        let staged = DIFF_STAGED_ARGS.contains(&arg.trim().to_lowercase().as_str());
        let patch = self.host.adapter().diff(staged);
        let id = self.host.next_block_id();
        self.host
            .append_block(Answer::new(id, diff_spans(patch.as_deref(), staged)).into());
    }

    /// `/skills` — the available-skills roster.
    pub fn show_skills(&self) {
        let skills = self.host.adapter().list_skills();
        let id = self.host.next_block_id();
        self.host
            .append_block(Answer::new(id, skills_spans(&skills)).into());
    }

    /// `/skill <name>` — load one skill into the session.
    pub fn load_skill(&self, name: &str) {
        if name.is_empty() {
            self.host
                .show_notice("usage: /skill <name> · /skills lists them");
            return;
        }
        if self.ops_starting() {
            return;
        }
        let (ok, payload) = self.host.adapter().load_skill(name);
        if ok {
            let id = self.host.next_block_id();
            self.host
                .append_block(Answer::new(id, skill_loaded_spans(name, &payload)).into());
            self.host.show_notice(&format!("skill loaded · {name}"));
        } else {
            // Python `payload or f"no such skill · {name}"`.
            let notice = if payload.is_empty() {
                format!("no such skill · {name}")
            } else {
                payload
            };
            self.host.show_notice(&notice);
        }
    }

    /// `/mcp [list|add|remove]` — configured servers + live tools.
    pub fn manage_mcp(&self, args: &str) {
        let parts: Vec<&str> = args.split_whitespace().collect();
        let sub = parts.first().map(|part| part.to_lowercase()).unwrap_or_default();
        let sub = if sub.is_empty() { "list".to_string() } else { sub };
        match sub.as_str() {
            "" | "list" => {
                let servers = self.host.mcp_servers();
                let live = self.host.adapter().mcp_tools();
                let id = self.host.next_block_id();
                self.host
                    .append_block(Answer::new(id, mcp_spans(&servers, &live)).into());
            }
            "add" => {
                if parts.len() < 3 {
                    self.host.show_notice("usage: /mcp add <name> <command> [args…]");
                    return;
                }
                let rest: Vec<String> =
                    parts[3..].iter().map(|part| part.to_string()).collect();
                self.host.add_mcp_stdio_server(parts[1], parts[2], &rest);
                self.host.show_notice(&format!(
                    "mcp server added · {} · restart the session to connect",
                    parts[1]
                ));
            }
            "remove" => {
                if parts.len() < 2 {
                    self.host.show_notice("usage: /mcp remove <name>");
                    return;
                }
                let removed = self.host.remove_mcp_server(parts[1]);
                let notice = if removed {
                    format!("mcp server removed · {} · restart to apply", parts[1])
                } else {
                    format!("no such server · {}", parts[1])
                };
                self.host.show_notice(&notice);
            }
            _ => {
                self.host
                    .show_notice(&format!("unknown /mcp subcommand · {sub} (list | add | remove)"));
            }
        }
    }

    /// `/bundle` — list deferred overlays; `load <name>` composes one.
    ///
    /// The in-session half of fast-boot deferral (`bundle.deferred`): heavy
    /// overlays skipped at boot are composed into the running session here
    /// on demand. Preparing an overlay can install modules (Python runs the
    /// compose on a worker); loading needs a live session.
    pub fn load_bundle(&self, args: &str) {
        let parts: Vec<&str> = args.split_whitespace().collect();
        let sub = parts.first().map(|part| part.to_lowercase()).unwrap_or_default();
        let sub = if sub.is_empty() { "list".to_string() } else { sub };
        if sub == "list" {
            self.list_deferred_bundles();
            return;
        }
        if sub == "load" {
            let name = if parts.len() > 1 { parts[1] } else { "" };
            if name.is_empty() {
                self.host
                    .show_notice("usage: /bundle load <name> · /bundle lists deferred");
                return;
            }
            if self.ops_starting() {
                return;
            }
            self.load_deferred(name);
            return;
        }
        // A bare `/bundle <name>` is the natural shorthand for `load <name>`.
        if self.ops_starting() {
            return;
        }
        self.load_deferred(parts[0]);
    }

    fn list_deferred_bundles(&self) {
        let names = self.host.adapter().deferred_bundles();
        let id = self.host.next_block_id();
        self.host.append_block(
            Answer::new(
                id,
                names_spans(
                    "Deferred overlays",
                    &names,
                    "none deferred · set bundle.deferred to hold heavy overlays back",
                ),
            )
            .into(),
        );
    }

    fn load_deferred(&self, name: &str) {
        let (ok, detail) = self.host.adapter().load_deferred_bundle(name);
        if ok {
            self.host.refresh_status(); // mounted tools/agents change the roster
        }
        let notice = if ok { format!("bundle · {detail}") } else { detail };
        self.host.show_notice(&notice);
    }
}

// ---------------------------------------------------------------------------
// Tests — port of tests/test_ui_session_ops_controller.py over a plain fake
// host + fake adapter (the same "no widget host involved" discipline).
// The Python fake's `workers_run` counter has no Rust analog (run_worker
// does not port); the gate-before-coordinator pins keep their real
// observable, `adapter.calls == []`.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;

    /// The RuntimeAdapter surface the controller touches — in memory.
    struct FakeAdapter {
        bundle_name: String,
        session_short: String,
        compaction: CompactionConfig,
        calls: RefCell<Vec<String>>,
        tools: RefCell<Vec<String>>,
        agents: Vec<String>,
        skills: Vec<SkillInfo>,
        models: ModelListing,
        status_info: StatusInfo,
        effort: Option<String>,
        patch: String,
        set_model_result: (bool, String),
        set_effort_result: (bool, String),
        compact_result: (bool, String),
        clear_result: (bool, u64),
        load_skill_result: (bool, String),
        deferred: RefCell<Vec<String>>,
        load_bundle_result: RefCell<(bool, String)>,
    }

    impl FakeAdapter {
        fn new() -> Self {
            Self {
                bundle_name: "dev-bundle".to_string(),
                session_short: "a1b2c3".to_string(),
                compaction: CompactionConfig::default(),
                calls: RefCell::new(Vec::new()),
                tools: RefCell::new(vec!["read".to_string(), "bash".to_string()]),
                agents: vec!["zen-architect".to_string()],
                skills: vec![SkillInfo {
                    name: "cranky-old-sam".to_string(),
                    description: "a reviewer".to_string(),
                    shortcut: "cosam".to_string(),
                }],
                models: ModelListing {
                    provider: "anthropic".to_string(),
                    current: "m1".to_string(),
                    available: vec!["m1".to_string(), "m2".to_string()],
                },
                status_info: StatusInfo {
                    session_id: "sess123456".to_string(),
                    provider: "anthropic".to_string(),
                    model: "m1".to_string(),
                    messages: 3,
                    tools: 2,
                    ..StatusInfo::default()
                },
                effort: Some("high".to_string()),
                patch: "diff --git a/x b/x\n+added line\n-removed line\n".to_string(),
                set_model_result: (true, "m2".to_string()),
                set_effort_result: (true, "medium".to_string()),
                compact_result: (true, "9 -> 1 messages".to_string()),
                clear_result: (true, 4),
                load_skill_result: (true, "# skill body".to_string()),
                deferred: RefCell::new(vec!["git+https://x/heavy@main".to_string()]),
                load_bundle_result: RefCell::new((
                    true,
                    "loaded · heavy · 2 module(s) mounted".to_string(),
                )),
            }
        }

        fn record(&self, entry: impl Into<String>) {
            self.calls.borrow_mut().push(entry.into());
        }

        fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }
    }

    impl SessionOpsAdapter for FakeAdapter {
        fn bundle_name(&self) -> String {
            self.bundle_name.clone()
        }

        fn session_short(&self) -> String {
            self.session_short.clone()
        }

        fn compaction(&self) -> CompactionConfig {
            self.compaction.clone()
        }

        fn status(&self) -> StatusInfo {
            self.record("status");
            self.status_info.clone()
        }

        fn set_model(&self, model: &str) -> (bool, String) {
            self.record(format!("set_model:{model}"));
            self.set_model_result.clone()
        }

        fn list_models(&self) -> ModelListing {
            self.record("list_models");
            self.models.clone()
        }

        fn set_effort(&self, level: &str) -> (bool, String) {
            self.record(format!("set_effort:{level}"));
            self.set_effort_result.clone()
        }

        fn get_effort(&self) -> Option<String> {
            self.record("get_effort");
            self.effort.clone()
        }

        fn compact(&self, focus: &str) -> (bool, String) {
            self.record(format!("compact:{focus}"));
            self.compact_result.clone()
        }

        fn clear_context(&self) -> (bool, u64) {
            self.record("clear_context");
            self.clear_result
        }

        fn list_tools(&self) -> Vec<String> {
            self.record("list_tools");
            self.tools.borrow().clone()
        }

        fn list_agents(&self) -> Vec<String> {
            self.record("list_agents");
            self.agents.clone()
        }

        fn diff(&self, staged: bool) -> Option<String> {
            // Python repr of the bool, so `calls` pins match verbatim.
            self.record(format!("diff:{}", if staged { "True" } else { "False" }));
            Some(self.patch.clone())
        }

        fn list_skills(&self) -> Vec<SkillInfo> {
            self.record("list_skills");
            self.skills.clone()
        }

        fn load_skill(&self, name: &str) -> (bool, String) {
            self.record(format!("load_skill:{name}"));
            self.load_skill_result.clone()
        }

        fn mcp_tools(&self) -> Vec<String> {
            self.record("mcp_tools");
            Vec::new()
        }

        fn deferred_bundles(&self) -> Vec<String> {
            self.record("deferred_bundles");
            self.deferred.borrow().clone()
        }

        fn load_deferred_bundle(&self, name: &str) -> (bool, String) {
            self.record(format!("load_deferred_bundle:{name}"));
            self.load_bundle_result.borrow().clone()
        }
    }

    /// A SessionOpsHost that is emphatically NOT a widget app.
    struct FakeHost {
        adapter: FakeAdapter,
        allocator: RefCell<crate::model::blocks::BlockIdAllocator>,
        mode_id: String,
        session_cost: Decimal,
        splash_active: bool,
        blocks: RefCell<Vec<TranscriptBlock>>,
        notices: RefCell<Vec<String>>,
        status_refreshes: RefCell<u32>,
        mcp_servers: Vec<(String, String)>,
        mcp_added: RefCell<Vec<String>>,
        mcp_removed: RefCell<Vec<String>>,
    }

    impl FakeHost {
        fn new(adapter: FakeAdapter) -> Self {
            Self::with_splash(adapter, false)
        }

        fn with_splash(adapter: FakeAdapter, splash_active: bool) -> Self {
            Self {
                adapter,
                allocator: RefCell::new(crate::model::blocks::BlockIdAllocator::new()),
                mode_id: "auto".to_string(),
                session_cost: Decimal::new(150, 2), // 1.50
                splash_active,
                blocks: RefCell::new(Vec::new()),
                notices: RefCell::new(Vec::new()),
                status_refreshes: RefCell::new(0),
                mcp_servers: Vec::new(),
                mcp_added: RefCell::new(Vec::new()),
                mcp_removed: RefCell::new(Vec::new()),
            }
        }

        fn notices(&self) -> Vec<String> {
            self.notices.borrow().clone()
        }

        fn block_text(&self, index: usize) -> String {
            let blocks = self.blocks.borrow();
            match &blocks[index] {
                TranscriptBlock::Answer(answer) => {
                    answer.spans.iter().map(|seg| seg.text.as_str()).collect()
                }
                other => panic!("expected an Answer block, got {}", other.kind()),
            }
        }

        fn status_refreshes(&self) -> u32 {
            *self.status_refreshes.borrow()
        }
    }

    impl SessionOpsHost for FakeHost {
        fn adapter(&self) -> &dyn SessionOpsAdapter {
            &self.adapter
        }

        fn next_block_id(&self) -> String {
            self.allocator.borrow_mut().next_id()
        }

        fn mode_id(&self) -> String {
            self.mode_id.clone()
        }

        fn session_cost(&self) -> Decimal {
            self.session_cost
        }

        fn splash_active(&self) -> bool {
            self.splash_active
        }

        fn append_block(&self, block: TranscriptBlock) {
            self.blocks.borrow_mut().push(block);
        }

        fn show_notice(&self, text: &str) {
            self.notices.borrow_mut().push(text.to_string());
        }

        fn refresh_status(&self) {
            *self.status_refreshes.borrow_mut() += 1;
        }

        fn mcp_servers(&self) -> Vec<(String, String)> {
            self.mcp_servers.clone()
        }

        fn add_mcp_stdio_server(&self, name: &str, command: &str, args: &[String]) {
            self.mcp_added
                .borrow_mut()
                .push(format!("{name}:{command}:{}", args.join(",")));
        }

        fn remove_mcp_server(&self, name: &str) -> bool {
            self.mcp_removed.borrow_mut().push(name.to_string());
            false
        }
    }

    fn host() -> FakeHost {
        FakeHost::new(FakeAdapter::new())
    }

    // Python: test_controller_needs_no_textual_app — the isinstance(App)
    // half is meaningless here (the fake is a plain struct by construction);
    // the "it still worked" half ports.
    #[test]
    fn test_controller_needs_no_textual_app() {
        let host = host();
        SessionOpsController::new(&host).show_tools();
        assert!(!host.blocks.borrow().is_empty()); // it still worked
    }

    // Python: test_show_tools_appends_roster
    #[test]
    fn test_show_tools_appends_roster() {
        let host = host();
        SessionOpsController::new(&host).show_tools();
        assert_eq!(host.adapter.calls(), vec!["list_tools"]);
        assert_eq!(host.blocks.borrow().len(), 1);
        let body = host.block_text(0);
        assert!(body.contains("Tools") && body.contains("read") && body.contains("bash"));
    }

    // Python: test_show_tools_empty
    #[test]
    fn test_show_tools_empty() {
        let host = host();
        host.adapter.tools.borrow_mut().clear();
        SessionOpsController::new(&host).show_tools();
        assert!(host.block_text(0).contains("no tools mounted"));
    }

    // Python: test_show_agents_appends_roster
    #[test]
    fn test_show_agents_appends_roster() {
        let host = host();
        SessionOpsController::new(&host).show_agents();
        assert_eq!(host.adapter.calls(), vec!["list_agents"]);
        assert!(host.block_text(0).contains("Agents"));
        assert!(host.block_text(0).contains("zen-architect"));
    }

    // Python: test_show_status_appends_block
    #[test]
    fn test_show_status_appends_block() {
        let host = host();
        SessionOpsController::new(&host).show_status();
        assert_eq!(host.adapter.calls(), vec!["status"]);
        let body = host.block_text(0);
        assert!(body.contains("Status") && body.contains("dev-bundle") && body.contains("$1.50"));
    }

    // Python: test_show_model_no_arg_lists
    #[test]
    fn test_show_model_no_arg_lists() {
        let host = host();
        SessionOpsController::new(&host).show_model("");
        assert_eq!(host.adapter.calls(), vec!["list_models"]);
        assert!(host.block_text(0).contains("anthropic"));
    }

    // Python: test_show_model_arg_switches
    #[test]
    fn test_show_model_arg_switches() {
        let host = host();
        SessionOpsController::new(&host).show_model("m2");
        assert_eq!(host.adapter.calls(), vec!["set_model:m2"]);
        assert_eq!(host.status_refreshes(), 1); // footer model field is adapter-derived
        assert_eq!(host.notices(), vec!["model · m2"]);
        assert!(host.blocks.borrow().is_empty());
    }

    // Python: test_apply_effort_shows_current
    #[test]
    fn test_apply_effort_shows_current() {
        let host = host();
        SessionOpsController::new(&host).apply_effort("");
        assert_eq!(host.adapter.calls(), vec!["get_effort"]);
        assert_eq!(host.notices(), vec!["effort · high · /effort <level> to set"]);
    }

    // Python: test_apply_effort_sets
    #[test]
    fn test_apply_effort_sets() {
        let host = host();
        SessionOpsController::new(&host).apply_effort("medium");
        assert_eq!(host.adapter.calls(), vec!["set_effort:medium"]);
        assert_eq!(host.notices(), vec!["effort · medium"]);
    }

    // Python: test_compact_context_notice
    #[test]
    fn test_compact_context_notice() {
        let host = host();
        SessionOpsController::new(&host).compact_context("tests");
        assert_eq!(host.adapter.calls(), vec!["compact:tests"]);
        assert_eq!(host.notices(), vec!["compacted · 9 -> 1 messages"]);
    }

    // Python: test_clear_context_notice
    #[test]
    fn test_clear_context_notice() {
        let host = host();
        SessionOpsController::new(&host).clear_context();
        assert_eq!(host.adapter.calls(), vec!["clear_context"]);
        assert_eq!(host.notices(), vec!["context cleared · 4 messages dropped"]);
    }

    // Python: test_show_diff_unstaged
    #[test]
    fn test_show_diff_unstaged() {
        let host = host();
        SessionOpsController::new(&host).show_diff("");
        assert_eq!(host.adapter.calls(), vec!["diff:False"]);
        assert!(host.block_text(0).contains("added line"));
    }

    // Python: test_show_diff_staged_arg
    #[test]
    fn test_show_diff_staged_arg() {
        let host = host();
        SessionOpsController::new(&host).show_diff("staged");
        assert_eq!(host.adapter.calls(), vec!["diff:True"]);
    }

    // Python: test_show_skills_roster
    #[test]
    fn test_show_skills_roster() {
        let host = host();
        SessionOpsController::new(&host).show_skills();
        assert_eq!(host.adapter.calls(), vec!["list_skills"]);
        assert!(host.block_text(0).contains("Skills"));
    }

    // Python: test_load_skill_requires_name — the `workers_run == 0` pin
    // becomes "no adapter call" (run_worker does not port).
    #[test]
    fn test_load_skill_requires_name() {
        let host = host();
        SessionOpsController::new(&host).load_skill("");
        assert!(host.adapter.calls().is_empty()); // never reached the coordinator
        assert_eq!(host.notices(), vec!["usage: /skill <name> · /skills lists them"]);
    }

    // Python: test_load_skill_loads
    #[test]
    fn test_load_skill_loads() {
        let host = host();
        SessionOpsController::new(&host).load_skill("cranky-old-sam");
        assert_eq!(host.adapter.calls(), vec!["load_skill:cranky-old-sam"]);
        assert!(host.block_text(0).contains("Skill loaded"));
        assert_eq!(host.notices(), vec!["skill loaded · cranky-old-sam"]);
    }

    // Python: test_ops_starting_gates_the_coordinator
    #[test]
    fn test_ops_starting_gates_the_coordinator() {
        let host = FakeHost::with_splash(FakeAdapter::new(), true);
        SessionOpsController::new(&host).compact_context("x");
        assert!(host.adapter.calls().is_empty()); // gated before any worker ran
        assert_eq!(
            host.notices(),
            vec!["session still starting · try again once the banner lands"]
        );
    }

    // Python: test_manage_mcp_add_usage
    #[test]
    fn test_manage_mcp_add_usage() {
        let host = host();
        SessionOpsController::new(&host).manage_mcp("add only-two");
        assert_eq!(host.notices(), vec!["usage: /mcp add <name> <command> [args…]"]);
        assert!(host.blocks.borrow().is_empty());
    }

    // Python: test_manage_mcp_list — the monkeypatched mcp_config functions
    // are the host's mcp_servers() here (empty by default in the fake).
    #[test]
    fn test_manage_mcp_list() {
        let host = host();
        SessionOpsController::new(&host).manage_mcp("");
        assert!(host.adapter.calls().contains(&"mcp_tools".to_string()));
        assert!(host.block_text(0).contains("MCP"));
    }

    // -----------------------------------------------------------------------
    // /bundle — deferred overlay listing + on-demand in-session load
    // -----------------------------------------------------------------------

    // Python: test_bundle_bare_lists_deferred
    #[test]
    fn test_bundle_bare_lists_deferred() {
        let host = host();
        SessionOpsController::new(&host).load_bundle("");
        assert_eq!(host.adapter.calls(), vec!["deferred_bundles"]);
        let body = host.block_text(0);
        assert!(body.contains("Deferred overlays") && body.contains("heavy"));
    }

    // Python: test_bundle_list_when_none_deferred
    #[test]
    fn test_bundle_list_when_none_deferred() {
        let host = host();
        host.adapter.deferred.borrow_mut().clear();
        SessionOpsController::new(&host).load_bundle("list");
        assert!(host.block_text(0).contains("none deferred"));
    }

    // Python: test_bundle_load_composes
    #[test]
    fn test_bundle_load_composes() {
        let host = host();
        SessionOpsController::new(&host).load_bundle("load heavy");
        assert_eq!(host.adapter.calls(), vec!["load_deferred_bundle:heavy"]);
        assert_eq!(host.status_refreshes(), 1); // mounted tools/agents change the roster
        assert_eq!(host.notices(), vec!["bundle · loaded · heavy · 2 module(s) mounted"]);
    }

    // Python: test_bundle_load_shorthand
    #[test]
    fn test_bundle_load_shorthand() {
        // `/bundle heavy` is shorthand for `/bundle load heavy`.
        let host = host();
        SessionOpsController::new(&host).load_bundle("heavy");
        assert_eq!(host.adapter.calls(), vec!["load_deferred_bundle:heavy"]);
    }

    // Python: test_bundle_load_missing_name
    #[test]
    fn test_bundle_load_missing_name() {
        let host = host();
        SessionOpsController::new(&host).load_bundle("load");
        assert!(host.adapter.calls().is_empty());
        assert_eq!(host.notices(), vec!["usage: /bundle load <name> · /bundle lists deferred"]);
    }

    // Python: test_bundle_load_failure_notices
    #[test]
    fn test_bundle_load_failure_notices() {
        let host = host();
        *host.adapter.load_bundle_result.borrow_mut() = (
            false,
            "'heavy' is not a deferred bundle · deferred: none".to_string(),
        );
        SessionOpsController::new(&host).load_bundle("load heavy");
        assert_eq!(host.status_refreshes(), 0); // nothing mounted
        assert_eq!(
            host.notices(),
            vec!["'heavy' is not a deferred bundle · deferred: none"]
        );
    }

    // Python: test_bundle_load_gated_while_starting
    #[test]
    fn test_bundle_load_gated_while_starting() {
        let host = FakeHost::with_splash(FakeAdapter::new(), true);
        SessionOpsController::new(&host).load_bundle("load heavy");
        assert!(host.adapter.calls().is_empty());
        assert_eq!(
            host.notices(),
            vec!["session still starting · try again once the banner lands"]
        );
    }
}
