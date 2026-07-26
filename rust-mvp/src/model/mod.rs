//! Pure domain state — mirrors the Python app's `model/` layer (no rendering here).

pub mod blocks;
pub mod config;
pub mod evidence;
pub mod formatting;
pub mod injection;
pub mod lanes;
pub mod modes;
pub mod native_modes;
pub mod queues;
pub mod redaction;
pub mod terminal;
pub mod trust;
pub mod turn;

// The pre-assembly demo placeholders (`Mode` / `Block` / `Tallies`) are
// gone: the assembled app runs on the ported units (`model/modes.rs`,
// `model/blocks.rs`, the reducer's cost tracking) end to end.
