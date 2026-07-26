//! The reducer — mirrors `ui/reducer.py`. A stateful `UiEvent → mutation`
//! translator that never draws; `ui.rs` renders purely from this state.

use crate::event::UiEvent;
use crate::kernel::cost::CostTracker;
use crate::model::{Block, Mode, Tallies};
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum TurnState {
    Idle,
    Running,
    AwaitingApproval,
}

pub struct App {
    pub blocks: Vec<Block>,
    pub live: Option<String>, // the single mutable streaming region (LiveTail)
    pub composer: String,
    pub mode: Mode,
    pub tallies: Tallies,
    /// Session/turn money ledger (`kernel::cost`): prices every
    /// `provider_response_usage` event against the active pricing table
    /// (provider `cost_usd` authoritative), mirroring the Python app's
    /// runtime-status aggregation. `tallies` is its footer projection.
    pub cost_tracker: CostTracker,
    pub state: TurnState,
    pub pending_action: Option<String>,
    pub pending_ticket: Option<String>,
    pub notice: Option<String>,
    pub spinner: usize,
    pub scroll: u16,
    pub should_quit: bool,
    pub bundle: String,
    pub session: String,
}

const SPINNER: [&str; 8] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧"];

impl App {
    pub fn new(bundle: &str, session: &str) -> Self {
        let mut app = Self {
            blocks: Vec::new(),
            live: None,
            composer: String::new(),
            mode: Mode::Chat,
            tallies: Tallies::default(),
            cost_tracker: CostTracker::new(),
            state: TurnState::Idle,
            pending_action: None,
            pending_ticket: None,
            notice: None,
            spinner: 0,
            scroll: 0,
            should_quit: false,
            bundle: bundle.into(),
            session: session.into(),
        };
        app.blocks.push(Block::SessionBanner {
            bundle: bundle.into(),
            session: session.into(),
        });
        app.blocks.push(Block::Narration(
            "Demo session — press Enter to run a scripted turn. Shift+Tab cycles mode. Ctrl+C quits.".into(),
        ));
        app
    }

