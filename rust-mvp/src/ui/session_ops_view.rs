//! Segment renderers for the in-session ops commands.
//!
//! Port of `src/amplifier_app_newtui/ui/session_ops_view.py`.
//!
//! `/model`, `/status`, `/tools`, `/agents` and `/diff` post an `Answer`
//! to the transcript; these pure functions turn the kernel result data
//! into the flat [`Segment`] stream that block carries, matching the
//! house style of `ui/app_support.native_modes_segments` (blue `·`
//! marker, bright-bold header, dim/teal detail). Pure and widget-free so
//! they unit-test as span tuples.
//!
//! Ratatui adaptation: everything here is already pure view-building, so
//! the whole unit ports. The input dataclasses live in *unported* kernel
//! units (`kernel.session_ops`, `kernel.session_manager`,
//! `kernel.compaction` — server-side coordinator/filesystem code), so
//! their frozen shapes are mirrored here as view-input structs
//! ([`ModelListing`], [`StatusInfo`], [`SkillInfo`], [`SessionSummary`],
//! [`CompactionConfig`]); app assembly maps kernel results into them.
//! Python's `dict[str, str]` of MCP servers becomes an ordered slice of
//! `(name, summary)` pairs — insertion order is render order.

use std::time::{SystemTime, UNIX_EPOCH};

use rust_decimal::Decimal;

use crate::model::blocks::{Segment, StyleToken};

use super::live_tail::answer_spans;

const DIFF_MAX_LINES: usize = 400;

/// How the effective context window counts tokens
/// (`kernel.compaction.AccountingMode = Literal["provider-observed", "estimated"]`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum AccountingMode {
    ProviderObserved,
    #[default]
    Estimated,
}

impl AccountingMode {
    /// The exact Python literal strings.
    pub fn as_str(self) -> &'static str {
        match self {
            AccountingMode::ProviderObserved => "provider-observed",
            AccountingMode::Estimated => "estimated",
        }
    }
}

/// Python `kernel.compaction.DEFAULT_CONTEXT_WINDOW = 200_000`.
pub const DEFAULT_CONTEXT_WINDOW: u64 = 200_000;

/// Effective context window and automatic-compaction posture
/// (mirror of frozen `kernel.compaction.CompactionConfig`).
#[derive(Clone, Debug, PartialEq)]
pub struct CompactionConfig {
    pub max_tokens: u64,
    pub auto_compact: Option<bool>,
    pub compact_threshold: Option<f64>,
    pub accounting: AccountingMode,
}

impl Default for CompactionConfig {
    fn default() -> Self {
        Self {
            max_tokens: DEFAULT_CONTEXT_WINDOW,
            auto_compact: None,
            compact_threshold: None,
            accounting: AccountingMode::Estimated,
        }
    }
}

/// Current model + the models each mounted provider advertises
/// (mirror of frozen `kernel.session_ops.ModelListing`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ModelListing {
    pub provider: String,
    pub current: String,
    pub available: Vec<String>,
}

/// The coordinator-derived half of `/status` (the app adds mode/cost);
/// mirror of frozen `kernel.session_ops.StatusInfo`.
///
/// Python `effort: str | None = None`; the renderer treats `None` *and*
/// empty as "(default)" (Python's `or`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct StatusInfo {
    pub session_id: String,
    pub provider: String,
    pub model: String,
    pub effort: Option<String>,
    pub messages: u64,
    pub tools: u64,
    pub agents: Vec<String>,
}

/// One discovered skill (mirror of frozen `kernel.session_ops.SkillInfo`).
///
/// `shortcut` is the optional slash alias from the skill's `shortcut:`
/// frontmatter (`/cosam` → `cranky-old-sam`); empty when the skill has none.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SkillInfo {
    pub name: String,
    pub description: String,
    pub shortcut: String,
}

impl SkillInfo {
    pub fn new(name: impl Into<String>, description: impl Into<String>) -> Self {
        Self { name: name.into(), description: description.into(), shortcut: String::new() }
    }
}

/// One row of the resume picker / `session list` table (mirror of frozen
/// `kernel.session_manager.SessionSummary`).
///
/// `messages` is the transcript line count; `mtime` is the directory
/// modification time (Unix seconds) used for the human [`time_ago`]
/// label; `turns` is `None` when stored metadata predates the field.
///
/// [`time_ago`]: SessionSummary::time_ago
#[derive(Clone, Debug, PartialEq)]
pub struct SessionSummary {
    pub session_id: String,
    pub name: String,
    pub bundle: String,
    pub messages: u64,
    pub mtime: f64,
    pub turns: Option<u64>,
}

