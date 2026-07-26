//! `/context` usage computation → [`ContextBlock`] (DESIGN-SPEC §6/§10).
//!
//! Pure math: token counts in → [`ContextUsage`] →
//! [`crate::model::blocks::ContextBlock`] with the `████████░░` bar
//! segmented conversation / tools / memory / free. The mockup line:
//!
//! ```text
//! · Context  41% of 200k
//!   ████████░░░░░░░░░░░░  conversation 52k · tools 18k · memory 8k · free 118k
//! ```
//!
//! Port of `src/amplifier_app_newtui/commands/context.py`.

use std::fmt;

use crate::model::blocks::ContextBlock;
pub use crate::model::formatting::format_tokens_compact as format_tokens;

pub const DEFAULT_WINDOW_TOKENS: u64 = 200_000;
/// Bar cell count in the mockup's `/context` line (20 × 5% cells).
pub const DEFAULT_BAR_WIDTH: u32 = 20;

/// Python `ValueError` from [`ContextUsage`] validation — the message
/// text matches the original exactly.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContextValueError(pub String);

impl fmt::Display for ContextValueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ContextValueError {}

/// Token accounting for the active context window.
///
/// `conversation` / `tools` / `memory` are the used buckets in the order
/// the bar renders them; `window` is the model context window (200k
/// default per the spec header `NN% of 200k`).
///
/// Pydantic model was `frozen=True, extra="forbid"` with a
/// used-fits-window validator — construct via [`ContextUsage::new`] /
/// [`ContextUsage::with_window`] (which validate) and treat the fields
/// as immutable by crate convention.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ContextUsage {
    pub conversation: u64,
    pub tools: u64,
    pub memory: u64,
    pub window: u64,
}

impl Default for ContextUsage {
    /// The all-defaults Python `ContextUsage()`: empty usage, 200k window.
    fn default() -> Self {
        Self {
            conversation: 0,
            tools: 0,
            memory: 0,
            window: DEFAULT_WINDOW_TOKENS,
        }
    }
}

impl ContextUsage {
    /// `ContextUsage(conversation=…, tools=…, memory=…)` with the default
    /// 200k window.
    pub fn new(conversation: u64, tools: u64, memory: u64) -> Result<Self, ContextValueError> {
        Self::with_window(conversation, tools, memory, DEFAULT_WINDOW_TOKENS)
    }

    /// Full constructor mirroring the pydantic validators: `window` must
    /// be positive (`gt=0`) and the used total must fit the window.
    pub fn with_window(
        conversation: u64,
        tools: u64,
        memory: u64,
        window: u64,
    ) -> Result<Self, ContextValueError> {
        if window == 0 {
            // pydantic `gt=0` constraint message (a ValidationError in
            // Python, not a hand-raised ValueError).
            return Err(ContextValueError(
                "Input should be greater than 0".to_string(),
            ));
        }
        let usage = Self {
            conversation,
            tools,
            memory,
            window,
        };
        if usage.used() > usage.window {
            return Err(ContextValueError(format!(
                "used tokens ({}) exceed the context window ({})",
                usage.used(),
                usage.window
            )));
        }
        Ok(usage)
    }

    pub fn used(&self) -> u64 {
        self.conversation + self.tools + self.memory
    }

    pub fn free(&self) -> u64 {
        self.window - self.used()
    }

    /// Whole-number percentage for the `NN% of 200k` header.
    ///
    /// Python `round()` is banker's rounding (ties to even); `used`
    /// never exceeds `window` so the result fits `u8` (≤ 100).
    pub fn used_pct(&self) -> u8 {
        (self.used() as f64 / self.window as f64 * 100.0).round_ties_even() as u8
    }

    /// `200k` — the header's window figure.
    pub fn window_label(&self) -> String {
        format_tokens(self.window)
    }

    /// `Context  41% of 200k` (the `· ` glyph is the renderer's).
    pub fn header_text(&self) -> String {
        format!("Context  {}% of {}", self.used_pct(), self.window_label())
    }
}

