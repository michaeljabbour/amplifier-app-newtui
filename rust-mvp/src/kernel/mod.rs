//! Client-side kernel logic ported from the Python app's `kernel/` layer —
//! event normalization, cost/usage accounting, and the trust/governance
//! decision logic. Process/IO orchestration stays in the Python backend
//! behind `serve`; this layer consumes its effects over the protocol.

pub mod events;
pub mod file_mentions;
pub mod git_yield;
pub mod prompt_history;
pub mod reminder_trust;
