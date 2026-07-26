//! The built-in command set — descriptions verbatim from the mockup.
//!
//! Port of `src/amplifier_app_newtui/commands/builtin.py`.
//!
//! Each handler acts on the app only through the
//! [`crate::commands::registry::CommandContext`] trait (posting messages /
//! mutating model state). The table below IS the mockup `COMMANDS` array
//! (group, name, description, tag) — the palette, help and keybinds all
//! read this one registry (DESIGN-SPEC §6).

use crate::commands::context::{build_context_block, ContextUsage, DEFAULT_BAR_WIDTH};
use crate::commands::doctor::{
    build_doctor_block, default_settings_paths, run_checks, DoctorInputs, McpServerStats,
    EXECUTABLE_NAME, PACKAGE_NAME,
};
use crate::commands::improve::{
    build_improve_block, improve_proposals, ApprovalTally, OverriddenDenial,
    MIN_ALLOWLIST_APPROVALS, MIN_OVERRIDDEN_DENIALS,
};
use crate::commands::registry::{
    CommandContext, CommandGroup, CommandHandler, CommandRegistry, CommandSpec,
};
use crate::model::blocks::{LedgerBlock, SessionBanner};
use crate::model::modes::MODE_PROFILES;
use std::sync::Arc;

/// `/mode` — cycle postures; `/mode plan` — jump to a posture;
/// `/mode <bundle-mode>` — ADD a native, bundle-composed mode
/// (superpowers, careful, audit, …) to the active set through the mounted
/// mode tool; `/mode off` — clear all native modes; `/mode off <name>`
/// or `/mode -<name>` — remove a single native mode from the set.
fn cmd_mode(ctx: &dyn CommandContext, args: &str) {
    let target = args.trim().to_lowercase();
    if target.is_empty() {
        ctx.cycle_mode();
    } else if MODE_PROFILES
        .iter()
        .any(|profile| profile.id.as_str() == target)
    {
        ctx.set_mode(&target);
    } else if target == "off" {
        ctx.set_native_mode(None);
    } else if let Some(rest) = target.strip_prefix("off ") {
        ctx.remove_native_mode(rest.trim());
    } else if let Some(rest) = target.strip_prefix('-') {
        if rest.trim().is_empty() {
            // Python: a bare `-` falls through to set_native_mode.
            ctx.set_native_mode(Some(&target));
        } else {
            ctx.remove_native_mode(rest.trim());
        }
    } else {
        ctx.set_native_mode(Some(&target));
    }
}

/// `/modes` — list the bundle-composed native modes + postures.
fn cmd_modes(ctx: &dyn CommandContext, _args: &str) {
    ctx.show_modes();
}

fn cmd_plan(ctx: &dyn CommandContext, _args: &str) {
    ctx.set_mode("plan");
}

fn cmd_brainstorm(ctx: &dyn CommandContext, _args: &str) {
    ctx.set_mode("brainstorm");
}

fn cmd_context(ctx: &dyn CommandContext, _args: &str) {
    // Python: `assert isinstance(usage, ContextUsage)`.
    let usage = ctx
        .context_usage()
        .downcast::<ContextUsage>()
        .expect("context_usage() must return a ContextUsage");
    ctx.post_block(build_context_block(&ctx.next_block_id(), &usage, DEFAULT_BAR_WIDTH).into());
}

/// `/config` — show/toggle/set/diff/save the live session config.
fn cmd_config(ctx: &dyn CommandContext, args: &str) {
    ctx.manage_config(args.trim());
}

fn cmd_tasks(ctx: &dyn CommandContext, _args: &str) {
    ctx.toggle_lanes();
}

/// `/status` — live session snapshot (model, mode, messages, cost).
fn cmd_status(ctx: &dyn CommandContext, _args: &str) {
    ctx.show_status();
}

/// `/model` — list models; `/model <name>` switches the live model.
fn cmd_model(ctx: &dyn CommandContext, args: &str) {
    ctx.show_model(args.trim());
}

/// `/effort` — show reasoning effort; `/effort <level>` sets it.
fn cmd_effort(ctx: &dyn CommandContext, args: &str) {
    ctx.apply_effort(args.trim());
}

/// `/compact` — compact context; `/compact <focus>` steers it.
fn cmd_compact(ctx: &dyn CommandContext, args: &str) {
    ctx.compact_context(args.trim());
}

/// `/clear` — clear the conversation context.
fn cmd_clear(ctx: &dyn CommandContext, _args: &str) {
    ctx.clear_context();
}

/// `/tools` — list the mounted tools.
fn cmd_tools(ctx: &dyn CommandContext, _args: &str) {
    ctx.show_tools();
}

/// `/agents` — list the delegatable agents.
fn cmd_agents(ctx: &dyn CommandContext, _args: &str) {
    ctx.show_agents();
}

/// `/diff` — working-tree patch; `/diff staged` for the cached diff.
fn cmd_diff(ctx: &dyn CommandContext, args: &str) {
    ctx.show_diff(args.trim());
}

/// `/skills` — list the available skills.
fn cmd_skills(ctx: &dyn CommandContext, _args: &str) {
    ctx.show_skills();
}

/// `/skill <name>` — load a skill via the mounted skills tool.
fn cmd_skill(ctx: &dyn CommandContext, args: &str) {
    ctx.load_skill(args.trim());
}

/// `/mcp` — list; `/mcp add|remove` manages MCP servers (mcp.json).
fn cmd_mcp(ctx: &dyn CommandContext, args: &str) {
    ctx.manage_mcp(args.trim());
}