/// Largest-remainder apportionment of `bar_width` cells over `values`.
///
/// Guarantees cells sum exactly to `bar_width` and any non-zero bucket
/// keeps at least one cell (so tiny-but-real usage stays visible).
fn bar_cells(values: &[u64], bar_width: u32) -> Vec<u32> {
    let total: u64 = values.iter().sum();
    if total == 0 {
        return vec![0; values.len()];
    }
    let exact: Vec<f64> = values
        .iter()
        .map(|&value| value as f64 / total as f64 * bar_width as f64)
        .collect();
    let mut cells: Vec<u32> = exact.iter().map(|&x| x.trunc() as u32).collect();
    // Non-zero buckets never render as zero cells.
    for (index, &value) in values.iter().enumerate() {
        if value > 0 && cells[index] == 0 {
            cells[index] = 1;
        }
    }
    // Reconcile to the exact bar width, adjusting the largest remainders
    // (shrink the biggest allocations first when over).
    while cells.iter().sum::<u32>() > bar_width {
        // Python `max(candidates, key=…)` keeps the FIRST maximal index.
        let mut largest: Option<usize> = None;
        for (i, &c) in cells.iter().enumerate() {
            let floor = if values[i] > 0 { 1 } else { 0 };
            if c > floor && largest.is_none_or(|best| c > cells[best]) {
                largest = Some(i);
            }
        }
        let Some(largest) = largest else { break };
        cells[largest] -= 1;
    }
    // `sorted(…, reverse=True)` is stable, as is Rust's `sort_by` —
    // remainder ties keep index order.
    let mut remainders: Vec<usize> = (0..values.len()).collect();
    remainders.sort_by(|&a, &b| {
        let frac = |i: usize| exact[i] - exact[i].trunc();
        frac(b).partial_cmp(&frac(a)).expect("fractions are finite")
    });
    let mut cursor = 0usize;
    while cells.iter().sum::<u32>() < bar_width && !remainders.is_empty() {
        cells[remainders[cursor % remainders.len()]] += 1;
        cursor += 1;
    }
    cells
}

/// `(label, cells)` pairs in conversation/tools/memory/free order.
///
/// Labels carry the mockup legend text (`conversation 52k`); cells sum
/// to `bar_width` (Python default: [`DEFAULT_BAR_WIDTH`]) for the
/// `████████░░` bar.
pub fn usage_segments(usage: &ContextUsage, bar_width: u32) -> Vec<(String, u32)> {
    let values = [usage.conversation, usage.tools, usage.memory, usage.free()];
    let names = ["conversation", "tools", "memory", "free"];
    let cells = bar_cells(&values, bar_width);
    names
        .iter()
        .zip(values.iter())
        .zip(cells)
        .map(|((name, &value), cell)| (format!("{name} {}", format_tokens(value)), cell))
        .collect()
}

