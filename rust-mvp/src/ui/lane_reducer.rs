//! Agent-lane presentation state: live tail + focused-lane transcripts.
//!
//! Port of `src/amplifier_app_newtui/ui/lane_reducer.py`.
//!
//! Extracted from the Python `TranscriptReducer` along the lane seam added
//! in PRs #13/#17. This unit owns the lane-scoped state that the turn
//! reducer used to carry inline:
//!
//! - the per-lane live-tail buffer (DESIGN-SPEC §8, design doc D4) with its
//!   accumulate-then-notify throttle and root-stream preemption, and
//! - the real-runtime focused-lane transcripts (DESIGN-SPEC §8) that child
//!   events (diverted from the root transcript by the foreign-turn rule)
//!   accumulate into so lane focus can replay a subagent's own work.
//!
//! The turn reducer still projects diverted child events onto lanes and
//! decides *when* lane activity changes; this unit owns *what* the lane
//! remembers and speaks to the app through the same narrow lane callbacks
//! (`lane_tail_updated` / `lane_tail_cleared`). Keeping the state here
//! makes lane behavior unit-testable with a fake host in isolation.

use std::collections::HashMap;
use std::time::Instant;

use crate::kernel::events as ev;
use crate::model::blocks::{BlockIdAllocator, SessionBanner, TranscriptBlock, UserLine};
use crate::model::lanes::{LaneRecord, LaneRegistry};
use crate::ui::needs_you::focused_lane_banner;

/// Lane-tail repaint floor — mirrors `_DELTA_NOTIFY_SECONDS` in
/// `kernel/trackers/stream_status.py`. The per-lane buffer accumulates
/// between paints, so throttling drops paints — never text.
pub const LANE_TAIL_NOTIFY_SECONDS: f64 = 0.05;

/// Per-lane tail buffer cap; the widget paints only the last 3 lines.
const LANE_TAIL_MAX_CHARS: usize = 2_000;

/// Per-lane focus-transcript cap; oldest activity rows drop first.
const LANE_TRANSCRIPT_MAX_BLOCKS: usize = 400;

/// Stored focus transcripts; the oldest lane's is evicted past this.
const LANE_TRANSCRIPT_MAX_LANES: usize = 32;

/// Rows the per-lane cap never trims (banner + delegated brief).
const LANE_SEED_ROWS: usize = 2;

/// First 6 usable chars of a session id for the focused-lane banner.
///
/// Governance redaction can rewrite ids on the live bus
/// (`[REDACTED:PII]…` — found live); bracketed tokens are stripped so
/// a mangled id neither leaks into the banner nor reads as markup.
fn display_short(session_id: &str) -> String {
    // Python `re.sub(r"\[[^\]]*\]", "", session_id)`: drop complete
    // bracketed spans; an unclosed `[` is left in place (then dropped by
    // the alphanumeric-or-dash filter below anyway).
    let mut without_brackets = String::with_capacity(session_id.len());
    let mut rest = session_id;
    while let Some(open) = rest.find('[') {
        match rest[open..].find(']') {
            Some(close) => {
                without_brackets.push_str(&rest[..open]);
                rest = &rest[open + close + 1..];
            }
            None => break,
        }
    }
    without_brackets.push_str(rest);
    without_brackets
        .chars()
        .filter(|ch| ch.is_alphanumeric() || *ch == '-')
        .take(6)
        .collect()
}

/// The last *max_chars* characters of *text* (Python `text[-max:]`).
fn char_suffix(text: &str, max_chars: usize) -> &str {
    let count = text.chars().count();
    if count <= max_chars {
        return text;
    }
    let skip = count - max_chars;
    match text.char_indices().nth(skip) {
        Some((idx, _)) => &text[idx..],
        None => "",
    }
}

/// The narrow lane-tail surface the [`LaneReducer`] drives.
///
/// A structural subset of the Python `ReducerHost` protocol — the two lane
/// callbacks are all this unit touches, so it never has to know about the
/// rest of the host (and there is no import cycle with the turn reducer
/// that owns the full protocol).
pub trait LaneTailHost {
    fn lane_tail_updated(&mut self, text: &str);
    fn lane_tail_cleared(&mut self);
}

