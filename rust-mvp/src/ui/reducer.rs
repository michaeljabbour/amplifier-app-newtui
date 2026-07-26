//! The event reducer: normalized UIEvents → transcript blocks + host actions.
//!
//! Port of `src/amplifier_app_newtui/ui/reducer.py`.
//!
//! The app consumes the runtime's UIEvent queue and feeds every event to
//! [`TranscriptReducer::handle`]. The reducer owns turn-shaped state (tool
//! correlation by `tool_call_id`, plan blocks keyed by title, working-status
//! telemetry, lane tree lines, ledger close-out) and acts on the app
//! exclusively through the narrow [`ReducerHost`] trait — it never touches
//! widgets directly, so the whole turn lifecycle is unit-testable with a
//! fake host.
//!
//! Demo conventions honored (see `kernel/demo.py` module docstring): role
//! markers in `ContentBlockEnd.block["demo_role"]`, `update_plan` tool calls
//! as plan checklists, `bash` denials as ⊘ blocked lines, and `DemoTurnSpec`
//! close-out labels via the adapter's `turn_spec` hook. The real runtime
//! flows through the same paths with generic fallbacks.
//!
//! # Porting notes
//!
//! - **Sharing shape**: the Python reducer shares one `BlockIdAllocator`,
//!   one `LaneRegistry` and one host with its [`LaneReducer`]. Here the
//!   `TranscriptReducer` *owns* the `LaneReducer` and reaches the shared
//!   pieces through it (`lane.ids_mut()`, `lane.lanes`, `lane.host_mut()`),
//!   so there is exactly one id sequence and one registry with no
//!   `Rc<RefCell<..>>`.
//! - **Replay proxy**: Python swaps `self._host` for a `_ReplayHost` proxy
//!   during [`TranscriptReducer::replay`]. Rust can't swap the host type at
//!   runtime, so the host is permanently wrapped in a private [`ReplayGate`]
//!   whose `replay` toggle applies the exact `_ReplayHost` suppression
//!   contract — dispatch stays one code path either way.
//! - `spec_lookup` / `lane_seed_lookup` / `evidence_lookup` callables →
//!   boxed closures in [`ReducerOptions`].

use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

use regex::Regex;
use rust_decimal::Decimal;
use serde_json::{Map, Value};

use crate::kernel::cost::CostTracker;
use crate::kernel::events as ev;
use crate::model::blocks::{
    ActivityBranch, Answer, BlockIdAllocator, Blocked, BrainstormIdea, DelegateEntry,
    DelegateState, DelegateSummaryBlock, Narration, PlanBlock, PlanItem, PlanItemState, Recap,
    Segment, StyleToken, Thinking, TodoItem, TodoStatus, ToolLine, ToolLineBodyStyle,
    ToolLineStatus, TranscriptBlock, TurnRule, UserLine, WorkingStatus,
};
use crate::model::evidence::EvidenceLink;
use crate::model::lanes::{LaneRegistry, LaneStateName, LaneUpdate, RegisterOptions};
use crate::model::turn::{OutcomeKind, OutcomeLedger, TurnOutcome, TurnTelemetry};
use crate::ui::lane_reducer::{LaneReducer, LaneTailHost};
use crate::ui::live_tail::answer_spans;

// Python `from .lane_reducer import LANE_TAIL_NOTIFY_SECONDS as
// LANE_TAIL_NOTIFY_SECONDS, _LANE_TRANSCRIPT_MAX_BLOCKS as ...` re-exports.
pub use crate::ui::lane_reducer::{LANE_TAIL_NOTIFY_SECONDS, LANE_TRANSCRIPT_MAX_BLOCKS};

fn recap_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?s)^Goal:\s*(?P<goal>.+?)\.\s*Next:\s*(?P<next>.+?)\.?\s*$")
            .expect("valid regex")
    })
}

fn idea_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?s)^(\d+)\s+(.*)$").expect("valid regex"))
}

fn mode_notice_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^mode (\w+)").expect("valid regex"))
}

const CHARS_PER_TOKEN: usize = 4;

/// Coerce a raw plan-step `status` to a valid state (else pending).
fn plan_state(value: Option<&Value>) -> PlanItemState {
    match value {
        Some(Value::String(s)) if s == "pending" => PlanItemState::Pending,
        Some(Value::String(s)) if s == "active" => PlanItemState::Active,
        Some(Value::String(s)) if s == "done" => PlanItemState::Done,
        _ => PlanItemState::Pending,
    }
}

/// Coerce a raw todo `status` to a valid state (else pending).
fn todo_status(value: Option<&Value>) -> TodoStatus {
    match value {
        Some(Value::String(s)) if s == "pending" => TodoStatus::Pending,
        Some(Value::String(s)) if s == "in_progress" => TodoStatus::InProgress,
        Some(Value::String(s)) if s == "completed" => TodoStatus::Completed,
        _ => TodoStatus::Pending,
    }
}

/// Python truthiness for a JSON value (`if tool_input.get(key):`).
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// Python `str(value)` for the payload shapes the reducer reads (strings
/// pass through verbatim; other JSON scalars use their JSON text).
fn value_text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// `str(map.get(key, ""))` over a JSON object.
fn get_str(map: &Map<String, Value>, key: &str) -> String {
    map.get(key).map(value_text).unwrap_or_default()
}

/// `map.get(key)` filtered through Python truthiness (`a or b or ""`).
fn get_truthy_str(map: &Map<String, Value>, keys: &[&str]) -> String {
    for key in keys {
        if let Some(value) = map.get(*key) {
            if truthy(value) {
                return value_text(value);
            }
        }
    }
    String::new()
}

/// Rough token estimate for tool traffic (~4 chars/token heuristic).
///
/// Provider usage events do not split tokens by bucket, so the /context
/// `tools` bucket is accounted from the serialized tool inputs and results
/// that actually occupy the window. (Python measures `len(str(dict))`; here
/// non-empty maps are measured via their JSON text — same order of
/// magnitude, slightly different constant.)
fn approx_tokens(parts: &[&dyn ApproxPart]) -> i64 {
    let total: usize = parts.iter().map(|part| part.approx_len()).sum();
    if total == 0 {
        0
    } else {
        ((total / CHARS_PER_TOKEN) as i64).max(1)
    }
}

trait ApproxPart {
    /// `len(str(part)) if part else 0`.
    fn approx_len(&self) -> usize;
}

impl ApproxPart for Map<String, Value> {
    fn approx_len(&self) -> usize {
        if self.is_empty() {
            return 0;
        }
        serde_json::to_string(self).map(|s| s.chars().count()).unwrap_or(0)
    }
}

impl ApproxPart for &str {
    fn approx_len(&self) -> usize {
        self.chars().count()
    }
}

// -- activity humanization (rolling burst digest + live tree) ------------------

/// tool name -> (verb, singular noun | None). `None` renders "verb N×".
fn tool_verbs(tool: &str) -> Option<(&'static str, Option<&'static str>)> {
    Some(match tool {
        "bash" | "shell" => ("ran", Some("shell command")),
        "read_file" => ("read", Some("file")),
        "write_file" => ("wrote", Some("file")),
        "edit_file" | "apply_patch" | "multi_edit" => ("edited", Some("file")),
        "grep" | "glob" | "search" => ("searched", None),
        "web_fetch" => ("fetched", Some("page")),
        "web_search" => ("searched web", None),
        "load_skill" => ("loaded", Some("skill")),
        _ => return None,
    })
}

/// Reading order for the digest so it scans naturally, whatever order the
/// model actually ran the tools in.
const VERB_ORDER: [&str; 8] = [
    "read",
    "searched",
    "searched web",
    "ran",
    "edited",
    "wrote",
    "fetched",
    "loaded",
];
/// Live-tree rows kept beneath the pulse.
const ACTIVITY_TAIL: usize = 3;
const OP_LABEL_MAX: usize = 52;
const CHANGE_PREVIEW_LINES: usize = 80;
const CHANGE_DETAIL_LINES: usize = 240;
const CHANGE_TOOLS: [&str; 3] = ["write_file", "edit_file", "apply_patch"];

/// Present-tense labels for the compact per-agent activity ticker.
fn live_tool_verb(tool: &str) -> Option<&'static str> {
    Some(match tool {
        "bash" | "shell" => "running",
        "read_file" => "reading",
        "write_file" => "writing",
        "edit_file" | "apply_patch" | "multi_edit" => "editing",
        "grep" | "search" => "searching",
        "glob" => "finding files",
        "web_fetch" => "fetching",
        "web_search" => "searching web",
        "load_skill" => "loading",
        "delegate" => "delegating",
        _ => return None,
    })
}

/// A digest tally key: `(verb, noun | None)`.
type VerbNoun = (String, Option<String>);

fn verb_noun(tool: &str) -> VerbNoun {
    match tool_verbs(tool) {
        Some((verb, noun)) => (verb.to_string(), noun.map(str::to_string)),
        None => ("used".to_string(), Some(tool.replace('_', " "))),
    }
}

fn basename(path: &str) -> String {
    let path = path.trim_end_matches('/');
    match path.rfind('/') {
        Some(index) => path[index + 1..].to_string(),
        None => path.to_string(),
    }
}

/// Short human target for a tool call (for the live tree).
fn op_target(tool: &str, tool_input: &Map<String, Value>) -> String {
    if tool == "bash" || tool == "shell" {
        let cmd = get_str(tool_input, "command").trim().replace('\n', " ");
        return format!("$ {cmd}");
    }
    for key in ["file_path", "path", "filename", "notebook_path"] {
        if let Some(value) = tool_input.get(key) {
            if truthy(value) {
                return basename(&value_text(value));
            }
        }
    }
    for key in ["pattern", "query", "url", "skill", "name"] {
        if let Some(value) = tool_input.get(key) {
            if truthy(value) {
                return value_text(value);
            }
        }
    }
    String::new()
}

/// One full detail line for the expandable digest body.
fn op_detail(tool: &str, tool_input: &Map<String, Value>) -> String {
    if tool == "bash" || tool == "shell" {
        let cmd = get_str(tool_input, "command").trim().to_string();
        return if cmd.is_empty() {
            "$ (command)".to_string()
        } else {
            format!("$ {cmd}")
        };
    }
    let verb = verb_noun(tool).0;
    let target = op_target(tool, tool_input);
    if target.is_empty() {
        verb
    } else {
        format!("{verb} {target}").trim().to_string()
    }
}

fn truncate(text: &str, width: usize) -> String {
    let text = text.replace('\n', " ");
    let text = text.trim();
    if text.chars().count() <= width {
        return text.to_string();
    }
    let mut clipped: String = text.chars().take(width.saturating_sub(1)).collect();
    clipped.push('…');
    clipped
}

fn md_link_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\[([^\]]+)\]\([^)]+\)").expect("valid regex"))
}

fn md_block_prefix_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\s*(#{1,6}|[-*+>]|\d+[.)])\s+").expect("valid regex"))
}

/// Python `_MD_INLINE_RE.sub("", text)` — the pattern needs look-around
/// (`_(?=\w)` / `(?<=\w)_`) which the `regex` crate lacks, so the exact
/// alternation `(\*\*|__|~~|`+|\*|_(?=\w)|(?<=\w)_)` is applied manually,
/// left-to-right over the original text like `re.sub` does.
fn strip_inline_md(text: &str) -> String {
    fn word(ch: char) -> bool {
        ch.is_alphanumeric() || ch == '_'
    }
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        let ch = chars[i];
        let next = chars.get(i + 1).copied();
        if (ch == '*' && next == Some('*'))
            || (ch == '_' && next == Some('_'))
            || (ch == '~' && next == Some('~'))
        {
            i += 2;
            continue;
        }
        if ch == '`' {
            while i < chars.len() && chars[i] == '`' {
                i += 1;
            }
            continue;
        }
        if ch == '*' {
            i += 1;
            continue;
        }
        if ch == '_' {
            let ahead = chars.get(i + 1).copied().is_some_and(word);
            let behind = i > 0 && word(chars[i - 1]);
            if ahead || behind {
                i += 1;
                continue;
            }
        }
        out.push(ch);
        i += 1;
    }
    out
}

/// Distil a delegate's (often Markdown) result into a clean one-line lane
/// summary: take the first non-empty line, drop a leading heading/list/quote
/// marker and inline emphasis, collapse whitespace, prefer the first sentence
/// when long, and truncate. Keeps the lane row readable instead of pasting raw
/// Markdown (`## Foo **bar**…`) into it.
pub fn lane_result_summary(result: &str, width: usize) -> String {
    let first = result
        .lines()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("");
    let first = md_block_prefix_re().replace(first, "");
    let first = md_link_re().replace_all(&first, "$1");
    let mut first = strip_inline_md(&first).trim().to_string();
    if first.chars().count() > width {
        first = first.split(". ").next().unwrap_or("").to_string();
    }
    truncate(&first, width)
}

/// Compact one-liner for the live activity tree.
fn op_label(tool: &str, tool_input: &Map<String, Value>) -> String {
    if tool == "bash" || tool == "shell" {
        return truncate(&op_target(tool, tool_input), OP_LABEL_MAX);
    }
    let verb = verb_noun(tool).0;
    let target = op_target(tool, tool_input);
    if target.is_empty() {
        truncate(&verb, OP_LABEL_MAX)
    } else {
        truncate(format!("{verb} {}", basename(&target)).trim(), OP_LABEL_MAX)
    }
}

/// Short present-tense child activity suitable for an in-place ticker.
fn live_op_label(tool: &str, tool_input: &Map<String, Value>) -> String {
    let verb = match live_tool_verb(tool) {
        Some(verb) => verb.to_string(),
        None => format!("using {}", tool.replace('_', " ")),
    };
    let mut target = op_target(tool, tool_input);
    if (tool == "bash" || tool == "shell") && target.starts_with("$ ") {
        target = target[2..].to_string();
    }
    if target.is_empty() {
        truncate(&verb, OP_LABEL_MAX)
    } else {
        truncate(format!("{verb} {}", basename(&target)).trim(), OP_LABEL_MAX)
    }
}

/// Return `(paths, bounded diff-like detail)` for a native file write.
fn change_preview(tool: &str, tool_input: &Map<String, Value>) -> (Vec<String>, Vec<String>) {
    let path = get_truthy_str(tool_input, &["file_path", "path"])
        .trim()
        .to_string();
    if !CHANGE_TOOLS.contains(&tool) {
        return (Vec::new(), Vec::new());
    }
    let (paths, mut lines): (Vec<String>, Vec<String>) = if tool == "apply_patch" {
        let patch = get_truthy_str(tool_input, &["patch", "diff"]);
        let mut paths: Vec<String> = Vec::new();
        for marker in patch.lines() {
            if marker.starts_with("*** Add File:")
                || marker.starts_with("*** Update File:")
                || marker.starts_with("*** Delete File:")
            {
                if let Some((_, rest)) = marker.split_once(" File:") {
                    let entry = rest.trim().to_string();
                    if !paths.contains(&entry) {
                        paths.push(entry);
                    }
                }
            }
        }
        if !path.is_empty() && !paths.contains(&path) {
            paths.push(path.clone());
        }
        (paths, patch.lines().map(str::to_string).collect())
    } else if path.is_empty() {
        return (Vec::new(), Vec::new());
    } else if tool == "edit_file" {
        let old = get_str(tool_input, "old_string");
        let new = get_str(tool_input, "new_string");
        let mut lines = vec![
            format!("--- {path}"),
            format!("+++ {path}"),
            "@@ replaced text @@".to_string(),
        ];
        lines.extend(old.lines().map(|line| format!("-{line}")));
        lines.extend(new.lines().map(|line| format!("+{line}")));
        (vec![path.clone()], lines)
    } else {
        let content = get_str(tool_input, "content");
        let content_lines: Vec<&str> = content.lines().collect();
        let mut lines = vec![
            format!("+++ {path}"),
            format!("@@ wrote file · {} lines @@", content_lines.len()),
        ];
        lines.extend(content_lines.iter().map(|line| format!("+{line}")));
        (vec![path.clone()], lines)
    };
    if lines.len() > CHANGE_PREVIEW_LINES {
        let hidden = lines.len() - CHANGE_PREVIEW_LINES;
        lines.truncate(CHANGE_PREVIEW_LINES);
        lines.push(format!("… {hidden} more lines"));
    }
    (paths, lines)
}

/// `{('read','file'):4, ('ran','command'):6}` -> `Read 4 files · ran
/// 6 commands`. First segment capitalized; ordered for natural reading.
fn digest_summary(counts: &[(VerbNoun, usize)]) -> String {
    let mut ordered: Vec<&(VerbNoun, usize)> = counts.iter().collect();
    // Python `sorted` is stable, so ties keep insertion order.
    ordered.sort_by_key(|((verb, _), _)| {
        VERB_ORDER
            .iter()
            .position(|known| known == verb)
            .unwrap_or(VERB_ORDER.len())
    });
    let mut parts: Vec<String> = Vec::new();
    for ((verb, noun), n) in ordered {
        match noun {
            None => parts.push(format!("{verb} {n}×")),
            Some(noun) => {
                let plural = if *n != 1 { "s" } else { "" };
                parts.push(format!("{verb} {n} {noun}{plural}"));
            }
        }
    }
    if parts.is_empty() {
        return String::new();
    }
    let summary = parts.join(" · ");
    let mut chars = summary.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => summary,
    }
}

/// Format an integer with `,` thousands separators (Python `f"{n:,}"`).
fn format_thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, ch) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    if n < 0 {
        format!("-{out}")
    } else {
        out
    }
}

/// Python `"...".rstrip(" ·")`.
fn rstrip_sep(text: &str) -> String {
    text.trim_end_matches([' ', '·']).to_string()
}

/// Python `text[:n]` (character slice).
fn char_prefix(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

/// Close-out data for one turn (structurally `kernel.demo.DemoTurnSpec`).
///
/// Python `TurnSpecLike` protocol → a concrete struct (the demo adapter
/// builds these; tests fill only the fields they exercise via `Default`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TurnSpec {
    pub duration_ms: i64,
    pub tokens: u64,
    pub cached_pct: Option<u8>,
    pub cost: Decimal,
    pub cost_after: Decimal,
    pub outcome: String,
    pub shipped: bool,
    pub rule_label: String,
    pub checkpoint_label: String,
}

/// Initial lane presentation supplied by the adapter (demo fidelity).
#[derive(Clone, Debug, PartialEq)]
pub struct LaneSeed {
    pub activity: String,
    pub elapsed: f64,
    pub cost: Decimal,
    pub tokens: u64,
    pub state: LaneStateName,
}

impl Default for LaneSeed {
    fn default() -> Self {
        LaneSeed {
            activity: String::new(),
            elapsed: 0.0,
            cost: Decimal::ZERO,
            tokens: 0,
            state: LaneStateName::Running,
        }
    }
}

/// Live state for one agent in the current fan-out summary (D5).
#[derive(Clone, Debug)]
struct DelegateRow {
    agent: String,
    spawned_ts: f64,
    state: DelegateState,
    elapsed_s: f64,
    snippet: String,
}

impl DelegateRow {
    fn new(agent: &str, spawned_ts: f64) -> Self {
        DelegateRow {
            agent: agent.to_string(),
            spawned_ts,
            state: DelegateState::Running,
            elapsed_s: 0.0,
            snippet: String::new(),
        }
    }
}

/// The narrow surface the reducer drives (implemented by the app).
pub trait ReducerHost {
    fn mode_id(&self) -> String;
    fn append_block(&mut self, block: TranscriptBlock);
    fn replace_block(&mut self, block: TranscriptBlock);
    fn remove_block(&mut self, block_id: &str);
    fn show_notice(&mut self, text: &str);
    fn set_mode_by_id(&mut self, mode_id: &str, notify: bool);
    fn turn_started(&mut self);
    fn turn_finished(&mut self);
    fn lanes_changed(&mut self);
    fn plan_changed(&mut self, items: &[TodoItem]);
    fn approval_opened(&mut self, prompt: &str, options: &[String]);
    fn decision_deferred(&mut self, message: &str, decision_id: &str);
    fn stream_opened(&mut self, block_type: &str);
    fn stream_delta(&mut self, text: &str);
    fn stream_closed(&mut self);
    fn lane_tail_updated(&mut self, text: &str);
    fn lane_tail_cleared(&mut self);
}

/// Every [`ReducerHost`] serves the [`LaneReducer`]'s two lane callbacks —
/// the Python `LaneTailHost` protocol is a structural subset of
/// `ReducerHost`, and this blanket impl encodes that subset relation.
impl<T: ReducerHost> LaneTailHost for T {
    fn lane_tail_updated(&mut self, text: &str) {
        ReducerHost::lane_tail_updated(self, text);
    }

    fn lane_tail_cleared(&mut self) {
        ReducerHost::lane_tail_cleared(self);
    }
}

/// Event kinds [`TranscriptReducer::replay`] never re-dispatches.
///
/// Channel A (`stream_*`): the durable content_block_end records carry the
/// text (ADR-0007: never reconstruct one channel from the other), and a
/// live-tail replay would only churn the stream surface. Interactive or
/// transient surfaces (`notification`, `provider_notice`, `approval_*`)
/// must not re-fire from history — a stale decision must not resurrect in
/// the queue, and retry/throttle toasts belong to the moment they happened.
pub const REPLAY_SKIPPED_KINDS: [&str; 8] = [
    "stream_block_start",
    "stream_block_delta",
    "stream_block_end",
    "stream_aborted",
    "notification",
    "provider_notice",
    "approval_required",
    "approval_granted",
];

/// ReducerHost proxy for resume replay (DESIGN-SPEC §3/§11).
///
/// The Rust rendering of Python's `_ReplayHost`: instead of swapping the
/// host object, the live host is permanently wrapped and a `replay` toggle
/// applies the suppression contract — durable block mutations and plan
/// state pass through; everything interactive or ephemeral (notices,
/// approval presentation, needs-you deferrals, turn timers/bells/queue
/// drains, stream tail, per-event lane repaints) is silenced, so replaying
/// history can never re-trigger a side effect the session already had live.
struct ReplayGate<H: ReducerHost> {
    inner: H,
    replay: bool,
    /// Working-pulse block ids minted during replay. The working pulse is
    /// running-turn chrome — a replayed transcript has no running turn, so
    /// it never mounts (also load-bearing: the live bottom-ride removes and
    /// re-appends the pulse in one synchronous stretch, and the Python
    /// widget prune was deferred — a replayed ride would mount a duplicate).
    working_ids: HashSet<String>,
}

impl<H: ReducerHost> ReplayGate<H> {
    fn new(inner: H) -> Self {
        ReplayGate {
            inner,
            replay: false,
            working_ids: HashSet::new(),
        }
    }

    fn begin_replay(&mut self) {
        self.replay = true;
        self.working_ids.clear();
    }

    fn end_replay(&mut self) {
        self.replay = false;
        self.working_ids.clear();
    }
}

impl<H: ReducerHost> ReducerHost for ReplayGate<H> {
    fn mode_id(&self) -> String {
        self.inner.mode_id()
    }

    fn append_block(&mut self, block: TranscriptBlock) {
        if self.replay && block.kind() == "working_status" {
            self.working_ids.insert(block.id().to_string());
            return;
        }
        self.inner.append_block(block);
    }

    fn replace_block(&mut self, block: TranscriptBlock) {
        if self.replay && block.kind() == "working_status" {
            return;
        }
        self.inner.replace_block(block);
    }

    fn remove_block(&mut self, block_id: &str) {
        if self.replay && self.working_ids.remove(block_id) {
            return;
        }
        self.inner.remove_block(block_id);
    }

    fn show_notice(&mut self, text: &str) {
        if !self.replay {
            self.inner.show_notice(text);
        }
    }

    fn set_mode_by_id(&mut self, mode_id: &str, notify: bool) {
        if !self.replay {
            self.inner.set_mode_by_id(mode_id, notify);
        }
    }

    fn turn_started(&mut self) {
        if !self.replay {
            self.inner.turn_started();
        }
    }

