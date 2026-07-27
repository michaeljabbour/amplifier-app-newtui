//! Demo glue: scripted demo data → the reducer's lookup seams.
//!
//! Port of `src/amplifier_app_newtui/ui/demo_wiring.py` (the pure logic)
//! plus the minimal slice of `kernel/demo.py` it consumes — the exported
//! mockup data (`DEMO_LANES`, `DEMO_EVIDENCE`, `DEMO_DEFERRED_DECISION`,
//! `DEMO_TURNS`) is inlined here because `kernel/demo.rs` (the event-
//! producing `DemoRuntime`) is not ported; the legacy scripted demo in
//! `src/runtime.rs` stays as-is.
//!
//! What ports: the panel-line → [`LaneSeed`] parser, the focused-lane
//! transcript builder, the evidence links, the deferred-decision data
//! hooks, and [`DemoWiring`] — the pure state machine behind Python's
//! `DemoRuntimeAdapter` data hooks (`turn_spec` / `lane_seed` /
//! `lane_blocks` / `evidence_links` / `deferred_decision` /
//! `decision_narration` plus the prompt→turn-key bookkeeping of
//! `submit`). These feed `ReducerOptions::{spec_lookup, lane_seed_lookup,
//! evidence_lookup}` via [`DemoTurnSpec::reducer_spec`].
//!
//! What does NOT port (Textual/asyncio app-assembly mechanics, wired at
//! app-assembly time instead):
//! - `DemoRuntimeAdapter.start/submit/submit_queued/interrupt` — they
//!   drive the asyncio `DemoRuntime` on the adapter's event queue. The
//!   event-producing turn scripts themselves ARE ported: see
//!   [`crate::runtime::DemoScript`] (`kernel/demo.py`'s `DemoRuntime`).
//! - the approval future plumbing (`_approve`/`answer_approval` ticket
//!   futures) — only the pure `Deny → build-denied close-out` flag is
//!   kept ([`DemoWiring::record_approval_choice`]).
//! - `_consume_steer` / `_current_mode` (steering + live-mode hooks) and
//!   the `/config` snapshot (`default_config_state`).
//!
//! The esc-interrupt close-out ports here as [`interrupted_spec`]; the
//! playing runtime records the live close-out on
//! [`crate::runtime::DemoScript::interrupted_close`] and the adapter
//! bridges it into [`DemoWiring::set_interrupted_close`] (Python
//! `turn_spec` reads `self._runtime.interrupted_close`).

use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

use regex::Regex;
use rust_decimal::Decimal;

use crate::model::blocks::{
    Answer, BlockIdAllocator, LiveCommand, Narration, Segment, SessionBanner, StyleToken, ToolLine,
    ToolLineStatus, TranscriptBlock, UserLine,
};
use crate::model::evidence::EvidenceLink;
use crate::model::formatting::format_tokens_k;
use crate::model::lanes::LaneStateName;
use crate::ui::live_tail::answer_spans;
use crate::ui::needs_you::focused_lane_banner;
use crate::ui::reducer::{LaneSeed, TurnSpec};

// --------------------------------------------------------------------------
// Session identity (mockup verbatim — kernel/demo.py)
// --------------------------------------------------------------------------

pub const DEMO_SESSION_ID: &str = "e07de0";
pub const DEMO_SESSION_SHORT: &str = "e07d";
pub const DEMO_BUNDLE: &str = "anchors";
pub const DEMO_PROVIDER: &str = "OpenAI";
pub const DEMO_MODEL: &str = "gpt-5.5";
pub const DEMO_BANNER: (&str, &str) = (
    "Amplifier 2026.07.13-87b93ef* · core 1.6.0",
    "Bundle: anchors | Provider: OpenAI | gpt-5.5 · session e07de0",
);

/// Session spend at mount time (mockup `this.cost = 0.57`); the seed
/// turn's $0.17 is already baked into it.
pub fn demo_session_cost_start() -> Decimal {
    Decimal::new(57, 2)
}

/// Persistent cached prefix (system prompt + memory files + tool defs)
/// every demo provider call reads back — carried as `cache_read` on each
/// usage event so `/context` shows the mockup's populated memory bucket
/// (Python `DEMO_MEMORY_TOKENS`).
pub const DEMO_MEMORY_TOKENS: i64 = 8_000;

/// The verbatim broker options (Python `APPROVAL_OPTIONS`).
pub const APPROVAL_OPTIONS: [&str; 3] = ["Allow once", "Allow always", "Deny"];

/// Python `TurnKey = Literal["seed", "build", "auto", "plan", "brainstorm",
/// "agents"]`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum TurnKey {
    Seed,
    Build,
    Auto,
    Plan,
    Brainstorm,
    Agents,
}

impl TurnKey {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            TurnKey::Seed => "seed",
            TurnKey::Build => "build",
            TurnKey::Auto => "auto",
            TurnKey::Plan => "plan",
            TurnKey::Brainstorm => "brainstorm",
            TurnKey::Agents => "agents",
        }
    }
}

/// Python `demo_wiring._TURN_ORDER` — the advance order for prompts that
/// match no scripted turn verbatim.
const TURN_ORDER: [TurnKey; 5] = [
    TurnKey::Build,
    TurnKey::Auto,
    TurnKey::Plan,
    TurnKey::Brainstorm,
    TurnKey::Agents,
];

// --------------------------------------------------------------------------
// Token / cost / label formulas (kernel/demo.py, mockup verbatim)
// --------------------------------------------------------------------------

/// The store turns' per-second draws from `random.Random("amplifier-demo:build")`.
///
/// Python derives these from a seeded Mersenne Twister
/// (`380 + rng.randrange(260)`); the sequence is version-stable and pinned
/// verbatim by `test_tick_tokens_deterministic_and_pinned`, so the pinned
/// draws are inlined here rather than reimplementing CPython seeding.
const BUILD_TICKS: [u64; 9] = [608, 439, 557, 425, 415, 450, 463, 470, 636];
/// `random.Random("amplifier-demo:auto")` draws — see [`BUILD_TICKS`].
const AUTO_TICKS: [u64; 9] = [411, 538, 606, 443, 416, 475, 455, 496, 541];
/// Mockup: the agents turn ticks a flat 900 tokens/s for 6 seconds.
const AGENTS_TICK: u64 = 900;
const AGENTS_TICK_COUNT: usize = 6;

/// Per-second output-token deltas for a ticking turn.
///
/// Mockup formulas: `380 + floor(random() * 260)` for the store turns,
/// flat `900` for the agents turn (`kernel.demo.tick_tokens`).
///
/// # Panics
/// Panics for keys without a scripted tick schedule, or when `count`
/// exceeds the pinned store-turn draws (Python would draw further from
/// the seeded RNG; nothing in the script ever does).
pub fn tick_tokens(key: TurnKey, count: Option<usize>) -> Vec<u64> {
    match key {
        TurnKey::Agents => vec![AGENTS_TICK; count.unwrap_or(AGENTS_TICK_COUNT)],
        TurnKey::Build | TurnKey::Auto => {
            let draws: &[u64] = if key == TurnKey::Build {
                &BUILD_TICKS
            } else {
                &AUTO_TICKS
            };
            let n = count.unwrap_or(draws.len());
            assert!(
                n <= draws.len(),
                "tick_tokens: only {} draws are pinned for {:?}",
                draws.len(),
                key
            );
            draws[..n].to_vec()
        }
        other => panic!("tick_tokens: no scripted tick schedule for {other:?}"),
    }
}

/// Mockup: `turnCost = 0.04 + secs * 0.01`.
pub fn store_turn_cost(secs: i64) -> Decimal {
    Decimal::new(4, 2) + Decimal::from(secs) * Decimal::new(1, 2)
}

/// Turn-rule label: `<Ns> · <X.Xk> tok[, NN% cached] · $<cost> · <outcome>`.
pub fn rule_label(
    secs_text: &str,
    tokens: u64,
    cached_pct: Option<u8>,
    cost: Decimal,
    outcome: &str,
) -> String {
    let mut token_part = format!("{} tok", format_tokens_k(tokens));
    if let Some(pct) = cached_pct {
        token_part.push_str(&format!(", {pct}% cached"));
    }
    // Python `f"${cost:.2f}"` rounds half-even; rust_decimal's `{:.2}`
    // truncates excess scale, so round first (banker's — same strategy).
    format!(
        "{secs_text} · {token_part} · ${:.2} · {outcome}",
        cost.round_dp(2)
    )
}

/// Mockup final-answer assembly for the build turn (`kernel.demo.build_answer`).
pub fn build_answer(denied: bool) -> String {
    let middle = if denied {
        " (tests skipped by your denial)"
    } else {
        ", tests pass"
    };
    format!(
        "Session store refactor is in: history behind one durable interface\
         {middle}, branch pushed. Ready for review."
    )
}

