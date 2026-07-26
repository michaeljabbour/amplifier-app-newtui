//! StepBoundaryBridge: the ONE steering path (ADR-0007 §Steering).
//!
//! Consumes exactly one queued steer per `provider:request` on the root
//! session and injects it as a user-role context message:
//!
//! ```text
//! HookResult(action="inject_context", context_injection_role="user")
//! ```
//!
//! Registered at priority ~950 so it runs just before the provider call.
//! Answered needs-you decisions ride the same boundary (the mockup's
//! "Applying decision: …" flow). Leftover steers at turn end are NOT this
//! module's job: the app drains them via [`SteeringQueue::drain_steers`]
//! and discards them (mockup: an unconsumed steer never becomes a turn).
//!
//! Port of `src/amplifier_app_newtui/kernel/steering.py`. The Python hook
//! is `async` only to satisfy the amplifier-core hook signature; its
//! decision logic is synchronous and ports as a plain function.

use serde_json::Value;

use super::events::Payload;
use crate::model::queues::{
    LaneSteeringQueue, NeedsYouItem, NeedsYouQueue, QueuedMessage, SteeringQueue,
};

/// Minimal local mirror of `amplifier_core.HookResult` — ONLY the fields
/// this bridge produces (and its tests assert). Defaults match the real
/// pydantic model exactly: `action="continue"`, no injection, role
/// `"system"`, `ephemeral=False`, `suppress_output=False`. Not a general
/// contract; later units needing more of HookResult mirror their own.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HookResult {
    /// `"continue"` or `"inject_context"` (the only actions this emits).
    pub action: &'static str,
    pub context_injection: Option<String>,
    /// `Literal["system", "user", "assistant"]` in Python.
    pub context_injection_role: &'static str,
    pub ephemeral: bool,
    pub suppress_output: bool,
}

impl Default for HookResult {
    fn default() -> Self {
        Self {
            action: "continue",
            context_injection: None,
            context_injection_role: "system",
            ephemeral: false,
            suppress_output: false,
        }
    }
}

impl HookResult {
    fn cont() -> Self {
        Self::default()
    }

    fn inject_user_context(injection: String) -> Self {
        Self {
            action: "inject_context",
            context_injection: Some(injection),
            context_injection_role: "user",
            ephemeral: false,
            suppress_output: true,
        }
    }
}

/// The registry surface `register_hooks` needs (Python duck-types the
/// coordinator's `hooks` object). `register` returns the unregister
/// callback, or `None` when the registry hands back something
/// non-callable — mirroring the Python `if not callable(unregister)`
/// guard.
pub trait HookRegistry {
    fn register(
        &mut self,
        event: &str,
        priority: i64,
        name: &str,
    ) -> Option<Box<dyn FnOnce() + Send>>;
}

type OnApplied = Box<dyn Fn(&QueuedMessage) + Send + Sync>;
type OnAnswers = Box<dyn Fn(&[NeedsYouItem]) + Send + Sync>;
type OnInject = Box<dyn Fn() + Send + Sync>;
type OnLaneApplied = Box<dyn Fn(&str, &QueuedMessage) + Send + Sync>;

/// Drain one steer (+ any answered deferred decisions) per step.
pub struct StepBoundaryBridge {
    root_session_id: String,
    steering: SteeringQueue,
    needs_you: Option<NeedsYouQueue>,
    lane_steering: Option<LaneSteeringQueue>,
    on_applied: Option<OnApplied>,
    on_answers: Option<OnAnswers>,
    on_inject: Option<OnInject>,
    on_lane_applied: Option<OnLaneApplied>,
}