    fn turn_finished(&mut self) {
        if !self.replay {
            self.inner.turn_finished();
        }
    }

    fn lanes_changed(&mut self) {
        // replay() repaints the lanes surface once at the end.
        if !self.replay {
            self.inner.lanes_changed();
        }
    }

    fn plan_changed(&mut self, items: &[TodoItem]) {
        // The final todo state is restored ambient state, not a side
        // effect — the plan panel reopens where the session left off.
        self.inner.plan_changed(items);
    }

    fn approval_opened(&mut self, prompt: &str, options: &[String]) {
        if !self.replay {
            self.inner.approval_opened(prompt, options);
        }
    }

    fn decision_deferred(&mut self, message: &str, decision_id: &str) {
        if !self.replay {
            self.inner.decision_deferred(message, decision_id);
        }
    }

    fn stream_opened(&mut self, block_type: &str) {
        if !self.replay {
            self.inner.stream_opened(block_type);
        }
    }

    fn stream_delta(&mut self, text: &str) {
        if !self.replay {
            self.inner.stream_delta(text);
        }
    }

    fn stream_closed(&mut self) {
        if !self.replay {
            self.inner.stream_closed();
        }
    }

    fn lane_tail_updated(&mut self, text: &str) {
        if !self.replay {
            ReducerHost::lane_tail_updated(&mut self.inner, text);
        }
    }

    fn lane_tail_cleared(&mut self) {
        if !self.replay {
            ReducerHost::lane_tail_cleared(&mut self.inner);
        }
    }
}

/// A pending root tool call awaiting its post/error (Python's
/// `turn.calls[tool_call_id]` dict entry).
#[derive(Clone, Debug)]
struct CallInfo {
    tool: String,
    input: Map<String, Value>,
    command: String,
}

/// A child tool input retained until post so successful edits can be shown.
#[derive(Clone, Debug)]
struct ChildCall {
    tool: String,
    input: Map<String, Value>,
}

/// Python `_Turn` — the running turn's mutable state.
#[derive(Debug, Default)]
struct Turn {
    turn_id: u64,
    session_id: String,
    prompt: String,
    start_ts: f64,
    mode: String,
    spec: Option<TurnSpec>,
    tokens: i64,
    working_id: Option<String>,
    plan_ids: HashMap<String, String>,
    active_step: Option<String>,
    calls: HashMap<String, CallInfo>,
    blocked: HashSet<String>,
    /// Turn hit the trust boundary and deferred a decision to the queue.
    deferred: bool,
    cancelled: bool,
    last_ts: f64,
    /// Subagents spawned this turn — pins `coordinating N agents`.
    agent_total: u32,
    /// Working-line pulse frame, advanced by the app's 1s heartbeat.
    spinner_frame: u32,
    /// Current work item for the working line (real turns): running
    /// tool / `thinking` — supervisor-facing context.
    activity: String,
    // -- rolling activity burst (DESIGN-SPEC §3) --------------------------
    /// The current burst's in-place digest ToolLine (`Read 4 files · …`);
    /// reset when the model speaks or the turn ends so the next run of
    /// tools opens a fresh digest below the answer.
    digest_id: Option<String>,
    /// Insertion-ordered digest tally (Python dict semantics).
    burst_counts: Vec<(VerbNoun, usize)>,
    burst_detail: Vec<String>,
    /// Bounded newest-last live tree beneath the pulse (single-agent).
    activity_ring: Vec<ActivityBranch>,
    /// Child tool inputs retained until post so successful edits can be shown.
    child_calls: HashMap<(String, String), ChildCall>,
    /// One in-place, expandable change summary shared by root and children.
    change_id: Option<String>,
    change_files: HashSet<String>,
    change_detail: Vec<String>,
    /// Production durable text as `(text, block_id)` candidates.
    ///
    /// Streaming orchestrators emit intermediate prose and the final
    /// response through the same `content_block:end` contract. Keep those
    /// blocks as styled, non-clickable candidates until
    /// `PromptComplete.response` identifies the one final answer.
    response_candidates: Vec<(String, String)>,
    /// Normalized answer texts already rendered for exact-once close-out.
    rendered_answers: HashSet<String>,
    /// Open Thinking block awaiting its `content_block:end` prose (issue
    /// #129). The loop-streaming runtime brackets a thinking block with
    /// start/end, so the collapsed block is minted on start and populated
    /// in place on end; reset once populated.
    thinking_id: Option<String>,
    /// Latest root-todo list this turn (ambient-progress D3) — folded into
    /// the delegate summary's `plan_final` at fan-out close (D5).
    todo_items: Vec<TodoItem>,
}

/// Adapter callable: prompt → the scripted close-out spec, if any
/// (Python `spec_lookup`).
pub type SpecLookup = Box<dyn Fn(&str) -> Option<TurnSpec>>;
/// Adapter callable: agent name → initial lane seed, if any
/// (Python `lane_seed_lookup`).
pub type LaneSeedLookup = Box<dyn Fn(&str) -> Option<LaneSeed>>;
/// Adapter callable: answer text → its evidence links
/// (Python `evidence_lookup`).
pub type EvidenceLookup = Box<dyn Fn(&str) -> Vec<EvidenceLink>>;

/// Optional constructor arguments of the Python `TranscriptReducer.__init__`.
#[derive(Default)]
pub struct ReducerOptions {
    pub spec_lookup: Option<SpecLookup>,
    pub lane_seed_lookup: Option<LaneSeedLookup>,
    pub evidence_lookup: Option<EvidenceLookup>,
    pub session_cost_start: Decimal,
    pub tail_clock: Option<Box<dyn Fn() -> f64>>,
    /// Rust-only determinism hook: pins the pricing table the internal
    /// [`CostTracker`] uses (Python snapshots the process-global table;
    /// tests inject the identical fallback table to stay isolated from
    /// other tests swapping the global).
    pub pricing: Option<std::sync::Arc<crate::kernel::cost::PricingTable>>,
}

/// UIEvent stream → block mutations on a [`ReducerHost`].
pub struct TranscriptReducer<H: ReducerHost> {
    /// Lane presentation state (per-lane live tail, focused-lane
    /// transcripts, pending delegate briefs) lives in its own unit; the
    /// turn reducer routes diverted child events onto lanes and drives it.
    /// It also owns the shared host, allocator and lane registry (see the
    /// module-level porting notes).
    lane: LaneReducer<ReplayGate<H>>,
    pub ledger: OutcomeLedger,
    spec_lookup: SpecLookup,
    lane_seed: LaneSeedLookup,
    evidence: EvidenceLookup,
    pub session_cost: Decimal,
    /// Usage records this session that could not be priced (real turns
    /// only — demo/spec turns carry scripted costs). Non-zero ⇒
    /// `session_cost` is a floor; the footer renders `~$` (never lie in
    /// the footer).
    pub unpriced_usage: i64,
    pub total_tokens: i64,
    /// /context "tools" bucket (estimated, §10).
    pub tool_tokens: i64,
    /// /context "memory" bucket (§10): the persistent cached prefix —
    /// system prompt, memory/instruction files and tool definitions —
    /// sized from provider cache traffic (largest cache_read+cache_write
    /// seen; reads cover the previously written prefix).
    pub memory_tokens: i64,
    cost: CostTracker,
    turn: Option<Turn>,
    /// User messages already in the live context before this session's
    /// ledger started counting (resume history). Foundation's fork `turn`
    /// is 1-indexed over ALL user messages in the context — including
    /// persistent steering/decision injections — so checkpoint turn ids
    /// must offset past the restored history (spec §9).
    pub turn_base: u64,
    // -- delegate fan-out summary (ambient-progress D5) -----------------
    // Reducer-held (not turn-held) so completions landing after turn end
    // still update the block, mirroring the old tree-line lifetime.
    delegate_summary_id: Option<String>,
    delegate_rows: HashMap<String, DelegateRow>,
    delegate_order: Vec<String>,
    fanout_start_ts: f64,
    fanout_duration_s: f64,
    delegate_plan_final: Option<Vec<TodoItem>>,
}

impl<H: ReducerHost> TranscriptReducer<H> {
    pub fn new(
        host: H,
        allocator: BlockIdAllocator,
        ledger: OutcomeLedger,
        lanes: LaneRegistry,
    ) -> Self {
        Self::with_options(host, allocator, ledger, lanes, ReducerOptions::default())
    }

    pub fn with_options(
        host: H,
        allocator: BlockIdAllocator,
        ledger: OutcomeLedger,
        lanes: LaneRegistry,
        options: ReducerOptions,
    ) -> Self {
        let gate = ReplayGate::new(host);
        let lane = match options.tail_clock {
            Some(clock) => LaneReducer::with_clock(gate, allocator, lanes, clock),
            None => LaneReducer::new(gate, allocator, lanes),
        };
        TranscriptReducer {
            lane,
            ledger,
            spec_lookup: options.spec_lookup.unwrap_or_else(|| Box::new(|_| None)),
            lane_seed: options
                .lane_seed_lookup
                .unwrap_or_else(|| Box::new(|_| None)),
            evidence: options
                .evidence_lookup
                .unwrap_or_else(|| Box::new(|_| Vec::new())),
            session_cost: options.session_cost_start,
            unpriced_usage: 0,
            total_tokens: 0,
            tool_tokens: 0,
            memory_tokens: 0,
            cost: match options.pricing {
                Some(pricing) => CostTracker::with_pricing(pricing),
                None => CostTracker::new(),
            },
            turn: None,
            turn_base: 0,
            delegate_summary_id: None,
            delegate_rows: HashMap::new(),
            delegate_order: Vec::new(),
            fanout_start_ts: 0.0,
            fanout_duration_s: 0.0,
            delegate_plan_final: None,
        }
    }

    /// The app host (Python's `self._host`, unwrapped from the gate).
    pub fn host(&self) -> &H {
        &self.lane.host().inner
    }

    pub fn host_mut(&mut self) -> &mut H {
        &mut self.lane.host_mut().inner
    }

    /// The shared lane registry (Python's public `self.lanes`).
    pub fn lanes(&self) -> &LaneRegistry {
        &self.lane.lanes
    }

    pub fn lanes_mut(&mut self) -> &mut LaneRegistry {
        &mut self.lane.lanes
    }

    // -- public state -------------------------------------------------------

    pub fn running(&self) -> bool {
        self.turn.is_some()
    }

    /// Committed session spend plus usage received in the active turn.
    pub fn live_session_cost(&self) -> Decimal {
        if self.turn.as_ref().is_some_and(|turn| turn.spec.is_some()) {
            return self.session_cost;
        }
        self.session_cost + self.cost.turn().cost
    }

    /// Whether the live total is only a floor because usage is unpriced.
    pub fn live_cost_estimated(&self) -> bool {
        if self.turn.as_ref().is_some_and(|turn| turn.spec.is_some()) {
            return self.unpriced_usage > 0;
        }
        self.unpriced_usage > 0 || self.cost.turn().unpriced > 0
    }

    /// The title bar's `<state>` fragment (DESIGN-SPEC §2).
    pub fn title_state(&self) -> String {
        let Some(turn) = self.turn.as_ref() else {
            return "ready".to_string();
        };
        if turn.agent_total > 0 {
            // Pinned for the whole multi-agent turn (mockup sets the
            // coordinating title once and never decrements it).
            let noun = if turn.agent_total == 1 { "agent" } else { "agents" };
            return format!("✳ coordinating {} {noun}", turn.agent_total);
        }
        if let Some(step) = turn.active_step.as_ref() {
            return step.to_lowercase();
        }
        if turn.mode == "plan" {
            return "planning".to_string();
        }
        if turn.mode == "brainstorm" {
            return "brainstorming".to_string();
        }
        // Mockup: the title only changes at step activation — before the
        // first step (and on step-less turns) it keeps the idle text.
        "ready".to_string()
    }

    // -- resume replay (DESIGN-SPEC §3/§11) -----------------------------------

    /// Rebuild the transcript from a resumed session's stored events.
    ///
    /// The session store persists every normalized UIEvent; feeding them
    /// back through the same dispatch rebuilds exactly what rendered live —
    /// tool digests, ⊘ blocked lines, delegate summaries, lane focus
    /// transcripts, plan state, turn rules with real telemetry — instead of
    /// the prose-only fallback. Side effects are suppressed via the
    /// [`ReplayGate`] + [`REPLAY_SKIPPED_KINDS`].
    ///
    /// `turn_base`/`session_cost` are the transcript-derived turn count and
    /// the kernel-restored cost baseline; both stay the post-replay
    /// authorities (see the reconciliation below). Returns `false` — with
    /// no state touched — when the log holds no replayable turn
    /// (absent/foreign log), so the caller can fall back to prose.
    pub fn replay(
        &mut self,
        events: &[ev::UIEvent],
        turn_base: u64,
        session_cost: Decimal,
    ) -> bool {
        if !events
            .iter()
            .any(|event| matches!(event, ev::UIEvent::PromptSubmit(_)))
        {
            return false;
        }
        self.lane.host_mut().begin_replay();
        // Replayed turns re-derive their own 1-indexed context positions
        // from zero, exactly as they did live (ContextInjected advances
        // included) — the LAST replayed checkpoint's turn_id must land on
        // *turn_base*; seeding it here as well would double the offset.
        self.turn_base = 0;
        self.session_cost = Decimal::ZERO;
        for event in events {
            if REPLAY_SKIPPED_KINDS.contains(&event.kind()) {
                continue;
            }
            self.handle(event);
        }
        if self.turn.is_some() {
            // The log ended mid-turn (crash/kill before close-out): settle
            // it as interrupted — the same durable shape a live Esc leaves.
            // ts stays in the log's clock domain.
            let (session_id, last_ts) = {
                let turn = self.turn.as_mut().expect("just checked");
                turn.cancelled = true;
                (turn.session_id.clone(), turn.last_ts)
            };
            self.handle(&ev::UIEvent::PromptComplete(ev::PromptComplete {
                session_id,
                ts: last_ts,
                ..ev::PromptComplete::default()
            }));
        }
        self.lane.host_mut().end_replay();
        // Lanes the log never completed (same crash case) must not keep
        // ticking against the wall clock after resume.
        for record in self.lane.lanes.lanes() {
            if record.lane.state != LaneStateName::Done {
                self.lane.lanes.complete(&record.session_id, "interrupted");
            }
        }
        let mismatch = self
            .ledger
            .checkpoints()
            .last()
            .map(|checkpoint| checkpoint.turn_id)
            != Some(turn_base);
        if mismatch {
            // Degrade explicitly: the event log disagrees with the stored
            // transcript (truncated log, or post-rewind ghost turns —
            // events.jsonl is append-only while a confirmed fork trims the
            // context). The replayed blocks stay as scrollback, but their
            // checkpoints would fork the live context at the wrong turns;
            // reset the ledger so new checkpoints fall back to the
            // transcript-derived turn_base (existing resume math, §9).
            self.ledger.clear();
        }
        self.turn_base = turn_base;
        // The kernel's restore_session_cost stays the single authority for
        // the resumed cost baseline (it carries the exactly-once repair for
        // logs older builds wrote) — replay's own accumulation stamped
        // self-consistent checkpoint cost_at values and is reconciled to
        // that authority here, never added on top of it.
        self.session_cost = session_cost;
        self.lane.host_mut().lanes_changed();
        true
    }

    // -- dispatch -------------------------------------------------------------

    /// Apply one normalized event; unknown kinds are ignored.
    pub fn handle(&mut self, event: &ev::UIEvent) {
        if self.is_foreign_turn_event(event) {
            self.track_child_activity(event);
            return;
        }
        if let Some(turn) = self.turn.as_mut() {
            // The envelope always stamps ts — no falsy-zero guard (the
            // demo's virtual clock legitimately starts at 0.0).
            turn.last_ts = event.ts();
        }
        match event {
            ev::UIEvent::SessionStart(e)
                if e.parent_id.as_deref().is_some_and(|p| !p.is_empty()) =>
            {
                if self
                    .lane
                    .lanes
                    .bind_session(&e.session_id, e.parent_id.as_deref())
                    .is_some()
                {
                    self.lane.host_mut().lanes_changed();
                }
            }
            ev::UIEvent::PromptSubmit(e) => self.start_turn(e),
            ev::UIEvent::StreamBlockStart(e) => {
                self.lane.root_streaming = true;
                self.lane.clear_tail(None);
                self.lane.host_mut().stream_opened(&e.block_type);
                if e.block_type == "thinking" {
                    self.set_activity("thinking");
                }
            }
            ev::UIEvent::StreamBlockDelta(e) => {
                self.lane.host_mut().stream_delta(&e.text);
            }
            ev::UIEvent::StreamBlockEnd(_) => {
                self.lane.root_streaming = false;
                self.lane.host_mut().stream_closed();
            }
            ev::UIEvent::StreamAborted(e) => {
                self.lane.root_streaming = false;
                self.lane.host_mut().stream_closed();
                let notice = rstrip_sep(&format!("stream aborted · {}", e.error_message));
                self.lane.host_mut().show_notice(&notice);
            }
            ev::UIEvent::ContentBlockStart(e) => {
                if e.block_type == "thinking" {
                    self.thinking_started();
                }
            }
            ev::UIEvent::ContentBlockEnd(e) => {
                if e.block_type == "thinking" {
                    self.thinking_recorded(e);
                } else {
                    self.durable_text(e);
                }
            }
            ev::UIEvent::ToolPre(e) => self.tool_pre(e),
            ev::UIEvent::ToolPost(e) => self.tool_post(e),
            ev::UIEvent::ToolError(e) => self.tool_error(e),
            ev::UIEvent::ProviderResponseUsage(e) => self.usage(e),
            ev::UIEvent::ProviderNotice(e) => {
                let notice = rstrip_sep(&format!(
                    "provider {} · {}",
                    e.notice.as_str(),
                    e.message
                ));
                self.lane.host_mut().show_notice(&notice);
            }
            ev::UIEvent::ApprovalRequired(e) => {
                self.lane.host_mut().approval_opened(&e.prompt, &e.options);
            }
            ev::UIEvent::ApprovalDenied(e) => self.approval_denied(e),
            ev::UIEvent::Notification(e) => self.notification(e),
            ev::UIEvent::AgentSpawned(e) => self.agent_spawned(e),
            ev::UIEvent::AgentCompleted(e) => self.agent_completed(e),
            ev::UIEvent::OrchestratorComplete(e) => {
                if e.status == ev::OrchestratorStatus::Cancelled {
                    if let Some(turn) = self.turn.as_mut() {
                        turn.cancelled = true;
                    }
                }
            }
            ev::UIEvent::CancelCompleted(_) => {
                if let Some(turn) = self.turn.as_mut() {
                    turn.cancelled = true;
                }
            }
            ev::UIEvent::ContextInjected(_) => self.context_injected(),
            ev::UIEvent::ContextCompacted(e) => self.context_compacted(e),
            ev::UIEvent::PromptComplete(e) => self.finish_turn(e),
            _ => {}
        }
    }

    /// Keep child execution traffic out of the root transcript.
    ///
    /// The runtime deliberately attaches the queue bridge to child sessions
    /// so their usage can feed lane telemetry. Their streams, prose, tools,
    /// and orchestrator close-outs must not mutate the root turn, though.
    /// Empty session ids remain accepted for compatibility with synthetic
    /// events and older tests.
    fn is_foreign_turn_event(&self, event: &ev::UIEvent) -> bool {
        let Some(turn) = self.turn.as_ref() else {
            return false;
        };
        if turn.session_id.is_empty()
            || event.session_id().is_empty()
            || event.session_id() == turn.session_id
        {
            return false;
        }
        matches!(
            event,
            ev::UIEvent::StreamBlockStart(_)
                | ev::UIEvent::StreamBlockDelta(_)
                | ev::UIEvent::StreamBlockEnd(_)
                | ev::UIEvent::StreamAborted(_)
                | ev::UIEvent::ContentBlockStart(_)
                | ev::UIEvent::ContentBlockEnd(_)
                | ev::UIEvent::ToolPre(_)
                | ev::UIEvent::ToolPost(_)
                | ev::UIEvent::ToolError(_)
                | ev::UIEvent::OrchestratorComplete(_)
        )
    }

    /// Project child execution into one compact lane/tree status line.
    ///
    /// Child prose and tools stay out of the parent transcript, but their
    /// high-signal lifecycle events make the existing lane and agent-tree
    /// labels useful as an in-place activity ticker.
    fn track_child_activity(&mut self, event: &ev::UIEvent) {
        let Some(record) = self.lane.lanes.get(event.session_id()) else {
            return;
        };
        if record.lane.state == LaneStateName::Done {
            return;
        }
        let mut state = LaneStateName::Running;
        let activity: Option<String> = match event {
            ev::UIEvent::ToolPre(e) => {
                if let Some(turn) = self.turn.as_mut() {
                    turn.child_calls.insert(
                        (record.session_id.clone(), e.tool_call_id.clone()),
                        ChildCall {
                            tool: e.tool_name.clone(),
                            input: e.tool_input.clone(),
                        },
                    );
                }
                state = LaneStateName::Working;
                Some(live_op_label(&e.tool_name, &e.tool_input))
            }
            ev::UIEvent::ToolPost(e) => {
                let call = self.turn.as_mut().and_then(|turn| {
                    turn.child_calls
                        .remove(&(record.session_id.clone(), e.tool_call_id.clone()))
                });
                let (tool, tool_input) = match call {
                    Some(call) => (call.tool, call.input),
                    None => (e.tool_name.clone(), e.tool_input.clone()),
                };
                let status = get_str(&e.result, "status").to_lowercase();
                let success_false = matches!(e.result.get("success"), Some(Value::Bool(false)));
                let ok = !success_false
                    && !["denied", "error", "failed"].contains(&status.as_str());
                if ok && self.turn.is_some() {
                    self.record_change(&record.lane.name, &tool, &tool_input);
                }
                let id = self.lane.ids_mut().next_id();
                let tool_call_ids = if e.tool_call_id.is_empty() {
                    Vec::new()
                } else {
                    vec![e.tool_call_id.clone()]
                };
                self.lane.append_block(
                    &record,
                    ToolLine {
                        status: if ok {
                            ToolLineStatus::Completed
                        } else {
                            ToolLineStatus::Failed
                        },
                        tool_call_ids,
                        ..ToolLine::new(id, live_op_label(&tool, &tool_input))
                    }
                    .into(),
                );
                Some("reviewing tool result".to_string())
            }
            ev::UIEvent::ToolError(e) => {
                let id = self.lane.ids_mut().next_id();
                let summary = rstrip_sep(&format!(
                    "{} · {}",
                    e.tool_name.replace('_', " "),
                    e.error_message
                ));
                let tool_call_ids = if e.tool_call_id.is_empty() {
                    Vec::new()
                } else {
                    vec![e.tool_call_id.clone()]
                };
                self.lane.append_block(
                    &record,
                    ToolLine {
                        status: ToolLineStatus::Failed,
                        tool_call_ids,
                        ..ToolLine::new(id, summary)
                    }
                    .into(),
                );
                Some(format!(
                    "recovering from {} error",
                    e.tool_name.replace('_', " ")
                ))
            }
            ev::UIEvent::StreamBlockStart(e) => Some(
                if e.block_type == "thinking" {
                    "thinking"
                } else {
                    "writing response"
                }
                .to_string(),
            ),
            ev::UIEvent::StreamBlockDelta(e) => {
                let activity = if e.block_type == "thinking" {
                    "thinking"
                } else {
                    "writing response"
                };
                self.lane.tail_delta(&record, e);
                Some(activity.to_string())
            }
            ev::UIEvent::StreamBlockEnd(_) => Some("reviewing response".to_string()),
            ev::UIEvent::ContentBlockEnd(e) => {
                if e.block_type == "text" {
                    let text = get_str(&e.block, "text");
                    if !text.is_empty() {
                        let id = self.lane.ids_mut().next_id();
                        self.lane.append_block(
                            &record,
                            Answer {
                                clickable: false,
                                ..Answer::new(id, answer_spans(&text))
                            }
                            .into(),
                        );
                    }
                    Some("reporting findings".to_string())
                } else {
                    Some("thinking".to_string())
                }
            }
            ev::UIEvent::OrchestratorComplete(_) => Some("wrapping up".to_string()),
            _ => return,
        };
        let Some(activity) = activity else { return };
        if record.lane.activity == activity && record.lane.state == state {
            return;
        }
        if self
            .lane
            .lanes
            .update(
                event.session_id(),
                LaneUpdate {
                    activity: Some(activity),
                    state: Some(state),
                    ..LaneUpdate::default()
                },
            )
            .is_none()
        {
            return;
        }
        self.lane.host_mut().lanes_changed();
    }

