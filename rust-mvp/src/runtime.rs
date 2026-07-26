//! Scripted runtime — offline stand-in for the serve backend. It emits the
//! SAME wire vocabulary the protocol backend does (`kernel/events.py` kinds
//! plus the ticket-bearing approval record), on a background thread, so
//! streaming/approvals are genuinely time-sliced. No network, no Python.
//!
//! NOTE: this is the serve-mock turn script in-process — the Python demo's
//! richer scripted turns (`kernel/demo.py`) are not ported; `--demo` plays
//! this single build-shaped turn through the real reducer paths.

use crate::message::Msg;
use crate::protocol::WireEvent;
use serde_json::{json, Map, Value};
use std::sync::mpsc::{channel, Sender};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::kernel::events as ev;

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