/// Lane presentation state: focus transcripts + the live tail.
///
/// Driven by the turn reducer (`ui/reducer.py`'s `TranscriptReducer`),
/// which routes child events onto lanes and calls the methods here to
/// accumulate a lane's focus transcript and paint the focused lane's tail.
pub struct LaneReducer<H: LaneTailHost> {
    host: H,
    ids: BlockIdAllocator,
    pub lanes: LaneRegistry,
    // -- lane live tail (DESIGN-SPEC §8, design doc D4) ----------------------
    tail_clock: Box<dyn Fn() -> f64>,
    lane_tails: HashMap<String, String>,
    lane_tail_last: f64,
    lane_tail_shown: Option<String>,
    /// The root session is streaming right now — it always preempts the
    /// lane tail (D4). Set by the turn reducer at each root stream
    /// transition; read only by the tail paths here.
    pub root_streaming: bool,
    // -- focused-lane transcripts (DESIGN-SPEC §8) ---------------------------
    // Real sessions have no scripted lane logs (that is the demo
    // adapter's `lane_blocks`); the child events already diverted
    // from the root transcript accumulate here instead, keyed by
    // canonical lane session id, so lane focus can replay a
    // subagent's own work. Insertion-ordered (Python dict) so the
    // oldest lane's transcript is the one evicted past the cap.
    lane_transcripts: Vec<(String, Vec<TranscriptBlock>)>,
    pending_briefs: HashMap<String, String>,
}

impl<H: LaneTailHost> LaneReducer<H> {
    /// Python `tail_clock=None` — defaults to a monotonic clock.
    pub fn new(host: H, allocator: BlockIdAllocator, lanes: LaneRegistry) -> Self {
        let start = Instant::now();
        Self::with_clock(
            host,
            allocator,
            lanes,
            Box::new(move || start.elapsed().as_secs_f64()),
        )
    }

    /// Python `tail_clock=<callable>` — injectable clock for tests.
    pub fn with_clock(
        host: H,
        allocator: BlockIdAllocator,
        lanes: LaneRegistry,
        tail_clock: Box<dyn Fn() -> f64>,
    ) -> Self {
        LaneReducer {
            host,
            ids: allocator,
            lanes,
            tail_clock,
            lane_tails: HashMap::new(),
            lane_tail_last: 0.0,
            lane_tail_shown: None,
            root_streaming: false,
            lane_transcripts: Vec::new(),
            pending_briefs: HashMap::new(),
        }
    }

    /// The host the lane callbacks are delivered to.
    pub fn host(&self) -> &H {
        &self.host
    }

    pub fn host_mut(&mut self) -> &mut H {
        &mut self.host
    }

    // -- delegated brief retention -------------------------------------------

    /// Stash a delegate call's instruction so the spawned lane's focus
    /// transcript can open with the delegated brief (the normalized
    /// AgentSpawned event carries no instruction).
    pub fn remember_brief(&mut self, agent: &str, brief: &str) {
        self.pending_briefs.insert(agent.to_string(), brief.to_string());
    }

    // -- focused-lane transcripts (DESIGN-SPEC §8) ---------------------------

    /// (Re)start a lane's focus transcript at spawn.
    ///
    /// A known sub-session re-spawning is a replayed turn reusing its
    /// ids (the `lanes.register` reopen rule) — its transcript resets
    /// with it. Opens with the focused-lane banner and, when the parent
    /// delegate call carried one, the delegated brief as a `delegated`
    /// user line (the demo's `lane_focus_blocks` shape).
    pub fn seed_transcript(&mut self, event: &ev::AgentSpawned) {
        let key = match self.lanes.get(&event.sub_session_id) {
            Some(record) => record.session_id,
            None => event.sub_session_id.clone(),
        };
        // The envelope session_id IS the parent for agent_spawned and sits
        // on the redaction module's structural allowlist; the payload's
        // parent_session_id may arrive scrubbed.
        let parent = if event.session_id.is_empty() {
            &event.parent_session_id
        } else {
            &event.session_id
        };
        let mut blocks: Vec<TranscriptBlock> = vec![SessionBanner {
            id: self.ids.next_id(),
            headline: String::new(),
            detail: String::new(),
            focus_note: focused_lane_banner(&event.agent, &display_short(parent)),
        }
        .into()];
        let brief = self.pending_briefs.remove(&event.agent).unwrap_or_default();
        if !brief.is_empty() {
            blocks.push(
                UserLine {
                    id: self.ids.next_id(),
                    text: brief,
                    mode: "delegated".to_string(),
                }
                .into(),
            );
        }
        if let Some(slot) = self.lane_transcripts.iter_mut().find(|(k, _)| *k == key) {
            slot.1 = blocks;
        } else {
            while self.lane_transcripts.len() >= LANE_TRANSCRIPT_MAX_LANES {
                self.lane_transcripts.remove(0);
            }
            self.lane_transcripts.push((key, blocks));
        }
    }