impl Default for SessionSummary {
    /// Python dataclass defaults (`bundle="unknown"`, the rest zero/empty).
    fn default() -> Self {
        Self {
            session_id: String::new(),
            name: String::new(),
            bundle: "unknown".to_string(),
            messages: 0,
            mtime: 0.0,
            turns: None,
        }
    }
}

impl SessionSummary {
    /// Python `session_id[:8]` (character slice).
    pub fn short_id(&self) -> String {
        self.session_id.chars().take(8).collect()
    }

    /// Human age against the wall clock (Python's `time_ago` property
    /// reads `datetime.now(UTC)`); zero `mtime` reads "unknown".
    pub fn time_ago(&self) -> String {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        self.time_ago_at(now)
    }

    /// Pure variant for injected clocks (`now_secs` in Unix seconds).
    pub fn time_ago_at(&self, now_secs: f64) -> String {
        if self.mtime == 0.0 {
            return "unknown".to_string();
        }
        format_time_ago(now_secs - self.mtime)
    }
}

/// Human-readable age (`just now` / `5m ago` / `2d ago`).
///
/// Port of `kernel.session_manager.format_time_ago`; takes the elapsed
/// seconds directly instead of a datetime (the injected-clock surface).
pub fn format_time_ago(elapsed_seconds: f64) -> String {
    let seconds = elapsed_seconds as i64; // Python int(): truncate toward zero
    if seconds < 60 {
        return "just now".to_string();
    }
    let minutes = seconds / 60;
    if minutes < 60 {
        return format!("{minutes}m ago");
    }
    let hours = minutes / 60;
    if hours < 24 {
        return format!("{hours}h ago");
    }
    let days = hours / 24;
    if days < 30 {
        return format!("{days}d ago");
    }
    let months = days / 30;
    if months < 12 {
        return format!("{months}mo ago");
    }
    format!("{}y ago", days / 365)
}

/// Shorthand for a token-styled segment (all other fields default).
fn seg(text: impl Into<String>, token: StyleToken) -> Segment {
    Segment { style_token: token, ..Segment::new(text) }
}

fn header(label: &str, detail: &str) -> Vec<Segment> {
    vec![
        seg("· ", StyleToken::Blue),
        Segment { style_token: StyleToken::Bright, bold: true, ..Segment::new(label) },
        seg(format!("  {detail}\n"), StyleToken::Dim),
    ]
}

/// Python `f"{n:,}"` — thousands separators.
fn thousands(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, ch) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

/// `/model` (no arg): current model + the provider's advertised set.
pub fn model_listing_spans(listing: &ModelListing) -> Vec<Segment> {
    if listing.provider.is_empty() {
        return vec![seg("  no provider mounted\n", StyleToken::Dimmer)];
    }
    let mut spans = header(
        "Model",
        &format!("provider {} · /model <name> switches", listing.provider),
    );
    let current = if listing.current.is_empty() {
        "(provider default)"
    } else {
        listing.current.as_str()
    };
    if !listing.available.is_empty() {
        for model in &listing.available {
            let is_current = *model == listing.current;
            spans.push(seg(
                format!("  {} ", if is_current { "▸" } else { " " }),
                if is_current { StyleToken::Green } else { StyleToken::Dim },
            ));
            spans.push(Segment {
                style_token: if is_current { StyleToken::Green } else { StyleToken::Teal },
                bold: is_current,
                ..Segment::new(format!("{model}\n"))
            });
        }
    } else {
        spans.push(seg("  current  ", StyleToken::Dim));
        spans.push(seg(format!("{current}\n"), StyleToken::Green));
        spans.push(seg("  (provider advertises no model list)\n", StyleToken::Dimmer));
    }
    spans
}

