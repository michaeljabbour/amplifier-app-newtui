//! Per-turn yield evidence from the normalized event stream.
//!
//! Port of `src/amplifier_app_newtui/kernel/turn_yield.py` (itself ported
//! from amplifier-app-cli `ui/turn_outcomes.py`): the `tests ✔` heuristic
//! watches the turn's tool results for test-runner commands (pytest /
//! npm test / …) and reports whether every one of them succeeded (exited
//! 0). The runtime feeds every emitted event through
//! [`TurnYieldTracker::observe`] and resets the tracker at each submit —
//! subagent tool results count too, exactly like the reference
//! implementation's cross-session tool snapshot.
//!
//! Kernel-pure: consumes typed [`UIEvent`]s only; no UI, no amplifier-core.

use std::collections::HashMap;

use serde_json::{Map, Value};

use crate::kernel::events::{ToolError, ToolPost, ToolPre, UIEvent};

const TEST_MARKERS: [&str; 4] = ["pytest", "npm test", "uv run pytest", "test runner"];

const FAILED_STATUSES: [&str; 3] = ["denied", "error", "failed"];

/// Python's `str(value)` for the payload scalars we encounter here.
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        // Containers: JSON text stands in for Python's repr (untested
        // shapes — commands and statuses are strings in practice).
        other => other.to_string(),
    }
}

/// `str(mapping.get(key, ""))` — the Python idiom used for `command` and
/// `status` lookups (a present-but-null value str()s to "None"; absent
/// keys default to the empty string).
fn str_get(mapping: &Map<String, Value>, key: &str) -> String {
    match mapping.get(key) {
        Some(Value::Null) => "None".to_string(),
        Some(value) => py_str(value),
        None => String::new(),
    }
}

/// Return whether a tool activity represents a real shell command.
pub fn is_shell_tool_name(name: &str) -> bool {
    // Python: str(name).strip().lower().rsplit(":", maxsplit=1)[-1]
    let lowered = name.trim().to_lowercase();
    let normalized = lowered
        .rsplit(':')
        .next()
        .unwrap_or(lowered.as_str())
        .replace('-', "_");
    matches!(
        normalized.as_str(),
        "bash" | "exec" | "exec_command" | "run_command" | "shell"
    ) || normalized.ends_with("_bash")
        || normalized.ends_with("_exec_command")
        || normalized.ends_with("_shell")
}

fn is_test_activity(tool_name: &str, command: &str) -> bool {
    let haystack = format!("{tool_name} {command}").to_lowercase();
    TEST_MARKERS.iter().any(|marker| haystack.contains(marker))
}

/// A tool:post counts as success unless its result says otherwise.
///
/// Reference semantics (turn_outcomes.py): tool:post terminal status is
/// `succeeded`, tool:error is `failed`. The normalized result dict
/// additionally carries denial/exit information when the runtime has it.
fn post_succeeded(result: &Map<String, Value>) -> bool {
    let status = str_get(result, "status").to_lowercase();
    if FAILED_STATUSES.contains(&status.as_str()) {
        return false;
    }
    for key in ["exit_code", "returncode", "exit_status"] {
        // Python: `isinstance(code, int) and not isinstance(code, bool)` —
        // serde_json numbers stored as floats fail `as_i64`, and booleans
        // are a distinct Value variant, matching the bool exclusion.
        if let Some(Value::Number(code)) = result.get(key) {
            if let Some(code) = code.as_i64() {
                return code == 0;
            }
        }
    }
    true
}

/// Accumulates one turn's test-run evidence from tool events.
#[derive(Debug, Default)]
pub struct TurnYieldTracker {
    test_results: Vec<bool>,
    pending_commands: HashMap<String, String>,
}