/// `/bundle` — list deferred overlays; `/bundle load <name>` composes
/// one into the running session (fast-boot deferral, `bundle.deferred`).
fn cmd_bundle(ctx: &dyn CommandContext, args: &str) {
    ctx.load_bundle(args.trim());
}

fn cmd_ledger(ctx: &dyn CommandContext, _args: &str) {
    let (turns, shipped, answer_only, cache_hit_pct) = {
        let ledger = ctx.ledger().lock().unwrap();
        (
            ledger.turn_count() as u64,
            ledger.shipped_count() as u64,
            ledger.answer_only_count() as u64,
            ledger.cache_hit_pct(),
        )
    };
    ctx.post_block(
        LedgerBlock {
            id: ctx.next_block_id(),
            session: ctx.session_short(),
            bundle: ctx.bundle_name(),
            turns,
            // Mockup cmdLedger prints ``this.cost`` — the session cost the
            // footer shows (includes any pre-session baseline).
            spend: ctx.session_cost(),
            shipped,
            answer_only,
            cache_hit_pct,
        }
        .into(),
    );
    // Mockup cmdLedger ends with this exact notice.
    ctx.show_notice("ledger printed to scrollback");
}

fn cmd_rewind(ctx: &dyn CommandContext, _args: &str) {
    ctx.open_rewind();
}

/// `/rename <name>` — label the current session for the resume picker.
fn cmd_rename(ctx: &dyn CommandContext, args: &str) {
    ctx.rename_session(args.trim());
}

/// `/sessions` — list this project's stored sessions.
fn cmd_sessions(ctx: &dyn CommandContext, _args: &str) {
    ctx.show_sessions();
}

/// `/branch [name]` — snapshot this conversation into a new session.
fn cmd_branch(ctx: &dyn CommandContext, args: &str) {
    ctx.branch_session(args.trim());
}

/// `/fork <directive>` — snapshot into a new session primed to run it.
fn cmd_fork(ctx: &dyn CommandContext, args: &str) {
    ctx.fork_session(args.trim());
}

fn cmd_permissions(ctx: &dyn CommandContext, _args: &str) {
    ctx.open_permissions();
}

fn cmd_allowed_dirs(ctx: &dyn CommandContext, args: &str) {
    ctx.manage_directories("allowed", args);
}

fn cmd_denied_dirs(ctx: &dyn CommandContext, args: &str) {
    ctx.manage_directories("denied", args);
}

/// Stand-in for Python's `importlib.metadata.version(package)` probe:
/// there is no package-metadata registry for a Rust binary — the running
/// executable IS the installation, so the probe asks the OS for it.
fn install_probe(_package: &str) -> bool {
    std::env::current_exe().is_ok()
}

fn cmd_doctor(ctx: &dyn CommandContext, _args: &str) {
    // Python filters with `isinstance(...)` — non-matching rows are skipped.
    let mcp_stats: Vec<McpServerStats> = ctx
        .mcp_server_stats()
        .into_iter()
        .filter_map(|stat| stat.downcast::<McpServerStats>().ok().map(|boxed| *boxed))
        .collect();
    let tallies: Vec<ApprovalTally> = ctx
        .approval_tallies()
        .into_iter()
        .filter_map(|tally| tally.downcast::<ApprovalTally>().ok().map(|boxed| *boxed))
        .collect();
    let settings_paths = default_settings_paths();
    let report = run_checks(&DoctorInputs {
        mcp_stats: &mcp_stats,
        approval_tallies: &tallies,
        settings_paths: &settings_paths,
        package: PACKAGE_NAME,
        executable: EXECUTABLE_NAME,
        anchors_status: None,
        probe_installed: &install_probe,
    });
    ctx.post_block(build_doctor_block(&ctx.next_block_id(), &report).into());
}

/// `/export` — write the transcript markdown, notice the path.
fn cmd_export(ctx: &dyn CommandContext, _args: &str) {
    ctx.show_notice(&format!(
        "transcript exported · {}",
        ctx.export_transcript()
    ));
}

/// `/copy` — copy the last answer to the clipboard, notice the char count.
fn cmd_copy(ctx: &dyn CommandContext, _args: &str) {
    let n = ctx.copy_answer();
    if n == 0 {
        ctx.show_notice("no answer to copy yet");
        return;
    }
    ctx.show_notice(&format!(
        "copied · {n} chars · empty clipboard? allow terminal clipboard access"
    ));
}

/// `/about` — post the app/core/bundle/session identity as a block
/// (the same data the session banner shows).
fn cmd_about(ctx: &dyn CommandContext, _args: &str) {
    let (app_version, core_version, bundle, session) = ctx.about_info();
    ctx.post_block(
        SessionBanner {
            id: ctx.next_block_id(),
            headline: format!("Amplifier {app_version} · core {core_version}"),
            detail: format!("Bundle: {bundle} | session {session}"),
            focus_note: String::new(),
        }
        .into(),
    );
}

/// `/quit` — exit the app (amplifier-app-cli parity: exit/quit).
fn cmd_quit(ctx: &dyn CommandContext, _args: &str) {
    ctx.quit_app();
}

/// `/theme` — cycle; `/theme graphite` — jump to a theme (spec §1).
fn cmd_theme(ctx: &dyn CommandContext, args: &str) {
    ctx.set_theme(&args.trim().to_lowercase());
}

