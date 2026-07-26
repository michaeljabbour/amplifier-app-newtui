//! Composition-root helpers kept out of the app assembly (Python
//! `ui/app_support.py`).
//!
//! Pure-ish functions the app delegates to: keymap-sourced global bindings,
//! block builders for the needs-you list and the `/permissions` surface,
//! transcript trimming after a confirmed fork, esc-chain resolution and the
//! plan panel's responsive ladder. Everything here operates on values the
//! caller passes in — no hidden state.
//!
//! Ratatui adaptation: the Python module also carries the app-driving
//! orchestration (`announce_ready`, `mount_approval`, `confirm_fork`, …)
//! that mounts Textual widgets and spawns workers. Those do not port —
//! their PURE cores do: [`resolve_esc`] is `handle_esc`'s chain decision,
//! [`plan_surface`] is `sync_plan_surfaces`' ladder decision, and the
//! notice-string constants keep the exact user-facing text for the app
//! assembly to emit.

use crate::commands::permissions::PermissionSurface;
use crate::model::blocks::{
    Answer, BlockIdAllocator, NeedsYouBlock, NeedsYouChoice, NeedsYouEntry, Segment, StyleToken,
    TodoItem, TranscriptBlock,
};
use crate::model::queues::NeedsYouItem;
use crate::ui::keymap::{self, Context};
use crate::ui::notifications::{self, Environ, Reason};
use crate::ui::plan_panel::{plan_counts, plan_panel_width};
use crate::ui::transcript::TranscriptView;

pub const STEER_NOTICE: &str = "steer queued · shift+enter queues a full next-turn message";
pub const STEER_NOTICE_LEGACY: &str = "steer queued · alt+enter queues a full next-turn message";
pub const STEER_DISCARDED_NOTICE: &str = "steer not applied · discarded at turn end";
pub const QUEUED_NOTICE: &str = "message queued · runs as the next turn";
pub const APPROVAL_NOTICE: &str = "approval required · choose below the transcript";
/// Approval notices linger 6s, not the 4s default (mockup requestApproval).
pub const APPROVAL_NOTICE_DURATION: f64 = 6.0;

/// Actions from the keymap table that become app-level global bindings.
const GLOBAL_ACTIONS: [&str; 8] = [
    "cycle_mode",
    "cycle_permission",
    "cycle_tail",
    "toggle_lanes",
    "toggle_thinking",
    "show_ledger",
    "show_needs_you",
    "open_rewind",
];

/// Turn-end attention threshold (re-exported from `ui::notifications`, the
/// single source of the ladder policy): a turn shorter than this is a live
/// exchange (the user is watching); a longer one plausibly lost their
/// attention, so its close-out notifies. Deferred decisions always notify —
/// they block on the human by definition.
pub use crate::ui::notifications::ATTENTION_MIN_TURN_SECONDS;

/// Whether the attention ladder should fire at all for `reason`.
///
/// This predicate is the ladder's floor — the `bell` rung — kept as a named
/// seam because the app and tests read it directly. True when a decision was
/// deferred (always) or a turn finished after
/// [`ATTENTION_MIN_TURN_SECONDS`]; `AMPLIFIER_NOTIFY=false/0/no/off`
/// disables it entirely.
pub fn attention_bell_needed(reason: Reason, elapsed_s: f64, environ: Environ<'_>) -> bool {
    notifications::attention_needed(reason, elapsed_s, environ)
}

/// The small state machine behind interrupt-then-backtrack.
///
/// Only an Esc that actually targets a running turn arms the sequence.
/// Panel-close and approval Esc presses therefore cannot accidentally open
/// rewind. The second press may land before or just after turn close-out.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct EscSequence {
    pub interrupted_at: Option<f64>,
}

impl EscSequence {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn arm_interrupt(&mut self, now: f64) {
        self.interrupted_at = Some(now);
    }

    pub fn consume_backtrack(&mut self, now: f64) -> bool {
        let interrupted_at = self.interrupted_at.take();
        match interrupted_at {
            Some(at) => {
                let elapsed = now - at;
                (0.0..=keymap::ESC_BACKTRACK_WINDOW_SECONDS).contains(&elapsed)
            }
            None => false,
        }
    }

    pub fn reset(&mut self) {
        self.interrupted_at = None;
    }
}

/// One app-level global key binding (the pure data of a Textual `Binding`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct GlobalBinding {
    pub key: &'static str,
    pub action: &'static str,
    pub label: &'static str,
    pub show: bool,
    pub priority: bool,
}

