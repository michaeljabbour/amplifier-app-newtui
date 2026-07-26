//! Turn-level telemetry, outcomes, checkpoints and the session ledger.
//!
//! Turn identity (ADR-0007 resolution 4): the app assigns `turn_id` at
//! `prompt:submit` as the 1-indexed user-message position in the live
//! context (resume history base + recorded ledger turns — rewound
//! automatically when a confirmed fork trims the ledger, spec §9). Steers
//! never increment it (leftover steers are discarded at turn end); queued
//! messages DO. Every turn rule records a [`Checkpoint`] stamped onto the
//! TurnRule block at emit time — rewind resolves checkpoints by id, never
//! by string matching rendered labels.
//!
//! Port of `src/amplifier_app_newtui/model/turn.py`.

use std::fmt;

use rust_decimal::Decimal;

use crate::model::formatting::format_tokens_k;

/// Elapsed format used in telemetry suffixes/labels.
///
/// Mockup: always raw integer seconds (`secs + "s"` — working line,
/// plan suffix and rule telemetry alike), so a 75-second turn reads
/// `75s`, never `1m 15s`. Truncates like Python `int()`.
fn format_elapsed(seconds: f64) -> String {
    format!("{}s", seconds as i64)
}

/// Two-decimal dollar figure matching Python's `f"{Decimal:.2f}"`
/// (round-half-even, zero-padded to exactly two places).
fn format_cost_2dp(cost: Decimal) -> String {
    // round_dp uses banker's rounding (MidpointNearestEven), matching the
    // default decimal context Python formats with; `{:.2}` then only pads.
    format!("{:.2}", cost.round_dp(2))
}

/// Compact per-turn (or live) telemetry (DESIGN-SPEC §3/§11).
///
/// - `secs`: wall-clock seconds for the turn so far.
/// - `tokens_down`: output tokens received (the `↓ X.Xk tok` figure).
/// - `cached_pct`: percentage of input tokens served from cache.
/// - `cost`: dollars, computed from provider usage (kernel/cost.py).
/// - `estimated`: some usage could not be priced, so `cost` is a
///   floor — the rendered $ figure gets a `~` prefix (never lie).
///
/// Frozen pydantic model in Python — treat as immutable once built.
#[derive(Clone, Debug, PartialEq)]
pub struct TurnTelemetry {
    pub secs: f64,
    pub tokens_down: u64,
    pub cached_pct: Option<u8>,
    pub cost: Decimal,
    pub estimated: bool,
}

impl TurnTelemetry {
    /// Constructor defaults matching Python (`secs` is the only required field).
    pub fn new(secs: f64) -> Self {
        Self {
            secs,
            tokens_down: 0,
            cached_pct: None,
            cost: Decimal::ZERO,
            estimated: false,
        }
    }

    /// Live plan-header suffix: `(Ns · ↓ X.Xk tok)`.
    pub fn suffix(&self) -> String {
        format!(
            "({} · ↓ {} tok)",
            format_elapsed(self.secs),
            format_tokens_k(self.tokens_down)
        )
    }

    /// Turn-rule label prefix: `<Ns> · <X.Xk> tok, <N>% cached · $<cost>`.
    ///
    /// `~$` when any of the turn's usage was unpriceable (the figure
    /// is a floor, not the real spend).
    pub fn label(&self) -> String {
        let mut token_part = format!("{} tok", format_tokens_k(self.tokens_down));
        if let Some(pct) = self.cached_pct {
            token_part.push_str(&format!(", {pct}% cached"));
        }
        let marker = if self.estimated { "~" } else { "" };
        format!(
            "{} · {} · {}${}",
            format_elapsed(self.secs),
            token_part,
            marker,
            format_cost_2dp(self.cost)
        )
    }
}

/// Python `OutcomeKind = Literal["answer", "shipped", "interrupted", "plan_ready"]`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutcomeKind {
    Answer,
    Shipped,
    Interrupted,
    PlanReady,
}

