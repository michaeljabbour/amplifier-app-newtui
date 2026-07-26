//! Trust resolution: mode → capability → allow/ask/deny (DESIGN-SPEC §4).
//!
//! Port of `src/amplifier_app_newtui/model/trust.py`. Semantics originate in
//! amplifier-app-cli `ui/governance.py` + `interaction_state.py`, collapsed to
//! the five spec modes:
//!
//! - **plan** — read-only: reads auto-allow, everything else denied.
//! - **brainstorm** — no tools: everything denied.
//! - **chat** — ask everything except reads.
//! - **build** — auto read/test; ask write/net/spend (and exec, which may
//!   touch any of those).
//! - **auto** — auto read/write; other capabilities are *classifier-gated*:
//!   [`resolve`] returns `ask` with `classifier_gated=true` and the kernel
//!   governance hook routes those through the reasoning-blind classifier
//!   (deny-and-continue on denial, DESIGN-SPEC §7).
//!
//! Deny is never a halt: the governance hook converts a `deny` decision into
//! a synthesized tool result + a Blocked transcript block, and the turn
//! continues (deny-and-continue). [`DenialLog`] counts denials and flags
//! escalation at 3 consecutive / 20 total, feeding the needs-you queue.

use std::fmt;
use std::sync::OnceLock;
use std::time::Instant;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// The capability a tool call exercises — the unit trust is granted in.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CapabilityClass {
    #[serde(rename = "read")]
    Read,
    #[serde(rename = "write")]
    Write,
    #[serde(rename = "net")]
    Net,
    #[serde(rename = "test")]
    Test,
    #[serde(rename = "spend")]
    Spend,
    #[serde(rename = "exec")]
    Exec,
    #[serde(rename = "outside-project")]
    OutsideProject,
}

impl CapabilityClass {
    /// The string value of the Python `str`-enum member.
    pub fn value(self) -> &'static str {
        match self {
            CapabilityClass::Read => "read",
            CapabilityClass::Write => "write",
            CapabilityClass::Net => "net",
            CapabilityClass::Test => "test",
            CapabilityClass::Spend => "spend",
            CapabilityClass::Exec => "exec",
            CapabilityClass::OutsideProject => "outside-project",
        }
    }
}

impl fmt::Display for CapabilityClass {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.value())
    }
}

/// `Decision = Literal["allow", "ask", "deny"]`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Decision {
    #[serde(rename = "allow")]
    Allow,
    #[serde(rename = "ask")]
    Ask,
    #[serde(rename = "deny")]
    Deny,
}

impl Decision {
    pub fn value(self) -> &'static str {
        match self {
            Decision::Allow => "allow",
            Decision::Ask => "ask",
            Decision::Deny => "deny",
        }
    }
}

impl fmt::Display for Decision {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.value())
    }
}

/// The outcome of resolving one tool call against the active mode.
///
/// - `decision`: allow (run silently), ask (approval bar), deny
///   (deny-and-continue with a Blocked block).
/// - `capability`: the classified capability the decision applied to.
/// - `reason`: short human explanation (surfaces in notices/blocks).
/// - `classifier_gated`: true only in auto mode for capabilities the static
///   table cannot settle — the kernel must run the two-stage classifier
///   before acting on `decision` (which is the fail-closed fallback if the
///   classifier is unavailable).
///
/// Frozen in Python (`frozen=True, extra="forbid"`): treated as immutable by
/// convention here; unknown fields are rejected on deserialization.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TrustDecision {
    pub decision: Decision,
    pub capability: CapabilityClass,
    pub reason: String,
    #[serde(default)]
    pub classifier_gated: bool,
}

impl TrustDecision {
    pub fn allowed(&self) -> bool {
        self.decision == Decision::Allow
    }
}