/// App bindings sourced from the keymap table (single source, NOTES #7).
pub fn global_bindings() -> Vec<GlobalBinding> {
    let mut bindings: Vec<GlobalBinding> = keymap::KEYMAP
        .iter()
        .filter(|binding| GLOBAL_ACTIONS.contains(&binding.action))
        .flat_map(|binding| {
            binding.keys.iter().map(|key| GlobalBinding {
                key,
                action: binding.action,
                label: binding.label,
                show: false,
                priority: true,
            })
        })
        .collect();
    bindings.push(GlobalBinding {
        key: "up",
        action: "palette_up",
        label: "↑",
        show: false,
        priority: true,
    });
    bindings.push(GlobalBinding {
        key: "down",
        action: "palette_down",
        label: "↓",
        show: false,
        priority: true,
    });
    bindings.push(GlobalBinding {
        key: "escape",
        action: "app_esc",
        label: "esc",
        show: false,
        priority: false,
    });
    // amplifier-app-cli parity: Ctrl-D exits (its banner advertises it).
    // Textual's stock ctrl+q quit binding stays too.
    bindings.push(GlobalBinding {
        key: "ctrl+d",
        action: "quit",
        label: "quit",
        show: false,
        priority: true,
    });
    // Copy whichever selection exists (composer text or transcript drag).
    // Priority: TextArea's own ctrl+c binding otherwise swallows the key
    // while the composer has focus — transcript copies silently no-oped.
    bindings.push(GlobalBinding {
        key: "ctrl+c,super+c",
        action: "copy_selection",
        label: "copy",
        show: false,
        priority: true,
    });
    bindings
}

/// The `Needs you` transcript block for the pending decisions (§7).
pub fn needs_you_block(
    pending: &[NeedsYouItem],
    allocator: &mut BlockIdAllocator,
) -> Option<NeedsYouBlock> {
    if pending.is_empty() {
        return None;
    }
    let entries = pending
        .iter()
        .map(|item| NeedsYouEntry {
            decision_id: item.decision_id.clone(),
            question: item.question.clone(),
            reason: item.reason.clone(),
            choices: item
                .choices
                .iter()
                .map(|c| NeedsYouChoice::new(c.clone(), c.clone()))
                .collect(),
            highlight: item.highlight.clone(),
        })
        .collect();
    Some(NeedsYouBlock::new(allocator.next_id(), entries))
}

fn seg(text: String, style_token: StyleToken) -> Segment {
    Segment {
        style_token,
        ..Segment::new(text)
    }
}

/// The `/permissions` trust-slot print as an Answer block.
pub fn permissions_block(
    surface: &PermissionSurface,
    trust_str: &str,
    allocator: &mut BlockIdAllocator,
) -> Answer {
    let snapshot = surface.snapshot();
    let mut spans: Vec<Segment> = vec![
        seg("· ".to_string(), StyleToken::Blue),
        Segment {
            style_token: StyleToken::Bright,
            bold: true,
            ..Segment::new("Permissions")
        },
        seg(format!("  {trust_str}\n"), StyleToken::Dim),
        seg(
            "  path policy · allowed roots + protected paths enforced\n".to_string(),
            StyleToken::Dim,
        ),
    ];
    spans.extend(
        surface
            .slots()
            .iter()
            .map(|slot| seg(format!("  {}\n", slot.label()), StyleToken::Fg)),
    );
    if !surface.exceptions().is_empty() {
        spans.push(seg(
            format!("  always allowed: {}\n", surface.exceptions().join(" · ")),
            StyleToken::Dim,
        ));
    }
    if !surface.blocks().is_empty() {
        spans.push(seg(
            format!("  blocked: {}\n", surface.blocks().join(" · ")),
            StyleToken::Dim,
        ));
    }
    spans.push(seg(
        format!("  boundary: {}", snapshot.boundary),
        StyleToken::Dim,
    ));
    Answer::new(allocator.next_id(), spans)
}

/// Drop every block after the turn rule stamped `checkpoint_id`.
///
/// Runs only AFTER the fork is confirmed (confirm-then-trim, ADR-0007).
pub fn trim_after_checkpoint(view: &mut TranscriptView, checkpoint_id: &str) {
    let ids = view.block_ids();
    let mut cut: Option<usize> = None;
    for (index, block_id) in ids.iter().enumerate() {
        if let Some(TranscriptBlock::TurnRule(rule)) = view.get_block(block_id) {
            if rule.checkpoint_id == checkpoint_id {
                cut = Some(index);
            }
        }
    }
    let Some(cut) = cut else {
        return;
    };
    for block_id in &ids[cut + 1..] {
        let _ = view.remove_block(block_id);
    }
}