// --------------------------------------------------------------------------
// Script data (kernel/demo.py, mockup verbatim strings — served both by the
// wiring's data hooks and by the ported turn scripts in `crate::runtime`)
// --------------------------------------------------------------------------

pub const SEED_PROMPT: &str = "explain what this repo is in simple terms";
pub const SEED_NARRATION: &str =
    "Reading the repo layout and entry points to ground the summary.";
pub const SEED_COMMANDS: [&str; 2] = ["ls -la", "cat pyproject.toml | head -40"];
pub const SEED_TOOL_BODY: &str = "$ ls -la && cat pyproject.toml | head -40";
pub const SEED_ANSWER: &str = "This repo is the **command-line app for Amplifier**. If amplifier-core is \
     the engine, this is the dashboard and steering wheel: the `amplifier` \
     command starts sessions, configures providers, loads bundles, and renders \
     this UI.";

pub const STORE_PLAN_TITLE: &str = "Refactor session store";
pub const STORE_STEPS: [&str; 3] = [
    "Audit persistence paths",
    "Migrate history to durable store",
    "Verify and push",
];
pub const STORE_NARRATIONS: [&str; 3] = [
    "Mapping every read and write against the current session store.",
    "History paths found in three modules. Moving them behind one durable interface.",
    "Tests green. Preparing the push.",
];
pub const STORE_COMMANDS: [&str; 3] = [
    "grep -rn \"session_store\" amplifier/ | head -12",
    "uv run pytest tests/store/ -q",
    "git push origin mj/durable-store",
];

pub const BUILD_PROMPT: &str = "refactor the session store so history is durable offline and online";
pub const PYTEST_APPROVAL_PROMPT: &str = "Run uv run pytest tests/store/ -q?";
pub const BUILD_RECAP: &str = "Goal: durable session store. Next: open PR against main.";
pub const BUILD_END_NOTICE: &str = "agents 1 done";
pub const DENY_REASON: &str = "denied by user";
pub const DENY_CONTINUATION: &str = "continuing without test run";
/// Mockup deny line: `⊘ blocked · uv run pytest · denied by user ·
/// continuing without test run`.
pub const DENY_BLOCKED_CMD: &str = "uv run pytest";

pub const AUTO_PROMPT: &str = "refactor the session store and push it up";
pub const AUTO_MODE_NOTICE: &str = "mode auto · auto read,write · asks if risky";
pub const FORCE_PUSH_COMMAND: &str = "git push --force origin main";
pub const AUTO_BLOCK_REASON: &str = "outside user authorization";
pub const AUTO_BLOCK_CONTINUATION: &str = "finding safer path";
pub const AUTO_DEFER_NARRATION: &str =
    "Force-push denied. Branch push also crosses the trust boundary; deferring \
     the decision and finishing local verification.";
pub const AUTO_DEFER_NOTICE: &str = "decision deferred to queue · run continues";
pub const AUTO_ANSWER: &str = "Store refactor complete and verified locally: history behind one durable \
     interface, tests green. The push crossed the trust boundary, so it is \
     waiting in your decision queue.";
pub const AUTO_RECAP: &str =
    "Goal: durable session store. Next: answer the deferred push decision (ctrl-y).";

/// Mockup interrupt close-out: `✳ ` dimmer + this text dim italic.
pub const INTERRUPTED_RECAP: &str =
    "Interrupted. Goal: durable session store. Context saved; resume or restate direction.";

pub const PLAN_PROMPT: &str = "how should we make session history durable?";
pub const PLAN_MODE_NOTICE: &str = "mode plan · read-only";
pub const PLAN_NARRATION: &str = "Reading the store modules — plan mode, no writes.";
pub const PLAN_TITLE: &str = "Proposed plan · durable session history";
pub const PLAN_STEPS: [&str; 3] = [
    "Extract a SessionStore interface from the three call sites",
    "Back it with sqlite + journal replay",
    "Migrate history lazily on first read",
];
pub const PLAN_RECAP: &str = "Plan ready. shift+tab to build hands it over for execution.";
pub const PLAN_END_NOTICE: &str = "plan mode: read-only · plan handed to build on mode switch";

pub const BRAINSTORM_PROMPT: &str = "how might we make long agent runs feel supervised?";
pub const BRAINSTORM_MODE_NOTICE: &str = "mode brainstorm · no tools";
pub const BRAINSTORM_NARRATION: &str =
    "No tools in brainstorm — pure divergence, cheapest turn there is.";
pub const BRAINSTORM_IDEAS: [&str; 4] = [
    "1 Ambient tab color: orange while running, red when a decision waits",
    "2 A \"confidence strip\" under the plan: what the agent would bet on each step",
    "3 Turn rules as a film strip — scrub the session like a timeline",
    "4 Steer suggestions: the agent drafts the correction it suspects you want",
];
pub const BRAINSTORM_RECAP: &str = "Converge with /plan when one of these sticks.";

pub const AGENTS_PROMPT: &str = "run the DTU reality check across provider docs, store, and tests";
pub const AGENTS_MODE_NOTICE: &str = "mode build · auto read,test · ask write,net,spend";
pub const AGENTS_NARRATION: &str =
    "Fanning out: researcher, coder, tester. Lanes above the composer track each one.";
pub const AGENTS_ANSWER: &str = "Reality check passed: provider docs match runtime behavior, store migration \
     verified, 41 tests green across three parallel agents.";
pub const AGENTS_END_NOTICE: &str = "agents 3 done · click a lane to inspect its transcript";

/// Scripted plan for the agents turn — feeds the plan panel (Phase 1) and
/// the delegate summary's `Plan 4/4` fold (Phase 2).
pub const AGENTS_PLAN_STEPS: [&str; 4] = [
    "scan provider docs",
    "migrate session store",
    "run store tests",
    "synthesize findings",
];

// --------------------------------------------------------------------------
// Exported structured data: lanes, evidence, deferred decision
// --------------------------------------------------------------------------

/// Python `LogRowKind = Literal["narration", "tool", "command", "answer"]`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LogRowKind {
    Narration,
    Tool,
    Command,
    Answer,
}

impl LogRowKind {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            LogRowKind::Narration => "narration",
            LogRowKind::Tool => "tool",
            LogRowKind::Command => "command",
            LogRowKind::Answer => "answer",
        }
    }
}

/// One row of a subagent's own transcript (mockup `lane.log`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DemoLogRow {
    pub kind: LogRowKind,
    pub text: String,
}

impl DemoLogRow {
    fn new(kind: LogRowKind, text: &str) -> Self {
        Self {
            kind,
            text: text.to_string(),
        }
    }
}

/// One agent lane: panel line, focus transcript, and live-tree labels.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DemoLane {
    pub name: String,
    pub glyph: String,
    pub color_token: String,
    pub sub_session_id: String,
    /// Lanes-panel row, spacing verbatim from the mockup.
    pub panel_line: String,
    /// The delegated brief shown as the `[delegated]` user line on focus.
    pub brief: String,
    /// State recap line at the bottom of the focus transcript.
    pub state_recap: String,
    /// Completion result summary (`tests ✔` / `3 findings` / `2 files`).
    pub result: String,
    /// Virtual ms into the agents turn when this lane completes.
    pub done_at_ms: i64,
    pub log: Vec<DemoLogRow>,
}

/// Python `kernel.demo._sub_session_id`.
fn sub_session_id(index: u64, name: &str) -> String {
    format!("{DEMO_SESSION_ID}-{index:016x}_{name}")
}