// Explicit tool-name -> capability table (declared config first; the
// substring heuristic below is only a fallback — RESEARCH-BRIEF risk 10).
fn tool_capability_table(name: &str) -> Option<CapabilityClass> {
    Some(match name {
        "read_file" | "list_files" | "glob" | "grep" | "search" => CapabilityClass::Read,
        "write_file" | "edit_file" | "apply_patch" | "create_file" | "delete_file" => {
            CapabilityClass::Write
        }
        "web_fetch" | "web_search" | "http_request" => CapabilityClass::Net,
        "run_tests" => CapabilityClass::Test,
        "task" | "spawn_agent" => CapabilityClass::Spend,
        "bash" | "shell" | "exec" | "exec_command" => CapabilityClass::Exec,
        _ => return None,
    })
}

const READ_HINTS: &[&str] = &[
    "read", "list", "glob", "grep", "search", "find", "cat", "view", "load",
];
const WRITE_HINTS: &[&str] = &[
    "write", "edit", "patch", "create", "delete", "move", "rename", "mkdir",
];
const NET_HINTS: &[&str] = &["web", "http", "fetch", "url", "download", "browse"];
const TEST_HINTS: &[&str] = &["test", "pytest", "check"];
const SPEND_HINTS: &[&str] = &["task", "agent", "spawn", "delegate"];
const EXEC_HINTS: &[&str] = &["bash", "shell", "exec", "command", "run"];

// Shell command prefixes that make an exec call effectively a test run.
const TEST_COMMAND_MARKERS: &[&str] = &[
    "pytest",
    "uv run pytest",
    "python -m pytest",
    "npm test",
    "npm run test",
    "cargo test",
    "go test",
    "make test",
];

/// Python truthiness for a JSON value (mirrors `tool_input.get(...) or ...`).
fn json_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `str(value)` for the command lookup (only strings matter for the markers).
fn json_to_command_string(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        other => other.to_string(),
    }
}

/// Classify one tool call into a [`CapabilityClass`].
///
/// Order: explicit table → exec-command test sniffing → name-substring
/// heuristic → EXEC (the most restrictive default: exec asks in every
/// non-auto mode, so misclassification fails safe).
pub fn classify_tool(tool_name: &str, tool_input: Option<&Map<String, Value>>) -> CapabilityClass {
    let name = tool_name.trim().to_lowercase();
    let capability = tool_capability_table(&name).unwrap_or_else(|| {
        let hint_groups: [(&[&str], CapabilityClass); 6] = [
            (NET_HINTS, CapabilityClass::Net),
            (TEST_HINTS, CapabilityClass::Test),
            (SPEND_HINTS, CapabilityClass::Spend),
            (WRITE_HINTS, CapabilityClass::Write),
            (READ_HINTS, CapabilityClass::Read),
            (EXEC_HINTS, CapabilityClass::Exec),
        ];
        hint_groups
            .iter()
            .find(|(hints, _)| hints.iter().any(|hint| name.contains(hint)))
            .map(|&(_, hinted)| hinted)
            .unwrap_or(CapabilityClass::Exec)
    });
    if capability == CapabilityClass::Exec {
        // Python: `if tool_input:` — an empty mapping is falsy and skipped.
        if let Some(input) = tool_input.filter(|map| !map.is_empty()) {
            let raw = input
                .get("command")
                .filter(|v| json_truthy(v))
                .or_else(|| input.get("cmd"))
                .map(json_to_command_string)
                .unwrap_or_default();
            let command = raw.trim();
            if !command.is_empty()
                && TEST_COMMAND_MARKERS.iter().any(|marker| {
                    command == *marker || command.starts_with(&format!("{marker} "))
                })
            {
                return CapabilityClass::Test;
            }
        }
    }
    capability
}

// Auto is a strict superset of build's auto set (read+test) plus write —
// amplifier's natural wide scope. NET/SPEND/EXEC stay classifier-gated.
const AUTO_STATIC_ALLOW: &[CapabilityClass] = &[
    CapabilityClass::Read,
    CapabilityClass::Write,
    CapabilityClass::Test,
];