/// Platform clipboard commands in preference order.
fn os_clipboard_commands() -> Vec<Vec<&'static str>> {
    if cfg!(target_os = "macos") {
        vec![vec!["pbcopy"]]
    } else {
        vec![
            vec!["wl-copy"],
            vec!["xclip", "-selection", "clipboard"],
            vec!["xsel", "-ib"],
        ]
    }
}

/// `shutil.which`-style lookup: is `program` an executable file on PATH?
fn which(program: &str) -> bool {
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|dir| {
        let candidate = dir.join(program);
        let Ok(metadata) = std::fs::metadata(&candidate) else {
            return false;
        };
        if !metadata.is_file() {
            return false;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            metadata.permissions().mode() & 0o111 != 0
        }
        #[cfg(not(unix))]
        {
            true
        }
    })
}

/// Whether a native clipboard writer is available without running it.
pub fn os_clipboard_available() -> bool {
    os_clipboard_commands()
        .iter()
        .any(|command| which(command[0]))
}

/// Write `text` to the OS clipboard via the platform tool, if any.
///
/// OSC 52 alone is not enough: iTerm2 ships with terminal clipboard writes
/// disabled, so copies silently vanished (user report). A local TUI can just
/// use pbcopy / wl-copy / xclip directly. Returns true when a tool accepted
/// the text; never panics.
pub fn os_clipboard_copy(text: &str) -> bool {
    use std::io::Write;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    for command in os_clipboard_commands() {
        if !which(command[0]) {
            continue;
        }
        let attempt = (|| -> std::io::Result<bool> {
            let mut child = Command::new(command[0])
                .args(&command[1..])
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()?;
            if let Some(mut stdin) = child.stdin.take() {
                stdin.write_all(text.as_bytes())?;
            } // drop closes the pipe
            // Python `subprocess.run(..., timeout=5, check=True)`.
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                if let Some(status) = child.try_wait()? {
                    return Ok(status.success());
                }
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Ok(false);
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        })();
        if let Ok(true) = attempt {
            return true;
        }
    }
    false
}

/// Python `str(value)` for the JSON values a mode-catalog entry carries.
fn value_text(value: Option<&serde_json::Value>) -> String {
    match value {
        None => String::new(),
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

/// Render the mode tool's catalog output grouped by source bundle.
///
/// The mounted mode tool reports `{"modes": [{name, description, source},
/// …]}` — dynamically composed (superpowers, modes, llm-wiki, …), so this
/// formats whatever arrives rather than any fixed list. Non-mapping payloads
/// fall back to plain text. Names in `active` are marked with a `◆` so
/// `/modes` shows the currently-active set.
///
/// (Python defaults: `term_width=120`, `active=()`.)
pub fn native_modes_segments(
    catalog: &serde_json::Value,
    term_width: usize,
    active: &[&str],
) -> Vec<Segment> {
    let mut modes: Vec<&serde_json::Map<String, serde_json::Value>> = Vec::new();
    if let serde_json::Value::Object(map) = catalog {
        if let Some(serde_json::Value::Array(raw)) = map.get("modes") {
            modes = raw.iter().filter_map(|m| m.as_object()).collect();
        }
    }
    if modes.is_empty() {
        let text = match catalog {
            serde_json::Value::String(s) => s.trim().to_string(),
            other => other.to_string().trim().to_string(),
        };
        if text.is_empty() {
            return Vec::new();
        }
        return vec![seg(format!("  {text}\n"), StyleToken::Dim)];
    }
    let mut by_source: std::collections::BTreeMap<
        String,
        Vec<&serde_json::Map<String, serde_json::Value>>,
    > = std::collections::BTreeMap::new();
    for mode in &modes {
        by_source
            .entry(value_text(mode.get("source")))
            .or_default()
            .push(mode);
    }
    let mut segments: Vec<Segment> = Vec::new();
    let name_w = modes
        .iter()
        .map(|m| value_text(m.get("name")).chars().count())
        .max()
        .unwrap_or(0);
    // Fill the terminal width instead of a fixed 90-col cap: indent(4) + name
    // column + 2-space gap leaves this for the description on one line.
    let desc_budget = (term_width as isize - 4 - name_w as isize - 2).max(24) as usize;
    for (source, group) in &by_source {
        let heading = if source.is_empty() { "bundle" } else { source };
        segments.push(seg(format!("  {heading}\n"), StyleToken::Dimmer));
        let mut sorted = group.clone();
        sorted.sort_by_key(|m| value_text(m.get("name")));
        for mode in sorted {
            let name = value_text(mode.get("name"));
            let full_desc = value_text(mode.get("description"));
            let mut desc = full_desc.split('\n').next().unwrap_or("").to_string();
            if desc.chars().count() > desc_budget {
                desc = desc.chars().take(desc_budget - 1).collect::<String>() + "…";
            }
            let marker = if active.contains(&name.as_str()) {
                "◆ "
            } else {
                "  "
            };
            segments.push(seg(
                format!("  {marker}{name:<name_w$}  "),
                StyleToken::Teal,
            ));
            segments.push(seg(format!("{desc}\n"), StyleToken::Dim));
        }
    }
    segments.push(seg(
        "  /mode <name> activates · /mode off clears".to_string(),
        StyleToken::Dimmer,
    ));
    segments
}

/// Which esc-consuming contexts are currently active (Python `handle_esc`'s
/// `checks` dict, evaluated by the caller against live app state).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct EscFlags {
    /// A subagent lane is focused.
    pub lane_focus: bool,
    /// Mockup Escape: `if (this.palFilter !== null)` — ANY live slash
    /// filter consumes the Esc, even a zero-match one whose strip is
    /// hidden, so typed "/…" text never falls through to interrupt.
    pub palette: bool,
    /// Rewind strip displayed.
    pub rewind: bool,
    /// Lanes panel displayed.
    pub lanes: bool,
    /// A turn is running.
    pub running: bool,
}

/// What the app must do for this Esc press (Python `handle_esc`'s
/// `actions` dict, returned instead of called).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EscAction {
    LaneUnfocus,
    ClosePalette,
    CloseRewind,
    CloseLanes,
    InterruptRunning,
    OpenRewind,
}

