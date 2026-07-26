//! Task status tracker: agent lanes from `task:agent_*` / `delegate:*` events.
//!
//! Port of `src/amplifier_app_newtui/kernel/trackers/task_status.py`.
//!
//! Hook-tracker pattern feeding a [`LaneRegistry`] — lanes are keyed by
//! `session_id` and routed by `parent_id` (the entire routing key, stamped
//! on every payload by `hooks.set_default_fields`).
//!
//! Race tolerance (RESEARCH-BRIEF risk 5): `session:start` can race
//! `task:agent_spawned`, and a grandchild's spawn event can arrive before
//! its parent's — registration is idempotent and the LaneRegistry
//! retro-patches depths when a missing parent appears. Legacy `task:spawned`
//! / `task:completed` names are adapted at the normalize boundary.
//!
//! Divergences from the Python original (recorded honestly):
//!
//! - `register_hooks` is not ported: the crate has no hooks registry —
//!   hook plumbing stays in the Python backend behind `serve` and the
//!   protocol client feeds [`TaskStatusTracker::consume`] directly.
//!   [`TaskStatusTracker::EVENTS`] (the subscription list it would
//!   register) is kept and pinned by tests.
//! - Python's async `handle_event` is the synchronous
//!   [`TaskStatusTracker::handle_event`] per the migration conventions.
//! - Python's `add_listener` returns a remove-closure; Rust returns a
//!   [`ListenerHandle`] consumed by [`TaskStatusTracker::remove_listener`]
//!   (same semantics, borrow-checker-friendly shape). `_notify`'s
//!   crash-isolation (`try/except` around each listener) is not
//!   replicated — a panicking listener propagates.

use serde_json::Value;

use crate::kernel::events::{normalize, Payload, UIEvent};
use crate::model::lanes::{LaneRecord, LaneRegistry, LaneStateName, RegisterOptions};

/// Minimal private mirror of amplifier-core's `HookResult` — this tracker
/// only ever answers `HookResult(action="continue")`.
///
/// No in-crate hook registry dispatches into `handle_event` yet (that
/// plumbing stays Python-side behind `serve`), so outside of tests this
/// is intentionally dormant — kept to pin the hook-entry contract.
#[allow(dead_code)]
#[derive(Clone, Debug, PartialEq, Eq)]
struct HookResult {
    action: &'static str,
}

impl HookResult {
    #[allow(dead_code)]
    fn cont() -> Self {
        HookResult { action: "continue" }
    }
}

/// Opaque handle returned by [`TaskStatusTracker::add_listener`] — the Rust
/// stand-in for the Python remove-closure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ListenerHandle(u64);

/// Open/close agent lanes; pure state, listener-driven.
pub struct TaskStatusTracker {
    pub root_session_id: String,
    pub lanes: LaneRegistry,
    listeners: Vec<(u64, Box<dyn Fn()>)>,
    next_listener_id: u64,
}

impl TaskStatusTracker {
    /// The hook names the Python tracker subscribes to (`EVENTS`).
    pub const EVENTS: [&'static str; 11] = [
        "task:agent_spawned",
        "task:agent_completed",
        "task:spawned",
        "task:completed",
        "delegate:agent_spawned",
        "delegate:agent_completed",
        "delegate:agent_resumed",
        "delegate:agent_cancelled",
        "delegate:error",
        "session:start",
        "session:end",
    ];

    pub fn new(root_session_id: &str) -> Self {
        Self::with_lanes(root_session_id, LaneRegistry::new())
    }

    /// Python's `lanes=` keyword: share an externally-owned registry.
    pub fn with_lanes(root_session_id: &str, lanes: LaneRegistry) -> Self {
        TaskStatusTracker {
            root_session_id: root_session_id.to_string(),
            lanes,
            listeners: Vec::new(),
            next_listener_id: 0,
        }
    }

    // -- state ---------------------------------------------------------------

    /// Drives `N agent(s)` in the working line and the coordinating title.
    pub fn active_count(&self) -> usize {
        self.lanes.active_count()
    }

