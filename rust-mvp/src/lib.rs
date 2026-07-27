//! Library surface of the ratatui client. The binary (`main.rs`) is a thin shell;
//! all layers live here so units port over from the Python app one module at a
//! time (see MIGRATION.md at the repo root) with their tests alongside.

pub mod app;
pub mod commands;
pub mod core_client;
pub mod kernel;
pub mod message;
pub mod model;
pub mod protocol;
pub mod runtime;
pub mod ui;