/// Resolve Esc priority plus interrupt-then-backtrack (spec §5).
///
/// The pure core of Python `handle_esc`: the caller evaluates the live
/// context into [`EscFlags`], passes the app's [`EscSequence`], and performs
/// the returned action. `None` means the press had no target.
pub fn resolve_esc(flags: EscFlags, sequence: &mut EscSequence, now: f64) -> Option<EscAction> {
    for (context, action) in keymap::ESC_CHAIN.iter() {
        let active = match context {
            Context::LaneFocus => flags.lane_focus,
            Context::Palette => flags.palette,
            Context::Rewind => flags.rewind,
            Context::Lanes => flags.lanes,
            Context::Running => flags.running,
            _ => false,
        };
        if !active {
            continue;
        }
        if *action == "interrupt_running" {
            if sequence.consume_backtrack(now) {
                return Some(EscAction::OpenRewind);
            }
            sequence.arm_interrupt(now);
            return Some(EscAction::InterruptRunning);
        }
        sequence.reset();
        return Some(match *action {
            "lane_unfocus" => EscAction::LaneUnfocus,
            "close_palette" => EscAction::ClosePalette,
            "close_rewind" => EscAction::CloseRewind,
            "close_lanes" => EscAction::CloseLanes,
            other => unreachable!("unknown esc action {other}"),
        });
    }
    if sequence.consume_backtrack(now) {
        return Some(EscAction::OpenRewind);
    }
    None
}

/// Below this terminal width the plan panel yields; a `Plan N/M` count
/// falls back to the footer (design D2 responsive ladder).
pub const PLAN_PANEL_MIN_WIDTH: usize = 90;

/// The plan's responsive-ladder decision (design D2).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PlanSurface {
    /// Show the bottom-strip panel at this content-fitted width.
    Panel { width: usize },
    /// Hide the panel; the footer carries the count (Task 5).
    Hidden,
}

/// One decision point for the plan's responsive ladder (D2) — the pure
/// core of Python `sync_plan_surfaces`.
///
/// Wide (≥ 90 cols) with todos → the bottom-strip panel at a
/// content-fitted width (37 floor, one-third cap); otherwise the panel
/// hides and the footer carries the count. Called on every plan change
/// and on terminal resize.
pub fn plan_surface(items: &[TodoItem], term_width: usize) -> PlanSurface {
    if !items.is_empty() && term_width >= PLAN_PANEL_MIN_WIDTH {
        PlanSurface::Panel {
            width: plan_panel_width(items, term_width),
        }
    } else {
        PlanSurface::Hidden
    }
}