    pub fn lane(&self, session_id: &str) -> Option<LaneRecord> {
        self.lanes.get(session_id)
    }

    pub fn add_listener(&mut self, listener: impl Fn() + 'static) -> ListenerHandle {
        let id = self.next_listener_id;
        self.next_listener_id += 1;
        self.listeners.push((id, Box::new(listener)));
        ListenerHandle(id)
    }

    /// The Python remove-closure: idempotent for already-removed handles.
    pub fn remove_listener(&mut self, handle: ListenerHandle) {
        self.listeners.retain(|(id, _)| *id != handle.0);
    }

    // -- hook plumbing ---------------------------------------------------------

    /// Python's async hook entry point, synchronous per the conventions.
    /// Dormant until a Rust-side hook registry exists (see [`HookResult`]).
    #[allow(dead_code)]
    fn handle_event(&mut self, event: &str, data: &Payload) -> HookResult {
        self.consume(event, data);
        HookResult::cont()
    }

    // -- consumption -----------------------------------------------------------

    pub fn consume(&mut self, event: &str, data: &Payload) {
        if event == "session:start" || event == "session:end" {
            self.consume_session(event, data);
            return;
        }
        match normalize(event, Some(data)) {
            Some(UIEvent::AgentSpawned(spawned)) => {
                let child_id =
                    first_non_empty(&[&spawned.sub_session_id, &spawned.session_id]).to_string();
                if child_id.is_empty() || child_id == self.root_session_id {
                    return;
                }
                let mut parent_id = first_non_empty(&[
                    &spawned.parent_session_id,
                    &spawned.session_id,
                    &self.root_session_id,
                ])
                .to_string();
                if parent_id == child_id {
                    parent_id = self.root_session_id.clone();
                }
                let name = agent_name(&spawned.agent, &child_id);
                self.lanes.register(
                    &child_id,
                    Some(&parent_id),
                    &name,
                    RegisterOptions {
                        activity: "running".to_string(),
                        ..RegisterOptions::default()
                    },
                );
                self.notify();
            }
            Some(UIEvent::AgentCompleted(completed)) => {
                let child_id =
                    first_non_empty(&[&completed.sub_session_id, &completed.session_id])
                        .to_string();
                if child_id.is_empty() || child_id == self.root_session_id {
                    return;
                }
                if self.lanes.get(&child_id).is_none() {
                    // Completion raced ahead of the spawn event: open then close.
                    let parent_id =
                        first_non_empty(&[&completed.parent_session_id, &self.root_session_id])
                            .to_string();
                    let name = agent_name(&completed.agent, &child_id);
                    self.lanes.register(
                        &child_id,
                        Some(&parent_id),
                        &name,
                        RegisterOptions::default(),
                    );
                }
                let result = if !completed.result.is_empty() {
                    completed.result.clone()
                } else if completed.success {
                    String::new()
                } else {
                    "failed".to_string()
                };
                self.lanes.complete(&child_id, &result);
                self.notify();
            }
            Some(UIEvent::AgentResumed(resumed)) => {
                // delegate:agent_resumed carries only the child session_id (the
                // envelope's own field) + parent_session_id -- no `agent` name
                // (intentional, see AgentResumed docstring): the lane already
                // exists from the original spawn, keyed by this same id, so
                // reopening it needs nothing new to key on.
                let child_id = resumed.session_id.clone();
                if child_id.is_empty() || child_id == self.root_session_id {
                    return;
                }
                let parent_id =
                    first_non_empty(&[&resumed.parent_session_id, &self.root_session_id])
                        .to_string();
                let name = agent_name(&resumed.agent, &child_id);
                self.lanes.register(
                    &child_id,
                    Some(&parent_id),
                    &name,
                    RegisterOptions {
                        activity: "running".to_string(),
                        reopen: true,
                        ..RegisterOptions::default()
                    },
                );
                self.notify();
            }
            _ => {}
        }
    }

