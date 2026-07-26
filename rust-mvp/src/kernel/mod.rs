//! Client-side kernel logic ported from the Python app's `kernel/` layer —
//! event normalization, cost/usage accounting, and the trust/governance
//! decision logic. Process/IO orchestration stays in the Python backend
//! behind `serve`; this layer consumes its effects over the protocol.

pub mod approval;
pub mod cost;
pub mod display;
pub mod events;
pub mod evidence;
pub mod file_mentions;
pub mod git_yield;
pub mod governance_hook;
pub mod prompt_history;
pub mod reminder_trust;
pub mod safety;
pub mod steering;
pub mod surface_hint;
pub mod trackers;
pub mod turn_yield;