impl OutcomeKind {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            OutcomeKind::Answer => "answer",
            OutcomeKind::Shipped => "shipped",
            OutcomeKind::Interrupted => "interrupted",
            OutcomeKind::PlanReady => "plan_ready",
        }
    }
}

impl fmt::Display for OutcomeKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// What a completed turn produced (DESIGN-SPEC §3 turn-rule outcomes).
///
/// Rendered outcome strings per kind:
///
/// - `answer`      → `answer` (dimmer label)
/// - `shipped`     → `3 files · +142/−38 · tests ✔` (dim label)
/// - `interrupted` → `· interrupted`
/// - `plan_ready`  → `· plan ready`
#[derive(Clone, Debug, PartialEq)]
pub struct TurnOutcome {
    pub kind: OutcomeKind,
    pub files_changed: u64,
    /// `+142/−38` style diffstat captured from git; empty when not shipped.
    pub diffstat: String,
    /// `Some(true)`/`Some(false)` when tests ran this turn; `None` when they did not.
    pub tests_ok: Option<bool>,
}

impl TurnOutcome {
    /// Constructor defaults matching Python (`kind` is the only required field).
    pub fn new(kind: OutcomeKind) -> Self {
        Self {
            kind,
            files_changed: 0,
            diffstat: String::new(),
            tests_ok: None,
        }
    }

    pub fn shipped(&self) -> bool {
        self.kind == OutcomeKind::Shipped
    }

    /// The outcome fragment of the turn-rule label.
    pub fn outcome_label(&self) -> String {
        match self.kind {
            OutcomeKind::Answer => "answer".to_string(),
            OutcomeKind::Interrupted => "· interrupted".to_string(),
            OutcomeKind::PlanReady => "· plan ready".to_string(),
            OutcomeKind::Shipped => {
                let plural = if self.files_changed != 1 { "s" } else { "" };
                let mut parts = vec![format!("{} file{plural}", self.files_changed)];
                if !self.diffstat.is_empty() {
                    parts.push(self.diffstat.clone());
                }
                if let Some(ok) = self.tests_ok {
                    parts.push(if ok { "tests ✔" } else { "tests ✗" }.to_string());
                }
                parts.join(" · ")
            }
        }
    }
}

/// One rewind target recorded at every turn rule (DESIGN-SPEC §9).
///
/// - `id`: `t1`, `t2`, … (stamped on the TurnRule block at emit).
/// - `turn_id`: 1-indexed user-message turn in the live context (the
///   fork point foundation's `fork_session[_in_memory]` slices at).
/// - `message_index`: transcript message index at the rule — the trim
///   point the backend fork restores to.
/// - `cost_at`: cumulative session spend when the checkpoint was cut.
/// - `label`: human description shown in the rewind picker.
#[derive(Clone, Debug, PartialEq)]
pub struct Checkpoint {
    pub id: String,
    pub turn_id: u64,
    pub message_index: u64,
    pub cost_at: Decimal,
    pub label: String,
}

/// One completed turn as the ledger records it.
#[derive(Clone, Debug, PartialEq)]
pub struct LedgerTurn {
    pub turn_id: u64,
    pub telemetry: TurnTelemetry,
    pub outcome: TurnOutcome,
    pub checkpoint: Checkpoint,
}

/// Error mirroring the Python `KeyError` raised by [`OutcomeLedger::trim_to`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TurnError {
    /// Python `KeyError(f"unknown checkpoint: {checkpoint_id}")`.
    UnknownCheckpoint(String),
}

impl fmt::Display for TurnError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TurnError::UnknownCheckpoint(id) => write!(f, "unknown checkpoint: {id}"),
        }
    }
}

impl std::error::Error for TurnError {}

/// Session-scope outcome accounting (DESIGN-SPEC §10).
///
/// Backs `/ledger`: `N turns · $X.XX · N shipped · N answer-only ·
/// cache hit NN%`, the footer `▲` yield glyph (last turn shipped) and
/// the rewind picker's checkpoint list. Mutable by design — one instance
/// per session, fed by the turn lifecycle.
#[derive(Debug, Default)]
pub struct OutcomeLedger {
    turns: Vec<LedgerTurn>,
}

