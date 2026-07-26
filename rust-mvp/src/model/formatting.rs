//! The one public home for token-count display formatting.
//!
//! Two DISTINCT display contracts live here, pinned by different tests for
//! different surfaces. They are deliberately NOT merged — each serves a
//! different part of the UI and renders the same count differently:
//!
//! - [`format_tokens_k`] — fixed one-decimal thousands (`0.0k` / `3.2k` /
//!   `1200.0k`). The turn-telemetry / lanes / demo-mockup surface: always
//!   `(tokens/1000).1f + "k"`, sub-1k counts included, never switches to
//!   `m` units.
//! - [`format_tokens_compact`] — compact human count (`742` / `4.1k` /
//!   `52k` / `1.2m`). The `/context` and `/doctor` surface: bare integer
//!   under 1k, adaptive-decimal `k`, `m` above a million.
//!
//! Pure arithmetic — no imports, no side effects — so it sits cleanly at
//! the bottom of the ADR-0007 layering and every layer above can share it.
//!
//! Port of `src/amplifier_app_newtui/model/formatting.py`.

/// Fixed one-decimal thousands: `0.0k` / `3.2k` / `1200.0k`.
///
/// The turn-telemetry surface (`TurnTelemetry` suffix/label, the
/// lanes-panel down-arrow `X.Xk tokens` figure, and the demo mockup's
/// rule labels). Always `(tokens/1000).toFixed(1) + "k"` per the
/// mockup — sub-1k counts are shown (`0.0k` at turn start) and it
/// never switches to `m` units, so 1.2M tokens reads `1200.0k`.
pub fn format_tokens_k(tokens: u64) -> String {
    format!("{:.1}k", tokens as f64 / 1_000.0)
}

/// Compact human count: `742` / `4.1k` / `52k` / `1.2m`.
///
/// The `/context` and `/doctor` surface. Bare integer below 1k;
/// `k` above that with a decimal only when it adds information
/// (`4.1k` but `8k`); `m` above a million.
pub fn format_tokens_compact(tokens: u64) -> String {
    if tokens < 1_000 {
        return tokens.to_string();
    }
    if tokens < 1_000_000 {
        let thousands = tokens as f64 / 1_000.0;
        if thousands < 10.0 {
            // Python: `round(thousands, 1) != round(thousands)` — i.e. the
            // one-decimal rounding carries a non-zero tenth. Both Python's
            // `round`/`:.1f` and Rust's `{:.1}` round the exact binary
            // double to nearest, ties to even, so the decimal strings agree.
            let one_decimal = format!("{thousands:.1}");
            if !one_decimal.ends_with(".0") {
                return format!("{one_decimal}k");
            }
        }
        // Python `round()` is banker's rounding (ties to even).
        return format!("{}k", thousands.round_ties_even() as u64);
    }
    format!("{:.1}m", tokens as f64 / 1_000_000.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Pins `test_format_tokens_k_is_fixed_one_decimal_thousands`.
    #[test]
    fn test_format_tokens_k_is_fixed_one_decimal_thousands() {
        // Sub-1k is shown, never rounded away; never switches to m-units.
        assert_eq!(format_tokens_k(0), "0.0k");
        assert_eq!(format_tokens_k(608), "0.6k");
        assert_eq!(format_tokens_k(3_200), "3.2k");
        assert_eq!(format_tokens_k(52_000), "52.0k");
        assert_eq!(format_tokens_k(1_200_000), "1200.0k");
    }

    /// Pins `test_format_tokens_compact_is_adaptive_human_units`.
    #[test]
    fn test_format_tokens_compact_is_adaptive_human_units() {
        assert_eq!(format_tokens_compact(742), "742");
        assert_eq!(format_tokens_compact(4_100), "4.1k");
        assert_eq!(format_tokens_compact(8_000), "8k");
        assert_eq!(format_tokens_compact(52_000), "52k");
        assert_eq!(format_tokens_compact(118_000), "118k");
        assert_eq!(format_tokens_compact(200_000), "200k");
        assert_eq!(format_tokens_compact(1_200_000), "1.2m");
    }

    /// Not from the Python test file: edge values oracle-checked against the
    /// real Python implementation (`uv run python`) to pin rounding parity.
    #[test]
    fn test_oracle_checked_rounding_edges_match_python() {
        assert_eq!(format_tokens_k(9_999), "10.0k");
        assert_eq!(format_tokens_k(999_500), "999.5k");
        assert_eq!(format_tokens_compact(950), "950");
        assert_eq!(format_tokens_compact(1_050), "1.1k"); // double 1.05 rounds up
        assert_eq!(format_tokens_compact(9_500), "9.5k");
        assert_eq!(format_tokens_compact(9_950), "9.9k"); // double 9.95 rounds down
        assert_eq!(format_tokens_compact(9_999), "10k");
        assert_eq!(format_tokens_compact(999_499), "999k");
        assert_eq!(format_tokens_compact(999_500), "1000k"); // banker's: ties to even
        assert_eq!(format_tokens_compact(1_000_000), "1.0m");
        assert_eq!(format_tokens_compact(1_049_999), "1.0m");
    }

    /// Pins `test_surfaces_diverge_on_the_same_count`.
    #[test]
    fn test_surfaces_diverge_on_the_same_count() {
        // Same input, two contracts: this difference is intentional, not a bug.
        assert_eq!(format_tokens_k(52_000), "52.0k");
        assert_eq!(format_tokens_compact(52_000), "52k");
        assert_ne!(format_tokens_k(52_000), format_tokens_compact(52_000));
    }
}
