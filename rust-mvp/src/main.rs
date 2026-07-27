//! Composition root + event loop. One asyncio-free loop: terminal input, runtime
//! events, and heartbeat ticks all arrive as `Msg` on a single channel — the Rust
//! analogue of the app-loop queue in `ui/app.py`. Key events convert to Textual
//! chord names and dispatch through the assembled `App` (keymap contexts,
//! composer semantics, approval bar, ESC_CHAIN).

use amplifier_newtui_rs::{app, core_client, live, message, runtime, ui};

use app::{App, DemoAdapter};
use core_client::CoreClientRuntime;
use crossterm::event as cterm;
use crossterm::event::{
    Event as CEvent, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use live::LiveRuntime;
use message::Msg;
use ratatui::prelude::*;
use runtime::DemoRuntime;
use std::cell::RefCell;
use std::io::{self, Stdout};
use std::rc::Rc;
use std::sync::mpsc::{channel, Sender};
use std::thread;
use std::time::Duration;
use ui::demo_wiring::DemoWiring;
use ui::runtime_adapter::ClientRuntimeAdapter;

fn main() -> io::Result<()> {
    let perf = PerfLog::from_env();
    let (tx, rx) = channel::<Msg>();
    spawn_input_reader(tx.clone());
    spawn_ticker(tx.clone());

    // Probe the hosting terminal once so the advertised queue chord matches
    // what it actually delivers (shift+enter vs alt+enter — ui/term_probe).
    let kitty_protocol = ui::term_probe::probe_kitty_protocol(None);

    // Runtime selection:
    //   --demo    → scripted in-process DemoRuntime (real event vocabulary)
    //   --direct  → LiveRuntime (illustrative UI-calls-provider shortcut, not
    //               the target architecture; falls back to demo without a key)
    //   default   → CoreClientRuntime: client of a backend process over the
    //               protocol (the canonical shape; drop-in for amplifier-core)
    let args: Vec<String> = std::env::args().collect();
    // TUI-relevant launch flags (main.py's interactive/serve options):
    // forwarded to the backend `serve` command; --mode also seeds the
    // opening interaction posture.
    let flags = parse_launch_flags(&args);
    let initial_mode = flags.mode.clone();
    let initial_mode = initial_mode.as_deref();
    let mut app = if args.iter().any(|a| a == "--demo" || a == "demo") {
        demo_app(&tx, kitty_protocol, initial_mode)
    } else if args.iter().any(|a| a == "--direct") {
        match LiveRuntime::from_env(tx.clone()) {
            Ok(rt) => {
                let model = rt.model().to_string();
                let mut adapter = ClientRuntimeAdapter::new(Box::new(rt));
                adapter.base_mut().bundle_name = "newtui".into();
                adapter.base_mut().model_name = model;
                adapter.base_mut().session_short = live::LIVE_SESSION_ID.into();
                App::new(Box::new(adapter), kitty_protocol, initial_mode, None)
            }
            Err(_) => {
                let app = demo_app(&tx, kitty_protocol, initial_mode);
                app.ui
                    .borrow_mut()
                    .show_notice("no ANTHROPIC_API_KEY — scripted demo", None);
                app
            }
        }
    } else {
        let env_cmd = std::env::var("AMPLIFIER_SERVE_CMD").ok();
        let (cmd, fallback_notice) =
            resolve_backend(env_cmd.as_deref(), repo_serve_root().as_deref(), &flags);
        match CoreClientRuntime::spawn(&cmd, tx.clone()) {
            Ok(rt) => {
                let app = App::new(
                    Box::new(ClientRuntimeAdapter::new(Box::new(rt))),
                    kitty_protocol,
                    initial_mode,
                    None,
                );
                if let Some(notice) = fallback_notice {
                    app.ui.borrow_mut().show_notice(&notice, None);
                }
                app
            }
            Err(e) => {
                let app = demo_app(&tx, kitty_protocol, initial_mode);
                app.ui
                    .borrow_mut()
                    .show_notice(&format!("backend spawn failed ({e}) — scripted demo"), None);
                app
            }
        }
    };
    app.boot();

    let mut terminal = setup_terminal()?;
    let size = terminal.size()?;
    app.on_resize(size.width, size.height);
    terminal.draw(|f| ui::draw(f, &app))?;
    flush_chrome(&mut terminal, &app)?;
    perf.mark("first_draw");

    while let Ok(msg) = rx.recv() {
        match msg {
            Msg::Term(CEvent::Key(k)) if k.kind == KeyEventKind::Press => {
                if let Some(name) = key_name(&k) {
                    app.on_key(&name);
                }
            }
            Msg::Term(CEvent::Paste(payload)) => app.on_paste(&payload),
            Msg::Term(CEvent::Resize(w, h)) => app.on_resize(w, h),
            Msg::Term(CEvent::Mouse(mouse)) => match mouse.kind {
                MouseEventKind::ScrollUp => app.on_mouse_scroll(true, mouse.column, mouse.row),
                MouseEventKind::ScrollDown => app.on_mouse_scroll(false, mouse.column, mouse.row),
                MouseEventKind::Down(MouseButton::Left) => {
                    app.on_mouse_down(mouse.column, mouse.row)
                }
                MouseEventKind::Drag(MouseButton::Left) => {
                    app.on_mouse_drag(mouse.column, mouse.row)
                }
                MouseEventKind::Up(MouseButton::Left) => app.on_mouse_up(mouse.column, mouse.row),
                _ => {}
            },
            Msg::Term(_) => {}
            Msg::Rt(ev) => {
                if matches!(ev, amplifier_newtui_rs::protocol::WireEvent::SessionStarted { .. }) {
                    perf.mark("session_started");
                }
                app.handle_wire(ev);
            }
            Msg::BootChatter(line) => app.on_boot_chatter(&line),
            Msg::BackendExited => app.on_backend_exited(),
            Msg::Tick => app.tick(),
        }
        if app.should_quit() {
            break;
        }
        terminal.draw(|f| ui::draw(f, &app))?;
        flush_chrome(&mut terminal, &app)?;
    }

    restore_terminal(&mut terminal)?;
    // Python `_print_resume_hint`: on TUI exit, echo how to get back into
    // this session (skipped when no stored session id was learned).
    if let Some(hint) = resume_hint(&app.resume_session_id()) {
        println!("{hint}");
    }
    Ok(())
}

/// The exit farewell of main.py `_print_resume_hint`, verbatim: real
/// sessions carry a stored id; demo sessions do not, so `None` skips it.
fn resume_hint(session_id: &str) -> Option<String> {
    if session_id.is_empty() {
        return None;
    }
    Some(format!(
        "resume this session: amplifier-newtui resume {session_id}\n\
         list sessions:       amplifier-newtui sessions"
    ))
}

/// Emit the native-terminal side effects the app parked this frame: the
/// OSC window/tab title (Python `write_terminal_title`, already deduped by
/// `TitleBar`) and the attention bell (Python's driver-safe `App.bell`).
fn flush_chrome(terminal: &mut Terminal<CrosstermBackend<Stdout>>, app: &App) -> io::Result<()> {
    use std::io::Write as _;
    let title = app.take_pending_title();
    let bell = app.take_bell();
    if title.is_none() && !bell {
        return Ok(());
    }
    if let Some(title) = title {
        write!(
            terminal.backend_mut(),
            "{}",
            ui::chrome::terminal_title_sequence(&title)
        )?;
    }
    if bell {
        write!(terminal.backend_mut(), "\x07")?;
    }
    std::io::Write::flush(terminal.backend_mut())
}

/// Opt-in boot-milestone log: `AMPLIFIER_PERF_LOG=<path>` appends JSONL lines
/// `{"event":..., "ms": <since process start>}` — used by perf/bench.py to
/// validate startup performance without instrumenting the render path.
struct PerfLog {
    start: std::time::Instant,
    path: Option<String>,
}

impl PerfLog {
    fn from_env() -> Self {
        Self { start: std::time::Instant::now(), path: std::env::var("AMPLIFIER_PERF_LOG").ok() }
    }

    fn mark(&self, event: &str) {
        let Some(path) = &self.path else { return };
        let ms = self.start.elapsed().as_secs_f64() * 1000.0;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
            use std::io::Write as _;
            let _ = writeln!(f, "{{\"event\":\"{event}\",\"ms\":{ms:.1}}}");
        }
    }
}

