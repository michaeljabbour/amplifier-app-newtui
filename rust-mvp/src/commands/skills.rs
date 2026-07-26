//! Skill aliases — discovered skills as first-class palette commands.
//!
//! Port of `src/amplifier_app_newtui/commands/skills.py`.
//!
//! Brian's story #1: `/cranky-old-sam` (and its `shortcut:` alias
//! `/cosam`) must resolve exactly like any built-in before slash input
//! can fall through as a chat turn. Rather than a second lookup table,
//! each discovered skill registers additively into the ONE command
//! registry (ADR-0007: commands are data + callables) — so the palette,
//! help listing, `parse_and_run` dispatch and the unknown-command check
//! all see skills for free.
//!
//! Layering: skills arrive duck-typed (`name` / `description` /
//! `shortcut` accessors, i.e. the `kernel.session_ops.SkillInfo` shape —
//! the [`SkillLike`] trait here, Python's `Protocol`) — this module still
//! imports nothing above `model/`. Handlers invoke the skill through
//! [`CommandContext::load_skill`], the same path the built-in
//! `/skill <name>` takes.

use std::collections::HashSet;
use std::sync::Arc;

use super::registry::{
    CommandContext, CommandGroup, CommandHandler, CommandRegistry, CommandSpec,
};

/// Registration source label for skill-contributed commands.
const SKILL_SOURCE: &str = "skill";

/// What a discovered skill must offer (`session_ops.SkillInfo` shape).
///
/// Python is a `Protocol` with `name` / `description` / `shortcut`
/// string properties; absent values surface as empty strings (the Python
/// call sites coerce with `str(value or "")`).
pub trait SkillLike {
    fn name(&self) -> String;
    fn description(&self) -> String;
    fn shortcut(&self) -> String;
}

fn load_handler(skill_name: &str) -> CommandHandler {
    let skill_name = skill_name.to_string();
    // Alias arguments are not plumbed through load_skill (yet).
    Arc::new(move |ctx: &dyn CommandContext, _args: &str| ctx.load_skill(&skill_name))
}

/// A `skill`-tagged spec for *trigger*, or `None` when the token is not
/// a valid slash trigger (spaces, empty — validator decides).
fn spec_for(trigger: &str, desc: &str, skill_name: &str) -> Option<CommandSpec> {
    CommandSpec::new(
        CommandGroup::During,
        &format!("/{trigger}"),
        desc,
        "skill",
        load_handler(skill_name),
    )
    .ok()
}

/// Palette rows for *skills* that don't collide with *registry*.
///
/// One row per skill name plus one per distinct `shortcut` alias
/// (the alias row names its target so the palette reads as an alias).
/// Collisions with already-registered commands — built-ins or earlier
/// skills — are skipped: first registration wins, never overridden.
pub fn skill_command_specs<S: SkillLike>(
    registry: &CommandRegistry,
    skills: &[S],
) -> Vec<CommandSpec> {
    let mut specs: Vec<CommandSpec> = Vec::new();
    let mut taken: HashSet<String> = registry.names().into_iter().collect();
    for skill in skills {
        let name = skill.name().trim().to_string();
        let joined = skill
            .description()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        let desc = if joined.is_empty() {
            format!("load skill {name}")
        } else {
            joined
        };
        let Some(spec) = spec_for(&name, &desc, &name) else {
            continue;
        };
        if taken.contains(&spec.name) {
            continue;
        }
        taken.insert(spec.name.clone());
        specs.push(spec);
        let shortcut = skill.shortcut().trim().to_string();
        if !shortcut.is_empty() && shortcut != name {
            if let Some(alias) = spec_for(&shortcut, &format!("{name} · {desc}"), &name) {
                if !taken.contains(&alias.name) {
                    taken.insert(alias.name.clone());
                    specs.push(alias);
                }
            }
        }
    }
    specs
}

