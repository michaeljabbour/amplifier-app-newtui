//! In-session `/config` UI controller (show/toggle/set/diff/save).
//!
//! Port of `src/amplifier_app_newtui/ui/config_admin.py`. The composer
//! posts `/config ...` to [`manage`], which parses the argument line with
//! the pure model router
//! ([`crate::model::config::parse_config_command`]) and drives the runtime
//! adapter's config surface, posting an [`Answer`] (or a transient notice)
//! per subcommand. It mirrors [`crate::ui::directory_admin`]: a fake host
//! + adapter unit-test it with no widget toolkit and no live session.
//!
//! Round-trip (acceptance): `toggle` and `set` re-post the refreshed view
//! so the change is visible on screen immediately; `diff` reports the
//! delta from session start; `save` persists to the chosen settings scope.
//!
//! Adaptation notes:
//! - The Python `ConfigAdminHost` protocol exposes `adapter` and
//!   `allocator` attributes; here those are flattened into trait methods
//!   on [`ConfigAdminHost`] (same pattern as `ui/directory_admin.rs`).
//!   App assembly must forward `config_view` / `config_toggle` /
//!   `config_set` / `config_diff` / `config_save` to the session adapter
//!   and `next_id` to the transcript's `BlockIdAllocator`.
//! - Python `manage` is `async` only because the adapter calls are
//!   awaited; the control flow is strictly sequential, so the port is a
//!   synchronous function.

use crate::model::blocks::Answer;
use crate::model::config::{
    parse_config_command, ConfigChange, ConfigSnapshotView, InvocationKind,
};
use crate::ui::config_view::{
    config_diff_spans, config_help_spans, config_item_spans, config_show_spans,
};

/// Host surface [`manage`] drives (Python `ConfigAdminHost` protocol with
/// the `adapter`/`allocator` attributes flattened into methods).
pub trait ConfigAdminHost {
    /// `host.adapter.config_view()` — a frozen snapshot of the live state.
    fn config_view(&mut self) -> ConfigSnapshotView;
    /// `host.adapter.config_toggle(category, name, enable)` → `(ok, message)`.
    fn config_toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String);
    /// `host.adapter.config_set(path, value)` → `(ok, message)`.
    fn config_set(&mut self, path: &str, value: &str) -> (bool, String);
    /// `host.adapter.config_diff()`.
    fn config_diff(&mut self) -> Vec<ConfigChange>;
    /// `host.adapter.config_save(scope)` → `(ok, message)`.
    fn config_save(&mut self, scope: &str) -> (bool, String);
    /// `host.allocator.next_id()`.
    fn next_id(&mut self) -> String;
    /// `host.append_block(block)` — only [`Answer`] blocks flow through
    /// this controller.
    fn append_block(&mut self, block: Answer);
    /// `host.show_notice(text, duration)`.
    fn show_notice(&mut self, text: &str, duration: Option<f64>);
}

/// Python `manage(host, args)`.
pub fn manage(host: &mut dyn ConfigAdminHost, args: &str) {
    let inv = parse_config_command(args);

    match inv.kind {
        InvocationKind::Help => {
            post(host, config_help_spans());
        }
        InvocationKind::Show => {
            let view = host.config_view();
            post(host, config_show_spans(&view, None));
        }
        InvocationKind::Category => {
            let view = host.config_view();
            post(host, config_show_spans(&view, Some(&inv.category)));
        }
        InvocationKind::Item => {
            let view = host.config_view();
            let items = view.items_in(&inv.category);
            let item = items.iter().find(|i| i.name == inv.name);
            post(host, config_item_spans(item, &inv.category, &inv.name));
        }
        InvocationKind::Toggle => {
            let (ok, message) = host.config_toggle(&inv.category, &inv.name, inv.enable);
            host.show_notice(&message, None);
            if ok {
                let view = host.config_view();
                post(host, config_show_spans(&view, Some(&inv.category)));
            }
        }
        InvocationKind::Set => {
            let (ok, message) = host.config_set(&inv.path, &inv.value);
            host.show_notice(&message, None);
            if ok {
                let view = host.config_view();
                post(host, config_show_spans(&view, None));
            }
        }
        InvocationKind::Diff => {
            let changes = host.config_diff();
            post(host, config_diff_spans(&changes));
        }
        InvocationKind::Save => {
            let (_ok, message) = host.config_save(&inv.scope);
            host.show_notice(&message, None);
        }
        InvocationKind::Error => {
            host.show_notice(&inv.message, None);
        }
    }
}