    // -- agent lanes: focus transcripts + live tail (LaneReducer) ------------

    /// A lane's accumulated focus transcript, by session id or name.
    ///
    /// The real-runtime counterpart of the demo adapter's `lane_blocks` —
    /// `None` (not `Some(vec![])`) when nothing is known so the caller's
    /// no-transcript notice stays meaningful. Owned by the LaneReducer;
    /// kept here as the reducer's public lane surface.
    pub fn lane_transcript(&self, key: &str) -> Option<Vec<TranscriptBlock>> {
        self.lane.transcript(key)
    }

    /// Paint the focused lane's buffered tail right now (ctrl+o).
    ///
    /// Cycling the pin must not wait for the new lane's next delta —
    /// otherwise the tail keeps showing the previous lane's text. Owned by
    /// the LaneReducer; kept here as the reducer's public lane surface.
    pub fn repaint_lane_tail(&mut self) {
        self.lane.repaint_tail();
    }

    /// Roll a successful native file write into one expandable diff row.
    fn record_change(&mut self, actor: &str, tool: &str, tool_input: &Map<String, Value>) {
        let (paths, preview) = change_preview(tool, tool_input);
        if paths.is_empty() || preview.is_empty() {
            return;
        }
        let Some(turn) = self.turn.as_mut() else {
            return;
        };
        for path in &paths {
            turn.change_files.insert(path.clone());
        }
        let path_label = paths.join(", ");
        let mut detail = vec![format!(
            "{actor} · {} · {path_label}",
            tool.replace('_', " ")
        )];
        detail.extend(preview);
        let remaining = CHANGE_DETAIL_LINES.saturating_sub(turn.change_detail.len());
        if remaining > 0 {
            turn.change_detail.extend(detail.into_iter().take(remaining));
        }
        let count = turn.change_files.len();
        let body = turn.change_detail.clone();
        let change_id = turn.change_id.clone();
        let summary = format!("Changed {count} file{}", if count != 1 { "s" } else { "" });
        match change_id {
            Some(id) => {
                self.lane.host_mut().replace_block(
                    ToolLine {
                        body,
                        status: ToolLineStatus::Completed,
                        body_style: ToolLineBodyStyle::Diff,
                        ..ToolLine::new(id, summary)
                    }
                    .into(),
                );
            }
            None => {
                let id = self.lane.ids_mut().next_id();
                if let Some(turn) = self.turn.as_mut() {
                    turn.change_id = Some(id.clone());
                }
                self.append_content(
                    ToolLine {
                        body,
                        status: ToolLineStatus::Completed,
                        body_style: ToolLineBodyStyle::Diff,
                        ..ToolLine::new(id, summary)
                    }
                    .into(),
                );
            }
        }
    }

    // -- turn lifecycle -------------------------------------------------------

    fn start_turn(&mut self, event: &ev::PromptSubmit) {
        // Turn id = 1-indexed user-message position in the live context:
        // resume history, every ledger-recorded turn AND any persistent
        // mid-turn context injections (steers / deferred-decision answers —
        // each is one more user-role message foundation's fork counts).
        // Past injections are baked into the last checkpoint's turn_id, so
        // deriving from it (instead of a monotonic counter) both carries
        // the injection offset forward and rewinds it automatically when a
        // confirmed fork trims the ledger (spec §9).
        let last_turn_id = self
            .ledger
            .checkpoints()
            .last()
            .map(|checkpoint| checkpoint.turn_id)
            .unwrap_or(self.turn_base);
        // The event carries the posture the turn was submitted under
        // (stamped into ui-events.jsonl), so resume replay stamps the user
        // line's `[mode]` badge with the HISTORICAL mode rather than the
        // current live one. Legacy logs (no mode) fall back to the live
        // posture — the pre-stamp behavior.
        let mode = if event.mode.is_empty() {
            self.lane.host().mode_id()
        } else {
            event.mode.clone()
        };
        let spec = (self.spec_lookup)(&event.prompt);
        let spec_is_none = spec.is_none();
        let turn = Turn {
            turn_id: last_turn_id + 1,
            session_id: event.session_id.clone(),
            prompt: event.prompt.clone(),
            start_ts: event.ts,
            last_ts: event.ts,
            mode: mode.clone(),
            spec,
            ..Turn::default()
        };
        self.turn = Some(turn);
        self.cost.start_turn();
        self.delegate_summary_id = None;
        self.delegate_rows = HashMap::new();
        self.delegate_order = Vec::new();
        self.fanout_start_ts = 0.0;
        self.fanout_duration_s = 0.0;
        self.delegate_plan_final = None;
        let user_id = self.lane.ids_mut().next_id();
        self.lane.host_mut().append_block(
            UserLine {
                mode,
                ..UserLine::new(user_id, event.prompt.clone())
            }
            .into(),
        );
        if spec_is_none {
            // Real turn: the working line mounts IMMEDIATELY — pre-model
            // hook work and provider latency can run for seconds before the
            // first content block, and the supervisor needs a pulse the
            // whole time. (Scripted demo turns keep the mockup's lazy mount
            // under the first content block.)
            let working_id = self.lane.ids_mut().next_id();
            let turn = self.turn.as_mut().expect("turn just started");
            turn.working_id = Some(working_id);
            let block = Self::working_block(turn);
            self.lane.host_mut().append_block(block.into());
        }
        // The working line mounts lazily under the turn's first content
        // block (mockup runTurn: after the plan header + items;
        // runAgentsTurn: after the fan-out narration) — see append_content.
        self.lane.host_mut().turn_started();
    }

    /// One persistent user-role message entered the context mid-turn.
    ///
    /// A consumed steer / answered deferred decisions injection is a real
    /// user message in the live transcript, and foundation's fork slicing
    /// counts EVERY user-role message as a turn boundary. Advance the
    /// running turn's id so its checkpoint addresses the LAST user message
    /// of the turn — forking there keeps the injection and the steered
    /// answer (spec §9).
    fn context_injected(&mut self) {
        if let Some(turn) = self.turn.as_mut() {
            turn.turn_id += 1;
        } else {
            // Defensive: an injection outside a running turn still shifts
            // every later user-message position.
            self.turn_base += 1;
        }
    }

    fn finish_turn(&mut self, event: &ev::PromptComplete) {
        if self.turn.is_none() {
            return;
        }
        self.lane.clear_tail(None);
        self.lane.root_streaming = false;
        // A cancelled turn strands running delegates: settle them as ⊘ so
        // the durable summary never claims work that was interrupted
        // (edge-case table, ambient-progress design).
        let cancelled = self.turn.as_ref().expect("checked above").cancelled;
        if cancelled
            && self
                .delegate_rows
                .values()
                .any(|row| row.state == DelegateState::Running)
        {
            let last_ts = self.turn.as_ref().expect("checked above").last_ts;
            for row in self.delegate_rows.values_mut() {
                if row.state == DelegateState::Running {
                    row.state = DelegateState::Cancelled;
                    row.elapsed_s = (last_ts - row.spawned_ts).max(0.0);
                }
            }
            self.fanout_duration_s = (last_ts - self.fanout_start_ts).max(0.0);
            self.render_delegate_summary();
        }
        // Re-resolve at close: mid-turn events (e.g. a denied approval)
        // may have changed the adapter's close-out spec for this prompt.
        let prompt = self.turn.as_ref().expect("checked above").prompt.clone();
        let spec = (self.spec_lookup)(&prompt)
            .or_else(|| self.turn.as_ref().expect("checked above").spec.clone());
        if spec.is_none() {
            self.finalize_response(&event.response);
        }
        if let Some(working_id) = self
            .turn
            .as_ref()
            .expect("checked above")
            .working_id
            .clone()
        {
            self.lane.host_mut().remove_block(&working_id);
        }
        // Tool calls that never got a post/error (a policy-denied tool
        // fires no tool:post; an interrupted turn abandons in-flight ops)
        // just close out the burst — the digest already reflects whatever
        // completed, and the ephemeral live tree vanished with the pulse.
        if let Some(turn) = self.turn.as_mut() {
            turn.calls.clear();
        }
        self.flush_burst();
        let usage = self.cost.end_turn();
        let (mode, tokens, start_ts, deferred, agent_total, turn_id) = {
            let turn = self.turn.as_ref().expect("checked above");
            (
                turn.mode.clone(),
                turn.tokens,
                turn.start_ts,
                turn.deferred,
                turn.agent_total,
                turn.turn_id,
            )
        };
        let (telemetry, shipped, kind, label) = match spec.as_ref() {
            Some(spec) => {
                let telemetry = TurnTelemetry {
                    secs: spec.duration_ms as f64 / 1000.0,
                    tokens_down: spec.tokens,
                    cached_pct: spec.cached_pct,
                    cost: spec.cost,
                    estimated: false,
                };
                let shipped = spec.shipped && !cancelled;
                let kind = if cancelled {
                    OutcomeKind::Interrupted
                } else if shipped {
                    OutcomeKind::Shipped
                } else if spec.outcome.contains("plan ready") {
                    OutcomeKind::PlanReady
                } else {
                    OutcomeKind::Answer
                };
                (telemetry, shipped, kind, spec.checkpoint_label.clone())
            }
            None => {
                // Real-runtime close-out: per-turn cost and cache % come
                // from the provider usage recorded by the CostTracker (spec
                // §11); the yield (files/diffstat/tests ✔) rides on the
                // runtime's synthesized PromptComplete (git snapshot delta
                // — spec §3).
                self.unpriced_usage += usage.unpriced;
                let telemetry = TurnTelemetry {
                    secs: (event.ts - start_ts).max(0.0), // one clock domain, no fallback
                    tokens_down: tokens.max(0) as u64,
                    cached_pct: usage.cached_pct().map(|pct| pct.clamp(0, 255) as u8),
                    cost: usage.cost,
                    estimated: usage.unpriced > 0,
                };
                let shipped = event.files_changed != 0 && !cancelled;
                let kind = if cancelled {
                    OutcomeKind::Interrupted
                } else if shipped {
                    OutcomeKind::Shipped
                } else if mode == "plan" {
                    OutcomeKind::PlanReady
                } else {
                    OutcomeKind::Answer
                };
                (telemetry, shipped, kind, char_prefix(&prompt, 40))
            }
        };
        let outcome = if spec.is_none() {
            TurnOutcome {
                kind,
                files_changed: if shipped {
                    event.files_changed.max(0) as u64
                } else {
                    0
                },
                diffstat: if shipped {
                    event.diffstat.clone()
                } else {
                    String::new()
                },
                tests_ok: if shipped { event.tests_ok } else { None },
            }
        } else {
            TurnOutcome::new(kind)
        };
        // Session spend is additive per turn (mockup `this.cost += turnCost`);
        // checkpoint $ always equals the footer $ at rule time (mockup
        // `cp.cost = this.cost`) — one session cost basis everywhere.
        self.session_cost += telemetry.cost;
        let rule_label = match spec.as_ref() {
            Some(spec) => spec.rule_label.clone(),
            None => {
                let outcome_text = outcome.outcome_label();
                // `· interrupted`/`· plan ready` carry their own separator.
                let joiner = if outcome_text.starts_with('·') { " " } else { " · " };
                format!("{}{joiner}{outcome_text}", telemetry.label())
            }
        };
        let checkpoint_id = self
            .ledger
            .record_turn(
                telemetry,
                outcome,
                turn_id,
                turn_id,
                &label,
                Some(self.session_cost),
            )
            .checkpoint
            .id
            .clone();
        if spec.is_none() && cancelled {
            // Real interrupted close-out: the italic recap the demo scripts
            // as its own recap event (spec §11 — `Interrupted. Goal:
            // <goal>. Context saved; resume or restate direction.`).
            let recap = self.recap_line(&format!(
                "Interrupted. Goal: {}. Context saved; resume or restate direction.",
                char_prefix(&prompt, 40)
            ));
            self.lane.host_mut().append_block(recap.into());
        }
        let rule_id = self.lane.ids_mut().next_id();
        self.lane.host_mut().append_block(
            TurnRule {
                shipped,
                ..TurnRule::new(rule_id, checkpoint_id, rule_label)
            }
            .into(),
        );
        self.turn = None;
        self.lane.host_mut().turn_finished();
        if deferred {
            // Mockup runTurn close-out `if (!blocked) this.showNotice(...)`:
            // a turn that deferred a decision to the queue shows NO end
            // notice — even when interrupted — so the earlier `decision
            // deferred to queue · run continues` notice stays visible
            // (spec §11).
        } else if cancelled {
            // Mockup runTurn close-out: the interrupted turn's end notice
            // fires only once the turn actually stops (spec §11).
            self.lane
                .host_mut()
                .show_notice("turn interrupted · context saved");
        } else if spec.is_none() {
            // Real runtime: the demo script carries its own end-notice
            // Notification events; here the reducer synthesizes spec §11's
            // `agents N done` success notice from the turn's fan-out.
            let notice = format!("agents {} done", agent_total.max(1));
            self.lane.host_mut().show_notice(&notice);
        }
    }

    /// Append turn content, keeping the working line directly below the
    /// turn's FIRST content block (mockup runTurn L313-315: plan header +
    /// items, then status; runAgentsTurn L466-467: fan-out narration, then
    /// status) — later content accumulates below the pinned status line.
    fn append_content(&mut self, block: TranscriptBlock) {
        self.lane.host_mut().append_block(block);
        let Some(turn) = self.turn.as_mut() else {
            return;
        };
        if let Some(working_id) = turn.working_id.clone() {
            if turn.spec.is_none() {
                // Real turn: keep the pulse at the BOTTOM, riding under the
                // newest content next to the composer. The re-append must
                // mint a FRESH id: the Textual host pruned asynchronously,
                // so remove+append under the same id in one synchronous
                // stretch mounted a duplicate widget id (found by resume
                // replay; live turns logged "reducer failed on tool_post"
                // and lost the pulse).
                self.lane.host_mut().remove_block(&working_id);
                turn.working_id = Some(self.lane.ids_mut().next_id());
                let refreshed = Self::working_block(turn);
                self.lane.host_mut().append_block(refreshed.into());
            }
            return;
        }
        turn.working_id = Some(self.lane.ids_mut().next_id());
        let mounted = Self::working_block(turn);
        self.lane.host_mut().append_block(mounted.into());
    }

    // -- assistant text (durable Channel B) -------------------------------------

    /// Open a collapsed Thinking block where the model began reasoning.
    ///
    /// The loop-streaming runtime carries no token deltas, so the block
    /// opens empty here and its prose lands via [`Self::thinking_recorded`]
    /// on the matching `content_block:end`. The lane/working label stays
    /// task-level (`thinking`) — reasoning prose lives only in this durable
    /// transcript block, never in the lanes pane (issue #129).
    fn thinking_started(&mut self) {
        if self.turn.is_none() {
            return;
        }
        self.set_activity("thinking");
        let id = self.lane.ids_mut().next_id();
        self.append_content(Thinking::new(id.clone()).into());
        if let Some(turn) = self.turn.as_mut() {
            turn.thinking_id = Some(id);
        }
    }

    /// Populate a Thinking block from its `content_block:end` payload.
    ///
    /// Reads `block["thinking"]` (core's ThinkingBlock field) then falls
    /// back to `block["text"]`. Degrades honestly on withheld reasoning:
    /// core's `ThinkingBlock.visibility` (LLM_ONLY/USER_ONLY) can strip the
    /// prose from UI-facing events, so the text may be empty — the block
    /// stays and renders "content withheld by provider" rather than
    /// vanishing. Replaces the open block in place (no working-line
    /// reflow); appends defensively if no start was seen (non-streaming
    /// provider).
    fn thinking_recorded(&mut self, event: &ev::ContentBlockEnd) {
        if self.turn.is_none() {
            return;
        }
        let text = {
            let thinking = get_str(&event.block, "thinking");
            if thinking.is_empty() {
                get_str(&event.block, "text")
            } else {
                thinking
            }
        };
        let thinking_id = self
            .turn
            .as_ref()
            .expect("checked above")
            .thinking_id
            .clone();
        match thinking_id {
            Some(id) => {
                self.lane.host_mut().replace_block(
                    Thinking {
                        text,
                        ..Thinking::new(id)
                    }
                    .into(),
                );
            }
            None => {
                let id = self.lane.ids_mut().next_id();
                self.append_content(
                    Thinking {
                        text,
                        ..Thinking::new(id)
                    }
                    .into(),
                );
            }
        }
        if let Some(turn) = self.turn.as_mut() {
            turn.thinking_id = None;
        }
    }

    fn durable_text(&mut self, event: &ev::ContentBlockEnd) {
        if event.block_type != "text" {
            return;
        }
        let text = get_str(&event.block, "text");
        if text.is_empty() {
            return;
        }
        // The model spoke: freeze the preceding tool burst into its digest
        // above this text, and start a fresh burst below it (spec §3).
        self.flush_burst();
        let explicit_role = event.block.get("demo_role").map(value_text);
        let Some(role) = explicit_role else {
            // Real-runtime text is provisional. The orchestrator can speak
            // before tools and again at the end; PromptComplete.response is
            // the authoritative final-answer identity. Commit the same
            // formatted shape the streaming tail just showed. It remains
            // non-clickable/provisional until PromptComplete adds evidence
            // and authoritatively identifies the final response.
            let id = self.lane.ids_mut().next_id();
            self.append_content(
                Answer {
                    clickable: false,
                    ..Answer::new(id.clone(), answer_spans(&text))
                }
                .into(),
            );
            if let Some(turn) = self.turn.as_mut() {
                turn.response_candidates
                    .push((text.trim().to_string(), id));
            }
            return;
        };

        if role == "narration" {
            let id = self.lane.ids_mut().next_id();
            self.append_content(Narration::new(id, text).into());
        } else if role == "idea" {
            let (number, body) = match idea_re().captures(&text) {
                Some(caps) => (
                    caps.get(1)
                        .and_then(|m| m.as_str().parse::<i64>().ok())
                        .unwrap_or(0),
                    caps.get(2).map(|m| m.as_str().to_string()).unwrap_or_default(),
                ),
                None => (0, text.clone()),
            };
            let id = self.lane.ids_mut().next_id();
            self.append_content(
                BrainstormIdea {
                    number: number as u32,
                    ..BrainstormIdea::new(id, body)
                }
                .into(),
            );
        } else if role == "recap" {
            self.append_recap(&text);
        } else {
            let links = (self.evidence)(&text);
            let id = self.lane.ids_mut().next_id();
            self.append_content(
                Answer {
                    evidence_refs: links,
                    ..Answer::new(id, answer_spans(&text))
                }
                .into(),
            );
            if let Some(turn) = self.turn.as_mut() {
                turn.rendered_answers.insert(text.trim().to_string());
            }
        }
    }

    /// Promote or append the real turn's one authoritative answer.
    fn finalize_response(&mut self, response: &str) {
        let text = response.trim().to_string();
        if text.is_empty() {
            return;
        }
        let Some(turn) = self.turn.as_ref() else {
            return;
        };
        if turn.rendered_answers.contains(&text) {
            return;
        }

        self.flush_burst();
        let links = (self.evidence)(&text);
        let candidates = self
            .turn
            .as_ref()
            .expect("checked above")
            .response_candidates
            .clone();
        for (candidate_text, block_id) in candidates.iter().rev() {
            if candidate_text != &text {
                continue;
            }
            self.lane.host_mut().replace_block(
                Answer {
                    evidence_refs: links,
                    ..Answer::new(block_id.clone(), answer_spans(response))
                }
                .into(),
            );
            if let Some(turn) = self.turn.as_mut() {
                turn.rendered_answers.insert(text);
            }
            return;
        }

        // This fallback runs only during close-out. Appending through
        // append_content would move/re-mount the working pulse immediately
        // before finish_turn removes it, creating an avoidable host race
        // for non-streaming providers whose answer exists only here.
        let id = self.lane.ids_mut().next_id();
        self.lane.host_mut().append_block(
            Answer {
                evidence_refs: links,
                ..Answer::new(id, answer_spans(response))
            }
            .into(),
        );
        if let Some(turn) = self.turn.as_mut() {
            turn.rendered_answers.insert(text);
        }
    }

    fn append_recap(&mut self, text: &str) {
        if let Some(caps) = recap_re().captures(text) {
            let id = self.lane.ids_mut().next_id();
            self.append_content(Recap::new(id, &caps["goal"], &caps["next"]).into());
            return;
        }
        // Non Goal/Next recaps render as the same ✳ italic-dim line shape;
        // the mockup creates them with click: null (not evidence targets).
        let line = self.recap_line(text);
        self.append_content(line.into());
    }

    /// The ✳ italic-dim recap line shape (demo and real turns alike).
    fn recap_line(&mut self, text: &str) -> Answer {
        let id = self.lane.ids_mut().next_id();
        Answer {
            clickable: false,
            ..Answer::new(
                id,
                vec![
                    Segment {
                        style_token: StyleToken::Dimmer,
                        ..Segment::new("✳ ")
                    },
                    Segment {
                        style_token: StyleToken::Dim,
                        italic: true,
                        ..Segment::new(text)
                    },
                ],
            )
        }
    }

    // -- tools -------------------------------------------------------------------

    fn tool_pre(&mut self, event: &ev::ToolPre) {
        if event.tool_name == "update_plan" {
            self.update_plan(event);
            return;
        }
        if event.tool_name == "todo" {
            self.update_todo(event);
            return;
        }
        let tool_input = &event.tool_input;
        let command = get_str(tool_input, "command");
        if event.tool_name.contains("delegate") {
            // Remember the instruction so the spawned lane's focus
            // transcript can open with the delegated brief (the normalized
            // AgentSpawned event carries no instruction).
            let agent = get_truthy_str(tool_input, &["agent", "agent_name"]);
            let brief = get_truthy_str(tool_input, &["instruction", "prompt", "task"]);
            if !agent.is_empty() && !brief.is_empty() {
                self.lane.remember_brief(&agent, &brief);
            }
        }
        // No durable per-tool line: the in-flight op shows as the active
        // branch in the live tree beneath the pulse, and rolls into the
        // burst digest on completion (DESIGN-SPEC §3).
        let label = op_label(&event.tool_name, tool_input);
        self.set_activity(&label);
        if let Some(turn) = self.turn.as_mut() {
            turn.calls.insert(
                event.tool_call_id.clone(),
                CallInfo {
                    tool: event.tool_name.clone(),
                    input: tool_input.clone(),
                    command,
                },
            );
            Self::push_activity(turn, &label);
        }
        self.update_working();
    }

    /// Add/replace the newest live-tree branch (bounded, newest last).
    fn push_activity(turn: &mut Turn, label: &str) {
        // Drop the previous still-"running" placeholder — only one op is
        // ever in flight for the pulse's purposes.
        let mut ring: Vec<ActivityBranch> = turn
            .activity_ring
            .iter()
            .filter(|branch| !branch.running)
            .cloned()
            .collect();
        ring.push(ActivityBranch {
            text: label.to_string(),
            running: true,
        });
        let start = ring.len().saturating_sub(ACTIVITY_TAIL);
        turn.activity_ring = ring[start..].to_vec();
    }

    /// Mark the in-flight branch done (keeps it in the tail, dim).
    fn settle_activity(turn: &mut Turn, label: &str) {
        let mut ring: Vec<ActivityBranch> = turn
            .activity_ring
            .iter()
            .filter(|branch| !branch.running)
            .cloned()
            .collect();
        ring.push(ActivityBranch {
            text: label.to_string(),
            running: false,
        });
        let start = ring.len().saturating_sub(ACTIVITY_TAIL);
        turn.activity_ring = ring[start..].to_vec();
    }