/// Python `DEMO_LANES` (mockup verbatim).
pub fn demo_lanes() -> &'static [DemoLane] {
    static LANES: OnceLock<Vec<DemoLane>> = OnceLock::new();
    LANES.get_or_init(|| {
        vec![
            DemoLane {
                name: "researcher".to_string(),
                glyph: "◐".to_string(),
                color_token: "teal".to_string(),
                sub_session_id: sub_session_id(1, "researcher"),
                panel_line:
                    "  ◐ researcher · scanning provider docs · 41s    · ↓ 100.1k tokens · $0.09"
                        .to_string(),
                brief: "Scan the provider docs and list every capability the runtime does not exercise."
                    .to_string(),
                state_recap: "running · 41s · $0.09".to_string(),
                result: "3 findings".to_string(),
                done_at_ms: 4_400,
                log: vec![
                    DemoLogRow::new(
                        LogRowKind::Narration,
                        "Fetching the provider capability matrix and diffing it against runtime calls.",
                    ),
                    DemoLogRow::new(LogRowKind::Tool, "Ran 3 web_fetch calls"),
                    DemoLogRow::new(
                        LogRowKind::Command,
                        "grep -rn \"capabilities\" providers/ | head -20",
                    ),
                    DemoLogRow::new(
                        LogRowKind::Narration,
                        "Two undocumented streaming flags found; verifying against the SDK.",
                    ),
                ],
            },
            DemoLane {
                name: "coder".to_string(),
                glyph: "■".to_string(),
                color_token: "fg".to_string(),
                sub_session_id: sub_session_id(2, "coder"),
                panel_line:
                    "  ■ coder      · migrating store        · 2m 04s · ↓ 48.3k tokens  · $0.31"
                        .to_string(),
                brief: "Move session history behind the durable SessionStore interface.".to_string(),
                state_recap: "running · 2m 04s · $0.31".to_string(),
                result: "2 files".to_string(),
                done_at_ms: 6_000,
                log: vec![
                    DemoLogRow::new(
                        LogRowKind::Narration,
                        "Extracting the SessionStore interface from three call sites.",
                    ),
                    DemoLogRow::new(LogRowKind::Command, "rg -n 'SessionStore\\(' src/"),
                    DemoLogRow::new(LogRowKind::Tool, "Ran 4 edit calls · 2 files"),
                    DemoLogRow::new(
                        LogRowKind::Narration,
                        "Wiring journal replay into resume; tests next.",
                    ),
                ],
            },
            DemoLane {
                name: "tester".to_string(),
                glyph: "✔".to_string(),
                color_token: "dim".to_string(),
                sub_session_id: sub_session_id(3, "tester"),
                panel_line:
                    "  ✔ tester     · done · tests ✔         · 55s    · ↓ 3.2k tokens   · $0.07"
                        .to_string(),
                brief: "Run the store test suite and report failures with evidence.".to_string(),
                state_recap: "completed · 55s · $0.07 · tests ✔".to_string(),
                result: "tests ✔".to_string(),
                done_at_ms: 2_600,
                log: vec![
                    DemoLogRow::new(LogRowKind::Command, "uv run pytest tests/store/ -q"),
                    DemoLogRow::new(LogRowKind::Tool, "Ran 1 shell command · 41 passed"),
                    DemoLogRow::new(
                        LogRowKind::Answer,
                        "All 41 store tests pass. Slowest: test_journal_replay (1.2s). \
                         No flakes across 3 runs.",
                    ),
                ],
            },
        ]
    })
}

/// Python `DEMO_LANE_BY_NAME` lookup.
pub fn demo_lane_by_name(name: &str) -> Option<&'static DemoLane> {
    demo_lanes().iter().find(|lane| lane.name == name)
}

/// One numbered evidence claim: `"quote" → grounding tool call`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DemoEvidenceClaim {
    pub quote: &'static str,
    pub source: &'static str,
}

/// Python `DEMO_EVIDENCE` (mockup verbatim).
pub const DEMO_EVIDENCE: [DemoEvidenceClaim; 2] = [
    DemoEvidenceClaim {
        quote: "dashboard and steering wheel",
        source: "Ran 2 shell commands (pyproject entry points)",
    },
    DemoEvidenceClaim {
        quote: "loads bundles",
        source: "grep amplifier_core bundle loader",
    },
];

/// The auto-turn deferred push decision (needs-you queue item).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DemoDeferredDecision {
    pub text: &'static str,
    pub chip_label: &'static str,
    pub applied_narration: &'static str,
    /// Question substring the UI renders teal (mockup: `mj/waypoint`).
    pub highlight: &'static str,
    /// Denied-action key joining the override to the DenialLog.
    pub action: &'static str,
}

/// Python `DEMO_DEFERRED_DECISION` (mockup verbatim).
pub const DEMO_DEFERRED_DECISION: DemoDeferredDecision = DemoDeferredDecision {
    text: "Push branch to origin was blocked (outside trust boundary). \
           Push to fork mj/waypoint instead?",
    chip_label: "yes · push to fork",
    applied_narration: "Applying decision: pushing to fork mj/waypoint. \
                        Trust-slot suggestion queued for /improve.",
    highlight: "mj/waypoint",
    action: "push-to-fork",
};

// --------------------------------------------------------------------------
// Per-turn specs (telemetry, outcome, labels — mockup verbatim)
// --------------------------------------------------------------------------

/// Everything the UI needs to close out one scripted demo turn
/// (`kernel.demo.DemoTurnSpec`).
#[derive(Clone, Debug, PartialEq)]
pub struct DemoTurnSpec {
    pub key: TurnKey,
    /// One of the exact Python literals `chat`/`plan`/`brainstorm`/`build`/`auto`.
    pub mode: &'static str,
    pub mode_notice: Option<String>,
    pub prompt: String,
    pub duration_ms: i64,
    pub secs_text: String,
    pub tokens: u64,
    pub cached_pct: Option<u8>,
    pub cost: Decimal,
    /// Cumulative session spend after this turn (mockup `this.cost`).
    pub cost_after: Decimal,
    pub outcome: String,
    pub shipped: bool,
    pub rule_label: String,
    pub checkpoint_id: String,
    pub checkpoint_label: String,
    pub answer: Option<String>,
    pub recap: Option<String>,
    pub end_notice: Option<String>,
}

impl DemoTurnSpec {
    /// The reducer-facing close-out subset (feeds `ReducerOptions::spec_lookup`).
    pub fn reducer_spec(&self) -> TurnSpec {
        TurnSpec {
            duration_ms: self.duration_ms,
            tokens: self.tokens,
            cached_pct: self.cached_pct,
            cost: self.cost,
            cost_after: self.cost_after,
            outcome: self.outcome.clone(),
            shipped: self.shipped,
            rule_label: self.rule_label.clone(),
            checkpoint_label: self.checkpoint_label.clone(),
        }
    }
}

/// Python `kernel.demo._build_turn_specs` (mockup verbatim).
fn build_turn_specs() -> Vec<DemoTurnSpec> {
    let build_tokens: u64 = tick_tokens(TurnKey::Build, None).iter().sum();
    let auto_tokens: u64 = tick_tokens(TurnKey::Auto, None).iter().sum();
    let agents_tokens: u64 = tick_tokens(TurnKey::Agents, None).iter().sum();
    let store_cost = store_turn_cost(9); // both store turns run 9 virtual seconds
    let shipped_outcome = "3 files · +142/−38 · tests ✔";
    let mut cost = demo_session_cost_start();
    let mut specs = vec![DemoTurnSpec {
        key: TurnKey::Seed,
        mode: "chat",
        mode_notice: None,
        prompt: SEED_PROMPT.to_string(),
        duration_ms: 0,
        secs_text: "6.1s".to_string(),
        tokens: 83_900,
        cached_pct: Some(91),
        cost: Decimal::new(17, 2),
        cost_after: cost,
        outcome: "answer".to_string(),
        shipped: false,
        rule_label: rule_label("6.1s", 83_900, Some(91), Decimal::new(17, 2), "answer"),
        checkpoint_id: "t1".to_string(),
        checkpoint_label: "repo explainer · answer".to_string(),
        answer: Some(SEED_ANSWER.to_string()),
        recap: None,
        end_notice: None,
    }];
    cost += store_cost;
    specs.push(DemoTurnSpec {
        key: TurnKey::Build,
        mode: "chat",
        mode_notice: None,
        prompt: BUILD_PROMPT.to_string(),
        duration_ms: 9_300,
        secs_text: "9s".to_string(),
        tokens: build_tokens,
        cached_pct: Some(88),
        cost: store_cost,
        cost_after: cost,
        outcome: shipped_outcome.to_string(),
        shipped: true,
        rule_label: rule_label("9s", build_tokens, Some(88), store_cost, shipped_outcome),
        checkpoint_id: "t2".to_string(),
        checkpoint_label: "store refactor · shipped".to_string(),
        answer: Some(build_answer(false)),
        recap: Some(BUILD_RECAP.to_string()),
        end_notice: Some(BUILD_END_NOTICE.to_string()),
    });
    cost += store_cost;
    specs.push(DemoTurnSpec {
        key: TurnKey::Auto,
        mode: "auto",
        mode_notice: Some(AUTO_MODE_NOTICE.to_string()),
        prompt: AUTO_PROMPT.to_string(),
        duration_ms: 9_700,
        secs_text: "9s".to_string(),
        tokens: auto_tokens,
        cached_pct: Some(88),
        cost: store_cost,
        cost_after: cost,
        outcome: shipped_outcome.to_string(),
        shipped: true,
        rule_label: rule_label("9s", auto_tokens, Some(88), store_cost, shipped_outcome),
        checkpoint_id: "t3".to_string(),
        checkpoint_label: "store refactor · shipped".to_string(),
        answer: Some(AUTO_ANSWER.to_string()),
        recap: Some(AUTO_RECAP.to_string()),
        end_notice: None,
    });
    cost += Decimal::new(6, 2);
    specs.push(DemoTurnSpec {
        key: TurnKey::Plan,
        mode: "plan",
        mode_notice: Some(PLAN_MODE_NOTICE.to_string()),
        prompt: PLAN_PROMPT.to_string(),
        duration_ms: 3_600,
        secs_text: "11s".to_string(),
        tokens: 9_400,
        cached_pct: Some(93),
        cost: Decimal::new(6, 2),
        cost_after: cost,
        outcome: "answer · plan ready".to_string(),
        shipped: false,
        rule_label: rule_label(
            "11s",
            9_400,
            Some(93),
            Decimal::new(6, 2),
            "answer · plan ready",
        ),
        checkpoint_id: "t4".to_string(),
        checkpoint_label: "durable-history plan · answer".to_string(),
        answer: None,
        recap: Some(PLAN_RECAP.to_string()),
        end_notice: Some(PLAN_END_NOTICE.to_string()),
    });
    cost += Decimal::new(3, 2);
    specs.push(DemoTurnSpec {
        key: TurnKey::Brainstorm,
        mode: "brainstorm",
        mode_notice: Some(BRAINSTORM_MODE_NOTICE.to_string()),
        prompt: BRAINSTORM_PROMPT.to_string(),
        duration_ms: 3_000,
        secs_text: "8s".to_string(),
        tokens: 4_100,
        cached_pct: None,
        cost: Decimal::new(3, 2),
        cost_after: cost,
        outcome: "answer".to_string(),
        shipped: false,
        rule_label: rule_label("8s", 4_100, None, Decimal::new(3, 2), "answer"),
        checkpoint_id: "t5".to_string(),
        checkpoint_label: "supervision ideas · answer".to_string(),
        answer: None,
        recap: Some(BRAINSTORM_RECAP.to_string()),
        end_notice: None,
    });
    cost += Decimal::new(52, 2);
    let agents_outcome = "2 files · tests ✔ · 3 agents";
    specs.push(DemoTurnSpec {
        key: TurnKey::Agents,
        mode: "build",
        mode_notice: Some(AGENTS_MODE_NOTICE.to_string()),
        prompt: AGENTS_PROMPT.to_string(),
        duration_ms: 6_000,
        secs_text: "6s".to_string(),
        tokens: agents_tokens,
        cached_pct: None,
        cost: Decimal::new(52, 2),
        cost_after: cost,
        outcome: agents_outcome.to_string(),
        shipped: true,
        rule_label: rule_label("6s", agents_tokens, None, Decimal::new(52, 2), agents_outcome),
        checkpoint_id: "t6".to_string(),
        checkpoint_label: "DTU reality check · shipped".to_string(),
        answer: Some(AGENTS_ANSWER.to_string()),
        recap: None,
        end_notice: Some(AGENTS_END_NOTICE.to_string()),
    });
    specs
}