fn cmd_improve(ctx: &dyn CommandContext, _args: &str) {
    let tallies: Vec<ApprovalTally> = ctx
        .approval_tallies()
        .into_iter()
        .filter_map(|tally| tally.downcast::<ApprovalTally>().ok().map(|boxed| *boxed))
        .collect();
    let overrides: Vec<OverriddenDenial> = ctx
        .overridden_denials()
        .into_iter()
        .filter_map(|row| row.downcast::<OverriddenDenial>().ok().map(|boxed| *boxed))
        .collect();
    let proposals = {
        let ledger = ctx.ledger().lock().unwrap();
        improve_proposals(
            &tallies,
            &overrides,
            Some(&ledger),
            MIN_ALLOWLIST_APPROVALS,
            MIN_OVERRIDDEN_DENIALS,
        )
    };
    ctx.post_block(build_improve_block(&ctx.next_block_id(), proposals).into());
}

/// Build one validated builtin spec (the table is a programming artifact —
/// an invalid row is a bug, exactly like Python's import-time `ValueError`).
fn spec(
    group: CommandGroup,
    name: &str,
    desc: &str,
    tag: &str,
    handler: fn(&dyn CommandContext, &str),
) -> CommandSpec {
    let handler: CommandHandler = Arc::new(handler);
    CommandSpec::new(group, name, desc, tag, handler).expect("builtin command spec is valid")
}

/// The mockup COMMANDS table, verbatim (group, name, description, tag) —
/// Python module constant `BUILTIN_COMMANDS` (a fresh equal table per call;
/// handlers are plain fns so every row behaves identically across calls).
pub fn builtin_commands() -> Vec<CommandSpec> {
    vec![
        spec(
            CommandGroup::During,
            "/mode",
            "cycle or jump posture: chat, plan, brainstorm, build, auto",
            "built-in",
            cmd_mode,
        )
        .with_key_action("cycle_mode"),
        // Beyond the mockup table: bundle-composed native modes (superpowers
        // et al) — discovered from the session, never hardcoded here.
        spec(
            CommandGroup::During,
            "/modes",
            "list native bundle modes; /mode <name> activates",
            "built-in",
            cmd_modes,
        ),
        spec(
            CommandGroup::During,
            "/plan",
            "read-only planning; hands the plan to build",
            "built-in",
            cmd_plan,
        ),
        spec(
            CommandGroup::During,
            "/brainstorm",
            "no tools, divergent output; /plan to converge",
            "built-in",
            cmd_brainstorm,
        ),
        spec(
            CommandGroup::During,
            "/context",
            "context usage grid + suggestions",
            "built-in",
            cmd_context,
        ),
        // Live session config editor (amplifier-app-cli /config parity).
        spec(
            CommandGroup::During,
            "/config",
            "live config: show · toggle · set · diff · save",
            "built-in",
            cmd_config,
        ),
        // In-session ops over the live amplifier coordinator (app-cli parity).
        spec(
            CommandGroup::During,
            "/status",
            "session status: model, mode, messages, cost",
            "built-in",
            cmd_status,
        ),
        spec(
            CommandGroup::During,
            "/model",
            "list models; /model <name> switches the live model",
            "built-in",
            cmd_model,
        ),
        spec(
            CommandGroup::During,
            "/effort",
            "reasoning effort; /effort <none…max> sets it",
            "built-in",
            cmd_effort,
        ),
        spec(
            CommandGroup::During,
            "/compact",
            "compact context; /compact <focus> to steer it",
            "built-in",
            cmd_compact,
        ),
        spec(
            CommandGroup::During,
            "/clear",
            "clear the conversation context",
            "built-in",
            cmd_clear,
        ),
        spec(
            CommandGroup::During,
            "/tools",
            "list the mounted tools",
            "built-in",
            cmd_tools,
        ),
        spec(
            CommandGroup::During,
            "/agents",
            "list the delegatable agents",
            "built-in",
            cmd_agents,
        ),
        spec(
            CommandGroup::During,
            "/skills",
            "list available skills",
            "skill",
            cmd_skills,
        ),
        spec(
            CommandGroup::During,
            "/skill",
            "load a skill by name: /skill <name>",
            "skill",
            cmd_skill,
        ),
        spec(
            CommandGroup::During,
            "/mcp",
            "MCP servers: list · add · remove",
            "built-in",
            cmd_mcp,
        ),
        // Fast boot: heavy bundle.app overlays are deferred and composed on
        // demand here, into the running session (kernel/bundle_compose).
        spec(
            CommandGroup::During,
            "/bundle",
            "deferred overlays; /bundle load <name> composes one now",
            "built-in",
            cmd_bundle,
        ),
        spec(
            CommandGroup::Parallel,
            "/tasks",
            "agent lanes: one line per subagent",
            "built-in",
            cmd_tasks,
        )
        .with_key_action("toggle_lanes"),
        spec(
            CommandGroup::Ship,
            "/ledger",
            "session outcome ledger: spend vs yield",
            "built-in",
            cmd_ledger,
        )
        .with_key_action("show_ledger"),
        // Beyond the mockup table: transcript markdown export.
        spec(
            CommandGroup::Ship,
            "/export",
            "write transcript markdown to exports/",
            "built-in",
            cmd_export,
        ),
        // Beyond the mockup table: last-answer clipboard copy.
        spec(
            CommandGroup::Ship,
            "/copy",
            "copy last answer to clipboard (OSC 52)",
            "built-in",
            cmd_copy,
        ),
        // In-session ops (app-cli parity): review the working-tree diff.
        spec(
            CommandGroup::Ship,
            "/diff",
            "working-tree diff; /diff staged for the cached diff",
            "built-in",
            cmd_diff,
        ),
        // Beyond the mockup table: app/core/bundle/session identity block.
        spec(
            CommandGroup::Ship,
            "/about",
            "app, core, bundle + session identity",
            "built-in",
            cmd_about,
        ),
        spec(
            CommandGroup::Between,
            "/rewind",
            "fork from any turn-rule checkpoint",
            "built-in",
            cmd_rewind,
        )
        .with_key_action("open_rewind"),
        // Stored-session lifecycle (amplifier-app-cli parity: /rename, session
        // picker, the /branch fork family) — the persisted counterparts to the
        // in-memory /rewind.
        spec(
            CommandGroup::Between,
            "/rename",
            "name this session for the resume picker",
            "built-in",
            cmd_rename,
        ),
        spec(
            CommandGroup::Between,
            "/sessions",
            "list stored sessions for this project",
            "built-in",
            cmd_sessions,
        ),
        spec(
            CommandGroup::Between,
            "/branch",
            "snapshot this conversation into a new session",
            "built-in",
            cmd_branch,
        ),
        spec(
            CommandGroup::Between,
            "/fork",
            "snapshot into a new session primed to run a directive",
            "built-in",
            cmd_fork,
        ),
        // Beyond the mockup table: exit path (amplifier-app-cli parity).
        spec(
            CommandGroup::Between,
            "/quit",
            "exit the app (ctrl-d works too)",
            "built-in",
            cmd_quit,
        ),
        spec(
            CommandGroup::Repair,
            "/permissions",
            "edit trust slots: boundary, blocks, exceptions",
            "built-in",
            cmd_permissions,
        ),
        spec(
            CommandGroup::Repair,
            "/allowed-dirs",
            "list or edit session allowed write directories",
            "built-in",
            cmd_allowed_dirs,
        ),
        spec(
            CommandGroup::Repair,
            "/denied-dirs",
            "list or edit session denied write directories",
            "built-in",
            cmd_denied_dirs,
        ),
        spec(
            CommandGroup::Repair,
            "/doctor",
            "setup checkup; reports, then fixes on confirm",
            "skill",
            cmd_doctor,
        ),
        spec(
            CommandGroup::Repair,
            "/improve",
            "tune config from ledger + denial log",
            "skill",
            cmd_improve,
        ),
        // Runtime theme switch (DESIGN-SPEC §1) — the one command beyond the
        // mockup COMMANDS table ("themes … in Tweaks" has no TUI equivalent).
        spec(
            CommandGroup::Repair,
            "/theme",
            "switch theme: slate, graphite, carbon",
            "built-in",
            cmd_theme,
        ),
    ]
}

