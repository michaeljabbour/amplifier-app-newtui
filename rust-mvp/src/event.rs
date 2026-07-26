//! The normalization boundary — mirrors `kernel/events.py`. Every runtime
//! (scripted or, later, the real amplifier-core) emits exactly these typed
//! events. The UI never sees anything else.

#[derive(Clone, Debug)]
pub enum UiEvent {
    /// User's line, echoed instantly at submit.
    PromptSubmit(String),
    /// Ambient narration ("Thinking…", "Coordinating 2 agents…").
    Narration(String),
    /// A durable tool record (Channel B).
    ToolLine { summary: String, ok: bool },
    /// A tool wants approval; the turn parks until answered. `ticket_id` routes
    /// the answer back to the broker (real vocabulary: `approval.required`).
    ApprovalRequired { ticket_id: String, action: String },
    /// Live streaming answer (Channel A): open, deltas, close.
    StreamStart,
    StreamDelta(String),
    StreamEnd,
    /// End of turn: shipped-outcome label + cost, enriches the turn rule.
    TurnComplete { files: u32, added: u32, removed: u32, tokens: u64, cost: f64 },
    /// Transient notice (floats, non-durable).
    Notice(String),
}