    fn tool_post(&mut self, event: &ev::ToolPost) {
        if event.tool_name == "update_plan" || event.tool_name == "todo" || self.turn.is_none() {
            // Plans are their own blocks (rendered from tool:pre); todos
            // feed the ambient plan panel — neither joins the digest.
            return;
        }
        let Some(info) = self
            .turn
            .as_mut()
            .and_then(|turn| turn.calls.remove(&event.tool_call_id))
        else {
            return;
        };
        self.set_activity(""); // tool finished — back to model time
        let tool_input = if !info.input.is_empty() {
            info.input.clone()
        } else {
            event.tool_input.clone()
        };
        self.tool_tokens += approx_tokens(&[&tool_input, &event.result]);
        let command = if !info.command.is_empty() {
            info.command.clone()
        } else {
            get_str(&tool_input, "command")
        };
        let tool = info.tool.clone();
        let status = get_str(&event.result, "status");
        if status == "denied" {
            // A denial is load-bearing: it always gets its own durable ⊘
            // line (spec §3/§7), never folded into the digest.
            let cmd = if command.is_empty() {
                op_label(&tool, &tool_input)
            } else {
                command
            };
            if let Some(turn) = self.turn.as_mut() {
                turn.blocked.insert(cmd.clone());
            }
            let reason = {
                let value = event.result.get("reason");
                match value {
                    Some(value) => value_text(value),
                    None => "denied".to_string(),
                }
            };
            let continuation = get_str(&event.result, "continuation");
            let id = self.lane.ids_mut().next_id();
            self.append_content(
                Blocked {
                    continuation,
                    ..Blocked::new(id, cmd, reason)
                }
                .into(),
            );
            if let Some(turn) = self.turn.as_mut() {
                Self::settle_activity(turn, &op_label(&tool, &tool_input));
            }
            self.update_working();
            return;
        }
        // Success: roll into the burst tally + live tree, update the digest.
        let success_false = matches!(event.result.get("success"), Some(Value::Bool(false)));
        if !success_false && !["error", "failed"].contains(&status.to_lowercase().as_str()) {
            self.record_change("main agent", &tool, &tool_input);
        }
        if let Some(turn) = self.turn.as_mut() {
            Self::settle_activity(turn, &op_label(&tool, &tool_input));
            let key = verb_noun(&tool);
            match turn.burst_counts.iter_mut().find(|(k, _)| *k == key) {
                Some((_, count)) => *count += 1,
                None => turn.burst_counts.push((key, 1)),
            }
            turn.burst_detail.push(op_detail(&tool, &tool_input));
        }
        self.render_digest();
        self.update_working();
    }

    /// Create or update this burst's single in-place digest line.
    fn render_digest(&mut self) {
        let (summary, body, digest_id) = {
            let Some(turn) = self.turn.as_ref() else { return };
            (
                digest_summary(&turn.burst_counts),
                turn.burst_detail.clone(),
                turn.digest_id.clone(),
            )
        };
        if summary.is_empty() {
            return;
        }
        match digest_id {
            None => {
                let id = self.lane.ids_mut().next_id();
                if let Some(turn) = self.turn.as_mut() {
                    turn.digest_id = Some(id.clone());
                }
                self.append_content(
                    ToolLine {
                        body,
                        status: ToolLineStatus::Completed,
                        ..ToolLine::new(id, summary)
                    }
                    .into(),
                );
            }
            Some(id) => {
                self.lane.host_mut().replace_block(
                    ToolLine {
                        body,
                        status: ToolLineStatus::Completed,
                        ..ToolLine::new(id, summary)
                    }
                    .into(),
                );
            }
        }
    }

    /// Freeze the current burst's digest and reset for the next run.
    ///
    /// Called when the model speaks (a durable answer/narration lands) and
    /// at turn end — the completed digest stays durable in place; the next
    /// tool opens a fresh digest below the answer (Claude-Code grammar).
    fn flush_burst(&mut self) {
        let Some(turn) = self.turn.as_mut() else { return };
        turn.digest_id = None;
        turn.burst_counts = Vec::new();
        turn.burst_detail = Vec::new();
        turn.activity_ring = Vec::new();
    }

    fn tool_error(&mut self, event: &ev::ToolError) {
        // Python pops the pending call and, when found, reads
        // `info["block_id"]` — a key `_tool_pre` never writes, so that arm
        // raises KeyError out of handle() (latent upstream bug; no pinned
        // test reaches it — root tool errors arrive without a matching pre
        // in practice). We keep the pop for state parity and degrade both
        // arms to the reachable fallback line instead of panicking.
        if let Some(turn) = self.turn.as_mut() {
            turn.calls.remove(&event.tool_call_id);
        }
        self.tool_tokens += approx_tokens(&[&event.error_message.as_str()]);
        let summary = rstrip_sep(&format!(
            "{} failed · {}",
            event.tool_name, event.error_message
        ));
        let id = self.lane.ids_mut().next_id();
        self.append_content(
            ToolLine {
                status: ToolLineStatus::Failed,
                ..ToolLine::new(id, summary)
            }
            .into(),
        );
    }

    fn update_plan(&mut self, event: &ev::ToolPre) {
        let raw = &event.tool_input;
        let title = {
            let explicit = get_truthy_str(raw, &["title"]);
            if explicit.is_empty() {
                "Plan".to_string()
            } else {
                explicit
            }
        };
        let items: Vec<PlanItem> = raw
            .get("steps")
            .and_then(Value::as_array)
            .map(|steps| {
                steps
                    .iter()
                    .filter_map(Value::as_object)
                    .map(|step| PlanItem {
                        text: get_str(step, "step"),
                        state: plan_state(step.get("status")),
                    })
                    .collect()
            })
            .unwrap_or_default();
        let read_only = raw.get("read_only").is_some_and(truthy);
        // Mockup: read-only (plan mode) headers never carry the live
        // telemetry suffix (runPlanTurn never calls setPlanTele).
        let telemetry = if read_only {
            None
        } else {
            Some(Self::live_telemetry(self.turn.as_ref()))
        };
        let block_id = self
            .turn
            .as_ref()
            .and_then(|turn| turn.plan_ids.get(&title).cloned());
        let id = match block_id.clone() {
            Some(id) => id,
            None => self.lane.ids_mut().next_id(),
        };
        let block = PlanBlock {
            read_only,
            items: items.clone(),
            telemetry,
            ..PlanBlock::new(id.clone(), title.clone())
        };
        if block_id.is_some() {
            self.lane.host_mut().replace_block(block.into());
        } else {
            if let Some(turn) = self.turn.as_mut() {
                turn.plan_ids.insert(title, id);
            }
            self.append_content(block.into());
        }
        if let Some(turn) = self.turn.as_mut() {
            let active = items
                .iter()
                .find(|item| item.state == PlanItemState::Active)
                .map(|item| item.text.clone());
            if let Some(active) = active {
                // Title keeps the last step name between steps — it is only
                // reassigned at step activation (mockup line 332).
                turn.active_step = Some(active);
            }
        }
    }

    /// Route the `todo` tool to the ambient plan panel — never the
    /// transcript (design 2026-07-21 D1/D3).
    ///
    /// The printing `hooks-todo-display` is stripped under the TUI, so
    /// newtui renders the list itself from the tool call's `todos` payload
    /// (`create`/`update` ops carry the full list; `list` carries none).
    /// Root-session only: child ToolPre events are diverted before dispatch
    /// (see [`Self::is_foreign_turn_event`]).
    fn update_todo(&mut self, event: &ev::ToolPre) {
        let Some(raw_todos) = event.tool_input.get("todos").and_then(Value::as_array) else {
            return; // a 'list' op or empty payload — nothing to redraw
        };
        if raw_todos.is_empty() {
            return;
        }
        let items: Vec<TodoItem> = raw_todos
            .iter()
            .filter_map(Value::as_object)
            .map(|todo| TodoItem {
                content: get_str(todo, "content"),
                status: todo_status(todo.get("status")),
            })
            .collect();
        if let Some(turn) = self.turn.as_mut() {
            turn.todo_items = items.clone();
        }
        self.lane.host_mut().plan_changed(&items);
        if self.delegate_summary_id.is_some() {
            // The runtime closes the plan AFTER the last AgentCompleted
            // (demo beat order: agent_completed → todo) — fold the fresh
            // todo state into the durable summary so its `Plan X/Y` header
            // ends true, not one beat behind (D3 plan-fold). Still an
            // in-turn replace: post-turn toggles are never clobbered.
            self.render_delegate_summary();
        }
    }

    // -- telemetry -------------------------------------------------------------------

    fn live_telemetry(turn: Option<&Turn>) -> TurnTelemetry {
        let Some(turn) = turn else {
            return TurnTelemetry::new(0.0);
        };
        TurnTelemetry {
            tokens_down: turn.tokens.max(0) as u64,
            ..TurnTelemetry::new((turn.last_ts - turn.start_ts).max(0.0))
        }
    }

    fn working_block(turn: &Turn) -> WorkingStatus {
        let working_id = turn
            .working_id
            .clone()
            .expect("working_block requires a mounted working line");
        // The live activity tree only rides single-agent turns; fan-out
        // turns get the dedicated DelegateSummaryBlock instead (D5).
        let lines = if turn.agent_total > 1 {
            Vec::new()
        } else {
            turn.activity_ring.clone()
        };
        WorkingStatus {
            // Spec §3: `N agent(s)` — 1 on single-agent turns, the fan-out
            // total (never decaying) on multi-agent turns.
            agent_count: turn.agent_total.max(1),
            spinner_frame: turn.spinner_frame,
            activity: turn.activity.clone(),
            activity_lines: lines,
            ..WorkingStatus::new(working_id, Self::live_telemetry(Some(turn)))
        }
    }

    fn update_working(&mut self) {
        let Some(turn) = self.turn.as_ref() else { return };
        if turn.working_id.is_none() {
            return;
        }
        let block = Self::working_block(turn);
        self.lane.host_mut().replace_block(block.into());
    }

    /// App 1s heartbeat while a turn runs: pulse the working line.
    ///
    /// Real turns get their clock bumped to wall time (usage events only
    /// arrive at each content-block end, which froze the seconds counter
    /// during long provider calls); scripted demo turns keep their
    /// virtual-clock telemetry and only pulse the spinner.
    pub fn tick(&mut self, now: f64) {
        let spec_is_none = {
            let Some(turn) = self.turn.as_mut() else { return };
            if turn.working_id.is_none() {
                return;
            }
            turn.spinner_frame += 1;
            let spec_is_none = turn.spec.is_none();
            if spec_is_none {
                turn.last_ts = turn.last_ts.max(now);
            }
            spec_is_none
        };
        self.update_working();
        // Per-agent lane clocks tick on the same heartbeat — real turns
        // only. Scripted lanes were stamped with the demo's virtual clock;
        // advancing them with wall time paints epoch-scale elapsed in the
        // panel.
        if spec_is_none && self.lane.lanes.advance(now) {
            self.lane.host_mut().lanes_changed();
        }
    }

    /// Update the working line's current-work note (real turns only).
    pub fn set_activity(&mut self, activity: &str) {
        {
            let Some(turn) = self.turn.as_mut() else { return };
            if turn.spec.is_some() || turn.activity == activity {
                return;
            }
            turn.activity = activity.to_string();
        }
        self.update_working();
    }

    fn usage(&mut self, event: &ev::ProviderResponseUsage) {
        self.total_tokens += event.output_tokens;
        self.memory_tokens = self.memory_tokens.max(event.cache_read + event.cache_write);
        let cost = self.cost.record(event);
        if let Some(turn) = self.turn.as_mut() {
            turn.tokens += event.output_tokens;
            self.update_working();
        }
        // Route per-lane telemetry: usage stamped with a registered child
        // session id belongs to that subagent's lane. The root turn session
        // is never a registered lane, so it never matches (no double count).
        let lane = self.lane.lanes.get(&event.session_id);
        if let Some(record) = lane {
            let lane_cost = event.cost_usd.unwrap_or(cost);
            self.lane.lanes.update(
                &event.session_id,
                LaneUpdate {
                    tokens: Some((record.lane.tokens as i64 + event.output_tokens).max(0) as u64),
                    cost: Some(record.lane.cost + lane_cost),
                    ..LaneUpdate::default()
                },
            );
            self.lane.host_mut().lanes_changed();
        }
    }

    /// Persist a quiet but inspectable compaction boundary in history.
    fn context_compacted(&mut self, event: &ev::ContextCompacted) {
        let token_delta = format!(
            "{} → {} tokens",
            format_thousands(event.before_tokens),
            format_thousands(event.after_tokens)
        );
        let message_delta = if event.before_messages != 0 || event.after_messages != 0 {
            format!(
                " · {} → {} messages",
                event.before_messages, event.after_messages
            )
        } else {
            String::new()
        };
        let level = if event.strategy_level != 0 {
            format!(" · strategy {}", event.strategy_level)
        } else {
            String::new()
        };
        let text = format!("Context compacted · {token_delta}{message_delta}{level}");
        let id = self.lane.ids_mut().next_id();
        self.append_content(Narration::new(id, text.clone()).into());
        self.lane.host_mut().show_notice(&text);
    }

    // -- approvals / notifications -----------------------------------------------------

    fn approval_denied(&mut self, event: &ev::ApprovalDenied) {
        let cmd = if event.command.is_empty() {
            event.prompt.clone()
        } else {
            event.command.clone()
        };
        if let Some(turn) = self.turn.as_ref() {
            if turn.blocked.contains(&cmd) || turn.blocked.contains(&event.prompt) {
                return; // already rendered from the denied tool:post
            }
        }
        let reason = if event.reason.is_empty() {
            "denied by user".to_string()
        } else {
            event.reason.clone()
        };
        let id = self.lane.ids_mut().next_id();
        self.append_content(
            Blocked {
                continuation: event.continuation.clone(),
                ..Blocked::new(id, cmd, reason)
            }
            .into(),
        );
    }

    fn notification(&mut self, event: &ev::Notification) {
        if event.source == "mode" {
            if let Some(caps) = mode_notice_re().captures(&event.message) {
                let mode = caps[1].to_string();
                self.lane.host_mut().set_mode_by_id(&mode, false);
            }
            self.lane.host_mut().show_notice(&event.message);
        } else if event.source == "needs_you" || event.level == "decision" {
            if let Some(turn) = self.turn.as_mut() {
                // Mockup runTurn `blocked = true` — the deferral marks the
                // turn so its close-out fires no end notice, keeping this
                // deferred-decision notice visible (spec §11).
                turn.deferred = true;
            }
            self.lane
                .host_mut()
                .decision_deferred(&event.message, &event.decision_id);
            self.lane.host_mut().show_notice(&event.message);
        } else if !event.message.is_empty() {
            self.lane.host_mut().show_notice(&event.message);
        }
    }

    // -- agent lanes --------------------------------------------------------------------

    fn agent_spawned(&mut self, event: &ev::AgentSpawned) {
        if let Some(turn) = self.turn.as_mut() {
            turn.agent_total += 1;
        }
        let seed = (self.lane_seed)(&event.agent).unwrap_or_default();
        let parent = if !event.parent_session_id.is_empty() {
            Some(event.parent_session_id.as_str())
        } else if !event.session_id.is_empty() {
            Some(event.session_id.as_str())
        } else {
            None
        };
        self.lane.lanes.register(
            &event.sub_session_id,
            parent,
            &event.agent,
            RegisterOptions {
                activity: if seed.activity.is_empty() {
                    "running".to_string()
                } else {
                    seed.activity.clone()
                },
                state: seed.state,
                // A done lane re-spawning here is a replayed turn reusing
                // its sub-session ids (completions for unknown lanes are
                // dropped, so no spawn/complete race reaches this path) —
                // reset it live.
                reopen: true,
                // Stamp the spawn time so advance() can tick the lane's
                // per-agent elapsed live between sparse usage events. The
                // envelope always stamps ts (default_factory) — no
                // fallback: the demo's virtual clock legitimately starts at
                // 0.0, and an `or time.time()` here mixes clock domains (0s
                // durations).
                now: event.ts,
            },
        );
        if seed.elapsed != 0.0 || seed.cost != Decimal::ZERO || seed.tokens != 0 {
            self.lane.lanes.update(
                &event.sub_session_id,
                LaneUpdate {
                    elapsed: Some(seed.elapsed),
                    cost: Some(seed.cost),
                    tokens: Some(seed.tokens),
                    ..LaneUpdate::default()
                },
            );
        }
        self.lane.seed_transcript(event);
        let now = event.ts;
        if self.delegate_rows.is_empty() {
            self.fanout_start_ts = now;
        }
        if !self.delegate_rows.contains_key(&event.sub_session_id) {
            self.delegate_order.push(event.sub_session_id.clone());
        }
        // A known sub-session re-spawning is a replayed turn reusing its
        // ids (see lanes.register reopen above) — reset the row live either
        // way.
        self.delegate_rows.insert(
            event.sub_session_id.clone(),
            DelegateRow::new(&event.agent, now),
        );
        self.render_delegate_summary();
        self.update_working();
        self.lane.host_mut().lanes_changed();
    }

    fn agent_completed(&mut self, event: &ev::AgentCompleted) {
        let result = if !event.result.is_empty() {
            event.result.clone()
        } else if event.success {
            String::new()
        } else {
            "failed".to_string()
        };
        let record = self.lane.lanes.get(&event.sub_session_id);
        let clear_key = record
            .as_ref()
            .map(|r| r.session_id.clone())
            .unwrap_or_else(|| event.sub_session_id.clone());
        self.lane.clear_tail(Some(&clear_key));
        if let Some(record) = record.as_ref() {
            // Focus-transcript close-out (mockup focusLane state recap):
            // `✳ ` dimmer + dim italic state line, never clickable.
            let recap = if event.success {
                "completed · result reported back to parent".to_string()
            } else if result.is_empty() || result == "failed" {
                "failed".to_string()
            } else {
                format!("failed · {result}")
            };
            let id = self.lane.ids_mut().next_id();
            self.lane.append_block(
                record,
                Answer {
                    clickable: false,
                    ..Answer::new(
                        id,
                        vec![
                            Segment {
                                style_token: StyleToken::Dimmer,
                                ..Segment::new("✳ ")
                            },
                            Segment {
                                style_token: StyleToken::Dim,
                                italic: true,
                                ..Segment::new(recap)
                            },
                        ],
                    )
                }
                .into(),
            );
        }
        self.lane.lanes.complete(
            &event.sub_session_id,
            &lane_result_summary(&result, OP_LABEL_MAX),
        );
        let row_known = if let Some(row) = self.delegate_rows.get_mut(&event.sub_session_id) {
            let end_ts = event.ts; // same clock domain as spawned_ts — no fallback
            row.state = if event.success {
                DelegateState::Done
            } else {
                DelegateState::Error
            };
            row.elapsed_s = (end_ts - row.spawned_ts).max(0.0);
            row.snippet = result;
            if self
                .delegate_rows
                .values()
                .all(|r| r.state != DelegateState::Running)
            {
                self.fanout_duration_s = (event.ts - self.fanout_start_ts).max(0.0);
            }
            true
        } else {
            false
        };
        if row_known {
            self.render_delegate_summary();
        }
        self.update_working();
        self.lane.host_mut().lanes_changed();
    }

    /// Append-once / replace-in-place, keyed by `delegate_summary_id`.
    ///
    /// Always rendered expanded=false — expansion is UI-local state; the
    /// transcript's replace path preserves a live widget's expansion so
    /// neither a mid-flight replace nor a post-turn straggler completion
    /// collapses a summary the user has opened.
    fn render_delegate_summary(&mut self) {
        if let Some(turn) = self.turn.as_ref() {
            if !turn.todo_items.is_empty() {
                self.delegate_plan_final = Some(turn.todo_items.clone());
            }
        }
        let entries: Vec<DelegateEntry> = self
            .delegate_order
            .iter()
            .map(|key| {
                let row = &self.delegate_rows[key];
                DelegateEntry {
                    agent: row.agent.clone(),
                    state: row.state,
                    elapsed_s: row.elapsed_s,
                    snippet: row.snippet.clone(),
                }
            })
            .collect();
        let id = match self.delegate_summary_id.clone() {
            Some(id) => id,
            None => self.lane.ids_mut().next_id(),
        };
        let block = DelegateSummaryBlock {
            entries,
            plan_final: self.delegate_plan_final.clone(),
            duration_s: self.fanout_duration_s,
            ..DelegateSummaryBlock::new(id.clone())
        };
        if self.delegate_summary_id.is_none() {
            self.delegate_summary_id = Some(id);
            self.append_content(block.into());
        } else {
            self.lane.host_mut().replace_block(block.into());
        }
    }
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::rc::Rc;
    use std::sync::Arc;

    use serde_json::json;

    use super::*;
    use crate::kernel::cost::PricingTable;
    use crate::model::lanes::LaneRegistry;
    use crate::model::turn::OutcomeLedger;

    fn dec(value: &str) -> Decimal {
        value.parse().expect("valid decimal literal")
    }

    fn obj(value: Value) -> Map<String, Value> {
        value.as_object().expect("json object").clone()
    }

    /// Union of the Python suites' FakeHost / ProbeHost / CountingHost:
    /// records blocks and every side-effect surface the tests inspect.
    struct FakeHost {
        mode_id: String,
        blocks: Vec<TranscriptBlock>,
        notices: Vec<String>,
        stream_events: Vec<(String, String)>,
        plan_changes: Vec<Vec<TodoItem>>,
        tail_updates: Vec<String>,
        tail_cleared: usize,
        approvals: Vec<String>,
        deferred: Vec<String>,
        turn_events: Vec<String>,
        lanes_changed_calls: usize,
    }

    impl FakeHost {
        fn new(mode_id: &str) -> Self {
            FakeHost {
                mode_id: mode_id.to_string(),
                blocks: Vec::new(),
                notices: Vec::new(),
                stream_events: Vec::new(),
                plan_changes: Vec::new(),
                tail_updates: Vec::new(),
                tail_cleared: 0,
                approvals: Vec::new(),
                deferred: Vec::new(),
                turn_events: Vec::new(),
                lanes_changed_calls: 0,
            }
        }
    }

    impl ReducerHost for FakeHost {
        fn mode_id(&self) -> String {
            self.mode_id.clone()
        }

        fn append_block(&mut self, block: TranscriptBlock) {
            self.blocks.push(block);
        }

        fn replace_block(&mut self, block: TranscriptBlock) {
            for existing in self.blocks.iter_mut() {
                if existing.id() == block.id() {
                    *existing = block;
                    return;
                }
            }
        }

        fn remove_block(&mut self, block_id: &str) {
            self.blocks.retain(|block| block.id() != block_id);
        }

        fn show_notice(&mut self, text: &str) {
            self.notices.push(text.to_string());
        }

        fn set_mode_by_id(&mut self, _mode_id: &str, _notify: bool) {}

        fn turn_started(&mut self) {
            self.turn_events.push("started".to_string());
        }

        fn turn_finished(&mut self) {
            self.turn_events.push("finished".to_string());
        }

        fn lanes_changed(&mut self) {
            self.lanes_changed_calls += 1;
        }

        fn plan_changed(&mut self, items: &[TodoItem]) {
            self.plan_changes.push(items.to_vec());
        }

        fn approval_opened(&mut self, prompt: &str, _options: &[String]) {
            self.approvals.push(prompt.to_string());
        }

        fn decision_deferred(&mut self, message: &str, _decision_id: &str) {
            self.deferred.push(message.to_string());
        }

        fn stream_opened(&mut self, block_type: &str) {
            self.stream_events
                .push(("opened".to_string(), block_type.to_string()));
        }

        fn stream_delta(&mut self, text: &str) {
            self.stream_events
                .push(("delta".to_string(), text.to_string()));
        }