/// `/status`: coordinator snapshot joined with app-side mode/cost.
pub fn status_spans(
    info: &StatusInfo,
    mode: &str,
    bundle: &str,
    session_short: &str,
    cost: Decimal,
    compaction: &CompactionConfig,
) -> Vec<Segment> {
    let session: String = if !session_short.is_empty() {
        session_short.to_string()
    } else if !info.session_id.is_empty() {
        info.session_id.chars().take(6).collect()
    } else {
        "—".to_string()
    };
    let mut spans = header("Status", &format!("session {session}"));
    let window = thousands(compaction.max_tokens);
    let mut compaction_label = match compaction.auto_compact {
        Some(true) => {
            let threshold = match compaction.compact_threshold {
                // Python f"{value:.0%}" — ×100, round to 0 places.
                Some(value) => format!(" · {:.0}%", value * 100.0),
                None => String::new(),
            };
            format!("on{threshold} · {window} token window")
        }
        Some(false) => format!("off · {window} token window"),
        None => format!("bundle default · {window} token window"),
    };
    compaction_label.push_str(&format!(" · {} accounting", compaction.accounting.as_str()));
    let effort = info.effort.as_deref().filter(|e| !e.is_empty()).unwrap_or("(default)");
    let provider = if info.provider.is_empty() { "—" } else { info.provider.as_str() };
    let model = if info.model.is_empty() { "(default)" } else { info.model.as_str() };
    // `round_dp` uses banker's rounding (MidpointNearestEven), matching the
    // default decimal context Python's `f"${cost:.2f}"` formats with.
    let rows: [(&str, String); 10] = [
        ("bundle", if bundle.is_empty() { "—".to_string() } else { bundle.to_string() }),
        ("mode", mode.to_string()),
        ("provider", provider.to_string()),
        ("model", model.to_string()),
        ("effort", effort.to_string()),
        ("messages", info.messages.to_string()),
        ("auto compact", compaction_label),
        ("tools", info.tools.to_string()),
        ("agents", info.agents.len().to_string()),
        ("cost", format!("${:.2}", cost.round_dp(2))),
    ];
    let width = rows.iter().map(|(label, _)| label.chars().count()).max().unwrap_or(0);
    for (label, value) in &rows {
        spans.push(seg(format!("  {label:<width$}  "), StyleToken::Dim));
        spans.push(seg(format!("{value}\n"), StyleToken::Teal));
    }
    spans
}

/// `/sessions`: the stored-session roster (name · id · msgs · age).
///
/// The live session (its short id is a prefix of *current*) is marked
/// with a green ▸; the rest read dim. Read-only — switching sessions is
/// a fresh `amplifier-newtui resume <id>` (noted in the header), never
/// an in-place teardown.
pub fn sessions_spans(summaries: &[SessionSummary], current: &str) -> Vec<Segment> {
    if summaries.is_empty() {
        return vec![seg(
            "  no stored sessions · this project has no history yet\n",
            StyleToken::Dimmer,
        )];
    }
    let mut spans = header(
        "Sessions",
        &format!("{} stored · resume: amplifier-newtui resume <id>", summaries.len()),
    );
    for summary in summaries {
        let is_current = !current.is_empty() && summary.session_id.starts_with(current);
        spans.push(seg(
            if is_current { "  ▸ " } else { "    " },
            if is_current { StyleToken::Green } else { StyleToken::Dim },
        ));
        spans.push(Segment {
            style_token: if is_current { StyleToken::Green } else { StyleToken::Teal },
            bold: is_current,
            ..Segment::new(format!("{}  ", summary.short_id()))
        });
        let name = if summary.name.is_empty() { "—" } else { summary.name.as_str() };
        spans.push(seg(
            format!(
                "{}  ·  {}  ·  {} msgs  ·  {}\n",
                name,
                summary.bundle,
                summary.messages,
                summary.time_ago()
            ),
            StyleToken::Dim,
        ));
    }
    spans
}

/// A simple bulleted roster for `/tools` and `/agents`.
pub fn names_spans<S: AsRef<str>>(label: &str, names: &[S], empty: &str) -> Vec<Segment> {
    if names.is_empty() {
        return vec![seg(format!("  {empty}\n"), StyleToken::Dimmer)];
    }
    let mut spans = header(label, &format!("{} mounted", names.len()));
    for name in names {
        spans.push(seg("  • ", StyleToken::Dim));
        spans.push(seg(format!("{}\n", name.as_ref()), StyleToken::Teal));
    }
    spans
}

