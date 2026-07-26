//! Shared terminal surface geometry (current width in columns).
//!
//! The full-screen TUI's rendering contract is *width-aware*: the kernel
//! injects a per-request surface hint (issue #35 / docs/BACKLOG.md section 2)
//! telling the model how many columns it has and which Markdown subset renders
//! cleanly. Width is owned by the UI (resize events, app loop) but consumed in
//! the kernel (a `provider:request` hook, runtime thread), so the value lives
//! here in `model/` — the one layer both may touch (ADR-0007 layering:
//! `ui/` -> `model/` -> `kernel/`).
//!
//! Reads and writes cross the app/runtime thread boundary, so a plain lock
//! keeps them honest; the stored value is always clamped to a sane column
//! range so a transient 0-width report during boot never leaks into the hint.

use std::sync::Mutex;

/// Assumed width before the UI reports a real size (VT100 default).
pub const DEFAULT_TERMINAL_COLS: u16 = 80;

/// Clamp bounds: guards against 0/negative boot reports and absurd values.
pub const MIN_TERMINAL_COLS: u16 = 20;
pub const MAX_TERMINAL_COLS: u16 = 1000;

/// Thread-safe holder for the current terminal width in columns.
///
/// The UI updates it on resize ([`TerminalSurface::set_cols`], app loop); the
/// kernel's surface-hint hook reads [`TerminalSurface::cols`] at
/// `provider:request` (runtime thread). A resize is therefore reflected on
/// the next turn's request.
#[derive(Debug)]
pub struct TerminalSurface {
    cols: Mutex<u16>,
}

impl TerminalSurface {
    /// Create a surface with the given width (out-of-range values are clamped).
    pub fn new(cols: i64) -> Self {
        Self {
            cols: Mutex::new(clamp(cols)),
        }
    }

    /// The current terminal width, clamped to the supported range.
    pub fn cols(&self) -> u16 {
        *self.cols.lock().expect("terminal surface lock poisoned")
    }

    /// Record a new terminal width (out-of-range values are clamped).
    pub fn set_cols(&self, cols: i64) {
        let clamped = clamp(cols);
        *self.cols.lock().expect("terminal surface lock poisoned") = clamped;
    }

    /// Record a width reported as text; junk that does not parse as an
    /// integer falls back to [`DEFAULT_TERMINAL_COLS`] (mirrors Python's
    /// `int(...)`-or-default behaviour for dynamically-typed junk input).
    pub fn set_cols_str(&self, cols: &str) {
        let clamped = clamp_str(cols);
        *self.cols.lock().expect("terminal surface lock poisoned") = clamped;
    }
}

impl Default for TerminalSurface {
    /// Defaults to [`DEFAULT_TERMINAL_COLS`], matching the Python constructor.
    fn default() -> Self {
        Self::new(i64::from(DEFAULT_TERMINAL_COLS))
    }
}

fn clamp(cols: i64) -> u16 {
    if cols < i64::from(MIN_TERMINAL_COLS) {
        MIN_TERMINAL_COLS
    } else if cols > i64::from(MAX_TERMINAL_COLS) {
        MAX_TERMINAL_COLS
    } else {
        cols as u16
    }
}

fn clamp_str(cols: &str) -> u16 {
    match cols.trim().parse::<i64>() {
        Ok(value) => clamp(value),
        Err(_) => DEFAULT_TERMINAL_COLS,
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::thread;

    use super::*;

    #[test]
    fn test_defaults_to_vt100_width() {
        assert_eq!(TerminalSurface::default().cols(), DEFAULT_TERMINAL_COLS);
        assert_eq!(DEFAULT_TERMINAL_COLS, 80);
    }

    #[test]
    fn test_set_cols_updates_width() {
        let surface = TerminalSurface::default();
        surface.set_cols(132);
        assert_eq!(surface.cols(), 132);
    }

    #[test]
    fn test_zero_and_negative_widths_clamp_to_floor() {
        // A transient 0-width report during boot must never leak into the hint.
        let surface = TerminalSurface::default();
        surface.set_cols(0);
        assert_eq!(surface.cols(), MIN_TERMINAL_COLS);
        surface.set_cols(-40);
        assert_eq!(surface.cols(), MIN_TERMINAL_COLS);
    }

    #[test]
    fn test_absurd_width_clamps_to_ceiling() {
        let surface = TerminalSurface::default();
        surface.set_cols(10_000);
        assert_eq!(surface.cols(), MAX_TERMINAL_COLS);
    }

    #[test]
    fn test_junk_width_falls_back_to_default() {
        let surface = TerminalSurface::new(200);
        surface.set_cols_str("not-an-int");
        assert_eq!(surface.cols(), DEFAULT_TERMINAL_COLS);
    }

    #[test]
    fn test_constructor_clamps_too() {
        assert_eq!(TerminalSurface::new(0).cols(), MIN_TERMINAL_COLS);
        assert_eq!(TerminalSurface::new(5_000).cols(), MAX_TERMINAL_COLS);
    }

    #[test]
    fn test_concurrent_writes_leave_a_valid_value() {
        // The UI (app loop) writes while the kernel (runtime thread) reads;
        // the lock guarantees a torn value never surfaces.
        let surface = Arc::new(TerminalSurface::default());
        let widths: [i64; 5] = [40, 80, 120, 200, 60];

        let handles: Vec<_> = widths
            .iter()
            .map(|&value| {
                let surface = Arc::clone(&surface);
                thread::spawn(move || {
                    for _ in 0..500 {
                        surface.set_cols(value);
                    }
                })
            })
            .collect();
        for handle in handles {
            handle.join().expect("writer thread panicked");
        }
        assert!(widths.contains(&i64::from(surface.cols())));
    }
}
