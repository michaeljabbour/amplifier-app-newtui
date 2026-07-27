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
use runtime::ScriptedDemoRuntime;
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
    demo_app_with(tx, kitty_protocol, initial_mode, false)
}

/// The full `--demo` composition (Python `NewTuiApp(DemoRuntimeAdapter())`):
/// the real scripted engine ([`ScriptedDemoRuntime`], the `kernel/demo.py`
/// port) behind [`DemoAdapter`], with the script's step-boundary hooks wired
/// to the live app — steer consumption, the LIVE mode gate, and the
/// esc-interrupt close-out bridge. `instant` is the tests' zero-sleep mode.
fn demo_app_with(
    tx: &Sender<Msg>,
    kitty_protocol: bool,
    initial_mode: Option<&str>,
    instant: bool,
) -> App {
    let wiring = Rc::new(RefCell::new(DemoWiring::new()));
    let runtime = ScriptedDemoRuntime::new(tx.clone());
    if instant {
        runtime.set_instant();
    }
    let runtime = Rc::new(RefCell::new(runtime));
    let adapter = DemoAdapter::new(Rc::clone(&runtime), Rc::clone(&wiring));
    let mut app = App::new(
        Box::new(adapter),
        kitty_protocol,
        initial_mode,
        Some(Rc::clone(&wiring)),
    );
    // Python `DemoRuntime(steer_source=self._consume_steer,
    // mode_source=self._current_mode)`: the script consumes ONE queued
    // steer per step boundary and reads the LIVE mode for the pytest
    // approval gate (spec §4/§5).
    let steering = std::sync::Arc::clone(&app.steering);
    runtime
        .borrow()
        .set_steer_source(Box::new(move || {
            steering.consume_next_steer().map(|message| message.text)
        }));
    let mode_shared = std::sync::Arc::clone(&app.ui.borrow().mode_shared);
    runtime
        .borrow()
        .set_mode_source(Box::new(move || mode_shared.lock().unwrap().clone()));
    // Esc-interrupt close-out: Python's adapter `turn_spec` reads
    // `runtime.interrupted_close` live; the Rust client copies it into the
    // wiring when the cancelled close-out lands (`set_demo_interrupt_bridge`).
    let bridge_runtime = Rc::clone(&runtime);
    app.set_demo_interrupt_bridge(Box::new(move || {
        wiring
            .borrow_mut()
            .set_interrupted_close(bridge_runtime.borrow().interrupted_close());
    }));
    app
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

    /// Pump the demo runtime's wire events into the app until `done` holds
    /// (the headless analogue of the Python tests' Pilot `wait_for`).
    fn drain_demo(app: &mut App, rx: &std::sync::mpsc::Receiver<Msg>, done: impl Fn(&App) -> bool) {
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while !done(app) {
            assert!(
                std::time::Instant::now() < deadline,
                "demo drain timed out before the condition held"
            );
            match rx.recv_timeout(Duration::from_secs(5)) {
                Ok(Msg::Rt(ev)) => app.handle_wire(ev),
                Ok(_) => {}
                Err(_) => panic!("demo runtime went quiet before the condition held"),
            }
        }
    }

    /// Plain text of every transcript block's spans/summary (assertions over
    /// durable content that the 100-col frame may wrap).
    fn transcript_text(app: &App) -> String {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        app.ui
            .borrow()
            .transcript
            .blocks()
            .iter()
            .map(|block| match block {
                TranscriptBlock::Answer(answer) => answer
                    .spans
                    .iter()
                    .map(|span| span.text.as_str())
                    .collect::<String>(),
                TranscriptBlock::UserLine(line) => line.text.clone(),
                _ => String::new(),
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    // The `--demo` composition end-to-end — REWRITTEN for the real
    // kernel/demo.py port (ScriptedDemoRuntime): the legacy pin asserted
    // the serve-mock-shaped health-endpoint turn ($0.41374, `Read 3 files
    // · ran 2 shell commands`, `/health`); the composition now plays the
    // REAL Python demo script, so this pins tests/test_flow_ledger.py's
    // expectations instead — the seed replay lands the $0.57 mount cost
    // ($0.40 baseline + the seed spec), the chat-mode build turn parks on
    // the scripted pytest approval (`Run uv run pytest tests/store/ -q?`,
    // ticket demo-ticket-1), and `Allow once` closes out with
    // kernel.demo.build_answer(denied=False) at the spec's cumulative cost.
    #[test]
    fn test_flow_demo_scripted_turn_end_to_end() {
        use amplifier_newtui_rs::ui::demo_wiring::{
            build_answer, demo_turn_by_key, TurnKey, PYTEST_APPROVAL_PROMPT,
        };
        let (tx, rx) = channel::<Msg>();
        let mut app = demo_app_with(&tx, true, None, true);
        app.boot();
        app.on_resize(100, 32);
        assert!(app.ui.borrow().splash.is_none(), "demo identity known at boot");
        assert_eq!(app.ui.borrow().bundle, "anchors");
        assert_eq!(app.ui.borrow().session_short, "e07d");

        // The seed transcript replays as a live turn (Python `start()`).
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 1 && !app.ui.borrow().turn_active
        });
        // $0.40 baseline + the seed spec's cost → the mockup's $0.57.
        assert_eq!(app.reducer.session_cost, Decimal::from_str("0.57").unwrap());
        assert!(
            transcript_text(&app).contains("command-line app for Amplifier"),
            "seed answer landed"
        );

        // Chat mode gates the pytest ask (spec §4 — mockup
        // `this.mode().id === "chat"` read LIVE at the step boundary);
        // the typed text echoes verbatim and plays the next unplayed
        // scripted turn: build.
        app.ui.borrow_mut().set_mode_by_id("chat", false);
        type_text(&mut app, "hi");
        app.on_key("enter");
        drain_demo(&mut app, &rx, |app| app.ui.borrow().approval.is_some());
        {
            let ui = app.ui.borrow();
            let bar = ui.approval.as_ref().expect("approval bar open");
            assert_eq!(bar.ticket_id, "demo-ticket-1", "the scripted broker ticket");
            assert_eq!(bar.prompt, PYTEST_APPROVAL_PROMPT);
            assert!(ui.turn_active, "turn parked, still active");
        }

        app.on_key("enter"); // Allow once
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 2 && !app.ui.borrow().turn_active
        });
        let text = transcript_text(&app);
        assert!(text.contains("hi"), "verbatim user echo:\n{text}");
        assert!(
            text.contains(&build_answer(false)),
            "allowed build close-out answer:\n{text}"
        );
        // Cumulative session spend after the build turn (mockup
        // `this.cost`), straight from the pinned spec table.
        assert_eq!(
            app.reducer.session_cost,
            demo_turn_by_key(TurnKey::Build).cost_after
        );
        assert!(app.reducer.ledger.last_shipped(), "build turn shipped");
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
        let (_, start, _) = layout
            .block_lines
            .iter()
            .find(|(id, _, _)| id == "sel-0")
            .cloned()
            .expect("first selection row laid out");
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
        let (_, start, _) = layout
            .block_lines
            .iter()
            .find(|(id, _, _)| id == "sel-0")
            .cloned()
            .expect("first selection row laid out");
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

    // Adapts tests/test_ui_reducer_replay.py::test_replay_closes_a_dangling_
    // turn_as_interrupted to the live wire: a backend that dies mid-turn can
    // never deliver its `prompt_complete` close-out, which left the working
    // pulse mounted (and "working…") forever under the user's last turn.
    // The exit settles the turn with the same durable shape a live Esc
    // leaves, and the session-lost notice stands.
    #[test]
    fn test_backend_exit_closes_a_dangling_turn_as_interrupted() {
        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("first turn"));
        assert_eq!(blocks_of(&app, "working_status").len(), 1, "pulse mounted");

        app.on_backend_exited();
        assert!(
            blocks_of(&app, "working_status").is_empty(),
            "the dangling pulse unmounted"
        );
        assert!(!app.ui.borrow().turn_active, "turn settled");
        let text = flat_text(&app);
        assert!(text.contains("Interrupted. Goal: first turn."), "recap:\n{text}");
        assert!(!app.reducer.ledger.last_shipped(), "nothing shipped");
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("backend exited · session lost — ctrl+d to quit"),
            "the session-lost notice outlives the interrupt notice"
        );
    }

    // The "stuck on my last turn" sweep: the working pulse must unmount on
    // EVERY close-out shape — normal completion, esc interrupt
    // (tests/test_flow_interrupt.py), a leftover steer discarded at turn end
    // (tests/test_flow_steer_queue.py::test_leftover_steer_discarded_at_turn_end),
    // a queued follow-up draining at close-out
    // (tests/test_flow_steer_queue.py), and a failed turn's error record
    // (kernel/serve.py: `RealRuntime.submit` emits its close-out from
    // `finally`, so the error record is still followed by prompt_complete).
    #[test]
    fn test_working_line_unmounts_on_every_close_out_path() {
        let no_working = |app: &App| blocks_of(app, "working_status").is_empty();

        // Normal completion.
        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("add a health check"));
        assert!(!no_working(&app), "pulse mounts with the turn");
        app.handle_wire(prompt_complete(ANSWER, 1, "+18/−0"));
        assert!(no_working(&app), "normal completion drops the pulse");

        // Esc interrupt → cancelled close-out.
        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("refactor the session store"));
        app.on_key("escape");
        app.handle_wire(wire(ev::UIEvent::CancelCompleted(ev::CancelCompleted {
            session_id: SESSION.into(),
            ts: 105.0,
            ..ev::CancelCompleted::default()
        })));
        app.handle_wire(prompt_complete("", 0, ""));
        assert!(no_working(&app), "interrupted close-out drops the pulse");

        // Steered turn (the leftover steer is discarded at turn end).
        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("first turn"));
        type_text(&mut app, "never applied");
        app.on_key("enter"); // Enter-while-running queues a steer
        app.handle_wire(prompt_complete("done", 0, ""));
        assert!(no_working(&app), "steered turn drops the pulse");

        // Queued follow-up drains at close-out — turn one's pulse still drops.
        let (mut app, ops) = test_app();
        app.handle_wire(prompt_submit("first turn"));
        type_text(&mut app, "second turn");
        app.on_key("shift+enter"); // queue the FULL next-turn message
        app.handle_wire(prompt_complete("done", 0, ""));
        assert!(
            ops.borrow().contains(&"submit:second turn".to_string()),
            "queue drained: {:?}",
            ops.borrow()
        );
        assert!(no_working(&app), "queued follow-up still drops the pulse");

        // Failed turn: error record, then the serve contract's close-out.
        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("auth expires mid-turn"));
        app.handle_wire(WireEvent::Error {
            error: "provider auth expired".into(),
            error_type: "APIStatusError".into(),
        });
        app.handle_wire(prompt_complete("", 0, ""));
        assert!(no_working(&app), "failed turn drops the pulse");
    }

    // The Rust half of the recorded MIGRATION gap "shimmer motion static":
    // nothing advanced the working label's motion frame and the draw path
    // rendered the reducer's raw block (`motion_frame` forever 0), so the
    // band froze — "shimmer stuck on my last turn". Adapts the
    // `_motion_timer` half of tests/test_ui_transcript_view.py::
    // test_working_status_widget_pulses_spinner: the tick clock sweeps the
    // band at MOTION_INTERVAL_SECONDS while a turn runs.
    #[test]
    fn test_working_label_shimmer_advances_on_the_tick_clock() {
        use amplifier_newtui_rs::ui::transcript::MOTION_INTERVAL_SECONDS;

        let (mut app, _ops) = test_app();
        app.handle_wire(prompt_submit("long provider call"));

        let motion_at = |app: &App, now: f64| -> u32 {
            app.ui
                .borrow()
                .transcript
                .display_blocks(now)
                .iter()
                .find_map(|block| match block {
                    amplifier_newtui_rs::model::blocks::TranscriptBlock::WorkingStatus(w) => {
                        Some(w.motion_frame)
                    }
                    _ => None,
                })
                .expect("working line mounted")
        };

        // Anchor past the process clock so every cadence gate is due.
        let base = amplifier_newtui_rs::app::monotonic() + 1000.0;
        app.tick_at(base);
        let first = motion_at(&app, base);
        let step = MOTION_INTERVAL_SECONDS + 0.001;
        app.tick_at(base + step);
        app.tick_at(base + 2.0 * step);
        let later = motion_at(&app, base + 2.0 * step);
        assert!(
            later >= first + 2,
            "the band sweeps one cell per interval: {first} → {later}"
        );

        // Between due intervals the frame holds — the shimmer runs at the
        // Python cadence, not the 25ms loop rate.
        app.tick_at(base + 2.0 * step + 0.01);
        assert_eq!(motion_at(&app, base + 2.0 * step + 0.01), later);
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

    // ------------------------------------------------------------------
    // Demo-composition flow adaptations (each test names the Python case
    // it adapts; the demo runtime plays instantly, events drain headless).
    // ------------------------------------------------------------------

    /// Instant demo app with the seed replay settled (the Python tests'
    /// `seed_done` helper).
    fn demo_seed_done() -> (App, std::sync::mpsc::Receiver<Msg>) {
        let (tx, rx) = channel::<Msg>();
        let mut app = demo_app_with(&tx, true, None, true);
        app.boot();
        app.on_resize(120, 40);
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 1 && !app.ui.borrow().turn_active
        });
        (app, rx)
    }

    fn blocks_of(app: &App, kind: &str) -> Vec<amplifier_newtui_rs::model::blocks::TranscriptBlock> {
        app.ui
            .borrow()
            .transcript
            .blocks()
            .into_iter()
            .filter(|block| block.kind() == kind)
            .collect()
    }

    /// Click the first painted row of `block_id` (drawing first so the
    /// frame layout is current).
    fn click_block(app: &mut App, block_id: &str) {
        let layout = layout_after_draw(app, 120, 40);
        let (_, start, _) = layout
            .block_lines
            .iter()
            .find(|(id, _, _)| id == block_id)
            .cloned()
            .expect("block laid out");
        let y = layout.transcript.y + (start - layout.transcript_scroll) as u16;
        app.on_mouse_down(layout.transcript.x, y);
    }

    // Adapts tests/test_app_boot.py::test_demo_boot_banner_seed_and_typed_
    // turn (banner + seed half): the session banner block renders at boot
    // from the adapter's identity with the exact DEMO_BANNER strings, and
    // the seed turn replays (verbatim seed prompt, batched tool line,
    // turn rule t1).
    #[test]
    fn test_demo_boot_banner_seed_and_typed_turn() {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        use amplifier_newtui_rs::ui::demo_wiring::{DEMO_BANNER, SEED_PROMPT};
        let (app, _rx) = demo_seed_done();
        let blocks = app.ui.borrow().transcript.blocks();
        let TranscriptBlock::SessionBanner(banner) = &blocks[0] else {
            panic!("first block is the session banner, got {:?}", blocks[0].kind());
        };
        assert_eq!(
            (banner.headline.as_str(), banner.detail.as_str()),
            DEMO_BANNER,
            "exact Python DEMO_BANNER"
        );
        let user_lines = blocks_of(&app, "user_line");
        let TranscriptBlock::UserLine(seed) = &user_lines[0] else {
            unreachable!()
        };
        assert_eq!(seed.text, SEED_PROMPT);
        assert!(
            blocks_of(&app, "tool_line").iter().any(|block| matches!(
                block,
                TranscriptBlock::ToolLine(tool) if tool.summary == "Ran 2 shell commands"
            )),
            "seed batch tool line"
        );
        let rules = blocks_of(&app, "turn_rule");
        let TranscriptBlock::TurnRule(rule) = &rules[0] else {
            unreachable!()
        };
        assert_eq!(rule.checkpoint_id, "t1");
        let text = draw_text(&app, 120, 40);
        assert!(text.contains(DEMO_BANNER.0), "banner headline renders:\n{text}");
    }

    // The protocol half of the session-banner wiring (Python
    // `announce_ready` appends SessionBanner once identity is known):
    // `session.started` carries no version headline, so the client
    // synthesizes the Python identity detail line.
    #[test]
    fn test_session_started_appends_identity_banner() {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        let mut app = boot_pending_app();
        app.handle_wire(WireEvent::SessionStarted {
            session_id: "core-0123456".into(),
            bundle: "newtui".into(),
            model: "claude-sonnet-4-5".into(),
        });
        let banners = blocks_of(&app, "session_banner");
        assert_eq!(banners.len(), 1, "one banner at boot");
        let TranscriptBlock::SessionBanner(banner) = &banners[0] else {
            unreachable!()
        };
        assert_eq!(banner.headline, "");
        assert_eq!(
            banner.detail,
            "Bundle: newtui | claude-sonnet-4-5 · session core-01"
        );
        let text = flat_text(&app);
        assert!(text.contains("Bundle: newtui"), "banner renders:\n{text}");
    }

    // Adapts tests/test_flow_ledger.py::test_ctrl_l_prints_session_ledger:
    // ctrl-l over the seeded demo prints the exact ledger scrollback block
    // (mockup cmdLedger prints `this.cost` — the $0.57 session cost).
    #[test]
    fn test_ctrl_l_prints_session_ledger() {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        let (mut app, _rx) = demo_seed_done();
        app.on_key("ctrl+l");
        let ledgers = blocks_of(&app, "ledger");
        let TranscriptBlock::Ledger(ledger) = ledgers.last().expect("ledger block") else {
            unreachable!()
        };
        assert_eq!((ledger.session.as_str(), ledger.bundle.as_str()), ("e07d", "anchors"));
        assert_eq!(ledger.turns, 1);
        assert_eq!(ledger.spend, Decimal::from_str("0.57").unwrap());
        assert_eq!((ledger.shipped, ledger.answer_only), (0, 1));
        assert_eq!(ledger.cache_hit_pct, 91);
        let text = draw_text(&app, 200, 40);
        assert!(
            text.contains("· Session ledger  e07d · anchors"),
            "exact header:\n{text}"
        );
        assert!(
            text.contains("1 turns · $0.57 · 0 shipped · 1 answer-only · cache hit 91%"),
            "exact summary line:\n{text}"
        );
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("ledger printed to scrollback")
        );
    }

    // Adapts tests/test_flow_ledger.py::test_clicking_final_answer_prints_
    // evidence_block: clicking the seed answer prints the scripted evidence
    // block (numbered teal claims → grounding tool calls) with the exact
    // §10 header and notice.
    #[test]
    fn test_clicking_final_answer_prints_evidence_block() {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        use amplifier_newtui_rs::ui::demo_wiring::DEMO_EVIDENCE;
        let (mut app, _rx) = demo_seed_done();
        let answer_id = blocks_of(&app, "answer")
            .iter()
            .find_map(|block| match block {
                TranscriptBlock::Answer(answer) if !answer.evidence_refs.is_empty() => {
                    Some(answer.id.clone())
                }
                _ => None,
            })
            .expect("seed answer carries evidence refs");
        click_block(&mut app, &answer_id);

        let evidences = blocks_of(&app, "evidence");
        let TranscriptBlock::Evidence(evidence) = evidences.last().expect("evidence block")
        else {
            unreachable!()
        };
        assert_eq!(evidence.links.len(), DEMO_EVIDENCE.len());
        assert_eq!(evidence.links[0].claim_quote, DEMO_EVIDENCE[0].quote);
        assert_eq!(evidence.links[0].tool_ref, DEMO_EVIDENCE[0].source);
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("evidence revealed · every claim traces to a tool call")
        );
        let text = draw_text(&app, 200, 40);
        assert!(
            text.contains("· Evidence  1/2 · ←/→ select · enter expand · esc close"),
            "exact §10 header:\n{text}"
        );
        assert!(
            text.contains("¹ \"dashboard and steering wheel\" → Ran 2 shell commands"),
            "first numbered claim:\n{text}"
        );
    }

    // Adapts tests/test_flow_ledger.py::test_evidence_block_keys_select_
    // expand_and_close (spec §10): the header's advertised keys actually
    // work — ←/→ select (header 1/N tracks), enter expand (the demo links
    // carry no correlation key, so the grounding reference surfaces as the
    // notice), esc close hands the keyboard back to the composer.
    #[test]
    fn test_evidence_block_keys_select_expand_and_close() {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        use amplifier_newtui_rs::ui::demo_wiring::DEMO_EVIDENCE;
        let (mut app, _rx) = demo_seed_done();
        let answer_id = blocks_of(&app, "answer")
            .iter()
            .find_map(|block| match block {
                TranscriptBlock::Answer(answer) if !answer.evidence_refs.is_empty() => {
                    Some(answer.id.clone())
                }
                _ => None,
            })
            .expect("seed answer carries evidence refs");
        click_block(&mut app, &answer_id);
        let evidence_id = match blocks_of(&app, "evidence").last() {
            Some(TranscriptBlock::Evidence(evidence)) => evidence.id.clone(),
            other => panic!("evidence block mounted, got {other:?}"),
        };
        // The block takes the keyboard so the advertised keys are live.
        assert_eq!(
            app.ui.borrow().focused_evidence.as_deref(),
            Some(evidence_id.as_str())
        );

        // → selects the next claim; the header counter tracks (2/2).
        app.on_key("right");
        let selected = match blocks_of(&app, "evidence").last() {
            Some(TranscriptBlock::Evidence(evidence)) => evidence.selected,
            _ => unreachable!(),
        };
        assert_eq!(selected, 1);
        assert!(
            draw_text(&app, 200, 40).contains("· Evidence  2/2 · "),
            "header tracks the selection"
        );
        // ← selects back (clamped at the first claim).
        app.on_key("left");
        app.on_key("left");
        let selected = match blocks_of(&app, "evidence").last() {
            Some(TranscriptBlock::Evidence(evidence)) => evidence.selected,
            _ => unreachable!(),
        };
        assert_eq!(selected, 0, "clamped at the first claim");

        // enter expands the selected claim: no correlation key on the demo
        // links, so the grounding reference surfaces as the exact notice.
        app.on_key("enter");
        assert_eq!(
            app.ui.borrow().notices.current().map(str::to_string),
            Some(format!("grounded by {}", DEMO_EVIDENCE[0].source))
        );

        // esc closes the block and hands the keyboard back.
        app.on_key("escape");
        assert!(blocks_of(&app, "evidence").is_empty(), "evidence closed");
        assert!(app.ui.borrow().focused_evidence.is_none());
        type_text(&mut app, "x");
        assert_eq!(
            app.ui.borrow().composer.text(),
            "x",
            "typing reaches the composer again"
        );
    }

    // The correlated half of Python `on_expand_evidence_claim` (spec §10
    // deep-link, exercised by tests/test_ui_transcript_view.py's message
    // plumbing): enter on a claim whose link carries a tool_call_id expands
    // the grounding tool line in place and scrolls it into view.
    #[test]
    fn test_evidence_enter_expands_grounding_tool_line() {
        use amplifier_newtui_rs::model::blocks::{Answer, Segment, ToolLine, TranscriptBlock};
        use amplifier_newtui_rs::model::evidence::EvidenceLink;
        let (mut app, _ops) = test_app();
        app.on_resize(120, 40);
        {
            let mut ui = app.ui.borrow_mut();
            let tool = ToolLine {
                body: vec!["$ pytest -q".into(), "42 passed".into()],
                tool_call_ids: vec!["call-9".into()],
                ..ToolLine::new("t-9", "✔ bash · pytest -q")
            };
            let _ = ui.transcript.append(tool.into(), 0.0);
            let answer = Answer {
                evidence_refs: vec![EvidenceLink {
                    tool_call_id: "call-9".into(),
                    ..EvidenceLink::new("tests pass", "bash pytest -q")
                }],
                ..Answer::new("a-9", vec![Segment::new("All 42 tests pass.")])
            };
            let _ = ui.transcript.append(answer.into(), 0.0);
        }
        click_block(&mut app, "a-9");
        assert!(
            app.ui.borrow().focused_evidence.is_some(),
            "evidence opened and focused"
        );

        app.on_key("enter");
        let block = app.ui.borrow().transcript.get_block("t-9").unwrap();
        let TranscriptBlock::ToolLine(tool) = block else {
            panic!("still a tool line");
        };
        assert!(tool.expanded, "enter expanded the grounding tool line");
        assert!(
            !app.ui.borrow().transcript.follow(),
            "deep-link released the tail anchor to reveal the line"
        );

        // esc closes the evidence block.
        let evidence_id = app.ui.borrow().focused_evidence.clone().unwrap();
        app.on_key("escape");
        assert!(app.ui.borrow().transcript.get_block(&evidence_id).is_none());
    }

    // Adapts tests/test_flow_steer_queue.py::test_enter_mid_turn_steers_
    // echo_and_applies_at_step_boundary over the REAL scripted demo: the
    // ↳ echo block + exact notice on Enter-while-running; the queued steer
    // is consumed at the build turn's next step boundary (`Applying steer:
    // …` narration) and its echo drops.
    #[test]
    fn test_enter_mid_turn_steers_echo_and_applies_at_step_boundary() {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        use amplifier_newtui_rs::ui::app_support::STEER_NOTICE;
        let (mut app, rx) = demo_seed_done();
        app.ui.borrow_mut().set_mode_by_id("chat", false);
        type_text(&mut app, "hi");
        app.on_key("enter");
        drain_demo(&mut app, &rx, |app| app.ui.borrow().turn_active);

        // Running + Enter → steer with the ↳ echo block + exact notice.
        type_text(&mut app, "focus on the tests");
        app.on_key("enter");
        {
            let echoes = blocks_of(&app, "steer_echo");
            assert_eq!(echoes.len(), 1);
            let TranscriptBlock::SteerEcho(echo) = &echoes[0] else {
                unreachable!()
            };
            assert_eq!(echo.text, "focus on the tests");
            assert_eq!(app.ui.borrow().notices.current(), Some(STEER_NOTICE));
            assert_eq!(app.footer_state().queued, 0, "steers are not the qN badge");
        }

        // The turn parks on the pytest approval; answering releases the
        // next step boundary, which consumes the steer.
        drain_demo(&mut app, &rx, |app| app.ui.borrow().approval.is_some());
        app.on_key("enter"); // Allow once
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 2 && !app.ui.borrow().turn_active
        });
        assert!(
            blocks_of(&app, "narration").iter().any(|block| matches!(
                block,
                TranscriptBlock::Narration(narration)
                    if narration.text == "Applying steer: focus on the tests"
            )),
            "step boundary consumed the steer"
        );
        assert!(blocks_of(&app, "steer_echo").is_empty(), "echo removed");
        assert!(app.steering.pending_steers().is_empty());
    }

    // Adapts tests/test_flow_steer_queue.py::test_leftover_steer_discarded_
    // at_turn_end: a steer no step boundary consumed is discarded at turn
    // end — never rolling forward as a turn the user never sent — and its
    // ↳ echo drops with the honest discard notice.
    #[test]
    fn test_leftover_steer_discarded_at_turn_end() {
        use amplifier_newtui_rs::ui::app_support::STEER_DISCARDED_NOTICE;
        let (mut app, ops) = test_app();
        app.handle_wire(prompt_submit("first turn"));
        type_text(&mut app, "never applied");
        app.on_key("enter");
        assert_eq!(blocks_of(&app, "steer_echo").len(), 1);
        assert_eq!(app.steering.pending_steers().len(), 1);

        app.handle_wire(prompt_complete("done", 0, ""));
        assert!(blocks_of(&app, "steer_echo").is_empty(), "echo removed at discard");
        assert!(app.steering.pending_steers().is_empty());
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some(STEER_DISCARDED_NOTICE)
        );
        assert!(
            !ops.borrow().iter().any(|op| op == "submit:never applied"),
            "a discarded steer never becomes a submitted turn: {:?}",
            ops.borrow()
        );
    }

    // Adapts tests/test_flow_interrupt.py::test_esc_interrupts_running_
    // turn_with_recap_and_interrupted_rule over the REAL scripted demo
    // (paced playback — the esc lands inside the first step's wait): the
    // turn stops at the next step boundary, the interrupt bridge serves
    // the interrupted close-out spec through the wiring, so the rule reads
    // `· interrupted`, the checkpoint carries the Python label, and
    // nothing ships.
    #[test]
    fn test_esc_interrupts_running_turn_with_recap_and_interrupted_rule() {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        use amplifier_newtui_rs::ui::demo_wiring::BUILD_PROMPT;
        let (tx, rx) = channel::<Msg>();
        let mut app = demo_app_with(&tx, true, None, false); // real pacing
        app.boot();
        app.on_resize(120, 40);
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 1 && !app.ui.borrow().turn_active
        });

        type_text(&mut app, BUILD_PROMPT);
        app.on_key("enter");
        drain_demo(&mut app, &rx, |app| app.ui.borrow().turn_active);
        app.on_key("escape");
        // Esc only requests the break — the notice waits for close-out.
        assert_ne!(
            app.ui.borrow().notices.current(),
            Some("turn interrupted · context saved")
        );
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 2 && !app.ui.borrow().turn_active
        });
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("turn interrupted · context saved")
        );
        let text = draw_text(&app, 120, 40);
        assert!(text.contains("Interrupted. Goal:"), "recap line:\n{text}");
        let rules = blocks_of(&app, "turn_rule");
        let TranscriptBlock::TurnRule(rule) = rules.last().unwrap() else {
            unreachable!()
        };
        assert!(
            rule.label.ends_with(" · interrupted"),
            "interrupted rule label: {:?}",
            rule.label
        );
        assert!(!rule.shipped, "dimmer label (spec §3: not shipped)");
        let checkpoints = app.reducer.ledger.checkpoints();
        assert_eq!(
            checkpoints.last().unwrap().label,
            "store refactor · interrupted",
            "the bridge served interrupted_spec's checkpoint label"
        );
        assert!(!app.reducer.ledger.last_shipped(), "no ▲ yield glyph");
    }

    // Adapts tests/test_flow_lanes.py::test_focus_lane_child_transcript_
    // banner_and_esc_back: ↓ + Enter focuses the second lane (coder) — the
    // transcript swaps to the subagent's own blocks (focus banner +
    // [delegated] brief + state recap), the footer shows the exact
    // lane-focus hint, and esc returns to the parent with the exact
    // `back to parent session` notice (test_flow_lanes.py:283).
    #[test]
    fn test_focus_lane_child_transcript_banner_and_esc_back() {
        use amplifier_newtui_rs::model::blocks::TranscriptBlock;
        use amplifier_newtui_rs::ui::demo_wiring::{demo_lane_by_name, AGENTS_PROMPT, DEMO_SESSION_ID};
        use amplifier_newtui_rs::ui::needs_you::focused_lane_banner;
        let (mut app, rx) = demo_seed_done();
        app.submit_prompt(AGENTS_PROMPT);
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 2 && !app.ui.borrow().turn_active
        });
        assert!(app.ui.borrow().lanes_panel.display(), "panel auto-opened at fan-out");

        // ↓ then Enter focuses the second lane (coder).
        app.on_key("down");
        app.on_key("enter");
        let lane = demo_lane_by_name("coder").expect("scripted lane");
        assert_eq!(
            app.ui.borrow().transcript.focused_lane(),
            Some(lane.sub_session_id.as_str())
        );
        // The panel stays open while a lane is focused.
        assert!(app.ui.borrow().lanes_panel.display());

        let blocks = app.ui.borrow().transcript.blocks();
        let TranscriptBlock::SessionBanner(banner) = &blocks[0] else {
            panic!("focus banner first, got {:?}", blocks[0].kind());
        };
        assert_eq!(banner.focus_note, focused_lane_banner("coder", DEMO_SESSION_ID));
        assert_eq!(
            banner.focus_note,
            "focused: coder · subagent of e07de0 · own context window \
             · results report back to parent · esc back"
        );
        let TranscriptBlock::UserLine(delegated) = &blocks[1] else {
            panic!("[delegated] brief second, got {:?}", blocks[1].kind());
        };
        assert_eq!(delegated.mode, "delegated");
        assert_eq!(delegated.text, lane.brief);
        assert!(!blocks_of(&app, "narration").is_empty(), "own log rendered");
        let TranscriptBlock::Answer(recap) = blocks.last().unwrap() else {
            panic!("state recap last");
        };
        assert!(recap
            .spans
            .iter()
            .any(|span| span.text.contains(&lane.state_recap)));

        // Footer hint while lane-focused (exact spec string).
        assert_eq!(app.ui.borrow().footer_context().as_str(), "lane_focus");
        assert_eq!(
            footer_right_text(&app.footer_state()),
            "esc back to parent · transcript is the subagent's own"
        );

        // Esc returns to the parent transcript with the exact notice.
        app.on_key("escape");
        assert!(app.ui.borrow().transcript.focused_lane().is_none());
        assert_eq!(
            app.ui.borrow().notices.current(),
            Some("back to parent session")
        );
        assert!(
            blocks_of(&app, "user_line").iter().any(|block| matches!(
                block,
                TranscriptBlock::UserLine(line) if line.text == AGENTS_PROMPT
            )),
            "parent transcript restored"
        );
    }

    // Adapts tests/test_flow_rewind.py::test_double_esc_interrupts_then_
    // opens_existing_rewind_picker: the first esc of a running turn only
    // requests the interrupt; the second esc backtracks into the rewind
    // picker on the existing checkpoint.
    #[test]
    fn test_double_esc_interrupts_then_opens_existing_rewind_picker() {
        let (mut app, ops) = test_app();
        // Turn 1 lands checkpoint t1.
        reach_approval(&mut app);
        app.on_key("enter");
        finish_granted_turn(&mut app);
        assert_eq!(app.reducer.ledger.checkpoints().len(), 1);

        app.handle_wire(prompt_submit("second turn"));
        assert!(app.ui.borrow().turn_active);
        app.on_key("escape");
        assert!(
            ops.borrow().contains(&"interrupt".to_string()),
            "first esc requested the interrupt"
        );
        assert!(!app.ui.borrow().rewind.display(), "no picker yet");
        app.on_key("escape");
        let ui = app.ui.borrow();
        assert!(ui.rewind.display(), "second esc opened the picker");
        assert_eq!(ui.rewind.current().map(|c| c.id.as_str()), Some("t1"));
    }

    // Adapts tests/test_flow_thinking_block.py::test_ctrl_g_toggles_
    // durable_thinking_block_in_place: the durable thinking block defaults
    // collapsed; ctrl-g expands/collapses it in place with the exact notices.
    #[test]
    fn test_ctrl_g_toggles_durable_thinking_block_in_place() {
        use amplifier_newtui_rs::model::blocks::{Thinking, TranscriptBlock};
        let (mut app, _ops) = test_app();
        let _ = app.ui.borrow_mut().transcript.append(
            Thinking {
                text: "weigh A\npick B".into(),
                ..Thinking::new("th-1")
            }
            .into(),
            0.0,
        );
        let expanded = |app: &App| match app.ui.borrow().transcript.get_block("th-1") {
            Some(TranscriptBlock::Thinking(thinking)) => thinking.expanded,
            _ => panic!("thinking block present"),
        };
        assert!(!expanded(&app), "default collapsed");

        app.on_key("ctrl+g");
        assert!(expanded(&app));
        assert_eq!(app.ui.borrow().notices.current(), Some("thinking · expanded"));

        app.on_key("ctrl+g");
        assert!(!expanded(&app));
        assert_eq!(app.ui.borrow().notices.current(), Some("thinking · collapsed"));
    }

    // Adapts tests/test_flow_thinking_block.py::test_ctrl_g_falls_back_to_
    // live_tail_without_durable_thinking: a withheld (empty-text) block is
    // not expandable, so ctrl-g still drives the live-tail reveal.
    #[test]
    fn test_ctrl_g_falls_back_to_live_tail_without_durable_thinking() {
        use amplifier_newtui_rs::model::blocks::{Thinking, TranscriptBlock};
        let (mut app, _ops) = test_app();
        let _ = app
            .ui
            .borrow_mut()
            .transcript
            .append(Thinking::new("th-2").into(), 0.0);
        assert!(!app.ui.borrow().live_tail.revealed());

        app.on_key("ctrl+g");
        assert!(app.ui.borrow().live_tail.revealed());
        assert_eq!(app.ui.borrow().notices.current(), Some("thinking · shown"));
        // The withheld block stays collapsed/untouched.
        match app.ui.borrow().transcript.get_block("th-2") {
            Some(TranscriptBlock::Thinking(thinking)) => assert!(!thinking.expanded),
            _ => panic!("thinking block present"),
        };
    }

    // Adapts tests/test_flow_plan_panel.py::test_plan_panel_lights_up_mid_
    // turn_and_collapses_when_done over the scripted build turn: the panel
    // lights up while the plan runs (▶ active step) and collapses to the
    // bare `Plan 3/3` header at close-out — with the footer never showing
    // the count twice (D2) and no live todo block in the transcript (D3).
    #[test]
    fn test_plan_panel_lights_up_mid_turn_and_collapses_when_done() {
        let (mut app, rx) = demo_seed_done();
        app.ui.borrow_mut().set_mode_by_id("chat", false);
        type_text(&mut app, "hi"); // → the build turn (next unplayed)
        app.on_key("enter");
        // The turn parks on the pytest approval: plan seeded, a step active.
        drain_demo(&mut app, &rx, |app| app.ui.borrow().approval.is_some());
        {
            let ui = app.ui.borrow();
            assert!(ui.plan_panel.display(), "panel lit up mid-turn");
            let lines = ui.plan_panel.plan_lines();
            assert!(lines[0].starts_with("Plan "), "count header: {lines:?}");
            assert!(
                lines.iter().any(|line| line.starts_with("  ▶ ")),
                "an active step renders ▶: {lines:?}"
            );
        }
        app.on_key("enter"); // Allow once → the turn runs to close-out
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 2 && !app.ui.borrow().turn_active
        });
        let ui = app.ui.borrow();
        assert!(ui.plan_panel.display(), "still visible when done");
        assert_eq!(ui.plan_panel.plan_lines(), vec!["Plan 3/3"]);
        drop(ui);
        assert!(
            !footer_left_text(&app.footer_state()).contains("Plan"),
            "D2: panel visible → footer never doubles the count"
        );
        assert!(blocks_of(&app, "todo").is_empty(), "D3: no live todo block");
    }

    // Adapts tests/test_flow_plan_panel.py::test_plan_panel_hides_below_90_
    // cols: the responsive ladder degrades to the footer count only.
    #[test]
    fn test_plan_panel_hides_below_90_cols() {
        let (mut app, rx) = demo_seed_done();
        app.on_resize(80, 40);
        app.ui.borrow_mut().set_mode_by_id("chat", false);
        type_text(&mut app, "hi");
        app.on_key("enter");
        drain_demo(&mut app, &rx, |app| app.ui.borrow().approval.is_some());
        assert!(!app.ui.borrow().plan_items.is_empty(), "plan landed");
        assert!(!app.ui.borrow().plan_panel.display(), "ladder: hidden below 90 cols");
        assert!(
            footer_left_text(&app.footer_state()).contains("Plan"),
            "count-only in the footer"
        );
        app.on_key("enter");
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 2 && !app.ui.borrow().turn_active
        });
        assert!(!app.ui.borrow().plan_panel.display());
        assert!(
            footer_left_text(&app.footer_state()).contains("Plan 3/3"),
            "final count in the footer"
        );
    }

    // Adapts tests/test_flow_palette.py::test_esc_with_zero_match_filter_
    // clears_filter_not_the_turn: a live zero-match slash filter consumes
    // the esc (strip hidden but filter live) — it never falls through to
    // interrupt-running; only the NEXT esc reaches the interrupt.
    #[test]
    fn test_esc_with_zero_match_filter_clears_filter_not_the_turn() {
        let (mut app, ops) = test_app();
        app.handle_wire(prompt_submit("first turn"));
        assert!(app.ui.borrow().turn_active);

        type_text(&mut app, "/zzz");
        {
            let ui = app.ui.borrow();
            assert!(!ui.palette.is_open(), "zero matches → strip hidden…");
            assert_eq!(ui.palette.filter_text(), Some("/zzz"), "…but the filter is live");
        }

        app.on_key("escape");
        {
            let ui = app.ui.borrow();
            assert_eq!(ui.palette.filter_text(), None, "esc cleared the filter");
            assert!(ui.turn_active, "the turn keeps running");
            assert_eq!(ui.composer.text(), "/zzz", "mockup: typed text stays");
        }
        assert!(
            !ops.borrow().contains(&"interrupt".to_string()),
            "first esc never reached interrupt"
        );

        app.on_key("escape");
        assert!(
            ops.borrow().contains(&"interrupt".to_string()),
            "only the NEXT esc interrupts"
        );
    }

    // ------------------------------------------------------------------
    // Full-frame render locks — the three unadapted SVG snapshots of
    // tests/test_ui_snapshots.py, as exact-content frame assertions.
    // ------------------------------------------------------------------

    // Adapts tests/test_ui_snapshots.py::test_double_esc_rewind_snapshot:
    // the stable rewind-open screen — the strip renders the exact
    // `rewind_line` text for the newest checkpoint.
    #[test]
    fn test_double_esc_rewind_snapshot() {
        use amplifier_newtui_rs::ui::rewind_strip::rewind_line;
        let (mut app, _ops) = test_app();
        reach_approval(&mut app);
        app.on_key("enter");
        finish_granted_turn(&mut app);
        app.handle_wire(prompt_submit("second turn"));
        app.on_key("escape"); // interrupt request
        app.on_key("escape"); // backtrack → rewind picker
        assert!(app.ui.borrow().rewind.display());

        let expected = {
            let ui = app.ui.borrow();
            let checkpoint = ui.rewind.current().expect("newest checkpoint").clone();
            rewind_line(&checkpoint)
        };
        let text = draw_text(&app, 140, 32);
        let flattened = text.split_whitespace().collect::<Vec<_>>().join(" ");
        assert!(
            flattened.contains(&expected.split_whitespace().collect::<Vec<_>>().join(" ")),
            "exact rewind strip line {expected:?} in frame:\n{text}"
        );
    }

    // Adapts tests/test_ui_snapshots.py::test_plan_panel_bottom_strip_
    // snapshot: the post-build-turn bottom strip — plan collapsed to
    // `Plan 3/3`, still visible in the frame.
    #[test]
    fn test_plan_panel_bottom_strip_snapshot() {
        let (mut app, rx) = demo_seed_done();
        app.ui.borrow_mut().set_mode_by_id("chat", false);
        type_text(&mut app, "hi");
        app.on_key("enter");
        drain_demo(&mut app, &rx, |app| app.ui.borrow().approval.is_some());
        app.on_key("enter");
        drain_demo(&mut app, &rx, |app| {
            app.reducer.ledger.checkpoints().len() == 2 && !app.ui.borrow().turn_active
        });
        assert_eq!(app.ui.borrow().plan_panel.plan_lines(), vec!["Plan 3/3"]);
        let text = draw_text(&app, 120, 40);
        assert!(text.contains("Plan 3/3"), "collapsed plan strip in frame:\n{text}");
    }

    // Adapts tests/test_ui_snapshots.py::test_lane_tail_snapshot: the dim
    // ┆-guttered lane tail rendering (last 3 non-blank lines).
    #[test]
    fn test_lane_tail_snapshot() {
        let (mut app, _ops) = test_app();
        // A lane exists so the panel has a row to hang the tail under.
        app.handle_wire(prompt_submit("fan out"));
        app.handle_wire(wire(ev::UIEvent::AgentSpawned(ev::AgentSpawned {
            session_id: SESSION.into(),
            ts: 101.0,
            agent: "researcher".into(),
            sub_session_id: "s1".into(),
            parent_session_id: SESSION.into(),
            ..ev::AgentSpawned::default()
        })));
        assert!(app.ui.borrow().lanes_panel.display(), "panel auto-opened");
        app.ui.borrow_mut().lanes_panel.show_lane_tail(
            "…the queue bridge normalizes delegate lifecycle events at a single\n\
             boundary, so the lanes are fed from the same UIEvent union as the\n\
             transcript — checking trackers/task_status.py next",
        );
        let text = draw_text(&app, 90, 24);
        assert!(text.contains('┆'), "dim gutter glyph in frame:\n{text}");
        assert!(
            text.contains("checking trackers/task_status.py next"),
            "tail text in frame:\n{text}"
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