/// `/skills`: the available-skills roster (name + one-line description).
pub fn skills_spans(skills: &[SkillInfo]) -> Vec<Segment> {
    if skills.is_empty() {
        return vec![seg(
            "  no skills · add sources under .amplifier/skills/ or ~/.amplifier/skills/\n",
            StyleToken::Dimmer,
        )];
    }
    let mut spans =
        header("Skills", &format!("{} available · /skill <name> loads one", skills.len()));

    // A shortcut alias reads as its slash trigger (story #1: /cosam).
    let label = |s: &SkillInfo| -> String {
        if s.shortcut.is_empty() {
            s.name.clone()
        } else {
            format!("{} (/{})", s.name, s.shortcut)
        }
    };

    let width = skills.iter().map(|s| label(s).chars().count()).max().unwrap_or(0);
    for skill in skills {
        let name = label(skill);
        spans.push(seg(format!("  {name:<width$}  "), StyleToken::Teal));
        let desc: String = skill
            .description
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .chars()
            .take(90)
            .collect();
        spans.push(seg(format!("{desc}\n"), StyleToken::Dim));
    }
    spans
}

/// `/skill <name>`: a loaded-skill header + the skill body (markdown).
pub fn skill_loaded_spans(name: &str, content: &str) -> Vec<Segment> {
    let mut spans = vec![
        seg("· ", StyleToken::Blue),
        Segment { style_token: StyleToken::Bright, bold: true, ..Segment::new("Skill loaded") },
        seg(format!("  {name}\n"), StyleToken::Dim),
    ];
    spans.extend(answer_spans(content));
    spans
}

/// `/mcp`: configured servers (mcp.json) + live-connected MCP tools.
///
/// *servers* is Python's `dict[str, str]` as insertion-ordered
/// `(name, summary)` pairs.
pub fn mcp_spans<S: AsRef<str>>(servers: &[(String, String)], live_tools: &[S]) -> Vec<Segment> {
    let mut spans = header(
        "MCP",
        &format!(
            "{} server(s) · {} tool(s) connected · /mcp add|remove",
            servers.len(),
            live_tools.len()
        ),
    );
    if !servers.is_empty() {
        let width = servers.iter().map(|(name, _)| name.chars().count()).max().unwrap_or(0);
        for (name, summary) in servers {
            spans.push(seg(format!("  {name:<width$}  "), StyleToken::Teal));
            spans.push(seg(format!("{summary}\n"), StyleToken::Dim));
        }
    } else {
        spans.push(seg(
            "  no servers in mcp.json · /mcp add <name> <cmd> [args…]\n",
            StyleToken::Dimmer,
        ));
    }
    if !live_tools.is_empty() {
        let joined = live_tools.iter().map(|t| t.as_ref()).collect::<Vec<_>>().join(", ");
        spans.push(seg(format!("  connected: {joined}\n"), StyleToken::Dimmer));
    }
    spans
}