/// `(done, total)` for the footer — `(0, 0)` unless the panel is hidden
/// while todos exist (the count never shows twice; design D2).
pub fn plan_footer_counts(items: &[TodoItem], panel_displayed: bool) -> (usize, usize) {
    if items.is_empty() || panel_displayed {
        return (0, 0);
    }
    plan_counts(items)
}

#[cfg(test)]
mod tests {
    //! Pins `tests/test_ui_app_support.py` plus the two app_support-adjacent
    //! cases deferred by the reducer port
    //! (`tests/test_ui_reducer_outcomes.py`).

    use super::*;
    use crate::ui::keymap::ESC_BACKTRACK_WINDOW_SECONDS;
    use serde_json::json;
    use std::collections::HashMap;

    fn joined(segments: &[Segment]) -> String {
        segments.iter().map(|s| s.text.as_str()).collect()
    }

    #[test]
    fn test_native_modes_use_full_terminal_width() {
        let long_desc =
            "Amplifier-way conformance audit of the working repo across every module today";
        let catalog = json!({
            "modes": [{"name": "audit", "description": long_desc, "source": "conformance"}]
        });
        let text = joined(&native_modes_segments(&catalog, 200, &[]));
        let wide = text
            .lines()
            .find(|line| line.contains("Amplifier-way"))
            .unwrap();
        let narrow_text = joined(&native_modes_segments(&catalog, 60, &[]));
        let narrow = narrow_text
            .lines()
            .find(|line| line.contains("Amplifier-way"))
            .unwrap();
        // Wider terminal → longer description line (no fixed 90-col cap), and
        // the narrow render truncates with an ellipsis to fit.
        assert!(wide.chars().count() > narrow.chars().count());
        assert!(narrow.contains('…') && !wide.contains('…'));
    }

    #[test]
    fn test_native_modes_mark_the_active_set() {
        let catalog = json!({
            "modes": [
                {"name": "audit", "description": "conformance audit", "source": "conformance"},
                {"name": "careful", "description": "extra caution", "source": "modes"},
            ]
        });
        let text = joined(&native_modes_segments(&catalog, 120, &["audit"]));
        let audit_line = text.lines().find(|line| line.contains("audit")).unwrap();
        let careful_line = text.lines().find(|line| line.contains("careful")).unwrap();
        assert!(audit_line.contains('◆')); // active mode is marked
        assert!(!careful_line.contains('◆')); // inactive mode is not
    }

    /// Segment-exact oracle parity (captured from the live Python module,
    /// 2026-07-26): headings, ljust padding, markers and style tokens.
    #[test]
    fn native_modes_segments_match_python_oracle_exactly() {
        let catalog = json!({
            "modes": [
                {"name": "audit", "description": "conformance audit", "source": "conformance"},
                {"name": "careful", "description": "extra caution", "source": "modes"},
            ]
        });
        let got: Vec<(String, &str)> = native_modes_segments(&catalog, 120, &["audit"])
            .into_iter()
            .map(|s| (s.text, s.style_token.as_str()))
            .collect();
        let expected = [
            ("  conformance\n", "dimmer"),
            ("  ◆ audit    ", "teal"),
            ("conformance audit\n", "dim"),
            ("  modes\n", "dimmer"),
            ("    careful  ", "teal"),
            ("extra caution\n", "dim"),
            ("  /mode <name> activates · /mode off clears", "dimmer"),
        ];
        assert_eq!(
            got,
            expected
                .iter()
                .map(|(t, s)| (t.to_string(), *s))
                .collect::<Vec<_>>()
        );
        // Narrow truncation, exact text (Python term_width=60):
        let long = "Amplifier-way conformance audit of the working repo across every module today";
        let narrow = json!({
            "modes": [{"name": "audit", "description": long, "source": "conformance"}]
        });
        let texts: Vec<String> = native_modes_segments(&narrow, 60, &[])
            .into_iter()
            .map(|s| s.text)
            .collect();
        assert_eq!(
            texts[2],
            "Amplifier-way conformance audit of the working r…\n"
        );
        // Non-mapping payload falls back to plain text.
        let plain = native_modes_segments(&json!("plain text"), 120, &[]);
        assert_eq!(plain[0].text, "  plain text\n");
    }