/// Python `DEMO_TURNS`.
pub fn demo_turns() -> &'static [DemoTurnSpec] {
    static TURNS: OnceLock<Vec<DemoTurnSpec>> = OnceLock::new();
    TURNS.get_or_init(build_turn_specs)
}

/// Python `DEMO_TURN_BY_KEY` lookup (every key exists by construction).
pub fn demo_turn_by_key(key: TurnKey) -> &'static DemoTurnSpec {
    demo_turns()
        .iter()
        .find(|spec| spec.key == key)
        .expect("every TurnKey has a scripted spec")
}

/// The build turn's alternate close-out when the pytest approval is denied.
///
/// Mockup: the deny path skips the command (1400ms) and the step's
/// trailing 400ms wait — 7 virtual seconds, $0.11, no `tests ✔`.
pub fn build_denied_spec() -> DemoTurnSpec {
    let secs = 7;
    let tokens: u64 = tick_tokens(TurnKey::Build, Some(secs as usize)).iter().sum();
    let cost = store_turn_cost(secs);
    let outcome = "3 files · +142/−38";
    let base = demo_turn_by_key(TurnKey::Build);
    DemoTurnSpec {
        duration_ms: 7_500,
        secs_text: format!("{secs}s"),
        tokens,
        cost,
        cost_after: demo_session_cost_start() + cost,
        outcome: outcome.to_string(),
        rule_label: rule_label(&format!("{secs}s"), tokens, Some(88), cost, outcome),
        answer: Some(build_answer(true)),
        ..base.clone()
    }
}

/// Close-out for an esc-interrupted demo turn (Python `interrupted_spec`).
///
/// Mockup `runTurn`: the rule is `tele + " · interrupted"` where `tele`
/// uses the *actual* elapsed secs/toks at the break and `turnCost = 0.04 +
/// secs * 0.01`; the checkpoint is labeled `<stem> · interrupted` and
/// nothing ships.
pub fn interrupted_spec(key: TurnKey, secs: i64, tokens: u64) -> DemoTurnSpec {
    let base = demo_turn_by_key(key);
    let cost = store_turn_cost(secs);
    // Python `checkpoint_label.rsplit(" · ", 1)[0]`.
    let stem = base
        .checkpoint_label
        .rsplit_once(" · ")
        .map(|(stem, _)| stem)
        .unwrap_or(&base.checkpoint_label);
    DemoTurnSpec {
        duration_ms: secs * 1000,
        secs_text: format!("{secs}s"),
        tokens,
        cost,
        cost_after: base.cost_after - base.cost + cost,
        outcome: "interrupted".to_string(),
        shipped: false,
        rule_label: rule_label(&format!("{secs}s"), tokens, base.cached_pct, cost, "interrupted"),
        checkpoint_label: format!("{stem} · interrupted"),
        answer: None,
        recap: Some(INTERRUPTED_RECAP.to_string()),
        end_notice: None,
        ..base.clone()
    }
}

// --------------------------------------------------------------------------
// demo_wiring.py: panel-line parsing → LaneSeed
// --------------------------------------------------------------------------

/// Python `demo_wiring._PANEL_LINE_RE`.
fn panel_line_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"^\s*\S\s+(?P<name>\S+)\s*·\s*(?P<activity>.+?)\s*·\s*(?P<elapsed>[\dms ]+?)\s*·\s*↓\s*(?P<tokens>[\d.]+)k\s+tokens\s*·\s*\$(?P<cost>[\d.]+)\s*$",
        )
        .expect("valid regex")
    })
}

/// Parse `41s` / `2m` / `2m 04s` into seconds (Python `_parse_elapsed`).
fn parse_elapsed(text: &str) -> f64 {
    static MINUTES: OnceLock<Regex> = OnceLock::new();
    static SECONDS: OnceLock<Regex> = OnceLock::new();
    let minutes = MINUTES.get_or_init(|| Regex::new(r"(\d+)\s*m").expect("valid regex"));
    let seconds = SECONDS.get_or_init(|| Regex::new(r"(\d+)\s*s").expect("valid regex"));
    let mut total = 0.0;
    if let Some(captures) = minutes.captures(text) {
        total += captures[1].parse::<f64>().expect("digits") * 60.0;
    }
    if let Some(captures) = seconds.captures(text) {
        total += captures[1].parse::<f64>().expect("digits");
    }
    total
}

/// `"100.1"` (k) → `100100` (Python `_parse_k_tokens`).
///
/// Python `round()` is half-even; the demo figures never land on an exact
/// .5 after the float multiply, so `f64::round` matches on all real inputs.
fn parse_k_tokens(text: &str) -> u64 {
    (text.parse::<f64>().expect("k-token figure") * 1000.0).round() as u64
}

/// Mockup LANES glyph → lane state (Python `_GLYPH_STATE`; DESIGN-SPEC §8
/// tri-state panel). Unknown glyphs fall back to `running`.
fn glyph_state(glyph: &str) -> LaneStateName {
    match glyph {
        "◐" => LaneStateName::Running,
        "■" => LaneStateName::Working,
        "✔" => LaneStateName::Done,
        _ => LaneStateName::Running,
    }
}

/// Reducer LaneSeed from the mockup lane's verbatim panel line.
pub fn lane_seed_for(name: &str) -> Option<LaneSeed> {
    let lane = demo_lane_by_name(name)?;
    let mut seed = LaneSeed {
        state: glyph_state(&lane.glyph),
        ..LaneSeed::default()
    };
    if let Some(captures) = panel_line_re().captures(&lane.panel_line) {
        seed.activity = captures["activity"].to_string();
        seed.elapsed = parse_elapsed(&captures["elapsed"]);
        seed.cost = captures["cost"].parse().expect("decimal cost");
        seed.tokens = parse_k_tokens(&captures["tokens"]);
    }
    Some(seed)
}

