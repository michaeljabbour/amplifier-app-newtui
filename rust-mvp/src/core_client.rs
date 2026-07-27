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
    /// Spawn the backend `cmd` (e.g. `["uv", "run", "amplifier-newtui", "serve"]`) and wire
    /// a reader thread that normalizes its event stream onto the app-loop queue.
    pub fn spawn(cmd: &[String], tx: Sender<Msg>) -> std::io::Result<Self> {
        let mut child = Command::new(&cmd[0])
            .args(&cmd[1..])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;

        let stdout = child.stdout.take().expect("child stdout");
        let stderr = child.stderr.take().expect("child stderr");
        let stdin = child.stdin.take().expect("child stdin");

        // serve keeps its protocol stream clean by redirecting boot/module
        // chatter to stderr; forward it so the splash can show what's loading.
        let chatter_tx = tx.clone();
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
                let Ok(line) = line else { break };
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                if chatter_tx.send(Msg::BootChatter(line.to_string())).is_err() {
                    break;
                }
            }
        });

        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                let Ok(line) = line else { break };
                if line.trim().is_empty() {
                    continue;
                }
                if let Some(ev) = protocol::decode_wire(&line) {
                    if tx.send(Msg::Rt(ev)).is_err() {
                        return; // app loop gone — no exit report needed
                    }
                }
            }
            // stdout EOF: the backend exited. Report it so the app can run
            // the boot-failure diagnosis (identity never arrived) or an
            // honest mid-session notice instead of hanging silently.
            let _ = tx.send(Msg::BackendExited);
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
        // This turn's exact-dollar assert prices against the process-wide
        // active pricing table — hold the same guard the kernel::cost swap
        // tests hold, or a concurrent expensive-table swap races this turn
        // onto $1/1k rates (observed: session cost $2.740, flaky).
        let _pricing = crate::kernel::cost::active_table_test_guard();
        let backend = format!("{}/backend/serve_mock.py", env!("CARGO_MANIFEST_DIR"));
        let (tx, rx) = channel::<Msg>();
        let rt = CoreClientRuntime::spawn(&["python3".to_string(), backend], tx)
            .expect("spawn backend");

        let adapter = ClientRuntimeAdapter::new(Box::new(rt));
        let mut app = App::new(Box::new(adapter), true, None, None);
        app.boot();

        // The mock emits boot.progress phases first (they paint the splash
        // status while modules load), then session.started (identity +
        // splash gone) — pinning the boot flow end-to-end offline.
        let mut boot_statuses: Vec<String> = Vec::new();
        loop {
            let msg = rx
                .recv_timeout(Duration::from_secs(5))
                .expect("boot/session record");
            let Msg::Rt(ev) = msg else { continue };
            let is_session = matches!(ev, protocol::WireEvent::SessionStarted { .. });
            app.handle_wire(ev);
            if is_session {
                break;
            }
            if let Some(splash) = app.ui.borrow().splash.as_ref() {
                boot_statuses.push(splash.status().to_string());
            }
        }
        assert!(
            boot_statuses.contains(&"loading · newtui".to_string())
                && boot_statuses.contains(&"installing package · tool-bash".to_string()),
            "boot.progress phases reached the splash (Python boot_progress text): {boot_statuses:?}"
        );
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

    /// LIVE end-to-end against the REAL Python backend (`uv run
    /// amplifier-newtui serve`, which wraps RealRuntime → real model calls).
    /// Ignored by default so CI/normal runs never touch the network; run it
    /// explicitly with:
    ///
    ///   cargo test live_serve_end_to_end -- --ignored --nocapture
    ///
    /// Override the backend command with AMPLIFIER_SERVE_CMD (executed via
    /// `sh -c` from the repo root, one directory above this crate).
    #[test]
    #[ignore = "live: spawns the real Python backend and makes a real model call"]
    fn live_serve_end_to_end() {
        let repo_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("rust-mvp lives inside the repo")
            .to_path_buf();
        let serve_cmd = std::env::var("AMPLIFIER_SERVE_CMD")
            .unwrap_or_else(|_| "uv run amplifier-newtui serve".to_string());
        let shell = format!("cd '{}' && exec {}", repo_root.display(), serve_cmd);
        let cmd = vec!["/bin/sh".to_string(), "-c".to_string(), shell];

        let (tx, rx) = channel::<Msg>();
        let rt = CoreClientRuntime::spawn(&cmd, tx).expect("spawn live backend");

        let adapter = ClientRuntimeAdapter::new(Box::new(rt));
        let mut app = App::new(Box::new(adapter), true, None, None);
        app.boot();

        // RealRuntime boot (foundation + modules) can take a while cold.
        match rx
            .recv_timeout(Duration::from_secs(300))
            .expect("session.started from live backend")
        {
            Msg::Rt(ev) => app.handle_wire(ev),
            _ => panic!("expected wire event"),
        }
        assert!(app.ui.borrow().splash.is_none(), "splash dissolves on identity");
        assert!(!app.ui.borrow().bundle.is_empty(), "real bundle name landed");
        assert!(!app.ui.borrow().model_name.is_empty(), "real model name landed");

        app.submit_prompt("Reply with exactly the word: pong");

        // Drive the assembled pipeline until the live turn completes.
        // turn_active flips true on the backend's prompt_submit event and
        // false again when the turn finishes. If the backend parks on an
        // approval (not expected for this prompt, but the posture decides),
        // answer it from the real approval-bar path.
        let mut answered_approval = false;
        let mut saw_active = false;
        loop {
            let msg = rx
                .recv_timeout(Duration::from_secs(180))
                .expect("live event stream stalled before turn completion");
            if let Msg::Rt(ev) = msg {
                app.handle_wire(ev);
                if app.ui.borrow().approval.is_some() {
                    answered_approval = true;
                    app.on_key("enter"); // "Allow once" is the default selection
                }
                if app.ui.borrow().turn_active {
                    saw_active = true;
                }
                if saw_active && !app.ui.borrow().turn_active {
                    break;
                }
            }
        }

        // Real usage/cost tallies from provider_response_usage events.
        assert!(app.reducer.total_tokens > 0, "output tokens tallied from live usage");
        assert!(
            app.reducer.session_cost > Decimal::ZERO,
            "session cost priced from live usage (got {})",
            app.reducer.session_cost
        );

        // The real answer landed as a durable transcript block.
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        terminal.draw(|f| ui::draw(f, &app)).unwrap();
        let text = buffer_text(terminal.backend().buffer());
        assert!(
            text.contains("Reply with exactly the word: pong"),
            "user line rendered:\n{text}"
        );
        assert!(
            text.to_lowercase().contains("pong"),
            "durable answer from the real model rendered:\n{text}"
        );

        eprintln!(
            "LIVE OK — cost=${} tokens={} approval_round_trip={}",
            app.reducer.session_cost, app.reducer.total_tokens, answered_approval
        );
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