    /// Append one block to a lane's focus transcript, bounded.
    ///
    /// Lanes restored without a spawn event get a banner-only seed so
    /// their activity still accumulates somewhere focusable.
    pub fn append_block(&mut self, record: &LaneRecord, block: TranscriptBlock) {
        let missing = !self
            .lane_transcripts
            .iter()
            .any(|(k, _)| *k == record.session_id);
        if missing {
            let seeded: Vec<TranscriptBlock> = vec![SessionBanner {
                id: self.ids.next_id(),
                headline: String::new(),
                detail: String::new(),
                focus_note: focused_lane_banner(
                    &record.lane.name,
                    &display_short(record.parent_id.as_deref().unwrap_or("")),
                ),
            }
            .into()];
            while self.lane_transcripts.len() >= LANE_TRANSCRIPT_MAX_LANES {
                self.lane_transcripts.remove(0);
            }
            self.lane_transcripts.push((record.session_id.clone(), seeded));
        }
        let blocks = &mut self
            .lane_transcripts
            .iter_mut()
            .find(|(k, _)| *k == record.session_id)
            .expect("transcript exists after seeding")
            .1;
        blocks.push(block);
        if blocks.len() > LANE_TRANSCRIPT_MAX_BLOCKS {
            blocks.remove(LANE_SEED_ROWS.min(blocks.len() - 1));
        }
    }

    /// A lane's accumulated focus transcript, by session id or name.
    ///
    /// The real-runtime counterpart of the demo adapter's
    /// `lane_blocks` — `None` (not `Some(vec![])`) when nothing is known
    /// so the caller's no-transcript notice stays meaningful.
    pub fn transcript(&self, key: &str) -> Option<Vec<TranscriptBlock>> {
        let key = match self.lanes.get(key) {
            Some(record) => record.session_id,
            None => key.to_string(),
        };
        let mut blocks = self
            .lane_transcripts
            .iter()
            .find(|(k, _)| *k == key)
            .map(|(_, b)| b);
        if blocks.is_none() {
            for candidate in self.lanes.lanes() {
                if candidate.lane.name == key {
                    blocks = self
                        .lane_transcripts
                        .iter()
                        .find(|(k, _)| *k == candidate.session_id)
                        .map(|(_, b)| b);
                    break;
                }
            }
        }
        match blocks {
            Some(found) if !found.is_empty() => Some(found.clone()),
            _ => None,
        }
    }

    // -- lane live tail (DESIGN-SPEC §8, design doc D4) ----------------------

    /// Buffer a child text delta; repaint the focused lane's tail.
    ///
    /// Accumulate-then-notify (the `StreamStatusTracker._on_delta`
    /// shape): the host is repainted with the whole buffer at most every
    /// [`LANE_TAIL_NOTIFY_SECONDS`], so throttling drops paints, never
    /// text. The root stream always preempts; thinking blocks stay dark.
    pub fn tail_delta(&mut self, record: &LaneRecord, event: &ev::StreamBlockDelta) {
        if !(event.block_type.is_empty() || event.block_type == "text") {
            return;
        }
        if !event.text.is_empty() {
            let mut buffered = self
                .lane_tails
                .get(&record.session_id)
                .cloned()
                .unwrap_or_default();
            buffered.push_str(&event.text);
            let capped = char_suffix(&buffered, LANE_TAIL_MAX_CHARS).to_string();
            self.lane_tails.insert(record.session_id.clone(), capped);
        }
        self.lanes.note_stream_activity(&record.session_id);
        if self.root_streaming {
            return; // root always preempts (D4)
        }
        let Some(focused) = self.lanes.tail_lane() else {
            return;
        };
        if focused.session_id != record.session_id {
            return;
        }
        let now = (self.tail_clock)();
        // 1e-9 slack: a clock landing exactly on the 0.05s boundary must
        // paint (float subtraction alone under-reports the elapsed time).
        if self.lane_tail_shown.as_deref() == Some(record.session_id.as_str())
            && now - self.lane_tail_last < LANE_TAIL_NOTIFY_SECONDS - 1e-9
        {
            return;
        }
        self.lane_tail_last = now;
        self.lane_tail_shown = Some(record.session_id.clone());
        let text = self
            .lane_tails
            .get(&record.session_id)
            .cloned()
            .unwrap_or_default();
        self.host.lane_tail_updated(&text);
    }

