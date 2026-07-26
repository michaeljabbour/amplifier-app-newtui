//! Boot splash: the AMPLIFIER wordmark drawn over the empty transcript.
//!
//! Port of `ui/splash.py` (275 lines).
//!
//! Module prepare can run for minutes on a cold cache; instead of a lone dim
//! line in an empty screen, the splash draws the wordmark with a left→right
//! scan (sweep), holds it with the shared shimmer band while foundation
//! reports install phases beneath it, and dissolves character-by-character
//! the moment the session banner is ready (`clear_boot_progress`).
//!
//! Presentation-only, like `ui/motion.rs`: every frame is a pure function of
//! (art, frame) returning [`Line`] rows styled ONLY by DESIGN-SPEC §1 theme
//! tokens — no colors here, and the dissolve order comes from a fixed seed so
//! frames stay deterministic. Python seeds `random.Random` (MT19937); the
//! private [`Mt19937`] below reproduces CPython's generator bit-for-bit so
//! `decay_grid` yields the *same* grid as the Python oracle.
//!
//! Textual widget mechanics (mount/remove/timers/`theme_variables` paint) do
//! not port; [`BootSplash`] keeps the pure lifecycle state machine
//! (sweep → hold → dissolve, dismiss semantics, spinner/status rows) and the
//! app-assembly layer drives it from its own frame timer, bridging rows via
//! `segments::to_ratatui_line`.

use std::collections::HashMap;
use std::sync::OnceLock;

use crate::model::blocks::{Segment, StyleToken, GLYPH_SPINNER_FRAMES};
use crate::ui::motion::shimmer_band;
use crate::ui::segments::Line;

/// Splash frame cadence — smooth motion, trivially cheap repaints.
pub const FRAME_SECONDS: f64 = 1.0 / 20.0;

/// Scan-edge speed: the 55-col wordmark draws on in under a second.
pub const SWEEP_COLS_PER_FRAME: usize = 4;

/// Per-cell decay starts are spread over this many frames.
pub const DISSOLVE_SPREAD_FRAMES: usize = 6;

/// Frames a decaying cell lingers as `·` before clearing.
pub const DISSOLVE_DOT_FRAMES: usize = 2;

/// Fixed seed: the dissolve order is decorative, determinism is load-bearing
/// (frames must be reproducible for tests and resumable repaints).
pub const DISSOLVE_SEED: u32 = 0x0A3D;

/// Splash frames per status-spinner glyph (~260ms, matching the title bar).
pub const SPINNER_TICKS: usize = 5;

const EDGE_GLYPHS: [char; 3] = ['░', '▒', '▓'];
const EDGE_WIDTH: usize = 3;

const WORDMARK_RAW: [&str; 5] = [
    r"    ___    __  _______  __    ________________________",
    r"   /   |  /  |/  / __ \/ /   /  _/ ____/  _/ ____/ __ \ ",
    r"  / /| | / /|_/ / /_/ / /    / // /_   / // __/ / /_/ /",
    r" / ___ |/ /  / / ____/ /____/ // __/ _/ // /___/ _, _/ ",
    r"/_/  |_/_/  /_/_/   /_____/___/_/   /___/_____/_/ |_|  ",
];

const FALLBACK_RAW: [&str; 1] = ["A M P L I F I E R"];

fn padded(art: &[&str]) -> Vec<String> {
    let width = art
        .iter()
        .map(|line| line.trim_end().chars().count())
        .max()
        .expect("art has at least one line");
    art.iter()
        .map(|line| {
            let stripped = line.trim_end();
            let pad = width - stripped.chars().count();
            format!("{stripped}{}", " ".repeat(pad))
        })
        .collect()
}

/// The padded AMPLIFIER wordmark (Python `WORDMARK`), rectangular.
pub fn wordmark() -> &'static [String] {
    static WORDMARK: OnceLock<Vec<String>> = OnceLock::new();
    WORDMARK.get_or_init(|| padded(&WORDMARK_RAW))
}