/// `/diff`: a compact, theme-token-only git patch.
///
/// `None` (git unavailable / not a repo) and a clean tree each get a
/// plain dim line; long patches truncate to [`DIFF_MAX_LINES`] with a
/// note (never flood the transcript). Additions and deletions use the
/// active theme's green/red foreground on its tab background, so the
/// highlight follows runtime theme switches without embedding colors.
pub fn diff_spans(patch: Option<&str>, staged: bool) -> Vec<Segment> {
    let scope = if staged { "staged " } else { "" };
    let Some(patch) = patch else {
        return vec![seg(
            format!("  no {scope}diff · not a git repo or git unavailable\n"),
            StyleToken::Dimmer,
        )];
    };
    if patch.trim().is_empty() {
        return vec![seg(format!("  working tree clean · no {scope}changes\n"), StyleToken::Dim)];
    }
    let lines: Vec<&str> = patch.lines().collect();
    let truncated = lines.len() > DIFF_MAX_LINES;
    let mut spans: Vec<Segment> = Vec::new();
    for line in lines.iter().take(DIFF_MAX_LINES) {
        let mut token = StyleToken::Dim;
        let mut background = None;
        let mut bold = false;
        if line.starts_with("@@") {
            token = StyleToken::Blue;
            bold = true;
        } else if line.starts_with("diff --git ")
            || line.starts_with("index ")
            || line.starts_with("--- ")
            || line.starts_with("+++ ")
        {
            token = StyleToken::Teal;
        } else if line.starts_with('+') {
            token = StyleToken::Green;
            background = Some(StyleToken::BgTab);
        } else if line.starts_with('-') {
            token = StyleToken::Red;
            background = Some(StyleToken::BgTab);
        }
        spans.push(Segment {
            style_token: token,
            bold,
            bg_token: background,
            ..Segment::new(format!("  {line}\n"))
        });
    }
    if truncated {
        spans.push(seg(
            format!(
                "\n  … +{} more lines · /diff shows the head\n",
                lines.len() - DIFF_MAX_LINES
            ),
            StyleToken::Dimmer,
        ));
    }
    spans
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::str::FromStr;

    use super::*;

    fn text(spans: &[Segment]) -> String {
        spans.iter().map(|s| s.text.as_str()).collect()
    }

    // Rust-only pin: boundary values oracle-checked against the real Python
    // `kernel.session_manager.format_time_ago` (including its `0y ago` quirk
    // for days in [360, 364], where months >= 12 but days // 365 == 0).
    #[test]
    fn format_time_ago_boundaries_match_python() {
        let cases: [(f64, &str); 14] = [
            (0.0, "just now"),
            (59.0, "just now"),
            (60.0, "1m ago"),
            (3599.0, "59m ago"),
            (3600.0, "1h ago"),
            (86399.0, "23h ago"),
            (86400.0, "1d ago"),
            (2591999.0, "29d ago"),
            (2592000.0, "1mo ago"),
            (31103999.0, "11mo ago"),
            (31104000.0, "0y ago"),
            (31535999.0, "0y ago"),
            (31536000.0, "1y ago"),
            (63072000.0, "2y ago"),
        ];
        for (elapsed, expected) in cases {
            assert_eq!(format_time_ago(elapsed), expected, "elapsed {elapsed}");
        }
    }

    // Python: tests/test_ui_session_ops_view.py::test_model_listing_marks_the_current_model
    #[test]
    fn test_model_listing_marks_the_current_model() {
        let spans = model_listing_spans(&ModelListing {
            provider: "anthropic".to_string(),
            current: "m2".to_string(),
            available: vec!["m1".to_string(), "m2".to_string()],
        });
        let text = text(&spans);
        assert!(text.contains("Model") && text.contains("anthropic"));
        let current: Vec<&Segment> = spans.iter().filter(|s| s.text.trim() == "m2").collect();
        assert!(!current.is_empty() && current[0].bold); // active model is bold
        assert!(text.contains("▸")); // current-row glyph
    }

    // Python: test_model_listing_no_provider
    #[test]
    fn test_model_listing_no_provider() {
        let spans = model_listing_spans(&ModelListing::default());
        assert!(text(&spans).contains("no provider"));
    }

    // Python: test_status_spans_include_mode_and_cost
    #[test]
    fn test_status_spans_include_mode_and_cost() {
        let info = StatusInfo {
            session_id: "abcdef123".to_string(),
            provider: "anthropic".to_string(),
            model: "m1".to_string(),
            effort: Some("high".to_string()),
            messages: 4,
            tools: 7,
            agents: vec!["explorer".to_string(), "critic".to_string()],
        };
        let spans = status_spans(
            &info,
            "build",
            "newtui",
            "abcdef",
            Decimal::from_str("1.23").unwrap(),
            &CompactionConfig {
                max_tokens: 200_000,
                auto_compact: Some(true),
                compact_threshold: Some(0.8),
                accounting: AccountingMode::Estimated,
            },
        );
        let text = text(&spans);
        assert!(text.contains("build"));
        assert!(text.contains("newtui"));
        assert!(text.contains("$1.23"));
        assert!(text.contains("high"));
        assert!(text.contains('2')); // agent count
        assert!(text.contains("auto compact"));
        assert!(text.contains("on · 80% · 200,000 token window · estimated accounting"));
    }

    // Python: test_names_spans_roster_and_empty
    #[test]
    fn test_names_spans_roster_and_empty() {
        assert!(text(&names_spans("Tools", &["a", "b", "c"], "none")).contains("3 mounted"));
        let empty: [&str; 0] = [];
        assert!(text(&names_spans("Tools", &empty, "none")).contains("none"));
    }

    // Python: test_diff_spans_states
    #[test]
    fn test_diff_spans_states() {
        assert!(text(&diff_spans(None, false)).contains("not a git repo"));
        assert!(text(&diff_spans(Some(""), false)).contains("clean"));
        let body = text(&diff_spans(Some("diff --git a/x b/x\n+added line\n"), false));
        assert!(body.contains("added line"));
    }

    // Python: test_diff_spans_uses_theme_tokens_for_patch_semantics
    #[test]
    fn test_diff_spans_uses_theme_tokens_for_patch_semantics() {
        let spans = diff_spans(
            Some("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n same"),
            false,
        );
        let by_text: HashMap<&str, &Segment> =
            spans.iter().map(|span| (span.text.trim(), span)).collect();
        assert_eq!(by_text["@@ -1 +1 @@"].style_token, StyleToken::Blue);
        assert!(by_text["@@ -1 +1 @@"].bold);
        assert_eq!(by_text["-old"].style_token, StyleToken::Red);
        assert_eq!(by_text["-old"].bg_token, Some(StyleToken::BgTab));
        assert_eq!(by_text["+new"].style_token, StyleToken::Green);
        assert_eq!(by_text["+new"].bg_token, Some(StyleToken::BgTab));
        assert_eq!(by_text["same"].style_token, StyleToken::Dim);
    }

    // Python: test_diff_spans_truncates_long_patches
    #[test]
    fn test_diff_spans_truncates_long_patches() {
        let patch =
            (0..1000).map(|i| format!("+line {i}")).collect::<Vec<_>>().join("\n");
        assert!(text(&diff_spans(Some(&patch), false)).contains("more lines"));
    }

    // Python: test_diff_spans_staged_scope_wording
    #[test]
    fn test_diff_spans_staged_scope_wording() {
        assert!(text(&diff_spans(Some(""), true)).contains("staged"));
    }

    // Python: test_skills_spans_roster_and_empty
    #[test]
    fn test_skills_spans_roster_and_empty() {
        let spans = skills_spans(&[
            SkillInfo::new("design-patterns", "SOLID principles"),
            SkillInfo::new("simplify", "cut cruft"),
        ]);
        let text_all = text(&spans);
        assert!(text_all.contains("2 available"));
        assert!(text_all.contains("design-patterns") && text_all.contains("SOLID"));
        assert!(text(&skills_spans(&[])).contains("no skills"));
    }

    // Python: test_skills_spans_show_shortcut_aliases
    #[test]
    fn test_skills_spans_show_shortcut_aliases() {
        let spans = skills_spans(&[
            SkillInfo {
                name: "cranky-old-sam".to_string(),
                description: "crusty review".to_string(),
                shortcut: "cosam".to_string(),
            },
            SkillInfo::new("simplify", "cut cruft"),
        ]);
        let text = text(&spans);
        assert!(text.contains("/cosam")); // the alias reads as its slash trigger
        assert!(!text.contains("/simplify")); // no fake alias for shortcut-less skills
    }

    // Python: test_skill_loaded_spans_has_header_and_body
    #[test]
    fn test_skill_loaded_spans_has_header_and_body() {
        let text = text(&skill_loaded_spans("simplify", "# simplify\n\ncut the cruft"));
        assert!(text.contains("Skill loaded"));
        assert!(text.contains("simplify"));
        assert!(text.contains("cut the cruft"));
    }

    // Python: test_mcp_spans_servers_and_empty
    #[test]
    fn test_mcp_spans_servers_and_empty() {
        let servers = vec![("postgres".to_string(), "stdio · npx".to_string())];
        let text_all = text(&mcp_spans(&servers, &["mcp_postgres_query"]));
        assert!(text_all.contains("1 server"));
        assert!(text_all.contains("postgres"));
        assert!(text_all.contains("mcp_postgres_query"));
        let empty: [&str; 0] = [];
        assert!(text(&mcp_spans(&[], &empty)).contains("no servers"));
    }

    // Python: test_sessions_spans_empty
    #[test]
    fn test_sessions_spans_empty() {
        assert!(text(&sessions_spans(&[], "")).contains("no stored sessions"));
    }

    // Python: test_sessions_spans_lists_rows_and_marks_current
    #[test]
    fn test_sessions_spans_lists_rows_and_marks_current() {
        let rows = [
            SessionSummary {
                session_id: "abc12345ff".to_string(),
                name: "auth".to_string(),
                bundle: "newtui".to_string(),
                messages: 6,
                mtime: 0.0,
                ..SessionSummary::default()
            },
            SessionSummary {
                session_id: "def67890aa".to_string(),
                name: String::new(),
                bundle: "dev".to_string(),
                messages: 2,
                mtime: 0.0,
                ..SessionSummary::default()
            },
        ];
        let spans = sessions_spans(&rows, "abc12345");
        let text_all = text(&spans);
        assert!(text_all.contains("Sessions"));
        assert!(text_all.contains("abc12345") && text_all.contains("def67890"));
        assert!(text_all.contains("auth"));
        assert!(text_all.contains("6 msgs"));
        assert!(text_all.contains("▸")); // current-session marker
        // The current session's short id renders bold.
        let current: Vec<&Segment> =
            spans.iter().filter(|sp| sp.text.trim() == "abc12345").collect();
        assert!(!current.is_empty() && current[0].bold);
    }
}