    /// Drop lane-tail state: one lane's buffer, or everything.
    ///
    /// Ephemeral by design — tail text never becomes a transcript block
    /// (durable content arrives via Channel B; see app.py stream_closed).
    pub fn clear_tail(&mut self, session_id: Option<&str>) {
        match session_id {
            None => self.lane_tails.clear(),
            Some(sid) => {
                self.lane_tails.remove(sid);
            }
        }
        if let Some(shown) = self.lane_tail_shown.as_deref() {
            if session_id.is_none() || session_id == Some(shown) {
                self.lane_tail_shown = None;
                self.host.lane_tail_cleared();
            }
        }
    }

    /// Paint the focused lane's buffered tail right now (ctrl+o).
    ///
    /// Cycling the pin must not wait for the new lane's next delta —
    /// otherwise the tail keeps showing the previous lane's text. Skips
    /// the throttle (a keypress, not a delta storm); clears instead when
    /// the pinned lane has nothing buffered yet.
    pub fn repaint_tail(&mut self) {
        if self.root_streaming {
            return;
        }
        let focused = self.lanes.tail_lane();
        let buffered = focused
            .as_ref()
            .and_then(|record| self.lane_tails.get(&record.session_id))
            .cloned()
            .unwrap_or_default();
        let Some(focused) = focused.filter(|_| !buffered.is_empty()) else {
            if self.lane_tail_shown.is_some() {
                self.lane_tail_shown = None;
                self.host.lane_tail_cleared();
            }
            return;
        };
        self.lane_tail_last = (self.tail_clock)();
        self.lane_tail_shown = Some(focused.session_id.clone());
        self.host.lane_tail_updated(&buffered);
    }
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::rc::Rc;

    use super::*;
    use crate::model::blocks::{Answer, Segment, StyleToken, ToolLine, ToolLineStatus};
    use crate::model::lanes::RegisterOptions;

    const ROOT: &str = "root-session";
    const CHILD_A: &str = "child-aaaaaaaaaaaaaaaa";
    const CHILD_B: &str = "child-bbbbbbbbbbbbbbbb";

    /// Python `FakeClock` — a settable monotonic clock.
    #[derive(Clone)]
    struct FakeClock {
        now: Rc<Cell<f64>>,
    }

    impl FakeClock {
        fn new() -> Self {
            FakeClock { now: Rc::new(Cell::new(100.0)) }
        }

        fn advance(&self, seconds: f64) {
            self.now.set(self.now.get() + seconds);
        }
    }

    /// Only the two lane-tail callbacks the LaneReducer actually drives.
    struct FakeHost {
        tail_updates: Vec<String>,
        tail_cleared: usize,
    }

    impl LaneTailHost for FakeHost {
        fn lane_tail_updated(&mut self, text: &str) {
            self.tail_updates.push(text.to_string());
        }

        fn lane_tail_cleared(&mut self) {
            self.tail_cleared += 1;
        }
    }

    fn make() -> (LaneReducer<FakeHost>, FakeClock) {
        let host = FakeHost { tail_updates: Vec::new(), tail_cleared: 0 };
        let clock = FakeClock::new();
        let handle = clock.clone();
        let lane = LaneReducer::with_clock(
            host,
            BlockIdAllocator::new(),
            LaneRegistry::new(),
            Box::new(move || handle.now.get()),
        );
        (lane, clock)
    }

    fn register(lanes: &mut LaneRegistry, sub: &str, name: &str) {
        lanes.register(
            sub,
            Some(ROOT),
            name,
            RegisterOptions { now: 1.0, ..RegisterOptions::default() },
        );
    }

