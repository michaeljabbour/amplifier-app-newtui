//! Scripted runtimes — offline stand-ins for the serve backend. They emit the
//! SAME wire vocabulary the protocol backend does (`kernel/events.py` kinds
//! plus the ticket-bearing approval record), on a background thread, so
//! streaming/approvals are genuinely time-sliced. No network, no Python.
//!
//! Two scripts live here:
//! - [`DemoScript`] / [`ScriptedDemoRuntime`] — the port of Python
//!   `kernel/demo.py`'s `DemoRuntime`: the mockup's rich multi-turn demo
//!   (seed / build / auto / plan / brainstorm / agents) as deterministic
//!   virtual-clock event scripts, selected per prompt through the
//!   [`DemoWiring`] key bookkeeping. This is what Python `--demo` plays.
//! - [`DemoRuntime`] — the legacy serve-mock single build-shaped turn kept
//!   for the current `--demo` wiring in `main.rs`; swap the composition to
//!   [`ScriptedDemoRuntime`] to play the full scripted demo.

use crate::message::Msg;
use crate::protocol::WireEvent;
use serde_json::{json, Map, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::kernel::events as ev;
use crate::ui::demo_wiring::{
    self as demo, build_answer, demo_lanes, demo_turn_by_key, interrupted_spec, tick_tokens,
    DemoLane, DemoTurnSpec, DemoWiring, LogRowKind, TurnKey,
};

/// The runtime seam — the one interface the UI talks to. `DemoRuntime`
/// (scripted) and `CoreClientRuntime` (spawned backend) both implement it,
/// so the app is identical whether events come from a script or a process.
pub trait Runtime {
    fn submit(&mut self, prompt: String);
    /// Answer a parked approval by ticket id with a broker choice string
    /// (`"Allow once"` / `"Allow always"` / `"Deny"`).
    fn answer_approval(&mut self, _ticket_id: &str, _choice: &str) {}
    fn interrupt(&mut self) {}
}

/// Broker convention: a choice grants iff it is an "Allow"-family string.
pub fn is_allow(choice: &str) -> bool {
    choice.starts_with("Allow")
}

pub const DEMO_SESSION_ID: &str = "demo-01";
const DEMO_TICKET: &str = "demo-1";
const DEMO_ANSWER: &str = "I've added a `/health` endpoint that returns 200 with a JSON status \
body, wired it into the router, and covered it with a test.";
const DEMO_DENIED_ANSWER: &str = "Understood — I left the endpoint out.";

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn obj(value: Value) -> Map<String, Value> {
    match value {
        Value::Object(map) => map,
        _ => Map::new(),
    }
}

pub struct DemoRuntime {
    tx: Sender<Msg>,
    /// Set each turn so the app can unblock a parked approval.
    approval_tx: Option<Sender<bool>>,
    interrupt_tx: Option<Sender<()>>,
}

impl DemoRuntime {
    pub fn new(tx: Sender<Msg>) -> Self {
        Self {
            tx,
            approval_tx: None,
            interrupt_tx: None,
        }
    }

    /// Kick off a scripted turn for `prompt` — the serve-mock script in the
    /// real event vocabulary, including a parked approval.
    fn run_turn(&mut self, prompt: String) {
        let tx = self.tx.clone();
        let (atx, arx) = channel::<bool>();
        let (itx, irx) = channel::<()>();
        self.approval_tx = Some(atx);
        self.interrupt_tx = Some(itx);

        thread::spawn(move || {
            let session = DEMO_SESSION_ID.to_string();
            let send = |e: ev::UIEvent| {
                let _ = tx.send(Msg::Rt(WireEvent::Event(e)));
            };
            let beat = |ms: u64| thread::sleep(Duration::from_millis(ms));
            let interrupted = || irx.try_recv().is_ok();

            send(ev::UIEvent::PromptSubmit(ev::PromptSubmit {
                session_id: session.clone(),
                ts: now_ts(),
                prompt: prompt.clone(),
                ..ev::PromptSubmit::default()
            }));
            beat(120);
            send(ev::UIEvent::Notification(ev::Notification {
                session_id: session.clone(),
                ts: now_ts(),
                message: "Thinking…".into(),
                ..ev::Notification::default()
            }));
            beat(120);
            // Tool burst: 3 file reads + 2 shell commands → the reducer's
            // digest reads "Read 3 files · ran 2 shell commands".
            for (index, (tool, input)) in [
                ("read_file", json!({"path": "src/app.py"})),
                ("read_file", json!({"path": "src/router.py"})),
                ("read_file", json!({"path": "tests/test_app.py"})),
                ("bash", json!({"command": "pytest -q"})),
                ("bash", json!({"command": "ruff check ."})),
            ]
            .into_iter()
            .enumerate()
            {
                if interrupted() {
                    finish_interrupted(&send, &session, &prompt);
                    return;
                }
                let call_id = format!("call-{index}");
                send(ev::UIEvent::ToolPre(ev::ToolPre {
                    session_id: session.clone(),
                    ts: now_ts(),
                    tool_name: tool.into(),
                    tool_call_id: call_id.clone(),
                    tool_input: obj(input.clone()),
                    ..ev::ToolPre::default()
                }));
                beat(40);
                send(ev::UIEvent::ToolPost(ev::ToolPost {
                    session_id: session.clone(),
                    ts: now_ts(),
                    tool_name: tool.into(),
                    tool_call_id: call_id,
                    tool_input: obj(input),
                    result: obj(json!({"status": "ok"})),
                    ..ev::ToolPost::default()
                }));
            }
            // First provider response of the turn (the planning/tool round).
            send(usage_event(&session, 1200, 340, 800, 100));
            beat(60);

            // Park on approval: the ticket-bearing record, then block for
            // the UI's decision routed back by ticket id.
            let _ = tx.send(Msg::Rt(WireEvent::Approval {
                ticket_id: DEMO_TICKET.into(),
                prompt: "write_file src/health.py".into(),
                options: vec!["Allow once".into(), "Allow always".into(), "Deny".into()],
            }));
            let granted = arx.recv().unwrap_or(false);

            if !granted {
                // Denied write → durable ⊘ blocked line via the denied
                // tool:post shape the real runtime emits.
                send(ev::UIEvent::ToolPre(ev::ToolPre {
                    session_id: session.clone(),
                    ts: now_ts(),
                    tool_name: "write_file".into(),
                    tool_call_id: "call-write".into(),
                    tool_input: obj(json!({"path": "src/health.py"})),
                    ..ev::ToolPre::default()
                }));
                send(ev::UIEvent::ToolPost(ev::ToolPost {
                    session_id: session.clone(),
                    ts: now_ts(),
                    tool_name: "write_file".into(),
                    tool_call_id: "call-write".into(),
                    tool_input: obj(json!({"path": "src/health.py"})),
                    result: obj(json!({
                        "status": "denied",
                        "reason": "denied by user",
                        "continuation": "continuing without the write",
                    })),
                    ..ev::ToolPost::default()
                }));
                stream_answer(&send, &session, DEMO_DENIED_ANSWER, interrupted);
                send(usage_event(&session, 600, 80, 0, 0));
                send(ev::UIEvent::PromptComplete(ev::PromptComplete {
                    session_id: session.clone(),
                    ts: now_ts(),
                    response: DEMO_DENIED_ANSWER.into(),
                    ..ev::PromptComplete::default()
                }));
                return;
            }

            send(ev::UIEvent::ToolPre(ev::ToolPre {
                session_id: session.clone(),
                ts: now_ts(),
                tool_name: "write_file".into(),
                tool_call_id: "call-write".into(),
                tool_input: obj(json!({"path": "src/health.py", "content": "def health():\n    return {\"status\": \"ok\"}\n"})),
                ..ev::ToolPre::default()
            }));
            beat(80);
            send(ev::UIEvent::ToolPost(ev::ToolPost {
                session_id: session.clone(),
                ts: now_ts(),
                tool_name: "write_file".into(),
                tool_call_id: "call-write".into(),
                tool_input: obj(json!({"path": "src/health.py"})),
                result: obj(json!({"status": "ok"})),
                ..ev::ToolPost::default()
            }));
            if interrupted() {
                finish_interrupted(&send, &session, &prompt);
                return;
            }
            stream_answer(&send, &session, DEMO_ANSWER, interrupted);
            // Final provider response (the streamed answer round).
            send(usage_event(&session, 900, 120, 0, 0));
            send(ev::UIEvent::PromptComplete(ev::PromptComplete {
                session_id: session.clone(),
                ts: now_ts(),
                response: DEMO_ANSWER.into(),
                files_changed: 1,
                diffstat: "+18/−0".into(),
                ..ev::PromptComplete::default()
            }));
        });
    }
}

fn usage_event(session: &str, input: i64, output: i64, cache_read: i64, cache_write: i64) -> ev::UIEvent {
    ev::UIEvent::ProviderResponseUsage(ev::ProviderResponseUsage {
        session_id: session.to_string(),
        ts: now_ts(),
        input_tokens: input,
        output_tokens: output,
        cache_read,
        cache_write,
        model: "claude-sonnet-4-5".into(),
        ..ev::ProviderResponseUsage::default()
    })
}

fn stream_answer(
    send: &dyn Fn(ev::UIEvent),
    session: &str,
    answer: &str,
    interrupted: impl Fn() -> bool,
) {
    send(ev::UIEvent::StreamBlockStart(ev::StreamBlockStart {
        session_id: session.to_string(),
        ts: now_ts(),
        ..ev::StreamBlockStart::default()
    }));
    for word in answer.split_inclusive(' ') {
        if interrupted() {
            break;
        }
        send(ev::UIEvent::StreamBlockDelta(ev::StreamBlockDelta {
            session_id: session.to_string(),
            ts: now_ts(),
            text: word.to_string(),
            ..ev::StreamBlockDelta::default()
        }));
        thread::sleep(Duration::from_millis(18));
    }
    send(ev::UIEvent::StreamBlockEnd(ev::StreamBlockEnd {
        session_id: session.to_string(),
        ts: now_ts(),
        ..ev::StreamBlockEnd::default()
    }));
}

/// Interrupted close-out: settle the turn the same durable way a live Esc
/// leaves it (cancel_completed + zero-yield prompt_complete).
fn finish_interrupted(send: &dyn Fn(ev::UIEvent), session: &str, _prompt: &str) {
    send(ev::UIEvent::CancelCompleted(ev::CancelCompleted {
        session_id: session.to_string(),
        ts: now_ts(),
        ..ev::CancelCompleted::default()
    }));
    send(ev::UIEvent::PromptComplete(ev::PromptComplete {
        session_id: session.to_string(),
        ts: now_ts(),
        ..ev::PromptComplete::default()
    }));
}

impl Runtime for DemoRuntime {
    fn submit(&mut self, prompt: String) {
        self.run_turn(prompt);
    }
    /// Answer the currently-parked approval (routes back into the worker thread).
    fn answer_approval(&mut self, _ticket_id: &str, choice: &str) {
        if let Some(tx) = self.approval_tx.take() {
            let _ = tx.send(is_allow(choice));
        }
    }
    fn interrupt(&mut self) {
        if let Some(tx) = self.interrupt_tx.take() {
            let _ = tx.send(());
        }
    }
}

// ---------------------------------------------------------------------------
// kernel/demo.py port — DemoScript: the mockup's demo turns as UIEvent scripts
// ---------------------------------------------------------------------------

/// Event sink (Python: `asyncio.Queue.put`).
pub type EmitFn = Box<dyn FnMut(ev::UIEvent) + Send>;
/// Pacing hook, seconds (Python `SleepFn`) — inject a no-op for instant,
/// zero-sleep runs; virtual time is unaffected.
pub type SleepFn = Box<dyn FnMut(f64) + Send>;
/// `(prompt, options) -> choice`, awaited for the chat-mode pytest approval
/// (Python `ApproverFn`). The default grants `Allow once` immediately.
pub type ApproverFn = Box<dyn FnMut(&str, &[String]) -> String + Send>;
/// `() -> Option<text>` polled once at every store-turn step boundary
/// (Python `SteerSourceFn` — the mockup's steer check).
pub type SteerSourceFn = Box<dyn FnMut() -> Option<String> + Send>;
/// `() -> mode_id` consulted at the store turn's approval step (Python
/// `ModeSourceFn` — the mockup's LIVE mode check).
pub type ModeSourceFn = Box<dyn FnMut() -> String + Send>;
/// `() -> bool` polled where Python reads the async-set `_interrupted` flag
/// (the synchronous engine's stand-in for `DemoRuntime.interrupt()` racing
/// the running script).
pub type InterruptPollFn = Box<dyn FnMut() -> bool + Send>;

/// Build one envelope-carrying [`ev::UIEvent`]: `demo-N` id, the scripted
/// demo session, virtual `ts` (Python `DemoRuntime._env`); the `child`
/// form parents a lane's sub-session to the root (`_child_env`, spec §8).
macro_rules! demo_event {
    ($s:expr, $variant:ident { $($f:ident : $v:expr),* $(,)? }) => {
        demo_event!(@build $s, $variant, demo::DEMO_SESSION_ID.to_string(), None, { $($f : $v),* })
    };
    ($s:expr, child $sid:expr, $variant:ident { $($f:ident : $v:expr),* $(,)? }) => {
        demo_event!(@build $s, $variant, $sid.to_string(), Some(demo::DEMO_SESSION_ID.to_string()), { $($f : $v),* })
    };
    (@build $s:expr, $variant:ident, $session:expr, $parent:expr, { $($f:ident : $v:expr),* }) => {{
        let (event_id, ts) = $s.next_envelope();
        #[allow(clippy::needless_update, clippy::redundant_field_names)]
        let event = ev::UIEvent::$variant(ev::$variant {
            event_id,
            session_id: $session,
            parent_id: $parent,
            ts,
            $($f: $v,)*
            ..Default::default()
        });
        event
    }};
}

/// Python `kernel.demo.DemoRuntime`: plays the scripted demo turns as
/// normalized UIEvents through the injected sink.
///
/// Determinism (module docstring, ported):
/// - **Timing** is virtual: the engine advances an internal millisecond
///   clock and calls the injectable [`SleepFn`] for real-time pacing;
///   every event carries the exact virtual `ts` it would have in real
///   time regardless of the sleep's speed.
/// - **Token ticks** follow the mockup formulas via the pinned
///   [`tick_tokens`] draws; event ids (`demo-N`) and `ts` are stamped
///   explicitly — no wall clock, no global counters.
pub struct DemoScript {
    emit: EmitFn,
    sleep: SleepFn,
    approver: ApproverFn,
    steer_source: Option<SteerSourceFn>,
    /// `() -> mode_id` (Python `mode_source`); `None` falls back to the
    /// turn spec's scripted mode.
    mode_source: Option<ModeSourceFn>,
    interrupt_poll: Option<InterruptPollFn>,
    clock_ms: i64,
    seq: u64,
    tool_seq: u64,
    group_seq: u64,
    turn_ms: i64,
    turn_tokens: u64,
    block_index: i64,
    request_id: String,
    ticks: Option<Vec<u64>>,
    running: bool,
    interrupted: bool,
    /// Verbatim user text echoed as this turn's user line (mockup
    /// `send()`/`drainQueue()`: `this.userLine(text)` keeps the typed
    /// text even though the turn script is fixed).
    prompt_override: Option<String>,
    /// One-shot: drop the next turn's scripted mode notice (mockup
    /// `drainQueue` runs a drained turn without any `setMode`).
    suppress_mode_notice: bool,
    /// Set when a turn breaks on esc: the live-telemetry close-out spec
    /// the adapter serves for that prompt (mockup `tele + " · interrupted"`).
    /// Cleared at the next turn start.
    pub interrupted_close: Option<DemoTurnSpec>,
}

impl DemoScript {
    pub fn new(emit: EmitFn) -> Self {
        Self {
            emit,
            sleep: Box::new(|seconds| thread::sleep(Duration::from_secs_f64(seconds))),
            // Python `_auto_allow`: grants `Allow once` immediately.
            approver: Box::new(|_prompt, _options| demo::APPROVAL_OPTIONS[0].to_string()),
            steer_source: None,
            mode_source: None,
            interrupt_poll: None,
            clock_ms: 0,
            seq: 0,
            tool_seq: 0,
            group_seq: 0,
            turn_ms: 0,
            turn_tokens: 0,
            block_index: 0,
            request_id: String::new(),
            ticks: None,
            running: false,
            interrupted: false,
            prompt_override: None,
            suppress_mode_notice: false,
            interrupted_close: None,
        }
    }

    pub fn set_sleep(&mut self, sleep: SleepFn) {
        self.sleep = sleep;
    }
    pub fn set_approver(&mut self, approver: ApproverFn) {
        self.approver = approver;
    }
    pub fn set_steer_source(&mut self, source: SteerSourceFn) {
        self.steer_source = Some(source);
    }
    pub fn set_mode_source(&mut self, source: ModeSourceFn) {
        self.mode_source = Some(source);
    }
    pub fn set_interrupt_poll(&mut self, poll: InterruptPollFn) {
        self.interrupt_poll = Some(poll);
    }

    // -- plumbing ---------------------------------------------------------

    /// Current virtual time in seconds (Python `DemoRuntime.clock`).
    pub fn clock(&self) -> f64 {
        self.clock_ms as f64 / 1000.0
    }

    fn next_envelope(&mut self) -> (String, f64) {
        self.seq += 1;
        (format!("demo-{}", self.seq), self.clock())
    }

    fn send(&mut self, event: ev::UIEvent) {
        (self.emit)(event);
    }

    /// Esc while running (mockup `if (this.running) this.interrupt = true`).
    ///
    /// The turns honor the flag at their next step boundary; returns
    /// `false` when no turn is running.
    pub fn interrupt(&mut self) -> bool {
        if !self.running {
            return false;
        }
        self.interrupted = true;
        true
    }

    /// Python reads the async-set `self._interrupted` at step boundaries;
    /// the synchronous engine latches the injected poll at the same sites.
    fn is_interrupted(&mut self) -> bool {
        if !self.interrupted {
            if let Some(poll) = self.interrupt_poll.as_mut() {
                if poll() {
                    self.interrupted = true;
                }
            }
        }
        self.interrupted
    }

    /// Advance virtual time, pacing via the injected sleep and emitting
    /// one usage tick at every whole-second boundary while a tick
    /// schedule is active (Python `_wait`).
    fn wait(&mut self, ms: i64) {
        let mut remaining = ms;
        while remaining > 0 {
            let step = remaining.min(1000 - self.turn_ms % 1000);
            (self.sleep)(step as f64 / 1000.0);
            self.turn_ms += step;
            self.clock_ms += step;
            remaining -= step;
            if self.turn_ms % 1000 == 0 {
                let tick = match self.ticks.as_mut() {
                    Some(ticks) if !ticks.is_empty() => Some(ticks.remove(0)),
                    _ => None,
                };
                if let Some(tick) = tick {
                    self.turn_tokens += tick;
                    self.usage(tick);
                }
            }
        }
    }

    /// One demo usage event: output tokens over the persistent cached
    /// prefix (`cache_read` memory bucket, `DEMO_MEMORY_TOKENS`).
    fn usage(&mut self, output_tokens: u64) {
        let event = demo_event!(self, ProviderResponseUsage {
            output_tokens: output_tokens as i64,
            cache_read: demo::DEMO_MEMORY_TOKENS,
            model: demo::DEMO_MODEL.to_string(),
        });
        self.send(event);
    }

    /// One assistant text block on both channels (A + durable B); the demo
    /// role travels in `StreamBlockStart.name` and `block["demo_role"]`.
    fn text(&mut self, text: &str, role: &str) {
        let index = self.block_index;
        self.block_index += 1;
        let request_id = self.request_id.clone();
        let event = demo_event!(self, StreamBlockStart {
            request_id: request_id.clone(),
            block_index: index,
            block_type: "text".to_string(),
            name: role.to_string(),
        });
        self.send(event);
        let event = demo_event!(self, StreamBlockDelta {
            request_id: request_id.clone(),
            block_index: index,
            block_type: "text".to_string(),
            sequence: 0,
            text: text.to_string(),
        });
        self.send(event);
        let event = demo_event!(self, StreamBlockEnd {
            request_id: request_id,
            block_index: index,
            block_type: "text".to_string(),
        });
        self.send(event);
        let event = demo_event!(self, ContentBlockEnd {
            block_type: "text".to_string(),
            block_index: index,
            block: obj(json!({"type": "text", "text": text, "demo_role": role})),
        });
        self.send(event);
    }

    /// One child-session Channel-A text burst — feeds the lane live tail.
    ///
    /// Channel A only: the child's durable record stays in its own
    /// transcript (lane focus), never the parent's (design doc D4).
    fn lane_stream(&mut self, lane: &DemoLane) {
        let request_id = format!("demo-req-{}", lane.name);
        let sid = lane.sub_session_id.clone();
        let event = demo_event!(self, child sid, StreamBlockStart {
            request_id: request_id.clone(),
            block_index: 0,
            block_type: "text".to_string(),
            name: "lane".to_string(),
        });
        self.send(event);
        let rows: Vec<String> = lane
            .log
            .iter()
            .filter(|row| matches!(row.kind, LogRowKind::Narration | LogRowKind::Answer))
            .map(|row| format!("{}\n", row.text))
            .collect();
        for (sequence, text) in rows.into_iter().enumerate() {
            let event = demo_event!(self, child sid, StreamBlockDelta {
                request_id: request_id.clone(),
                block_index: 0,
                block_type: "text".to_string(),
                sequence: sequence as i64,
                text: text,
            });
            self.send(event);
        }
        let event = demo_event!(self, child sid, StreamBlockEnd {
            request_id: request_id,
            block_index: 0,
            block_type: "text".to_string(),
        });
        self.send(event);
    }

    fn tool_pre(
        &mut self,
        tool_name: &str,
        tool_input: Map<String, Value>,
        group: Option<String>,
    ) -> String {
        self.tool_seq += 1;
        let call_id = format!("demo-call-{}", self.tool_seq);
        let event = demo_event!(self, ToolPre {
            tool_name: tool_name.to_string(),
            tool_call_id: call_id.clone(),
            tool_input: tool_input,
            parallel_group_id: group,
        });
        self.send(event);
        call_id
    }

    fn tool_post(
        &mut self,
        call_id: &str,
        tool_name: &str,
        tool_input: Map<String, Value>,
        result: Map<String, Value>,
    ) {
        let event = demo_event!(self, ToolPost {
            tool_name: tool_name.to_string(),
            tool_call_id: call_id.to_string(),
            tool_input: tool_input,
            result: result,
        });
        self.send(event);
    }

    fn tool(&mut self, tool_name: &str, tool_input: Map<String, Value>, result: Map<String, Value>) {
        let call_id = self.tool_pre(tool_name, tool_input.clone(), None);
        self.tool_post(&call_id, tool_name, tool_input, result);
    }

    /// Plan checklist as an `update_plan` tool call (Python `_plan`).
    fn plan(&mut self, title: &str, steps: &[&str], statuses: &[&str], read_only: bool) {
        debug_assert_eq!(steps.len(), statuses.len()); // Python zip(strict=True)
        let steps: Vec<Value> = steps
            .iter()
            .zip(statuses)
            .map(|(step, status)| json!({"step": step, "status": status}))
            .collect();
        self.tool(
            "update_plan",
            obj(json!({"title": title, "read_only": read_only, "steps": steps})),
            obj(json!({"ok": true})),
        );
    }

    /// Mirror the plan as a `todo` tool call (Python `_todo`).
    fn todo(&mut self, steps: &[&str], statuses: &[&str]) {
        debug_assert_eq!(steps.len(), statuses.len()); // Python zip(strict=True)
        let todos: Vec<Value> = steps
            .iter()
            .zip(statuses)
            .map(|(step, status)| {
                // Python `_TODO_STATUS_BY_PLAN` (KeyError on anything else).
                let status = match *status {
                    "pending" => "pending",
                    "active" => "in_progress",
                    "done" => "completed",
                    other => unreachable!("no todo status for plan status {other}"),
                };
                json!({"content": step, "status": status, "activeForm": step})
            })
            .collect();
        self.tool(
            "todo",
            obj(json!({"operation": "update", "todos": todos})),
            obj(json!({"ok": true})),
        );
    }

    /// Step boundary: consume one queued steer (Python `_apply_steer`).
    fn apply_steer(&mut self) {
        let Some(source) = self.steer_source.as_mut() else {
            return;
        };
        if let Some(text) = source() {
            if !text.is_empty() {
                self.text(&format!("Applying steer: {text}"), "narration");
            }
        }
    }

    fn begin_turn(&mut self, key: TurnKey) -> DemoTurnSpec {
        let spec = demo_turn_by_key(key).clone();
        self.turn_ms = 0;
        self.turn_tokens = 0;
        self.running = true;
        self.interrupted = false;
        self.interrupted_close = None;
        self.block_index = 0;
        self.request_id = format!("demo-req-{}", key.as_str());
        self.ticks = match key {
            TurnKey::Build | TurnKey::Auto | TurnKey::Agents => Some(tick_tokens(key, None)),
            _ => None,
        };
        if let Some(notice) = spec.mode_notice.clone() {
            if !self.suppress_mode_notice {
                let event = demo_event!(self, Notification {
                    message: notice,
                    source: "mode".to_string(),
                });
                self.send(event);
            }
        }
        self.suppress_mode_notice = false;
        // Python `self._prompt_override or spec.prompt` (empty is falsy).
        let prompt = self
            .prompt_override
            .take()
            .filter(|text| !text.is_empty())
            .unwrap_or_else(|| spec.prompt.clone());
        let event = demo_event!(self, PromptSubmit { prompt: prompt });
        self.send(event);
        let event = demo_event!(self, ExecutionStart {});
        self.send(event);
        spec
    }

    fn end_turn(&mut self, response: &str, notice: Option<&str>, status: ev::OrchestratorStatus) {
        self.ticks = None;
        self.running = false;
        let event = demo_event!(self, OrchestratorComplete {
            orchestrator: "demo".to_string(),
            turn_count: 1,
            status: status,
        });
        self.send(event);
        let event = demo_event!(self, ExecutionEnd {});
        self.send(event);
        let event = demo_event!(self, PromptComplete {
            response: response.to_string(),
        });
        self.send(event);
        if let Some(notice) = notice {
            let event = demo_event!(self, Notification {
                message: notice.to_string(),
                source: "turn".to_string(),
            });
            self.send(event);
        }
    }

    // -- turns --------------------------------------------------------------

    /// Session start → seed + five demo turns → session end (Python `run_all`).
    pub fn run_all(&mut self) {
        let event = demo_event!(self, SessionStart {});
        self.send(event);
        self.run_seed();
        self.run_build_turn();
        self.run_auto_turn();
        self.run_plan_turn();
        self.run_brainstorm_turn();
        self.run_agents_turn();
        let event = demo_event!(self, SessionEnd {});
        self.send(event);
    }

    /// Dispatch a single scripted turn by key (Python `run_turn`).
    ///
    /// `prompt` echoes the user's own text as the turn's user line;
    /// `queued` marks a queue-drained turn (scripted mode notice skipped
    /// so the `queued message picked up` notice stays visible).
    pub fn run_turn(&mut self, key: TurnKey, prompt: Option<String>, queued: bool) {
        self.prompt_override = prompt;
        self.suppress_mode_notice = queued;
        match key {
            TurnKey::Seed => self.run_seed(),
            TurnKey::Build => self.run_build_turn(),
            TurnKey::Auto => self.run_auto_turn(),
            TurnKey::Plan => self.run_plan_turn(),
            TurnKey::Brainstorm => self.run_brainstorm_turn(),
            TurnKey::Agents => self.run_agents_turn(),
        }
    }

    /// `seedTranscript()`: the pre-existing repo-explainer turn.
    pub fn run_seed(&mut self) {
        let spec = self.begin_turn(TurnKey::Seed);
        self.text(demo::SEED_NARRATION, "narration");
        self.group_seq += 1;
        let group = format!("demo-group-{}", self.group_seq);
        let call_ids: Vec<String> = demo::SEED_COMMANDS
            .iter()
            .map(|command| self.tool_pre("bash", obj(json!({"command": command})), Some(group.clone())))
            .collect();
        for (call_id, command) in call_ids.iter().zip(demo::SEED_COMMANDS.iter()) {
            self.tool_post(
                call_id,
                "bash",
                obj(json!({"command": command})),
                obj(json!({"output": "(output collapsed)"})),
            );
        }
        self.text(demo::SEED_ANSWER, "answer");
        self.usage(spec.tokens);
        self.end_turn(demo::SEED_ANSWER, None, ev::OrchestratorStatus::Success);
    }

    /// `runTurn(false)` in chat mode — pytest approval on step 2.
    pub fn run_build_turn(&mut self) {
        self.run_store_turn(false);
    }

    /// `runTurn(true)` — force-push block + deferred decision.
    pub fn run_auto_turn(&mut self) {
        self.run_store_turn(true);
    }

    fn run_store_turn(&mut self, auto: bool) {
        let key = if auto { TurnKey::Auto } else { TurnKey::Build };
        let spec = self.begin_turn(key);
        let mut statuses: Vec<&'static str> = vec!["pending"; demo::STORE_STEPS.len()];
        self.plan(demo::STORE_PLAN_TITLE, &demo::STORE_STEPS, &statuses, false);
        self.todo(&demo::STORE_STEPS, &statuses);
        let mut denied = false;
        for (i, command) in demo::STORE_COMMANDS.iter().enumerate() {
            if self.is_interrupted() {
                break; // mockup: step-boundary break
            }
            self.apply_steer();
            statuses[i] = "active";
            self.plan(demo::STORE_PLAN_TITLE, &demo::STORE_STEPS, &statuses, false);
            self.todo(&demo::STORE_STEPS, &statuses);
            self.text(demo::STORE_NARRATIONS[i], "narration");
            self.wait(1300);
            if self.is_interrupted() {
                break;
            }
            if auto && i == 2 {
                let tool_input = obj(json!({"command": demo::FORCE_PUSH_COMMAND}));
                let call_id = self.tool_pre("bash", tool_input.clone(), None);
                self.wait(900);
                self.tool_post(
                    &call_id,
                    "bash",
                    tool_input,
                    obj(json!({
                        "status": "denied",
                        "reason": demo::AUTO_BLOCK_REASON,
                        "continuation": demo::AUTO_BLOCK_CONTINUATION,
                    })),
                );
                let event = demo_event!(self, ApprovalDenied {
                    prompt: demo::FORCE_PUSH_COMMAND.to_string(),
                    reason: demo::AUTO_BLOCK_REASON.to_string(),
                });
                self.send(event);
                self.wait(900);
                self.text(demo::AUTO_DEFER_NARRATION, "narration");
                let event = demo_event!(self, Notification {
                    message: demo::AUTO_DEFER_NOTICE.to_string(),
                    level: "decision".to_string(),
                    source: "needs_you".to_string(),
                });
                self.send(event);
            } else {
                // Mockup: LIVE mode at the step boundary gates the pytest
                // approval (`if (this.mode().id === "chat" && i === 1)`) —
                // spec §4: build trust is `auto read,test`, so any
                // non-chat mode auto-runs pytest with no ask.
                let live_mode = match self.mode_source.as_mut() {
                    Some(source) => source(),
                    None => spec.mode.to_string(),
                };
                if !auto && i == 1 && live_mode == "chat" {
                    let options: Vec<String> =
                        demo::APPROVAL_OPTIONS.iter().map(|s| s.to_string()).collect();
                    let event = demo_event!(self, ApprovalRequired {
                        prompt: demo::PYTEST_APPROVAL_PROMPT.to_string(),
                        options: options.clone(),
                    });
                    self.send(event);
                    let choice = (self.approver)(demo::PYTEST_APPROVAL_PROMPT, &options);
                    if choice == "Deny" {
                        let event = demo_event!(self, ApprovalDenied {
                            prompt: demo::PYTEST_APPROVAL_PROMPT.to_string(),
                            reason: demo::DENY_REASON.to_string(),
                            command: demo::DENY_BLOCKED_CMD.to_string(),
                            continuation: demo::DENY_CONTINUATION.to_string(),
                        });
                        self.send(event);
                        denied = true;
                        statuses[i] = "done";
                        self.plan(demo::STORE_PLAN_TITLE, &demo::STORE_STEPS, &statuses, false);
                        self.todo(&demo::STORE_STEPS, &statuses);
                        continue;
                    }
                    let event = demo_event!(self, ApprovalGranted {
                        prompt: demo::PYTEST_APPROVAL_PROMPT.to_string(),
                        choice: choice,
                    });
                    self.send(event);
                }
                let tool_input = obj(json!({"command": command}));
                let call_id = self.tool_pre("bash", tool_input.clone(), None);
                self.wait(1400);
                if self.is_interrupted() {
                    // Mockup breaks before rm(cmdLine): the live `└ $ cmd`
                    // line stays in the transcript, no collapsed tool line.
                    break;
                }
                self.tool_post(&call_id, "bash", tool_input, obj(json!({"output": "(output collapsed)"})));
            }
            statuses[i] = "done";
            self.plan(demo::STORE_PLAN_TITLE, &demo::STORE_STEPS, &statuses, false);
            self.todo(&demo::STORE_STEPS, &statuses);
            self.wait(400);
        }
        self.ticks = None;
        if self.interrupted {
            // Mockup interrupt close-out: italic recap + `· interrupted`
            // rule from the actual elapsed secs/toks; the end notice
            // (`turn interrupted · context saved`) comes from the UI.
            self.interrupted_close =
                Some(interrupted_spec(key, self.turn_ms / 1000, self.turn_tokens));
            self.text(demo::INTERRUPTED_RECAP, "recap");
            self.end_turn("", None, ev::OrchestratorStatus::Cancelled);
            return;
        }
        let answer = if auto {
            demo::AUTO_ANSWER.to_string()
        } else {
            build_answer(denied)
        };
        let recap = if auto { demo::AUTO_RECAP } else { demo::BUILD_RECAP };
        self.text(&answer, "answer");
        self.text(recap, "recap");
        // Mockup: the auto (blocked) turn ends with no notice at all.
        let notice = if auto { None } else { spec.end_notice.as_deref() };
        self.end_turn(&answer, notice, ev::OrchestratorStatus::Success);
    }

    /// Step-boundary esc check for non-store turns (DESIGN-SPEC §11).
    ///
    /// Same close-out as the store turns: italic recap + `· interrupted`
    /// rule from the actual elapsed secs/toks; `true` when the turn ended.
    fn close_interrupted(&mut self, spec: &DemoTurnSpec) -> bool {
        if !self.is_interrupted() {
            return false;
        }
        self.ticks = None;
        self.interrupted_close =
            Some(interrupted_spec(spec.key, self.turn_ms / 1000, self.turn_tokens));
        self.text(demo::INTERRUPTED_RECAP, "recap");
        self.end_turn("", None, ev::OrchestratorStatus::Cancelled);
        true
    }

    /// `runPlanTurn()`: read-only proposed plan, steps landing live.
    pub fn run_plan_turn(&mut self) {
        let spec = self.begin_turn(TurnKey::Plan);
        self.text(demo::PLAN_NARRATION, "narration");
        self.wait(1400);
        self.plan(demo::PLAN_TITLE, &[], &[], true);
        for count in 1..=demo::PLAN_STEPS.len() {
            self.wait(500);
            if self.close_interrupted(&spec) {
                return;
            }
            let statuses = vec!["pending"; count];
            self.plan(demo::PLAN_TITLE, &demo::PLAN_STEPS[..count], &statuses, true);
        }
        self.wait(700);
        if self.close_interrupted(&spec) {
            return;
        }
        self.text(demo::PLAN_RECAP, "recap");
        self.usage(spec.tokens);
        self.end_turn("", spec.end_notice.as_deref(), ev::OrchestratorStatus::Success);
    }

    /// `runBrainstormTurn()`: no tools, four ideas, recap.
    pub fn run_brainstorm_turn(&mut self) {
        let spec = self.begin_turn(TurnKey::Brainstorm);
        self.text(demo::BRAINSTORM_NARRATION, "narration");
        self.wait(1200);
        for idea in demo::BRAINSTORM_IDEAS {
            if self.close_interrupted(&spec) {
                return;
            }
            self.text(idea, "idea");
            self.wait(450);
        }
        if self.close_interrupted(&spec) {
            return;
        }
        self.text(demo::BRAINSTORM_RECAP, "recap");
        self.usage(spec.tokens);
        self.end_turn("", None, ev::OrchestratorStatus::Success);
    }

    /// `runAgentsTurn()`: researcher/coder/tester fan-out.
    pub fn run_agents_turn(&mut self) {
        let spec = self.begin_turn(TurnKey::Agents);
        self.text(demo::AGENTS_NARRATION, "narration");
        // Scripted todo beats (ambient-progress Phase 2): three lane steps
        // start together; "synthesize findings" completes with the last lane.
        let mut statuses: Vec<&'static str> = vec!["active", "active", "active", "pending"];
        self.todo(&demo::AGENTS_PLAN_STEPS, &statuses);
        for lane in demo_lanes() {
            let event = demo_event!(self, AgentSpawned {
                agent: lane.name.clone(),
                sub_session_id: lane.sub_session_id.clone(),
                parent_session_id: demo::DEMO_SESSION_ID.to_string(),
            });
            self.send(event);
        }
        for lane in demo_lanes() {
            self.lane_stream(lane);
        }
        let mut order: Vec<&DemoLane> = demo_lanes().iter().collect();
        order.sort_by_key(|lane| lane.done_at_ms);
        let mut elapsed = 0;
        for lane in order {
            self.wait(lane.done_at_ms - elapsed);
            elapsed = lane.done_at_ms;
            if self.close_interrupted(&spec) {
                return;
            }
            let event = demo_event!(self, AgentCompleted {
                agent: lane.name.clone(),
                sub_session_id: lane.sub_session_id.clone(),
                parent_session_id: demo::DEMO_SESSION_ID.to_string(),
                success: true,
                result: lane.result.clone(),
            });
            self.send(event);
            statuses[agents_step_index(&lane.name)] = "done";
            if statuses[..3].iter().all(|status| *status == "done") {
                statuses[3] = "done";
            }
            self.todo(&demo::AGENTS_PLAN_STEPS, &statuses);
        }
        self.ticks = None;
        self.text(demo::AGENTS_ANSWER, "answer");
        self.end_turn(
            demo::AGENTS_ANSWER,
            spec.end_notice.as_deref(),
            ev::OrchestratorStatus::Success,
        );
    }
}

/// Python `_AGENTS_STEP_BY_LANE`.
fn agents_step_index(name: &str) -> usize {
    match name {
        "researcher" => 0,
        "coder" => 1,
        "tester" => 2,
        other => unreachable!("no scripted agents step for lane {other}"),
    }
}

// ---------------------------------------------------------------------------
// ScriptedDemoRuntime — DemoScript behind the Runtime seam
// ---------------------------------------------------------------------------

/// The Python `--demo` adapter's runtime half: plays [`DemoScript`] turns on
/// a worker thread, one per submit, selecting the scripted turn with the
/// same key bookkeeping as the adapter's [`DemoWiring`] (verbatim mockup
/// prompt → that turn, else the next unplayed turn in mockup order; both
/// sides see the same submits, so both resolve the same keys). Approvals
/// surface as the ticket-bearing wire record (`demo-ticket-N`, Python
/// `_approve`) and park the script until [`Runtime::answer_approval`]
/// routes the choice back.
pub struct ScriptedDemoRuntime {
    script: Arc<Mutex<DemoScript>>,
    /// Prompt→turn-key bookkeeping (Python `_key_for`/`_played`), mirroring
    /// the adapter's shared wiring.
    wiring: DemoWiring,
    approval_tx: Sender<String>,
    interrupt_flag: Arc<AtomicBool>,
    running: Arc<AtomicBool>,
}

impl ScriptedDemoRuntime {
    pub fn new(tx: Sender<Msg>) -> Self {
        let interrupt_flag = Arc::new(AtomicBool::new(false));
        let running = Arc::new(AtomicBool::new(false));
        let (approval_tx, approval_rx) = channel::<String>();
        let emit_tx = tx.clone();
        let mut script = DemoScript::new(Box::new(move |event| {
            let _ = emit_tx.send(Msg::Rt(WireEvent::Event(event)));
        }));
        // Python `_approve`: mint a ticket, present it, await the choice.
        let mut ticket_seq = 0u64;
        script.set_approver(Box::new(move |prompt, options| {
            ticket_seq += 1;
            let _ = tx.send(Msg::Rt(WireEvent::Approval {
                ticket_id: format!("demo-ticket-{ticket_seq}"),
                prompt: prompt.to_string(),
                options: options.to_vec(),
            }));
            // A closed channel fails closed (deny), like a dropped future.
            approval_rx.recv().unwrap_or_else(|_| "Deny".to_string())
        }));
        let poll_flag = Arc::clone(&interrupt_flag);
        script.set_interrupt_poll(Box::new(move || poll_flag.load(Ordering::SeqCst)));
        let mut wiring = DemoWiring::new();
        // The adapter replays the seed at start (Python `start()`); keep the
        // key bookkeeping aligned with it.
        wiring.mark_seed_played();
        Self {
            script: Arc::new(Mutex::new(script)),
            wiring,
            approval_tx,
            interrupt_flag,
            running,
        }
    }

    /// Zero-sleep playback (tests/headless runs); virtual `ts` unaffected.
    pub fn set_instant(&self) {
        if let Ok(mut script) = self.script.lock() {
            script.set_sleep(Box::new(|_| {}));
        }
    }

    /// Replay the seed transcript (Python adapter `start()` runs the seed
    /// turn live before the first submit).
    pub fn play_seed(&mut self) {
        self.spawn_turn(TurnKey::Seed, None, false);
    }

    /// Queue-drained turn: the scripted mode notice is suppressed so the
    /// `queued message picked up` notice stays visible (Python
    /// `submit_queued`, spec §5).
    pub fn submit_queued(&mut self, prompt: String) {
        let text = prompt.trim().to_string();
        let key = self.wiring.record_submit(&text);
        self.spawn_turn(key, Some(text), true);
    }

    /// The live-telemetry close-out of an esc-interrupted turn (Python
    /// adapter `turn_spec` reads `runtime.interrupted_close`) — bridge it
    /// into the adapter wiring's `set_interrupted_close`.
    pub fn interrupted_close(&self) -> Option<DemoTurnSpec> {
        self.script
            .lock()
            .ok()
            .and_then(|script| script.interrupted_close.clone())
    }

    fn spawn_turn(&mut self, key: TurnKey, prompt: Option<String>, queued: bool) {
        self.interrupt_flag.store(false, Ordering::SeqCst);
        self.running.store(true, Ordering::SeqCst);
        let script = Arc::clone(&self.script);
        let running = Arc::clone(&self.running);
        thread::spawn(move || {
            // Turns serialize on the script lock (the adapter submits one
            // turn at a time; a stray double-submit just queues here).
            if let Ok(mut script) = script.lock() {
                script.run_turn(key, prompt, queued);
            }
            running.store(false, Ordering::SeqCst);
        });
    }
}

impl Runtime for ScriptedDemoRuntime {
    fn submit(&mut self, prompt: String) {
        let text = prompt.trim().to_string();
        let key = self.wiring.record_submit(&text);
        self.spawn_turn(key, Some(text), false);
    }
    fn answer_approval(&mut self, _ticket_id: &str, choice: &str) {
        let _ = self.approval_tx.send(choice.to_string());
    }
    fn interrupt(&mut self) {
        // Python: esc is a no-op unless a turn is running.
        if self.running.load(Ordering::SeqCst) {
            self.interrupt_flag.store(true, Ordering::SeqCst);
        }
    }
}

// ---------------------------------------------------------------------------
// Tests — pinned 1:1 from tests/test_kernel_demo_turns.py (15 cases), plus
// oracle-verified interrupt close-out and ScriptedDemoRuntime wiring checks.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod demo_script_tests {
    use super::*;
    use std::collections::HashSet;

    type Events = Arc<Mutex<Vec<ev::UIEvent>>>;

    /// A zero-sleep engine writing into a shared event vector (the Python
    /// tests' `DemoRuntime(sleep=_instant)` + queue drain).
    fn instant_script() -> (DemoScript, Events) {
        let events: Events = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&events);
        let mut script = DemoScript::new(Box::new(move |event| {
            sink.lock().expect("event sink").push(event);
        }));
        script.set_sleep(Box::new(|_| {}));
        (script, events)
    }

    /// Run one scripted turn instantly and drain its events (Python `play`).
    fn play(run: impl FnOnce(&mut DemoScript)) -> (DemoScript, Vec<ev::UIEvent>) {
        let (mut script, events) = instant_script();
        run(&mut script);
        let drained = events.lock().expect("event sink").clone();
        (script, drained)
    }

    fn kinds(events: &[ev::UIEvent]) -> Vec<&'static str> {
        events.iter().map(|event| event.kind()).collect()
    }

    /// Durable text-block contents in order (Python `texts`).
    fn texts(events: &[ev::UIEvent]) -> Vec<String> {
        events
            .iter()
            .filter_map(|event| match event {
                ev::UIEvent::ContentBlockEnd(block) => {
                    Some(block.block["text"].as_str().expect("text block").to_string())
                }
                _ => None,
            })
            .collect()
    }

    fn usage_tokens(events: &[ev::UIEvent]) -> Vec<i64> {
        events
            .iter()
            .filter_map(|event| match event {
                ev::UIEvent::ProviderResponseUsage(usage) => Some(usage.output_tokens),
                _ => None,
            })
            .collect()
    }

    fn tool_pres(events: &[ev::UIEvent]) -> Vec<&ev::ToolPre> {
        events
            .iter()
            .filter_map(|event| match event {
                ev::UIEvent::ToolPre(pre) => Some(pre),
                _ => None,
            })
            .collect()
    }

    fn bash_commands(events: &[ev::UIEvent]) -> Vec<String> {
        tool_pres(events)
            .into_iter()
            .filter(|pre| pre.tool_name == "bash")
            .map(|pre| pre.tool_input["command"].as_str().expect("command").to_string())
            .collect()
    }

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| item.to_string()).collect()
    }

    // Python sequence vocabulary: TEXT / PLAN / TODO / U.
    const TEXT: [&str; 4] = [
        "stream_block_start",
        "stream_block_delta",
        "stream_block_end",
        "content_block_end",
    ];
    const PLAN: [&str; 2] = ["tool_pre", "tool_post"];
    const TODO: [&str; 2] = ["tool_pre", "tool_post"];
    const U: [&str; 1] = ["provider_response_usage"];

    fn cat(parts: &[&[&'static str]]) -> Vec<&'static str> {
        parts.iter().flat_map(|part| part.iter().copied()).collect()
    }

    // -- seed transcript ----------------------------------------------------

    /// Pins Python `test_seed_sequence`.
    #[test]
    fn test_seed_sequence() {
        let (_script, events) = play(|script| script.run_seed());
        assert_eq!(
            kinds(&events),
            cat(&[
                &["prompt_submit", "execution_start"],
                &TEXT,
                &["tool_pre", "tool_pre", "tool_post", "tool_post"],
                &TEXT,
                &["provider_response_usage", "orchestrator_complete", "execution_end", "prompt_complete"],
            ])
        );
        let ev::UIEvent::PromptSubmit(submit) = &events[0] else {
            panic!("prompt_submit first");
        };
        assert_eq!(submit.prompt, "explain what this repo is in simple terms");
        assert_eq!(bash_commands(&events), strings(&demo::SEED_COMMANDS));
        // Two parallel shell calls share one batch group (one dim line per batch).
        let pres = tool_pres(&events);
        let groups: HashSet<Option<String>> =
            pres.iter().map(|pre| pre.parallel_group_id.clone()).collect();
        assert_eq!(groups.len(), 1);
        assert!(pres[0].parallel_group_id.is_some());
        assert_eq!(texts(&events).last().map(String::as_str), Some(demo::SEED_ANSWER));
        assert_eq!(usage_tokens(&events), vec![83_900]);
        let ev::UIEvent::PromptComplete(complete) = events.last().expect("events") else {
            panic!("prompt_complete last");
        };
        assert_eq!(complete.response, demo::SEED_ANSWER);
    }

    // -- build turn (runTurn(false), chat mode, approval allowed) -----------

    /// Python `_BUILD_KINDS`.
    fn build_kinds() -> Vec<&'static str> {
        cat(&[
            &["prompt_submit", "execution_start"],
            &PLAN,
            &TODO, // plan seeded: all pending
            // step 0
            &PLAN, &TODO, &TEXT, &U, &["tool_pre"], &U, &["tool_post"], &PLAN, &TODO, &U,
            // step 1 — chat-mode pytest approval
            &PLAN, &TODO, &TEXT, &U,
            &["approval_required", "approval_granted"],
            &["tool_pre"], &U, &["tool_post"], &PLAN, &TODO, &U,
            // step 2
            &PLAN, &TODO, &TEXT, &U, &["tool_pre"], &U, &["tool_post"], &PLAN, &TODO, &U,
            &TEXT, // answer
            &TEXT, // recap
            &["orchestrator_complete", "execution_end", "prompt_complete", "notification"],
        ])
    }

    /// Pins Python `test_build_turn_full_sequence`.
    #[test]
    fn test_build_turn_full_sequence() {
        let (script, events) = play(|script| script.run_build_turn());
        assert_eq!(kinds(&events), build_kinds());
        assert_eq!(script.clock(), 9.3); // 3 × (1300 + 1400 + 400) ms of virtual time
        let ev::UIEvent::Notification(last) = events.last().expect("events") else {
            panic!("end notice last");
        };
        assert_eq!(last.message, demo::BUILD_END_NOTICE);
        assert_eq!(bash_commands(&events), strings(&demo::STORE_COMMANDS));
        assert_eq!(texts(&events)[..1], strings(&[demo::STORE_NARRATIONS[0]]));
    }

    /// Pins Python `test_build_turn_token_ticks_match_mockup_formula`.
    #[test]
    fn test_build_turn_token_ticks_match_mockup_formula() {
        let (_script, events) = play(|script| script.run_build_turn());
        let expected: Vec<i64> = tick_tokens(TurnKey::Build, None)
            .into_iter()
            .map(|tick| tick as i64)
            .collect();
        assert_eq!(usage_tokens(&events), expected);
        assert_eq!(usage_tokens(&events).len(), 9); // one tick per virtual second
        assert!(usage_tokens(&events).iter().all(|tick| (380..=639).contains(tick)));
    }

    /// Per-call status lists for one checklist tool (plan/todo progression).
    fn checklist_statuses(
        events: &[ev::UIEvent],
        tool_name: &str,
        list_key: &str,
    ) -> Vec<Vec<String>> {
        tool_pres(events)
            .into_iter()
            .filter(|pre| pre.tool_name == tool_name)
            .map(|pre| {
                pre.tool_input[list_key]
                    .as_array()
                    .expect("checklist array")
                    .iter()
                    .map(|item| item["status"].as_str().expect("status").to_string())
                    .collect()
            })
            .collect()
    }

    /// Pins Python `test_build_turn_plan_progression`.
    #[test]
    fn test_build_turn_plan_progression() {
        let (_script, events) = play(|script| script.run_build_turn());
        assert_eq!(
            checklist_statuses(&events, "update_plan", "steps"),
            vec![
                strings(&["pending", "pending", "pending"]),
                strings(&["active", "pending", "pending"]),
                strings(&["done", "pending", "pending"]),
                strings(&["done", "active", "pending"]),
                strings(&["done", "done", "pending"]),
                strings(&["done", "done", "active"]),
                strings(&["done", "done", "done"]),
            ]
        );
        let plans: Vec<&ev::ToolPre> = tool_pres(&events)
            .into_iter()
            .filter(|pre| pre.tool_name == "update_plan")
            .collect();
        assert!(plans
            .iter()
            .all(|plan| plan.tool_input["title"] == "Refactor session store"));
        assert_eq!(plans[0].tool_input["read_only"], Value::Bool(false));
        let steps: Vec<String> = plans[0].tool_input["steps"]
            .as_array()
            .expect("steps")
            .iter()
            .map(|step| step["step"].as_str().expect("step").to_string())
            .collect();
        assert_eq!(steps, strings(&demo::STORE_STEPS));
    }

    /// Pins Python `test_build_turn_todo_progression_mirrors_the_plan`.
    #[test]
    fn test_build_turn_todo_progression_mirrors_the_plan() {
        let (_script, events) = play(|script| script.run_build_turn());
        assert_eq!(
            checklist_statuses(&events, "todo", "todos"),
            vec![
                strings(&["pending", "pending", "pending"]),
                strings(&["in_progress", "pending", "pending"]),
                strings(&["completed", "pending", "pending"]),
                strings(&["completed", "in_progress", "pending"]),
                strings(&["completed", "completed", "pending"]),
                strings(&["completed", "completed", "in_progress"]),
                strings(&["completed", "completed", "completed"]),
            ]
        );
        let todos: Vec<&ev::ToolPre> = tool_pres(&events)
            .into_iter()
            .filter(|pre| pre.tool_name == "todo")
            .collect();
        assert!(todos
            .iter()
            .all(|todo| todo.tool_input["operation"] == "update"));
        let contents: Vec<String> = todos[0].tool_input["todos"]
            .as_array()
            .expect("todos")
            .iter()
            .map(|todo| todo["content"].as_str().expect("content").to_string())
            .collect();
        assert_eq!(contents, strings(&demo::STORE_STEPS));
    }

    /// `(prompt, options)` pairs an injected approver saw.
    type ApproverCalls = Arc<Mutex<Vec<(String, Vec<String>)>>>;

    /// Pins Python `test_build_turn_approval_contract`.
    #[test]
    fn test_build_turn_approval_contract() {
        let seen: ApproverCalls = Arc::new(Mutex::new(Vec::new()));
        let (mut script, events) = instant_script();
        let record = Arc::clone(&seen);
        script.set_approver(Box::new(move |prompt, options| {
            record
                .lock()
                .expect("approver record")
                .push((prompt.to_string(), options.to_vec()));
            "Allow always".to_string()
        }));
        script.run_build_turn();
        let events = events.lock().expect("event sink").clone();
        assert_eq!(
            *seen.lock().expect("approver record"),
            vec![(
                demo::PYTEST_APPROVAL_PROMPT.to_string(),
                strings(&demo::APPROVAL_OPTIONS)
            )]
        );
        let required = events
            .iter()
            .find_map(|event| match event {
                ev::UIEvent::ApprovalRequired(required) => Some(required),
                _ => None,
            })
            .expect("approval_required");
        assert_eq!(required.prompt, demo::PYTEST_APPROVAL_PROMPT);
        assert_eq!(required.options, strings(&["Allow once", "Allow always", "Deny"]));
        let granted = events
            .iter()
            .find_map(|event| match event {
                ev::UIEvent::ApprovalGranted(granted) => Some(granted),
                _ => None,
            })
            .expect("approval_granted");
        assert_eq!(granted.choice, "Allow always");
    }

    /// Pins Python `test_build_turn_skips_approval_outside_chat_mode`:
    /// spec §4 / mockup `if (this.mode().id === "chat" && i === 1)` — the
    /// pytest approval is gated on the LIVE mode; build trust is
    /// `auto read,test`, so pytest auto-runs with no ask.
    #[test]
    fn test_build_turn_skips_approval_outside_chat_mode() {
        let (mut script, events) = instant_script();
        script.set_mode_source(Box::new(|| "build".to_string()));
        script.run_build_turn();
        let events = events.lock().expect("event sink").clone();
        assert!(!kinds(&events)
            .iter()
            .any(|kind| *kind == "approval_required" || *kind == "approval_granted"));
        // pytest still runs (auto read,test) — all three commands execute.
        assert_eq!(bash_commands(&events), strings(&demo::STORE_COMMANDS));
    }

    /// Pins Python `test_build_turn_deny_path`.
    #[test]
    fn test_build_turn_deny_path() {
        let (mut script, events) = instant_script();
        script.set_approver(Box::new(|_prompt, _options| "Deny".to_string()));
        script.run_build_turn();
        let events = events.lock().expect("event sink").clone();
        let expected = cat(&[
            &["prompt_submit", "execution_start"],
            &PLAN, &TODO,
            &PLAN, &TODO, &TEXT, &U, &["tool_pre"], &U, &["tool_post"], &PLAN, &TODO, &U,
            &PLAN, &TODO, &TEXT, &U,
            &["approval_required", "approval_denied"],
            &PLAN, &TODO,
            &PLAN, &TODO, &TEXT, &U, &["tool_pre"], &U, &U, &["tool_post"], &PLAN, &TODO,
            &TEXT, &TEXT,
            &["orchestrator_complete", "execution_end", "prompt_complete", "notification"],
        ]);
        assert_eq!(kinds(&events), expected);
        let denied = events
            .iter()
            .find_map(|event| match event {
                ev::UIEvent::ApprovalDenied(denied) => Some(denied),
                _ => None,
            })
            .expect("approval_denied");
        assert_eq!(denied.prompt, demo::PYTEST_APPROVAL_PROMPT);
        assert_eq!(denied.reason, "denied by user");
        // The pytest command never runs; the denied step still completes.
        assert_eq!(
            bash_commands(&events),
            strings(&[demo::STORE_COMMANDS[0], demo::STORE_COMMANDS[2]])
        );
        let all_texts = texts(&events);
        assert!(all_texts[all_texts.len() - 2].contains("(tests skipped by your denial)"));
        // Deny path: 7 virtual seconds, first 7 formula draws.
        assert_eq!(script.clock(), 7.5);
        let expected_ticks: Vec<i64> = tick_tokens(TurnKey::Build, Some(7))
            .into_iter()
            .map(|tick| tick as i64)
            .collect();
        assert_eq!(usage_tokens(&events), expected_ticks);
    }

    // -- auto turn (runTurn(true)): force-push block + deferred decision ----

    /// Python `_AUTO_KINDS`.
    fn auto_kinds() -> Vec<&'static str> {
        cat(&[
            &["notification", "prompt_submit", "execution_start"],
            &PLAN, &TODO,
            &PLAN, &TODO, &TEXT, &U, &["tool_pre"], &U, &["tool_post"], &PLAN, &TODO, &U,
            &PLAN, &TODO, &TEXT, &U, &["tool_pre"], &U, &["tool_post"], &PLAN, &TODO, &U,
            &PLAN, &TODO, &TEXT, &U, &["tool_pre"], &U,
            &["tool_post", "approval_denied"],
            &U,
            &TEXT,             // defer narration
            &["notification"], // decision deferred to needs-you
            &PLAN, &TODO,
            &TEXT, &TEXT,
            &["orchestrator_complete", "execution_end", "prompt_complete"], // no end notice
        ])
    }

    /// Pins Python `test_auto_turn_full_sequence`.
    #[test]
    fn test_auto_turn_full_sequence() {
        let (script, events) = play(|script| script.run_auto_turn());
        assert_eq!(kinds(&events), auto_kinds());
        assert_eq!(script.clock(), 9.7);
        let ev::UIEvent::Notification(first) = &events[0] else {
            panic!("mode notice first");
        };
        assert_eq!(first.message, demo::AUTO_MODE_NOTICE);
        assert_eq!(first.source, "mode");
        // Mockup: the blocked turn ends with NO turn-end notice.
        assert_eq!(events.last().expect("events").kind(), "prompt_complete");
    }

    /// Pins Python `test_auto_turn_force_push_block`.
    #[test]
    fn test_auto_turn_force_push_block() {
        let (_script, events) = play(|script| script.run_auto_turn());
        let force_pre = tool_pres(&events)
            .into_iter()
            .find(|pre| pre.tool_input.get("command") == Some(&Value::from(demo::FORCE_PUSH_COMMAND)))
            .expect("force-push tool_pre");
        let force_post = events
            .iter()
            .find_map(|event| match event {
                ev::UIEvent::ToolPost(post) if post.tool_call_id == force_pre.tool_call_id => {
                    Some(post)
                }
                _ => None,
            })
            .expect("force-push tool_post");
        assert_eq!(
            force_post.result,
            obj(json!({
                "status": "denied",
                "reason": demo::AUTO_BLOCK_REASON,
                "continuation": "finding safer path",
            }))
        );
        let denied = events
            .iter()
            .find_map(|event| match event {
                ev::UIEvent::ApprovalDenied(denied) => Some(denied),
                _ => None,
            })
            .expect("approval_denied");
        assert_eq!(denied.prompt, demo::FORCE_PUSH_COMMAND);
        assert_eq!(denied.reason, demo::AUTO_BLOCK_REASON);
        let deferred = events
            .iter()
            .find_map(|event| match event {
                ev::UIEvent::Notification(notice) if notice.source == "needs_you" => Some(notice),
                _ => None,
            })
            .expect("deferred-decision notice");
        assert_eq!(deferred.message, demo::AUTO_DEFER_NOTICE);
        assert_eq!(deferred.level, "decision");
        let expected_ticks: Vec<i64> = tick_tokens(TurnKey::Auto, None)
            .into_iter()
            .map(|tick| tick as i64)
            .collect();
        assert_eq!(usage_tokens(&events), expected_ticks);
    }

    // -- plan turn -----------------------------------------------------------

    /// Pins Python `test_plan_turn_sequence`.
    #[test]
    fn test_plan_turn_sequence() {
        let (script, events) = play(|script| script.run_plan_turn());
        assert_eq!(
            kinds(&events),
            cat(&[
                &["notification", "prompt_submit", "execution_start"],
                &TEXT,
                &PLAN, &PLAN, &PLAN, &PLAN,
                &TEXT,
                &[
                    "provider_response_usage",
                    "orchestrator_complete",
                    "execution_end",
                    "prompt_complete",
                    "notification",
                ],
            ])
        );
        assert_eq!(script.clock(), 3.6);
        let ev::UIEvent::Notification(first) = &events[0] else {
            panic!("mode notice first");
        };
        assert_eq!(first.message, "mode plan · read-only");
        let plans = tool_pres(&events);
        // Head lands first, then steps stream in one at a time — all read-only.
        let step_counts: Vec<usize> = plans
            .iter()
            .map(|plan| plan.tool_input["steps"].as_array().expect("steps").len())
            .collect();
        assert_eq!(step_counts, vec![0, 1, 2, 3]);
        assert!(plans
            .iter()
            .all(|plan| plan.tool_input["read_only"] == Value::Bool(true)));
        assert!(plans
            .iter()
            .all(|plan| plan.tool_input["title"] == demo::PLAN_TITLE));
        let last_steps: Vec<String> = plans
            .last()
            .expect("plans")
            .tool_input["steps"]
            .as_array()
            .expect("steps")
            .iter()
            .map(|step| step["step"].as_str().expect("step").to_string())
            .collect();
        assert_eq!(last_steps, strings(&demo::PLAN_STEPS));
        // Plan mode never executes.
        assert!(plans.iter().all(|plan| plan.tool_input["steps"]
            .as_array()
            .expect("steps")
            .iter()
            .all(|step| step["status"] == "pending")));
        assert_eq!(texts(&events).last().map(String::as_str), Some(demo::PLAN_RECAP));
        assert_eq!(usage_tokens(&events), vec![9_400]);
        let ev::UIEvent::Notification(last) = events.last().expect("events") else {
            panic!("end notice last");
        };
        assert_eq!(last.message, demo::PLAN_END_NOTICE);
    }

    // -- brainstorm turn -----------------------------------------------------

    /// Pins Python `test_brainstorm_turn_sequence`.
    #[test]
    fn test_brainstorm_turn_sequence() {
        let (script, events) = play(|script| script.run_brainstorm_turn());
        assert_eq!(
            kinds(&events),
            cat(&[
                &["notification", "prompt_submit", "execution_start"],
                &TEXT, &TEXT, &TEXT, &TEXT, &TEXT, &TEXT,
                &["provider_response_usage", "orchestrator_complete", "execution_end", "prompt_complete"],
            ])
        );
        assert_eq!(script.clock(), 3.0);
        // No tools in brainstorm — spec §4 trust string is literal.
        assert!(!kinds(&events)
            .iter()
            .any(|kind| *kind == "tool_pre" || *kind == "tool_post"));
        assert_eq!(texts(&events)[1..5], strings(&demo::BRAINSTORM_IDEAS));
        let roles: Vec<String> = events
            .iter()
            .filter_map(|event| match event {
                ev::UIEvent::ContentBlockEnd(block) => {
                    Some(block.block["demo_role"].as_str().expect("role").to_string())
                }
                _ => None,
            })
            .collect();
        assert_eq!(roles, strings(&["narration", "idea", "idea", "idea", "idea", "recap"]));
        assert_eq!(usage_tokens(&events), vec![4_100]);
    }

    // -- multi-agent turn ----------------------------------------------------

    /// One child-session Channel-A burst (lane live tail, spec §8): a full
    /// stream envelope but NO durable `content_block_end` — child prose
    /// never lands in the parent transcript (design doc D4).
    fn child_stream(deltas: usize) -> Vec<&'static str> {
        let mut sequence = vec!["stream_block_start"];
        sequence.extend(vec!["stream_block_delta"; deltas]);
        sequence.push("stream_block_end");
        sequence
    }

    /// Pins Python `test_agents_turn_sequence`.
    #[test]
    fn test_agents_turn_sequence() {
        let (script, events) = play(|script| script.run_agents_turn());
        assert_eq!(
            kinds(&events),
            cat(&[
                &["notification", "prompt_submit", "execution_start"],
                &TEXT,
                &TODO,
                &["agent_spawned"; 3],
                &child_stream(2), // researcher: 2 narration rows
                &child_stream(2), // coder: 2 narration rows
                &child_stream(1), // tester: 1 answer row
                &U, &U, &["agent_completed"], &TODO, // tester at 2.6s
                &U, &U, &["agent_completed"], &TODO, // researcher at 4.4s
                &U, &U, &["agent_completed"], &TODO, // coder at 6.0s
                &TEXT,
                &["orchestrator_complete", "execution_end", "prompt_complete", "notification"],
            ])
        );
        // Child bursts travel on the lanes' own sessions, parented to the root.
        let child_events: Vec<&ev::UIEvent> = events
            .iter()
            .filter(|event| event.session_id() != demo::DEMO_SESSION_ID)
            .collect();
        let child_sessions: HashSet<&str> =
            child_events.iter().map(|event| event.session_id()).collect();
        let lane_sessions: HashSet<&str> = demo_lanes()
            .iter()
            .map(|lane| lane.sub_session_id.as_str())
            .collect();
        assert_eq!(child_sessions, lane_sessions);
        assert!(child_events
            .iter()
            .all(|event| event.parent_id() == Some(demo::DEMO_SESSION_ID)));
        let child_kinds: HashSet<&str> =
            child_events.iter().map(|event| event.kind()).collect();
        assert_eq!(
            child_kinds,
            HashSet::from(["stream_block_start", "stream_block_delta", "stream_block_end"])
        );
        assert_eq!(script.clock(), 6.0);
        let spawned: Vec<&ev::AgentSpawned> = events
            .iter()
            .filter_map(|event| match event {
                ev::UIEvent::AgentSpawned(agent) => Some(agent),
                _ => None,
            })
            .collect();
        let spawn_names: Vec<&str> = spawned.iter().map(|agent| agent.agent.as_str()).collect();
        assert_eq!(spawn_names, ["researcher", "coder", "tester"]);
        assert!(spawned
            .iter()
            .all(|agent| agent.parent_session_id == demo::DEMO_SESSION_ID));
        let spawn_sessions: HashSet<&str> = spawned
            .iter()
            .map(|agent| agent.sub_session_id.as_str())
            .collect();
        assert_eq!(spawn_sessions, lane_sessions);
        let completed: Vec<(&str, f64)> = events
            .iter()
            .filter_map(|event| match event {
                ev::UIEvent::AgentCompleted(agent) => Some((agent.agent.as_str(), agent.ts)),
                _ => None,
            })
            .collect();
        assert_eq!(
            completed,
            [("tester", 2.6), ("researcher", 4.4), ("coder", 6.0)]
        );
        assert!(events.iter().all(|event| match event {
            ev::UIEvent::AgentCompleted(agent) => agent.success,
            _ => true,
        }));
        assert_eq!(usage_tokens(&events), vec![900; 6]);
        let ev::UIEvent::Notification(last) = events.last().expect("events") else {
            panic!("end notice last");
        };
        assert_eq!(last.message, demo::AGENTS_END_NOTICE);
        // The scripted plan progresses to all-completed: the ambient plan
        // panel (Phase 1) and the delegate summary's `Plan 4/4` fold
        // (Phase 2) both feed off these todo beats.
        let todo_pres: Vec<&ev::ToolPre> = tool_pres(&events)
            .into_iter()
            .filter(|pre| pre.tool_name == "todo")
            .collect();
        assert_eq!(todo_pres.len(), 4);
        let contents: Vec<String> = todo_pres[0].tool_input["todos"]
            .as_array()
            .expect("todos")
            .iter()
            .map(|todo| todo["content"].as_str().expect("content").to_string())
            .collect();
        assert_eq!(contents, strings(&demo::AGENTS_PLAN_STEPS));
        let final_todos = todo_pres
            .last()
            .expect("todos")
            .tool_input["todos"]
            .as_array()
            .expect("todos")
            .clone();
        assert!(final_todos.iter().all(|todo| todo["status"] == "completed"));
    }

    // -- whole-session run ---------------------------------------------------

    /// Pins Python `test_run_all_lifecycle_and_determinism`.
    #[test]
    fn test_run_all_lifecycle_and_determinism() {
        let sleeps: Arc<Mutex<Vec<f64>>> = Arc::new(Mutex::new(Vec::new()));
        let events: Events = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&events);
        let mut script = DemoScript::new(Box::new(move |event| {
            sink.lock().expect("event sink").push(event);
        }));
        let counter = Arc::clone(&sleeps);
        script.set_sleep(Box::new(move |seconds| {
            counter.lock().expect("sleep counter").push(seconds);
        }));
        script.run_all();
        let events = events.lock().expect("event sink").clone();
        assert_eq!(events[0].kind(), "session_start");
        assert_eq!(events.last().expect("events").kind(), "session_end");
        let prompts: Vec<String> = events
            .iter()
            .filter_map(|event| match event {
                ev::UIEvent::PromptSubmit(submit) => Some(submit.prompt.clone()),
                _ => None,
            })
            .collect();
        let expected_prompts: Vec<String> = [
            TurnKey::Seed,
            TurnKey::Build,
            TurnKey::Auto,
            TurnKey::Plan,
            TurnKey::Brainstorm,
            TurnKey::Agents,
        ]
        .into_iter()
        .map(|key| demo_turn_by_key(key).prompt.clone())
        .collect();
        assert_eq!(prompts, expected_prompts);
        // Deterministic envelope: unique monotonic ids, monotonic virtual ts.
        let ids: Vec<&str> = events.iter().map(|event| event.event_id()).collect();
        let unique: HashSet<&&str> = ids.iter().collect();
        assert_eq!(unique.len(), ids.len());
        let ts: Vec<f64> = events.iter().map(|event| event.ts()).collect();
        assert!(ts.windows(2).all(|pair| pair[0] <= pair[1]));
        // Total virtual time = 9.3 + 9.7 + 3.6 + 3.0 + 6.0 (seed is instant).
        assert_eq!(script.clock(), 31.6);
        let total: f64 = sleeps.lock().expect("sleep counter").iter().sum();
        assert_eq!((total * 1e6).round() / 1e6, 31.6); // paced entirely through the injected sleep
        // Only the agents turn's child stream bursts leave the root session —
        // each parented to it (lane live tail, spec §8).
        let lane_sessions: HashSet<&str> = demo_lanes()
            .iter()
            .map(|lane| lane.sub_session_id.as_str())
            .collect();
        for event in &events {
            if event.session_id() == demo::DEMO_SESSION_ID {
                continue;
            }
            assert!(lane_sessions.contains(event.session_id()));
            assert_eq!(event.parent_id(), Some(demo::DEMO_SESSION_ID));
            assert!(matches!(
                event.kind(),
                "stream_block_start" | "stream_block_delta" | "stream_block_end"
            ));
        }
    }

    /// Pins Python `test_two_runs_emit_identical_streams` (UIEvent's
    /// `PartialEq` covers every field the pydantic `model_dump` compares).
    #[test]
    fn test_two_runs_emit_identical_streams() {
        let (_first_script, first) = play(|script| script.run_build_turn());
        let (_second_script, second) = play(|script| script.run_build_turn());
        assert_eq!(first, second);
    }

    // -- interrupted close-out (oracle-verified against kernel/demo.py) ------

    /// Oracle check (not a pinned pytest case): esc during the build turn's
    /// first command — event tail, live telemetry and `interrupted_spec`
    /// close-out verified against the real Python `DemoRuntime`.
    #[test]
    fn oracle_interrupted_build_turn_close_out() {
        let events: Events = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&events);
        let mut script = DemoScript::new(Box::new(move |event| {
            sink.lock().expect("event sink").push(event);
        }));
        let elapsed = Arc::new(Mutex::new(0.0_f64));
        let tracker = Arc::clone(&elapsed);
        script.set_sleep(Box::new(move |seconds| {
            *tracker.lock().expect("elapsed") += seconds;
        }));
        // The esc lands once 2s of (virtual) runtime elapsed; the script
        // honors it at the next step boundary (mid-command → the live
        // `└ $ cmd` line stays, no collapsed tool line).
        let poll = Arc::clone(&elapsed);
        script.set_interrupt_poll(Box::new(move || *poll.lock().expect("elapsed") >= 2.0));
        script.run_build_turn();
        let events = events.lock().expect("event sink").clone();
        assert_eq!(
            kinds(&events),
            cat(&[
                &["prompt_submit", "execution_start"],
                &PLAN, &TODO,
                &PLAN, &TODO, &TEXT, &U, &["tool_pre"], &U,
                &TEXT, // interrupted recap
                &["orchestrator_complete", "execution_end", "prompt_complete"],
            ])
        );
        assert_eq!(script.clock(), 2.7);
        assert_eq!(usage_tokens(&events), vec![608, 439]);
        assert_eq!(texts(&events).last().map(String::as_str), Some(demo::INTERRUPTED_RECAP));
        let status = events
            .iter()
            .find_map(|event| match event {
                ev::UIEvent::OrchestratorComplete(complete) => Some(complete.status),
                _ => None,
            })
            .expect("orchestrator_complete");
        assert_eq!(status, ev::OrchestratorStatus::Cancelled);
        let ev::UIEvent::PromptComplete(complete) = events.last().expect("events") else {
            panic!("prompt_complete last");
        };
        assert_eq!(complete.response, "");
        // The close-out spec carries the ACTUAL elapsed secs/toks (2s, 1047).
        let close = script.interrupted_close.clone().expect("interrupted close");
        assert_eq!(close, interrupted_spec(TurnKey::Build, 2, 1_047));
        assert_eq!(close.rule_label, "2s · 1.0k tok, 88% cached · $0.06 · interrupted");
        assert_eq!(close.checkpoint_label, "store refactor · interrupted");
    }

    /// Oracle check (not a pinned pytest case): `run_turn` echoes the typed
    /// prompt verbatim and `queued=true` drops the scripted mode notice
    /// (mockup `drainQueue` runs without `setMode`) — verified against the
    /// real Python `DemoRuntime.run_turn`.
    #[test]
    fn oracle_run_turn_prompt_override_and_queued_mode_notice() {
        let (_script, queued) = play(|script| {
            script.run_turn(TurnKey::Auto, Some("do it my way".to_string()), true);
        });
        assert_eq!(kinds(&queued)[..3], ["prompt_submit", "execution_start", "tool_pre"]);
        let ev::UIEvent::PromptSubmit(submit) = &queued[0] else {
            panic!("prompt_submit first");
        };
        assert_eq!(submit.prompt, "do it my way");
        let (_script, plain) = play(|script| {
            script.run_turn(TurnKey::Auto, Some("do it".to_string()), false);
        });
        assert_eq!(kinds(&plain)[..2], ["notification", "prompt_submit"]);
        let ev::UIEvent::Notification(notice) = &plain[0] else {
            panic!("mode notice first");
        };
        assert_eq!(notice.message, demo::AUTO_MODE_NOTICE);
    }

    /// Oracle check (not a pinned pytest case): the step-boundary steer hook
    /// plays the DESIGN-SPEC §3 `Applying steer: <text>` narration once —
    /// verified against the real Python `_apply_steer`.
    #[test]
    fn oracle_steer_source_narrates_at_step_boundary() {
        let (mut script, events) = instant_script();
        let mut queue = vec!["focus on the sqlite backend".to_string()];
        script.set_steer_source(Box::new(move || queue.pop()));
        script.run_build_turn();
        let events = events.lock().expect("event sink").clone();
        let all_texts = texts(&events);
        assert_eq!(all_texts[0], "Applying steer: focus on the sqlite backend");
        // One queued steer → exactly one steer narration.
        assert_eq!(
            all_texts
                .iter()
                .filter(|text| text.starts_with("Applying steer:"))
                .count(),
            1
        );
    }

    // -- ScriptedDemoRuntime (threaded wiring over the engine) ---------------

    /// The Runtime-seam composition end-to-end: submit parks on the
    /// ticket-bearing pytest approval (`demo-ticket-1`), the answered choice
    /// resumes the script, and the turn closes with the scripted build
    /// answer (offline analogue of Python's DemoRuntimeAdapter flow).
    #[test]
    fn test_scripted_demo_runtime_parks_and_answers_approval() {
        let (tx, rx) = channel::<Msg>();
        let mut runtime = ScriptedDemoRuntime::new(tx);
        runtime.set_instant();
        runtime.submit(demo::BUILD_PROMPT.to_string());
        let mut seen_kinds: Vec<&'static str> = Vec::new();
        let mut answered = false;
        let mut response = String::new();
        while let Ok(msg) = rx.recv_timeout(Duration::from_secs(10)) {
            match msg {
                Msg::Rt(WireEvent::Approval { ticket_id, prompt, options }) => {
                    assert_eq!(ticket_id, "demo-ticket-1");
                    assert_eq!(prompt, demo::PYTEST_APPROVAL_PROMPT);
                    assert_eq!(options, strings(&demo::APPROVAL_OPTIONS));
                    answered = true;
                    runtime.answer_approval(&ticket_id, "Allow once");
                }
                Msg::Rt(WireEvent::Event(event)) => {
                    seen_kinds.push(event.kind());
                    if let ev::UIEvent::PromptComplete(complete) = &event {
                        response = complete.response.clone();
                        break;
                    }
                }
                _ => {}
            }
        }
        assert!(answered, "demo turn parked on the approval");
        assert_eq!(seen_kinds[0], "prompt_submit");
        assert!(seen_kinds.contains(&"approval_granted"));
        assert_eq!(response, build_answer(false));
        assert!(runtime.interrupted_close().is_none());
    }

    /// Unknown prompts advance the mockup turn order (the runtime mirrors
    /// the adapter's DemoWiring key bookkeeping): with the seed marked
    /// played, the first free-text submit plays the build turn.
    #[test]
    fn test_scripted_demo_runtime_key_selection_for_free_text() {
        let (tx, rx) = channel::<Msg>();
        let mut runtime = ScriptedDemoRuntime::new(tx);
        runtime.set_instant();
        runtime.submit("  make history durable  ".to_string());
        let mut first_prompt = None;
        let mut saw_build_plan = false;
        while let Ok(msg) = rx.recv_timeout(Duration::from_secs(10)) {
            match msg {
                Msg::Rt(WireEvent::Event(event)) => match &event {
                    ev::UIEvent::PromptSubmit(submit) => {
                        first_prompt = Some(submit.prompt.clone());
                    }
                    ev::UIEvent::ToolPre(pre) if pre.tool_name == "update_plan" => {
                        saw_build_plan = pre.tool_input["title"] == demo::STORE_PLAN_TITLE;
                    }
                    ev::UIEvent::PromptComplete(_) => break,
                    _ => {}
                },
                Msg::Rt(WireEvent::Approval { ticket_id, .. }) => {
                    runtime.answer_approval(&ticket_id, "Allow once");
                }
                _ => {}
            }
        }
        // The user line echoes the typed text verbatim (trimmed)…
        assert_eq!(first_prompt.as_deref(), Some("make history durable"));
        // …while the scripted BUILD turn plays underneath.
        assert!(saw_build_plan, "free text played the build turn's plan");
    }
}