    pub fn spinner_frame(&self) -> &'static str {
        SPINNER[self.spinner % SPINNER.len()]
    }

    pub fn state_label(&self) -> &'static str {
        match self.state {
            TurnState::Idle => "idle",
            TurnState::Running => "working",
            TurnState::AwaitingApproval => "needs you",
        }
    }

    /// The reducer: fold a normalized runtime event into UI state.
    pub fn on_event(&mut self, ev: UiEvent) {
        match ev {
            UiEvent::PromptSubmit(text) => {
                self.blocks.push(Block::User(text));
                self.state = TurnState::Running;
                self.notice = None;
                // Turn boundary: reset per-turn usage (session totals kept),
                // mirroring runtime_status's root prompt:submit handling.
                self.cost_tracker.start_turn();
            }
            UiEvent::Narration(text) => self.blocks.push(Block::Narration(text)),
            UiEvent::ToolLine { summary, ok } => self.blocks.push(Block::Tool { summary, ok }),
            UiEvent::ApprovalRequired { ticket_id, action } => {
                self.state = TurnState::AwaitingApproval;
                self.pending_action = Some(action);
                self.pending_ticket = Some(ticket_id);
            }
            UiEvent::StreamStart => self.live = Some(String::new()),
            UiEvent::StreamDelta(d) => {
                if let Some(buf) = self.live.as_mut() {
                    buf.push_str(&d);
                }
            }
            UiEvent::StreamEnd => {
                if let Some(buf) = self.live.take() {
                    self.blocks.push(Block::Answer(buf));
                }
            }
            UiEvent::Usage(usage) => {
                // Live tallies per provider response — session cost is exact
                // Decimal via kernel::cost (provider cost_usd authoritative,
                // else the pricing table); tokens count the ↓ output figure.
                self.cost_tracker.record(&usage);
                self.tallies.tokens += usage.output_tokens.max(0) as u64;
                self.tallies.cost = self.cost_tracker.session_cost();
            }
            UiEvent::TurnComplete { files, added, removed, tokens, cost } => {
                // Scripted runtimes carry the turn's figures on the event
                // itself; the serve protocol carries zeros here because the
                // real numbers already arrived as usage events.
                if let Some(scripted) = Decimal::from_f64(cost).filter(|c| *c > Decimal::ZERO) {
                    self.cost_tracker.seed(scripted);
                }
                self.tallies.tokens += tokens;
                self.tallies.cost = self.cost_tracker.session_cost();
                let turn = self.cost_tracker.end_turn();
                let rule_cost = if cost > 0.0 {
                    cost
                } else {
                    turn.cost.to_f64().unwrap_or(0.0)
                };
                self.blocks.push(Block::TurnRule { files, added, removed, cost: rule_cost });
                self.state = TurnState::Idle;
            }
            UiEvent::Notice(n) => self.notice = Some(n),
        }
    }

    pub fn tick(&mut self) {
        if self.state == TurnState::Running {
            self.spinner = self.spinner.wrapping_add(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kernel::events::ProviderResponseUsage;
    use std::str::FromStr;

    fn dec(text: &str) -> Decimal {
        Decimal::from_str(text).unwrap()
    }

    fn usage(input: i64, output: i64, cache_read: i64, cache_write: i64) -> ProviderResponseUsage {
        ProviderResponseUsage {
            session_id: "core-01".into(),
            input_tokens: input,
            output_tokens: output,
            cache_read,
            cache_write,
            model: "claude-sonnet-4-5".into(),
            ..ProviderResponseUsage::default()
        }
    }

    /// N usage events → exact token totals and exact Decimal cost from the
    /// kernel::cost fallback pricing table. Expected costs are oracle-checked
    /// against the real Python `kernel.cost.cost_of`:
    ///   (1200, 340, 800, 100, "claude-sonnet-4-5") → Decimal("0.00924")
    ///   (900, 120, 0, 0, "claude-sonnet-4-5")      → Decimal("0.0045")
    #[test]
    fn usage_events_accumulate_exact_tokens_and_cost() {
        let mut app = App::new("newtui", "core-01");
        app.on_event(UiEvent::PromptSubmit("go".into()));

        app.on_event(UiEvent::Usage(usage(1200, 340, 800, 100)));
        assert_eq!(app.tallies.tokens, 340);
        assert_eq!(app.tallies.cost, dec("0.00924"));

        app.on_event(UiEvent::Usage(usage(900, 120, 0, 0)));
        assert_eq!(app.tallies.tokens, 460);
        assert_eq!(app.tallies.cost, dec("0.01374"));
        assert_eq!(app.cost_tracker.turn().cost, dec("0.01374"));

        // Serve-protocol turn close-out carries zeros; the tallies keep the
        // usage-derived figures and the turn rule shows the priced turn cost.
        app.on_event(UiEvent::TurnComplete { files: 1, added: 18, removed: 0, tokens: 0, cost: 0.0 });
        assert_eq!(app.tallies.tokens, 460);
        assert_eq!(app.tallies.cost, dec("0.01374"));
        let rule = format!("{:?}", app.blocks.last().unwrap());
        assert!(rule.contains("0.01374"), "turn rule priced from usage: {rule}");

        // A new turn resets per-turn usage but keeps the session totals.
        app.on_event(UiEvent::PromptSubmit("next".into()));
        assert_eq!(app.cost_tracker.turn().cost, dec("0"));
        assert_eq!(app.tallies.cost, dec("0.01374"));
    }

    /// A provider-reported `cost_usd` is authoritative over the table
    /// (oracle: Python cost_of returns exactly Decimal("0.0123") for it).
    #[test]
    fn provider_reported_cost_usd_is_authoritative() {
        let mut app = App::new("newtui", "core-01");
        app.on_event(UiEvent::PromptSubmit("go".into()));
        let mut u = usage(10, 5, 0, 0);
        u.model = String::new(); // no table entry — cost_usd still prices it
        u.cost_usd = Some(dec("0.0123"));
        app.on_event(UiEvent::Usage(u));
        assert_eq!(app.tallies.cost, dec("0.0123"));
        assert_eq!(app.tallies.tokens, 5);
    }

    /// The scripted demo runtime carries cost/tokens on TurnComplete itself;
    /// those still land in the session tallies (no usage events emitted).
    #[test]
    fn demo_turn_complete_still_tallies() {
        let mut app = App::new("newtui", "demo-01");
        app.on_event(UiEvent::PromptSubmit("go".into()));
        app.on_event(UiEvent::TurnComplete { files: 1, added: 18, removed: 0, tokens: 1240, cost: 0.0123 });
        assert_eq!(app.tallies.tokens, 1240);
        assert_eq!(app.tallies.cost, dec("0.0123"));
        assert_eq!(app.state, TurnState::Idle);
    }
}