/// The single-row fallback (Python `FALLBACK`) for tiny terminals.
pub fn fallback() -> &'static [String] {
    static FALLBACK: OnceLock<Vec<String>> = OnceLock::new();
    FALLBACK.get_or_init(|| padded(&FALLBACK_RAW))
}

/// Per-cell dissolve start frames, one row per art row.
pub type DecayGrid = Vec<Vec<u32>>;

type Cell = (char, StyleToken, bool);

/// The wordmark when it fits (art + status rows), else the plain row.
pub fn art_for(width: usize, height: usize) -> &'static [String] {
    let mark = wordmark();
    if width >= mark[0].chars().count() + 2 && height >= mark.len() + 4 {
        return mark;
    }
    fallback()
}

/// Adjacent same-styled cells collapse into one Segment.
fn merged(cells: &[Cell]) -> Line {
    let mut segments: Vec<Segment> = Vec::new();
    for &(ch, token, bold) in cells {
        match segments.last_mut() {
            Some(last) if last.style_token == token && last.bold == bold => last.text.push(ch),
            _ => segments.push(Segment {
                style_token: token,
                bold,
                ..Segment::new(ch.to_string())
            }),
        }
    }
    segments
}

/// Draw-on: revealed columns in orange behind a bright noise edge.
///
/// Returns `None` once the scan has crossed the full width (sweep done).
pub fn sweep_frame(art: &[String], frame: usize) -> Option<Vec<Line>> {
    let width = art[0].chars().count();
    let reveal = frame * SWEEP_COLS_PER_FRAME;
    if reveal >= width {
        return None;
    }
    let mut lines: Vec<Line> = Vec::new();
    for (row, text) in art.iter().enumerate() {
        let mut cells: Vec<Cell> = text
            .chars()
            .take(reveal)
            .map(|ch| (ch, StyleToken::Orange, false))
            .collect();
        for col in reveal..(reveal + EDGE_WIDTH).min(width) {
            let glyph = EDGE_GLYPHS[(row + col + frame) % EDGE_GLYPHS.len()];
            cells.push((glyph, StyleToken::Bright, true));
        }
        lines.push(merged(&cells));
    }
    Some(lines)
}

/// Idle: the full wordmark with the shared shimmer band drifting across.
///
/// Plain text never changes (`line_plain` equals the art), so selection
/// and copy stay stable while packages install — same rule as motion.rs.
pub fn hold_frame(art: &[String], frame: usize) -> Vec<Line> {
    let band: HashMap<usize, (StyleToken, bool)> = shimmer_band(art[0].chars().count(), frame)
        .into_iter()
        .map(|(index, token, bold)| (index, (token, bold)))
        .collect();
    let mut lines: Vec<Line> = Vec::new();
    for text in art {
        let mut cells: Vec<Cell> = Vec::new();
        for (col, ch) in text.chars().enumerate() {
            let (mut token, mut bold) = band
                .get(&col)
                .copied()
                .unwrap_or((StyleToken::Orange, false));
            if ch == ' ' {
                (token, bold) = (StyleToken::Orange, false); // spaces carry no visible style
            }
            cells.push((ch, token, bold));
        }
        lines.push(merged(&cells));
    }
    lines
}

/// Per-cell dissolve start frames (fixed seed → deterministic order).
pub fn decay_grid(art: &[String]) -> DecayGrid {
    decay_grid_with_seed(art, DISSOLVE_SEED)
}

/// [`decay_grid`] with an explicit seed (Python's `seed=` keyword).
pub fn decay_grid_with_seed(art: &[String], seed: u32) -> DecayGrid {
    let mut rng = Mt19937::new(seed);
    art.iter()
        .map(|line| {
            line.chars()
                .map(|_| rng.randint(0, DISSOLVE_SPREAD_FRAMES as u32))
                .collect()
        })
        .collect()
}