impl OutcomeLedger {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn turns(&self) -> &[LedgerTurn] {
        &self.turns
    }

    pub fn turn_count(&self) -> usize {
        self.turns.len()
    }

    /// Total session cost across recorded turns.
    pub fn spend(&self) -> Decimal {
        self.turns
            .iter()
            .map(|turn| turn.telemetry.cost)
            .sum::<Decimal>()
    }

    pub fn shipped_count(&self) -> usize {
        self.turns.iter().filter(|turn| turn.outcome.shipped()).count()
    }

    /// Mockup cmdLedger math: every non-shipped turn is answer-only.
    ///
    /// `turns − shipped` so the ledger line always sums
    /// (plan-ready and interrupted turns count as answer-only).
    pub fn answer_only_count(&self) -> usize {
        self.turn_count() - self.shipped_count()
    }

    /// Token-weighted aggregate cache-hit percentage across turns.
    pub fn cache_hit_pct(&self) -> u8 {
        let mut weighted = 0.0_f64;
        let mut total = 0_u64;
        for turn in &self.turns {
            let Some(pct) = turn.telemetry.cached_pct else {
                continue;
            };
            weighted += f64::from(pct) * turn.telemetry.tokens_down as f64;
            total += turn.telemetry.tokens_down;
        }
        if total == 0 {
            return 0;
        }
        // Python round() is round-half-to-even.
        (weighted / total as f64).round_ties_even() as u8
    }

    /// True when the most recent turn shipped (footer `▲` yield glyph).
    pub fn last_shipped(&self) -> bool {
        self.turns
            .last()
            .is_some_and(|turn| turn.outcome.shipped())
    }

    pub fn checkpoints(&self) -> Vec<&Checkpoint> {
        self.turns.iter().map(|turn| &turn.checkpoint).collect()
    }

    pub fn next_checkpoint_id(&self) -> String {
        format!("t{}", self.turns.len() + 1)
    }

    /// Record a completed turn, cutting its checkpoint at the same time.
    ///
    /// `cost_at` is the cumulative SESSION cost at the rule (mockup
    /// `cp.cost = this.cost` — the footer $ at that moment, including
    /// any pre-session baseline). Falls back to recorded-turn spend when
    /// the caller has no session baseline (`None`).
    pub fn record_turn(
        &mut self,
        telemetry: TurnTelemetry,
        outcome: TurnOutcome,
        turn_id: u64,
        message_index: u64,
        label: &str,
        cost_at: Option<Decimal>,
    ) -> &LedgerTurn {
        let checkpoint = Checkpoint {
            id: self.next_checkpoint_id(),
            turn_id,
            message_index,
            cost_at: cost_at.unwrap_or_else(|| self.spend() + telemetry.cost),
            label: label.to_string(),
        };
        self.turns.push(LedgerTurn {
            turn_id,
            telemetry,
            outcome,
            checkpoint,
        });
        self.turns.last().expect("just pushed")
    }

    pub fn checkpoint_by_id(&self, checkpoint_id: &str) -> Option<&Checkpoint> {
        self.turns
            .iter()
            .map(|turn| &turn.checkpoint)
            .find(|checkpoint| checkpoint.id == checkpoint_id)
    }

    /// Drop every recorded turn (resume-replay degrade path, spec §9).
    ///
    /// Used when a replayed event log disagrees with the restored
    /// transcript (foreign/truncated log, post-rewind ghost turns): the
    /// replayed checkpoints would slice the live context at the wrong
    /// turns, so they are discarded and new checkpoints fall back to the
    /// transcript-derived `turn_base` offset.
    pub fn clear(&mut self) {
        self.turns.clear();
    }