// Per-mode static policy: capability -> decision. Missing key = "ask"
// (never silently widen an incomplete table — governance.py invariant).
fn static_policy(mode: &str, capability: CapabilityClass) -> Decision {
    match mode {
        "plan" => {
            if capability == CapabilityClass::Read {
                Decision::Allow
            } else {
                Decision::Deny
            }
        }
        "brainstorm" => Decision::Deny,
        "build" => match capability {
            CapabilityClass::Read | CapabilityClass::Test => Decision::Allow,
            CapabilityClass::Write
            | CapabilityClass::Net
            | CapabilityClass::Spend
            | CapabilityClass::Exec => Decision::Ask,
            // Missing key in the Python table = "ask".
            CapabilityClass::OutsideProject => Decision::Ask,
        },
        // "chat" and unknown modes (the safest interactive default).
        _ => {
            if capability == CapabilityClass::Read {
                Decision::Allow
            } else {
                Decision::Ask
            }
        }
    }
}

/// Resolve one tool call against a mode's trust posture.
///
/// Unknown modes resolve with chat's posture (the safest interactive default:
/// ask everything but reads). In auto mode, capabilities outside the static
/// read/write allowance come back `ask` + `classifier_gated=true` — the caller
/// must run the classifier and treat this decision as the fail-closed
/// fallback.
pub fn resolve(
    mode: &str,
    tool_name: &str,
    tool_input: Option<&Map<String, Value>>,
) -> TrustDecision {
    let capability = classify_tool(tool_name, tool_input);
    resolve_capability(mode, capability)
}

/// Resolve an already-classified capability against a mode.
///
/// The kernel uses this after concrete path inspection identifies an
/// outside-project action. The mode table remains the single policy source.
pub fn resolve_capability(mode: &str, capability: CapabilityClass) -> TrustDecision {
    if mode == "auto" {
        if AUTO_STATIC_ALLOW.contains(&capability) {
            return TrustDecision {
                decision: Decision::Allow,
                capability,
                reason: format!("auto {} · bypasses classification", capability.value()),
                classifier_gated: false,
            };
        }
        return TrustDecision {
            decision: Decision::Ask,
            capability,
            reason: format!("{} has real downside · asks if risky", capability.value()),
            classifier_gated: true,
        };
    }
    let decision = static_policy(mode, capability);
    let reason = match decision {
        Decision::Allow => format!("auto {}", capability.value()),
        Decision::Ask => format!("ask {}", capability.value()),
        Decision::Deny => format!("blocked {} · {} mode", capability.value(), mode),
    };
    TrustDecision {
        decision,
        capability,
        reason,
        classifier_gated: false,
    }
}

/// Errors from [`DenialLog`] operations (Python raises `ValueError`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TrustValueError(pub String);

impl fmt::Display for TrustValueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TrustValueError {}

/// One recorded denial with its escalation bookkeeping.
///
/// Frozen in Python; treated as immutable by convention here.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DenialRecord {
    pub denial_id: String,
    pub capability: CapabilityClass,
    pub action: String,
    pub reason: String,
    pub created_at: f64,
    pub consecutive_count: u64,
    pub total_count: u64,
    #[serde(default)]
    pub escalation_reasons: Vec<String>,
}

impl DenialRecord {
    /// True when this denial crossed an escalation threshold — the governance
    /// hook must surface a needs-you decision.
    pub fn escalation_due(&self) -> bool {
        !self.escalation_reasons.is_empty()
    }
}

fn monotonic_seconds() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// Deny-and-continue accounting with escalation thresholds.
///
/// Ported from amplifier-app-cli `ui/governance.py`: 3 consecutive denials or
/// 20 total denials trigger escalation (a needs-you question asking the human
/// to review the pattern). [`DenialLog::record_non_denial`] resets the
/// consecutive counter on any allowed/asked action.
pub struct DenialLog {
    consecutive_threshold: u64,
    total_threshold: u64,
    clock: Box<dyn Fn() -> f64 + Send>,
    records: Vec<DenialRecord>,
    consecutive: u64,
    total: u64,
}

impl DenialLog {
    const MAX_RETAINED: usize = 1_000;

    /// Python constructor defaults: `consecutive_threshold=3`,
    /// `total_threshold=20`, `clock=time.monotonic`.
    pub fn new() -> Self {
        Self::with_config(3, 20, Box::new(monotonic_seconds))
            .expect("default thresholds are positive")
    }

