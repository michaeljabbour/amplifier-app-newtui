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
                if let Some(ev) = protocol::decode_wire(&line) {
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
    fn answer_approval(&mut self, ticket_id: &str, choice: &str) {
        self.send(protocol::approve(ticket_id, choice));
    }
    fn interrupt(&mut self) {
        self.send(protocol::interrupt());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::App;
    use crate::ui;
    use crate::ui::runtime_adapter::ClientRuntimeAdapter;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    use rust_decimal::Decimal;
    use std::str::FromStr;
    use std::sync::mpsc::channel;
    use std::time::Duration;

    /// End-to-end over the real protocol: spawn the Python backend, drive a
    /// full interactive turn through the ASSEMBLED reducer pipeline —
    /// INCLUDING answering the approval from the Rust approval bar — across
    /// the process boundary, and assert both App state and the rendered
    /// frame. Zero amplifier-core. (Adapted from the pre-assembly test,
    /// which asserted the legacy demo model instead of the reducer.)
    #[test]
    fn interactive_turn_over_process_boundary() {
        let backend = format!("{}/backend/serve_mock.py", env!("CARGO_MANIFEST_DIR"));
        let (tx, rx) = channel::<Msg>();
        let rt = CoreClientRuntime::spawn(&["python3".to_string(), backend], tx)
            .expect("spawn backend");

        let adapter = ClientRuntimeAdapter::new(Box::new(rt));
        let mut app = App::new(Box::new(adapter), true, None, None);
        app.boot();

        // The mock emits session.started immediately: identity + splash gone.
        match rx.recv_timeout(Duration::from_secs(5)).expect("session record") {
            Msg::Rt(ev) => app.handle_wire(ev),
            _ => panic!("expected wire event"),
        }
        assert!(app.ui.borrow().splash.is_none(), "splash dissolves on identity");
        assert_eq!(app.ui.borrow().bundle, "newtui");

        app.submit_prompt("Add a health check endpoint");

        let mut answered = false;
        while let Ok(msg) = rx.recv_timeout(Duration::from_secs(5)) {
            if let Msg::Rt(ev) = msg {
                app.handle_wire(ev);
                // The mock parks on the approval: answer it from the REAL
                // approval-bar path (Enter confirms the selected option).
                if app.ui.borrow().approval.is_some() && !answered {
                    answered = true;
                    assert_eq!(
                        app.ui.borrow().approval.as_ref().unwrap().ticket_id,
                        "approval-1",
                        "ticket id carried over the wire"
                    );
                    app.on_key("enter"); // "Allow once" is the default selection
                }
                if !app.ui.borrow().turn_active && answered {
                    break;
                }
            }
        }

        assert!(answered, "backend requested approval and UI answered it");
        assert!(!app.ui.borrow().turn_active, "turn completed over the protocol");

        // The two provider_response_usage events (1200/340/800/100 and
        // 900/120/0/0, claude-sonnet-4-5) price exactly through kernel::cost:
        // $0.00924 + $0.0045 = $0.01374 (oracle-checked against Python cost_of).
        assert_eq!(
            app.reducer.session_cost,
            Decimal::from_str("0.01374").unwrap(),
            "session cost priced from usage events"
        );
        assert_eq!(app.reducer.total_tokens, 460, "output tokens from usage events");

        let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
        terminal.draw(|f| ui::draw(f, &app)).unwrap();
        let text = buffer_text(terminal.backend().buffer());
        assert!(text.contains("Add a health check endpoint"), "user line:\n{text}");
        assert!(
            text.contains("Read 3 files · ran 2 shell commands"),
            "tool digest from correlated tool_pre/tool_post:\n{text}"
        );
        assert!(text.contains("/health"), "durable answer:\n{text}");
        assert!(text.contains("+18/−0"), "turn rule carries the diffstat:\n{text}");
        assert!(text.contains("$0.01"), "footer cost from exact Decimal:\n{text}");
    }

    fn buffer_text(buf: &ratatui::buffer::Buffer) -> String {
        let mut s = String::new();
        for y in 0..buf.area.height {
            for x in 0..buf.area.width {
                s.push_str(buf[(x, y)].symbol());
            }
            s.push('\n');
        }
        s
    }
}