impl TurnYieldTracker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn start_turn(&mut self) {
        self.test_results = Vec::new();
        self.pending_commands = HashMap::new();
    }

    pub fn observe(&mut self, event: &UIEvent) {
        match event {
            UIEvent::ToolPre(pre) => self.observe_pre(pre),
            UIEvent::ToolPost(post) => self.observe_post(post),
            UIEvent::ToolError(error) => self.observe_error(error),
            _ => {}
        }
    }

    fn observe_pre(&mut self, event: &ToolPre) {
        // Remember the command so a later tool:error (which carries no
        // input) can still be classified as a failed test run.
        let command = str_get(&event.tool_input, "command");
        if !command.is_empty() && !event.tool_call_id.is_empty() {
            self.pending_commands
                .insert(event.tool_call_id.clone(), command);
        }
    }

    fn observe_post(&mut self, event: &ToolPost) {
        let mut command = str_get(&event.tool_input, "command");
        if command.is_empty() {
            command = self
                .pending_commands
                .remove(&event.tool_call_id)
                .unwrap_or_default();
        }
        if is_test_activity(&event.tool_name, &command) {
            self.test_results.push(post_succeeded(&event.result));
        }
    }

    fn observe_error(&mut self, event: &ToolError) {
        let command = self
            .pending_commands
            .remove(&event.tool_call_id)
            .unwrap_or_default();
        if is_test_activity(&event.tool_name, &command) {
            self.test_results.push(false);
        }
    }

    /// `Some(true)`/`Some(false)` when test commands ran this turn;
    /// `None` when none did (Python's `bool | None` property).
    pub fn tests_ok(&self) -> Option<bool> {
        if self.test_results.is_empty() {
            return None;
        }
        Some(self.test_results.iter().all(|ok| *ok))
    }
}

#[cfg(test)]
mod tests {
    //! Pins the tracker cases from `tests/test_kernel_turn_yield.py`
    //! (the git cases there live in kernel/git_yield.rs; the
    //! RealRuntime/bridge cases belong to the runtime unit).

    use super::*;
    use serde_json::json;

    fn obj(value: Value) -> Map<String, Value> {
        match value {
            Value::Object(map) => map,
            _ => panic!("payload literal must be a JSON object"),
        }
    }

    fn tool_post(tool_call_id: &str, tool_input: Value, result: Value) -> UIEvent {
        UIEvent::ToolPost(ToolPost {
            tool_name: "bash".to_string(),
            tool_call_id: tool_call_id.to_string(),
            tool_input: obj(tool_input),
            result: obj(result),
            ..ToolPost::default()
        })
    }

    #[test]
    fn test_tracker_reports_none_without_test_commands() {
        let mut tracker = TurnYieldTracker::new();
        tracker.start_turn();
        tracker.observe(&tool_post("c1", json!({"command": "ls"}), json!({})));
        assert_eq!(tracker.tests_ok(), None);
    }

    #[test]
    fn test_tracker_marks_passing_pytest_run() {
        let mut tracker = TurnYieldTracker::new();
        tracker.start_turn();
        tracker.observe(&tool_post(
            "c1",
            json!({"command": "uv run pytest -q"}),
            json!({"exit_code": 0}),
        ));
        assert_eq!(tracker.tests_ok(), Some(true));
    }

    #[test]
    fn test_tracker_marks_failing_and_errored_test_runs() {
        let mut tracker = TurnYieldTracker::new();
        tracker.start_turn();
        tracker.observe(&tool_post(
            "c1",
            json!({"command": "pytest tests/"}),
            json!({"exit_code": 1}),
        ));
        assert_eq!(tracker.tests_ok(), Some(false));

        tracker.start_turn(); // tool:error path correlates via the tool:pre command
        tracker.observe(&UIEvent::ToolPre(ToolPre {
            tool_name: "bash".to_string(),
            tool_call_id: "c2".to_string(),
            tool_input: obj(json!({"command": "npm test"})),
            ..ToolPre::default()
        }));
        tracker.observe(&UIEvent::ToolError(ToolError {
            tool_name: "bash".to_string(),
            tool_call_id: "c2".to_string(),
            error_message: "boom".to_string(),
            ..ToolError::default()
        }));
        assert_eq!(tracker.tests_ok(), Some(false));

        tracker.start_turn(); // reset clears prior evidence
        assert_eq!(tracker.tests_ok(), None);
    }
}