    fn consume_session(&mut self, event: &str, payload: &Payload) {
        // Python: `str(payload.get("session_id") or "")`.
        let session_id = payload
            .get("session_id")
            .filter(|value| is_truthy(value))
            .map(py_str)
            .unwrap_or_default();
        if session_id.is_empty() || session_id == self.root_session_id {
            return;
        }
        if event == "session:start" {
            // Python: `if not parent_id: return` — a root session starting
            // is not a lane.
            let Some(parent_id) = payload
                .get("parent_id")
                .filter(|value| is_truthy(value))
                .map(py_str)
            else {
                return;
            };
            let name = agent_from_session_id(&session_id);
            self.lanes.register(
                &session_id,
                Some(&parent_id),
                &name,
                RegisterOptions {
                    activity: "running".to_string(),
                    ..RegisterOptions::default()
                },
            );
            self.notify();
            return;
        }
        if let Some(record) = self.lanes.get(&session_id) {
            if record.lane.state != LaneStateName::Done {
                self.lanes.complete(&session_id, "");
                self.notify();
            }
        }
    }

    fn notify(&self) {
        for (_, listener) in &self.listeners {
            listener();
        }
    }
}

/// Python `normalized.agent or _agent_from_session_id(child_id)`.
fn agent_name(agent: &str, session_id: &str) -> String {
    if agent.is_empty() {
        agent_from_session_id(session_id)
    } else {
        agent.to_string()
    }
}

/// Hierarchical sub-session ids end `_{agent_name}` — recover it.
fn agent_from_session_id(session_id: &str) -> String {
    match session_id.rfind('_') {
        Some(index) => session_id[index + 1..].to_string(),
        None => "agent".to_string(),
    }
}

/// Python's chained `a or b or c` over strings ("" is falsy).
fn first_non_empty<'a>(candidates: &[&'a str]) -> &'a str {
    for candidate in candidates {
        if !candidate.is_empty() {
            return candidate;
        }
    }
    ""
}

/// Python truthiness for JSON values (mirrors `kernel::events`' private helper).
fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `str(value)` for the payload scalars we encounter.
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