fn demo_app(tx: &Sender<Msg>, kitty_protocol: bool, initial_mode: Option<&str>) -> App {
    let wiring = Rc::new(RefCell::new(DemoWiring::new()));
    let adapter = DemoAdapter::new(Box::new(DemoRuntime::new(tx.clone())), Rc::clone(&wiring));
    App::new(Box::new(adapter), kitty_protocol, initial_mode, Some(wiring))
}

/// Crossterm key event → Textual chord name (the grammar the ported units
/// speak: `"enter"`, `"shift+tab"`, `"ctrl+t"`, single chars insert
/// themselves). `None` for chords the app has no binding for.
fn key_name(key: &KeyEvent) -> Option<String> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    let shift = key.modifiers.contains(KeyModifiers::SHIFT);
    Some(match key.code {
        KeyCode::Enter => {
            if ctrl {
                "ctrl+enter".into()
            } else if alt {
                "alt+enter".into()
            } else if shift {
                "shift+enter".into()
            } else {
                "enter".into()
            }
        }
        KeyCode::BackTab => "shift+tab".into(),
        KeyCode::Tab => {
            if shift {
                "shift+tab".into()
            } else {
                "tab".into()
            }
        }
        KeyCode::Esc => "escape".into(),
        KeyCode::Backspace => "backspace".into(),
        KeyCode::Up => "up".into(),
        KeyCode::Down => "down".into(),
        KeyCode::Left => "left".into(),
        KeyCode::Right => "right".into(),
        KeyCode::Char(c) if ctrl => format!("ctrl+{}", c.to_ascii_lowercase()),
        KeyCode::Char(c) if alt => format!("alt+{}", c.to_ascii_lowercase()),
        KeyCode::Char(c) => c.to_string(),
        _ => return None,
    })
}

/// The TUI-relevant launch flags of main.py's interactive group + `serve`
/// subcommand: `--bundle`, `--provider`/`-p`, `--model`/`-m`, `--mode`, and
/// `--resume <id>` (serve resolves the partial id itself). All are forwarded
/// to the backend command; `--mode` also seeds the App's opening posture.
#[derive(Clone, Debug, Default, PartialEq)]
struct LaunchFlags {
    bundle: Option<String>,
    provider: Option<String>,
    model: Option<String>,
    mode: Option<String>,
    resume: Option<String>,
}

/// Parse the launch flags out of `argv` (both `--flag value` and
/// `--flag=value`, like click). Unknown args pass through untouched.
fn parse_launch_flags(args: &[String]) -> LaunchFlags {
    let mut flags = LaunchFlags::default();
    let mut rest = args.iter().skip(1);
    while let Some(arg) = rest.next() {
        let (name, inline) = match arg.split_once('=') {
            Some((name, value)) => (name, Some(value.to_string())),
            None => (arg.as_str(), None),
        };
        let slot = match name {
            "--bundle" => &mut flags.bundle,
            "--provider" | "-p" => &mut flags.provider,
            "--model" | "-m" => &mut flags.model,
            "--mode" => &mut flags.mode,
            "--resume" => &mut flags.resume,
            _ => continue,
        };
        *slot = inline.or_else(|| rest.next().cloned());
    }
    flags
}

/// The launch flags as backend `serve` arguments (long-form, click grammar).
fn serve_flag_args(flags: &LaunchFlags) -> Vec<String> {
    let mut args = Vec::new();
    for (flag, value) in [
        ("--bundle", &flags.bundle),
        ("--provider", &flags.provider),
        ("--model", &flags.model),
        ("--mode", &flags.mode),
        ("--resume", &flags.resume),
    ] {
        if let Some(value) = value {
            args.push(flag.to_string());
            args.push(value.clone());
        }
    }
    args
}

/// The repo root when this crate sits inside the amplifier-app-newtui
/// checkout (`rust-mvp/` next to `src/amplifier_app_newtui/`) — the layout
/// where the REAL `serve` backend is runnable via `uv run`.
fn repo_serve_root() -> Option<std::path::PathBuf> {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent()?;
    root.join("src/amplifier_app_newtui")
        .is_dir()
        .then(|| root.to_path_buf())
}

/// The backend to spawn for the default (core-client) runtime, with the
/// launch flags appended, mirroring Python's launch order: an explicit
/// `AMPLIFIER_SERVE_CMD` wins; otherwise the REAL `uv run amplifier-newtui
/// serve` backend when the checkout is present (real session by default);
/// otherwise the offline mock with an honest notice (second value).
fn resolve_backend(
    env_cmd: Option<&str>,
    repo_root: Option<&std::path::Path>,
    flags: &LaunchFlags,
) -> (Vec<String>, Option<String>) {
    if let Some(cmd) = env_cmd {
        let mut parts: Vec<String> = cmd.split_whitespace().map(String::from).collect();
        if !parts.is_empty() {
            parts.extend(serve_flag_args(flags));
            return (parts, None);
        }
    }
    if let Some(root) = repo_root {
        // `--project` pins uv to the checkout while the session's project
        // dir stays the user's cwd (the Python launcher's behavior).
        let mut cmd = vec![
            "uv".to_string(),
            "run".to_string(),
            "--project".to_string(),
            root.display().to_string(),
            "amplifier-newtui".to_string(),
            "serve".to_string(),
        ];
        cmd.extend(serve_flag_args(flags));
        return (cmd, None);
    }
    (
        vec![
            "python3".to_string(),
            format!("{}/backend/serve_mock.py", env!("CARGO_MANIFEST_DIR")),
        ],
        Some("no amplifier-newtui checkout — offline mock backend (scripted turn)".to_string()),
    )
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
        // Faster than the quickest animation (splash 50ms); each animation
        // gates itself on its own cadence inside App::tick.
        thread::sleep(Duration::from_millis(25));
        if tx.send(Msg::Tick).is_err() {
            break;
        }
    });
}

fn setup_terminal() -> io::Result<Terminal<CrosstermBackend<Stdout>>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    // Bracketed paste keeps multi-line pastes atomic (the composer's stub
    // collapse + duplicate fence depend on whole-paste delivery). Mouse
    // capture feeds wheel scrolling + block/chip/badge/lane clicks.
    execute!(
        stdout,
        EnterAlternateScreen,
        cterm::EnableBracketedPaste,
        cterm::EnableMouseCapture
    )?;
    Terminal::new(CrosstermBackend::new(stdout))
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> io::Result<()> {
    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        cterm::DisableMouseCapture,
        cterm::DisableBracketedPaste,
        LeaveAlternateScreen
    )?;
    terminal.show_cursor()
}