/// Assemble the `/context` transcript block from a usage snapshot
/// (Python keyword default: `bar_width=DEFAULT_BAR_WIDTH`).
pub fn build_context_block(block_id: &str, usage: &ContextUsage, bar_width: u32) -> ContextBlock {
    ContextBlock {
        id: block_id.to_string(),
        used_pct: usage.used_pct(),
        window_label: usage.window_label(),
        segments: usage_segments(usage, bar_width),
        bar_width,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::blocks::TranscriptBlock;

    /// Pins `test_format_tokens`.
    #[test]
    fn test_format_tokens() {
        assert_eq!(format_tokens(742), "742");
        assert_eq!(format_tokens(4_100), "4.1k");
        assert_eq!(format_tokens(8_000), "8k");
        assert_eq!(format_tokens(52_000), "52k");
        assert_eq!(format_tokens(118_000), "118k");
        assert_eq!(format_tokens(200_000), "200k");
        assert_eq!(format_tokens(1_200_000), "1.2m");
    }

    /// Pins `test_usage_accounting`.
    #[test]
    fn test_usage_accounting() {
        let usage = ContextUsage::new(52_000, 18_000, 8_000).unwrap();
        assert_eq!(usage.used(), 78_000);
        assert_eq!(usage.free(), 122_000);
        assert_eq!(usage.used_pct(), 39);
        assert_eq!(usage.window_label(), "200k");
        assert_eq!(usage.header_text(), "Context  39% of 200k");
    }

    /// Pins `test_usage_rejects_overflow`.
    #[test]
    fn test_usage_rejects_overflow() {
        let err = ContextUsage::new(150_000, 60_000, 0).unwrap_err();
        assert_eq!(
            err.to_string(),
            "used tokens (210000) exceed the context window (200000)"
        );
    }

    /// Pins `test_segments_sum_to_bar_width_and_keep_order`.
    #[test]
    fn test_segments_sum_to_bar_width_and_keep_order() {
        let usage = ContextUsage::new(52_000, 18_000, 8_000).unwrap();
        let segments = usage_segments(&usage, 20);
        assert_eq!(segments.iter().map(|(_, cells)| cells).sum::<u32>(), 20);
        let names: Vec<&str> = segments
            .iter()
            .map(|(label, _)| label.split_whitespace().next().unwrap())
            .collect();
        assert_eq!(names, ["conversation", "tools", "memory", "free"]);
        // Non-zero buckets never vanish from the bar.
        assert!(segments.iter().all(|&(_, cells)| cells >= 1));
    }

    /// Pins `test_tiny_bucket_keeps_a_cell`.
    #[test]
    fn test_tiny_bucket_keeps_a_cell() {
        let usage = ContextUsage::new(180_000, 100, 100).unwrap();
        let segments = usage_segments(&usage, 10);
        assert_eq!(segments.iter().map(|(_, cells)| cells).sum::<u32>(), 10);
        let cells_for = |name: &str| {
            segments
                .iter()
                .find(|(label, _)| label.split_whitespace().next() == Some(name))
                .map(|&(_, cells)| cells)
                .unwrap()
        };
        assert!(cells_for("tools") >= 1);
        assert!(cells_for("memory") >= 1);
    }

    /// Pins `test_empty_usage_is_all_free`.
    #[test]
    fn test_empty_usage_is_all_free() {
        let usage = ContextUsage::default();
        let segments = usage_segments(&usage, 10);
        assert_eq!(segments.last().unwrap(), &("free 200k".to_string(), 10));
        assert_eq!(usage.used_pct(), 0);
    }

    /// Extra (no Python counterpart): cell-exact oracle rows captured from
    /// the real Python `usage_segments`, including float-tie-break cases
    /// (e.g. 52k/18k/8k gives memory — not tools — the remainder cell
    /// because the binary fractions differ in the last ulp). The Rust port
    /// runs the identical float ops in the identical order, so the doubles
    /// — and therefore the apportionment — match bit-for-bit.
    #[test]
    fn oracle_segments_match_python_exactly() {
        #[allow(clippy::type_complexity)]
        let cases: [(u64, u64, u64, u32, u8, [(&str, u32); 4]); 9] = [
            (
                52_000,
                18_000,
                8_000,
                20,
                39,
                [
                    ("conversation 52k", 5),
                    ("tools 18k", 1),
                    ("memory 8k", 2),
                    ("free 122k", 12),
                ],
            ),
            (
                180_000,
                100,
                100,
                10,
                90,
                [
                    ("conversation 180k", 7),
                    ("tools 100", 1),
                    ("memory 100", 1),
                    ("free 20k", 1),
                ],
            ),
            (
                0,
                0,
                0,
                10,
                0,
                [
                    ("conversation 0", 0),
                    ("tools 0", 0),
                    ("memory 0", 0),
                    ("free 200k", 10),
                ],
            ),
            (
                1,
                1,
                1,
                3,
                0,
                [
                    ("conversation 1", 1),
                    ("tools 1", 1),
                    ("memory 1", 1),
                    ("free 200k", 1),
                ],
            ),
            (
                99_999,
                99_999,
                1,
                20,
                100,
                [
                    ("conversation 100k", 9),
                    ("tools 100k", 9),
                    ("memory 1", 1),
                    ("free 1", 1),
                ],
            ),
            (
                66_666,
                66_667,
                66_667,
                7,
                100,
                [
                    ("conversation 67k", 2),
                    ("tools 67k", 3),
                    ("memory 67k", 2),
                    ("free 0", 0),
                ],
            ),
            (
                12_345,
                6_789,
                101,
                20,
                10,
                [
                    ("conversation 12k", 1),
                    ("tools 6.8k", 1),
                    ("memory 101", 1),
                    ("free 181k", 17),
                ],
            ),
            (
                100,
                0,
                0,
                5,
                0,
                [
                    ("conversation 100", 1),
                    ("tools 0", 0),
                    ("memory 0", 0),
                    ("free 200k", 4),
                ],
            ),
            (
                742,
                4_100,
                8_000,
                20,
                6,
                [
                    ("conversation 742", 1),
                    ("tools 4.1k", 1),
                    ("memory 8k", 1),
                    ("free 187k", 17),
                ],
            ),
        ];
        for (conversation, tools, memory, bar_width, used_pct, expected) in cases {
            let usage = ContextUsage::new(conversation, tools, memory).unwrap();
            assert_eq!(
                usage.used_pct(),
                used_pct,
                "used_pct for {conversation}/{tools}/{memory}"
            );
            let expected: Vec<(String, u32)> = expected
                .iter()
                .map(|&(label, cells)| (label.to_string(), cells))
                .collect();
            assert_eq!(
                usage_segments(&usage, bar_width),
                expected,
                "segments for {conversation}/{tools}/{memory} @ {bar_width}"
            );
        }
    }

    /// Pins `test_build_context_block`.
    #[test]
    fn test_build_context_block() {
        let usage = ContextUsage::new(52_000, 18_000, 8_000).unwrap();
        let block = build_context_block("b7", &usage, DEFAULT_BAR_WIDTH);
        assert_eq!(block.id, "b7");
        assert_eq!(TranscriptBlock::from(block.clone()).kind(), "context");
        assert_eq!(block.used_pct, 39);
        assert_eq!(block.window_label, "200k");
        assert_eq!(block.bar_width, 20);
        assert_eq!(
            block.segments.iter().map(|(_, cells)| cells).sum::<u32>(),
            20
        );
        assert_eq!(block.segments.first().unwrap().0, "conversation 52k");
        assert_eq!(block.segments.last().unwrap().0, "free 122k");
    }
}
