//! `CoreClientRuntime` — the canonical runtime shape. The front-end owns nothing
//! but rendering; a spawned backend process owns the turn loop (model client,
//! tools, approvals). Events stream in over the child's stdout; submissions go
//! out over its stdin. This is the Codex `codex-tui` ⇄ `codex-core` relationship,
//! and the drop-in point for amplifier-core: swap the backend command for this
//! repo's Python `serve` shim and nothing in the UI changes.

use crate::message::Msg;
use crate::protocol;
use crate::runtime::Runtime;
use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::thread;

pub struct CoreClientRuntime {
    stdin: Arc<Mutex<ChildStdin>>,
    child: Child,
}

impl CoreClientRuntime {
    /// Spawn the backend `cmd` (e.g. `["python3", ".../serve_mock.py"]`) and wire
    /// a reader thread that normalizes its event stream onto the app-loop queue.
    pub fn spawn(cmd: &[String], tx: Sender<Msg>) -> std::io::Result<Self> {
        let mut child = Command::new(&cmd[0])
            .args(&cmd[1..])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;

        let stdout = child.stdout.take().expect("child stdout");
        let stdin = child.stdin.take().expect("child stdin");

        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                let Ok(line) = line else { break };
                if line.trim().is_empty() {
                    continue;
                }
                if let Some(ev) = protocol::decode_event(&line) {
                    if tx.send(Msg::Rt(ev)).is_err() {
                        break;
                    }
                }
            }
        });

        Ok(Self { stdin: Arc::new(Mutex::new(stdin)), child })
    }

    fn send(&self, op: Value) {
        if let Ok(mut w) = self.stdin.lock() {
            let _ = writeln!(w, "{}", op);
            let _ = w.flush();
        }
    }
}

impl Drop for CoreClientRuntime {
    fn drop(&mut self) {
        // Dropping stdin sends EOF so the backend shuts down cleanly; then reap.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Runtime for CoreClientRuntime {
    fn submit(&mut self, prompt: String) {
        self.send(protocol::submit(&prompt));
    }
    fn answer_approval(&mut self, granted: bool) {
        self.send(protocol::approve(granted));
    }
    fn interrupt(&mut self) {
        self.send(protocol::interrupt());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::{App, TurnState};
    use crate::event::UiEvent;
    use std::sync::mpsc::channel;
    use std::time::Duration;

    /// End-to-end over the real protocol: spawn the Python backend, drive a full
    /// interactive turn — INCLUDING answering the approval from the Rust side —
    /// across the process boundary, and assert the transcript. Zero amplifier-core.
    #[test]
    fn interactive_turn_over_process_boundary() {
        let backend = format!("{}/backend/serve_mock.py", env!("CARGO_MANIFEST_DIR"));
        let (tx, rx) = channel::<Msg>();
        let mut rt = CoreClientRuntime::spawn(&["python3".to_string(), backend], tx)
            .expect("spawn backend");

        let mut app = App::new("newtui", "core-01");
        rt.submit("Add a health check endpoint".to_string());

        let mut answered = false;
        let mut completed = false;
        while let Ok(msg) = rx.recv_timeout(Duration::from_secs(5)) {
            if let Msg::Rt(ev) = msg {
                let is_complete = matches!(ev, UiEvent::TurnComplete { .. });
                app.on_event(ev);
                // The UI answers the parked approval over the same protocol.
                if app.state == TurnState::AwaitingApproval && !answered {
                    answered = true;
                    app.state = TurnState::Running;
                    rt.answer_approval(true);
                }
                if is_complete {
                    completed = true;
                    break;
                }
            }
        }

        assert!(answered, "backend requested approval and UI answered it");
        assert!(completed, "turn completed over the protocol");
        assert_eq!(app.state, TurnState::Idle);

        let transcript: String = format!("{:?}", app.blocks);
        assert!(transcript.contains("Add a health check endpoint"), "user line");
        assert!(transcript.contains("Read 3 files"), "tool line");
        assert!(transcript.contains("/health"), "streamed answer");
        assert!((app.tallies.cost - 0.0123).abs() < 1e-9, "priced from backend usage");
    }
}