/// The focused-lane transcript (DESIGN-SPEC §8) from DEMO_LANES data.
pub fn lane_focus_blocks(lane: &DemoLane, allocator: &mut BlockIdAllocator) -> Vec<TranscriptBlock> {
    let mut blocks: Vec<TranscriptBlock> = vec![
        SessionBanner {
            focus_note: focused_lane_banner(&lane.name, DEMO_SESSION_ID),
            ..SessionBanner::new(allocator.next_id(), "")
        }
        .into(),
        UserLine {
            mode: "delegated".to_string(),
            ..UserLine::new(allocator.next_id(), lane.brief.clone())
        }
        .into(),
    ];
    for row in &lane.log {
        blocks.push(match row.kind {
            LogRowKind::Narration => Narration::new(allocator.next_id(), row.text.clone()).into(),
            LogRowKind::Tool => ToolLine {
                status: ToolLineStatus::Completed,
                ..ToolLine::new(allocator.next_id(), row.text.clone())
            }
            .into(),
            LogRowKind::Command => LiveCommand::new(allocator.next_id(), row.text.clone()).into(),
            // Mockup focusLane `F(...)`: every focus-lane row is created
            // with click: null — log answers are not evidence targets.
            LogRowKind::Answer => Answer {
                clickable: false,
                ..Answer::new(allocator.next_id(), answer_spans(&row.text))
            }
            .into(),
        });
    }
    blocks.push(
        Answer {
            // Mockup focusLane: `✳ ` dimmer + lane state dim italic;
            // focus-lane lines are created with click: null.
            spans: vec![
                Segment {
                    style_token: StyleToken::Dimmer,
                    ..Segment::new("✳ ")
                },
                Segment {
                    style_token: StyleToken::Dim,
                    italic: true,
                    ..Segment::new(lane.state_recap.clone())
                },
            ],
            clickable: false,
            ..Answer::new(allocator.next_id(), Vec::new())
        }
        .into(),
    );
    blocks
}

/// Python `demo_evidence_links` — the scripted showEvidence claims.
pub fn demo_evidence_links() -> Vec<EvidenceLink> {
    DEMO_EVIDENCE
        .iter()
        .map(|claim| EvidenceLink::new(claim.quote, claim.source))
        .collect()
}

// --------------------------------------------------------------------------
// DemoWiring: the pure data-hook core of Python's DemoRuntimeAdapter
// --------------------------------------------------------------------------

/// The scripted demo's prompt→turn bookkeeping and data hooks.
///
/// The pure state behind `DemoRuntimeAdapter` (ADR-0007): every composer
/// submit maps to one scripted demo turn — the mockup prompt when typed
/// verbatim, otherwise advancing build → auto → plan → brainstorm →
/// agents. The asyncio lifecycle (queue, approval futures, DemoRuntime)
/// is app-assembly and stays unported.
#[derive(Debug, Default)]
pub struct DemoWiring {
    played: HashSet<TurnKey>,
    /// Mockup send()/drainQueue(): the user line echoes the typed text
    /// verbatim even though the scripted turn is fixed — remember which
    /// spec the echo stands for so close-out lookups still resolve
    /// (Python `_prompt_alias`).
    prompt_alias: HashMap<String, TurnKey>,
    build_denied: bool,
    /// The live-telemetry close-out of an esc-interrupted turn (Python
    /// adapter `turn_spec` reads `self._runtime.interrupted_close`): the
    /// playing runtime records it ([`crate::runtime::DemoScript`]), the
    /// adapter bridges it here. Cleared at the next submit (the runtime
    /// clears its copy at the next turn start).
    interrupted_close: Option<DemoTurnSpec>,
}

impl DemoWiring {
    pub fn new() -> Self {
        Self::default()
    }

    /// Python `start()`'s pure part: the seed replay marks `seed` played.
    pub fn mark_seed_played(&mut self) {
        self.played.insert(TurnKey::Seed);
    }

    /// Python `_key_for`: verbatim prompt match, else the first unplayed
    /// turn in mockup order, else `build`.
    pub fn key_for(&self, text: &str) -> TurnKey {
        let text = text.trim();
        if let Some(spec) = demo_turns().iter().find(|spec| spec.prompt == text) {
            return spec.key;
        }
        for key in TURN_ORDER {
            if !self.played.contains(&key) {
                return key;
            }
        }
        TurnKey::Build
    }

    /// Python `submit()`'s pure part: resolve the turn key, reset the
    /// per-run deny flag on a build rerun, record the played key and the
    /// verbatim-echo alias. Returns the key the runtime would play.
    pub fn record_submit(&mut self, text: &str) -> TurnKey {
        let text = text.trim();
        let key = self.key_for(text);
        if key == TurnKey::Build {
            // Mockup runTurn: `denied` is a per-run local — a denial in
            // one build run must not leak into a later rerun's close-out.
            self.build_denied = false;
        }
        // Python `DemoRuntime._begin_turn` clears `interrupted_close` when
        // the next turn starts; the submit boundary is that turn start.
        self.interrupted_close = None;
        self.played.insert(key);
        self.prompt_alias.insert(text.to_string(), key);
        key
    }

    /// Bridge for the runtime's esc-interrupt close-out (Python adapter
    /// `turn_spec` reads `self._runtime.interrupted_close` live; here the
    /// adapter copies it over when the cancelled turn settles).
    pub fn set_interrupted_close(&mut self, close: Option<DemoTurnSpec>) {
        self.interrupted_close = close;
    }

    /// Python `answer_approval`'s pure part: `Deny` on the pytest ticket
    /// switches the build close-out to the mockup deny spec.
    pub fn record_approval_choice(&mut self, choice: &str) {
        if choice == "Deny" {
            self.build_denied = true;
        }
    }

    /// Python `turn_spec`: verbatim/aliased spec, overridden by the
    /// esc-interrupt close-out for that same turn, then by the build-deny
    /// close-out.
    pub fn turn_spec(&self, prompt: &str) -> Option<DemoTurnSpec> {
        let text = prompt.trim();
        let spec = demo_turns()
            .iter()
            .find(|spec| spec.prompt == text)
            .or_else(|| {
                // The user line echoes the typed text verbatim (mockup
                // userLine(text)); resolve it back to the scripted spec.
                self.prompt_alias.get(text).map(|key| demo_turn_by_key(*key))
            })?;
        if let Some(interrupted) = &self.interrupted_close {
            if interrupted.key == spec.key {
                return Some(interrupted.clone());
            }
        }
        if spec.key == TurnKey::Build && self.build_denied {
            return Some(build_denied_spec());
        }
        Some(spec.clone())
    }

    /// Python `lane_seed`.
    pub fn lane_seed(&self, agent_name: &str) -> Option<LaneSeed> {
        lane_seed_for(agent_name)
    }

    /// Python `lane_blocks`: by lane name, else by hierarchical
    /// sub-session id.
    pub fn lane_blocks(
        &self,
        name: &str,
        session_id: &str,
        allocator: &mut BlockIdAllocator,
    ) -> Option<Vec<TranscriptBlock>> {
        let lane = demo_lane_by_name(name)
            .or_else(|| demo_lanes().iter().find(|lane| lane.sub_session_id == session_id))?;
        Some(lane_focus_blocks(lane, allocator))
    }

    /// Python `evidence_links` — every final-answer click reveals the same
    /// scripted showEvidence block, regardless of which answer was clicked.
    pub fn evidence_links(&self, _answer_text: &str) -> Vec<EvidenceLink> {
        demo_evidence_links()
    }

    /// Python `deferred_decision`:
    /// `(text, "", (chip_label,), highlight, action)`.
    pub fn deferred_decision(
        &self,
        _message: &str,
        _decision_id: &str,
    ) -> (String, String, Vec<String>, String, String) {
        (
            DEMO_DEFERRED_DECISION.text.to_string(),
            String::new(),
            vec![DEMO_DEFERRED_DECISION.chip_label.to_string()],
            DEMO_DEFERRED_DECISION.highlight.to_string(),
            DEMO_DEFERRED_DECISION.action.to_string(),
        )
    }

    /// Python `decision_narration` — scripted narration keyed by the chip
    /// label alone (`action` is ignored).
    pub fn decision_narration(&self, choice: &str, _action: &str) -> String {
        if choice == DEMO_DEFERRED_DECISION.chip_label {
            return DEMO_DEFERRED_DECISION.applied_narration.to_string();
        }
        format!("Applying decision: {choice}")
    }