        fn stream_closed(&mut self) {
            self.stream_events
                .push(("closed".to_string(), String::new()));
        }

        fn lane_tail_updated(&mut self, text: &str) {
            self.tail_updates.push(text.to_string());
        }

        fn lane_tail_cleared(&mut self) {
            self.tail_cleared += 1;
        }
    }

    /// Deterministic pricing: the identical fallback table Python resolves,
    /// pinned so cost.rs tests swapping the process-global table can never
    /// race these (see [`ReducerOptions::pricing`]).
    fn pinned_pricing() -> Option<Arc<PricingTable>> {
        Some(Arc::new(PricingTable::default()))
    }

    fn make_reducer(mode_id: &str) -> TranscriptReducer<FakeHost> {
        TranscriptReducer::with_options(
            FakeHost::new(mode_id),
            BlockIdAllocator::new(),
            OutcomeLedger::new(),
            LaneRegistry::new(),
            ReducerOptions {
                pricing: pinned_pricing(),
                ..ReducerOptions::default()
            },
        )
    }

    // -- event builders (the Python suites' `_env` fixtures) -----------------

    const SID: &str = "root-session";

    fn prompt_submit(session_id: &str, prompt: &str, ts: f64) -> ev::UIEvent {
        ev::UIEvent::PromptSubmit(ev::PromptSubmit {
            session_id: session_id.to_string(),
            ts,
            prompt: prompt.to_string(),
            ..ev::PromptSubmit::default()
        })
    }

    fn prompt_complete(session_id: &str, response: &str, ts: f64) -> ev::UIEvent {
        ev::UIEvent::PromptComplete(ev::PromptComplete {
            session_id: session_id.to_string(),
            ts,
            response: response.to_string(),
            ..ev::PromptComplete::default()
        })
    }

    fn tool_pre(session_id: &str, call_id: &str, tool: &str, input: Value, ts: f64) -> ev::UIEvent {
        ev::UIEvent::ToolPre(ev::ToolPre {
            session_id: session_id.to_string(),
            ts,
            tool_name: tool.to_string(),
            tool_call_id: call_id.to_string(),
            tool_input: obj(input),
            ..ev::ToolPre::default()
        })
    }

    fn tool_post(
        session_id: &str,
        call_id: &str,
        tool: &str,
        input: Value,
        result: Value,
        ts: f64,
    ) -> ev::UIEvent {
        ev::UIEvent::ToolPost(ev::ToolPost {
            session_id: session_id.to_string(),
            ts,
            tool_name: tool.to_string(),
            tool_call_id: call_id.to_string(),
            tool_input: obj(input),
            result: obj(result),
            ..ev::ToolPost::default()
        })
    }

    fn content_end(session_id: &str, parent: Option<&str>, block: Value, ts: f64) -> ev::UIEvent {
        ev::UIEvent::ContentBlockEnd(ev::ContentBlockEnd {
            session_id: session_id.to_string(),
            parent_id: parent.map(str::to_string),
            ts,
            block_type: block
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or("text")
                .to_string(),
            block: obj(block),
            ..ev::ContentBlockEnd::default()
        })
    }

    fn agent_spawned(agent: &str, sub: &str, ts: f64) -> ev::UIEvent {
        ev::UIEvent::AgentSpawned(ev::AgentSpawned {
            session_id: SID.to_string(),
            ts,
            agent: agent.to_string(),
            sub_session_id: sub.to_string(),
            parent_session_id: SID.to_string(),
            ..ev::AgentSpawned::default()
        })
    }

    fn agent_completed(agent: &str, sub: &str, ts: f64, success: bool, result: &str) -> ev::UIEvent {
        ev::UIEvent::AgentCompleted(ev::AgentCompleted {
            session_id: SID.to_string(),
            ts,
            agent: agent.to_string(),
            sub_session_id: sub.to_string(),
            parent_session_id: SID.to_string(),
            success,
            result: result.to_string(),
            ..ev::AgentCompleted::default()
        })
    }

    fn answer_text(answer: &Answer) -> String {
        answer.spans.iter().map(|s| s.text.as_str()).collect()
    }