impl StepBoundaryBridge {
    /// `StepBoundaryBridge.EVENTS = ("provider:request",)`.
    pub const EVENTS: [&'static str; 1] = ["provider:request"];

    /// `register_hooks`'s default `priority=950` keyword.
    pub const DEFAULT_PRIORITY: i64 = 950;

    /// The wired hook name (Python passes it inline to `hooks.register`).
    pub const HOOK_NAME: &'static str = "newtui-step-boundary-steering";

    pub fn new(root_session_id: impl Into<String>, steering: SteeringQueue) -> Self {
        Self {
            root_session_id: root_session_id.into(),
            steering,
            needs_you: None,
            lane_steering: None,
            on_applied: None,
            on_answers: None,
            on_inject: None,
            on_lane_applied: None,
        }
    }

    /// Python keyword argument `needs_you=`.
    pub fn with_needs_you(mut self, needs_you: NeedsYouQueue) -> Self {
        self.needs_you = Some(needs_you);
        self
    }

    /// Python keyword argument `lane_steering=`.
    pub fn with_lane_steering(mut self, lane_steering: LaneSteeringQueue) -> Self {
        self.lane_steering = Some(lane_steering);
        self
    }

    /// Python keyword argument `on_applied=` (receives the consumed steer).
    pub fn with_on_applied(
        mut self,
        on_applied: impl Fn(&QueuedMessage) + Send + Sync + 'static,
    ) -> Self {
        self.on_applied = Some(Box::new(on_applied));
        self
    }

    /// Python keyword argument `on_answers=` (receives the consumed batch).
    pub fn with_on_answers(
        mut self,
        on_answers: impl Fn(&[NeedsYouItem]) + Send + Sync + 'static,
    ) -> Self {
        self.on_answers = Some(Box::new(on_answers));
        self
    }

    /// Python keyword argument `on_inject=` (fires once per injection).
    pub fn with_on_inject(mut self, on_inject: impl Fn() + Send + Sync + 'static) -> Self {
        self.on_inject = Some(Box::new(on_inject));
        self
    }

    /// Python keyword argument `on_lane_applied=` (child session + steer).
    pub fn with_on_lane_applied(
        mut self,
        on_lane_applied: impl Fn(&str, &QueuedMessage) + Send + Sync + 'static,
    ) -> Self {
        self.on_lane_applied = Some(Box::new(on_lane_applied));
        self
    }

    /// Shared queue access for the app (the Python bridge holds references
    /// to externally-owned queues; here the bridge owns them — the queues'
    /// interior mutability keeps every method `&self`).
    pub fn steering(&self) -> &SteeringQueue {
        &self.steering
    }

    pub fn needs_you(&self) -> Option<&NeedsYouQueue> {
        self.needs_you.as_ref()
    }

    pub fn lane_steering(&self) -> Option<&LaneSteeringQueue> {
        self.lane_steering.as_ref()
    }

    pub fn handle_event(&self, event: &str, data: &Payload) -> HookResult {
        if event != "provider:request" {
            return HookResult::cont();
        }
        let session_id = self.session_id_from(data);
        if session_id != self.root_session_id {
            return self.handle_lane(&session_id);
        }
        let steer = self.steering.consume_next_steer();
        let answers: Vec<NeedsYouItem> = match &self.needs_you {
            Some(needs_you) => needs_you.consume_answered(),
            None => Vec::new(),
        };
        if steer.is_none() && answers.is_empty() {
            return HookResult::cont();
        }
        if let (Some(steer), Some(on_applied)) = (&steer, &self.on_applied) {
            on_applied(steer);
        }
        if !answers.is_empty() {
            if let Some(on_answers) = &self.on_answers {
                on_answers(&answers);
            }
        }
        let mut injections: Vec<String> = Vec::new();
        if let Some(steer) = &steer {
            injections.push(format!(
                "User steering received during this turn. Apply it at this safe \
                 step boundary:\n{}",
                steer.text
            ));
        }
        if !answers.is_empty() {
            let answer_lines: Vec<String> = answers
                .iter()
                .map(|item| {
                    format!(
                        "{}: {}\nAnswer: {}",
                        item.decision_id, item.question, item.answer
                    )
                })
                .collect();
            injections.push(format!(
                "The user answered deferred decisions. Apply these answers to \
                 dependent work:\n{}",
                answer_lines.join("\n")
            ));
        }
        if let Some(on_inject) = &self.on_inject {
            // Exactly ONE persistent user-role message enters the context
            // below (steer + answers are joined into a single injection).
            // Foundation's fork slicing counts it as a turn boundary, so
            // the runtime must advance checkpoint turn accounting (§9).
            on_inject();
        }
        HookResult::inject_user_context(injections.join("\n\n"))
    }

    /// Per-lane steering: deliver one queued steer to the delegate at
    /// `session_id` at its OWN step boundary (issue #39).
    ///
    /// Root steering is untouched — a child session only ever drains its
    /// own lane queue, so the root [`SteeringQueue`] is never consumed by
    /// a child `provider:request` (the historical "root only" contract).
    fn handle_lane(&self, session_id: &str) -> HookResult {
        let Some(lane_steering) = &self.lane_steering else {
            return HookResult::cont();
        };
        let Some(steer) = lane_steering.consume_next(session_id) else {
            return HookResult::cont();
        };
        if let Some(on_lane_applied) = &self.on_lane_applied {
            on_lane_applied(session_id, &steer);
        }
        HookResult::inject_user_context(format!(
            "User steering received for this delegate. Apply it at this \
             safe step boundary:\n{}",
            steer.text
        ))
    }

    /// Python `str(data.get("session_id") or self._root_session_id)`:
    /// a missing / null / otherwise-falsy `session_id` falls back to root.
    fn session_id_from(&self, data: &Payload) -> String {
        match data.get("session_id") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            Some(Value::Bool(true)) => "True".to_string(),
            Some(Value::Number(n)) if n.as_f64() != Some(0.0) => n.to_string(),
            _ => self.root_session_id.clone(),
        }
    }