/// Melt-out: each cell decays `char → · → space` on its own schedule.
///
/// Returns `None` once every cell has cleared (remove the widget).
pub fn dissolve_frame(art: &[String], grid: &DecayGrid, frame: usize) -> Option<Vec<Line>> {
    if frame > DISSOLVE_SPREAD_FRAMES + DISSOLVE_DOT_FRAMES {
        return None;
    }
    let mut lines: Vec<Line> = Vec::new();
    for (row, text) in art.iter().enumerate() {
        let mut cells: Vec<Cell> = Vec::new();
        for (col, ch) in text.chars().enumerate() {
            let age = frame as i64 - i64::from(grid[row][col]);
            if ch == ' ' || age > DISSOLVE_DOT_FRAMES as i64 {
                cells.push((' ', StyleToken::Dimmer, false));
            } else if age < 0 {
                cells.push((ch, StyleToken::Orange, false));
            } else {
                cells.push(('·', StyleToken::Dimmer, false));
            }
        }
        lines.push(merged(&cells));
    }
    Some(lines)
}

/// The boot-phase line, hand-centered under the wordmark.
///
/// Centering is done with pad segments (not text-align) so the status row
/// and the art rows share one coordinate system whatever alignment does.
pub fn status_line(art_width: usize, status: &str, spinner_glyph: &str) -> Line {
    let status_len = status.chars().count();
    let text: String = if status_len <= art_width.saturating_sub(2) {
        status.to_string()
    } else {
        let mut truncated: String = status.chars().take(art_width.saturating_sub(3)).collect();
        truncated.push('…');
        truncated
    };
    let pad = (art_width.max(2) - 2).saturating_sub(text.chars().count()) / 2;
    vec![
        Segment {
            style_token: StyleToken::Orange,
            ..Segment::new(format!("{}{spinner_glyph} ", " ".repeat(pad)))
        },
        Segment {
            style_token: StyleToken::Dim,
            ..Segment::new(text)
        },
    ]
}

/// Splash animation phase (Python's `_phase` string).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Sweep,
    Hold,
    Dissolve,
}

/// What one timer tick produced (return value of [`BootSplash::advance`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Advance {
    /// Not laid out yet (`size.width <= 0` in Python) — paint nothing.
    NotLaidOut,
    /// A frame was produced; repaint [`BootSplash::rows`].
    Painted,
    /// The dissolve finished — stop the timer and remove the widget.
    Remove,
}

/// The splash overlay's pure state machine; owned by the app for the boot
/// window only.
///
/// Lifecycle: created on the first `boot_progress` call, fed phase text via
/// [`set_status`](Self::set_status), and dismissed by `clear_boot_progress` —
/// dissolving on a normal ready, instantly on boot failure (the error text
/// must not sit under a melting wordmark).
///
/// Widget mechanics stay with the host: it runs the [`FRAME_SECONDS`] timer
/// (calling [`advance`](Self::advance) with the laid-out size), removes the
/// overlay when told to, and paints [`rows`](Self::rows) through the theme's
/// token→color table.
#[derive(Debug, Default)]
pub struct BootSplash {
    status: String,
    phase: Option<Phase>,
    frame: usize,
    tick: usize,
    art: Option<&'static [String]>,
    grid: Option<DecayGrid>,
    lines: Vec<Line>,
    dismissed: bool,
}

impl BootSplash {
    pub fn new() -> Self {
        Self {
            phase: Some(Phase::Sweep),
            ..Self::default()
        }
    }

    fn phase(&self) -> Phase {
        self.phase.unwrap_or(Phase::Sweep)
    }

    /// Store the boot-phase text; returns `true` when a repaint is due
    /// (Python repaints immediately if any frame has already been drawn).
    pub fn set_status(&mut self, text: &str) -> bool {
        self.status = text.to_string();
        !self.lines.is_empty()
    }

