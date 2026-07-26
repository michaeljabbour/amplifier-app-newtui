//! Segment renderers for the `/config` command output.
//!
//! `/config show` / `<category>` / `<name>` / `diff` / help each post an
//! `Answer` block to the transcript; these pure functions turn the frozen
//! [`ConfigSnapshotView`] into the flat [`Segment`] stream that block
//! carries, matching the house style of the session-ops views (blue `·`
//! marker, bright-bold header, dim/teal detail). Pure and widget-free so
//! they unit-test as span tuples.
//!
//! Port of `src/amplifier_app_newtui/ui/config_view.py`.

use crate::model::blocks::{Segment, StyleToken};
use crate::model::config::{ConfigChange, ConfigItem, ConfigSnapshotView, CONFIG_CATEGORIES};

fn seg(text: impl Into<String>, token: StyleToken) -> Segment {
    Segment {
        style_token: token,
        ..Segment::new(text)
    }
}

fn seg_bold(text: impl Into<String>, token: StyleToken, bold: bool) -> Segment {
    Segment {
        style_token: token,
        bold,
        ..Segment::new(text)
    }
}

/// Python `str.ljust` (pads to a code-point count with spaces).
fn ljust(text: &str, width: usize) -> String {
    let len = text.chars().count();
    if len >= width {
        text.to_string()
    } else {
        let mut out = String::with_capacity(text.len() + width - len);
        out.push_str(text);
        out.extend(std::iter::repeat_n(' ', width - len));
        out
    }
}

fn header(label: &str, detail: &str) -> Vec<Segment> {
    vec![
        seg("\u{b7} ", StyleToken::Blue),
        seg_bold(label, StyleToken::Bright, true),
        seg(format!("  {detail}\n"), StyleToken::Dim),
    ]
}

fn item_line(item: &ConfigItem) -> Vec<Segment> {
    let glyph = if item.enabled { "\u{25cf} " } else { "\u{25cb} " };
    let glyph_token = if item.enabled {
        StyleToken::Green
    } else {
        StyleToken::Dimmer
    };
    let name_token = if item.enabled {
        StyleToken::Teal
    } else {
        StyleToken::Dimmer
    };
    let mut spans = vec![
        seg(format!("    {glyph}"), glyph_token),
        seg_bold(item.name.clone(), name_token, item.enabled),
    ];
    if !item.detail.is_empty() {
        spans.push(seg(format!("  {}", item.detail), StyleToken::Dim));
    }
    if item.read_only() {
        spans.push(seg("  (read-only)", StyleToken::Dimmer));
    }
    spans.push(seg("\n", StyleToken::Dim));
    spans
}

fn category_block(view: &ConfigSnapshotView, category: &str) -> Vec<Segment> {
    let items = view.items_in(category);
    if items.is_empty() {
        return Vec::new();
    }
    let enabled = items.iter().filter(|item| item.enabled).count();
    let mut spans = vec![
        seg_bold(format!("  {category}"), StyleToken::Bright, true),
        seg(
            format!("  {enabled}/{} on\n", items.len()),
            StyleToken::Dim,
        ),
    ];
    for item in &items {
        spans.extend(item_line(item));
    }
    spans
}

fn overrides_block(view: &ConfigSnapshotView) -> Vec<Segment> {
    if view.overrides.is_empty() {
        return Vec::new();
    }
    let mut spans = vec![seg_bold("  set values\n", StyleToken::Bright, true)];
    let width = view
        .overrides
        .iter()
        .map(|(path, _)| path.chars().count())
        .max()
        .unwrap_or(0);
    for (path, value) in &view.overrides {
        spans.push(seg(format!("    {}  ", ljust(path, width)), StyleToken::Teal));
        spans.push(seg(format!("{value}\n"), StyleToken::Dim));
    }
    spans
}

/// `/config show` (all categories) or `/config <category>` (one).
pub fn config_show_spans(view: &ConfigSnapshotView, category: Option<&str>) -> Vec<Segment> {
    let change_count = view.changes.len();
    let changes = if change_count > 0 {
        format!("{change_count} change(s)")
    } else {
        "no changes".to_string()
    };
    let bundle = if view.bundle.is_empty() {
        "session"
    } else {
        view.bundle.as_str()
    };
    let detail = format!("{bundle} \u{b7} {changes} \u{b7} /config set|diff|save");
    let mut spans = header("Config", &detail);
    let categories: Vec<&str> = match category {
        Some(category) => vec![category],
        None => CONFIG_CATEGORIES.iter().map(|c| c.as_str()).collect(),
    };
    let mut rendered_any = false;
    for cat in categories {
        let block = category_block(view, cat);
        if !block.is_empty() {
            rendered_any = true;
            spans.extend(block);
        }
    }
    if category.is_none() {
        spans.extend(overrides_block(view));
    }
    if !rendered_any {
        // Python interpolates `category` directly, so `None` renders as the
        // literal string "None" when a fully empty view is shown unfiltered.
        spans.push(seg(
            format!("    no {} configured\n", category.unwrap_or("None")),
            StyleToken::Dimmer,
        ));
    }
    spans
}