// --------------------------------------------------------------------------
// Tests — ports of the TaskStatusTracker section of
// tests/test_kernel_trackers.py. The rest of that file pins the stream/
// runtime trackers, QueueBridge, and DisplaySystem (other units);
// tests/test_kernel_trackers_spawner.py pins kernel/spawner.py only.
// --------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    const ROOT: &str = "sess-root";

    fn payload(value: Value) -> Payload {
        match value {
            Value::Object(map) => map,
            _ => panic!("payload literal must be a JSON object"),
        }
    }

    /// Pins Python `test_task_tracker_opens_and_completes_lanes`.
    #[test]
    fn test_task_tracker_opens_and_completes_lanes() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        let sub = format!("{ROOT}-abc123_test-writer");
        tracker.consume(
            "task:agent_spawned",
            &payload(json!({
                "session_id": ROOT,
                "agent": "test-writer",
                "sub_session_id": sub,
                "parent_session_id": ROOT,
            })),
        );
        assert_eq!(tracker.active_count(), 1);
        let lane = tracker.lane(&sub);
        assert!(lane.is_some());
        let lane = lane.unwrap();
        assert_eq!(lane.lane.name, "test-writer");
        assert_eq!(lane.lane.state.as_str(), "running");
        assert_eq!(lane.lane.glyph, "◐");

        tracker.consume(
            "task:agent_completed",
            &payload(json!({
                "session_id": ROOT,
                "agent": "test-writer",
                "sub_session_id": sub,
                "parent_session_id": ROOT,
                "success": true,
            })),
        );
        assert_eq!(tracker.active_count(), 0);
        let lane = tracker.lane(&sub);
        assert!(lane.is_some());
        let lane = lane.unwrap();
        assert_eq!(lane.lane.state.as_str(), "done");
        assert_eq!(lane.lane.glyph, "✔");
    }

    /// Pins Python `test_task_tracker_legacy_event_names`.
    #[test]
    fn test_task_tracker_legacy_event_names() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        tracker.consume(
            "task:spawned",
            &payload(json!({
                "session_id": ROOT, "agent": "worker", "sub_session_id": "kid-1_worker",
            })),
        );
        assert_eq!(tracker.active_count(), 1);
        tracker.consume(
            "task:completed",
            &payload(json!({
                "session_id": ROOT, "sub_session_id": "kid-1_worker", "success": false,
            })),
        );
        let lane = tracker.lane("kid-1_worker");
        assert!(lane.is_some());
        let lane = lane.unwrap();
        assert_eq!(lane.lane.state.as_str(), "done");
        assert!(lane.lane.activity.contains("failed"));
    }

    /// Pins Python `test_task_tracker_depth_race_child_before_parent`.
    #[test]
    fn test_task_tracker_depth_race_child_before_parent() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        // Grandchild spawn event arrives before its parent's.
        tracker.consume(
            "task:agent_spawned",
            &payload(json!({
                "session_id": "kid-1_worker",
                "sub_session_id": "kid-1_worker-9f_helper",
                "parent_session_id": "kid-1_worker",
                "agent": "helper",
            })),
        );
        let grandchild = tracker.lane("kid-1_worker-9f_helper");
        assert!(grandchild.is_some());
        assert_eq!(grandchild.unwrap().depth, 1); // parent unknown yet
        tracker.consume(
            "task:agent_spawned",
            &payload(json!({
                "session_id": ROOT,
                "sub_session_id": "kid-1_worker",
                "parent_session_id": ROOT,
                "agent": "worker",
            })),
        );
        let grandchild = tracker.lane("kid-1_worker-9f_helper");
        assert!(grandchild.is_some());
        assert_eq!(grandchild.unwrap().depth, 2); // retro-patched
    }

    /// Pins Python `test_task_tracker_session_start_races_agent_spawned`.
    #[test]
    fn test_task_tracker_session_start_races_agent_spawned() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        tracker.consume(
            "session:start",
            &payload(json!({"session_id": "kid-1_worker", "parent_id": ROOT})),
        );
        assert_eq!(tracker.active_count(), 1);
        // Later duplicate registration is idempotent.
        tracker.consume(
            "task:agent_spawned",
            &payload(json!({
                "session_id": ROOT, "sub_session_id": "kid-1_worker", "agent": "worker",
            })),
        );
        assert_eq!(tracker.active_count(), 1);
        tracker.consume(
            "session:end",
            &payload(json!({"session_id": "kid-1_worker", "parent_id": ROOT})),
        );
        assert_eq!(tracker.active_count(), 0);
    }

    /// Pins Python `test_task_tracker_completion_races_ahead_of_spawn`.
    #[test]
    fn test_task_tracker_completion_races_ahead_of_spawn() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        tracker.consume(
            "task:agent_completed",
            &payload(json!({
                "session_id": ROOT, "sub_session_id": "kid-2_scout", "success": true,
            })),
        );
        let lane = tracker.lane("kid-2_scout");
        assert!(lane.is_some());
        let lane = lane.unwrap();
        assert_eq!(lane.lane.state.as_str(), "done");
        assert_eq!(lane.lane.name, "scout");
    }

    /// Pins Python `test_task_tracker_ignores_root_session_events`.
    #[test]
    fn test_task_tracker_ignores_root_session_events() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        tracker.consume(
            "session:start",
            &payload(json!({"session_id": ROOT, "parent_id": null})),
        );
        tracker.consume(
            "task:agent_spawned",
            &payload(json!({"session_id": ROOT, "sub_session_id": ROOT})),
        );
        assert_eq!(tracker.active_count(), 0);
    }

    /// Pins Python `test_task_tracker_subscribes_to_delegate_lifecycle`:
    /// anchors' tool-delegate emits delegate:* — the lanes panel and the
    /// working-line agent count go blind without these subscriptions.
    #[test]
    fn test_task_tracker_subscribes_to_delegate_lifecycle() {
        for name in [
            "delegate:agent_spawned",
            "delegate:agent_completed",
            "delegate:agent_resumed",
            "delegate:agent_cancelled",
            "delegate:error",
        ] {
            assert!(TaskStatusTracker::EVENTS.contains(&name), "{name}");
        }
    }

    /// Pins Python `test_task_tracker_delegate_spawn_and_complete`.
    #[test]
    fn test_task_tracker_delegate_spawn_and_complete() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        tracker.consume(
            "delegate:agent_spawned",
            &payload(json!({
                "session_id": ROOT,
                "agent": "worker",
                "sub_session_id": "kid-1_worker",
                "parent_session_id": ROOT,
            })),
        );
        assert_eq!(tracker.active_count(), 1);
        tracker.consume(
            "delegate:agent_completed",
            &payload(json!({
                "session_id": ROOT,
                "sub_session_id": "kid-1_worker",
                "parent_session_id": ROOT,
                "success": true,
            })),
        );
        assert_eq!(tracker.active_count(), 0);
    }

    /// Pins Python `test_task_tracker_delegate_resume_reopens_lane`.
    #[test]
    fn test_task_tracker_delegate_resume_reopens_lane() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        tracker.consume(
            "delegate:agent_resumed",
            &payload(json!({"session_id": "kid-1_worker", "parent_session_id": ROOT})),
        );
        assert_eq!(tracker.active_count(), 1);
        let lane = tracker.lane("kid-1_worker");
        assert!(lane.is_some());
        // Recovered from the session-id suffix.
        assert_eq!(lane.unwrap().lane.name, "worker");
    }

    /// Pins Python `test_task_tracker_delegate_cancelled_shows_cancelled`.
    #[test]
    fn test_task_tracker_delegate_cancelled_shows_cancelled() {
        let mut tracker = TaskStatusTracker::new(ROOT);
        tracker.consume(
            "delegate:agent_spawned",
            &payload(json!({
                "session_id": ROOT,
                "agent": "worker",
                "sub_session_id": "kid-1_worker",
                "parent_session_id": ROOT,
            })),
        );
        tracker.consume(
            "delegate:agent_cancelled",
            &payload(json!({
                "session_id": ROOT,
                "agent": "worker",
                "sub_session_id": "kid-1_worker",
                "parent_session_id": ROOT,
            })),
        );
        let lane = tracker.lane("kid-1_worker");
        assert!(lane.is_some());
        let lane = lane.unwrap();
        assert_eq!(lane.lane.state.as_str(), "done");
        assert!(lane.lane.activity.contains("cancelled"));
    }

    /// Not a pinned pytest case: exercises the Rust `handle_event` mirror
    /// (Python's async hook entry) plus the listener plumbing — behavior
    /// oracle-checked against the Python module (`handle_event` returns
    /// `HookResult(action="continue")` and fires listeners on lane change;
    /// removed listeners stop firing).
    #[test]
    fn oracle_handle_event_and_listeners_match_python() {
        use std::cell::Cell;
        use std::rc::Rc;

        let mut tracker = TaskStatusTracker::new(ROOT);
        let calls = Rc::new(Cell::new(0u32));
        let seen = Rc::clone(&calls);
        let handle = tracker.add_listener(move || seen.set(seen.get() + 1));
        let result = tracker.handle_event(
            "task:agent_spawned",
            &payload(json!({
                "session_id": ROOT, "sub_session_id": "kid-1_worker", "agent": "worker",
            })),
        );
        assert_eq!(result.action, "continue");
        assert_eq!(calls.get(), 1);
        tracker.remove_listener(handle);
        tracker.remove_listener(handle); // idempotent, like the Python closure
        tracker.consume(
            "task:agent_completed",
            &payload(json!({"session_id": ROOT, "sub_session_id": "kid-1_worker"})),
        );
        assert_eq!(calls.get(), 1); // removed listener no longer fires
        assert_eq!(tracker.active_count(), 0);
    }
}