    /// Python adapter init: session cost accumulates per turn (mockup
    /// `this.cost += turnCost`); the mount-time $0.57 already includes the
    /// seed turn's $0.17, and the adapter replays the seed as a live turn —
    /// start below it so the footer lands on $0.57 once the seed rule is cut.
    pub fn session_cost_start(&self) -> Decimal {
        demo_session_cost_start() - demo_turn_by_key(TurnKey::Seed).cost
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dec(text: &str) -> Decimal {
        text.parse().expect("decimal literal")
    }

    // -- token / cost formulas (tests/test_kernel_demo_data.py) -----------

    /// Pins Python `test_tick_tokens_deterministic_and_pinned` (the pinned
    /// seeded draws are inlined constants here — same guarded values).
    #[test]
    fn test_tick_tokens_deterministic_and_pinned() {
        assert_eq!(
            tick_tokens(TurnKey::Build, None),
            vec![608, 439, 557, 425, 415, 450, 463, 470, 636]
        );
        assert_eq!(
            tick_tokens(TurnKey::Auto, None),
            vec![411, 538, 606, 443, 416, 475, 455, 496, 541]
        );
        assert_eq!(tick_tokens(TurnKey::Agents, None), vec![900; 6]);
        assert_eq!(tick_tokens(TurnKey::Build, None), tick_tokens(TurnKey::Build, None));
        assert_eq!(
            tick_tokens(TurnKey::Build, Some(7)),
            tick_tokens(TurnKey::Build, None)[..7].to_vec()
        );
        // Mockup formula bounds: 380 + floor(random() * 260).
        for tick in tick_tokens(TurnKey::Build, None)
            .into_iter()
            .chain(tick_tokens(TurnKey::Auto, None))
        {
            assert!((380..=639).contains(&tick));
        }
    }

    /// Pins Python `test_store_turn_cost_formula`.
    #[test]
    fn test_store_turn_cost_formula() {
        assert_eq!(store_turn_cost(9), dec("0.13")); // 0.04 + 9 * 0.01
        assert_eq!(store_turn_cost(7), dec("0.11"));
    }

    /// Pins Python `test_label_helpers_match_mockup_formatting`.
    #[test]
    fn test_label_helpers_match_mockup_formatting() {
        assert_eq!(format_tokens_k(5_400), "5.4k");
        assert_eq!(format_tokens_k(83_900), "83.9k");
        assert_eq!(
            rule_label("6.1s", 83_900, Some(91), dec("0.17"), "answer"),
            "6.1s · 83.9k tok, 91% cached · $0.17 · answer"
        );
        assert_eq!(
            rule_label("6s", 5_400, None, dec("0.52"), "2 files · tests ✔ · 3 agents"),
            "6s · 5.4k tok · $0.52 · 2 files · tests ✔ · 3 agents"
        );
    }

    // -- turn specs --------------------------------------------------------

    /// Pins Python `test_turn_order_and_checkpoint_ids`.
    #[test]
    fn test_turn_order_and_checkpoint_ids() {
        let keys: Vec<&str> = demo_turns().iter().map(|t| t.key.as_str()).collect();
        assert_eq!(keys, ["seed", "build", "auto", "plan", "brainstorm", "agents"]);
        let ids: Vec<&str> = demo_turns().iter().map(|t| t.checkpoint_id.as_str()).collect();
        assert_eq!(ids, ["t1", "t2", "t3", "t4", "t5", "t6"]);
        let modes: Vec<&str> = demo_turns().iter().map(|t| t.mode).collect();
        assert_eq!(modes, ["chat", "chat", "auto", "plan", "brainstorm", "build"]);
    }

    /// Pins Python `test_rule_labels_verbatim`.
    #[test]
    fn test_rule_labels_verbatim() {
        let label = |key| &demo_turn_by_key(key).rule_label;
        assert_eq!(label(TurnKey::Seed), "6.1s · 83.9k tok, 91% cached · $0.17 · answer");
        assert_eq!(
            label(TurnKey::Build),
            "9s · 4.5k tok, 88% cached · $0.13 · 3 files · +142/−38 · tests ✔"
        );
        assert_eq!(
            label(TurnKey::Auto),
            "9s · 4.4k tok, 88% cached · $0.13 · 3 files · +142/−38 · tests ✔"
        );
        assert_eq!(
            label(TurnKey::Plan),
            "11s · 9.4k tok, 93% cached · $0.06 · answer · plan ready"
        );
        assert_eq!(label(TurnKey::Brainstorm), "8s · 4.1k tok · $0.03 · answer");
        assert_eq!(
            label(TurnKey::Agents),
            "6s · 5.4k tok · $0.52 · 2 files · tests ✔ · 3 agents"
        );
    }

    /// Pins Python `test_checkpoint_labels_and_shipped_flags`.
    #[test]
    fn test_checkpoint_labels_and_shipped_flags() {
        let label = |key| &demo_turn_by_key(key).checkpoint_label;
        assert_eq!(label(TurnKey::Seed), "repo explainer · answer");
        assert_eq!(label(TurnKey::Build), "store refactor · shipped");
        assert_eq!(label(TurnKey::Auto), "store refactor · shipped");
        assert_eq!(label(TurnKey::Plan), "durable-history plan · answer");
        assert_eq!(label(TurnKey::Brainstorm), "supervision ideas · answer");
        assert_eq!(label(TurnKey::Agents), "DTU reality check · shipped");
        let shipped: Vec<bool> = demo_turns().iter().map(|t| t.shipped).collect();
        assert_eq!(shipped, [false, true, true, false, false, true]);
    }

    /// Pins Python `test_costs_accumulate_like_the_mockup`.
    #[test]
    fn test_costs_accumulate_like_the_mockup() {
        // Mockup: this.cost starts at 0.57, then +0.13, +0.13, +0.06, +0.03, +0.52.
        assert_eq!(demo_session_cost_start(), dec("0.57"));
        let mut running = demo_session_cost_start();
        for spec in &demo_turns()[1..] {
            running += spec.cost;
            assert_eq!(spec.cost_after, running);
        }
        assert_eq!(demo_turns().last().expect("six turns").cost_after, dec("1.44"));
        assert_eq!(demo_turns()[0].cost_after, demo_session_cost_start()); // seed pre-baked
    }

    /// Pins Python `test_recaps_and_notices_verbatim`.
    #[test]
    fn test_recaps_and_notices_verbatim() {
        let by_key = demo_turn_by_key;
        assert_eq!(
            by_key(TurnKey::Build).recap.as_deref(),
            Some("Goal: durable session store. Next: open PR against main.")
        );
        assert_eq!(
            by_key(TurnKey::Auto).recap.as_deref(),
            Some("Goal: durable session store. Next: answer the deferred push decision (ctrl-y).")
        );
        assert_eq!(
            by_key(TurnKey::Plan).recap.as_deref(),
            Some("Plan ready. shift+tab to build hands it over for execution.")
        );
        assert_eq!(
            by_key(TurnKey::Brainstorm).recap.as_deref(),
            Some("Converge with /plan when one of these sticks.")
        );
        assert_eq!(by_key(TurnKey::Build).end_notice.as_deref(), Some("agents 1 done"));
        assert_eq!(by_key(TurnKey::Auto).end_notice, None);
        assert_eq!(
            by_key(TurnKey::Plan).end_notice.as_deref(),
            Some("plan mode: read-only · plan handed to build on mode switch")
        );
        assert_eq!(by_key(TurnKey::Brainstorm).end_notice, None);
        assert_eq!(
            by_key(TurnKey::Agents).end_notice.as_deref(),
            Some("agents 3 done · click a lane to inspect its transcript")
        );
    }

    /// Pins Python `test_build_denied_spec`.
    #[test]
    fn test_build_denied_spec() {
        let denied = build_denied_spec();
        assert_eq!(denied.rule_label, "7s · 3.4k tok, 88% cached · $0.11 · 3 files · +142/−38");
        assert_eq!(denied.outcome, "3 files · +142/−38"); // no tests ✔
        assert_eq!(denied.cost, dec("0.11"));
        assert_eq!(denied.duration_ms, 7_500);
        assert_eq!(
            denied.answer.as_deref(),
            Some(
                "Session store refactor is in: history behind one durable interface \
                 (tests skipped by your denial), branch pushed. Ready for review."
            )
        );
        assert!(denied.shipped);
    }

    /// Oracle check (not a pinned pytest case): `interrupted_spec` output
    /// for a store key and a non-store key, verified against the real
    /// Python `kernel.demo.interrupted_spec`.
    #[test]
    fn oracle_interrupted_spec_matches_python() {
        // Esc during the build turn's first command: 2 virtual seconds,
        // two ticks (608 + 439) consumed.
        let build = interrupted_spec(TurnKey::Build, 2, 1_047);
        assert_eq!(build.secs_text, "2s");
        assert_eq!(build.duration_ms, 2_000);
        assert_eq!(build.tokens, 1_047);
        assert_eq!(build.cost, dec("0.06"));
        assert_eq!(build.cost_after, dec("0.63"));
        assert_eq!(build.outcome, "interrupted");
        assert!(!build.shipped);
        assert_eq!(build.rule_label, "2s · 1.0k tok, 88% cached · $0.06 · interrupted");
        assert_eq!(build.checkpoint_label, "store refactor · interrupted");
        assert_eq!(build.checkpoint_id, "t2");
        assert_eq!(build.answer, None);
        assert_eq!(build.recap.as_deref(), Some(INTERRUPTED_RECAP));
        assert_eq!(build.end_notice, None);
        // Non-store keys reuse the same close-out formula (spec §11).
        let plan = interrupted_spec(TurnKey::Plan, 2, 800);
        assert_eq!(plan.cost_after, dec("0.89"));
        assert_eq!(plan.rule_label, "2s · 0.8k tok, 93% cached · $0.06 · interrupted");
        assert_eq!(plan.checkpoint_label, "durable-history plan · interrupted");
        assert_eq!(plan.mode, "plan");
        assert_eq!(plan.mode_notice.as_deref(), Some(PLAN_MODE_NOTICE));
        assert_eq!(plan.checkpoint_id, "t4");
    }

    /// Oracle check (not a pinned pytest case): the adapter `turn_spec`
    /// serves the runtime's interrupted close-out when its key matches the
    /// prompt's turn, and the next submit clears it (Python `turn_spec`
    /// reads `self._runtime.interrupted_close`; `_begin_turn` clears it).
    #[test]
    fn oracle_turn_spec_serves_interrupted_close_for_matching_key() {
        let mut wiring = DemoWiring::new();
        wiring.mark_seed_played();
        wiring.record_submit(BUILD_PROMPT);
        let close = interrupted_spec(TurnKey::Build, 2, 1_047);
        wiring.set_interrupted_close(Some(close.clone()));
        assert_eq!(wiring.turn_spec(BUILD_PROMPT), Some(close.clone()));
        // A different turn's prompt is untouched by the close-out.
        assert_eq!(
            wiring.turn_spec(PLAN_PROMPT),
            Some(demo_turn_by_key(TurnKey::Plan).clone())
        );
        // The interrupted close-out outranks the build-deny close-out.
        wiring.record_approval_choice("Deny");
        assert_eq!(wiring.turn_spec(BUILD_PROMPT), Some(close));
        // The next submit starts a new turn — the close-out is cleared.
        wiring.record_submit(BUILD_PROMPT);
        assert_eq!(
            wiring.turn_spec(BUILD_PROMPT),
            Some(demo_turn_by_key(TurnKey::Build).clone())
        );
    }

    // -- lanes (tests/test_kernel_demo_data.py) -----------------------------

    /// Pins Python `test_lane_panel_lines_verbatim`.
    #[test]
    fn test_lane_panel_lines_verbatim() {
        let lines: Vec<&str> = demo_lanes().iter().map(|lane| lane.panel_line.as_str()).collect();
        assert_eq!(
            lines,
            [
                "  ◐ researcher · scanning provider docs · 41s    · ↓ 100.1k tokens · $0.09",
                "  ■ coder      · migrating store        · 2m 04s · ↓ 48.3k tokens  · $0.31",
                "  ✔ tester     · done · tests ✔         · 55s    · ↓ 3.2k tokens   · $0.07",
            ]
        );
        let glyphs: Vec<(&str, &str)> = demo_lanes()
            .iter()
            .map(|lane| (lane.glyph.as_str(), lane.color_token.as_str()))
            .collect();
        assert_eq!(glyphs, [("◐", "teal"), ("■", "fg"), ("✔", "dim")]);
    }

    /// Pins Python `test_lane_completion_times`.
    #[test]
    fn test_lane_completion_times() {
        let times: Vec<(&str, i64)> = demo_lanes()
            .iter()
            .map(|lane| (lane.name.as_str(), lane.done_at_ms))
            .collect();
        assert_eq!(times, [("researcher", 4_400), ("coder", 6_000), ("tester", 2_600)]);
    }

    /// Pins Python `test_lane_focus_transcript_data`.
    #[test]
    fn test_lane_focus_transcript_data() {
        let researcher = demo_lane_by_name("researcher").expect("scripted lane");
        assert_eq!(
            researcher.brief,
            "Scan the provider docs and list every capability the runtime does not exercise."
        );
        assert_eq!(researcher.state_recap, "running · 41s · $0.09");
        let rows: Vec<(&str, &str)> = researcher
            .log
            .iter()
            .map(|row| (row.kind.as_str(), row.text.as_str()))
            .collect();
        assert_eq!(
            rows,
            [
                (
                    "narration",
                    "Fetching the provider capability matrix and diffing it against runtime calls."
                ),
                ("tool", "Ran 3 web_fetch calls"),
                ("command", "grep -rn \"capabilities\" providers/ | head -20"),
                (
                    "narration",
                    "Two undocumented streaming flags found; verifying against the SDK."
                ),
            ]
        );
        let tester = demo_lane_by_name("tester").expect("scripted lane");
        assert_eq!(tester.state_recap, "completed · 55s · $0.07 · tests ✔");
        let last = tester.log.last().expect("log rows");
        assert_eq!(last.kind, LogRowKind::Answer);
        assert!(last.text.starts_with("All 41 store tests pass."));
        let coder = demo_lane_by_name("coder").expect("scripted lane");
        assert_eq!(coder.state_recap, "running · 2m 04s · $0.31");
        let kinds: Vec<&str> = coder.log.iter().map(|row| row.kind.as_str()).collect();
        assert_eq!(kinds, ["narration", "command", "tool", "narration"]);
        // Hierarchical sub-session ids route lanes by session_id/parent_id.
        for lane in demo_lanes() {
            assert!(lane.sub_session_id.starts_with(&format!("{DEMO_SESSION_ID}-")));
            assert!(lane.sub_session_id.ends_with(&format!("_{}", lane.name)));
        }
    }

    // -- evidence, banner, deferred decision --------------------------------

    /// Pins Python `test_evidence_claims_verbatim`.
    #[test]
    fn test_evidence_claims_verbatim() {
        let claims: Vec<(&str, &str)> =
            DEMO_EVIDENCE.iter().map(|claim| (claim.quote, claim.source)).collect();
        assert_eq!(
            claims,
            [
                (
                    "dashboard and steering wheel",
                    "Ran 2 shell commands (pyproject entry points)"
                ),
                ("loads bundles", "grep amplifier_core bundle loader"),
            ]
        );
    }

    /// Pins Python `test_banner_verbatim`.
    #[test]
    fn test_banner_verbatim() {
        assert_eq!(
            DEMO_BANNER,
            (
                "Amplifier 2026.07.13-87b93ef* · core 1.6.0",
                "Bundle: anchors | Provider: OpenAI | gpt-5.5 · session e07de0",
            )
        );
    }

    /// Pins Python `test_deferred_decision_verbatim`.
    #[test]
    fn test_deferred_decision_verbatim() {
        assert_eq!(
            DEMO_DEFERRED_DECISION.text,
            "Push branch to origin was blocked (outside trust boundary). \
             Push to fork mj/waypoint instead?"
        );
        assert_eq!(DEMO_DEFERRED_DECISION.chip_label, "yes · push to fork");
        assert_eq!(
            DEMO_DEFERRED_DECISION.applied_narration,
            "Applying decision: pushing to fork mj/waypoint. \
             Trust-slot suggestion queued for /improve."
        );
    }

    // -- lane focus transcript (tests/test_flow_interrupt.py) --------------

    /// Pins Python `test_lane_focus_state_recap_carries_recap_glyph`
    /// (mockup focusLane: `✳ ` dimmer + lane state dim italic, spec §8).
    #[test]
    fn test_lane_focus_state_recap_carries_recap_glyph() {
        let lane = demo_lane_by_name("coder").expect("scripted lane");
        let blocks = lane_focus_blocks(lane, &mut BlockIdAllocator::new());
        let recap = blocks.last().expect("recap block");
        let TranscriptBlock::Answer(recap) = recap else {
            panic!("expected an Answer block, got {}", recap.kind());
        };
        let spans: Vec<(&str, &str, bool)> = recap
            .spans
            .iter()
            .map(|span| (span.text.as_str(), span.style_token.as_str(), span.italic))
            .collect();
        assert_eq!(
            spans,
            [("✳ ", "dimmer", false), (lane.state_recap.as_str(), "dim", true)]
        );
    }

    /// Oracle check (not a pinned pytest case): the full researcher focus
    /// transcript, verified against the real `lane_focus_blocks` output.
    #[test]
    fn oracle_lane_focus_blocks_researcher_structure() {
        let lane = demo_lane_by_name("researcher").expect("scripted lane");
        let blocks = lane_focus_blocks(lane, &mut BlockIdAllocator::new());
        let kinds: Vec<&str> = blocks.iter().map(|block| block.kind()).collect();
        assert_eq!(
            kinds,
            [
                "session_banner",
                "user_line",
                "narration",
                "tool_line",
                "live_command",
                "narration",
                "answer",
            ]
        );
        let ids: Vec<&str> = blocks.iter().map(|block| block.id()).collect();
        assert_eq!(ids, ["b1", "b2", "b3", "b4", "b5", "b6", "b7"]);
        let TranscriptBlock::SessionBanner(banner) = &blocks[0] else {
            panic!("expected session banner");
        };
        assert_eq!(banner.headline, "");
        assert_eq!(
            banner.focus_note,
            "focused: researcher · subagent of e07de0 · own context window \
             · results report back to parent · esc back"
        );
        let TranscriptBlock::UserLine(user) = &blocks[1] else {
            panic!("expected user line");
        };
        assert_eq!(user.mode, "delegated");
        assert_eq!(user.text, lane.brief);
        let TranscriptBlock::ToolLine(tool) = &blocks[3] else {
            panic!("expected tool line");
        };
        assert_eq!(tool.summary, "Ran 3 web_fetch calls");
        assert_eq!(tool.status, ToolLineStatus::Completed);
        let TranscriptBlock::LiveCommand(command) = &blocks[4] else {
            panic!("expected live command");
        };
        assert_eq!(command.command, "grep -rn \"capabilities\" providers/ | head -20");
        let TranscriptBlock::Answer(recap) = &blocks[6] else {
            panic!("expected answer recap");
        };
        assert!(!recap.clickable);
    }

    // -- lane seeds (oracle-verified against Python lane_seed_for) ----------

    /// Oracle check (not a pinned pytest case): Python `lane_seed_for`
    /// output for all three mockup lanes, verified against the real module.
    #[test]
    fn oracle_lane_seed_for_parses_panel_lines() {
        let researcher = lane_seed_for("researcher").expect("scripted lane");
        assert_eq!(
            researcher,
            LaneSeed {
                activity: "scanning provider docs".to_string(),
                elapsed: 41.0,
                cost: dec("0.09"),
                tokens: 100_100,
                state: LaneStateName::Running,
            }
        );
        let coder = lane_seed_for("coder").expect("scripted lane");
        assert_eq!(
            coder,
            LaneSeed {
                activity: "migrating store".to_string(),
                elapsed: 124.0,
                cost: dec("0.31"),
                tokens: 48_300,
                state: LaneStateName::Working,
            }
        );
        let tester = lane_seed_for("tester").expect("scripted lane");
        assert_eq!(
            tester,
            LaneSeed {
                activity: "done · tests ✔".to_string(),
                elapsed: 55.0,
                cost: dec("0.07"),
                tokens: 3_200,
                state: LaneStateName::Done,
            }
        );
        assert_eq!(lane_seed_for("nope"), None);
    }

    /// Oracle check (not a pinned pytest case): `_parse_elapsed` accepts
    /// `41s` / `2m` / `2m 04s` — verified `124.0 / 41.0 / 120.0 / 0.0`.
    #[test]
    fn oracle_parse_elapsed_forms() {
        assert_eq!(parse_elapsed("2m 04s"), 124.0);
        assert_eq!(parse_elapsed("41s"), 41.0);
        assert_eq!(parse_elapsed("2m"), 120.0);
        assert_eq!(parse_elapsed(""), 0.0);
    }

    /// Oracle check (not a pinned pytest case): `demo_evidence_links()`
    /// output verified against the real Python module.
    #[test]
    fn oracle_demo_evidence_links_matches_python() {
        assert_eq!(
            demo_evidence_links(),
            vec![
                EvidenceLink::new(
                    "dashboard and steering wheel",
                    "Ran 2 shell commands (pyproject entry points)"
                ),
                EvidenceLink::new("loads bundles", "grep amplifier_core bundle loader"),
            ]
        );
    }

    // -- DemoWiring bookkeeping (oracle-verified against DemoRuntimeAdapter) --

    /// Oracle check (not a pinned pytest case): `_key_for` matches the
    /// verbatim prompt, else advances build → auto → plan → brainstorm →
    /// agents, else falls back to build — verified against the adapter.
    #[test]
    fn oracle_key_for_advances_turn_order() {
        let mut wiring = DemoWiring::new();
        assert_eq!(wiring.key_for("random text"), TurnKey::Build);
        wiring.mark_seed_played();
        wiring.record_submit("whatever");
        assert_eq!(wiring.key_for("whatever else"), TurnKey::Auto);
        assert_eq!(wiring.key_for(PLAN_PROMPT), TurnKey::Plan);
        for prompt in ["a", "b", "c", "d"] {
            wiring.record_submit(prompt);
        }
        assert_eq!(wiring.key_for("anything else"), TurnKey::Build);
    }

    /// Oracle check (not a pinned pytest case): the verbatim-echo alias —
    /// a typed prompt that mapped to a scripted turn resolves back to that
    /// spec at close-out (Python `_prompt_alias` in `turn_spec`).
    #[test]
    fn oracle_turn_spec_resolves_prompt_alias() {
        let mut wiring = DemoWiring::new();
        wiring.mark_seed_played();
        assert_eq!(wiring.turn_spec("do something great"), None);
        let key = wiring.record_submit("  do something great  ");
        assert_eq!(key, TurnKey::Build);
        let spec = wiring.turn_spec("do something great").expect("aliased spec");
        assert_eq!(spec.key, TurnKey::Build);
        assert_eq!(spec, demo_turn_by_key(TurnKey::Build).clone());
        // Verbatim mockup prompts resolve without an alias.
        let plan = wiring.turn_spec(PLAN_PROMPT).expect("verbatim spec");
        assert_eq!(plan.key, TurnKey::Plan);
    }

    /// Oracle check (not a pinned pytest case): `Deny` on the pytest
    /// approval swaps the build close-out for the deny spec, and a build
    /// rerun resets the per-run flag (mockup runTurn local `denied`;
    /// exercised end-to-end by Python
    /// `test_esc_denies_blocked_line_and_turn_continues`).
    #[test]
    fn oracle_deny_swaps_build_close_out_and_rerun_resets() {
        let mut wiring = DemoWiring::new();
        wiring.mark_seed_played();
        wiring.record_submit(BUILD_PROMPT);
        wiring.record_approval_choice("Deny");
        let spec = wiring.turn_spec(BUILD_PROMPT).expect("denied spec");
        assert_eq!(spec, build_denied_spec());
        assert_eq!(spec.cost_after, dec("0.68"));
        // Allow choices never flip the flag.
        wiring.record_approval_choice("Allow once");
        assert_eq!(wiring.turn_spec(BUILD_PROMPT).expect("still denied"), build_denied_spec());
        // A later build rerun must not inherit the denial.
        wiring.record_submit(BUILD_PROMPT);
        assert_eq!(
            wiring.turn_spec(BUILD_PROMPT).expect("clean spec"),
            demo_turn_by_key(TurnKey::Build).clone()
        );
    }

    /// Oracle check (not a pinned pytest case): the deferred-decision
    /// tuple and narration data hooks, verified against the adapter.
    #[test]
    fn oracle_deferred_decision_and_narration_hooks() {
        let wiring = DemoWiring::new();
        assert_eq!(
            wiring.deferred_decision("ignored", "ignored"),
            (
                "Push branch to origin was blocked (outside trust boundary). \
                 Push to fork mj/waypoint instead?"
                    .to_string(),
                String::new(),
                vec!["yes · push to fork".to_string()],
                "mj/waypoint".to_string(),
                "push-to-fork".to_string(),
            )
        );
        assert_eq!(
            wiring.decision_narration("yes · push to fork", "ignored"),
            "Applying decision: pushing to fork mj/waypoint. \
             Trust-slot suggestion queued for /improve."
        );
        assert_eq!(
            wiring.decision_narration("something else", ""),
            "Applying decision: something else"
        );
    }

    /// Oracle check (not a pinned pytest case): lane_blocks resolves by
    /// name first, then by hierarchical sub-session id.
    #[test]
    fn oracle_lane_blocks_by_name_or_sub_session_id() {
        let wiring = DemoWiring::new();
        let mut ids = BlockIdAllocator::new();
        let by_name = wiring.lane_blocks("tester", "", &mut ids).expect("by name");
        assert_eq!(by_name.len(), 2 + 3 + 1); // banner + brief + 3 log rows + recap
        let mut ids = BlockIdAllocator::new();
        let coder_session = &demo_lane_by_name("coder").expect("lane").sub_session_id;
        let by_session = wiring.lane_blocks("unknown", coder_session, &mut ids).expect("by session");
        let TranscriptBlock::UserLine(user) = &by_session[1] else {
            panic!("expected user line");
        };
        assert_eq!(user.text, "Move session history behind the durable SessionStore interface.");
        let mut ids = BlockIdAllocator::new();
        assert_eq!(wiring.lane_blocks("unknown", "nope", &mut ids), None);
    }

    /// Oracle check (not a pinned pytest case): adapter init subtracts the
    /// seed turn's $0.17 from the mount-time $0.57 (footer lands on $0.57
    /// once the seed rule is cut).
    #[test]
    fn oracle_session_cost_start_excludes_seed_cost() {
        assert_eq!(DemoWiring::new().session_cost_start(), dec("0.40"));
    }

    /// The reducer-facing conversion carries every close-out field.
    #[test]
    fn test_reducer_spec_conversion() {
        let spec = demo_turn_by_key(TurnKey::Agents).reducer_spec();
        assert_eq!(
            spec,
            TurnSpec {
                duration_ms: 6_000,
                tokens: 5_400,
                cached_pct: None,
                cost: dec("0.52"),
                cost_after: dec("1.44"),
                outcome: "2 files · tests ✔ · 3 agents".to_string(),
                shipped: true,
                rule_label: "6s · 5.4k tok · $0.52 · 2 files · tests ✔ · 3 agents".to_string(),
                checkpoint_label: "DTU reality check · shipped".to_string(),
            }
        );
    }
}
