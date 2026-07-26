//! Client-side kernel logic ported from the Python app's `kernel/` layer —
//! event normalization, cost/usage accounting, and the trust/governance
//! decision logic. Process/IO orchestration stays in the Python backend
//! behind `serve`; this layer consumes its effects over the protocol.