    /// Full constructor; errors (Python `ValueError`) on non-positive
    /// thresholds.
    pub fn with_config(
        consecutive_threshold: u64,
        total_threshold: u64,
        clock: Box<dyn Fn() -> f64 + Send>,
    ) -> Result<Self, TrustValueError> {
        if consecutive_threshold < 1 || total_threshold < 1 {
            return Err(TrustValueError(
                "denial thresholds must be positive".to_string(),
            ));
        }
        Ok(Self {
            consecutive_threshold,
            total_threshold,
            clock,
            records: Vec::new(),
            consecutive: 0,
            total: 0,
        })
    }

    pub fn records(&self) -> &[DenialRecord] {
        &self.records
    }

    pub fn consecutive_count(&self) -> u64 {
        self.consecutive
    }

    pub fn total_count(&self) -> u64 {
        self.total
    }

    /// Record one denial; the returned record says whether to escalate.
    /// Errors (Python `ValueError`) when the reason is empty/whitespace.
    pub fn record_denial(
        &mut self,
        capability: CapabilityClass,
        action: &str,
        reason: &str,
    ) -> Result<DenialRecord, TrustValueError> {
        let clean_reason = reason.split_whitespace().collect::<Vec<_>>().join(" ");
        if clean_reason.is_empty() {
            return Err(TrustValueError("denial reason is required".to_string()));
        }
        self.consecutive += 1;
        self.total += 1;
        let mut triggers: Vec<String> = Vec::new();
        if self.consecutive == self.consecutive_threshold {
            triggers.push(format!(
                "{} consecutive denials",
                self.consecutive_threshold
            ));
        }
        if self.total == self.total_threshold {
            triggers.push(format!("{} total denials", self.total_threshold));
        }
        let record = DenialRecord {
            denial_id: format!("denial-{}", self.total),
            capability,
            action: action.split_whitespace().collect::<Vec<_>>().join(" "),
            reason: clean_reason,
            created_at: (self.clock)(),
            consecutive_count: self.consecutive,
            total_count: self.total,
            escalation_reasons: triggers,
        };
        self.records.push(record.clone());
        if self.records.len() > Self::MAX_RETAINED {
            let excess = self.records.len() - Self::MAX_RETAINED;
            self.records.drain(..excess);
        }
        Ok(record)
    }

    /// Reset the consecutive-denial streak (any allow/ask outcome).
    pub fn record_non_denial(&mut self) {
        self.consecutive = 0;
    }
}

impl Default for DenialLog {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn input(value: Value) -> Map<String, Value> {
        value.as_object().expect("test input is an object").clone()
    }

    // --- trust resolution (DESIGN-SPEC §4 gating) ----------------------------

    #[test]
    fn test_plan_is_read_only() {
        assert_eq!(resolve("plan", "read_file", None).decision, Decision::Allow);
        assert_eq!(resolve("plan", "grep", None).decision, Decision::Allow);
        for tool in ["write_file", "bash", "web_fetch", "task", "run_tests"] {
            assert_eq!(resolve("plan", tool, None).decision, Decision::Deny, "{tool}");
        }
    }

    #[test]
    fn test_brainstorm_has_no_tools() {
        for tool in ["read_file", "write_file", "bash", "web_fetch", "task"] {
            assert_eq!(
                resolve("brainstorm", tool, None).decision,
                Decision::Deny,
                "{tool}"
            );
        }
    }

    #[test]
    fn test_chat_asks_everything_except_reads() {
        assert_eq!(resolve("chat", "read_file", None).decision, Decision::Allow);
        for tool in ["write_file", "bash", "web_fetch", "task", "run_tests"] {
            assert_eq!(resolve("chat", tool, None).decision, Decision::Ask, "{tool}");
        }
    }

    #[test]
    fn test_build_auto_read_test_ask_write_net_spend() {
        assert_eq!(resolve("build", "read_file", None).decision, Decision::Allow);
        assert_eq!(resolve("build", "run_tests", None).decision, Decision::Allow);
        for tool in ["write_file", "web_fetch", "task"] {
            assert_eq!(resolve("build", tool, None).decision, Decision::Ask, "{tool}");
        }
    }