/// A fresh registry loaded with the built-in command set.
pub fn build_registry() -> CommandRegistry {
    CommandRegistry::with_specs(builtin_commands()).expect("builtin command table has no duplicates")
}

// ---------------------------------------------------------------------------
// Tests — port of tests/test_commands_builtin.py (all cases). The fake
// context replicates tests/conftest.py's FakeCommandContext (registry.rs's
// port of the same fixture is #[cfg(test)]-private there, so it is
// re-stated here with the extra mutable knobs the builtin tests poke:
// session_cost, answer_chars, mcp_stats, tallies, overrides).
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use std::any::Any;
    use std::collections::HashSet;
    use std::sync::Mutex;

    use rust_decimal::Decimal;

    use super::*;
    use crate::model::blocks::{BlockIdAllocator, TranscriptBlock};
    use crate::model::queues::{NeedsYouQueue, SteeringQueue};
    use crate::model::trust::DenialLog;
    use crate::model::turn::{OutcomeKind, OutcomeLedger, TurnOutcome, TurnTelemetry};

    /// Port of `tests/conftest.py::FakeCommandContext` — records every
    /// action a command handler takes.
    struct FakeCommandContext {
        ledger: Mutex<OutcomeLedger>,
        denial_log: Mutex<DenialLog>,
        steering: SteeringQueue,
        needs_you: NeedsYouQueue,
        ids: Mutex<BlockIdAllocator>,
        session_cost: Mutex<Decimal>,
        mcp_stats: Mutex<Vec<McpServerStats>>,
        tallies: Mutex<Vec<ApprovalTally>>,
        overrides: Mutex<Vec<OverriddenDenial>>,
        answer_chars: Mutex<usize>,
        user_lines: Mutex<Vec<String>>,
        blocks: Mutex<Vec<TranscriptBlock>>,
        notices: Mutex<Vec<String>>,
        calls: Mutex<Vec<String>>,
    }

    impl FakeCommandContext {
        fn new() -> Self {
            Self {
                ledger: Mutex::new(OutcomeLedger::new()),
                denial_log: Mutex::new(DenialLog::new()),
                steering: SteeringQueue::new(),
                needs_you: NeedsYouQueue::new(),
                ids: Mutex::new(BlockIdAllocator::new()),
                session_cost: Mutex::new(Decimal::ZERO),
                mcp_stats: Mutex::new(Vec::new()),
                tallies: Mutex::new(Vec::new()),
                overrides: Mutex::new(Vec::new()),
                answer_chars: Mutex::new(42),
                user_lines: Mutex::new(Vec::new()),
                blocks: Mutex::new(Vec::new()),
                notices: Mutex::new(Vec::new()),
                calls: Mutex::new(Vec::new()),
            }
        }

        fn record(&self, entry: impl Into<String>) {
            self.calls.lock().unwrap().push(entry.into());
        }

        fn user_lines(&self) -> Vec<String> {
            self.user_lines.lock().unwrap().clone()
        }

        fn blocks(&self) -> Vec<TranscriptBlock> {
            self.blocks.lock().unwrap().clone()
        }

        fn notices(&self) -> Vec<String> {
            self.notices.lock().unwrap().clone()
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
            *self.session_cost.lock().unwrap()
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
            Box::new(ContextUsage::new(52_000, 18_000, 8_000).expect("fixture usage is valid"))
        }

        fn approval_tallies(&self) -> Vec<Box<dyn Any>> {
            self.tallies
                .lock()
                .unwrap()
                .iter()
                .map(|tally| Box::new(tally.clone()) as Box<dyn Any>)
                .collect()
        }

        fn overridden_denials(&self) -> Vec<Box<dyn Any>> {
            self.overrides
                .lock()
                .unwrap()
                .iter()
                .map(|row| Box::new(row.clone()) as Box<dyn Any>)
                .collect()
        }

        fn mcp_server_stats(&self) -> Vec<Box<dyn Any>> {
            self.mcp_stats
                .lock()
                .unwrap()
                .iter()
                .map(|stat| Box::new(stat.clone()) as Box<dyn Any>)
                .collect()
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
            *self.answer_chars.lock().unwrap()
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

    fn dec(value: &str) -> Decimal {
        value.parse().expect("valid decimal literal")
    }

    /// The mockup COMMANDS table, verbatim: (group, name, desc, tag).
    const MOCKUP_TABLE: [(&str, &str, &str, &str); 35] = [
        (
            "During",
            "/mode",
            "cycle or jump posture: chat, plan, brainstorm, build, auto",
            "built-in",
        ),
        // Beyond the mockup table: bundle-composed native modes (dynamic).
        (
            "During",
            "/modes",
            "list native bundle modes; /mode <name> activates",
            "built-in",
        ),
        (
            "During",
            "/plan",
            "read-only planning; hands the plan to build",
            "built-in",
        ),
        (
            "During",
            "/brainstorm",
            "no tools, divergent output; /plan to converge",
            "built-in",
        ),
        (
            "During",
            "/context",
            "context usage grid + suggestions",
            "built-in",
        ),
        // Live session config editor (amplifier-app-cli /config parity).
        (
            "During",
            "/config",
            "live config: show \u{b7} toggle \u{b7} set \u{b7} diff \u{b7} save",
            "built-in",
        ),
        // Beyond the mockup table: in-session ops over the live coordinator
        // (amplifier-app-cli parity).
        (
            "During",
            "/status",
            "session status: model, mode, messages, cost",
            "built-in",
        ),
        (
            "During",
            "/model",
            "list models; /model <name> switches the live model",
            "built-in",
        ),
        (
            "During",
            "/effort",
            "reasoning effort; /effort <none…max> sets it",
            "built-in",
        ),
        (
            "During",
            "/compact",
            "compact context; /compact <focus> to steer it",
            "built-in",
        ),
        (
            "During",
            "/clear",
            "clear the conversation context",
            "built-in",
        ),
        ("During", "/tools", "list the mounted tools", "built-in"),
        (
            "During",
            "/agents",
            "list the delegatable agents",
            "built-in",
        ),
        ("During", "/skills", "list available skills", "skill"),
        (
            "During",
            "/skill",
            "load a skill by name: /skill <name>",
            "skill",
        ),
        (
            "During",
            "/mcp",
            "MCP servers: list · add · remove",
            "built-in",
        ),
        (
            "During",
            "/bundle",
            "deferred overlays; /bundle load <name> composes one now",
            "built-in",
        ),
        (
            "Parallel",
            "/tasks",
            "agent lanes: one line per subagent",
            "built-in",
        ),
        (
            "Ship",
            "/ledger",
            "session outcome ledger: spend vs yield",
            "built-in",
        ),
        // Beyond the mockup table: transcript markdown export.
        (
            "Ship",
            "/export",
            "write transcript markdown to exports/",
            "built-in",
        ),
        // Beyond the mockup table: last-answer clipboard copy.
        (
            "Ship",
            "/copy",
            "copy last answer to clipboard (OSC 52)",
            "built-in",
        ),
        // Beyond the mockup table: working-tree diff (app-cli parity).
        (
            "Ship",
            "/diff",
            "working-tree diff; /diff staged for the cached diff",
            "built-in",
        ),
        // Beyond the mockup table: app/core/bundle/session identity block.
        (
            "Ship",
            "/about",
            "app, core, bundle + session identity",
            "built-in",
        ),
        (
            "Between",
            "/rewind",
            "fork from any turn-rule checkpoint",
            "built-in",
        ),
        // Stored-session lifecycle (amplifier-app-cli parity).
        (
            "Between",
            "/rename",
            "name this session for the resume picker",
            "built-in",
        ),
        (
            "Between",
            "/sessions",
            "list stored sessions for this project",
            "built-in",
        ),
        (
            "Between",
            "/branch",
            "snapshot this conversation into a new session",
            "built-in",
        ),
        (
            "Between",
            "/fork",
            "snapshot into a new session primed to run a directive",
            "built-in",
        ),
        // Beyond the mockup table: exit path (amplifier-app-cli parity).
        (
            "Between",
            "/quit",
            "exit the app (ctrl-d works too)",
            "built-in",
        ),
        (
            "Repair",
            "/permissions",
            "edit trust slots: boundary, blocks, exceptions",
            "built-in",
        ),
        (
            "Repair",
            "/allowed-dirs",
            "list or edit session allowed write directories",
            "built-in",
        ),
        (
            "Repair",
            "/denied-dirs",
            "list or edit session denied write directories",
            "built-in",
        ),
        (
            "Repair",
            "/doctor",
            "setup checkup; reports, then fixes on confirm",
            "skill",
        ),
        (
            "Repair",
            "/improve",
            "tune config from ledger + denial log",
            "skill",
        ),
        // Beyond the mockup table: runtime theme switch (DESIGN-SPEC §1).
        (
            "Repair",
            "/theme",
            "switch theme: slate, graphite, carbon",
            "built-in",
        ),
    ];

    /// Pins `test_table_matches_mockup_exactly`.
    #[test]
    fn test_table_matches_mockup_exactly() {
        let table = builtin_commands();
        let actual: Vec<(&str, &str, &str, &str)> = table
            .iter()
            .map(|s| {
                (
                    s.group.as_str(),
                    s.name.as_str(),
                    s.desc.as_str(),
                    s.tag.as_str(),
                )
            })
            .collect();
        assert_eq!(actual, MOCKUP_TABLE.to_vec());
    }

    /// Pins `test_registry_holds_all_commands`.
    #[test]
    fn test_registry_holds_all_commands() {
        let registry = build_registry();
        assert_eq!(registry.specs().len(), 35);
        let grouped = registry.grouped_rows("/");
        assert_eq!(
            grouped
                .iter()
                .map(|(group, _)| group.as_str())
                .collect::<Vec<_>>(),
            vec!["During", "Parallel", "Ship", "Between", "Repair"]
        );
    }

    /// Pins `test_theme_command_dispatches_set_theme`.
    #[test]
    fn test_theme_command_dispatches_set_theme() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/theme", &ctx, "").unwrap();
        assert_eq!(ctx.calls(), vec!["set_theme:"]); // empty arg cycles
        registry.run("/theme", &ctx, "Graphite").unwrap();
        assert_eq!(ctx.calls().last().unwrap(), "set_theme:graphite");
    }

    /// Pins `test_mode_cycles_without_args_and_jumps_with_mode_arg`.
    #[test]
    fn test_mode_cycles_without_args_and_jumps_with_mode_arg() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/mode", &ctx, "").unwrap();
        assert_eq!(ctx.calls(), vec!["cycle_mode"]);
        registry.run("/mode", &ctx, "plan").unwrap();
        assert_eq!(ctx.calls(), vec!["cycle_mode", "set_mode:plan"]);
        // Non-posture args route to the NATIVE bundle-composed mode system
        // (superpowers, careful, audit, …) — never an app-local list.
        registry.run("/mode", &ctx, "debug").unwrap();
        assert_eq!(ctx.calls().last().unwrap(), "set_native_mode:debug"); // ADD to the active set
        registry.run("/mode", &ctx, "off").unwrap();
        assert_eq!(ctx.calls().last().unwrap(), "set_native_mode:None"); // clear ALL native modes
    }

    /// Pins `test_mode_removes_a_single_native_mode`.
    #[test]
    fn test_mode_removes_a_single_native_mode() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        // /mode -<name> removes one native mode from the set (promotes the next).
        registry.run("/mode", &ctx, "-team-pulse").unwrap();
        assert_eq!(ctx.calls().last().unwrap(), "remove_native_mode:team-pulse");
        // /mode off <name> is the same remove-one operation, spelled out.
        registry.run("/mode", &ctx, "off audit").unwrap();
        assert_eq!(ctx.calls().last().unwrap(), "remove_native_mode:audit");
    }

    /// Pins `test_modes_lists_native_catalog`.
    #[test]
    fn test_modes_lists_native_catalog() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/modes", &ctx, "").unwrap();
        assert_eq!(ctx.calls(), vec!["show_modes"]);
    }

    /// Pins `test_plan_and_brainstorm_jump_modes`.
    #[test]
    fn test_plan_and_brainstorm_jump_modes() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/plan", &ctx, "").unwrap();
        registry.run("/brainstorm", &ctx, "").unwrap();
        assert_eq!(ctx.calls(), vec!["set_mode:plan", "set_mode:brainstorm"]);
    }

    /// Pins `test_context_posts_context_block`.
    #[test]
    fn test_context_posts_context_block() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/context", &ctx, "").unwrap();
        assert_eq!(ctx.user_lines(), vec!["/context"]);
        let blocks = ctx.blocks();
        assert_eq!(blocks.len(), 1);
        let TranscriptBlock::Context(block) = &blocks[0] else {
            panic!("expected a ContextBlock, got {:?}", blocks[0]);
        };
        assert_eq!(block.used_pct, 39); // 78k of 200k
        assert_eq!(block.window_label, "200k");
        let labels: Vec<&str> = block
            .segments
            .iter()
            .map(|(label, _)| label.as_str())
            .collect();
        assert_eq!(
            labels,
            vec!["conversation 52k", "tools 18k", "memory 8k", "free 122k"]
        );
    }

    /// Pins `test_tasks_rewind_permissions_dispatch_actions`.
    #[test]
    fn test_tasks_rewind_permissions_dispatch_actions() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/tasks", &ctx, "").unwrap();
        registry.run("/rewind", &ctx, "").unwrap();
        registry.run("/permissions", &ctx, "").unwrap();
        assert_eq!(
            ctx.calls(),
            vec!["toggle_lanes", "open_rewind", "open_permissions"]
        );
    }

    /// Pins `test_directory_commands_dispatch_session_management`.
    #[test]
    fn test_directory_commands_dispatch_session_management() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/allowed-dirs", &ctx, "add ../shared").unwrap();
        registry.run("/denied-dirs", &ctx, "remove .env").unwrap();
        assert_eq!(
            ctx.calls(),
            vec![
                "manage_directories:allowed:add ../shared",
                "manage_directories:denied:remove .env",
            ]
        );
    }

    /// Pins `test_in_session_ops_dispatch_through_context`.
    #[test]
    fn test_in_session_ops_dispatch_through_context() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/status", &ctx, "").unwrap();
        registry.run("/model", &ctx, "").unwrap();
        registry.run("/model", &ctx, "claude-opus-4").unwrap();
        registry.run("/effort", &ctx, "").unwrap();
        registry.run("/effort", &ctx, "high").unwrap();
        registry.run("/compact", &ctx, "keep the API design").unwrap();
        registry.run("/clear", &ctx, "").unwrap();
        registry.run("/tools", &ctx, "").unwrap();
        registry.run("/agents", &ctx, "").unwrap();
        registry.run("/diff", &ctx, "").unwrap();
        registry.run("/diff", &ctx, "staged").unwrap();
        assert_eq!(
            ctx.calls(),
            vec![
                "show_status",
                "show_model:",
                "show_model:claude-opus-4",
                "apply_effort:",
                "apply_effort:high",
                "compact_context:keep the API design",
                "clear_context",
                "show_tools",
                "show_agents",
                "show_diff:",
                "show_diff:staged",
            ]
        );
    }

    /// Pins `test_config_dispatches_through_context`.
    #[test]
    fn test_config_dispatches_through_context() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/config", &ctx, "").unwrap();
        registry.run("/config", &ctx, "show").unwrap();
        registry.run("/config", &ctx, "tools disable bash").unwrap();
        registry.run("/config", &ctx, "save --scope project").unwrap();
        assert_eq!(
            ctx.calls(),
            vec![
                "manage_config:",
                "manage_config:show",
                "manage_config:tools disable bash",
                "manage_config:save --scope project",
            ]
        );
        assert_eq!(ctx.user_lines()[0], "/config");
    }

    /// Pins `test_session_lifecycle_dispatch_through_context`.
    #[test]
    fn test_session_lifecycle_dispatch_through_context() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/rename", &ctx, "auth refactor").unwrap();
        registry.run("/sessions", &ctx, "").unwrap();
        registry.run("/branch", &ctx, "").unwrap();
        registry.run("/branch", &ctx, "spike").unwrap();
        registry.run("/fork", &ctx, "continue the refactor").unwrap();
        assert_eq!(
            ctx.calls(),
            vec![
                "rename_session:auth refactor",
                "show_sessions",
                "branch_session:",
                "branch_session:spike",
                "fork_session:continue the refactor",
            ]
        );
    }

    /// Pins `test_skills_and_mcp_dispatch_through_context`.
    #[test]
    fn test_skills_and_mcp_dispatch_through_context() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/skills", &ctx, "").unwrap();
        registry.run("/skill", &ctx, "design-patterns").unwrap();
        registry.run("/mcp", &ctx, "").unwrap();
        registry.run("/mcp", &ctx, "add postgres npx -y server").unwrap();
        registry.run("/mcp", &ctx, "remove postgres").unwrap();
        assert_eq!(
            ctx.calls(),
            vec![
                "show_skills",
                "load_skill:design-patterns",
                "manage_mcp:",
                "manage_mcp:add postgres npx -y server",
                "manage_mcp:remove postgres",
            ]
        );
    }

    /// Pins `test_ledger_posts_ledger_block_with_aggregates`.
    #[test]
    fn test_ledger_posts_ledger_block_with_aggregates() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        // /ledger prints the session cost (mockup ``this.cost`` — the footer $),
        // which includes any pre-session baseline, not the recorded-turn sum.
        *ctx.session_cost.lock().unwrap() = dec("0.76");
        {
            let mut ledger = ctx.ledger.lock().unwrap();
            ledger.record_turn(
                TurnTelemetry {
                    secs: 12.0,
                    tokens_down: 3_200,
                    cached_pct: Some(80),
                    cost: dec("0.31"),
                    estimated: false,
                },
                TurnOutcome {
                    kind: OutcomeKind::Shipped,
                    files_changed: 3,
                    diffstat: "+142/−38".to_string(),
                    tests_ok: Some(true),
                },
                1,
                4,
                "ship it",
                None,
            );
            ledger.record_turn(
                TurnTelemetry {
                    secs: 5.0,
                    tokens_down: 800,
                    cached_pct: Some(40),
                    cost: dec("0.05"),
                    estimated: false,
                },
                TurnOutcome::new(OutcomeKind::Answer),
                2,
                8,
                "",
                None,
            );
        }
        registry.run("/ledger", &ctx, "").unwrap();
        let blocks = ctx.blocks();
        assert_eq!(blocks.len(), 1);
        let TranscriptBlock::Ledger(block) = &blocks[0] else {
            panic!("expected a LedgerBlock, got {:?}", blocks[0]);
        };
        assert_eq!(block.session, "a1b2c3");
        assert_eq!(block.bundle, "dev-bundle");
        assert_eq!(block.turns, 2);
        assert_eq!(block.spend, dec("0.76"));
        assert_eq!(block.shipped, 1);
        assert_eq!(block.answer_only, 1);
        assert_eq!(block.cache_hit_pct, 72); // token-weighted
    }

    /// Pins `test_export_writes_via_context_and_notices_the_path`.
    #[test]
    fn test_export_writes_via_context_and_notices_the_path() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/export", &ctx, "").unwrap();
        assert_eq!(ctx.user_lines(), vec!["/export"]);
        assert_eq!(ctx.calls(), vec!["export_transcript"]);
        // The handler surfaces the path the context impl returns.
        assert_eq!(
            ctx.notices(),
            vec!["transcript exported · exports/a1b2c3-20260101-000000.md"]
        );
    }

    /// Pins `test_copy_copies_via_context_and_notices_char_count`.
    #[test]
    fn test_copy_copies_via_context_and_notices_char_count() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/copy", &ctx, "").unwrap();
        assert_eq!(ctx.user_lines(), vec!["/copy"]);
        assert_eq!(ctx.calls(), vec!["copy_answer"]);
        // The handler surfaces the char count the context impl returns.
        assert_eq!(
            ctx.notices(),
            vec!["copied · 42 chars · empty clipboard? allow terminal clipboard access"]
        );
    }

    /// Pins `test_about_posts_session_banner_block`.
    #[test]
    fn test_about_posts_session_banner_block() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        registry.run("/about", &ctx, "").unwrap();
        assert_eq!(ctx.user_lines(), vec!["/about"]);
        assert_eq!(ctx.calls(), vec!["about_info"]);
        // The handler posts the same identity data the session banner shows.
        let blocks = ctx.blocks();
        assert_eq!(blocks.len(), 1);
        let TranscriptBlock::SessionBanner(block) = &blocks[0] else {
            panic!("expected a SessionBanner, got {:?}", blocks[0]);
        };
        assert_eq!(block.headline, "Amplifier 0.1.0 · core 1.2.3");
        assert_eq!(block.detail, "Bundle: dev-bundle | session a1b2c3");
        assert_eq!(ctx.notices(), Vec::<String>::new());
    }

    /// Pins `test_copy_with_no_answer_notices_nothing_to_copy`.
    #[test]
    fn test_copy_with_no_answer_notices_nothing_to_copy() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        *ctx.answer_chars.lock().unwrap() = 0;
        registry.run("/copy", &ctx, "").unwrap();
        assert_eq!(ctx.calls(), vec!["copy_answer"]);
        assert_eq!(ctx.notices(), vec!["no answer to copy yet"]);
    }

    /// Pins `test_doctor_posts_doctor_block_with_findings`.
    #[test]
    fn test_doctor_posts_doctor_block_with_findings() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        *ctx.mcp_stats.lock().unwrap() = vec![
            McpServerStats {
                name: "alpha".to_string(),
                last_used_days_ago: Some(45.0),
                tokens_per_session: 2_100,
            },
            McpServerStats {
                name: "beta".to_string(),
                last_used_days_ago: None,
                tokens_per_session: 2_000,
            },
        ];
        *ctx.tallies.lock().unwrap() = vec![ApprovalTally {
            action: "read docs/".to_string(),
            approved: 14,
            asked: 14,
            capability: "read".to_string(),
        }];
        registry.run("/doctor", &ctx, "").unwrap();
        let blocks = ctx.blocks();
        assert_eq!(blocks.len(), 1);
        let TranscriptBlock::Doctor(block) = &blocks[0] else {
            panic!("expected a DoctorBlock, got {:?}", blocks[0]);
        };
        let texts: Vec<&str> = block
            .findings
            .iter()
            .map(|finding| finding.text.as_str())
            .collect();
        assert!(
            texts.contains(&"2 MCP servers unused in 30 days · cost 4.1k tok/session"),
            "missing mcp finding in {texts:?}"
        );
        assert!(
            texts.contains(&"14 identical read-only approvals this week · candidate allowlist"),
            "missing approvals finding in {texts:?}"
        );
    }

    /// Pins `test_improve_posts_proposals_and_never_mutates`.
    #[test]
    fn test_improve_posts_proposals_and_never_mutates() {
        let registry = build_registry();
        let ctx = FakeCommandContext::new();
        *ctx.tallies.lock().unwrap() = vec![ApprovalTally {
            action: "uv run pytest".to_string(),
            approved: 22,
            asked: 22,
            capability: "test".to_string(),
        }];
        *ctx.overrides.lock().unwrap() = vec![OverriddenDenial {
            action: "push-to-fork".to_string(),
            denied: 3,
            overridden: 3,
        }];
        registry.run("/improve", &ctx, "").unwrap();
        let blocks = ctx.blocks();
        assert_eq!(blocks.len(), 1);
        let TranscriptBlock::Improve(block) = &blocks[0] else {
            panic!("expected an ImproveBlock, got {:?}", blocks[0]);
        };
        // Mockup rows: dim title prefix + the action named once in green.
        assert_eq!(
            block
                .proposals
                .iter()
                .map(|p| (p.title.as_str(), p.action.as_str()))
                .collect::<Vec<_>>(),
            vec![("allowlist:", "uv run pytest"), ("trust slot:", "")]
        );
        assert_eq!(
            block.proposals[0].rationale,
            "approved 22/22 times · add to auto"
        );
        // Proposals only — nothing was applied to any surface.
        assert_eq!(ctx.calls(), Vec::<String>::new());
        assert_eq!(ctx.notices(), Vec::<String>::new());
    }

    /// Pins `test_key_actions_exist_in_keymap` — registry key_action ids
    /// must be real keymap actions (single source).
    #[test]
    fn test_key_actions_exist_in_keymap() {
        use crate::ui::keymap::KEYMAP;

        let keymap_actions: HashSet<&str> = KEYMAP.iter().map(|binding| binding.action).collect();
        let registry = build_registry();
        let keybound: HashSet<String> = registry.keybound().into_keys().collect();
        assert!(keybound
            .iter()
            .all(|action| keymap_actions.contains(action.as_str())));
        assert_eq!(
            keybound,
            HashSet::from([
                "cycle_mode".to_string(),
                "toggle_lanes".to_string(),
                "show_ledger".to_string(),
                "open_rewind".to_string(),
            ])
        );
    }
}