    fn spawned(sub: &str, name: &str) -> ev::AgentSpawned {
        ev::AgentSpawned {
            session_id: ROOT.to_string(),
            ts: 1.0,
            agent: name.to_string(),
            sub_session_id: sub.to_string(),
            parent_session_id: ROOT.to_string(),
            ..ev::AgentSpawned::default()
        }
    }

    fn delta(sub: &str, text: &str) -> ev::StreamBlockDelta {
        delta_typed(sub, text, "text")
    }

    fn delta_typed(sub: &str, text: &str, block_type: &str) -> ev::StreamBlockDelta {
        ev::StreamBlockDelta {
            session_id: sub.to_string(),
            request_id: format!("req-{sub}"),
            block_index: 0,
            block_type: block_type.to_string(),
            sequence: 0,
            text: text.to_string(),
            ..ev::StreamBlockDelta::default()
        }
    }

    fn texts(blocks: &[TranscriptBlock]) -> Vec<String> {
        blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::Answer(answer) => Some(
                    answer
                        .spans
                        .iter()
                        .map(|span| span.text.as_str())
                        .collect::<String>(),
                ),
                _ => None,
            })
            .collect()
    }

    fn answer(id: &str, text: &str) -> TranscriptBlock {
        Answer {
            clickable: false,
            ..Answer::new(id, vec![Segment::new(text)])
        }
        .into()
    }

    /// Oracle check (not a pinned pytest case): `_display_short` outputs
    /// captured from the real Python module (`uv run python -c ...` against
    /// `ui/lane_reducer.py`).
    #[test]
    fn oracle_display_short_matches_python() {
        assert_eq!(display_short("root-session"), "root-s");
        assert_eq!(display_short("[REDACTED:PII]abcdef123456"), "abcdef");
        assert_eq!(display_short("[]x[y]z-abc-def"), "xz-abc");
        assert_eq!(display_short("[unclosed-bracket-a1b2"), "unclos");
        assert_eq!(display_short("ab_cd!ef-ghij"), "abcdef");
        assert_eq!(display_short(""), "");
    }

    // -- focused-lane transcripts ---------------------------------------------

    /// Pins Python `test_seed_transcript_opens_banner_then_delegated_brief`.
    #[test]
    fn test_seed_transcript_opens_banner_then_delegated_brief() {
        let (mut lane, _clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        lane.remember_brief("researcher", "find the flaky tests");
        lane.seed_transcript(&spawned(CHILD_A, "researcher"));
        let blocks = lane.transcript(CHILD_A).expect("transcript exists");
        assert_eq!(blocks.len(), 2);
        let TranscriptBlock::SessionBanner(banner) = &blocks[0] else {
            panic!("expected SessionBanner");
        };
        assert!(banner.focus_note.contains("focused: researcher"));
        assert!(banner.focus_note.contains(&ROOT[..6]));
        let TranscriptBlock::UserLine(brief) = &blocks[1] else {
            panic!("expected UserLine");
        };
        assert_eq!(brief.text, "find the flaky tests");
        assert_eq!(brief.mode, "delegated");
    }

    /// Pins Python `test_seed_transcript_without_brief_is_banner_only`.
    #[test]
    fn test_seed_transcript_without_brief_is_banner_only() {
        let (mut lane, _clock) = make();
        register(&mut lane.lanes, CHILD_A, "coder");
        lane.seed_transcript(&spawned(CHILD_A, "coder"));
        let blocks = lane.transcript(CHILD_A).expect("transcript exists");
        assert_eq!(blocks.len(), 1);
        assert!(matches!(blocks[0], TranscriptBlock::SessionBanner(_)));
    }

    /// Pins Python `test_append_block_seeds_a_banner_for_an_unknown_lane`.
    #[test]
    fn test_append_block_seeds_a_banner_for_an_unknown_lane() {
        let (mut lane, _clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        let record = lane.lanes.get(CHILD_A).expect("lane registered");
        lane.append_block(&record, answer("x", "hi"));
        let blocks = lane.transcript(CHILD_A).expect("transcript exists");
        // banner-only seed prepended
        assert!(matches!(blocks[0], TranscriptBlock::SessionBanner(_)));
        assert_eq!(texts(&blocks), vec!["hi"]);
    }

    /// Pins Python `test_transcript_resolves_by_name_and_misses_cleanly`.
    #[test]
    fn test_transcript_resolves_by_name_and_misses_cleanly() {
        let (mut lane, _clock) = make();
        register(&mut lane.lanes, CHILD_A, "modular-builder");
        lane.seed_transcript(&spawned(CHILD_A, "modular-builder"));
        assert!(lane.transcript("modular-builder").is_some());
        assert!(lane.transcript(CHILD_A).is_some());
        assert!(lane.transcript("nope").is_none());
    }

    /// Pins Python `test_transcript_is_bounded_and_keeps_seed_rows`.
    #[test]
    fn test_transcript_is_bounded_and_keeps_seed_rows() {
        let (mut lane, _clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        lane.remember_brief("researcher", "the brief");
        lane.seed_transcript(&spawned(CHILD_A, "researcher"));
        let record = lane.lanes.get(CHILD_A).expect("lane registered");
        for n in 0..(LANE_TRANSCRIPT_MAX_BLOCKS + 25) {
            lane.append_block(&record, answer(&format!("a{n}"), &format!("row {n}")));
        }
        let blocks = lane.transcript(CHILD_A).expect("transcript exists");
        assert!(blocks.len() <= LANE_TRANSCRIPT_MAX_BLOCKS);
        assert!(matches!(blocks[0], TranscriptBlock::SessionBanner(_)));
        // seed rows survive the trim
        assert!(matches!(blocks[1], TranscriptBlock::UserLine(_)));
        let last = texts(&blocks).pop().expect("answer rows present");
        assert!(last.contains(&format!("row {}", LANE_TRANSCRIPT_MAX_BLOCKS + 24)));
    }

    /// Pins Python `test_stored_transcripts_are_capped_by_lane_count`.
    #[test]
    fn test_stored_transcripts_are_capped_by_lane_count() {
        let (mut lane, _clock) = make();
        for n in 0..(LANE_TRANSCRIPT_MAX_LANES + 5) {
            let sub = format!("lane-{n:016}");
            lane.lanes.register(
                &sub,
                Some(ROOT),
                &format!("agent{n}"),
                RegisterOptions { now: 1.0, ..RegisterOptions::default() },
            );
            lane.seed_transcript(&spawned(&sub, &format!("agent{n}")));
        }
        // The oldest lanes' transcripts were evicted; the newest survive.
        assert!(lane.transcript("lane-0000000000000000").is_none());
        let newest = format!("lane-{:016}", LANE_TRANSCRIPT_MAX_LANES + 4);
        assert!(lane.transcript(&newest).is_some());
    }

    // -- live tail --------------------------------------------------------------

    /// Pins Python `test_tail_delta_paints_the_accumulated_buffer`.
    #[test]
    fn test_tail_delta_paints_the_accumulated_buffer() {
        let (mut lane, clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        let record = lane.lanes.get(CHILD_A).expect("lane registered");
        lane.tail_delta(&record, &delta(CHILD_A, "reading the "));
        clock.advance(LANE_TAIL_NOTIFY_SECONDS);
        lane.tail_delta(&record, &delta(CHILD_A, "queue bridge"));
        assert_eq!(
            lane.host().tail_updates,
            vec!["reading the ", "reading the queue bridge"]
        );
    }

    /// Pins Python `test_tail_delta_throttle_coalesces_without_losing_text`.
    #[test]
    fn test_tail_delta_throttle_coalesces_without_losing_text() {
        let (mut lane, clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        let record = lane.lanes.get(CHILD_A).expect("lane registered");
        lane.tail_delta(&record, &delta(CHILD_A, "one "));
        lane.tail_delta(&record, &delta(CHILD_A, "two ")); // same instant — throttled
        assert_eq!(lane.host().tail_updates, vec!["one "]);
        clock.advance(LANE_TAIL_NOTIFY_SECONDS);
        lane.tail_delta(&record, &delta(CHILD_A, "three"));
        assert_eq!(lane.host().tail_updates, vec!["one ", "one two three"]);
    }

    /// Pins Python `test_thinking_deltas_never_reach_the_tail`.
    #[test]
    fn test_thinking_deltas_never_reach_the_tail() {
        let (mut lane, _clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        let record = lane.lanes.get(CHILD_A).expect("lane registered");
        lane.tail_delta(&record, &delta_typed(CHILD_A, "hmm", "thinking"));
        assert!(lane.host().tail_updates.is_empty());
    }

    /// Pins Python `test_root_stream_preempts_the_tail`.
    #[test]
    fn test_root_stream_preempts_the_tail() {
        let (mut lane, clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        let record = lane.lanes.get(CHILD_A).expect("lane registered");
        lane.root_streaming = true;
        clock.advance(LANE_TAIL_NOTIFY_SECONDS);
        lane.tail_delta(&record, &delta(CHILD_A, "buffered but dark"));
        // never painted while the root streams
        assert!(lane.host().tail_updates.is_empty());
        lane.root_streaming = false;
        clock.advance(LANE_TAIL_NOTIFY_SECONDS);
        lane.tail_delta(&record, &delta(CHILD_A, ", resumes"));
        // buffer never lost
        assert_eq!(
            lane.host().tail_updates.last().map(String::as_str),
            Some("buffered but dark, resumes")
        );
    }

    /// Pins Python `test_clear_tail_clears_a_shown_tail`.
    #[test]
    fn test_clear_tail_clears_a_shown_tail() {
        let (mut lane, _clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        let record = lane.lanes.get(CHILD_A).expect("lane registered");
        lane.tail_delta(&record, &delta(CHILD_A, "child text"));
        assert_eq!(lane.host().tail_updates, vec!["child text"]);
        lane.clear_tail(Some(CHILD_A));
        assert_eq!(lane.host().tail_cleared, 1);
    }

    /// Pins Python
    /// `test_repaint_tail_paints_newly_pinned_buffer_and_clears_when_empty`.
    #[test]
    fn test_repaint_tail_paints_newly_pinned_buffer_and_clears_when_empty() {
        let (mut lane, clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        register(&mut lane.lanes, CHILD_B, "coder");
        let rec_a = lane.lanes.get(CHILD_A).expect("lane registered");
        lane.tail_delta(&rec_a, &delta(CHILD_A, "aaa"));
        clock.advance(LANE_TAIL_NOTIFY_SECONDS);
        lane.lanes.cycle_tail_focus(); // A (current) -> B, which never streamed
        lane.repaint_tail();
        assert_eq!(lane.host().tail_cleared, 1); // pinned lane has no buffer -> clears
        lane.lanes.cycle_tail_focus(); // B -> A, which has "aaa" buffered
        lane.repaint_tail();
        assert_eq!(lane.host().tail_updates.last().map(String::as_str), Some("aaa"));
    }

    /// Pins Python `test_lane_activity_recap_row_appends_to_the_transcript`:
    /// a completion recap row (built by the turn reducer) still lands in the
    /// lane transcript via append_block — the extracted unit owns the list.
    #[test]
    fn test_lane_activity_recap_row_appends_to_the_transcript() {
        let (mut lane, _clock) = make();
        register(&mut lane.lanes, CHILD_A, "researcher");
        lane.seed_transcript(&spawned(CHILD_A, "researcher"));
        let record = lane.lanes.get(CHILD_A).expect("lane registered");
        lane.append_block(
            &record,
            ToolLine {
                status: ToolLineStatus::Completed,
                tool_call_ids: vec!["t1".to_string()],
                ..ToolLine::new("t1", "read ci.log")
            }
            .into(),
        );
        lane.append_block(
            &record,
            Answer {
                clickable: false,
                ..Answer::new(
                    "r1",
                    vec![
                        Segment {
                            style_token: StyleToken::Dimmer,
                            ..Segment::new("\u{2733} ")
                        },
                        Segment {
                            style_token: StyleToken::Dim,
                            ..Segment::new("completed \u{b7} result reported back to parent")
                        },
                    ],
                )
            }
            .into(),
        );
        let blocks = lane.transcript(CHILD_A).expect("transcript exists");
        let tools: Vec<&ToolLine> = blocks
            .iter()
            .filter_map(|block| match block {
                TranscriptBlock::ToolLine(tool) => Some(tool),
                _ => None,
            })
            .collect();
        assert!(!tools.is_empty());
        assert_eq!(tools[0].status, ToolLineStatus::Completed);
        let last = texts(&blocks).pop().expect("answer rows present");
        assert!(last.contains("completed \u{b7} result reported back to parent"));
    }
}