    /// Start the dissolve; `immediate` skips straight to removal.
    ///
    /// Returns `true` when the host must stop the timer and remove the
    /// overlay right now (immediate dismissal, or dismissal before layout).
    pub fn dismiss_splash(&mut self, immediate: bool) -> bool {
        if self.dismissed && !immediate {
            return false;
        }
        self.dismissed = true;
        if immediate || self.art.is_none() {
            return true;
        }
        if self.phase() != Phase::Dissolve {
            self.grid = Some(decay_grid(self.art.expect("checked above")));
            self.phase = Some(Phase::Dissolve);
            self.frame = 0;
        }
        false
    }

    /// One frame-timer tick (Python `_advance`), given the laid-out size.
    pub fn advance(&mut self, width: usize, height: usize) -> Advance {
        self.tick += 1;
        if self.art.is_none() {
            if width == 0 {
                return Advance::NotLaidOut; // not laid out yet
            }
            self.art = Some(art_for(width, height));
        }
        let art = self.art.expect("assigned above");
        let frame = match self.phase() {
            Phase::Sweep => match sweep_frame(art, self.frame) {
                Some(frame) => frame,
                None => {
                    self.phase = Some(Phase::Hold);
                    self.frame = 0;
                    hold_frame(art, 0)
                }
            },
            Phase::Hold => hold_frame(art, self.frame),
            Phase::Dissolve => {
                let grid = self.grid.as_ref().expect("dissolve phase set the grid");
                match dissolve_frame(art, grid, self.frame) {
                    Some(frame) => frame,
                    None => return Advance::Remove,
                }
            }
        };
        self.frame += 1;
        self.lines = frame;
        Advance::Painted
    }

    /// The rows to paint (Python `_paint` sans Rich/theme plumbing): the
    /// current frame plus, outside the dissolve, a blank row and the
    /// spinner-prefixed status line.
    pub fn rows(&self) -> Vec<Line> {
        let mut rows = self.lines.clone();
        if !self.status.is_empty() && self.phase() != Phase::Dissolve {
            if let Some(art) = self.art {
                let glyph =
                    GLYPH_SPINNER_FRAMES[(self.tick / SPINNER_TICKS) % GLYPH_SPINNER_FRAMES.len()];
                rows.push(Vec::new());
                rows.push(status_line(art[0].chars().count(), &self.status, glyph));
            }
        }
        rows
    }

    /// The stored status text (test/introspection hook; Python `_status`).
    pub fn status(&self) -> &str {
        &self.status
    }

    /// Whether a dismissal has been requested (Python `_dismissed`).
    pub fn is_dismissed(&self) -> bool {
        self.dismissed
    }
}

/// CPython's `random.Random` core: MT19937 seeded via `init_by_array`, with
/// `getrandbits`-based rejection sampling for `randint` — bit-for-bit the
/// generator behind Python's `random.Random(seed)`.
struct Mt19937 {
    mt: [u32; 624],
    index: usize,
}