    fn answers(host: &FakeHost) -> Vec<&Answer> {
        host.blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::Answer(answer) => Some(answer),
                _ => None,
            })
            .collect()
    }

    fn summaries(host: &FakeHost) -> Vec<&DelegateSummaryBlock> {
        host.blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::DelegateSummary(summary) => Some(summary),
                _ => None,
            })
            .collect()
    }

    fn last_rule(host: &FakeHost) -> &TurnRule {
        host.blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::TurnRule(rule) => Some(rule),
                _ => None,
            })
            .next_back()
            .expect("a TurnRule in the transcript")
    }

    fn kinds(host: &FakeHost) -> Vec<&'static str> {
        host.blocks.iter().map(TranscriptBlock::kind).collect()
    }

    fn block_texts(blocks: &[TranscriptBlock]) -> Vec<String> {
        blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::Answer(answer) => Some(answer_text(answer)),
                _ => None,
            })
            .collect()
    }

    /// Oracle check (not a pinned pytest case): helper outputs captured from
    /// the real Python module (`uv run python -c ...` against `ui/reducer.py`).
    #[test]
    fn oracle_helpers_match_python() {
        // _lane_result_summary: look-around underscore stripping, tilde/backtick
        // pairs, quote-marker + link unwrap.
        assert_eq!(
            lane_result_summary("snake_case_name and _leading under_score _ alone", 52),
            "snakecasename and leading underscore _ alone"
        );
        assert_eq!(lane_result_summary("a**b__c~~d```e*f", 52), "abcdef");
        assert_eq!(
            lane_result_summary("> quoted **line** with [x](y) tail", 52),
            "quoted line with x tail"
        );
        // _digest_summary: verb order + unknown-tool noun pluralization.
        let counts = vec![
            (("used".to_string(), Some("my tool".to_string())), 2),
            (("read".to_string(), Some("file".to_string())), 1),
            (("searched web".to_string(), None), 3),
        ];
        assert_eq!(
            digest_summary(&counts),
            "Read 1 file · searched web 3× · used 2 my tools"
        );
        // _op_label / _live_op_label basename the target — even URLs/patterns.
        assert_eq!(
            op_label("web_fetch", &obj(json!({"url": "https://example.com/some/page"}))),
            "fetched page"
        );
        assert_eq!(op_label("grep", &obj(json!({"pattern": "a/b/c"}))), "searched c");
        assert_eq!(
            live_op_label("bash", &obj(json!({"command": "echo hi"}))),
            "running echo hi"
        );
        // _truncate: 52-char cap → 51 chars + ellipsis.
        assert_eq!(truncate(&"x".repeat(60), OP_LABEL_MAX), format!("{}…", "x".repeat(51)));
        // _change_preview write_file cap: 2 header rows + 100 content rows
        // → 80 kept + '… 22 more lines'.
        let content = (0..100).map(|i| format!("l{i}")).collect::<Vec<_>>().join("\n");
        let (paths, lines) = change_preview(
            "write_file",
            &obj(json!({"file_path": "/tmp/f.txt", "content": content})),
        );
        assert_eq!(paths, vec!["/tmp/f.txt"]);
        assert_eq!(lines.len(), 81);
        assert_eq!(lines[1], "@@ wrote file · 100 lines @@");
        assert_eq!(lines.last().map(String::as_str), Some("… 22 more lines"));
    }

    // =====================================================================
    // tests/test_ui_reducer_delegates.py
    // =====================================================================

    fn start_fanout(reducer: &mut TranscriptReducer<FakeHost>) {
        reducer.handle(&prompt_submit(SID, "fan out", 0.0));
    }

    /// Pins Python `test_fanout_appends_exactly_one_summary_block`.
    #[test]
    fn test_fanout_appends_exactly_one_summary_block() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&agent_spawned("researcher", "s1", 1.0));
        reducer.handle(&agent_spawned("coder", "s2", 1.0));
        reducer.handle(&agent_spawned("tester", "s3", 1.0));
        let host = reducer.host();
        let blocks = summaries(host);
        assert_eq!(blocks.len(), 1);
        let block = blocks[0];
        let agents: Vec<&str> = block.entries.iter().map(|e| e.agent.as_str()).collect();
        assert_eq!(agents, vec!["researcher", "coder", "tester"]);
        assert!(block
            .entries
            .iter()
            .all(|e| e.state == DelegateState::Running));
        assert!(!block.expanded);
    }

    /// Pins Python `test_no_tree_line_answer_blocks_anymore`.
    #[test]
    fn test_no_tree_line_answer_blocks_anymore() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&agent_spawned("researcher", "s1", 1.0));
        reducer.handle(&agent_completed("researcher", "s1", 3.0, true, "3 findings"));
        assert!(!answers(reducer.host())
            .iter()
            .any(|a| answer_text(a).contains("researcher")));
    }

    /// Pins Python `test_completion_updates_in_place_with_elapsed_and_snippet`.
    #[test]
    fn test_completion_updates_in_place_with_elapsed_and_snippet() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&agent_spawned("researcher", "s1", 1.0));
        reducer.handle(&agent_spawned("coder", "s2", 1.0));
        reducer.handle(&agent_completed("researcher", "s1", 5.4, true, "3 findings"));
        let host = reducer.host();
        let block = summaries(host)[0];
        let done = &block.entries[0];
        assert_eq!(done.state, DelegateState::Done);
        assert_eq!(done.snippet, "3 findings");
        assert_eq!(done.elapsed_s, 4.4);
        assert_eq!(block.entries[1].state, DelegateState::Running);
        assert_eq!(summaries(host).len(), 1); // replaced, never re-appended
    }

    /// Pins Python `test_all_complete_finalizes_duration_and_failure_state`.
    #[test]
    fn test_all_complete_finalizes_duration_and_failure_state() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&agent_spawned("coder", "s1", 1.0));
        reducer.handle(&agent_spawned("tester", "s2", 1.0));
        reducer.handle(&agent_completed("tester", "s2", 3.6, true, "tests ✔"));
        reducer.handle(&agent_completed("coder", "s1", 7.0, false, ""));
        let block = summaries(reducer.host())[0];
        assert_eq!(block.entries[0].state, DelegateState::Error);
        assert_eq!(block.entries[0].snippet, "failed");
        assert_eq!(block.duration_s, 6.0); // last completion − first spawn
    }

    /// Pins Python `test_plan_final_captured_from_turn_todos`.
    #[test]
    fn test_plan_final_captured_from_turn_todos() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&tool_pre(
            SID,
            "t1",
            "todo",
            json!({"todos": [
                {"content": "scan docs", "status": "completed"},
                {"content": "synthesize", "status": "in_progress"},
            ]}),
            0.5,
        ));
        reducer.handle(&agent_spawned("researcher", "s1", 1.0));
        reducer.handle(&agent_completed("researcher", "s1", 2.0, true, "ok"));
        let block = summaries(reducer.host())[0];
        let plan = block.plan_final.as_ref().expect("plan_final captured");
        let contents: Vec<&str> = plan.iter().map(|i| i.content.as_str()).collect();
        assert_eq!(contents, vec!["scan docs", "synthesize"]);
    }

    /// Pins Python `test_todo_beat_after_last_completion_folds_into_plan_final`.
    ///
    /// The runtime closes the plan AFTER the last AgentCompleted (demo:
    /// `…agent_completed + TODO`) — the durable summary must fold that
    /// final todo state in, so its header ends `Plan 4/4`, not one beat
    /// behind (design D3 plan-fold).
    #[test]
    fn test_todo_beat_after_last_completion_folds_into_plan_final() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&tool_pre(
            SID,
            "t1",
            "todo",
            json!({"todos": [{"content": "scan docs", "status": "in_progress"}]}),
            0.5,
        ));
        reducer.handle(&agent_spawned("coder", "s1", 1.0));
        reducer.handle(&agent_completed("coder", "s1", 2.0, true, "ok"));
        reducer.handle(&tool_pre(
            SID,
            "t2",
            "todo",
            json!({"todos": [{"content": "scan docs", "status": "completed"}]}),
            2.1,
        ));
        let host = reducer.host();
        let block = summaries(host)[0];
        let plan = block.plan_final.as_ref().expect("plan_final captured");
        let statuses: Vec<TodoStatus> = plan.iter().map(|i| i.status).collect();
        assert_eq!(statuses, vec![TodoStatus::Completed]);
        assert_eq!(summaries(host).len(), 1); // replaced in place, never re-appended
    }

    /// Pins Python `test_no_todos_means_plan_final_none`.
    #[test]
    fn test_no_todos_means_plan_final_none() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&agent_spawned("coder", "s1", 1.0));
        reducer.handle(&agent_completed("coder", "s1", 2.0, true, "ok"));
        assert!(summaries(reducer.host())[0].plan_final.is_none());
    }

    /// Pins Python `test_cancelled_turn_marks_running_entries_cancelled`.
    #[test]
    fn test_cancelled_turn_marks_running_entries_cancelled() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&agent_spawned("coder", "s1", 1.0));
        reducer.handle(&ev::UIEvent::CancelCompleted(ev::CancelCompleted {
            session_id: SID.to_string(),
            ts: 4.0,
            ..ev::CancelCompleted::default()
        }));
        reducer.handle(&prompt_complete(SID, "", 5.0));
        let block = summaries(reducer.host())[0];
        assert_eq!(block.entries[0].state, DelegateState::Cancelled);
    }

    /// Pins Python `test_second_turn_gets_a_fresh_summary_block`.
    #[test]
    fn test_second_turn_gets_a_fresh_summary_block() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&agent_spawned("coder", "s1", 1.0));
        reducer.handle(&agent_completed("coder", "s1", 2.0, true, "ok"));
        reducer.handle(&prompt_complete(SID, "", 3.0));
        reducer.handle(&prompt_submit(SID, "again", 10.0));
        reducer.handle(&agent_spawned("tester", "s9", 11.0));
        assert_eq!(summaries(reducer.host()).len(), 2);
    }

    // -- heartbeat vs scripted lanes (found live in forge, 2026-07-21) ------

    /// Pins Python `test_demo_turn_heartbeat_keeps_virtual_lane_clocks`.
    ///
    /// Scripted lanes are stamped with the demo's virtual clock (~seconds);
    /// the app heartbeat passes wall time. Advancing them with wall time
    /// paints epoch-scale elapsed (`29744551m 45s`) in the lanes panel.
    #[test]
    fn test_demo_turn_heartbeat_keeps_virtual_lane_clocks() {
        let mut reducer = TranscriptReducer::with_options(
            FakeHost::new("chat"),
            BlockIdAllocator::new(),
            OutcomeLedger::new(),
            LaneRegistry::new(),
            ReducerOptions {
                spec_lookup: Some(Box::new(|_| {
                    Some(TurnSpec {
                        duration_ms: 6000,
                        ..TurnSpec::default()
                    })
                })),
                pricing: pinned_pricing(),
                ..ReducerOptions::default()
            },
        );
        reducer.handle(&prompt_submit(SID, "fan out", 0.0));
        reducer.handle(&agent_spawned("researcher", "s1", 1.0));
        // Precondition: the working pulse is mounted, so tick() reaches the lanes.
        assert!(kinds(reducer.host()).contains(&"working_status"));
        reducer.tick(1_753_000_000.0); // wall clock, ~55 years after ts=1.0
        let lane = reducer.lanes().active()[0].lane.clone();
        assert!(lane.elapsed < 60.0); // virtual-clock telemetry kept, not clobbered
    }

    /// Pins Python `test_real_turn_heartbeat_advances_lane_clocks`.
    ///
    /// Spec-less (real) turns DO tick per-lane clocks on the heartbeat —
    /// both spawn ts and tick now are wall clock there.
    #[test]
    fn test_real_turn_heartbeat_advances_lane_clocks() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit(SID, "fan out", 100.0));
        reducer.handle(&agent_spawned("researcher", "s1", 100.0));
        reducer.tick(103.0);
        let lane = reducer.lanes().active()[0].lane.clone();
        assert_eq!(lane.elapsed, 3.0);
    }

    /// Pins Python `test_fanout_at_virtual_clock_zero_keeps_duration_and_elapsed`.
    ///
    /// The demo's virtual clock legitimately starts at ts=0.0; a falsy-ts
    /// fallback to wall time mixes clock domains and clamps the fan-out
    /// duration to 0 (found live in forge: `· 0s ▸` after `seed → agents`,
    /// where the waitless seed turn leaves the clock at zero).
    #[test]
    fn test_fanout_at_virtual_clock_zero_keeps_duration_and_elapsed() {
        let mut reducer = make_reducer("chat");
        start_fanout(&mut reducer);
        reducer.handle(&agent_spawned("researcher", "s1", 0.0));
        reducer.handle(&agent_spawned("coder", "s2", 0.0));
        reducer.handle(&agent_completed("researcher", "s1", 2.6, true, "3 findings"));
        reducer.handle(&agent_completed("coder", "s2", 6.0, true, "2 files"));
        let block = summaries(reducer.host())[0];
        assert_eq!(block.duration_s, 6.0);
        assert_eq!(block.entries[0].elapsed_s, 2.6);
        assert_eq!(block.entries[1].elapsed_s, 6.0);
    }

    // =====================================================================
    // tests/test_ui_reducer_lane_tail.py
    // =====================================================================

    const ROOT: &str = "root-session";
    const CHILD_A: &str = "child-aaaaaaaaaaaaaaaa";
    const CHILD_B: &str = "child-bbbbbbbbbbbbbbbb";

    fn make_tail() -> (TranscriptReducer<FakeHost>, Rc<Cell<f64>>) {
        let clock = Rc::new(Cell::new(100.0));
        let handle = clock.clone();
        let mut reducer = TranscriptReducer::with_options(
            FakeHost::new("auto"),
            BlockIdAllocator::new(),
            OutcomeLedger::new(),
            LaneRegistry::new(),
            ReducerOptions {
                tail_clock: Some(Box::new(move || handle.get())),
                pricing: pinned_pricing(),
                ..ReducerOptions::default()
            },
        );
        reducer.handle(&prompt_submit(ROOT, "fan out", 1.0));
        (reducer, clock)
    }

    fn delta_event(sub: &str, text: &str, block_type: &str) -> ev::UIEvent {
        ev::UIEvent::StreamBlockDelta(ev::StreamBlockDelta {
            session_id: sub.to_string(),
            request_id: format!("req-{sub}"),
            block_index: 0,
            block_type: block_type.to_string(),
            sequence: 0,
            text: text.to_string(),
            ..ev::StreamBlockDelta::default()
        })
    }

    /// Pins Python `test_child_text_delta_paints_the_accumulated_buffer`.
    #[test]
    fn test_child_text_delta_paints_the_accumulated_buffer() {
        let (mut reducer, clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&delta_event(CHILD_A, "reading the ", "text"));
        clock.set(clock.get() + LANE_TAIL_NOTIFY_SECONDS);
        reducer.handle(&delta_event(CHILD_A, "queue bridge", "text"));
        assert_eq!(
            reducer.host().tail_updates,
            vec!["reading the ", "reading the queue bridge"]
        );
    }

    /// Pins Python `test_thinking_deltas_never_reach_the_tail`.
    #[test]
    fn test_thinking_deltas_never_reach_the_tail() {
        let (mut reducer, _clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&delta_event(CHILD_A, "hmm", "thinking"));
        assert!(reducer.host().tail_updates.is_empty());
    }

    /// Pins Python `test_deltas_within_the_notify_window_coalesce_without_losing_text`.
    #[test]
    fn test_deltas_within_the_notify_window_coalesce_without_losing_text() {
        let (mut reducer, clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&delta_event(CHILD_A, "one ", "text"));
        reducer.handle(&delta_event(CHILD_A, "two ", "text")); // same clock instant — paint throttled
        assert_eq!(reducer.host().tail_updates, vec!["one "]);
        clock.set(clock.get() + LANE_TAIL_NOTIFY_SECONDS);
        reducer.handle(&delta_event(CHILD_A, "three", "text"));
        assert_eq!(
            reducer.host().tail_updates,
            vec!["one ", "one two three"] // nothing lost
        );
    }

    /// Pins Python `test_focus_follows_the_most_recently_streaming_lane`.
    #[test]
    fn test_focus_follows_the_most_recently_streaming_lane() {
        let (mut reducer, clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&agent_spawned("coder", CHILD_B, 1.0));
        reducer.handle(&delta_event(CHILD_A, "aaa", "text"));
        clock.set(clock.get() + LANE_TAIL_NOTIFY_SECONDS);
        reducer.handle(&delta_event(CHILD_B, "bbb", "text"));
        assert_eq!(reducer.host().tail_updates, vec!["aaa", "bbb"]);
        let tailed = reducer.lanes().tail_lane().expect("a tailed lane");
        assert_eq!(tailed.session_id, CHILD_B);
    }

    /// Pins Python `test_explicit_cycle_pin_wins_over_recent_activity`.
    #[test]
    fn test_explicit_cycle_pin_wins_over_recent_activity() {
        let (mut reducer, clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&agent_spawned("coder", CHILD_B, 1.0));
        reducer.handle(&delta_event(CHILD_A, "aaa", "text"));
        let pinned = reducer.lanes_mut().cycle_tail_focus(); // A (current) → B
        assert_eq!(
            pinned.expect("a pinned lane").session_id,
            CHILD_B.to_string()
        );
        clock.set(clock.get() + LANE_TAIL_NOTIFY_SECONDS);
        reducer.handle(&delta_event(CHILD_A, "more-a", "text")); // not focused: buffered, not painted
        clock.set(clock.get() + LANE_TAIL_NOTIFY_SECONDS);
        reducer.handle(&delta_event(CHILD_B, "bbb", "text"));
        assert_eq!(reducer.host().tail_updates, vec!["aaa", "bbb"]);
    }

    /// Pins Python `test_root_stream_preempts_clears_and_suppresses_the_tail`.
    #[test]
    fn test_root_stream_preempts_clears_and_suppresses_the_tail() {
        let (mut reducer, clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&delta_event(CHILD_A, "child text", "text"));
        assert_eq!(reducer.host().tail_updates, vec!["child text"]);
        reducer.handle(&ev::UIEvent::StreamBlockStart(ev::StreamBlockStart {
            session_id: ROOT.to_string(),
            request_id: "req-root".to_string(),
            block_index: 0,
            block_type: "text".to_string(),
            ..ev::StreamBlockStart::default()
        }));
        assert_eq!(reducer.host().tail_cleared, 1); // cleared the instant the root speaks
        clock.set(clock.get() + LANE_TAIL_NOTIFY_SECONDS);
        reducer.handle(&delta_event(CHILD_A, " while root streams", "text")); // buffered, never painted
        assert_eq!(reducer.host().tail_updates, vec!["child text"]);
        reducer.handle(&ev::UIEvent::StreamBlockEnd(ev::StreamBlockEnd {
            session_id: ROOT.to_string(),
            request_id: "req-root".to_string(),
            block_index: 0,
            block_type: "text".to_string(),
            ..ev::StreamBlockEnd::default()
        }));
        clock.set(clock.get() + LANE_TAIL_NOTIFY_SECONDS);
        reducer.handle(&delta_event(CHILD_A, ", resumes", "text"));
        // Preemption DISCARDED the old buffer (ephemeral, D4) — the tail
        // restarts from whatever streamed after the root went idle again.
        assert_eq!(
            reducer.host().tail_updates.last().map(String::as_str),
            Some(" while root streams, resumes")
        );
    }

    /// Pins Python `test_lane_completion_clears_a_shown_tail`.
    #[test]
    fn test_lane_completion_clears_a_shown_tail() {
        let (mut reducer, _clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&delta_event(CHILD_A, "child text", "text"));
        reducer.handle(&ev::UIEvent::AgentCompleted(ev::AgentCompleted {
            session_id: ROOT.to_string(),
            agent: "researcher".to_string(),
            sub_session_id: CHILD_A.to_string(),
            parent_session_id: ROOT.to_string(),
            success: true,
            result: "3 findings".to_string(),
            ..ev::AgentCompleted::default()
        }));
        assert_eq!(reducer.host().tail_cleared, 1);
    }

    /// Pins Python `test_turn_end_discards_all_tail_state_and_leaves_no_block_behind`.
    #[test]
    fn test_turn_end_discards_all_tail_state_and_leaves_no_block_behind() {
        let (mut reducer, _clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&delta_event(CHILD_A, "ephemeral child prose", "text"));
        reducer.handle(&prompt_complete(ROOT, "", 2.0));
        assert_eq!(reducer.host().tail_cleared, 1);
        // Ephemeral: the tail text never became transcript content.
        assert!(!answers(reducer.host())
            .iter()
            .any(|a| answer_text(a).contains("ephemeral child prose")));
    }

    /// Pins Python `test_repaint_lane_tail_paints_the_newly_pinned_buffer`.
    ///
    /// ctrl+o repaints immediately — without this the tail keeps showing
    /// the previous lane's text until the pinned lane's next delta (found
    /// live in forge: demo bursts are one-shot, so it never updated).
    #[test]
    fn test_repaint_lane_tail_paints_the_newly_pinned_buffer() {
        let (mut reducer, clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&agent_spawned("coder", CHILD_B, 1.0));
        reducer.handle(&delta_event(CHILD_A, "aaa", "text"));
        clock.set(clock.get() + LANE_TAIL_NOTIFY_SECONDS);
        reducer.handle(&delta_event(CHILD_B, "bbb", "text")); // focus follows B, painted
        reducer.lanes_mut().cycle_tail_focus(); // pin cycles B → A
        reducer.repaint_lane_tail();
        assert_eq!(
            reducer.host().tail_updates.last().map(String::as_str),
            Some("aaa")
        );
    }

    /// Pins Python `test_repaint_lane_tail_clears_when_pinned_lane_has_no_buffer`.
    #[test]
    fn test_repaint_lane_tail_clears_when_pinned_lane_has_no_buffer() {
        let (mut reducer, _clock) = make_tail();
        reducer.handle(&agent_spawned("researcher", CHILD_A, 1.0));
        reducer.handle(&agent_spawned("coder", CHILD_B, 1.0));
        reducer.handle(&delta_event(CHILD_A, "aaa", "text"));
        reducer.lanes_mut().cycle_tail_focus(); // A (current) → B, which never streamed
        reducer.repaint_lane_tail();
        assert_eq!(reducer.host().tail_cleared, 1);
    }

    // =====================================================================
    // tests/test_ui_reducer_lane_transcripts.py
    // =====================================================================

    fn start_and_delegate(
        reducer: &mut TranscriptReducer<FakeHost>,
        agent: &str,
        sub: &str,
        brief: &str,
    ) {
        reducer.handle(&prompt_submit(SID, "fan out", 0.0));
        reducer.handle(&tool_pre(
            SID,
            "d1",
            "delegate",
            json!({"agent": agent, "instruction": brief}),
            0.5,
        ));
        reducer.handle(&agent_spawned(agent, sub, 1.0));
    }

    /// Pins Python `test_child_events_accumulate_a_focus_transcript`.
    #[test]
    fn test_child_events_accumulate_a_focus_transcript() {
        let mut reducer = make_reducer("chat");
        start_and_delegate(&mut reducer, "researcher", "s1", "find the flaky tests");
        reducer.handle(&content_end(
            "s1",
            Some(SID),
            json!({"type": "text", "text": "Scanning CI history for retries."}),
            2.0,
        ));
        reducer.handle(&ev::UIEvent::ToolPost(ev::ToolPost {
            session_id: "s1".to_string(),
            parent_id: Some(SID.to_string()),
            ts: 3.0,
            tool_name: "read_file".to_string(),
            tool_call_id: "t1".to_string(),
            tool_input: obj(json!({"path": "ci.log"})),
            result: obj(json!({"success": true})),
            ..ev::ToolPost::default()
        }));
        reducer.handle(&agent_completed(
            "researcher",
            "s1",
            4.0,
            true,
            "3 flaky tests found",
        ));

        let blocks = reducer.lane_transcript("s1").expect("transcript exists");
        assert_eq!(blocks.len(), 5);
        let TranscriptBlock::SessionBanner(banner) = &blocks[0] else {
            panic!("expected SessionBanner");
        };
        assert!(banner.focus_note.contains("focused: researcher"));
        assert!(banner.focus_note.contains(&SID[..6]));
        let TranscriptBlock::UserLine(brief) = &blocks[1] else {
            panic!("expected UserLine");
        };
        assert_eq!(brief.text, "find the flaky tests");
        assert_eq!(brief.mode, "delegated");
        let TranscriptBlock::Answer(prose) = &blocks[2] else {
            panic!("expected Answer");
        };
        assert!(!prose.clickable);
        assert!(answer_text(prose).contains("Scanning CI history"));
        let TranscriptBlock::ToolLine(tool) = &blocks[3] else {
            panic!("expected ToolLine");
        };
        assert_eq!(tool.status, ToolLineStatus::Completed);
        assert_eq!(tool.tool_call_ids, vec!["t1".to_string()]);
        let TranscriptBlock::Answer(recap) = &blocks[4] else {
            panic!("expected Answer recap");
        };
        let recap_text = answer_text(recap);
        assert!(recap_text.contains("✳ "));
        assert!(recap_text.contains("completed · result reported back to parent"));
        // The foreign-turn rule still holds: none of it reached the root.
        assert!(!answers(reducer.host())
            .iter()
            .any(|a| answer_text(a).contains("Scanning CI history")));
    }

    /// Pins Python `test_lane_transcript_resolves_by_agent_name_and_misses_cleanly`.
    #[test]
    fn test_lane_transcript_resolves_by_agent_name_and_misses_cleanly() {
        let mut reducer = make_reducer("chat");
        start_and_delegate(&mut reducer, "modular-builder", "s1", "build the module");
        assert!(reducer.lane_transcript("modular-builder").is_some());
        assert!(reducer.lane_transcript("s1").is_some());
        assert!(reducer.lane_transcript("nope").is_none());
    }

    /// Pins Python `test_failed_tool_error_and_failure_recap_rows`.
    #[test]
    fn test_failed_tool_error_and_failure_recap_rows() {
        let mut reducer = make_reducer("chat");
        start_and_delegate(&mut reducer, "debugger", "s1", "fix it");
        reducer.handle(&ev::UIEvent::ToolPost(ev::ToolPost {
            session_id: "s1".to_string(),
            parent_id: Some(SID.to_string()),
            ts: 2.0,
            tool_name: "bash".to_string(),
            tool_call_id: "t1".to_string(),
            tool_input: obj(json!({"command": "pytest"})),
            result: obj(json!({"success": false})),
            ..ev::ToolPost::default()
        }));
        reducer.handle(&ev::UIEvent::ToolError(ev::ToolError {
            session_id: "s1".to_string(),
            parent_id: Some(SID.to_string()),
            ts: 2.5,
            tool_name: "read_file".to_string(),
            tool_call_id: "t2".to_string(),
            error_message: "no such file".to_string(),
            ..ev::ToolError::default()
        }));
        reducer.handle(&agent_completed("debugger", "s1", 3.0, false, "boom"));
        let blocks = reducer.lane_transcript("s1").expect("transcript exists");
        let tools: Vec<&ToolLine> = blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::ToolLine(tool) => Some(tool),
                _ => None,
            })
            .collect();
        let statuses: Vec<ToolLineStatus> = tools.iter().map(|t| t.status).collect();
        assert_eq!(statuses, vec![ToolLineStatus::Failed, ToolLineStatus::Failed]);
        assert!(tools[1].summary.contains("no such file"));
        let texts = block_texts(&blocks);
        assert!(texts.last().expect("answer rows").contains("failed · boom"));
    }

    /// Pins Python `test_respawn_resets_the_lane_transcript`.
    #[test]
    fn test_respawn_resets_the_lane_transcript() {
        let mut reducer = make_reducer("chat");
        start_and_delegate(&mut reducer, "researcher", "s1", "first brief");
        reducer.handle(&content_end(
            "s1",
            Some(SID),
            json!({"type": "text", "text": "old work"}),
            2.0,
        ));
        // Replayed turn reuses the sub-session id (the lanes.register reopen
        // rule) — the focus transcript must restart with it.
        start_and_delegate(&mut reducer, "researcher", "s1", "second brief");
        let blocks = reducer.lane_transcript("s1").expect("transcript exists");
        assert!(!block_texts(&blocks).join(" ").contains("old work"));
        let briefs: Vec<String> = blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::UserLine(line) => Some(line.text.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(briefs, vec!["second brief"]);
    }

    /// Pins Python `test_lane_transcript_is_bounded_and_keeps_the_seed_rows`.
    #[test]
    fn test_lane_transcript_is_bounded_and_keeps_the_seed_rows() {
        let mut reducer = make_reducer("chat");
        start_and_delegate(&mut reducer, "researcher", "s1", "the brief");
        for n in 0..(LANE_TRANSCRIPT_MAX_BLOCKS + 25) {
            reducer.handle(&ev::UIEvent::ContentBlockEnd(ev::ContentBlockEnd {
                event_id: format!("c-{n}"),
                session_id: "s1".to_string(),
                parent_id: Some(SID.to_string()),
                ts: 2.0 + n as f64,
                block_type: "text".to_string(),
                block: obj(json!({"text": format!("row {n}")})),
                ..ev::ContentBlockEnd::default()
            }));
        }
        let blocks = reducer.lane_transcript("s1").expect("transcript exists");
        assert!(blocks.len() <= LANE_TRANSCRIPT_MAX_BLOCKS);
        assert!(matches!(blocks[0], TranscriptBlock::SessionBanner(_)));
        assert!(matches!(blocks[1], TranscriptBlock::UserLine(_))); // seed rows survive the trim
        let last = block_texts(&blocks).pop().expect("answer rows");
        assert!(last.contains(&format!("row {}", LANE_TRANSCRIPT_MAX_BLOCKS + 24)));
    }

    // =====================================================================
    // tests/test_ui_reducer_outcomes.py
    // =====================================================================

    /// Pins Python
    /// `test_production_text_stays_styled_and_final_response_promotes_exactly_once`.
    #[test]
    fn test_production_text_stays_styled_and_final_response_promotes_exactly_once() {
        let evidence = vec![EvidenceLink::new("Done", "$ pytest")];
        let evidence_for_lookup = evidence.clone();
        let mut reducer = TranscriptReducer::with_options(
            FakeHost::new("chat"),
            BlockIdAllocator::new(),
            OutcomeLedger::new(),
            LaneRegistry::new(),
            ReducerOptions {
                evidence_lookup: Some(Box::new(move |text| {
                    if text.trim() == "Done." {
                        evidence_for_lookup.clone()
                    } else {
                        Vec::new()
                    }
                })),
                pricing: pinned_pricing(),
                ..ReducerOptions::default()
            },
        );
        reducer.handle(&prompt_submit("root", "do it", 1.0));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "text", "text": "Checking the files."}),
            2.0,
        ));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "text", "text": "Done."}),
            3.0,
        ));

        let candidates: Vec<String> = answers(reducer.host())
            .iter()
            .map(|a| answer_text(a))
            .collect();
        assert_eq!(candidates, vec!["Checking the files.", "Done."]);
        assert!(answers(reducer.host()).iter().all(|a| !a.clickable));
        let promoted_id = answers(reducer.host()).last().expect("candidate").id.clone();

        reducer.handle(&prompt_complete("root", "Done.", 4.0));

        let all = answers(reducer.host());
        assert_eq!(all.len(), 2);
        let final_answer = all
            .iter()
            .find(|a| a.id == promoted_id)
            .expect("promoted answer");
        assert_eq!(answer_text(final_answer), "Done.");
        assert_eq!(final_answer.evidence_refs, evidence);
        assert!(final_answer.clickable);
        // The earlier intermediate prose remains once; the final is replaced in place.
        let done_count = all.iter().filter(|a| answer_text(a) == "Done.").count();
        assert_eq!(done_count, 1);
    }

    /// Pins Python `test_stream_then_durable_close_never_replays_raw_final_markdown`.
    ///
    /// Real ordering: stream ends, durable text lands, PromptComplete promotes it.
    #[test]
    fn test_stream_then_durable_close_never_replays_raw_final_markdown() {
        let mut reducer = make_reducer("chat");
        let response = "## Result\n\n**Done.**";
        reducer.handle(&prompt_submit("root", "do it", 1.0));
        reducer.handle(&ev::UIEvent::StreamBlockStart(ev::StreamBlockStart {
            session_id: "root".to_string(),
            block_type: "text".to_string(),
            ts: 2.0,
            ..ev::StreamBlockStart::default()
        }));
        reducer.handle(&ev::UIEvent::StreamBlockDelta(ev::StreamBlockDelta {
            session_id: "root".to_string(),
            block_type: "text".to_string(),
            text: response.to_string(),
            ts: 2.1,
            ..ev::StreamBlockDelta::default()
        }));
        reducer.handle(&ev::UIEvent::StreamBlockEnd(ev::StreamBlockEnd {
            session_id: "root".to_string(),
            block_type: "text".to_string(),
            ts: 2.2,
            ..ev::StreamBlockEnd::default()
        }));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "text", "text": response}),
            2.3,
        ));

        let provisional = answers(reducer.host());
        assert_eq!(provisional.len(), 1);
        assert!(!provisional[0].clickable);
        let provisional_id = provisional[0].id.clone();
        assert!(!reducer
            .host()
            .blocks
            .iter()
            .any(|b| matches!(b, TranscriptBlock::Narration(_))));

        reducer.handle(&prompt_complete("root", response, 2.5));
        let final_answers = answers(reducer.host());
        assert_eq!(final_answers.len(), 1);
        assert_eq!(final_answers[0].id, provisional_id);
        assert!(final_answers[0].clickable);
        assert_eq!(answer_text(final_answers[0]).matches("Done.").count(), 1);
    }

    /// Pins Python `test_prompt_complete_appends_one_fallback_answer_without_durable_text`.
    #[test]
    fn test_prompt_complete_appends_one_fallback_answer_without_durable_text() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "answer me", 1.0));
        reducer.handle(&prompt_complete("root", "The final answer.", 2.0));

        let all = answers(reducer.host());
        assert_eq!(all.len(), 1);
        assert_eq!(answer_text(all[0]), "The final answer.");
    }

    /// Pins Python `test_explicit_demo_answer_is_not_duplicated_at_prompt_complete`.
    #[test]
    fn test_explicit_demo_answer_is_not_duplicated_at_prompt_complete() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "demo", 1.0));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "text", "text": "Scripted answer.", "demo_role": "answer"}),
            2.0,
        ));
        reducer.handle(&prompt_complete("root", "Scripted answer.", 3.0));

        let all = answers(reducer.host());
        assert_eq!(all.len(), 1);
        assert_eq!(answer_text(all[0]), "Scripted answer.");
    }

    /// Pins Python `test_foreign_session_execution_cannot_mutate_root_transcript_or_close_out`.
    #[test]
    fn test_foreign_session_execution_cannot_mutate_root_transcript_or_close_out() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "delegate", 1.0));
        reducer.handle(&ev::UIEvent::StreamBlockStart(ev::StreamBlockStart {
            session_id: "child".to_string(),
            parent_id: Some("root".to_string()),
            block_type: "text".to_string(),
            ts: 2.0,
            ..ev::StreamBlockStart::default()
        }));
        reducer.handle(&ev::UIEvent::StreamBlockDelta(ev::StreamBlockDelta {
            session_id: "child".to_string(),
            parent_id: Some("root".to_string()),
            block_type: "text".to_string(),
            text: "child".to_string(),
            ts: 2.1,
            ..ev::StreamBlockDelta::default()
        }));
        reducer.handle(&ev::UIEvent::StreamBlockEnd(ev::StreamBlockEnd {
            session_id: "child".to_string(),
            parent_id: Some("root".to_string()),
            block_type: "text".to_string(),
            ts: 2.2,
            ..ev::StreamBlockEnd::default()
        }));
        reducer.handle(&ev::UIEvent::ToolPre(ev::ToolPre {
            session_id: "child".to_string(),
            parent_id: Some("root".to_string()),
            tool_name: "bash".to_string(),
            tool_call_id: "child-call".to_string(),
            tool_input: obj(json!({"command": "cat secret"})),
            ts: 2.3,
            ..ev::ToolPre::default()
        }));
        reducer.handle(&ev::UIEvent::ToolPost(ev::ToolPost {
            session_id: "child".to_string(),
            parent_id: Some("root".to_string()),
            tool_name: "bash".to_string(),
            tool_call_id: "child-call".to_string(),
            tool_input: obj(json!({"command": "cat secret"})),
            result: obj(json!({"output": "child output"})),
            ts: 2.4,
            ..ev::ToolPost::default()
        }));
        reducer.handle(&content_end(
            "child",
            Some("root"),
            json!({"type": "text", "text": "child internal narration"}),
            2.5,
        ));
        reducer.handle(&ev::UIEvent::OrchestratorComplete(ev::OrchestratorComplete {
            session_id: "child".to_string(),
            parent_id: Some("root".to_string()),
            status: ev::OrchestratorStatus::Cancelled,
            ts: 2.6,
            ..ev::OrchestratorComplete::default()
        }));

        assert!(reducer.host().stream_events.is_empty());
        assert!(!kinds(reducer.host()).contains(&"tool_line"));
        assert!(!reducer.host().blocks.iter().any(|b| matches!(
            b,
            TranscriptBlock::Narration(n) if n.text == "child internal narration"
        )));

        reducer.handle(&prompt_complete("root", "Root answer.", 3.0));
        let all = answers(reducer.host());
        let texts: Vec<String> = all.iter().map(|a| answer_text(a)).collect();
        assert_eq!(texts, vec!["Root answer."]);
        assert!(last_rule(reducer.host()).label.ends_with(" · answer"));
    }

    /// Pins Python `test_real_turn_with_file_changes_ships`.
    #[test]
    fn test_real_turn_with_file_changes_ships() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("", "refactor the store", 1.0));
        reducer.handle(&ev::UIEvent::ProviderResponseUsage(
            ev::ProviderResponseUsage {
                input_tokens: 100,
                output_tokens: 3200,
                model: "fake".to_string(),
                ts: 2.0,
                ..ev::ProviderResponseUsage::default()
            },
        ));
        reducer.handle(&ev::UIEvent::PromptComplete(ev::PromptComplete {
            response: "done".to_string(),
            files_changed: 3,
            diffstat: "+142/−38".to_string(),
            tests_ok: Some(true),
            ts: 13.0,
            ..ev::PromptComplete::default()
        }));
        let rule = last_rule(reducer.host());
        assert!(rule.shipped);
        assert!(rule.label.ends_with("3 files · +142/−38 · tests ✔"));
        let recorded = reducer.ledger.turns().last().expect("recorded turn");
        assert_eq!(recorded.outcome.kind, OutcomeKind::Shipped);
        assert_eq!(recorded.outcome.files_changed, 3);
        assert_eq!(recorded.outcome.diffstat, "+142/−38");
        assert_eq!(recorded.outcome.tests_ok, Some(true));
        assert!(reducer.ledger.last_shipped()); // footer ▲ yield glyph
    }

    /// Pins Python `test_context_compaction_is_visible_and_persistent`.
    #[test]
    fn test_context_compaction_is_visible_and_persistent() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&ev::UIEvent::ContextCompacted(ev::ContextCompacted {
            before_tokens: 120_000,
            after_tokens: 60_000,
            before_messages: 42,
            after_messages: 23,
            strategy_level: 3,
            ..ev::ContextCompacted::default()
        }));
        let TranscriptBlock::Narration(narration) = reducer.host().blocks.last().expect("a block")
        else {
            panic!("expected Narration");
        };
        assert_eq!(
            narration.text,
            "Context compacted · 120,000 → 60,000 tokens · 42 → 23 messages · strategy 3"
        );
        assert_eq!(
            reducer.host().notices.last().map(String::as_str),
            Some(narration.text.as_str())
        );
    }

    /// Pins Python `test_real_turn_with_unpriceable_usage_marks_rule_cost_estimated`.
    ///
    /// Never lie: an unknown model with no cost_usd renders `~$` not `$0.00`.
    #[test]
    fn test_real_turn_with_unpriceable_usage_marks_rule_cost_estimated() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("", "ask the mystery model", 1.0));
        reducer.handle(&ev::UIEvent::ProviderResponseUsage(
            ev::ProviderResponseUsage {
                input_tokens: 100,
                output_tokens: 3200,
                model: "mystery-model-9000".to_string(),
                ts: 2.0,
                ..ev::ProviderResponseUsage::default()
            },
        ));
        reducer.handle(&prompt_complete("", "done", 13.0));
        let rule = last_rule(reducer.host());
        assert!(rule.label.contains("~$0.00"));
        assert!(
            reducer
                .ledger
                .turns()
                .last()
                .expect("recorded turn")
                .telemetry
                .estimated
        );
        // session-level flag feeds the footer's ~$ total
        assert_eq!(reducer.unpriced_usage, 1);
    }

    /// Pins Python `test_real_turn_with_priced_usage_keeps_plain_dollar`.
    #[test]
    fn test_real_turn_with_priced_usage_keeps_plain_dollar() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("", "priced turn", 1.0));
        reducer.handle(&ev::UIEvent::ProviderResponseUsage(
            ev::ProviderResponseUsage {
                input_tokens: 1000,
                output_tokens: 1000,
                model: "claude-sonnet-4".to_string(),
                ts: 2.0,
                ..ev::ProviderResponseUsage::default()
            },
        ));
        reducer.handle(&prompt_complete("", "done", 4.0));
        let rule = last_rule(reducer.host());
        assert!(!rule.label.contains("~$"));
        assert!(rule.label.contains("$0.02")); // 1k in + 1k out on the fallback table
        assert_eq!(reducer.unpriced_usage, 0);
    }

    /// Pins Python `test_real_turn_failed_tests_render_tests_cross`.
    #[test]
    fn test_real_turn_failed_tests_render_tests_cross() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("", "fix the flake", 1.0));
        reducer.handle(&ev::UIEvent::PromptComplete(ev::PromptComplete {
            response: "tried".to_string(),
            files_changed: 1,
            diffstat: "+4/−1".to_string(),
            tests_ok: Some(false),
            ts: 5.0,
            ..ev::PromptComplete::default()
        }));
        assert!(last_rule(reducer.host())
            .label
            .ends_with("1 file · +4/−1 · tests ✗"));
    }

    /// Pins Python `test_real_turn_without_file_changes_stays_answer_only`.
    #[test]
    fn test_real_turn_without_file_changes_stays_answer_only() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("", "explain the store", 1.0));
        reducer.handle(&prompt_complete("", "it stores", 4.0));
        let rule = last_rule(reducer.host());
        assert!(!rule.shipped);
        assert!(rule.label.ends_with(" · answer"));
        assert_eq!(
            reducer.ledger.turns().last().expect("turn").outcome.kind,
            OutcomeKind::Answer
        );
        assert!(!reducer.ledger.last_shipped());
    }

    /// Pins Python `test_real_plan_mode_turn_is_plan_ready`.
    #[test]
    fn test_real_plan_mode_turn_is_plan_ready() {
        let mut reducer = make_reducer("plan");
        reducer.handle(&prompt_submit("", "how should we do it?", 1.0));
        reducer.handle(&prompt_complete("", "plan", 3.0));
        let rule = last_rule(reducer.host());
        assert!(!rule.shipped);
        assert!(rule.label.ends_with(" · plan ready"));
        assert_eq!(
            reducer.ledger.turns().last().expect("turn").outcome.kind,
            OutcomeKind::PlanReady
        );
    }

    /// Pins Python `test_real_interrupted_turn_appends_recap_and_never_ships`.
    #[test]
    fn test_real_interrupted_turn_appends_recap_and_never_ships() {
        let prompt = "refactor the session store";
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("", prompt, 1.0));
        reducer.handle(&ev::UIEvent::CancelCompleted(ev::CancelCompleted {
            ts: 6.0,
            ..ev::CancelCompleted::default()
        }));
        // Even a cancelled turn that touched files must NOT count as shipped.
        reducer.handle(&ev::UIEvent::PromptComplete(ev::PromptComplete {
            response: String::new(),
            files_changed: 2,
            diffstat: "+9/−1".to_string(),
            tests_ok: None,
            ts: 7.0,
            ..ev::PromptComplete::default()
        }));
        let host = reducer.host();
        let rule = last_rule(host);
        assert!(!rule.shipped);
        assert!(rule.label.ends_with(" · interrupted"));
        assert_eq!(
            reducer.ledger.turns().last().expect("turn").outcome.kind,
            OutcomeKind::Interrupted
        );
        // The italic recap sits directly above the rule, demo shape exactly.
        let rule_index = host
            .blocks
            .iter()
            .position(|b| matches!(b, TranscriptBlock::TurnRule(_)))
            .expect("rule index");
        let TranscriptBlock::Answer(recap) = &host.blocks[rule_index - 1] else {
            panic!("expected recap Answer above the rule");
        };
        assert!(!recap.clickable);
        assert_eq!(recap.spans[0].text, "✳ ");
        assert_eq!(recap.spans[0].style_token, StyleToken::Dimmer);
        assert_eq!(
            recap.spans[1].text,
            "Interrupted. Goal: refactor the session store. \
             Context saved; resume or restate direction."
        );
        assert_eq!(recap.spans[1].style_token, StyleToken::Dim);
        assert!(recap.spans[1].italic);
        assert_eq!(
            host.notices.last().map(String::as_str),
            Some("turn interrupted · context saved")
        );
    }

    /// Pins Python `test_real_interrupted_recap_comes_from_orchestrator_cancelled_too`.
    #[test]
    fn test_real_interrupted_recap_comes_from_orchestrator_cancelled_too() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("", "build the thing", 1.0));
        reducer.handle(&ev::UIEvent::OrchestratorComplete(ev::OrchestratorComplete {
            status: ev::OrchestratorStatus::Cancelled,
            ts: 5.0,
            ..ev::OrchestratorComplete::default()
        }));
        reducer.handle(&prompt_complete("", "", 5.5));
        let host = reducer.host();
        let rule = last_rule(host);
        assert!(rule.label.ends_with(" · interrupted"));
        let rule_index = host
            .blocks
            .iter()
            .position(|b| matches!(b, TranscriptBlock::TurnRule(_)))
            .expect("rule index");
        let TranscriptBlock::Answer(recap) = &host.blocks[rule_index - 1] else {
            panic!("expected recap Answer above the rule");
        };
        assert!(recap.spans[1]
            .text
            .starts_with("Interrupted. Goal: build the thing."));
    }

    /// Pins Python `test_demo_spec_interrupted_close_out_adds_no_extra_recap`.
    ///
    /// The demo scripts its own recap event; the spec path must not add one.
    #[test]
    fn test_demo_spec_interrupted_close_out_adds_no_extra_recap() {
        let rule_label = "6s · 1.0k tok, 50% cached · $0.05 · interrupted";
        let mut reducer = TranscriptReducer::with_options(
            FakeHost::new("chat"),
            BlockIdAllocator::new(),
            OutcomeLedger::new(),
            LaneRegistry::new(),
            ReducerOptions {
                spec_lookup: Some(Box::new(move |_| {
                    Some(TurnSpec {
                        duration_ms: 6000,
                        tokens: 1000,
                        cached_pct: Some(50),
                        cost: dec("0.05"),
                        cost_after: dec("0.05"),
                        outcome: "interrupted".to_string(),
                        shipped: false,
                        rule_label: rule_label.to_string(),
                        checkpoint_label: "store refactor · interrupted".to_string(),
                    })
                })),
                pricing: pinned_pricing(),
                ..ReducerOptions::default()
            },
        );
        reducer.handle(&prompt_submit("", "refactor the store", 1.0));
        reducer.handle(&ev::UIEvent::CancelCompleted(ev::CancelCompleted {
            ts: 2.0,
            ..ev::CancelCompleted::default()
        }));
        reducer.handle(&prompt_complete("", "", 3.0));
        let host = reducer.host();
        let rule = last_rule(host);
        assert_eq!(rule.label, rule_label);
        let rule_index = host
            .blocks
            .iter()
            .position(|b| matches!(b, TranscriptBlock::TurnRule(_)))
            .expect("rule index");
        // Directly above the rule is the user line — no synthesized recap.
        assert!(!matches!(
            host.blocks[rule_index - 1],
            TranscriptBlock::Answer(_)
        ));
    }

    /// Pins Python `test_real_turn_mounts_working_line_immediately_and_ticks`.
    ///
    /// Supervisor feedback: spec-less (real) turns pulse from second zero.
    #[test]
    fn test_real_turn_mounts_working_line_immediately_and_ticks() {
        use crate::ui::transcript_render::render_block;

        fn line_text(line: &[Segment]) -> String {
            line.iter().map(|s| s.text.as_str()).collect()
        }

        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("s", "hi", 100.0));
        assert_eq!(kinds(reducer.host()), vec!["user_line", "working_status"]);

        // 1s heartbeat: wall clock bumps the seconds and the spinner pulses.
        reducer.tick(103.0);
        let TranscriptBlock::WorkingStatus(working) =
            reducer.host().blocks.last().expect("a block").clone()
        else {
            panic!("expected working_status last");
        };
        assert_eq!(working.spinner_frame, 1);
        let block: TranscriptBlock = working.into();
        let line = line_text(&render_block(&block, 200)[0]);
        assert!(line.contains("working · 3s"), "line was {line:?}");
        assert!(line.contains("1 agent"), "line was {line:?}");

        // A running tool shows as the active branch of the live tree beneath
        // the pulse (not inline); the static '1 agent' fallback drops away.
        reducer.handle(&tool_pre(
            "s",
            "t1",
            "bash",
            json!({"command": "uv run pytest -q"}),
            104.0,
        ));
        let working = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::WorkingStatus(w) => Some(w.clone()),
                _ => None,
            })
            .expect("working line");
        let activity_lines = working.activity_lines.clone();
        let block: TranscriptBlock = working.into();
        let rendered: Vec<String> = render_block(&block, 200)
            .iter()
            .map(|line| line_text(line))
            .collect();
        let joined = rendered.join("\n");
        assert!(joined.contains("$ uv run pytest -q")); // in the tree
        assert!(activity_lines.last().expect("a branch").running);
        assert!(!rendered[0].contains("1 agent")); // not inline on the pulse
        // ...and the pulse rides at the BOTTOM, under the newest content.
        assert_eq!(
            reducer.host().blocks.last().expect("a block").kind(),
            "working_status"
        );

        // A durable answer flushes the burst into a digest and clears the tree.
        reducer.handle(&tool_post(
            "s",
            "t1",
            "bash",
            json!({"command": "uv run pytest -q"}),
            json!({"output": "ok"}),
            105.0,
        ));
        reducer.handle(&content_end(
            "s",
            None,
            json!({"type": "text", "text": "done"}),
            106.0,
        ));
        let working = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::WorkingStatus(w) => Some(w),
                _ => None,
            })
            .expect("working line");
        assert!(working.activity_lines.is_empty()); // burst flushed — tree cleared
        let digest = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::ToolLine(t) if t.summary.starts_with("Ran") => Some(t),
                _ => None,
            })
            .expect("digest line");
        assert_eq!(digest.summary, "Ran 1 shell command");
    }

    /// DESIGN-SPEC §3 (live activity tree): "up to 3 recent ops render …
    /// beneath the working line (the in-flight op is dim, settled ops
    /// dimmer)" — the ring is capped at 3, newest last, with exactly the
    /// in-flight op flagged running.
    #[test]
    fn test_live_activity_tree_caps_at_three_recent_ops() {
        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("s", "big burst", 100.0));
        for i in 0..5 {
            let call = format!("t{i}");
            let path = format!("src/file{i}.py");
            reducer.handle(&tool_pre(
                "s",
                &call,
                "read_file",
                json!({ "path": path }),
                101.0 + i as f64,
            ));
            reducer.handle(&tool_post(
                "s",
                &call,
                "read_file",
                json!({ "path": path }),
                json!({"output": "ok"}),
                101.5 + i as f64,
            ));
        }
        // A sixth op stays in flight.
        reducer.handle(&tool_pre(
            "s",
            "t9",
            "bash",
            json!({"command": "uv run pytest -q"}),
            107.0,
        ));

        let working = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::WorkingStatus(w) => Some(w.clone()),
                _ => None,
            })
            .expect("working line");
        let branches = &working.activity_lines;
        assert_eq!(branches.len(), 3, "tree capped at 3 recent ops");
        let newest = branches.last().expect("a branch");
        assert!(newest.running, "in-flight op is the newest branch");
        assert!(newest.text.contains("uv run pytest -q"), "was {:?}", newest.text);
        assert!(
            branches[..2].iter().all(|b| !b.running),
            "settled ops are dim history"
        );
        // Newest-last order: the settled slots hold the most recent reads.
        assert!(branches[0].text.contains("file3"), "was {:?}", branches[0].text);
        assert!(branches[1].text.contains("file4"), "was {:?}", branches[1].text);
    }

    /// Pins Python `test_mixed_tool_burst_collapses_to_one_humanized_digest`.
    ///
    /// A run of many tools between answers is ONE line — not one per tool
    /// (DESIGN-SPEC §3): `Read 2 files · searched 1× · ran 1 shell command`
    /// with every op in the expandable body.
    #[test]
    fn test_mixed_tool_burst_collapses_to_one_humanized_digest() {
        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("s", "investigate", 0.0));

        let ops: Vec<(&str, Value)> = vec![
            ("read_file", json!({"file_path": "src/a.py"})),
            ("read_file", json!({"file_path": "src/b.py"})),
            ("grep", json!({"pattern": "TODO"})),
            ("bash", json!({"command": "uv run pytest -q"})),
        ];
        for (i, (tool, input)) in ops.iter().enumerate() {
            let cid = format!("t{i}");
            reducer.handle(&tool_pre("s", &cid, tool, input.clone(), 0.0));
            reducer.handle(&tool_post(
                "s",
                &cid,
                tool,
                input.clone(),
                json!({"output": "ok"}),
                0.0,
            ));
        }

        let digests: Vec<&ToolLine> = reducer
            .host()
            .blocks
            .iter()
            .filter_map(|b| match b {
                TranscriptBlock::ToolLine(t) => Some(t),
                _ => None,
            })
            .collect();
        assert_eq!(digests.len(), 1); // the whole burst is a single line
        let digest = digests[0];
        assert_eq!(
            digest.summary,
            "Read 2 files · searched 1× · ran 1 shell command"
        );
        // every op is preserved in the (collapsed) expandable body
        assert_eq!(
            digest.body,
            vec!["read a.py", "read b.py", "searched TODO", "$ uv run pytest -q"]
        );
        // live tree beneath the pulse is bounded to the most recent ops
        let working = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::WorkingStatus(w) => Some(w),
                _ => None,
            })
            .expect("working line");
        assert!(working.activity_lines.len() <= 3);
    }

    // =====================================================================
    // tests/test_ui_reducer_replay.py
    // =====================================================================

    /// One real shipped turn, as the runtime would have persisted it.
    fn one_turn_events() -> Vec<ev::UIEvent> {
        vec![
            prompt_submit(SID, "fix the bug", 0.0),
            tool_pre(SID, "c1", "bash", json!({"command": "uv run pytest -q"}), 1.0),
            tool_post(
                SID,
                "c1",
                "bash",
                json!({"command": "uv run pytest -q"}),
                json!({"success": true, "output": "ok"}),
                2.0,
            ),
            ev::UIEvent::ProviderResponseUsage(ev::ProviderResponseUsage {
                session_id: SID.to_string(),
                ts: 2.5,
                input_tokens: 100,
                output_tokens: 700,
                ..ev::ProviderResponseUsage::default()
            }),
            content_end(SID, None, json!({"type": "text", "text": "All done."}), 3.0),
            ev::UIEvent::PromptComplete(ev::PromptComplete {
                session_id: SID.to_string(),
                ts: 4.0,
                response: "All done.".to_string(),
                files_changed: 2,
                diffstat: "+10/−2".to_string(),
                tests_ok: Some(true),
                ..ev::PromptComplete::default()
            }),
        ]
    }

    /// Pins Python `test_replay_rebuilds_digest_answer_and_shipped_rule`.
    #[test]
    fn test_replay_rebuilds_digest_answer_and_shipped_rule() {
        let mut reducer = make_reducer("chat");
        assert!(reducer.replay(&one_turn_events(), 1, Decimal::ZERO));

        let ks = kinds(reducer.host());
        assert!(ks.contains(&"user_line"));
        assert!(ks.contains(&"tool_line")); // the burst digest, not prose-only replay
        assert!(ks.contains(&"answer"));
        assert!(ks.contains(&"turn_rule"));
        assert!(!ks.contains(&"working_status")); // the pulse never survives replay
        let digest = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::ToolLine(t) => Some(t),
                _ => None,
            })
            .expect("digest");
        assert_eq!(digest.summary, "Ran 1 shell command");
        let rule = last_rule(reducer.host());
        assert!(rule.shipped);
        assert!(rule.label.contains("2 files") && rule.label.contains("tests ✔"));
        assert!(rule.label.contains("0.7k tok")); // telemetry from the stored usage event

        // Checkpoint math stays the existing resume math (spec §9): the one
        // replayed turn IS user message 1, and the next live turn continues.
        let turn_ids: Vec<u64> = reducer
            .ledger
            .checkpoints()
            .iter()
            .map(|c| c.turn_id)
            .collect();
        assert_eq!(turn_ids, vec![1]);
        reducer.handle(&prompt_submit(SID, "next", 10.0));
        reducer.handle(&prompt_complete(SID, "ok", 11.0));
        let turn_ids: Vec<u64> = reducer
            .ledger
            .checkpoints()
            .iter()
            .map(|c| c.turn_id)
            .collect();
        assert_eq!(turn_ids, vec![1, 2]);
    }

    /// Pins Python `test_replay_suppresses_every_interactive_side_effect`.
    #[test]
    fn test_replay_suppresses_every_interactive_side_effect() {
        let mut reducer = make_reducer("chat");
        let mut events = one_turn_events();
        // Skipped kinds mixed in, as a real log would carry them.
        events.push(ev::UIEvent::StreamBlockDelta(ev::StreamBlockDelta {
            session_id: SID.to_string(),
            ts: 1.5,
            text: "partial".to_string(),
            ..ev::StreamBlockDelta::default()
        }));
        events.push(ev::UIEvent::Notification(ev::Notification {
            session_id: SID.to_string(),
            ts: 1.6,
            message: "mode plan · read-only".to_string(),
            source: "mode".to_string(),
            ..ev::Notification::default()
        }));
        events.push(ev::UIEvent::Notification(ev::Notification {
            session_id: SID.to_string(),
            ts: 1.7,
            message: "decision deferred".to_string(),
            source: "needs_you".to_string(),
            ..ev::Notification::default()
        }));
        events.push(ev::UIEvent::ApprovalRequired(ev::ApprovalRequired {
            session_id: SID.to_string(),
            ts: 1.8,
            prompt: "Allow rm?".to_string(),
            options: vec!["Deny".to_string()],
            ..ev::ApprovalRequired::default()
        }));
        events.push(ev::UIEvent::ProviderNotice(ev::ProviderNotice {
            session_id: SID.to_string(),
            ts: 1.9,
            notice: ev::NoticeKind::Retry,
            message: "throttled".to_string(),
            ..ev::ProviderNotice::default()
        }));
        assert!(reducer.replay(&events, 1, Decimal::ZERO));
        let host = reducer.host();
        assert!(host.notices.is_empty());
        assert!(host.stream_events.is_empty());
        assert!(host.approvals.is_empty());
        assert!(host.deferred.is_empty());
        assert!(host.turn_events.is_empty()); // no timers/bells/queue drains from history
        assert_eq!(host.lanes_changed_calls, 1); // exactly the one final repaint
    }

    /// Pins Python `test_replay_closes_a_dangling_turn_as_interrupted`.
    ///
    /// A log that ends mid-turn (crash/kill) settles like a live Esc did.
    #[test]
    fn test_replay_closes_a_dangling_turn_as_interrupted() {
        let mut reducer = make_reducer("chat");
        let events = vec![
            prompt_submit(SID, "never finished", 0.0),
            tool_pre(SID, "c1", "bash", json!({}), 1.0),
        ];
        assert!(reducer.replay(&events, 1, Decimal::ZERO));
        assert!(!reducer.running());
        let rule = last_rule(reducer.host());
        assert!(rule.label.contains("interrupted"));
        let recap_texts: Vec<String> = answers(reducer.host())
            .iter()
            .map(|a| answer_text(a))
            .collect();
        assert!(recap_texts.iter().any(|t| t.contains("Interrupted.")));
    }

    /// Pins Python `test_replay_degrades_ledger_on_transcript_mismatch`.
    ///
    /// Post-rewind ghost turns / truncated logs: events.jsonl is
    /// append-only while a confirmed fork trims the context, so the
    /// replayed checkpoint chain can disagree with the restored
    /// transcript's user-message count. The blocks stay as scrollback but
    /// the checkpoints are dropped — forking through them would slice the
    /// live context at the wrong turns.
    #[test]
    fn test_replay_degrades_ledger_on_transcript_mismatch() {
        let mut reducer = make_reducer("chat");
        let two_turns = vec![
            prompt_submit(SID, "turn one", 0.0),
            prompt_complete(SID, "one", 1.0),
            prompt_submit(SID, "ghost turn (forked away)", 2.0),
            prompt_complete(SID, "two", 3.0),
        ];
        assert!(reducer.replay(&two_turns, 1, Decimal::ZERO));
        assert!(reducer.ledger.checkpoints().is_empty());
        assert_eq!(reducer.turn_base, 1); // new checkpoints use the transcript base
        let rule_count = reducer
            .host()
            .blocks
            .iter()
            .filter(|b| matches!(b, TranscriptBlock::TurnRule(_)))
            .count();
        assert_eq!(rule_count, 2);
        reducer.handle(&prompt_submit(SID, "next", 10.0));
        reducer.handle(&prompt_complete(SID, "ok", 11.0));
        let turn_ids: Vec<u64> = reducer
            .ledger
            .checkpoints()
            .iter()
            .map(|c| c.turn_id)
            .collect();
        assert_eq!(turn_ids, vec![2]);
    }

    /// Pins Python `test_replay_without_a_turn_reports_false_and_touches_nothing`.
    ///
    /// No prompt_submit in the log (foreign/absent events file) → the
    /// caller falls back to the prose restored_history path.
    #[test]
    fn test_replay_without_a_turn_reports_false_and_touches_nothing() {
        let mut reducer = make_reducer("chat");
        reducer.turn_base = 5;
        reducer.session_cost = dec("2.50");
        let events = vec![ev::UIEvent::Notification(ev::Notification {
            session_id: SID.to_string(),
            ts: 0.0,
            message: "stale".to_string(),
            ..ev::Notification::default()
        })];
        assert!(!reducer.replay(&events, 9, dec("9")));
        assert!(reducer.host().blocks.is_empty());
        assert_eq!(reducer.turn_base, 5);
        assert_eq!(reducer.session_cost, dec("2.50"));
    }

    /// Pins Python `test_replay_rebuilds_delegate_summary_lane_transcript_and_plan`.
    #[test]
    fn test_replay_rebuilds_delegate_summary_lane_transcript_and_plan() {
        let mut reducer = make_reducer("chat");
        let events = vec![
            prompt_submit(SID, "fan out", 0.0),
            tool_pre(
                SID,
                "t1",
                "todo",
                json!({"todos": [{"content": "step", "status": "completed"}]}),
                1.0,
            ),
            tool_pre(
                SID,
                "d1",
                "delegate",
                json!({"agent": "researcher", "instruction": "dig in"}),
                2.0,
            ),
            agent_spawned("researcher", "sub1", 2.5),
            content_end(
                "sub1",
                Some(SID),
                json!({"type": "text", "text": "found it"}),
                3.0,
            ),
            agent_completed("researcher", "sub1", 4.0, true, "1 finding"),
            prompt_complete(SID, "delegated work done", 5.0),
        ];
        assert!(reducer.replay(&events, 1, Decimal::ZERO));

        let host = reducer.host();
        let blocks = summaries(host);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].entries.len(), 1);
        let entry = &blocks[0].entries[0];
        assert_eq!(entry.agent, "researcher");
        assert_eq!(entry.state, DelegateState::Done);
        assert_eq!(entry.snippet, "1 finding");
        assert_eq!(
            blocks[0].plan_final,
            Some(vec![TodoItem {
                content: "step".to_string(),
                status: TodoStatus::Completed,
            }])
        );

        let lane_blocks = reducer.lane_transcript("sub1").expect("lane transcript");
        let lane_texts = block_texts(&lane_blocks);
        assert!(lane_texts.iter().any(|t| t.contains("found it")));
        assert!(!reducer.host().plan_changes.is_empty()); // restored ambient plan state (spec §2/D3)
        assert!(reducer
            .lanes()
            .lanes()
            .iter()
            .all(|record| record.lane.state == LaneStateName::Done));
    }

    /// Pins Python `test_replay_settles_lanes_the_log_never_completed`.
    ///
    /// A crashed session's dangling lane must not tick wall-clock forever.
    #[test]
    fn test_replay_settles_lanes_the_log_never_completed() {
        let mut reducer = make_reducer("chat");
        let events = vec![
            prompt_submit(SID, "fan out", 0.0),
            agent_spawned("coder", "sub9", 1.0),
        ];
        assert!(reducer.replay(&events, 1, Decimal::ZERO));
        assert!(reducer
            .lanes()
            .lanes()
            .iter()
            .all(|record| record.lane.state == LaneStateName::Done));
    }

    /// Pins Python `test_replay_reconciles_cost_to_the_kernel_baseline`.
    ///
    /// restore_session_cost stays the single cost authority on resume —
    /// replay's own accumulation is presentation-level and never adds on top.
    #[test]
    fn test_replay_reconciles_cost_to_the_kernel_baseline() {
        let mut reducer = make_reducer("chat");
        assert!(reducer.replay(&one_turn_events(), 1, dec("1.23")));
        assert_eq!(reducer.session_cost, dec("1.23"));
    }

    /// Pins Python `test_replay_stamps_historical_mode_on_the_user_line`.
    ///
    /// The stored prompt_submit carries the posture the turn ran under, so
    /// replay stamps that HISTORICAL mode badge — not the current live one.
    #[test]
    fn test_replay_stamps_historical_mode_on_the_user_line() {
        let mut reducer = make_reducer("chat"); // live mode is 'chat'
        let events = vec![
            ev::UIEvent::PromptSubmit(ev::PromptSubmit {
                session_id: SID.to_string(),
                ts: 0.0,
                prompt: "draft the plan".to_string(),
                mode: "plan".to_string(),
                ..ev::PromptSubmit::default()
            }),
            prompt_complete(SID, "planned", 1.0),
        ];
        assert!(reducer.replay(&events, 1, Decimal::ZERO));
        let user_line = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::UserLine(line) => Some(line),
                _ => None,
            })
            .expect("user line");
        assert_eq!(user_line.mode, "plan"); // recorded posture, not the live 'chat'
    }

    /// Pins Python `test_replay_falls_back_to_live_mode_on_legacy_logs`.
    ///
    /// Pre-stamp logs have no mode field; the badge falls back to the live
    /// posture rather than an empty/blank badge (backward compatible).
    #[test]
    fn test_replay_falls_back_to_live_mode_on_legacy_logs() {
        let mut reducer = make_reducer("chat");
        reducer.host_mut().mode_id = "auto".to_string();
        let events = vec![
            prompt_submit(SID, "legacy turn", 0.0), // mode == ""
            prompt_complete(SID, "done", 1.0),
        ];
        assert!(reducer.replay(&events, 1, Decimal::ZERO));
        let user_line = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::UserLine(line) => Some(line),
                _ => None,
            })
            .expect("user line");
        assert_eq!(user_line.mode, "auto");
    }

    /// Pins Python `test_live_turn_prefers_event_mode_over_host_posture`.
    ///
    /// Live dispatch honours the event's stamped mode too, so the durable
    /// user line matches the posture at submit even if the app later flips.
    #[test]
    fn test_live_turn_prefers_event_mode_over_host_posture() {
        let mut reducer = make_reducer("chat");
        reducer.host_mut().mode_id = "auto".to_string();
        reducer.handle(&ev::UIEvent::PromptSubmit(ev::PromptSubmit {
            session_id: SID.to_string(),
            ts: 0.0,
            prompt: "build it".to_string(),
            mode: "build".to_string(),
            ..ev::PromptSubmit::default()
        }));
        let user_line = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::UserLine(line) => Some(line),
                _ => None,
            })
            .expect("user line");
        assert_eq!(user_line.mode, "build");
    }

    // =====================================================================
    // tests/test_ui_reducer_steer_turns.py
    // =====================================================================

    fn run_turn(reducer: &mut TranscriptReducer<FakeHost>, prompt: &str, injections: usize) {
        reducer.handle(&prompt_submit("", prompt, 1.0));
        for _ in 0..injections {
            reducer.handle(&ev::UIEvent::ContextInjected(ev::ContextInjected::default()));
        }
        reducer.handle(&prompt_complete("", "", 2.0));
    }

    fn checkpoint_turn_ids(reducer: &TranscriptReducer<FakeHost>) -> Vec<u64> {
        reducer
            .ledger
            .checkpoints()
            .iter()
            .map(|c| c.turn_id)
            .collect()
    }

    /// Pins Python `test_plain_turns_keep_sequential_turn_ids`.
    #[test]
    fn test_plain_turns_keep_sequential_turn_ids() {
        let mut reducer = make_reducer("chat");
        run_turn(&mut reducer, "one", 0);
        run_turn(&mut reducer, "two", 0);
        assert_eq!(checkpoint_turn_ids(&reducer), vec![1, 2]);
    }

    /// Pins Python `test_steer_injection_shifts_checkpoint_to_last_user_message`.
    ///
    /// Turn 2 consumes one steer → its transcript is [U1, A1, U2, partial,
    /// U-steer, final]; the checkpoint must address user message 3 (the
    /// steer) so a fork keeps the whole steered turn (spec §9).
    #[test]
    fn test_steer_injection_shifts_checkpoint_to_last_user_message() {
        let mut reducer = make_reducer("chat");
        run_turn(&mut reducer, "one", 0);
        run_turn(&mut reducer, "two", 1);
        run_turn(&mut reducer, "three", 0);
        assert_eq!(checkpoint_turn_ids(&reducer), vec![1, 3, 4]);
    }

    /// Pins Python `test_multiple_injections_accumulate`.
    #[test]
    fn test_multiple_injections_accumulate() {
        let mut reducer = make_reducer("chat");
        run_turn(&mut reducer, "one", 2); // steer + answered decision steps
        run_turn(&mut reducer, "two", 0);
        assert_eq!(checkpoint_turn_ids(&reducer), vec![3, 4]);
    }

    /// Pins Python `test_turn_base_offsets_resume_history_before_first_checkpoint`.
    #[test]
    fn test_turn_base_offsets_resume_history_before_first_checkpoint() {
        let mut reducer = make_reducer("chat");
        reducer.turn_base = 5; // resumed session: 5 user messages restored
        run_turn(&mut reducer, "one", 1);
        assert_eq!(checkpoint_turn_ids(&reducer), vec![7]);
    }

    /// Pins Python `test_trim_rewinds_turn_ids_past_dropped_injections`.
    #[test]
    fn test_trim_rewinds_turn_ids_past_dropped_injections() {
        let mut reducer = make_reducer("chat");
        run_turn(&mut reducer, "one", 0);
        run_turn(&mut reducer, "two", 1);
        reducer.ledger.trim_to("t1").expect("known checkpoint"); // confirmed fork back to turn 1
        run_turn(&mut reducer, "two-b", 0);
        assert_eq!(checkpoint_turn_ids(&reducer), vec![1, 2]);
    }

    // =====================================================================
    // tests/test_ui_reducer_thinking.py
    // =====================================================================

    fn thinking_blocks(host: &FakeHost) -> Vec<&Thinking> {
        host.blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::Thinking(thinking) => Some(thinking),
                _ => None,
            })
            .collect()
    }

    fn thinking_start(ts: f64) -> ev::UIEvent {
        ev::UIEvent::ContentBlockStart(ev::ContentBlockStart {
            session_id: "root".to_string(),
            block_type: "thinking".to_string(),
            ts,
            ..ev::ContentBlockStart::default()
        })
    }

    /// Pins Python `test_start_then_end_populates_one_collapsed_block_in_place`.
    #[test]
    fn test_start_then_end_populates_one_collapsed_block_in_place() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "think", 1.0));
        reducer.handle(&thinking_start(2.0));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "thinking", "thinking": "weigh A vs B\npick A"}),
            2.5,
        ));
        let blocks = thinking_blocks(reducer.host());
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].text, "weigh A vs B\npick A");
        assert!(!blocks[0].expanded); // default collapsed, Claude-Code style
    }

    /// Pins Python `test_thinking_prefers_thinking_field_over_text`.
    #[test]
    fn test_thinking_prefers_thinking_field_over_text() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "think", 1.0));
        reducer.handle(&thinking_start(2.0));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "thinking", "thinking": "real reasoning", "text": "ignored"}),
            2.5,
        ));
        assert_eq!(thinking_blocks(reducer.host())[0].text, "real reasoning");
    }

    /// Pins Python `test_thinking_falls_back_to_text_key`.
    #[test]
    fn test_thinking_falls_back_to_text_key() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "think", 1.0));
        reducer.handle(&thinking_start(2.0));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "thinking", "text": "text-key reasoning"}),
            2.5,
        ));
        assert_eq!(thinking_blocks(reducer.host())[0].text, "text-key reasoning");
    }

    /// Pins Python `test_withheld_thinking_keeps_the_block_with_empty_text`.
    ///
    /// Honest degradation: core withheld the prose (visibility LLM_ONLY),
    /// the payload arrives empty — the block survives rather than vanishing.
    #[test]
    fn test_withheld_thinking_keeps_the_block_with_empty_text() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "think", 1.0));
        reducer.handle(&thinking_start(2.0));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "thinking"}), // no thinking/text: withheld
            2.5,
        ));
        let blocks = thinking_blocks(reducer.host());
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].text, "");
    }

    /// Pins Python `test_thinking_end_without_start_appends_defensively`.
    ///
    /// Non-streaming provider (no start): the end alone still lands a block.
    #[test]
    fn test_thinking_end_without_start_appends_defensively() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "think", 1.0));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "thinking", "thinking": "standalone"}),
            2.0,
        ));
        let blocks = thinking_blocks(reducer.host());
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].text, "standalone");
    }

    /// Pins Python `test_thinking_does_not_bleed_into_answer_channel`.
    ///
    /// A thinking content block must never be treated as durable answer text.
    #[test]
    fn test_thinking_does_not_bleed_into_answer_channel() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit("root", "think", 1.0));
        reducer.handle(&thinking_start(2.0));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "thinking", "thinking": "private"}),
            2.5,
        ));
        reducer.handle(&content_end(
            "root",
            None,
            json!({"type": "text", "text": "The answer."}),
            3.0,
        ));
        let texts: Vec<String> = answers(reducer.host())
            .iter()
            .map(|a| answer_text(a))
            .collect();
        assert_eq!(texts, vec!["The answer."]);
        assert!(!texts.join("").contains("private"));
    }

    // =====================================================================
    // tests/test_ui_lane_summary.py (`_lane_result_summary`, #91)
    // =====================================================================

    /// Pins Python `test_strips_heading_and_inline_markdown`.
    #[test]
    fn test_strips_heading_and_inline_markdown() {
        let raw = "## What Amplifier attractors do\n\n**Core concept.** An attractor is a workflow.";
        let out = lane_result_summary(raw, 52);
        assert!(out.starts_with("What Amplifier attractors do"));
        assert!(!out.contains("##") && !out.contains("**"));
    }

    /// Pins Python `test_takes_first_nonempty_line`.
    #[test]
    fn test_takes_first_nonempty_line() {
        assert_eq!(
            lane_result_summary("\n\n  # Title line  \nbody", 52),
            "Title line"
        );
    }

    /// Pins Python `test_prefers_first_sentence_when_long`.
    #[test]
    fn test_prefers_first_sentence_when_long() {
        let raw =
            "An attractor is a multi-stage AI workflow defined as a DOT graph. More detail follows here.";
        let out = lane_result_summary(raw, 80);
        assert_eq!(
            out,
            "An attractor is a multi-stage AI workflow defined as a DOT graph"
        );
        assert!(!out.contains("More detail"));
    }

    /// Pins Python `test_unwraps_links_and_truncates`.
    #[test]
    fn test_unwraps_links_and_truncates() {
        assert_eq!(
            lane_result_summary("see [the docs](http://x)", 52),
            "see the docs"
        );
        let long = "x".repeat(200);
        assert!(lane_result_summary(&long, 52).chars().count() <= 52);
    }

    /// Pins Python `test_empty_result_is_empty`.
    #[test]
    fn test_empty_result_is_empty() {
        assert_eq!(lane_result_summary("", 52), "");
        assert_eq!(lane_result_summary("   \n  ", 52), "");
    }

    // =====================================================================
    // tests/test_ui_lanes_telemetry.py — TranscriptReducer-pinned cases
    // =====================================================================

    /// Pins Python `test_usage_routes_child_tokens_to_lane_but_not_root`.
    #[test]
    fn test_usage_routes_child_tokens_to_lane_but_not_root() {
        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("root", "fan out", 1.0));
        reducer.lanes_mut().register(
            "child",
            Some("root"),
            "coder",
            RegisterOptions {
                now: 1.0,
                ..RegisterOptions::default()
            },
        );

        // Usage stamped with the child session lands on the child lane AND
        // the session/turn totals.
        reducer.handle(&ev::UIEvent::ProviderResponseUsage(
            ev::ProviderResponseUsage {
                session_id: "child".to_string(),
                output_tokens: 1000,
                cost_usd: Some(dec("0.20")),
                ts: 2.0,
                ..ev::ProviderResponseUsage::default()
            },
        ));
        let child = reducer.lanes().get("child").expect("child lane");
        assert_eq!(child.lane.tokens, 1000);
        assert_eq!(child.lane.cost, dec("0.20"));
        assert_eq!(reducer.total_tokens, 1000);
        assert_eq!(reducer.turn.as_ref().expect("running turn").tokens, 1000);

        // Usage stamped with the ROOT session touches no lane (root is
        // never a registered lane) but still increments the session/turn
        // totals.
        reducer.handle(&ev::UIEvent::ProviderResponseUsage(
            ev::ProviderResponseUsage {
                session_id: "root".to_string(),
                output_tokens: 500,
                ts: 3.0,
                ..ev::ProviderResponseUsage::default()
            },
        ));
        assert_eq!(
            reducer.lanes().get("child").expect("child lane").lane.tokens,
            1000 // unchanged
        );
        assert_eq!(reducer.total_tokens, 1500);
        assert_eq!(reducer.turn.as_ref().expect("running turn").tokens, 1500);
    }

    /// Pins Python `test_usage_without_cost_usd_falls_back_to_estimate`.
    #[test]
    fn test_usage_without_cost_usd_falls_back_to_estimate() {
        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("root", "go", 1.0));
        reducer.lanes_mut().register(
            "child",
            Some("root"),
            "coder",
            RegisterOptions {
                now: 1.0,
                ..RegisterOptions::default()
            },
        );
        reducer.handle(&ev::UIEvent::ProviderResponseUsage(
            ev::ProviderResponseUsage {
                session_id: "child".to_string(),
                input_tokens: 100,
                output_tokens: 2000,
                model: "fake".to_string(),
                ts: 2.0,
                ..ev::ProviderResponseUsage::default()
            },
        ));
        let lane = reducer.lanes().get("child").expect("child lane");
        assert_eq!(lane.lane.tokens, 2000); // tokens are the requirement
        assert!(lane.lane.cost >= Decimal::ZERO); // cost best-effort (0 when unpriceable)
    }

    /// Pins Python `test_child_session_start_reconciles_redacted_spawn_id_for_live_usage`.
    ///
    /// Foundation may redact the spawn id but expose the real id on session:start.
    #[test]
    fn test_child_session_start_reconciles_redacted_spawn_id_for_live_usage() {
        let mut reducer = make_reducer("auto");
        let root = "root-session";
        let redacted = "[REDACTED:PII]-a7b97feb6f684d29_foundation-explorer";
        let actual = "0000000000000000-a7b97feb6f684d29_foundation-explorer";
        reducer.handle(&prompt_submit(root, "fan out", 1.0));
        reducer.handle(&ev::UIEvent::AgentSpawned(ev::AgentSpawned {
            session_id: root.to_string(),
            parent_session_id: root.to_string(),
            sub_session_id: redacted.to_string(),
            agent: "foundation:explorer".to_string(),
            ts: 2.0,
            ..ev::AgentSpawned::default()
        }));
        reducer.handle(&ev::UIEvent::SessionStart(ev::SessionStart {
            session_id: actual.to_string(),
            parent_id: Some(root.to_string()),
            ts: 2.1,
            ..ev::SessionStart::default()
        }));
        reducer.handle(&ev::UIEvent::ProviderResponseUsage(
            ev::ProviderResponseUsage {
                session_id: actual.to_string(),
                output_tokens: 9904,
                cost_usd: Some(dec("1.9752735")),
                ts: 3.0,
                ..ev::ProviderResponseUsage::default()
            },
        ));

        let lane = reducer.lanes().get(actual).expect("lane by actual id");
        assert_eq!(lane.session_id, actual); // lane focus now has a usable session id
        assert_eq!(lane.lane.tokens, 9904);
        assert_eq!(lane.lane.cost, dec("1.9752735"));
        // completion's redacted id remains an alias
        assert_eq!(reducer.lanes().get(redacted), Some(lane));

        reducer.handle(&ev::UIEvent::AgentCompleted(ev::AgentCompleted {
            session_id: root.to_string(),
            parent_session_id: root.to_string(),
            sub_session_id: redacted.to_string(),
            agent: "foundation:explorer".to_string(),
            success: true,
            ts: 4.0,
            ..ev::AgentCompleted::default()
        }));
        assert_eq!(
            reducer.lanes().get(actual).expect("lane").lane.state,
            LaneStateName::Done
        );
    }

    /// Pins Python `test_redacted_lane_reconciliation_tolerates_session_start_race`.
    #[test]
    fn test_redacted_lane_reconciliation_tolerates_session_start_race() {
        let mut reducer = make_reducer("auto");
        let root = "root-session";
        let redacted = "[REDACTED:PII]-abcdef1234567890_foundation-explorer";
        let actual = "0000000000000000-abcdef1234567890_foundation-explorer";
        reducer.handle(&prompt_submit(root, "fan out", 1.0));
        reducer.handle(&ev::UIEvent::SessionStart(ev::SessionStart {
            session_id: actual.to_string(),
            parent_id: Some(root.to_string()),
            ts: 1.5,
            ..ev::SessionStart::default()
        }));
        reducer.handle(&ev::UIEvent::AgentSpawned(ev::AgentSpawned {
            session_id: root.to_string(),
            parent_session_id: root.to_string(),
            sub_session_id: redacted.to_string(),
            agent: "foundation:explorer".to_string(),
            ts: 2.0,
            ..ev::AgentSpawned::default()
        }));
        let lane = reducer.lanes().get(actual).expect("lane by actual id");
        assert_eq!(lane.session_id, actual);
    }

    /// Pins Python `test_live_session_cost_moves_before_turn_close`.
    #[test]
    fn test_live_session_cost_moves_before_turn_close() {
        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("root", "go", 1.0));
        reducer.handle(&ev::UIEvent::ProviderResponseUsage(
            ev::ProviderResponseUsage {
                session_id: "root".to_string(),
                output_tokens: 500,
                cost_usd: Some(dec("0.75")),
                ts: 2.0,
                ..ev::ProviderResponseUsage::default()
            },
        ));

        assert_eq!(reducer.session_cost, Decimal::ZERO); // checkpoint total commits at close
        assert_eq!(reducer.live_session_cost(), dec("0.75"));
        assert!(!reducer.live_cost_estimated());
    }

    /// Pins Python `test_tick_advances_lanes_and_fires_lanes_changed`.
    #[test]
    fn test_tick_advances_lanes_and_fires_lanes_changed() {
        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("root", "fan out", 100.0));
        reducer.lanes_mut().register(
            "child",
            Some("root"),
            "coder",
            RegisterOptions {
                now: 100.0,
                ..RegisterOptions::default()
            },
        );
        let before = reducer.host().lanes_changed_calls;

        reducer.tick(105.0);
        assert_eq!(
            reducer.lanes().get("child").expect("child lane").lane.elapsed,
            5.0
        );
        assert!(reducer.host().lanes_changed_calls > before);
    }

    /// Pins Python `test_child_events_stream_compact_activity_into_lane_and_tree`.
    #[test]
    fn test_child_events_stream_compact_activity_into_lane_and_tree() {
        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("root", "fan out", 1.0));
        reducer.handle(&ev::UIEvent::AgentSpawned(ev::AgentSpawned {
            session_id: "root".to_string(),
            parent_session_id: "root".to_string(),
            sub_session_id: "child".to_string(),
            agent: "foundation:explorer".to_string(),
            ts: 2.0,
            ..ev::AgentSpawned::default()
        }));

        reducer.handle(&tool_pre(
            "child",
            "read-1",
            "read_file",
            json!({"file_path": "/repo/README.md"}),
            3.0,
        ));
        let lane = reducer.lanes().get("child").expect("child lane");
        assert_eq!(lane.lane.state, LaneStateName::Working);
        assert_eq!(lane.lane.activity, "reading README.md");
        // The in-transcript agent-tree activity ticker is retired
        // (ambient-progress D5) — live child activity now lives only on the
        // lane (asserted above); the LanesPanel is the activity surface.
        assert!(answers(reducer.host()).is_empty());

        reducer.handle(&tool_pre(
            "child",
            "edit-1",
            "edit_file",
            json!({
                "file_path": "/repo/src/app.py",
                "old_string": "old_value = 1",
                "new_string": "new_value = 2",
            }),
            3.5,
        ));
        reducer.handle(&tool_post(
            "child",
            "edit-1",
            "edit_file",
            json!({}),
            json!({"success": true}),
            3.6,
        ));
        let changes = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::ToolLine(t) => Some(t),
                _ => None,
            })
            .expect("changes row");
        assert_eq!(changes.summary, "Changed 1 file");
        assert_eq!(changes.body_style, ToolLineBodyStyle::Diff);
        assert!(changes
            .body
            .contains(&"foundation:explorer · edit file · /repo/src/app.py".to_string()));
        assert!(changes.body.contains(&"-old_value = 1".to_string()));
        assert!(changes.body.contains(&"+new_value = 2".to_string()));

        let patch = [
            "*** Begin Patch",
            "*** Update File: src/one.py",
            "-old",
            "+new",
            "*** Add File: src/two.py",
            "+created",
            "*** End Patch",
        ]
        .join("\n");
        reducer.handle(&tool_pre(
            "child",
            "patch-1",
            "apply_patch",
            json!({"patch": patch}),
            3.7,
        ));
        reducer.handle(&tool_post(
            "child",
            "patch-1",
            "apply_patch",
            json!({}),
            json!({"success": true}),
            3.8,
        ));
        let changes = reducer
            .host()
            .blocks
            .iter()
            .find_map(|b| match b {
                TranscriptBlock::ToolLine(t) => Some(t),
                _ => None,
            })
            .expect("changes row");
        assert_eq!(changes.summary, "Changed 3 files");
        assert!(changes
            .body
            .contains(&"foundation:explorer · apply patch · src/one.py, src/two.py".to_string()));

        reducer.handle(&ev::UIEvent::StreamBlockStart(ev::StreamBlockStart {
            session_id: "child".to_string(),
            block_type: "text".to_string(),
            request_id: "r1".to_string(),
            ts: 4.0,
            ..ev::StreamBlockStart::default()
        }));
        let lane = reducer.lanes().get("child").expect("child lane");
        assert_eq!(lane.lane.state, LaneStateName::Running);
        assert_eq!(lane.lane.activity, "writing response");
        let tool_lines = reducer
            .host()
            .blocks
            .iter()
            .filter(|b| matches!(b, TranscriptBlock::ToolLine(_)))
            .count();
        assert_eq!(tool_lines, 1);
    }

    // =====================================================================
    // tests/test_ui_lane_steering.py — reducer-owned case
    // =====================================================================

    /// Pins Python `test_delivery_echo_lands_in_the_lanes_focus_transcript`.
    #[test]
    fn test_delivery_echo_lands_in_the_lanes_focus_transcript() {
        let mut reducer = make_reducer("chat");
        reducer.handle(&prompt_submit(SID, "fan out", 0.0));
        reducer.handle(&agent_spawned("researcher", "s1", 1.0));
        // The runtime's _lane_steer_applied emits exactly this child-stamped
        // narration when it delivers a lane steer at the child's step boundary.
        reducer.handle(&content_end(
            "s1",
            Some(SID),
            json!({
                "type": "text",
                "text": "Applying steer: focus on the tests",
                "demo_role": "narration",
            }),
            2.0,
        ));
        let blocks = reducer.lane_transcript("s1").expect("lane transcript");
        let prose = block_texts(&blocks).join("\n");
        assert!(prose.contains("Applying steer: focus on the tests"));
    }

    // =====================================================================
    // tests/test_ui_transcript_render.py — reducer-owned case
    // =====================================================================

    /// Pins Python `test_todo_tool_reroutes_to_plan_changed_never_the_transcript`.
    ///
    /// Design 2026-07-21 D1/D3: the todo tool feeds the plan panel via
    /// host.plan_changed(); no TodoBlock, no tool_line, no digest entry.
    #[test]
    fn test_todo_tool_reroutes_to_plan_changed_never_the_transcript() {
        let mut reducer = make_reducer("auto");
        reducer.handle(&prompt_submit("s", "do it", 0.0));

        let todo_call = |reducer: &mut TranscriptReducer<FakeHost>, cid: &str, statuses: &[&str]| {
            let todos: Vec<Value> = statuses
                .iter()
                .enumerate()
                .map(|(i, st)| {
                    json!({
                        "content": format!("step {i}"),
                        "status": st,
                        "activeForm": format!("doing {i}"),
                    })
                })
                .collect();
            let input = json!({"operation": "update", "todos": todos});
            reducer.handle(&tool_pre("s", cid, "todo", input.clone(), 1.0));
            reducer.handle(&tool_post(
                "s",
                cid,
                "todo",
                input,
                json!({"status": "ok"}),
                1.0,
            ));
        };

        todo_call(&mut reducer, "t1", &["in_progress", "pending"]);
        todo_call(&mut reducer, "t2", &["completed", "in_progress"]);
        // a 'list' op carries no todos — must not fire plan_changed
        reducer.handle(&tool_pre("s", "t3", "todo", json!({"operation": "list"}), 2.0));

        let host = reducer.host();
        assert_eq!(host.plan_changes.len(), 2); // one push per create/update call
        let last = host.plan_changes.last().expect("plan change");
        let statuses: Vec<TodoStatus> = last.iter().map(|i| i.status).collect();
        assert_eq!(statuses, vec![TodoStatus::Completed, TodoStatus::InProgress]);
        let contents: Vec<&str> = last.iter().map(|i| i.content.as_str()).collect();
        assert_eq!(contents, vec!["step 0", "step 1"]);
        // never in the transcript, never in the activity digest
        assert!(!host
            .blocks
            .iter()
            .any(|b| matches!(b, TranscriptBlock::ToolLine(_))));
    }
}