    /// Register the bridge on `provider:request` at [`Self::DEFAULT_PRIORITY`];
    /// returns the unregister callback (a no-op when the registry's return
    /// value was not callable, exactly like the Python guard).
    pub fn register_hooks(&self, hooks: &mut dyn HookRegistry) -> Box<dyn FnOnce() + Send> {
        self.register_hooks_with_priority(hooks, Self::DEFAULT_PRIORITY)
    }

    /// `register_hooks(hooks, priority=…)` with an explicit priority.
    pub fn register_hooks_with_priority(
        &self,
        hooks: &mut dyn HookRegistry,
        priority: i64,
    ) -> Box<dyn FnOnce() + Send> {
        match hooks.register("provider:request", priority, Self::HOOK_NAME) {
            Some(unregister) => unregister,
            None => Box::new(|| {}),
        }
    }
}

// ---------------------------------------------------------------------------
// Tests — ports of tests/test_kernel_steering.py and
// tests/test_kernel_lane_steering.py. The RealRuntime-narration tests in
// those files pin kernel/runtime.py (not ported) and are skipped here.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::queues::MessageKind;
    use serde_json::{json, Map};
    use std::sync::{Arc, Mutex};

    const ROOT: &str = "sess-root";
    const CHILD: &str = "sess-child_worker";
    const OTHER: &str = "sess-child_other";

    fn payload(session_id: &str) -> Payload {
        let mut map = Map::new();
        map.insert("session_id".to_string(), json!(session_id));
        map
    }

    /// The injected context, asserted present (Python `_injection`).
    fn injection(result: &HookResult) -> &str {
        result
            .context_injection
            .as_deref()
            .expect("context_injection is set")
    }

    /// Python `FakeHooks`: records registrations; `register` returns a
    /// closure appending the hook name to `unregistered`.
    struct FakeHooks {
        registered: Vec<(String, i64, String)>,
        unregistered: Arc<Mutex<Vec<String>>>,
    }

    impl FakeHooks {
        fn new() -> Self {
            Self {
                registered: Vec::new(),
                unregistered: Arc::new(Mutex::new(Vec::new())),
            }
        }
    }

    impl HookRegistry for FakeHooks {
        fn register(
            &mut self,
            event: &str,
            priority: i64,
            name: &str,
        ) -> Option<Box<dyn FnOnce() + Send>> {
            self.registered
                .push((event.to_string(), priority, name.to_string()));
            let sink = Arc::clone(&self.unregistered);
            let name = name.to_string();
            Some(Box::new(move || sink.lock().unwrap().push(name)))
        }
    }

    // --- test_kernel_steering.py ------------------------------------------

    #[test]
    fn test_drains_exactly_one_steer_per_step() {
        let steering = SteeringQueue::new();
        steering
            .enqueue("focus on the parser", MessageKind::Steer)
            .unwrap();
        steering
            .enqueue("skip the docs", MessageKind::Steer)
            .unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, steering);

        let first = bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(first.action, "inject_context");
        assert!(injection(&first).contains("focus on the parser"));
        assert!(!injection(&first).contains("skip the docs"));
        assert_eq!(first.context_injection_role, "user");
        assert!(first.suppress_output);

        let second = bridge.handle_event("provider:request", &payload(ROOT));
        assert!(injection(&second).contains("skip the docs"));

        let third = bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(third.action, "continue");
    }

    #[test]
    fn test_root_session_only() {
        let steering = SteeringQueue::new();
        steering.enqueue("steer me", MessageKind::Steer).unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, steering);
        let result = bridge.handle_event("provider:request", &payload("sess-child_worker"));
        assert_eq!(result.action, "continue");
        assert_eq!(bridge.steering().pending_steers().len(), 1); // untouched for the child
    }

    #[test]
    fn test_next_turn_messages_are_never_injected_mid_turn() {
        let steering = SteeringQueue::new();
        steering
            .enqueue("full follow-up", MessageKind::NextTurn)
            .unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, steering);
        let result = bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(result.action, "continue");
        assert_eq!(bridge.steering().pending_next_turn().len(), 1);
    }

    #[test]
    fn test_answered_decisions_ride_the_same_boundary() {
        let steering = SteeringQueue::new();
        let needs_you = NeedsYouQueue::new();
        let item = needs_you
            .defer("Push to fork?", "trust boundary", Default::default())
            .unwrap();
        needs_you
            .answer(&item.decision_id, "yes · push to fork")
            .unwrap();
        let applied: Arc<Mutex<Vec<QueuedMessage>>> = Arc::new(Mutex::new(Vec::new()));
        let answers: Arc<Mutex<Vec<Vec<NeedsYouItem>>>> = Arc::new(Mutex::new(Vec::new()));
        let applied_sink = Arc::clone(&applied);
        let answers_sink = Arc::clone(&answers);
        let bridge = StepBoundaryBridge::new(ROOT, steering)
            .with_needs_you(needs_you)
            .with_on_applied(move |steer| applied_sink.lock().unwrap().push(steer.clone()))
            .with_on_answers(move |batch| answers_sink.lock().unwrap().push(batch.to_vec()));
        let result = bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(result.action, "inject_context");
        assert!(injection(&result).contains("Push to fork?"));
        assert!(injection(&result).contains("yes · push to fork"));
        assert!(applied.lock().unwrap().is_empty()); // no steer this step
        assert_eq!(answers.lock().unwrap().len(), 1);
        // Consumed: the same answer never re-injects.
        let again = bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(again.action, "continue");
    }

    #[test]
    fn test_steer_and_answers_combine_into_one_injection() {
        let steering = SteeringQueue::new();
        steering
            .enqueue("prefer the fast path", MessageKind::Steer)
            .unwrap();
        let needs_you = NeedsYouQueue::new();
        let item = needs_you
            .defer("Enable cache?", "", Default::default())
            .unwrap();
        needs_you.answer(&item.decision_id, "yes").unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, steering).with_needs_you(needs_you);
        let result = bridge.handle_event("provider:request", &payload(ROOT));
        assert!(injection(&result).contains("prefer the fast path"));
        assert!(injection(&result).contains("Enable cache?"));
    }

    #[test]
    fn test_on_inject_fires_once_per_persistent_injection() {
        // The injection is ONE persistent user-role message (steer + answers
        // combined) — foundation's fork counts it as a turn boundary, so the
        // runtime is told exactly once per applied injection (spec §9).
        let steering = SteeringQueue::new();
        steering
            .enqueue("prefer the fast path", MessageKind::Steer)
            .unwrap();
        let needs_you = NeedsYouQueue::new();
        let item = needs_you
            .defer("Enable cache?", "", Default::default())
            .unwrap();
        needs_you.answer(&item.decision_id, "yes").unwrap();
        let injects: Arc<Mutex<Vec<()>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&injects);
        let bridge = StepBoundaryBridge::new(ROOT, steering)
            .with_needs_you(needs_you)
            .with_on_inject(move || sink.lock().unwrap().push(()));
        let result = bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(result.action, "inject_context");
        assert_eq!(injects.lock().unwrap().len(), 1); // steer + answer = one injected message
        let again = bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(again.action, "continue");
        assert_eq!(injects.lock().unwrap().len(), 1); // nothing injected → no callback
    }

    #[test]
    fn test_on_applied_callback_receives_the_steer() {
        let steering = SteeringQueue::new();
        let queued = steering.enqueue("steer text", MessageKind::Steer).unwrap();
        let applied: Arc<Mutex<Vec<QueuedMessage>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&applied);
        let bridge = StepBoundaryBridge::new(ROOT, steering)
            .with_on_applied(move |steer| sink.lock().unwrap().push(steer.clone()));
        bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(*applied.lock().unwrap(), vec![queued]);
    }

    #[test]
    fn test_leftover_steers_discarded_via_drain() {
        // The bridge leaves un-applied steers in the queue; at turn end the app
        // drains and DISCARDS them (mockup: an unconsumed steer never becomes
        // a turn the user never sent — ADR-0007 §Steering).
        let steering = SteeringQueue::new();
        steering
            .enqueue("never applied", MessageKind::Steer)
            .unwrap();
        steering
            .enqueue("also pending", MessageKind::Steer)
            .unwrap();
        steering
            .enqueue("next turn message", MessageKind::NextTurn)
            .unwrap();
        let leftover = steering.drain_steers();
        assert_eq!(
            leftover.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(),
            vec!["never applied", "also pending"]
        );
        assert_eq!(steering.pending_next_turn().len(), 1); // untouched
    }

    #[test]
    fn test_register_hooks_priority_950() {
        let mut hooks = FakeHooks::new();
        let bridge = StepBoundaryBridge::new(ROOT, SteeringQueue::new());
        let unregister = bridge.register_hooks(&mut hooks);
        assert_eq!(
            hooks.registered,
            vec![(
                "provider:request".to_string(),
                950,
                "newtui-step-boundary-steering".to_string()
            )]
        );
        let unregistered = Arc::clone(&hooks.unregistered);
        unregister();
        assert_eq!(
            *unregistered.lock().unwrap(),
            vec!["newtui-step-boundary-steering".to_string()]
        );
    }

    #[test]
    fn test_non_provider_request_events_continue() {
        let bridge = StepBoundaryBridge::new(ROOT, SteeringQueue::new());
        let result = bridge.handle_event("tool:pre", &payload(ROOT));
        assert_eq!(result.action, "continue");
    }

    // Skipped: test_real_runtime_steer_applied_emits_narration pins
    // kernel/runtime.py (RealRuntime), which is not part of this unit.

    // --- test_kernel_lane_steering.py ---------------------------------------

    #[test]
    fn test_child_step_boundary_delivers_its_lane_steer() {
        let lane = LaneSteeringQueue::new();
        lane.enqueue(CHILD, "prefer the fast path").unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, SteeringQueue::new()).with_lane_steering(lane);

        let result = bridge.handle_event("provider:request", &payload(CHILD));
        assert_eq!(result.action, "inject_context");
        assert!(injection(&result).contains("prefer the fast path"));
        assert!(injection(&result).to_lowercase().contains("delegate"));
        assert_eq!(result.context_injection_role, "user");
        assert!(result.suppress_output);
        // Consumed: the next child boundary injects nothing.
        let again = bridge.handle_event("provider:request", &payload(CHILD));
        assert_eq!(again.action, "continue");
    }

    #[test]
    fn test_one_lane_steer_per_step_fifo() {
        let lane = LaneSteeringQueue::new();
        lane.enqueue(CHILD, "first").unwrap();
        lane.enqueue(CHILD, "second").unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, SteeringQueue::new()).with_lane_steering(lane);

        let first = bridge.handle_event("provider:request", &payload(CHILD));
        assert!(injection(&first).contains("first") && !injection(&first).contains("second"));
        let second = bridge.handle_event("provider:request", &payload(CHILD));
        assert!(injection(&second).contains("second"));
    }

    #[test]
    fn test_lanes_are_isolated_by_session() {
        let lane = LaneSteeringQueue::new();
        lane.enqueue(CHILD, "for the worker").unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, SteeringQueue::new()).with_lane_steering(lane);

        // A different child's boundary must not drain the worker's queue.
        let other = bridge.handle_event("provider:request", &payload(OTHER));
        assert_eq!(other.action, "continue");
        assert_eq!(bridge.lane_steering().unwrap().queued_count(CHILD), 1);
    }

    #[test]
    fn test_root_steer_never_leaks_to_a_child() {
        let steering = SteeringQueue::new();
        steering
            .enqueue("steer the coordinator", MessageKind::Steer)
            .unwrap();
        let lane = LaneSteeringQueue::new();
        let bridge = StepBoundaryBridge::new(ROOT, steering).with_lane_steering(lane);

        let child = bridge.handle_event("provider:request", &payload(CHILD));
        assert_eq!(child.action, "continue");
        // root queue untouched by the child
        assert_eq!(bridge.steering().pending_steers().len(), 1);

        let root = bridge.handle_event("provider:request", &payload(ROOT));
        assert!(injection(&root).contains("steer the coordinator"));
    }

    #[test]
    fn test_child_without_lane_steering_configured_continues() {
        // Regression guard for the historical "root only" contract: with no
        // lane_steering wired, a child boundary is still a no-op.
        let bridge = StepBoundaryBridge::new(ROOT, SteeringQueue::new());
        let result = bridge.handle_event("provider:request", &payload(CHILD));
        assert_eq!(result.action, "continue");
    }

    #[test]
    fn test_on_lane_applied_receives_session_and_steer() {
        let lane = LaneSteeringQueue::new();
        let queued = lane.enqueue(CHILD, "narrate me").unwrap();
        let applied: Arc<Mutex<Vec<(String, QueuedMessage)>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&applied);
        let bridge = StepBoundaryBridge::new(ROOT, SteeringQueue::new())
            .with_lane_steering(lane)
            .with_on_lane_applied(move |sid, steer| {
                sink.lock().unwrap().push((sid.to_string(), steer.clone()));
            });
        bridge.handle_event("provider:request", &payload(CHILD));
        assert_eq!(*applied.lock().unwrap(), vec![(CHILD.to_string(), queued)]);
    }

    // Skipped: test_real_runtime_lane_steer_applied_emits_child_stamped_narration
    // and test_real_runtime_wires_a_shared_lane_steering_queue pin
    // kernel/runtime.py (RealRuntime), which is not part of this unit.

    // --- Rust-specific edge pins --------------------------------------------

    #[test]
    fn missing_session_id_falls_back_to_root() {
        // Python: `str(data.get("session_id") or self._root_session_id)` —
        // an absent/null session_id drains the root queue.
        let steering = SteeringQueue::new();
        steering.enqueue("rooted", MessageKind::Steer).unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, steering);
        let result = bridge.handle_event("provider:request", &Map::new());
        assert_eq!(result.action, "inject_context");
        assert!(injection(&result).contains("rooted"));
    }

    #[test]
    fn injection_text_matches_python_exactly() {
        // Oracle-pinned against the Python bridge (uv run, 2026-07-26).
        let steering = SteeringQueue::new();
        steering
            .enqueue("focus on the parser", MessageKind::Steer)
            .unwrap();
        let needs_you = NeedsYouQueue::new();
        let item = needs_you
            .defer("Push to fork?", "trust boundary", Default::default())
            .unwrap();
        needs_you
            .answer(&item.decision_id, "yes · push to fork")
            .unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, steering).with_needs_you(needs_you);
        let result = bridge.handle_event("provider:request", &payload(ROOT));
        assert_eq!(
            injection(&result),
            "User steering received during this turn. Apply it at this safe \
             step boundary:\nfocus on the parser\n\nThe user answered deferred \
             decisions. Apply these answers to dependent work:\ndecision-1: \
             Push to fork?\nAnswer: yes · push to fork"
        );

        let lane = LaneSteeringQueue::new();
        lane.enqueue(CHILD, "narrate me").unwrap();
        let bridge = StepBoundaryBridge::new(ROOT, SteeringQueue::new()).with_lane_steering(lane);
        let result = bridge.handle_event("provider:request", &payload(CHILD));
        assert_eq!(
            injection(&result),
            "User steering received for this delegate. Apply it at this \
             safe step boundary:\nnarrate me"
        );
    }
}