impl Mt19937 {
    /// `random.Random(seed)` for a non-negative int seed that fits in u32
    /// (the key array is the seed's single 32-bit digit).
    fn new(seed: u32) -> Self {
        let key = [seed];
        let mut mt = [0u32; 624];
        // init_genrand(19650218)
        mt[0] = 19650218;
        for i in 1..624 {
            mt[i] = 1812433253u32
                .wrapping_mul(mt[i - 1] ^ (mt[i - 1] >> 30))
                .wrapping_add(i as u32);
        }
        // init_by_array(key)
        let (mut i, mut j) = (1usize, 0usize);
        for _ in 0..624.max(key.len()) {
            mt[i] = (mt[i] ^ (mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_mul(1664525))
                .wrapping_add(key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= 624 {
                mt[0] = mt[623];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
        }
        for _ in 0..623 {
            mt[i] = (mt[i] ^ (mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_mul(1566083941))
                .wrapping_sub(i as u32);
            i += 1;
            if i >= 624 {
                mt[0] = mt[623];
                i = 1;
            }
        }
        mt[0] = 0x8000_0000;
        Self { mt, index: 624 }
    }

    fn genrand_u32(&mut self) -> u32 {
        if self.index >= 624 {
            for i in 0..624 {
                let y = (self.mt[i] & 0x8000_0000) | (self.mt[(i + 1) % 624] & 0x7fff_ffff);
                let mut next = self.mt[(i + 397) % 624] ^ (y >> 1);
                if y & 1 != 0 {
                    next ^= 0x9908_b0df;
                }
                self.mt[i] = next;
            }
            self.index = 0;
        }
        let mut y = self.mt[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^= y >> 18;
        y
    }

    /// `getrandbits(k)` for `1 <= k <= 32`.
    fn getrandbits(&mut self, k: u32) -> u32 {
        self.genrand_u32() >> (32 - k)
    }

    /// `_randbelow_with_getrandbits(n)`: rejection-sample `n.bit_length()` bits.
    fn randbelow(&mut self, n: u32) -> u32 {
        let k = 32 - n.leading_zeros();
        loop {
            let r = self.getrandbits(k);
            if r < n {
                return r;
            }
        }
    }

    /// `randint(a, b)` — inclusive on both ends.
    fn randint(&mut self, a: u32, b: u32) -> u32 {
        a + self.randbelow(b - a + 1)
    }
}

#[cfg(test)]
mod tests {
    //! Pins tests/test_ui_splash.py — the pure frame functions. The three
    //! Pilot lifecycle tests (widget mount/dismiss/removal through Textual's
    //! message pump) do not port; the state-machine tests at the bottom cover
    //! their pure core against the same contract.

    use super::*;
    use crate::ui::segments::line_plain;

    /// §1 tokens the splash may reference (fg comes from the shimmer band).
    const SPLASH_TOKENS: [StyleToken; 5] = [
        StyleToken::Orange,
        StyleToken::Bright,
        StyleToken::Fg,
        StyleToken::Dim,
        StyleToken::Dimmer,
    ];

    // -- pure frame functions --------------------------------------------------------

    // Python: test_wordmark_is_rectangular_and_wide
    #[test]
    fn test_wordmark_is_rectangular_and_wide() {
        let widths: std::collections::HashSet<usize> =
            wordmark().iter().map(|line| line.chars().count()).collect();
        assert_eq!(widths.len(), 1);
        assert_eq!(wordmark().len(), 5);
        assert_eq!(widths, std::collections::HashSet::from([55]));
    }

    // Python: test_art_for_picks_wordmark_when_it_fits_else_fallback
    #[test]
    fn test_art_for_picks_wordmark_when_it_fits_else_fallback() {
        assert!(std::ptr::eq(art_for(110, 30), wordmark()));
        assert!(std::ptr::eq(art_for(50, 30), fallback())); // too narrow
        assert!(std::ptr::eq(art_for(110, 6), fallback())); // too short
        assert!(std::ptr::eq(art_for(20, 3), fallback()));
    }

    // Python: test_sweep_reveals_left_to_right_and_finishes
    #[test]
    fn test_sweep_reveals_left_to_right_and_finishes() {
        let mark = wordmark();
        let width = mark[0].chars().count();
        let first = sweep_frame(mark, 0).expect("frame 0 is drawn");
        // Frame 0: nothing revealed yet — only the bright noise edge.
        assert!(first
            .iter()
            .all(|line| line.iter().all(|s| s.style_token == StyleToken::Bright)));
        let mid = sweep_frame(mark, 5).expect("frame 5 is drawn");
        let reveal = 5 * SWEEP_COLS_PER_FRAME;
        for (row, line) in mid.iter().enumerate() {
            let plain = line_plain(line);
            let revealed: String = plain.chars().take(reveal).collect();
            let expected: String = mark[row].chars().take(reveal).collect();
            assert_eq!(revealed, expected); // revealed art is verbatim
            assert_eq!(plain.chars().count(), reveal + 3); // plus the three-cell edge
        }
        let frames_needed = width.div_ceil(SWEEP_COLS_PER_FRAME);
        assert!(sweep_frame(mark, frames_needed).is_none());
    }

    // Python: test_hold_frame_never_changes_plain_text
    #[test]
    fn test_hold_frame_never_changes_plain_text() {
        // Motion is style-only (ui/motion.rs rule): copy/selection stay stable.
        let mark = wordmark();
        for frame in (0..80).step_by(7) {
            let held = hold_frame(mark, frame);
            let plains: Vec<String> = held.iter().map(|line| line_plain(line)).collect();
            assert_eq!(plains, mark.to_vec());
        }
    }

    // Python: test_hold_frame_shimmer_moves
    #[test]
    fn test_hold_frame_shimmer_moves() {
        let styles = |frame: usize| -> Vec<(StyleToken, bool)> {
            hold_frame(wordmark(), frame)
                .iter()
                .flat_map(|line| line.iter().map(|s| (s.style_token, s.bold)))
                .collect()
        };
        assert_ne!(styles(2), styles(6)); // the band actually drifts
    }

    // Python: test_dissolve_is_deterministic_and_reaches_empty
    #[test]
    fn test_dissolve_is_deterministic_and_reaches_empty() {
        let mark = wordmark();
        let grid = decay_grid(mark);
        assert_eq!(grid, decay_grid(mark)); // fixed seed
        let last = DISSOLVE_SPREAD_FRAMES + DISSOLVE_DOT_FRAMES;
        let art_chars: std::collections::HashSet<char> =
            mark.iter().flat_map(|line| line.chars()).collect();
        let allowed: std::collections::HashSet<char> =
            art_chars.union(&['·', ' '].into()).copied().collect();
        for frame in 0..=last {
            let rows = dissolve_frame(mark, &grid, frame).expect("frame within the window");
            for line in &rows {
                assert!(line_plain(line).chars().all(|ch| allowed.contains(&ch)));
            }
        }
        let final_rows = dissolve_frame(mark, &grid, last).expect("last frame is drawn");
        // By the last frame every surviving cell is a dot or blank — no art left.
        assert!(final_rows
            .iter()
            .flat_map(|line| line_plain(line).chars().collect::<Vec<_>>())
            .all(|ch| ch == '·' || ch == ' '));
        assert!(dissolve_frame(mark, &grid, last + 1).is_none());
    }

    // Python: test_all_frames_use_theme_tokens_only
    #[test]
    fn test_all_frames_use_theme_tokens_only() {
        let mark = wordmark();
        let grid = decay_grid(mark);
        let frames = [
            sweep_frame(mark, 3).expect("sweep frame 3"),
            hold_frame(mark, 12),
            dissolve_frame(mark, &grid, 4).expect("dissolve frame 4"),
        ];
        for rows in &frames {
            for line in rows {
                assert!(line.iter().all(|s| SPLASH_TOKENS.contains(&s.style_token)));
            }
        }
    }

    // Python: test_status_line_centers_and_truncates
    #[test]
    fn test_status_line_centers_and_truncates() {
        let width = wordmark()[0].chars().count();
        let line = status_line(width, "installing · amplifier-foundation", "✳");
        let plain = line_plain(&line);
        assert!(plain.contains("✳ installing · amplifier-foundation"));
        assert!(plain.starts_with(' ')); // centered under the wordmark
        let long = status_line(width, &"x".repeat(200), "✳");
        assert!(line_plain(&long).chars().count() <= width + 2);
    }

    // -- oracle pins beyond the Python file --------------------------------------------

    /// Pins the exact MT19937 stream against CPython:
    /// `[random.Random(0x0A3D).randint(0, 6) for _ in range(12)]` and the
    /// raw `getrandbits(3)` stream showing the rejected value 7.
    #[test]
    fn mt19937_matches_cpython_random() {
        let mut rng = Mt19937::new(DISSOLVE_SEED);
        let ints: Vec<u32> = (0..12).map(|_| rng.randint(0, 6)).collect();
        assert_eq!(ints, vec![3, 2, 6, 1, 0, 3, 3, 3, 3, 6, 0, 5]);
        let mut raw = Mt19937::new(DISSOLVE_SEED);
        let bits: Vec<u32> = (0..12).map(|_| raw.getrandbits(3)).collect();
        assert_eq!(bits, vec![3, 2, 6, 1, 7, 0, 3, 3, 3, 3, 6, 0]);
    }

    /// Pins the full decay grid byte-for-byte against the Python oracle
    /// (`splash.decay_grid(splash.WORDMARK)`), so dissolve frames are
    /// identical across the two implementations.
    #[test]
    fn decay_grid_matches_python_oracle_exactly() {
        let grid = decay_grid(wordmark());
        let expected: DecayGrid = vec![
            vec![
                3, 2, 6, 1, 0, 3, 3, 3, 3, 6, 0, 5, 0, 4, 3, 5, 2, 3, 3, 5, 2, 4, 3, 4, 0, 2, 6,
                2, 2, 5, 5, 5, 1, 3, 5, 6, 5, 1, 2, 1, 6, 5, 4, 3, 0, 4, 2, 6, 6, 2, 1, 5, 2, 1, 4,
            ],
            vec![
                2, 5, 4, 2, 1, 5, 2, 0, 2, 3, 1, 3, 2, 4, 3, 5, 2, 1, 5, 6, 6, 6, 5, 1, 6, 0, 5,
                2, 3, 4, 2, 2, 3, 0, 0, 6, 2, 4, 5, 4, 0, 5, 1, 2, 3, 2, 1, 2, 5, 3, 4, 1, 5, 2, 0,
            ],
            vec![
                0, 4, 0, 2, 6, 3, 6, 4, 3, 0, 4, 3, 2, 0, 0, 6, 6, 2, 5, 3, 6, 3, 5, 0, 5, 5, 2,
                2, 3, 5, 4, 0, 4, 5, 6, 4, 3, 6, 2, 3, 2, 6, 2, 1, 0, 2, 2, 0, 1, 5, 5, 6, 2, 1, 6,
            ],
            vec![
                1, 0, 4, 2, 4, 4, 2, 4, 5, 6, 3, 3, 0, 3, 0, 5, 1, 0, 3, 2, 4, 3, 3, 1, 5, 2, 0,
                6, 1, 6, 4, 3, 1, 1, 3, 6, 0, 1, 6, 1, 4, 0, 3, 0, 4, 0, 4, 3, 2, 5, 4, 0, 2, 2, 5,
            ],
            vec![
                3, 5, 6, 6, 3, 1, 0, 5, 4, 4, 5, 2, 4, 2, 6, 1, 5, 4, 3, 4, 1, 4, 5, 0, 5, 4, 1,
                5, 5, 0, 6, 0, 4, 3, 4, 6, 5, 3, 2, 0, 6, 2, 1, 0, 3, 2, 6, 1, 2, 1, 3, 0, 3, 4, 1,
            ],
        ];
        assert_eq!(grid, expected);
    }

    /// Oracle pins for merged-segment shapes (from the Python oracle):
    ///   sweep_frame(WORDMARK, 0)[0]  == [('░▒▓', bright, bold)]
    ///   sweep_frame(WORDMARK, 5)[0]  == [(20 revealed chars, orange), ('▒▓░', bright, bold)]
    ///   hold_frame(WORDMARK, 2)[0]   == ['    ' orange, '_' fg, rest orange]
    ///   status_line pads with 10 spaces before the spinner glyph.
    #[test]
    fn frame_segments_match_python_oracle() {
        let mark = wordmark();
        let flat = |line: &Line| -> Vec<(String, StyleToken, bool)> {
            line.iter()
                .map(|s| (s.text.clone(), s.style_token, s.bold))
                .collect()
        };
        let f0 = sweep_frame(mark, 0).expect("frame 0");
        assert_eq!(
            flat(&f0[0]),
            vec![("░▒▓".to_string(), StyleToken::Bright, true)]
        );
        let f5 = sweep_frame(mark, 5).expect("frame 5");
        assert_eq!(
            flat(&f5[0]),
            vec![
                ("    ___    __  _____".to_string(), StyleToken::Orange, false),
                ("▒▓░".to_string(), StyleToken::Bright, true),
            ]
        );
        let h2 = hold_frame(mark, 2);
        assert_eq!(
            flat(&h2[0]),
            vec![
                ("    ".to_string(), StyleToken::Orange, false),
                ("_".to_string(), StyleToken::Fg, false),
                (
                    "__    __  _______  __    ________________________ ".to_string(),
                    StyleToken::Orange,
                    false
                ),
            ]
        );
        let status = status_line(55, "installing · amplifier-foundation", "✳");
        assert_eq!(
            flat(&status),
            vec![
                ("          ✳ ".to_string(), StyleToken::Orange, false),
                (
                    "installing · amplifier-foundation".to_string(),
                    StyleToken::Dim,
                    false
                ),
            ]
        );
    }

    // -- state machine (pure core of the skipped Pilot lifecycle tests) ---------------

    #[test]
    fn state_machine_sweeps_then_holds_then_dissolves_to_removal() {
        let mut splash = BootSplash::new();
        assert!(!splash.set_status("installing · amplifier-foundation")); // no frame yet
        // Not laid out: advancing paints nothing (Python's size.width <= 0 guard).
        assert_eq!(splash.advance(0, 0), Advance::NotLaidOut);
        // Sweep crosses the wordmark, then hands off to hold seamlessly.
        let frames_needed = 55usize.div_ceil(SWEEP_COLS_PER_FRAME);
        for _ in 0..=frames_needed {
            assert_eq!(splash.advance(110, 40), Advance::Painted);
        }
        // Held frames keep the art's plain text; the status row is appended.
        let rows = splash.rows();
        assert_eq!(rows.len(), wordmark().len() + 2);
        assert!(line_plain(rows.last().expect("status row"))
            .contains("installing · amplifier-foundation"));
        // Normal dismissal dissolves rather than removing outright…
        assert!(!splash.dismiss_splash(false));
        assert!(splash.is_dismissed());
        // …a second non-immediate dismissal is a no-op…
        assert!(!splash.dismiss_splash(false));
        // …and the status row disappears during the dissolve.
        assert_eq!(splash.advance(110, 40), Advance::Painted);
        assert_eq!(splash.rows().len(), wordmark().len());
        // The dissolve runs its fixed window, then asks for removal.
        let mut outcome = Advance::Painted;
        for _ in 0..=(DISSOLVE_SPREAD_FRAMES + DISSOLVE_DOT_FRAMES + 1) {
            outcome = splash.advance(110, 40);
            if outcome == Advance::Remove {
                break;
            }
        }
        assert_eq!(outcome, Advance::Remove);
    }

    #[test]
    fn immediate_or_prelayout_dismissal_removes_without_dissolve() {
        // Boot failure: remove instantly — no melting wordmark over the error.
        let mut failing = BootSplash::new();
        failing.advance(110, 40);
        assert!(failing.dismiss_splash(true));
        // Dismissed before layout (instant boot): removal, not dissolve.
        let mut instant = BootSplash::new();
        assert!(instant.dismiss_splash(false));
        // Even after a soft dismissal, an immediate one still forces removal.
        let mut escalated = BootSplash::new();
        escalated.advance(110, 40);
        assert!(!escalated.dismiss_splash(false));
        assert!(escalated.dismiss_splash(true));
    }
}
