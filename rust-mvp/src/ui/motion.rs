//! Small, shared motion primitives for active TUI labels.
//!
//! Motion here is presentation-only: it never changes the underlying text, so
//! snapshots, selection, and copy/paste remain deterministic.
//!
//! Port of `ui/motion.py` (46 lines). Callers (lanes panel, splash, transcript
//! working-label) overlay the returned `(index, token, bold)` cells on top of
//! their base styling for one frame.

use crate::model::blocks::StyleToken;

/// Soft-band cadence: quick enough to read as motion without busy redraws.
pub const SHIMMER_INTERVAL_SECONDS: f64 = 0.08;

/// Quiet cells after a band crosses a label before it loops.
pub const SHIMMER_GAP_CELLS: usize = 5;

/// A soft five-cell `shadow -> light -> peak -> light -> shadow` band.
const SHIMMER_BAND: [(isize, StyleToken, bool); 5] = [
    (-2, StyleToken::Fg, false),
    (-1, StyleToken::Bright, false),
    (0, StyleToken::Bright, true),
    (1, StyleToken::Bright, false),
    (2, StyleToken::Fg, false),
];

/// Return visible `(index, theme-token, bold)` cells for one frame.
///
/// Indices outside the label are clipped. During the quiet gap the result is
/// empty, leaving callers' base styling untouched.
pub fn shimmer_band(length: usize, frame: usize) -> Vec<(usize, StyleToken, bool)> {
    if length == 0 {
        return Vec::new();
    }
    let peak = frame % (length + SHIMMER_GAP_CELLS);
    if peak >= length {
        return Vec::new();
    }
    SHIMMER_BAND
        .iter()
        .filter_map(|&(offset, token, bold)| {
            let index = peak as isize + offset;
            (0 <= index && (index as usize) < length).then_some((index as usize, token, bold))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    //! No dedicated Python test file exists for `ui/motion.py`; every expected
    //! value below is pinned against the real Python oracle
    //! (`amplifier_app_newtui.ui.motion.shimmer_band`).

    use super::*;
    use StyleToken::{Bright, Fg};

    #[test]
    fn constants_match_python() {
        assert_eq!(SHIMMER_INTERVAL_SECONDS, 0.08);
        assert_eq!(SHIMMER_GAP_CELLS, 5);
    }

    #[test]
    fn zero_length_returns_empty() {
        // Python: shimmer_band(0, 0) == ()
        assert!(shimmer_band(0, 0).is_empty());
    }

    #[test]
    fn band_is_clipped_at_the_left_edge() {
        // Python: shimmer_band(10, 0) == ((0,'bright',True),(1,'bright',False),(2,'fg',False))
        assert_eq!(
            shimmer_band(10, 0),
            vec![(0, Bright, true), (1, Bright, false), (2, Fg, false)]
        );
        // Python: shimmer_band(10, 1) — one shadow cell still clipped
        assert_eq!(
            shimmer_band(10, 1),
            vec![(0, Bright, false), (1, Bright, true), (2, Bright, false), (3, Fg, false)]
        );
    }

    #[test]
    fn full_band_is_soft_shadow_light_peak_light_shadow() {
        // Python: shimmer_band(10, 2) — all five cells visible
        assert_eq!(
            shimmer_band(10, 2),
            vec![
                (0, Fg, false),
                (1, Bright, false),
                (2, Bright, true),
                (3, Bright, false),
                (4, Fg, false),
            ]
        );
    }

    #[test]
    fn band_is_clipped_at_the_right_edge() {
        // Python: shimmer_band(10, 9) == ((7,'fg',False),(8,'bright',False),(9,'bright',True))
        assert_eq!(
            shimmer_band(10, 9),
            vec![(7, Fg, false), (8, Bright, false), (9, Bright, true)]
        );
    }

    #[test]
    fn quiet_gap_yields_empty_frames_then_loops() {
        // Python: frames 10..=14 for length 10 are the quiet gap; frame 15 wraps to frame 0.
        assert!(shimmer_band(10, 10).is_empty());
        assert!(shimmer_band(10, 14).is_empty());
        assert_eq!(shimmer_band(10, 15), shimmer_band(10, 0));
    }

    #[test]
    fn short_labels_keep_only_in_range_cells() {
        // Python: shimmer_band(1, 0) == ((0,'bright',True),)
        assert_eq!(shimmer_band(1, 0), vec![(0, Bright, true)]);
        // Python: shimmer_band(2, 1) == ((0,'bright',False),(1,'bright',True))
        assert_eq!(shimmer_band(2, 1), vec![(0, Bright, false), (1, Bright, true)]);
    }

    #[test]
    fn large_frames_wrap_by_modulo() {
        // Python: shimmer_band(3, 42) — 42 % (3 + 5) == 2 → peak at the last cell
        assert_eq!(
            shimmer_band(3, 42),
            vec![(0, Fg, false), (1, Bright, false), (2, Bright, true)]
        );
    }
}