// ---------------------------------------------------------------------------
// Headless flow tests — TestBackend renditions of the Python Pilot suites.
// Each test names the Python file it adapts.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use amplifier_newtui_rs::kernel::events as ev;
    use amplifier_newtui_rs::model::queues::{LaneSteeringQueue, NeedsYouQueue, SteeringQueue};
    use amplifier_newtui_rs::model::terminal::TerminalSurface;
    use amplifier_newtui_rs::model::trust::DenialLog;
    use amplifier_newtui_rs::protocol::WireEvent;
    use amplifier_newtui_rs::ui::app_support::QUEUED_NOTICE;
    use amplifier_newtui_rs::ui::footer::{footer_left_text, footer_right_text, footer_wrap};
    use amplifier_newtui_rs::ui::runtime_adapter::{RuntimeAdapter, RuntimeAdapterBase};
    use ratatui::backend::TestBackend;
    use rust_decimal::Decimal;
    use serde_json::{json, Map, Value};
    use std::str::FromStr;
    use std::sync::Mutex;

    // -- offline test adapter: records the ops the app routes to the runtime --

    struct TestAdapter {
        base: RuntimeAdapterBase,
        ops: Rc<RefCell<Vec<String>>>,
    }

    impl TestAdapter {
        fn new(ops: Rc<RefCell<Vec<String>>>) -> Self {
            let mut base = RuntimeAdapterBase::new();
            base.bundle_name = "newtui".into();
            base.model_name = "claude-sonnet-4-5".into();
            base.session_short = "core-01".into();
            Self { base, ops }
        }
    }

    impl RuntimeAdapter for TestAdapter {
        fn steering(&self) -> &SteeringQueue {
            &self.base.steering
        }
        fn lane_steering(&self) -> &LaneSteeringQueue {
            &self.base.lane_steering
        }
        fn needs_you(&self) -> &NeedsYouQueue {
            &self.base.needs_you
        }
        fn denial_log(&self) -> &Mutex<DenialLog> {
            &self.base.denial_log
        }
        fn terminal(&self) -> &TerminalSurface {
            &self.base.terminal
        }
        fn bundle_name(&self) -> String {
            self.base.bundle_name.clone()
        }
        fn model_name(&self) -> String {
            self.base.model_name.clone()
        }
        fn session_short(&self) -> String {
            self.base.session_short.clone()
        }
        fn submit(&mut self, text: &str, _a: &[ui::composer::ImageAttachment]) {
            self.ops.borrow_mut().push(format!("submit:{text}"));
        }
        fn interrupt(&mut self) -> bool {
            self.ops.borrow_mut().push("interrupt".into());
            true
        }
        fn answer_approval(&mut self, ticket_id: &str, choice: &str) {
            self.ops
                .borrow_mut()
                .push(format!("approve:{ticket_id}:{choice}"));
        }
        fn config_view(&mut self) -> amplifier_newtui_rs::model::config::ConfigSnapshotView {
            RuntimeAdapter::config_view(&mut self.base)
        }
        fn config_toggle(&mut self, c: &str, n: &str, e: bool) -> (bool, String) {
            RuntimeAdapter::config_toggle(&mut self.base, c, n, e)
        }
        fn config_set(&mut self, p: &str, v: &str) -> (bool, String) {
            RuntimeAdapter::config_set(&mut self.base, p, v)
        }
        fn config_diff(&mut self) -> Vec<amplifier_newtui_rs::model::config::ConfigChange> {
            RuntimeAdapter::config_diff(&mut self.base)
        }
        fn config_save(&mut self, scope: &str) -> (bool, String) {
            RuntimeAdapter::config_save(&mut self.base, scope)
        }
    }

    fn test_app() -> (App, Rc<RefCell<Vec<String>>>) {
        let ops = Rc::new(RefCell::new(Vec::new()));
        let mut app = App::new(
            Box::new(TestAdapter::new(Rc::clone(&ops))),
            true,
            None,
            None,
        );
        app.boot();
        app.on_resize(100, 32);
        (app, ops)
    }

    const SESSION: &str = "core-01";

    fn obj(v: Value) -> Map<String, Value> {
        match v {
            Value::Object(m) => m,
            _ => Map::new(),
        }
    }

    fn wire(event: ev::UIEvent) -> WireEvent {
        WireEvent::Event(event)
    }

    fn prompt_submit(text: &str) -> WireEvent {
        wire(ev::UIEvent::PromptSubmit(ev::PromptSubmit {
            session_id: SESSION.into(),
            ts: 100.0,
            prompt: text.into(),
            ..ev::PromptSubmit::default()
        }))
    }

    fn tool_pair(index: usize, tool: &str, input: Value, result: Value) -> Vec<WireEvent> {
        let call_id = format!("call-{index}");
        vec![
            wire(ev::UIEvent::ToolPre(ev::ToolPre {
                session_id: SESSION.into(),
                ts: 101.0,
                tool_name: tool.into(),
                tool_call_id: call_id.clone(),
                tool_input: obj(input.clone()),
                ..ev::ToolPre::default()
            })),
            wire(ev::UIEvent::ToolPost(ev::ToolPost {
                session_id: SESSION.into(),
                ts: 101.5,
                tool_name: tool.into(),
                tool_call_id: call_id,
                tool_input: obj(input),
                result: obj(result),
                ..ev::ToolPost::default()
            })),
        ]
    }

    fn usage(input: i64, output: i64, cache_read: i64, cache_write: i64) -> WireEvent {
        wire(ev::UIEvent::ProviderResponseUsage(ev::ProviderResponseUsage {
            session_id: SESSION.into(),
            ts: 102.0,
            input_tokens: input,
            output_tokens: output,
            cache_read,
            cache_write,
            model: "claude-sonnet-4-5".into(),
            ..ev::ProviderResponseUsage::default()
        }))
    }

    fn stream(answer: &str) -> Vec<WireEvent> {
        let mut events = vec![wire(ev::UIEvent::StreamBlockStart(ev::StreamBlockStart {
            session_id: SESSION.into(),
            ts: 103.0,
            ..ev::StreamBlockStart::default()
        }))];
        for word in answer.split_inclusive(' ') {
            events.push(wire(ev::UIEvent::StreamBlockDelta(ev::StreamBlockDelta {
                session_id: SESSION.into(),
                ts: 103.1,
                text: word.into(),
                ..ev::StreamBlockDelta::default()
            })));
        }
        events.push(wire(ev::UIEvent::StreamBlockEnd(ev::StreamBlockEnd {
            session_id: SESSION.into(),
            ts: 103.9,
            ..ev::StreamBlockEnd::default()
        })));
        events
    }

    fn prompt_complete(response: &str, files: i64, diffstat: &str) -> WireEvent {
        wire(ev::UIEvent::PromptComplete(ev::PromptComplete {
            session_id: SESSION.into(),
            ts: 104.0,
            response: response.into(),
            files_changed: files,
            diffstat: diffstat.into(),
            ..ev::PromptComplete::default()
        }))
    }

    const ANSWER: &str = "I've added a `/health` endpoint that returns 200 with a JSON \
status body, wired it into the router, and covered it with a test.";

    /// Feed a full serve-shaped turn up to (and including) the parked
    /// approval record.
    fn reach_approval(app: &mut App) {
        app.handle_wire(prompt_submit("Add a health check endpoint"));
        app.handle_wire(wire(ev::UIEvent::Notification(ev::Notification {
            session_id: SESSION.into(),
            ts: 100.5,
            message: "Thinking…".into(),
            ..ev::Notification::default()
        })));
        for (i, (tool, input)) in [
            ("read_file", json!({"path": "src/app.py"})),
            ("read_file", json!({"path": "src/router.py"})),
            ("read_file", json!({"path": "tests/test_app.py"})),
            ("bash", json!({"command": "pytest -q"})),
            ("bash", json!({"command": "ruff check ."})),
        ]
        .into_iter()
        .enumerate()
        {
            for event in tool_pair(i, tool, input, json!({"status": "ok"})) {
                app.handle_wire(event);
            }
        }
        app.handle_wire(usage(1200, 340, 800, 100));
        app.handle_wire(WireEvent::Approval {
            ticket_id: "approval-1".into(),
            prompt: "write_file src/health.py".into(),
            options: vec!["Allow once".into(), "Allow always".into(), "Deny".into()],
        });
    }

    /// Feed the granted-branch close-out (write, stream, usage, complete).
    fn finish_granted_turn(app: &mut App) {
        for event in tool_pair(
            9,
            "write_file",
            json!({"path": "src/health.py"}),
            json!({"status": "ok"}),
        ) {
            app.handle_wire(event);
        }
        for event in stream(ANSWER) {
            app.handle_wire(event);
        }
        app.handle_wire(usage(900, 120, 0, 0));
        app.handle_wire(prompt_complete(ANSWER, 1, "+18/−0"));
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

    fn draw_text(app: &App, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|f| ui::draw(f, app)).unwrap();
        buffer_text(terminal.backend().buffer())
    }

    fn type_text(app: &mut App, text: &str) {
        for ch in text.chars() {
            app.on_key(&ch.to_string());
        }
    }

    // Adapts tests/test_ui_snapshots.py + the pre-assembly
    // `renders_a_full_turn_headless`: prompt submit → narration/tool digest
    // → approval open → approve by ticket → streamed answer → turn rule,
    // with the exact Decimal session cost in the footer.
    #[test]
    fn test_ui_snapshots_full_turn_renders_headless() {
        let (mut app, ops) = test_app();
        reach_approval(&mut app);

        {
            let ui = app.ui.borrow();
            let bar = ui.approval.as_ref().expect("approval bar open");
            assert_eq!(bar.ticket_id, "approval-1");
            assert_eq!(bar.prompt, "write_file src/health.py");
            assert!(ui.turn_active, "turn parked, still active");
        }
        // Approve by ticket: Enter confirms the default "Allow once".
        app.on_key("enter");
        assert!(app.ui.borrow().approval.is_none(), "bar closes on resolve");
        assert!(
            ops.borrow().contains(&"approve:approval-1:Allow once".to_string()),
            "answer routed by ticket id: {:?}",
            ops.borrow()
        );

        finish_granted_turn(&mut app);
        assert!(!app.ui.borrow().turn_active, "turn closed out");

        // Exact money: $0.00924 + $0.0045 (kernel::cost fallback table,
        // oracle-checked against Python cost_of).
        assert_eq!(
            app.reducer.session_cost,
            Decimal::from_str("0.01374").unwrap()
        );

        let text = draw_text(&app, 100, 32);
        assert!(text.contains("Add a health check endpoint"), "user line:\n{text}");
        assert!(
            text.contains("Read 3 files · ran 2 shell commands"),
            "burst digest:\n{text}"
        );
        assert!(text.contains("/health"), "streamed answer made durable:\n{text}");
        assert!(text.contains("+18/−0"), "turn rule diffstat:\n{text}");
        assert!(text.contains("$0.01"), "footer cost:\n{text}");
        // The journal recorded the ask (approval → /improve evidence).
        assert_eq!(app.journal.lock().unwrap().tallies().len(), 1);
    }

    // Adapts tests/test_flow_approval.py: arrows/tab cycle the selection,
    // shift+tab cycles the SELECTION (never the mode) while the bar owns
    // the keyboard, esc denies, and the denied write renders the durable
    // ⊘ blocked line while the turn continues to its close-out.
    #[test]
    fn test_flow_approval_arrows_cycle_and_esc_denies_with_blocked_line() {
        let (mut app, ops) = test_app();
        reach_approval(&mut app);

        assert_eq!(
            app.ui.borrow().approval.as_ref().unwrap().option_texts(),
            vec!["› Allow once", "Allow always", "Deny"]
        );
        assert_eq!(app.ui.borrow().footer_context().as_str(), "approval");
        assert_eq!(
            footer_right_text(&app.footer_state()),
            "arrows select · enter confirm · esc deny"
        );

        app.on_key("right");
        assert_eq!(
            app.ui.borrow().approval.as_ref().unwrap().option_texts(),
            vec!["Allow once", "› Allow always", "Deny"]
        );
        app.on_key("tab");
        assert_eq!(
            app.ui.borrow().approval.as_ref().unwrap().option_texts(),
            vec!["Allow once", "Allow always", "› Deny"]
        );
        // Shift+tab cycles the selection — it must NOT cycle the mode.
        let mode_before = app.ui.borrow().mode.id;
        app.on_key("shift+tab");
        assert_eq!(app.ui.borrow().mode.id, mode_before);
        assert_eq!(
            app.ui.borrow().approval.as_ref().unwrap().option_texts(),
            vec!["› Allow once", "Allow always", "Deny"]
        );

        // Esc = Deny (spec §7).
        app.on_key("escape");
        assert!(
            ops.borrow().contains(&"approve:approval-1:Deny".to_string()),
            "esc denies by ticket: {:?}",
            ops.borrow()
        );

        // The denied write arrives as a denied tool:post → durable ⊘ line;
        // the turn continues to a no-ship close-out.
        for event in tool_pair(
            9,
            "write_file",
            json!({"path": "src/health.py"}),
            json!({
                "status": "denied",
                "reason": "denied by user",
                "continuation": "continuing without the write",
            }),
        ) {
            app.handle_wire(event);
        }
        let denied = "Understood — I left the endpoint out.";
        for event in stream(denied) {
            app.handle_wire(event);
        }
        app.handle_wire(usage(600, 80, 0, 0));
        app.handle_wire(prompt_complete(denied, 0, ""));
        assert!(!app.ui.borrow().turn_active);

        let text = draw_text(&app, 100, 32);
        assert!(text.contains('⊘'), "blocked glyph:\n{text}");
        assert!(text.contains("denied by user"), "deny reason:\n{text}");
        assert!(
            text.contains("continuing without the write"),
            "deny-and-continue note:\n{text}"
        );
        assert!(text.contains("I left the endpoint out"), "denied answer:\n{text}");
    }

    // DESIGN-SPEC §7 parity: opening an approval posts the exact notice
    // `approval required · choose below the transcript` while the bar
    // replaces the composer.
    #[test]
    fn test_flow_approval_open_posts_exact_notice() {
        let (mut app, _ops) = test_app();
        reach_approval(&mut app);
        let ui = app.ui.borrow();
        assert!(ui.approval.is_some(), "bar replaces composer");
        assert_eq!(
            ui.notices.current(),
            Some("approval required · choose below the transcript")
        );
    }

    // DESIGN-SPEC §7.3 parity (Python `app_support.mount_approval`): an
    // approval arriving while a lane is focused auto-returns to the parent
    // transcript with the `back to parent · approval required` notice.
    #[test]
    fn test_flow_approval_while_lane_focused_auto_returns_to_parent() {
        let (mut app, _ops) = test_app();
        // Fan the turn out to one delegate so a lane transcript exists.
        app.handle_wire(prompt_submit("fan out"));
        app.handle_wire(wire(ev::UIEvent::ToolPre(ev::ToolPre {
            session_id: SESSION.into(),
            ts: 100.5,
            tool_name: "delegate".into(),
            tool_call_id: "d1".into(),
            tool_input: obj(json!({"agent": "researcher", "instruction": "dig in"})),
            ..ev::ToolPre::default()
        })));
        app.handle_wire(wire(ev::UIEvent::AgentSpawned(ev::AgentSpawned {
            session_id: SESSION.into(),
            ts: 101.0,
            agent: "researcher".into(),
            sub_session_id: "s1".into(),
            parent_session_id: SESSION.into(),
            ..ev::AgentSpawned::default()
        })));

        // Focus the lane through the public key path: the panel auto-opened
        // at fan-out; Enter on the empty composer focuses the selected lane
        // (spec §8 focus).
        assert!(app.ui.borrow().lanes_panel.display(), "lanes panel auto-opened");
        app.on_key("enter");
        assert!(
            app.ui.borrow().transcript.focused_lane().is_some(),
            "lane focused before the approval arrives"
        );

        app.handle_wire(WireEvent::Approval {
            ticket_id: "approval-9".into(),
            prompt: "write_file src/notes.md".into(),
            options: vec!["Allow once".into(), "Allow always".into(), "Deny".into()],
        });

        let ui = app.ui.borrow();
        assert!(
            ui.transcript.focused_lane().is_none(),
            "auto-returned to the parent transcript"
        );
        assert!(ui.approval.is_some(), "bar still opened");
        assert_eq!(
            ui.notices.current(),
            Some("back to parent · approval required"),
            "auto-return notice overwrites the approval notice and stays"
        );
    }

    // DESIGN-SPEC §5 parity: Enter while a turn runs STEERS this turn
    // (kernel steering queue + the exact notice); a second steer queues a
    // FULL next-turn message instead (queued strip + `· q1` footer badge).
    #[test]
    fn test_flow_steer_running_enter_steers_then_second_steer_queues() {
        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("first turn"));
        assert!(app.ui.borrow().turn_active);

        type_text(&mut app, "focus on the tests");
        app.on_key("enter");
        assert_eq!(
            app.steering.pending_steers().len(),
            1,
            "running Enter queues a steer, not a submit"
        );
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("steer queued · shift+enter queues a full next-turn message")
        );
        assert!(
            !app.ui.borrow().queued_strip.display(),
            "a steer is not a queued next-turn message"
        );

        // Second steer while one is pending → queue a full message (§5).
        type_text(&mut app, "and update the docs");
        app.on_key("enter");
        assert_eq!(app.steering.pending_steers().len(), 1, "still one steer");
        let ui = app.ui.borrow();
        assert_eq!(
            ui.queued_strip.queued(),
            Some("and update the docs"),
            "second steer became the queued next-turn message"
        );
        drop(ui);
        assert!(
            footer_left_text(&app.footer_state()).contains("q1"),
            "footer queue badge"
        );
    }

    // Adapts tests/test_flow_modes.py: boot posture is auto (§4 amendment);
    // shift+tab walks chat → plan → brainstorm → build → auto with the
    // exact `mode <id> · <trust>` notice and the footer mode segment.
    #[test]
    fn test_flow_modes_shift_tab_cycles_with_notice() {
        let (mut app, _ops) = test_app();
        assert_eq!(app.ui.borrow().mode.id.as_str(), "auto");
        assert!(app.ui.borrow().composer.has_class("mode-auto"));

        for expected in ["chat", "plan", "brainstorm", "build", "auto"] {
            app.on_key("shift+tab");
            let ui = app.ui.borrow();
            assert_eq!(ui.mode.id.as_str(), expected);
            let trust = ui.mode.trust_str;
            assert_eq!(
                ui.notices.current(),
                Some(format!("mode {expected} · {trust}").as_str())
            );
            assert!(ui.composer.has_class(&format!("mode-{expected}")));
            drop(ui);
            assert!(
                footer_left_text(&app.footer_state())
                    .starts_with(&format!("mode {expected}")),
                "footer mode segment"
            );
        }
    }

    // Adapts tests/test_flow_interrupt.py: esc while running requests the
    // interrupt (the notice waits for close-out); the settled turn carries
    // the italic `Interrupted. Goal: …` recap, the `· interrupted` rule,
    // and the `turn interrupted · context saved` end notice.
    #[test]
    fn test_flow_interrupt_esc_requests_break_then_recap_and_rule() {
        let (mut app, ops) = test_app();
        app.handle_wire(prompt_submit("refactor the session store"));
        assert!(app.ui.borrow().turn_active);

        app.on_key("escape");
        assert!(
            ops.borrow().contains(&"interrupt".to_string()),
            "esc routed the interrupt: {:?}",
            ops.borrow()
        );
        // Esc only requests the break — the notice waits for close-out.
        assert_ne!(
            app.ui.borrow().notices.current(),
            Some("turn interrupted · context saved")
        );

        app.handle_wire(wire(ev::UIEvent::CancelCompleted(ev::CancelCompleted {
            session_id: SESSION.into(),
            ts: 105.0,
            ..ev::CancelCompleted::default()
        })));
        app.handle_wire(prompt_complete("", 0, ""));

        assert!(!app.ui.borrow().turn_active);
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("turn interrupted · context saved")
        );
        let text = draw_text(&app, 100, 32);
        assert!(text.contains("Interrupted. Goal:"), "recap line:\n{text}");
        assert!(text.contains("interrupted"), "interrupted rule:\n{text}");
        // Nothing shipped.
        assert!(!app.reducer.ledger.last_shipped());
    }

    // Adapts tests/test_flow_palette.py: "/" opens the palette strip with
    // grouped rows; the live filter narrows; Enter runs the matched
    // builtin (here /theme — cycles slate → graphite) and closes the
    // filter with the composer cleared.
    #[test]
    fn test_flow_palette_slash_opens_and_builtin_runs() {
        let (mut app, _ops) = test_app();
        app.on_key("/");
        assert!(app.ui.borrow().palette.is_open());
        let text = draw_text(&app, 100, 32);
        assert!(text.contains("DURING"), "group headers show unfiltered:\n{text}");

        type_text(&mut app, "theme");
        {
            let ui = app.ui.borrow();
            assert_eq!(ui.palette.filter_text(), Some("/theme"));
            assert_eq!(
                ui.palette.selected_command().map(|spec| spec.name.clone()),
                Some("/theme".to_string())
            );
        }
        app.on_key("enter");
        let ui = app.ui.borrow();
        assert_eq!(ui.theme_name, "graphite", "builtin ran and cycled the theme");
        assert_eq!(ui.notices.current(), Some("theme graphite"));
        assert!(!ui.palette.is_open(), "filter cleared after run");
        assert_eq!(ui.composer.text(), "", "composer cleared");
        // The command echoed as a ❯ user line.
        drop(ui);
        let text = draw_text(&app, 100, 32);
        assert!(text.contains("/theme"), "command echo:\n{text}");
    }

    // Adapts tests/test_flow_steer_queue.py: shift+enter mid-turn queues
    // the FULL next-turn message (queued strip + `· q1` footer + notice);
    // at close-out the queue drains into the next submitted turn.
    #[test]
    fn test_flow_steer_queue_shift_enter_queues_and_drains_at_turn_end() {
        let (mut app, ops) = test_app();
        app.handle_wire(prompt_submit("first turn"));
        assert!(app.ui.borrow().turn_active);

        type_text(&mut app, "also update the docs");
        app.on_key("shift+enter");
        {
            let ui = app.ui.borrow();
            assert!(ui.queued_strip.display(), "queued strip shows");
            assert_eq!(
                ui.queued_strip.queued(),
                Some("also update the docs"),
                "queued text kept verbatim"
            );
            assert_eq!(ui.notices.current(), Some(QUEUED_NOTICE));
        }
        assert!(
            footer_left_text(&app.footer_state()).contains("q1"),
            "footer queue badge"
        );
        let text = draw_text(&app, 100, 32);
        assert!(text.contains("queued"), "queued strip rendered:\n{text}");

        // Turn end → the queue drains into the next submitted turn.
        app.handle_wire(prompt_complete("done", 0, ""));
        assert!(
            ops.borrow()
                .contains(&"submit:also update the docs".to_string()),
            "queued message drained as the next turn: {:?}",
            ops.borrow()
        );
        let ui = app.ui.borrow();
        assert!(!ui.queued_strip.display(), "strip cleared after drain");
        assert_eq!(ui.notices.current(), Some("queued message picked up"));
    }

    // Adapts tests/test_ui_snapshots.py's narrow-width golden: at 40 cols
    // the footer hints wrap to their own full-width second row instead of
    // clipping (mockup flex-wrap).
    #[test]
    fn test_ui_snapshots_footer_wraps_at_narrow_width() {
        let (mut app, _ops) = test_app();
        app.on_resize(40, 20);
        let state = app.footer_state();
        assert!(footer_wrap(&state, 40).wrapped, "narrow width wraps the hints");
        let text = draw_text(&app, 40, 20);
        let rows: Vec<&str> = text.lines().collect();
        let last = rows[rows.len() - 1];
        let second_last = rows[rows.len() - 2];
        assert!(
            last.contains("history") || last.contains("rewind"),
            "hints on their own row:\n{text}"
        );
        assert!(
            second_last.contains("mode auto"),
            "left segment on the row above:\n{text}"
        );
    }

    // The `--demo` composition end-to-end: DemoAdapter + DemoRuntime play
    // the scripted turn (real event vocabulary) through the assembled
    // reducer, including the parked approval answered from the bar. The
    // demo session carries the $0.40 scripted cost baseline (DemoWiring).
    #[test]
    fn test_flow_demo_scripted_turn_end_to_end() {
        let (tx, rx) = channel::<Msg>();
        let mut app = demo_app(&tx, true, None);
        app.boot();
        app.on_resize(100, 32);
        assert!(app.ui.borrow().splash.is_none(), "demo identity known at boot");
        assert_eq!(app.ui.borrow().bundle, "anchors");
        app.submit_prompt("Add a health check endpoint");

        let mut answered = false;
        while let Ok(msg) = rx.recv_timeout(std::time::Duration::from_secs(5)) {
            if let Msg::Rt(ev) = msg {
                app.handle_wire(ev);
                if app.ui.borrow().approval.is_some() && !answered {
                    answered = true;
                    app.on_key("enter"); // Allow once
                }
                if answered && !app.ui.borrow().turn_active {
                    break;
                }
            }
        }
        assert!(answered, "demo turn parked on the approval");
        assert!(!app.ui.borrow().turn_active, "demo turn closed out");
        // $0.40 baseline + $0.00924 + $0.0045 priced usage.
        assert_eq!(
            app.reducer.session_cost,
            Decimal::from_str("0.41374").unwrap()
        );
        let text = draw_text(&app, 100, 32);
        assert!(text.contains("Read 3 files · ran 2 shell commands"), "digest:\n{text}");
        assert!(text.contains("/health"), "answer:\n{text}");
    }

    // ------------------------------------------------------------------
    // Mouse wiring (hit-testing against the FrameLayout recorded by draw;
    // each test names the Python case it adapts).
    // ------------------------------------------------------------------

    /// Draw once and return the recorded frame layout.
    fn layout_after_draw(app: &App, width: u16, height: u16) -> amplifier_newtui_rs::ui::FrameLayout {
        let _ = draw_text(app, width, height);
        app.layout.borrow().clone()
    }

    // Adapts tests/test_ui_transcript_view.py::test_tool_line_click_toggles_
    // body_in_place: a click on the tool line's summary row expands the body
    // in place (same block id); a second click collapses it.
    #[test]
    fn test_tool_line_click_toggles_body_in_place() {
        use amplifier_newtui_rs::model::blocks::{ToolLine, TranscriptBlock};
        let (mut app, _ops) = test_app();
        let tool = ToolLine {
            body: vec!["$ pytest -q".into(), "42 passed".into()],
            ..ToolLine::new("t-1", "✔ bash · pytest -q")
        };
        let _ = app.ui.borrow_mut().transcript.append(tool.into(), 0.0);

        let layout = layout_after_draw(&app, 100, 32);
        let (_, start, len) = layout
            .block_lines
            .iter()
            .find(|(id, _, _)| id == "t-1")
            .cloned()
            .expect("tool line laid out");
        assert_eq!(len, 1, "collapsed tool line is one row");
        let y = layout.transcript.y + (start - layout.transcript_scroll) as u16;
        app.on_mouse_down(layout.transcript.x, y);

        let block = app.ui.borrow().transcript.get_block("t-1").unwrap();
        let TranscriptBlock::ToolLine(tool) = block else {
            panic!("still a tool line");
        };
        assert!(tool.expanded, "click expanded the body in place");
        let text = draw_text(&app, 100, 32);
        assert!(text.contains("42 passed"), "body rows visible:\n{text}");

        // Second click on the same summary row collapses it again.
        let layout = layout_after_draw(&app, 100, 32);
        let (_, start, len) = layout
            .block_lines
            .iter()
            .find(|(id, _, _)| id == "t-1")
            .cloned()
            .expect("tool line laid out");
        assert!(len > 1, "expanded tool line grew rows");
        let y = layout.transcript.y + (start - layout.transcript_scroll) as u16;
        app.on_mouse_down(layout.transcript.x, y);
        let block = app.ui.borrow().transcript.get_block("t-1").unwrap();
        let TranscriptBlock::ToolLine(tool) = block else {
            panic!("still a tool line");
        };
        assert!(!tool.expanded, "second click collapsed it");
    }

    // Adapts tests/test_ui_approval.py::test_click_confirms_that_option:
    // clicking the Deny chip selects AND confirms that option by ticket.
    #[test]
    fn test_click_confirms_that_option() {
        let (mut app, ops) = test_app();
        reach_approval(&mut app);
        let layout = layout_after_draw(&app, 100, 32);
        let col = (0..layout.input.width as usize)
            .find(|col| {
                let ui = app.ui.borrow();
                ui.approval
                    .as_ref()
                    .and_then(|bar| ui::approval_hit(bar, *col, 1))
                    == Some(2)
            })
            .expect("deny chip x-range");
        app.on_mouse_down(layout.input.x + col as u16, layout.input.y + 1);
        assert!(app.ui.borrow().approval.is_none(), "bar closes on resolve");
        assert!(
            ops.borrow().contains(&"approve:approval-1:Deny".to_string()),
            "clicked option confirmed by ticket: {:?}",
            ops.borrow()
        );
    }

    // Adapts tests/test_ui_footer.py::test_footer_badge_shows_and_click_
    // posts_message (the click half): clicking the orange waiting badge
    // runs the same action as ctrl+y — the needs-you listing mounts.
    #[test]
    fn test_footer_badge_shows_and_click_posts_message() {
        use amplifier_newtui_rs::model::queues::DeferOptions;
        let (mut app, _ops) = test_app();
        let _ = app
            .needs_you
            .defer("Deploy to prod?", "risky", DeferOptions::default());
        let blocks_before = app.ui.borrow().transcript.block_ids().len();

        let layout = layout_after_draw(&app, 140, 32);
        let (start, end) = layout.badge_span.expect("waiting badge painted inline");
        let text = draw_text(&app, 140, 32);
        assert!(text.contains("1 decision waiting · ctrl-y"), "badge:\n{text}");

        app.on_mouse_down((start + end) / 2, layout.footer.y);
        let ui = app.ui.borrow();
        assert_eq!(
            ui.transcript.block_ids().len(),
            blocks_before + 1,
            "needs-you listing mounted"
        );
        drop(ui);
        let text = draw_text(&app, 140, 32);
        assert!(text.contains("Deploy to prod?"), "listing shows the ask:\n{text}");
    }

    // Adapts tests/test_flow_modes.py::test_mode_badge_click_cycles: a click
    // on the composer's [mode] badge cycles the posture, exactly like
    // shift+tab (auto → chat at boot).
    #[test]
    fn test_mode_badge_click_cycles() {
        let (mut app, _ops) = test_app();
        assert_eq!(app.ui.borrow().mode.id.as_str(), "auto");
        let layout = layout_after_draw(&app, 100, 32);
        assert!(layout.mode_badge_width > 0, "badge painted");
        app.on_mouse_down(layout.input.x, layout.input.y);
        let ui = app.ui.borrow();
        assert_eq!(ui.mode.id.as_str(), "chat", "badge click cycled the mode");
        let trust = ui.mode.trust_str;
        assert_eq!(ui.notices.current(), Some(format!("mode chat · {trust}").as_str()));
        drop(ui);
        // A click past the badge is composer body, not the badge.
        let layout = layout_after_draw(&app, 100, 32);
        app.on_mouse_down(layout.input.x + layout.mode_badge_width, layout.input.y);
        assert_eq!(app.ui.borrow().mode.id.as_str(), "chat", "no second cycle");
    }

    // Adapts ui/transcript.py's follow-anchor cases through the mouse layer
    // (test_tail_follow_sticks_to_bottom_until_user_scrolls_up): wheel-up
    // over the transcript releases the anchor and scrolls up; wheel-down
    // re-arms it only once the view is back at the bottom.
    #[test]
    fn test_wheel_up_releases_follow_and_wheel_down_at_bottom_rearms() {
        use amplifier_newtui_rs::model::blocks::{Answer, Segment};
        let (mut app, _ops) = test_app();
        reach_approval(&mut app);
        app.on_key("enter");
        finish_granted_turn(&mut app);
        // Pad history so the content is comfortably taller than the view.
        for index in 0..8 {
            let answer = Answer::new(
                format!("fill-{index}"),
                vec![Segment::new(format!("history line {index}"))],
            );
            let _ = app.ui.borrow_mut().transcript.append(answer.into(), 0.0);
        }

        let layout = layout_after_draw(&app, 100, 12);
        let rect = layout.transcript;
        let visible = rect.height as usize;
        assert!(
            layout.transcript_total_lines > visible + 4,
            "content taller than the viewport: total={} visible={}",
            layout.transcript_total_lines,
            visible
        );
        let bottom = layout.transcript_total_lines - visible;
        assert!(app.ui.borrow().transcript.follow());
        assert_eq!(layout.transcript_scroll, bottom, "anchored at the bottom");

        // Wheel up twice: anchor releases; the view walks up 2 lines/notch.
        app.on_mouse_scroll(true, rect.x, rect.y);
        assert!(!app.ui.borrow().transcript.follow(), "wheel-up released the anchor");
        app.on_mouse_scroll(true, rect.x, rect.y);
        assert_eq!(layout_after_draw(&app, 100, 12).transcript_scroll, bottom - 4);

        // Wheel down while still above the bottom: no re-arm yet.
        app.on_mouse_scroll(false, rect.x, rect.y);
        assert!(!app.ui.borrow().transcript.follow(), "not at the bottom yet");
        assert_eq!(layout_after_draw(&app, 100, 12).transcript_scroll, bottom - 2);

        // Wheel down to the bottom: the anchor re-arms.
        app.on_mouse_scroll(false, rect.x, rect.y);
        assert!(app.ui.borrow().transcript.follow(), "back at bottom re-arms follow");
        assert_eq!(layout_after_draw(&app, 100, 12).transcript_scroll, bottom);

        // A wheel outside the transcript region is ignored.
        app.on_mouse_scroll(true, layout.footer.x, layout.footer.y);
        assert!(app.ui.borrow().transcript.follow(), "footer wheel does not scroll");
    }

    // Adapts the lanes-panel click half of tests/test_ui_lanes.py: a click
    // on a lane row focuses that lane's transcript (Python _LaneRow.on_click
    // → FocusLane), without moving the selection highlight.
    #[test]
    fn test_lanes_panel_row_click_focuses_lane() {
        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("fan out"));
        app.handle_wire(wire(ev::UIEvent::ToolPre(ev::ToolPre {
            session_id: SESSION.into(),
            ts: 100.5,
            tool_name: "delegate".into(),
            tool_call_id: "d1".into(),
            tool_input: obj(json!({"agent": "researcher", "instruction": "dig in"})),
            ..ev::ToolPre::default()
        })));
        app.handle_wire(wire(ev::UIEvent::AgentSpawned(ev::AgentSpawned {
            session_id: SESSION.into(),
            ts: 101.0,
            agent: "researcher".into(),
            sub_session_id: "s1".into(),
            parent_session_id: SESSION.into(),
            ..ev::AgentSpawned::default()
        })));
        assert!(app.ui.borrow().lanes_panel.display(), "panel auto-opened");

        let layout = layout_after_draw(&app, 100, 32);
        let row = layout
            .lane_rows
            .iter()
            .position(|index| *index == Some(0))
            .expect("lane row laid out");
        assert!(layout.lane_rows[0].is_none(), "row 0 is the header");
        app.on_mouse_down(layout.lanes.x, layout.lanes.y + row as u16);
        assert!(
            app.ui.borrow().transcript.focused_lane().is_some(),
            "row click focused the lane"
        );
    }

    // ------------------------------------------------------------------
    // Transcript drag-selection + copy (each test names the Python case in
    // tests/test_ui_composer.py it adapts).
    // ------------------------------------------------------------------

    /// Append a few answer rows and drag-select the first `rows` of the
    /// first block. Returns the selected text.
    fn drag_select_rows(app: &mut App, rows: u16) -> String {
        use amplifier_newtui_rs::model::blocks::{Answer, Segment};
        for index in 0..4 {
            let answer = Answer::new(
                format!("sel-{index}"),
                vec![Segment::new(format!("selection line {index}"))],
            );
            let _ = app.ui.borrow_mut().transcript.append(answer.into(), 0.0);
        }
        let layout = layout_after_draw(app, 100, 32);
        let rect = layout.transcript;
        let (_, start, _) = layout.block_lines[0].clone();
        let y0 = rect.y + (start - layout.transcript_scroll) as u16;
        app.on_mouse_down(rect.x, y0);
        app.on_mouse_drag(rect.x + 30, y0 + rows - 1);
        app.on_mouse_up(rect.x + 30, y0 + rows - 1);
        app.selected_text()
    }

    /// Route selection copies into a recording sink (the injectable copier).
    fn recording_copier(app: &mut App, accept: bool) -> Rc<RefCell<Vec<String>>> {
        let copied = Rc::new(RefCell::new(Vec::<String>::new()));
        let sink = Rc::clone(&copied);
        app.set_clipboard_copier(Box::new(move |text| {
            sink.borrow_mut().push(text.to_string());
            accept
        }));
        copied
    }

    // Adapts tests/test_ui_composer.py::test_settled_drag_selection_copies_
    // automatically: a settled transcript drag-selection lands on the
    // clipboard by itself (the 0.4s settle timer via the tick clock), with
    // the exact notice, and never re-copies the same settled selection.
    #[test]
    fn test_settled_drag_selection_copies_automatically() {
        use ratatui::style::Modifier;
        let (mut app, _ops) = test_app();
        let copied = recording_copier(&mut app, true);

        let text = drag_select_rows(&mut app, 3);
        assert!(text.contains("selection line 0"), "anchor row selected: {text}");
        assert_eq!(text.lines().count(), 3, "three rendered rows: {text:?}");
        assert!(
            app.ui.borrow().selection_settle_deadline.is_some(),
            "settle timer armed by the drag"
        );
        assert!(copied.borrow().is_empty(), "no copy before the settle");

        // The selected rows paint REVERSED (the selection highlight).
        let layout = layout_after_draw(&app, 100, 32);
        let rect = layout.transcript;
        let (_, start, _) = layout.block_lines[0].clone();
        let y0 = rect.y + (start - layout.transcript_scroll) as u16;
        let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
        terminal.draw(|f| ui::draw(f, &app)).unwrap();
        let cell = &terminal.backend().buffer()[(rect.x, y0)];
        assert!(
            cell.modifier.contains(Modifier::REVERSED),
            "selected row highlighted"
        );

        // Let the settle timer fire on the tick clock.
        app.ui.borrow_mut().selection_settle_deadline = Some(0.0);
        app.tick();
        assert_eq!(copied.borrow().len(), 1, "settled selection copied once");
        assert_eq!(copied.borrow()[0], text);
        let chars = text.chars().count();
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some(format!("copied on select · {chars} chars").as_str()),
            "Python's exact copy-on-select notice"
        );

        // No duplicate copy for the same settled selection.
        app.ui.borrow_mut().selection_settle_deadline = Some(0.0);
        app.tick();
        assert_eq!(copied.borrow().len(), 1, "no duplicate auto-copy");
    }

    // Adapts tests/test_ui_composer.py::test_ctrl_c_copies_transcript_
    // selection_despite_composer_focus: ctrl+c copies an active transcript
    // selection (and clears it) instead of quitting; with nothing selected
    // it keeps the interrupt (running) / quit (idle) convention.
    #[test]
    fn test_ctrl_c_copies_transcript_selection_despite_composer_focus() {
        let (mut app, _ops) = test_app();
        let copied = recording_copier(&mut app, true);

        let text = drag_select_rows(&mut app, 2);
        assert!(!text.is_empty());
        // The composer holds the keyboard — the selection still wins.
        type_text(&mut app, "hi");
        app.on_key("ctrl+c");
        assert!(!app.should_quit(), "copy short-circuits quit");
        assert_eq!(copied.borrow().as_slice(), std::slice::from_ref(&text));
        let chars = text.chars().count();
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some(format!("copied · {chars} chars").as_str()),
            "Python's exact explicit-copy notice"
        );
        assert!(app.ui.borrow().selection.is_none(), "selection cleared by the copy");

        // Nothing selected, idle → quit (like ctrl+d).
        app.on_key("ctrl+c");
        assert!(app.should_quit(), "idle ctrl+c still quits");

        // Nothing selected, running turn → interrupt, not quit.
        let (mut app, ops) = test_app();
        app.handle_wire(prompt_submit("first turn"));
        assert!(app.ui.borrow().turn_active);
        app.on_key("ctrl+c");
        assert!(
            ops.borrow().contains(&"interrupt".to_string()),
            "running ctrl+c interrupts: {:?}",
            ops.borrow()
        );
        assert!(!app.should_quit(), "running ctrl+c does not quit");

        // A failing OS clipboard tool keeps Python's honest suffix.
        let (mut app, _ops) = test_app();
        let _copied = recording_copier(&mut app, false);
        let text = drag_select_rows(&mut app, 2);
        app.on_key("ctrl+c");
        let chars = text.chars().count();
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some(
                format!(
                    "copied · {chars} chars · empty clipboard? allow terminal clipboard access"
                )
                .as_str()
            )
        );
    }

    // Python screen-selection semantics: a plain click (no drag) clears the
    // selection before the normal click dispatch, and no settled copy fires
    // for the cleared selection.
    #[test]
    fn test_click_without_drag_clears_selection() {
        let (mut app, _ops) = test_app();
        let copied = recording_copier(&mut app, true);
        let text = drag_select_rows(&mut app, 2);
        assert!(!text.is_empty());

        let layout = layout_after_draw(&app, 100, 32);
        let rect = layout.transcript;
        app.on_mouse_down(rect.x + 5, rect.y);
        app.on_mouse_up(rect.x + 5, rect.y);
        assert!(app.ui.borrow().selection.is_none(), "plain click cleared it");
        assert_eq!(app.selected_text(), "", "nothing left to copy");
        app.tick();
        assert!(copied.borrow().is_empty(), "no settled copy after the clear");
    }

    // ------------------------------------------------------------------
    // Boot progress (regression: no module names while amplifier loads).
    // ------------------------------------------------------------------

    // The Rust half of tests/test_serve_offline.py::test_serve_emits_boot_
    // progress_records_before_session_started: a boot.progress record paints
    // the splash with Python boot_progress's exact text (snake_case action
    // read as words, `action · detail`), stderr chatter no longer overwrites
    // it, and session.started still dissolves the splash.
    #[test]
    fn test_boot_progress_sets_splash_status_and_wins_over_chatter() {
        let ops = Rc::new(RefCell::new(Vec::new()));
        let mut adapter = TestAdapter::new(Rc::clone(&ops));
        adapter.base.session_short = String::new(); // protocol boot: no identity yet
        let mut app = App::new(Box::new(adapter), true, None, None);
        app.boot();
        app.on_resize(100, 32);
        assert!(app.ui.borrow().splash.is_some(), "splash up while booting");

        app.handle_wire(WireEvent::BootProgress {
            action: "installing_package".into(),
            detail: "tool-bash".into(),
        });
        assert_eq!(
            app.ui.borrow().splash.as_ref().unwrap().status(),
            "installing package · tool-bash"
        );

        // Fallback stderr chatter loses to the structured record.
        app.on_boot_chatter("stray module print");
        assert_eq!(
            app.ui.borrow().splash.as_ref().unwrap().status(),
            "installing package · tool-bash"
        );

        // A detail-less phase renders the bare action (Python parity).
        app.handle_wire(WireEvent::BootProgress {
            action: "creating".into(),
            detail: String::new(),
        });
        assert_eq!(app.ui.borrow().splash.as_ref().unwrap().status(), "creating");

        app.handle_wire(WireEvent::SessionStarted {
            session_id: "core-0123456".into(),
            bundle: "newtui".into(),
            model: "claude-sonnet-4-5".into(),
        });
        assert!(app.ui.borrow().splash.is_none(), "identity dissolves the splash");
    }

    // ------------------------------------------------------------------
    // Launch surface + serve-error/boot-failure plumbing.
    // ------------------------------------------------------------------

    /// A protocol-boot app: no identity yet, splash up (the state a real
    /// `serve` spawn is in until `session.started` lands).
    fn boot_pending_app() -> App {
        let ops = Rc::new(RefCell::new(Vec::new()));
        let mut adapter = TestAdapter::new(ops);
        adapter.base.session_short = String::new();
        let mut app = App::new(Box::new(adapter), true, None, None);
        app.boot();
        app.on_resize(140, 32);
        assert!(app.ui.borrow().splash.is_some(), "splash up while booting");
        app
    }

    /// Python `announce_boot_failure`'s exact hint line.
    const DOCTOR_HINT: &str = "Check provider setup with `amplifier-newtui doctor`, or run \
`--demo` for a credential-free UI. Press ctrl+d to quit.";

    /// The rendered frame with all wrapping/padding whitespace collapsed —
    /// exact-string asserts over lines the terminal width may wrap.
    fn flat_text(app: &App) -> String {
        draw_text(app, 140, 32)
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
    }

    // Adapts tests/test_interactive_launch.py::test_app_seeds_initial_mode:
    // `--mode plan` seeds the opening posture through App::new.
    #[test]
    fn test_app_seeds_initial_mode() {
        let ops = Rc::new(RefCell::new(Vec::new()));
        let app = App::new(Box::new(TestAdapter::new(ops)), true, Some("plan"), None);
        assert_eq!(app.ui.borrow().mode.id.as_str(), "plan");
        assert!(app.ui.borrow().composer.has_class("mode-plan"));
    }

    // Adapts tests/test_interactive_launch.py::test_app_defaults_to_auto_
    // without_initial_mode.
    #[test]
    fn test_app_defaults_to_auto_without_initial_mode() {
        let ops = Rc::new(RefCell::new(Vec::new()));
        let app = App::new(Box::new(TestAdapter::new(ops)), true, None, None);
        assert_eq!(app.ui.borrow().mode.id.as_str(), "auto");
    }

    /// Pure flag→backend-args assembly: click grammar in (`--flag value`,
    /// `--flag=value`, short `-p`/`-m`), serve argv out, launch order
    /// env-override → real serve (repo layout) → mock + honest notice.
    #[test]
    fn test_launch_flags_assemble_backend_command() {
        let argv: Vec<String> = [
            "amplifier-newtui-rs",
            "--bundle",
            "newtui",
            "-p",
            "anthropic",
            "-m",
            "claude-x",
            "--mode=plan",
            "--resume",
            "core-0123",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let flags = parse_launch_flags(&argv);
        assert_eq!(
            flags,
            LaunchFlags {
                bundle: Some("newtui".into()),
                provider: Some("anthropic".into()),
                model: Some("claude-x".into()),
                mode: Some("plan".into()),
                resume: Some("core-0123".into()),
            }
        );
        let forwarded = [
            "--bundle", "newtui", "--provider", "anthropic", "--model", "claude-x", "--mode",
            "plan", "--resume", "core-0123",
        ];

        // AMPLIFIER_SERVE_CMD wins, with the flags appended to it too.
        let (cmd, notice) =
            resolve_backend(Some("uv run amplifier-newtui serve"), None, &flags);
        let expected: Vec<&str> = ["uv", "run", "amplifier-newtui", "serve"]
            .into_iter()
            .chain(forwarded)
            .collect();
        assert_eq!(cmd, expected);
        assert!(notice.is_none());

        // Repo layout → the REAL serve backend by default (uv --project).
        let (cmd, notice) =
            resolve_backend(None, Some(std::path::Path::new("/checkout")), &flags);
        let expected: Vec<&str> =
            ["uv", "run", "--project", "/checkout", "amplifier-newtui", "serve"]
                .into_iter()
                .chain(forwarded)
                .collect();
        assert_eq!(cmd, expected);
        assert!(notice.is_none());

        // No checkout → the offline mock, honestly labeled.
        let (cmd, notice) = resolve_backend(None, None, &LaunchFlags::default());
        assert_eq!(cmd[0], "python3");
        assert!(cmd[1].ends_with("backend/serve_mock.py"), "mock fallback: {cmd:?}");
        assert!(
            notice.as_deref().is_some_and(|n| n.contains("mock")),
            "fallback carries an honest notice: {notice:?}"
        );
    }

    // The serve boot-failure record (`{"type":"error",...}` before
    // session.started, exit 1) dismisses the splash immediately and renders
    // announce_boot_failure's exact diagnosis + doctor hint + notice.
    #[test]
    fn test_boot_error_record_dismisses_splash_with_exact_diagnosis() {
        let mut app = boot_pending_app();
        app.handle_wire(WireEvent::Error {
            error: "no provider configured".into(),
            error_type: "RuntimeError".into(),
        });
        assert!(app.ui.borrow().splash.is_none(), "splash dismissed immediately");
        assert_eq!(app.ui.borrow().notices.current(), Some("session failed to start"));
        let text = flat_text(&app);
        assert!(
            text.contains("⊘ session failed to start · no provider configured"),
            "diagnosis line:\n{text}"
        );
        assert!(text.contains(DOCTOR_HINT), "doctor hint:\n{text}");

        // A blank error message falls back to the exception type (Python
        // `str(error).strip() or error.__class__.__name__`).
        let mut app = boot_pending_app();
        app.handle_wire(WireEvent::Error {
            error: "  ".into(),
            error_type: "ProviderAuthError".into(),
        });
        let text = flat_text(&app);
        assert!(
            text.contains("⊘ session failed to start · ProviderAuthError"),
            "error_type fallback:\n{text}"
        );
    }

    // Backend stdout EOF before session.started (crash without a structured
    // record — the old failure mode left the splash hanging forever) runs
    // the same boot-failure diagnosis.
    #[test]
    fn test_backend_eof_before_identity_runs_boot_failure_diagnosis() {
        let mut app = boot_pending_app();
        app.on_backend_exited();
        assert!(app.ui.borrow().splash.is_none(), "splash dismissed");
        assert_eq!(app.ui.borrow().notices.current(), Some("session failed to start"));
        let text = flat_text(&app);
        assert!(
            text.contains("⊘ session failed to start · backend exited before session.started"),
            "EOF diagnosis:\n{text}"
        );
        assert!(text.contains(DOCTOR_HINT), "doctor hint:\n{text}");

        // The failed backend's EOF trails its own error record: the
        // rendered diagnosis must not be clobbered by a second notice.
        let mut app = boot_pending_app();
        app.handle_wire(WireEvent::Error {
            error: "boom".into(),
            error_type: "RuntimeError".into(),
        });
        app.on_backend_exited();
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("session failed to start"),
            "error-record diagnosis stays"
        );
    }

    // Mid-session paths: a failed turn surfaces Python's exact
    // `turn failed · <error>` notice (`_submit_prompt`'s except-arm — the
    // session stays live); a backend exit after identity says so honestly.
    #[test]
    fn test_midsession_error_and_backend_exit_notices() {
        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("add a health check"));
        app.handle_wire(WireEvent::Error {
            error: "provider auth expired".into(),
            error_type: "APIStatusError".into(),
        });
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("turn failed · provider auth expired")
        );
        assert!(app.ui.borrow().splash.is_none(), "no boot diagnosis mid-turn");

        app.on_backend_exited();
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("backend exited · session lost — ctrl+d to quit")
        );
    }

    // Python `_print_resume_hint`: a real session id learned from
    // session.started prints the exact two-line farewell; demo/unstarted
    // sessions (no stored id) print nothing.
    #[test]
    fn test_resume_hint_exact_text_after_session_started() {
        let mut app = boot_pending_app();
        assert_eq!(app.resume_session_id(), "");
        assert_eq!(resume_hint(&app.resume_session_id()), None, "no id · no hint");
        app.handle_wire(WireEvent::SessionStarted {
            session_id: "core-0123456789abcdef".into(),
            bundle: "newtui".into(),
            model: "claude-sonnet-4-5".into(),
        });
        assert_eq!(app.resume_session_id(), "core-0123456789abcdef", "FULL id kept");
        assert_eq!(
            resume_hint(&app.resume_session_id()).as_deref(),
            Some(
                "resume this session: amplifier-newtui resume core-0123456789abcdef\n\
                 list sessions:       amplifier-newtui sessions"
            )
        );
    }

    /// Prints a real rendered frame (run: `cargo test -- --nocapture snapshot`).
    #[test]
    fn snapshot() {
        let (mut app, _ops) = test_app();
        reach_approval(&mut app);
        app.on_key("enter");
        finish_granted_turn(&mut app);
        println!("\n{}", draw_text(&app, 100, 32));
    }
}