    /// Drop ledger turns after `checkpoint_id` (post-fork, confirm-then-trim).
    ///
    /// Called only after the backend confirms the session fork
    /// (ADR-0007 rewind contract). The checkpoint's own turn survives.
    pub fn trim_to(&mut self, checkpoint_id: &str) -> Result<(), TurnError> {
        for (index, turn) in self.turns.iter().enumerate() {
            if turn.checkpoint.id == checkpoint_id {
                self.turns.truncate(index + 1);
                return Ok(());
            }
        }
        Err(TurnError::UnknownCheckpoint(checkpoint_id.to_string()))
    }
}

#[cfg(test)]
mod tests {
    //! Pins the turn-related cases of `tests/test_model_turn_queues_lanes.py`
    //! (queue cases live in `model/queues.rs`; lane cases port separately).

    use super::*;

    fn dec(value: &str) -> Decimal {
        value.parse().expect("valid decimal literal")
    }

    fn telemetry(cost: &str) -> TurnTelemetry {
        TurnTelemetry {
            tokens_down: 1000,
            cached_pct: Some(50),
            cost: dec(cost),
            ..TurnTelemetry::new(10.0)
        }
    }

    // --- telemetry formatting (DESIGN-SPEC §3) -------------------------------

    /// estimated=True → the $ figure is a floor (some usage was unpriceable).
    #[test]
    fn test_telemetry_label_marks_unpriced_cost_with_tilde() {
        let telemetry = TurnTelemetry {
            tokens_down: 3200,
            cached_pct: Some(80),
            cost: dec("0.12"),
            estimated: true,
            ..TurnTelemetry::new(24.0)
        };
        assert_eq!(telemetry.label(), "24s · 3.2k tok, 80% cached · ~$0.12");
    }

    #[test]
    fn test_telemetry_suffix_and_label() {
        let telemetry = TurnTelemetry {
            tokens_down: 3200,
            cached_pct: Some(80),
            cost: dec("0.12"),
            ..TurnTelemetry::new(24.0)
        };
        assert_eq!(telemetry.suffix(), "(24s · ↓ 3.2k tok)");
        assert_eq!(telemetry.label(), "24s · 3.2k tok, 80% cached · $0.12");
    }

    #[test]
    fn test_telemetry_elapsed_stays_raw_seconds_past_a_minute() {
        // Mockup renders `secs + "s"` everywhere — no m/h rollover (75s, not 1m 15s).
        let telemetry = TurnTelemetry {
            tokens_down: 3200,
            ..TurnTelemetry::new(75.0)
        };
        assert_eq!(telemetry.suffix(), "(75s · ↓ 3.2k tok)");
        let long_turn = TurnTelemetry {
            tokens_down: 3200,
            cost: dec("0.50"),
            ..TurnTelemetry::new(3725.0)
        };
        assert_eq!(long_turn.label(), "3725s · 3.2k tok · $0.50");
    }

    #[test]
    fn test_telemetry_elapsed_is_integer_seconds_not_a_float() {
        // Issue #34: the mockup shows `8s`, never `8.0s` — fractional wall-clock
        // seconds always render as a truncated integer in every telemetry surface.
        let telemetry = TurnTelemetry {
            tokens_down: 3200,
            cached_pct: Some(91),
            cost: dec("0.17"),
            ..TurnTelemetry::new(8.7)
        };
        assert_eq!(telemetry.suffix(), "(8s · ↓ 3.2k tok)");
        assert_eq!(telemetry.label(), "8s · 3.2k tok, 91% cached · $0.17");
    }

    #[test]
    fn test_outcome_labels_match_spec_examples() {
        assert_eq!(TurnOutcome::new(OutcomeKind::Answer).outcome_label(), "answer");
        assert_eq!(
            TurnOutcome::new(OutcomeKind::Interrupted).outcome_label(),
            "· interrupted"
        );
        assert_eq!(
            TurnOutcome::new(OutcomeKind::PlanReady).outcome_label(),
            "· plan ready"
        );
        let shipped = TurnOutcome {
            files_changed: 3,
            diffstat: "+142/−38".to_string(),
            tests_ok: Some(true),
            ..TurnOutcome::new(OutcomeKind::Shipped)
        };
        assert_eq!(shipped.outcome_label(), "3 files · +142/−38 · tests ✔");
        assert!(shipped.shipped());
    }