    #[test]
    fn test_esc_sequence_accepts_the_boundary_once() {
        let mut sequence = EscSequence::new();
        sequence.arm_interrupt(10.0);
        assert!(sequence.consume_backtrack(10.0 + ESC_BACKTRACK_WINDOW_SECONDS));
        assert!(!sequence.consume_backtrack(10.1));
    }

    #[test]
    fn test_esc_sequence_expires_and_clears() {
        let mut sequence = EscSequence::new();
        sequence.arm_interrupt(10.0);
        assert!(!sequence.consume_backtrack(10.0 + ESC_BACKTRACK_WINDOW_SECONDS + 0.001));
        assert_eq!(sequence.interrupted_at, None);
    }

    // -- attention bell (hook-output adapter for the suppressed hooks-notify)

    /// A deferred decision always needs the human — elapsed is irrelevant.
    #[test]
    fn test_attention_bell_rings_when_a_decision_is_deferred() {
        let environ = HashMap::new();
        assert!(attention_bell_needed(
            Reason::DecisionDeferred,
            0.0,
            Some(&environ)
        ));
    }

    /// Turn end rings only when the turn ran long enough that the user has
    /// plausibly looked away; quick exchanges stay silent.
    #[test]
    fn test_attention_bell_rings_only_after_long_turns() {
        let environ = HashMap::new();
        assert!(!attention_bell_needed(
            Reason::TurnFinished,
            0.0,
            Some(&environ)
        ));
        assert!(!attention_bell_needed(
            Reason::TurnFinished,
            ATTENTION_MIN_TURN_SECONDS - 0.1,
            Some(&environ)
        ));
        assert!(attention_bell_needed(
            Reason::TurnFinished,
            ATTENTION_MIN_TURN_SECONDS,
            Some(&environ)
        ));
    }

    /// `AMPLIFIER_NOTIFY=false/0/no/off` disables the bell — same kill
    /// switch the suppressed hooks-notify module honored.
    #[test]
    fn test_attention_bell_honors_amplifier_notify_env() {
        for value in ["false", "0", "no", "off", "FALSE", "Off"] {
            let environ: HashMap<String, String> =
                HashMap::from([("AMPLIFIER_NOTIFY".to_string(), value.to_string())]);
            assert!(!attention_bell_needed(
                Reason::DecisionDeferred,
                0.0,
                Some(&environ)
            ));
            assert!(!attention_bell_needed(
                Reason::TurnFinished,
                999.0,
                Some(&environ)
            ));
        }
        let environ: HashMap<String, String> =
            HashMap::from([("AMPLIFIER_NOTIFY".to_string(), "true".to_string())]);
        assert!(attention_bell_needed(
            Reason::DecisionDeferred,
            0.0,
            Some(&environ)
        ));
    }

    /// Regression: /permissions once rendered `<bound method TrustSlot.label …>`
    /// because `slot.label` was never called (found live in forge, 2026-07-16).
    /// (Pins `test_ui_reducer_outcomes.py::
    /// test_permissions_block_renders_slot_labels_not_bound_methods`.)
    #[test]
    fn test_permissions_block_renders_slot_labels_not_bound_methods() {
        let mut surface = PermissionSurface::with_mode("auto");
        surface.add_exception("uv run pytest").unwrap();
        let block = permissions_block(
            &surface,
            "auto read,write · asks if risky",
            &mut BlockIdAllocator::new(),
        );
        let text: String = block.spans.iter().map(|s| s.text.as_str()).collect();
        assert!(!text.contains("bound method"));
        assert!(text.contains("path policy · allowed roots + protected paths enforced"));
        assert!(!text.contains("execution confinement"));
        assert!(text.contains("read · allow"));
        assert!(text.contains("always allowed: uv run pytest"));
        assert!(text.contains("boundary: within project"));
    }

    /// /improve with no evidence must say so, not print a bare header.
    /// (Pins `test_ui_reducer_outcomes.py::
    /// test_improve_block_empty_state_renders_placeholder_row`.)
    #[test]
    fn test_improve_block_empty_state_renders_placeholder_row() {
        use crate::commands::improve::build_improve_block;
        use crate::ui::transcript_render::render_block;

        let block = build_improve_block("b1", Vec::new());
        let lines = render_block(&TranscriptBlock::from(block), 120);
        assert_eq!(lines.len(), 2);
        let row: String = lines[1].iter().map(|s| s.text.as_str()).collect();
        assert!(row.contains("no proposals yet"));
    }
}
