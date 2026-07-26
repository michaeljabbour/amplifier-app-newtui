//! Composition root + event loop. One asyncio-free loop: terminal input, runtime
//! events, and spinner ticks all arrive as `Msg` on a single channel — the Rust
//! analogue of the app-loop queue in `ui/app.py`.

mod app;
mod event;
mod message;
mod model;
mod runtime;
mod ui;

use app::{App, TurnState};
use crossterm::event as cterm;
use crossterm::event::{Event as CEvent, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::execute;
use message::Msg;
use runtime::DemoRuntime;
use std::io::{self, Stdout};
use std::sync::mpsc::{channel, Sender};
use std::thread;
use std::time::Duration;
use ratatui::prelude::*;

fn main() -> io::Result<()> {
    let (tx, rx) = channel::<Msg>();
    spawn_input_reader(tx.clone());
    spawn_ticker(tx.clone());

    let mut terminal = setup_terminal()?;
    let mut app = App::new("newtui", "demo-01");
    let mut runtime = DemoRuntime::new(tx.clone());

    terminal.draw(|f| ui::draw(f, &app))?;

    while let Ok(msg) = rx.recv() {
        match msg {
            Msg::Term(CEvent::Key(k)) if k.kind == KeyEventKind::Press => {
                handle_key(&mut app, &mut runtime, k);
            }
            Msg::Term(_) => {}
            Msg::Rt(ev) => app.on_event(ev),
            Msg::Tick => app.tick(),
        }
        if app.should_quit {
            break;
        }
        terminal.draw(|f| ui::draw(f, &app))?;
    }

    restore_terminal(&mut terminal)
}

fn handle_key(app: &mut App, runtime: &mut DemoRuntime, key: KeyEvent) {
    // Global quit.
    if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
        app.should_quit = true;
        return;
    }

    match app.state {
        TurnState::AwaitingApproval => match key.code {
            KeyCode::Char('y') => {
                app.state = TurnState::Running;
                app.pending_action = None;
                runtime.answer_approval(true);
            }
            KeyCode::Char('n') | KeyCode::Esc => {
                app.state = TurnState::Running;
                app.pending_action = None;
                runtime.answer_approval(false);
            }
            _ => {}
        },
        _ => match key.code {
            KeyCode::BackTab => app.mode = app.mode.next(),
            KeyCode::Enter => {
                let text = app.composer.trim().to_string();
                let prompt = if text.is_empty() {
                    "Add a health check endpoint to the service".to_string()
                } else {
                    text
                };
                app.composer.clear();
                if app.state == TurnState::Idle {
                    runtime.submit(prompt);
                }
            }
            KeyCode::Char(c) => app.composer.push(c),
            KeyCode::Backspace => {
                app.composer.pop();
            }
            KeyCode::Up => app.scroll = app.scroll.saturating_sub(1),
            KeyCode::Down => app.scroll = app.scroll.saturating_add(1),
            KeyCode::Esc if app.state == TurnState::Running => { /* interrupt hook point */ }
            _ => {}
        },
    }
}

fn spawn_input_reader(tx: Sender<Msg>) {
    thread::spawn(move || loop {
        if cterm::poll(Duration::from_millis(200)).unwrap_or(false) {
            if let Ok(ev) = cterm::read() {
                if tx.send(Msg::Term(ev)).is_err() {
                    break;
                }
            }
        }
    });
}

fn spawn_ticker(tx: Sender<Msg>) {
    thread::spawn(move || loop {
        thread::sleep(Duration::from_millis(120));
        if tx.send(Msg::Tick).is_err() {
            break;
        }
    });
}

fn setup_terminal() -> io::Result<Terminal<CrosstermBackend<Stdout>>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    Terminal::new(CrosstermBackend::new(stdout))
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> io::Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::UiEvent;
    use ratatui::backend::TestBackend;

    /// Headless render smoke test — the analogue of the Python app's Pilot tests.
    /// Drives a full scripted turn's events through the reducer and asserts the
    /// pure `draw` produces the expected transcript, with zero real terminal.
    #[test]
    fn renders_a_full_turn_headless() {
        let mut app = App::new("newtui", "demo-01");
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).unwrap();

        // Replay a turn including the approval → answer → stream → complete arc.
        for ev in [
            UiEvent::PromptSubmit("Add a health check endpoint".into()),
            UiEvent::Narration("Thinking…".into()),
            UiEvent::ToolLine { summary: "Read 3 files · ran 2 commands".into(), ok: true },
            UiEvent::ApprovalRequired { action: "write_file src/health.py".into() },
        ] {
            app.on_event(ev);
        }
        assert_eq!(app.state, TurnState::AwaitingApproval);

        // Approve, then stream the answer.
        app.state = TurnState::Running;
        app.on_event(UiEvent::StreamStart);
        for w in "I've added a /health endpoint.".split_inclusive(' ') {
            app.on_event(UiEvent::StreamDelta(w.into()));
        }
        app.on_event(UiEvent::StreamEnd);
        app.on_event(UiEvent::TurnComplete { files: 1, added: 18, removed: 0, tokens: 1240, cost: 0.0123 });

        assert_eq!(app.state, TurnState::Idle);
        assert!((app.tallies.cost - 0.0123).abs() < 1e-9);

        terminal.draw(|f| ui::draw(f, &app)).unwrap();
        let text = buffer_text(terminal.backend().buffer());

        assert!(text.contains("Add a health check endpoint"), "user line missing");
        assert!(text.contains("Read 3 files"), "tool line missing");
        assert!(text.contains("/health endpoint"), "streamed answer missing");
        assert!(text.contains("files 1 · +18/−0"), "turn rule missing");
        assert!(text.contains("chat ·"), "footer mode/cost missing");
    }

    #[test]
    fn shift_tab_cycles_modes() {
        let mut app = App::new("newtui", "demo-01");
        assert_eq!(app.mode.label(), "chat");
        app.mode = app.mode.next();
        assert_eq!(app.mode.label(), "plan");
    }

    /// Prints a real rendered frame (run: `cargo test -- --nocapture snapshot`).
    #[test]
    fn snapshot() {
        let mut app = App::new("newtui", "demo-01");
        for ev in [
            UiEvent::PromptSubmit("Add a health check endpoint to the service".into()),
            UiEvent::Narration("Thinking…".into()),
            UiEvent::ToolLine { summary: "Read 3 files · ran 2 commands".into(), ok: true },
            UiEvent::ToolLine { summary: "Changed 1 file  (+18/−0)".into(), ok: true },
            UiEvent::StreamStart,
        ] {
            app.on_event(ev);
        }
        for w in "I've added a `/health` endpoint that returns 200 with a JSON status body and covered it with a test.".split_inclusive(' ') {
            app.on_event(UiEvent::StreamDelta(w.into()));
        }
        app.state = TurnState::Running;
        let mut terminal = Terminal::new(TestBackend::new(78, 18)).unwrap();
        terminal.draw(|f| ui::draw(f, &app)).unwrap();
        println!("\n{}", buffer_text(terminal.backend().buffer()));
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