    /// Not a pinned Python test: oracle-checked against the real Python
    /// (`TurnTelemetry(secs=1, cost=Decimal('0.125')).label()` etc.) to lock
    /// the `f"{Decimal:.2f}"` semantics — zero-padding and round-half-even.
    #[test]
    fn oracle_cost_formats_like_python_2f_half_even() {
        let label = |cost: &str| TurnTelemetry {
            cost: dec(cost),
            ..TurnTelemetry::new(1.0)
        }
        .label();
        assert_eq!(label("0.5"), "1s · 0.0k tok · $0.50");
        assert_eq!(label("0.125"), "1s · 0.0k tok · $0.12");
        assert_eq!(label("0.135"), "1s · 0.0k tok · $0.14");
        // int() truncation, not rounding: Python suffix for secs=9.999 is 9s.
        assert_eq!(TurnTelemetry::new(9.999).suffix(), "(9s · ↓ 0.0k tok)");
    }

    // --- ledger + checkpoints (DESIGN-SPEC §9/§10) ----------------------------

    #[test]
    fn test_ledger_records_turns_and_aggregates() {
        let mut ledger = OutcomeLedger::new();
        ledger.record_turn(
            telemetry("0.10"),
            TurnOutcome::new(OutcomeKind::Answer),
            1,
            2,
            "",
            None,
        );
        ledger.record_turn(
            telemetry("0.30"),
            TurnOutcome {
                files_changed: 1,
                ..TurnOutcome::new(OutcomeKind::Shipped)
            },
            2,
            6,
            "fix retry",
            None,
        );
        assert_eq!(ledger.turn_count(), 2);
        assert_eq!(ledger.spend(), dec("0.40"));
        assert_eq!(ledger.shipped_count(), 1);
        assert_eq!(ledger.answer_only_count(), 1);
        assert!(ledger.last_shipped());
        let ids: Vec<&str> = ledger
            .checkpoints()
            .iter()
            .map(|c| c.id.as_str())
            .collect();
        assert_eq!(ids, ["t1", "t2"]);
        assert_eq!(ledger.checkpoints()[1].cost_at, dec("0.40"));
        assert_eq!(ledger.checkpoint_by_id("t2").unwrap().label, "fix retry");
    }

    #[test]
    fn test_ledger_trim_to_checkpoint_confirm_then_trim() {
        let mut ledger = OutcomeLedger::new();
        for turn_id in [1_u64, 2, 3] {
            ledger.record_turn(
                telemetry("0.10"),
                TurnOutcome::new(OutcomeKind::Answer),
                turn_id,
                turn_id * 2,
                "",
                None,
            );
        }
        ledger.trim_to("t1").unwrap();
        let ids: Vec<&str> = ledger
            .checkpoints()
            .iter()
            .map(|c| c.id.as_str())
            .collect();
        assert_eq!(ids, ["t1"]);
        assert_eq!(ledger.next_checkpoint_id(), "t2");
        assert_eq!(
            ledger.trim_to("t9"),
            Err(TurnError::UnknownCheckpoint("t9".to_string()))
        );
    }

    #[test]
    fn test_ledger_cache_hit_is_token_weighted() {
        let mut ledger = OutcomeLedger::new();
        ledger.record_turn(
            TurnTelemetry {
                tokens_down: 1000,
                cached_pct: Some(100),
                ..TurnTelemetry::new(1.0)
            },
            TurnOutcome::new(OutcomeKind::Answer),
            1,
            1,
            "",
            None,
        );
        ledger.record_turn(
            TurnTelemetry {
                tokens_down: 3000,
                cached_pct: Some(0),
                ..TurnTelemetry::new(1.0)
            },
            TurnOutcome::new(OutcomeKind::Answer),
            2,
            2,
            "",
            None,
        );
        assert_eq!(ledger.cache_hit_pct(), 25);
    }
}