/// Python `_post(host, spans)`.
fn post(host: &mut dyn ConfigAdminHost, spans: Vec<crate::model::blocks::Segment>) {
    let id = host.next_id();
    host.append_block(Answer::new(id, spans));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    use serde_json::json;

    use crate::model::config::{default_config_state, ConfigValue, SessionConfigState};

    /// Python `FakeConfigAdapter` + `FakeAllocator` + `FakeHost` collapsed
    /// into one struct (the Rust trait flattens the adapter/allocator
    /// attributes into host methods). The adapter delegates to a real
    /// `SessionConfigState`, exactly like the Python fake; `config_save`
    /// mirrors the observable contract of `kernel.config_ops.save_config`
    /// (unported kernel unit): write `{configurator: to_settings()}` as
    /// YAML to the global-scope `settings.yaml` and return the donor's
    /// success message.
    struct FakeHost {
        state: SessionConfigState,
        home: PathBuf,
        next: u64,
        blocks: Vec<Answer>,
        notices: Vec<String>,
    }

    impl FakeHost {
        fn new(tmp_path: &Path) -> Self {
            Self {
                state: default_config_state("anchors"),
                home: tmp_path.to_path_buf(),
                next: 0,
                blocks: Vec::new(),
                notices: Vec::new(),
            }
        }
    }

    impl ConfigAdminHost for FakeHost {
        fn config_view(&mut self) -> ConfigSnapshotView {
            ConfigSnapshotView::of(&self.state)
        }

        fn config_toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String) {
            self.state.toggle(category, name, enable)
        }

        fn config_set(&mut self, path: &str, value: &str) -> (bool, String) {
            self.state.set_value(path, value)
        }

        fn config_diff(&mut self) -> Vec<ConfigChange> {
            self.state.diff()
        }

        fn config_save(&mut self, scope: &str) -> (bool, String) {
            // Minimal stand-in for `kernel.config_ops.save_config` (global
            // scope path = home/settings.yaml, `configurator:` key).
            let path = self.home.join("settings.yaml");
            let merged = json!({ "configurator": self.state.to_settings() });
            std::fs::write(&path, serde_yaml::to_string(&merged).unwrap()).unwrap();
            let count = self.state.change_count();
            let detail = if count > 0 {
                format!("{count} change(s)")
            } else {
                "no session changes".to_string()
            };
            (
                true,
                format!(
                    "\u{2713} config saved \u{b7} {scope} scope \u{b7} {detail} \u{b7} {}",
                    path.display()
                ),
            )
        }

        fn next_id(&mut self) -> String {
            self.next += 1;
            format!("b{}", self.next)
        }

        fn append_block(&mut self, block: Answer) {
            self.blocks.push(block);
        }

        fn show_notice(&mut self, text: &str, _duration: Option<f64>) {
            self.notices.push(text.to_string());
        }
    }

    fn text(block: &Answer) -> String {
        block.spans.iter().map(|s| s.text.as_str()).collect()
    }

    #[test]
    fn test_help_posts_subcommand_listing() {
        let tmp = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new(tmp.path());
        manage(&mut host, "");
        assert_eq!(host.blocks.len(), 1);
        assert!(text(&host.blocks[0]).contains("save"));
    }

    #[test]
    fn test_show_posts_full_config() {
        let tmp = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new(tmp.path());
        manage(&mut host, "show");
        let text = text(&host.blocks[0]);
        assert!(text.contains("tools") && text.contains("providers"));
    }

    #[test]
    fn test_toggle_round_trips_and_reposts_category() {
        let tmp = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new(tmp.path());
        manage(&mut host, "tools disable bash");
        // Notice confirms the toggle; a refreshed tools view is re-posted.
        assert_eq!(host.notices, vec!["\u{2713} Disabled bash".to_string()]);
        assert_eq!(host.blocks.len(), 1);
        let text = text(&host.blocks[0]);
        assert!(text.contains("bash") && text.contains("\u{25cb} ")); // hollow glyph = disabled
        let item = host.state.find("tools", "bash");
        assert!(item.is_some_and(|item| !item.enabled));
    }

    #[test]
    fn test_toggle_hooks_read_only_notice_no_block() {
        let tmp = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new(tmp.path());
        manage(&mut host, "hooks disable hooks-mode");
        assert!(!host.notices.is_empty() && host.notices[0].contains("read-only"));
        assert!(host.blocks.is_empty()); // a refused toggle re-posts nothing
    }

    #[test]
    fn test_set_round_trips_and_reposts_show() {
        let tmp = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new(tmp.path());
        manage(&mut host, "set session.reasoning_effort high");
        assert_eq!(
            host.notices,
            vec!["\u{2713} Set session.reasoning_effort = 'high'".to_string()]
        );
        assert_eq!(
            host.state.value("session.reasoning_effort"),
            Some(&ConfigValue::Str("high".to_string()))
        );
        assert!(text(&host.blocks[0]).contains("set values"));
    }

    #[test]
    fn test_diff_reports_session_changes() {
        let tmp = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new(tmp.path());
        manage(&mut host, "tools disable bash");
        host.blocks.clear();
        manage(&mut host, "diff");
        let text = text(&host.blocks[0]);
        assert!(text.contains("tools bash") && text.contains("disabled"));
    }

    #[test]
    fn test_save_writes_scope_and_notices_path() {
        let tmp = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new(tmp.path());
        manage(&mut host, "tools disable bash");
        host.notices.clear();
        manage(&mut host, "save --scope global");
        assert!(!host.notices.is_empty() && host.notices[0].contains("global scope"));
        let raw = std::fs::read_to_string(tmp.path().join("settings.yaml")).unwrap();
        let written: serde_json::Value = serde_yaml::from_str(&raw).unwrap();
        assert_eq!(written["configurator"]["disabled"], json!({"tools": ["bash"]}));
    }

    #[test]
    fn test_error_invocation_only_notices() {
        let tmp = tempfile::tempdir().unwrap();
        let mut host = FakeHost::new(tmp.path());
        manage(&mut host, "frobnicate");
        assert!(host.blocks.is_empty());
        assert!(!host.notices.is_empty() && host.notices[0].contains("unknown /config subcommand"));
    }
}
