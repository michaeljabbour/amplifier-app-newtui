//! Slash-command layer ported from the Python app's `commands/` — pure
//! command specs, parsing, and block-building handlers. Dispatch side
//! effects stay with the app shell / backend.

pub mod context;
pub mod copy;
pub mod export;
pub mod improve;
pub mod permissions;
pub mod registry;
