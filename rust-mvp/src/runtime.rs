//! Scripted runtime — the Rust analogue of `kernel/demo.py`. It emits the same
//! typed `UiEvent`s a real amplifier-core boot would, on a background thread, so
//! streaming/approvals are genuinely time-sliced. No network, no Python.

use crate::event::UiEvent;
use crate::message::Msg;
use std::sync::mpsc::{channel, Sender};
use std::thread;
use std::time::Duration;

/// The runtime seam — the one interface the UI talks to. `DemoRuntime` (scripted)
/// and `LiveRuntime` (real provider) both implement it, so the app is identical
/// whether events come from a script, an HTTP stream, or (later) amplifier-core.
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

pub struct DemoRuntime {
    tx: Sender<Msg>,
    /// Set each turn so the app can unblock a parked approval.
    approval_tx: Option<Sender<bool>>,
}

impl DemoRuntime {
    pub fn new(tx: Sender<Msg>) -> Self {
        Self { tx, approval_tx: None }
    }

    /// Kick off a scripted turn for `prompt`.
    fn run_turn(&mut self, prompt: String) {
        let tx = self.tx.clone();
        let (atx, arx) = channel::<bool>();
        self.approval_tx = Some(atx);

        thread::spawn(move || {
            let send = |e: UiEvent| {
                let _ = tx.send(Msg::Rt(e));
            };
            let beat = |ms: u64| thread::sleep(Duration::from_millis(ms));

            send(UiEvent::PromptSubmit(prompt.clone()));
            beat(180);
            send(UiEvent::Narration("Thinking…".into()));
            beat(320);
            send(UiEvent::ToolLine { summary: "Read 3 files · ran 2 commands".into(), ok: true });
            beat(260);

            // A signature interaction: the write asks for approval and the turn parks.
            send(UiEvent::ApprovalRequired {
                ticket_id: "demo-1".into(),
                action: "write_file src/health.py".into(),
            });
            let granted = arx.recv().unwrap_or(false);
            if !granted {
                send(UiEvent::Notice("Denied — continuing without the write".into()));
                send(UiEvent::ToolLine { summary: "write_file src/health.py (denied)".into(), ok: false });
                beat(160);
                send(UiEvent::StreamStart);
                for w in "Understood — I left the endpoint out. Tell me how you'd like to proceed.".split_inclusive(' ') {
                    send(UiEvent::StreamDelta(w.to_string()));
                    beat(28);
                }
                send(UiEvent::StreamEnd);
                send(UiEvent::TurnComplete { files: 0, added: 0, removed: 0, tokens: 640, cost: 0.0041 });
                return;
            }

            send(UiEvent::ToolLine { summary: "Changed 1 file  (+18/−0)".into(), ok: true });
            beat(200);
            send(UiEvent::StreamStart);
            let answer = "I've added a `/health` endpoint that returns 200 with a JSON \
                          status body, wired it into the router, and covered it with a \
                          test. The service now reports readiness for load balancers.";
            for word in answer.split_inclusive(' ') {
                send(UiEvent::StreamDelta(word.to_string()));
                beat(26);
            }
            send(UiEvent::StreamEnd);
            send(UiEvent::TurnComplete { files: 1, added: 18, removed: 0, tokens: 1240, cost: 0.0123 });
        });
    }
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
}