/// Register *skills* (names + shortcuts) into *registry*; returns the
/// specs actually added — empty when everything was already present.
///
/// Rides the open-registry mechanism (story #2): each row registers as
/// a `skill`-sourced contribution, so `registry.contributions("skill")`
/// lists them and the registry's own collision policy (existing command
/// wins, skip with a log line) backstops the prefilter above.
pub fn register_skill_commands<S: SkillLike>(
    registry: &CommandRegistry,
    skills: &[S],
) -> Vec<CommandSpec> {
    skill_command_specs(registry, skills)
        .into_iter()
        .filter(|spec| {
            registry
                .register_with_source(spec.clone(), SKILL_SOURCE)
                .expect("non-builtin registrations never error")
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Tests — port of tests/test_commands_skills.py (all cases). The fake
// context mirrors tests/conftest.py's FakeCommandContext; `build_registry`
// stands in for commands/builtin.py's seed (not yet ported) with just the
// built-ins these tests collide with (/mode, /status, /skill), using the
// real builtin descriptions and handler behaviors.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use std::any::Any;
    use std::sync::Mutex;

    use rust_decimal::Decimal;

    use super::*;
    use crate::model::blocks::{BlockIdAllocator, TranscriptBlock};
    use crate::model::queues::{NeedsYouQueue, SteeringQueue};
    use crate::model::trust::DenialLog;
    use crate::model::turn::OutcomeLedger;

    /// Python `_skill(name, description="", shortcut="")` (SimpleNamespace).
    struct TestSkill {
        name: String,
        description: String,
        shortcut: String,
    }

    impl SkillLike for TestSkill {
        fn name(&self) -> String {
            self.name.clone()
        }

        fn description(&self) -> String {
            self.description.clone()
        }

        fn shortcut(&self) -> String {
            self.shortcut.clone()
        }
    }

    fn skill(name: &str, description: &str, shortcut: &str) -> TestSkill {
        TestSkill {
            name: name.to_string(),
            description: description.to_string(),
            shortcut: shortcut.to_string(),
        }
    }

    /// Minimal stand-in for `commands.builtin.build_registry()` — the
    /// builtin module is a later unit; these are the three seeded rows the
    /// pinned tests touch, with their real descriptions and handlers.
    fn build_registry() -> CommandRegistry {
        let mode: CommandHandler = Arc::new(|ctx: &dyn CommandContext, args: &str| {
            if args.trim().is_empty() {
                ctx.cycle_mode();
            } else {
                ctx.set_mode(args.trim());
            }
        });
        let status: CommandHandler =
            Arc::new(|ctx: &dyn CommandContext, _args: &str| ctx.show_status());
        let skill: CommandHandler =
            Arc::new(|ctx: &dyn CommandContext, args: &str| ctx.load_skill(args.trim()));
        CommandRegistry::with_specs([
            CommandSpec::new(
                CommandGroup::During,
                "/mode",
                "cycle or jump posture: chat, plan, brainstorm, build, auto",
                "built-in",
                mode,
            )
            .unwrap()
            .with_key_action("cycle_mode"),
            CommandSpec::new(
                CommandGroup::During,
                "/status",
                "session status: model, mode, messages, cost",
                "built-in",
                status,
            )
            .unwrap(),
            CommandSpec::new(
                CommandGroup::During,
                "/skill",
                "load a skill by name: /skill <name>",
                "skill",
                skill,
            )
            .unwrap(),
        ])
        .expect("seed registry builds")
    }

    /// Port of `tests/conftest.py::FakeCommandContext` — records every
    /// action a command handler takes.
    struct FakeCommandContext {
        ledger: Mutex<OutcomeLedger>,
        denial_log: Mutex<DenialLog>,
        steering: SteeringQueue,
        needs_you: NeedsYouQueue,
        ids: Mutex<BlockIdAllocator>,
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
            Decimal::ZERO
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
            Box::new(())
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
            42
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

    fn added_names(added: &[CommandSpec]) -> Vec<&str> {
        added.iter().map(|spec| spec.name.as_str()).collect()
    }

    #[test]
    fn test_registers_skill_and_shortcut_rows() {
        let registry = build_registry();
        let added = register_skill_commands(
            &registry,
            &[skill("cranky-old-sam", "crusty review", "cosam")],
        );
        assert_eq!(added_names(&added), vec!["/cranky-old-sam", "/cosam"]);
        let spec = registry.get("/cranky-old-sam").expect("registered");
        assert_eq!(spec.tag, "skill");
        assert!(spec.desc.contains("crusty review"));
        let alias = registry.get("/cosam").expect("registered");
        assert_eq!(alias.tag, "skill");
        // Alias row names its target.
        assert!(alias.desc.contains("cranky-old-sam"));
    }

    #[test]
    fn test_parse_and_run_resolves_name_and_shortcut() {
        let fake = FakeCommandContext::new();
        let registry = build_registry();
        register_skill_commands(
            &registry,
            &[skill("cranky-old-sam", "crusty review", "cosam")],
        );
        assert!(registry.parse_and_run(&fake, "/cranky-old-sam"));
        assert!(registry.parse_and_run(&fake, "/cosam"));
        // Both routes invoke the skill exactly like `/skill <name>` does.
        assert_eq!(
            fake.calls(),
            vec!["load_skill:cranky-old-sam", "load_skill:cranky-old-sam"]
        );
        assert_eq!(fake.user_lines(), vec!["/cranky-old-sam", "/cosam"]);
    }

    #[test]
    fn test_skips_collisions_with_existing_commands() {
        let fake = FakeCommandContext::new();
        let registry = build_registry();
        let added = register_skill_commands(
            &registry,
            &[
                skill("status", "shadows a built-in", ""), // /status is built-in
                skill("review", "fine", "skill"),          // /skill is built-in
            ],
        );
        assert_eq!(added_names(&added), vec!["/review"]);
        // The built-in survives untouched.
        registry.parse_and_run(&fake, "/status");
        assert_eq!(fake.calls(), vec!["show_status"]);
    }

    #[test]
    fn test_skips_tokens_that_are_not_slash_triggers() {
        let registry = build_registry();
        let added = register_skill_commands(
            &registry,
            &[
                skill("bad name with spaces", "", ""),
                skill("", "", ""),
                skill("ok", "", ""),
            ],
        );
        assert_eq!(added_names(&added), vec!["/ok"]);
    }

    #[test]
    fn test_shortcut_equal_to_name_registers_once() {
        let registry = build_registry();
        let added = register_skill_commands(&registry, &[skill("simplify", "cut", "simplify")]);
        assert_eq!(added_names(&added), vec!["/simplify"]);
    }

    #[test]
    fn test_empty_description_gets_a_default() {
        let registry = build_registry();
        register_skill_commands(&registry, &[skill("terse", "", "")]);
        let spec = registry.get("/terse").expect("registered");
        assert!(!spec.desc.trim().is_empty());
    }

    #[test]
    fn test_registering_twice_is_idempotent() {
        let registry = build_registry();
        let skills = [skill("cranky-old-sam", "crusty review", "cosam")];
        register_skill_commands(&registry, &skills);
        assert_eq!(register_skill_commands(&registry, &skills), Vec::new());
    }

    #[test]
    fn test_skill_rows_are_skill_sourced_contributions() {
        // Story #2: skills ride the open-registry mechanism — their rows are
        // 'skill'-sourced contributions, unregisterable as a group, distinct
        // from the seeded built-ins.
        let registry = build_registry();
        let added = register_skill_commands(
            &registry,
            &[skill("cranky-old-sam", "crusty review", "cosam")],
        );
        assert_eq!(registry.contributions("skill"), added);
        assert_eq!(registry.source_of("/cosam").as_deref(), Some("skill"));
        assert_eq!(registry.source_of("/mode").as_deref(), Some("builtin"));
        assert!(registry.unregister("/cosam").unwrap());
        assert!(registry.get("/cosam").is_none());
        assert!(registry.get("/cranky-old-sam").is_some());
    }
}