/// `/config <category> <name>` — one item's detail (or a not-found line).
pub fn config_item_spans(item: Option<&ConfigItem>, category: &str, name: &str) -> Vec<Segment> {
    let Some(item) = item else {
        return vec![seg(
            format!("  no {category} item named '{name}'\n"),
            StyleToken::Dimmer,
        )];
    };
    let mut spans = header("Config", &format!("{category} \u{b7} {name}"));
    spans.extend(item_line(item));
    if !item.read_only() {
        let verb = if item.enabled { "disable" } else { "enable" };
        spans.push(seg(
            format!("    /config {category} {verb} {name}\n"),
            StyleToken::Dimmer,
        ));
    }
    spans
}

/// `/config diff` — what changed since session start (donor parity).
pub fn config_diff_spans(changes: &[ConfigChange]) -> Vec<Segment> {
    if changes.is_empty() {
        return vec![seg(
            "  no changes from session start \u{b7} config matches the bundle\n",
            StyleToken::Dim,
        )];
    }
    let mut spans = header("Config diff", &format!("{} change(s) since start", changes.len()));
    let width = changes
        .iter()
        .map(|c| format!("{} {}", c.category, c.name).chars().count())
        .max()
        .unwrap_or(0);
    for change in changes {
        let label = format!("{} {}", change.category, change.name);
        spans.push(seg(format!("    {}  ", ljust(&label, width)), StyleToken::Teal));
        spans.push(seg(format!("{}\n", change.action), StyleToken::Dim));
    }
    spans
}

/// `/config` (no args) — a concise subcommand listing (donor parity).
pub fn config_help_spans() -> Vec<Segment> {
    let rows: [(&str, &str); 7] = [
        ("show", "live config tree across all categories"),
        ("<category>", "list one category (context/tools/hooks/providers/agents)"),
        ("<category> <name>", "detail for one item"),
        ("<category> disable|enable <n>", "toggle an item (hooks are read-only)"),
        ("set <path> <value>", "set a config value (session scope)"),
        ("diff", "changes since session start"),
        ("save [--scope global|project|local]", "persist to settings.yaml"),
    ];
    let mut spans = header("Config", "live session configuration");
    let width = rows
        .iter()
        .map(|(cmd, _)| cmd.chars().count())
        .max()
        .unwrap_or(0);
    for (cmd, desc) in rows {
        spans.push(seg(format!("  /config {}  ", ljust(cmd, width)), StyleToken::Teal));
        spans.push(seg(format!("{desc}\n"), StyleToken::Dim));
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::config::default_config_state;

    fn text(spans: &[Segment]) -> String {
        spans.iter().map(|s| s.text.as_str()).collect()
    }

    #[test]
    fn test_show_lists_every_category_with_counts() {
        let view = ConfigSnapshotView::of(&default_config_state("anchors"));
        let text = text(&config_show_spans(&view, None));
        assert!(text.contains("Config"));
        for category in ["context", "tools", "hooks", "providers", "agents"] {
            assert!(text.contains(category));
        }
        assert!(text.contains("read_file"));
        // Read-only hooks are labelled so the user knows why they can't toggle.
        assert!(text.contains("(read-only)"));
    }

    #[test]
    fn test_show_reflects_a_disable_and_change_count() {
        let mut state = default_config_state("anchors");
        state.toggle("tools", "bash", false);
        let view = ConfigSnapshotView::of(&state);
        let text = text(&config_show_spans(&view, None));
        assert!(text.contains("1 change(s)"));
        // The disabled item uses the hollow glyph, the enabled ones the filled one.
        assert!(text.contains("\u{25cb} ") && text.contains("\u{25cf} "));
    }

    #[test]
    fn test_show_single_category_filters() {
        let view = ConfigSnapshotView::of(&default_config_state("anchors"));
        let text = text(&config_show_spans(&view, Some("providers")));
        assert!(text.contains("providers") && text.contains("anthropic"));
        assert!(!text.contains("read_file")); // tools section not rendered
    }

    #[test]
    fn test_show_overrides_section() {
        let mut state = default_config_state("anchors");
        state.set_value("session.reasoning_effort", "high");
        let view = ConfigSnapshotView::of(&state);
        let text = text(&config_show_spans(&view, None));
        assert!(text.contains("set values"));
        assert!(text.contains("session.reasoning_effort"));
    }

    #[test]
    fn test_item_spans_found_and_missing() {
        let item = ConfigItem::new("tools", "bash", true, "tool-shell");
        let found = text(&config_item_spans(Some(&item), "tools", "bash"));
        assert!(found.contains("bash") && found.contains("/config tools disable bash"));
        let missing = text(&config_item_spans(None, "tools", "ghost"));
        assert!(missing.contains("no tools item named 'ghost'"));
    }

    #[test]
    fn test_diff_spans_empty_and_populated() {
        assert!(text(&config_diff_spans(&[])).contains("no changes from session start"));
        let changes = [
            ConfigChange {
                category: "tools".to_string(),
                name: "bash".to_string(),
                action: "disabled".to_string(),
            },
            ConfigChange {
                category: "set".to_string(),
                name: "x".to_string(),
                action: "= 1".to_string(),
            },
        ];
        let text = text(&config_diff_spans(&changes));
        assert!(text.contains("2 change(s)") && text.contains("tools bash") && text.contains("disabled"));
    }

    #[test]
    fn test_help_spans_lists_subcommands() {
        let text = text(&config_help_spans());
        for token in ["show", "diff", "save", "set", "disable"] {
            assert!(text.contains(token));
        }
    }
}