    #[test]
    fn test_build_exec_test_command_is_auto() {
        // A shell call running the test suite classifies as TEST → auto in build.
        let decision = resolve("build", "bash", Some(&input(json!({"command": "pytest -q"}))));
        assert_eq!(decision.capability, CapabilityClass::Test);
        assert_eq!(decision.decision, Decision::Allow);
        // A non-test shell command stays exec → ask.
        assert_eq!(
            resolve("build", "bash", Some(&input(json!({"command": "rm -rf build"})))).decision,
            Decision::Ask
        );
    }

    #[test]
    fn test_auto_mode_static_allows_and_classifier_gates() {
        assert_eq!(resolve("auto", "read_file", None).decision, Decision::Allow);
        assert_eq!(resolve("auto", "write_file", None).decision, Decision::Allow);
        for tool in ["bash", "web_fetch", "task"] {
            let decision = resolve("auto", tool, None);
            assert_eq!(decision.decision, Decision::Ask, "{tool}");
            assert!(decision.classifier_gated, "{tool}");
        }
        assert!(!resolve("auto", "read_file", None).classifier_gated);
    }

    #[test]
    fn test_unknown_mode_uses_chat_posture() {
        assert_eq!(resolve("bogus", "read_file", None).decision, Decision::Allow);
        assert_eq!(resolve("bogus", "write_file", None).decision, Decision::Ask);
    }

    #[test]
    fn test_unknown_tool_fails_safe_as_exec() {
        assert_eq!(classify_tool("mystery_widget", None), CapabilityClass::Exec);
        assert_eq!(resolve("build", "mystery_widget", None).decision, Decision::Ask);
    }

    #[test]
    fn test_classify_tool_table_and_heuristics() {
        assert_eq!(classify_tool("read_file", None), CapabilityClass::Read);
        assert_eq!(classify_tool("web_fetch", None), CapabilityClass::Net);
        assert_eq!(classify_tool("spawn_agent", None), CapabilityClass::Spend);
        // heuristic
        assert_eq!(classify_tool("fancy_file_search", None), CapabilityClass::Read);
    }

    // --- denial log escalation (3 consecutive / 20 total) --------------------

    #[test]
    fn test_denial_log_escalates_at_three_consecutive() {
        let mut log = DenialLog::new();
        let records: Vec<DenialRecord> = (0..3)
            .map(|i| {
                log.record_denial(CapabilityClass::Exec, &format!("cmd{i}"), "blocked")
                    .expect("valid denial")
            })
            .collect();
        assert!(!records[0].escalation_due());
        assert!(!records[1].escalation_due());
        assert!(records[2].escalation_due());
        assert_eq!(
            records[2].escalation_reasons,
            vec!["3 consecutive denials".to_string()]
        );
    }

    #[test]
    fn test_non_denial_resets_consecutive_streak() {
        let mut log = DenialLog::new();
        for i in 0..2 {
            log.record_denial(CapabilityClass::Exec, &format!("a{i}"), "r")
                .expect("valid denial");
        }
        log.record_non_denial();
        let record = log
            .record_denial(CapabilityClass::Exec, "b", "r")
            .expect("valid denial");
        assert_eq!(record.consecutive_count, 1);
        assert!(!record.escalation_due());
    }

    #[test]
    fn test_denial_log_escalates_at_twenty_total() {
        let mut log = DenialLog::new();
        let mut escalations: Vec<Vec<String>> = Vec::new();
        for i in 0..20 {
            log.record_non_denial(); // keep the consecutive streak at 1
            let record = log
                .record_denial(CapabilityClass::Net, &format!("fetch{i}"), "r")
                .expect("valid denial");
            escalations.push(record.escalation_reasons);
        }
        assert_eq!(
            escalations.last().expect("twenty records"),
            &vec!["20 total denials".to_string()]
        );
        assert!(escalations[..escalations.len() - 1]
            .iter()
            .all(|reasons| reasons.is_empty()));
    }

    #[test]
    fn test_denial_log_requires_reason() {
        let mut log = DenialLog::new();
        assert_eq!(
            log.record_denial(CapabilityClass::Exec, "x", "   "),
            Err(TrustValueError("denial reason is required".to_string()))
        );
    }
}
