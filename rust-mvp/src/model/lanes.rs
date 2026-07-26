//! Agent lanes: per-subagent state keyed by session id (DESIGN-SPEC §8).
//!
//! Port of `src/amplifier_app_newtui/model/lanes.py`.
//!
//! Every amplifier event payload carries `session_id` + `parent_id` — that
//! pair is the entire routing key for lanes. The registry tolerates events
//! arriving before their parent lane exists (`session:start` can race
//! `task:agent_spawned` — RESEARCH-BRIEF risk 5): a lane registered with an
//! unknown `parent_id` still routes; depth is patched when the parent
//! appears.
//!
//! Lane line format: `  <glyph> <name> · <activity> · <elapsed> · $<cost>`
//! with glyph/color per state: `◐` teal running, `■` fg working, `✔` dim
//! done.

use std::collections::HashMap;

use rust_decimal::Decimal;

use crate::model::blocks::StyleToken;

/// Python `LaneStateName = Literal["running", "working", "done"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum LaneStateName {
    #[default]
    Running,
    Working,
    Done,
}

impl LaneStateName {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            LaneStateName::Running => "running",
            LaneStateName::Working => "working",
            LaneStateName::Done => "done",
        }
    }

    /// Python `_STATE_GLYPHS` — glyph/color pairs per state.
    fn glyph_and_color(self) -> (&'static str, StyleToken) {
        match self {
            LaneStateName::Running => ("◐", StyleToken::Teal),
            LaneStateName::Working => ("■", StyleToken::Fg),
            LaneStateName::Done => ("✔", StyleToken::Dim),
        }
    }
}

/// Python `_redacted_suffix`: match `^\[REDACTED:[^\]]+\](?P<suffix>.+)$`
/// and return the suffix when it is long enough to route on. Foundation
/// sub-session suffixes are long random identifiers; short redacted
/// fragments that could match two lanes are never fuzzy-routed.
fn redacted_suffix(session_id: &str) -> Option<&str> {
    let rest = session_id.strip_prefix("[REDACTED:")?;
    let close = rest.find(']')?;
    if close == 0 {
        return None; // `[^\]]+` requires at least one redacted char
    }
    // Python `$` (non-MULTILINE) also matches just before one trailing `\n`.
    let suffix = rest[close + 1..].strip_suffix('\n').unwrap_or(&rest[close + 1..]);
    // `.` never crosses a newline, so an interior `\n` fails the match.
    if suffix.is_empty() || suffix.contains('\n') {
        return None;
    }
    // Avoid fuzzy-routing short redacted fragments (< 12 chars).
    if suffix.chars().count() >= 12 {
        Some(suffix)
    } else {
        None
    }
}

/// Match a redacted spawn id to the real child `session:start` id.
fn compatible_session_ids(left: &str, right: &str) -> bool {
    if let Some(left_suffix) = redacted_suffix(left) {
        return right.ends_with(left_suffix);
    }
    if let Some(right_suffix) = redacted_suffix(right) {
        return left.ends_with(right_suffix);
    }
    false
}

/// One subagent lane's presentation state.
///
/// - `name`: agent name (e.g. `test-writer`).
/// - `glyph`/`color_token`: derived from `state` at construction via
///   [`LaneState::for_state`] — kept as fields so a lane snapshot is fully
///   renderable without lookups.
/// - `activity`: current one-line activity description.
/// - `elapsed`: seconds since spawn; `cost`: dollars spent so far.
///
/// Frozen pydantic model in Python — treat as immutable by convention.
#[derive(Clone, Debug, PartialEq)]
pub struct LaneState {
    pub name: String,
    pub glyph: String,
    pub color_token: StyleToken,
    pub activity: String,
    pub elapsed: f64,
    pub tokens: u64,
    pub cost: Decimal,
    pub state: LaneStateName,
}

impl LaneState {
    /// Build a lane with the spec glyph/color for *state* (Python
    /// `LaneState.for_state` with keyword defaults for the rest).
    pub fn for_state(name: &str, state: LaneStateName) -> Self {
        Self::for_state_with(name, state, "", 0.0, 0, Decimal::ZERO)
    }

    /// [`LaneState::for_state`] with every optional field supplied.
    pub fn for_state_with(
        name: &str,
        state: LaneStateName,
        activity: &str,
        elapsed: f64,
        tokens: u64,
        cost: Decimal,
    ) -> Self {
        let (glyph, color) = state.glyph_and_color();
        LaneState {
            name: name.to_string(),
            glyph: glyph.to_string(),
            color_token: color,
            activity: activity.to_string(),
            elapsed,
            tokens,
            cost,
            state,
        }
    }
}

/// A lane plus its routing identity in the session tree.
#[derive(Clone, Debug, PartialEq)]
pub struct LaneRecord {
    pub session_id: String,
    pub parent_id: Option<String>,
    pub depth: u32,
    pub started_at: f64,
    pub lane: LaneState,
}

/// Optional keyword arguments of Python `LaneRegistry.register`.
#[derive(Clone, Debug)]
pub struct RegisterOptions {
    pub activity: String,
    pub state: LaneStateName,
    pub reopen: bool,
    pub now: f64,
}

impl Default for RegisterOptions {
    fn default() -> Self {
        RegisterOptions {
            activity: String::new(),
            state: LaneStateName::Running,
            reopen: false,
            now: 0.0,
        }
    }
}

/// Optional keyword arguments of Python `LaneRegistry.update` — `None`
/// fields keep the lane's current value.
#[derive(Clone, Debug, Default)]
pub struct LaneUpdate {
    pub activity: Option<String>,
    pub elapsed: Option<f64>,
    pub tokens: Option<u64>,
    pub cost: Option<Decimal>,
    pub state: Option<LaneStateName>,
}

/// All live/finished lanes keyed by `session_id`, routed by `parent_id`.
///
/// Mutable by design (one per app). [`LaneRegistry::register`] opens a lane
/// on `task:agent_spawned`/`session:start`; [`LaneRegistry::update`] patches
/// activity/telemetry from any child-stamped event; [`LaneRegistry::complete`]
/// closes it on `task:agent_completed`. Unknown-parent registration is
/// tolerated and depth is retro-patched when the parent lane appears.
///
/// Concurrency invariant (from the Python original): every writer runs on
/// the single UI event loop; do not share across threads without external
/// synchronization.
#[derive(Debug, Default)]
pub struct LaneRegistry {
    records: HashMap<String, LaneRecord>,
    order: Vec<String>,
    aliases: HashMap<String, String>,
    /// Insertion-ordered `session_id -> parent_id` map (Python dict).
    pending_sessions: Vec<(String, Option<String>)>,
    tail_focus: Option<String>,
    tail_recent: Option<String>,
}

impl LaneRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// All lanes in registration order (the lanes panel listing).
    pub fn lanes(&self) -> Vec<LaneRecord> {
        self.order.iter().map(|sid| self.records[sid].clone()).collect()
    }

    pub fn active(&self) -> Vec<LaneRecord> {
        self.lanes()
            .into_iter()
            .filter(|r| r.lane.state != LaneStateName::Done)
            .collect()
    }

    /// Drives `N agent(s)` in the working line and the coordinating title.
    pub fn active_count(&self) -> usize {
        self.active().len()
    }

    pub fn get(&self, session_id: &str) -> Option<LaneRecord> {
        let key = self.resolve_id(session_id, None)?;
        self.records.get(&key).cloned()
    }

    pub fn children_of(&self, parent_id: &str) -> Vec<LaneRecord> {
        self.lanes()
            .into_iter()
            .filter(|r| r.parent_id.as_deref() == Some(parent_id))
            .collect()
    }

    /// Open a lane for a spawned subagent.
    ///
    /// Idempotent for known session ids by default (`session:start` can race
    /// `task:agent_spawned`, and a completion that raced ahead of its spawn
    /// must stay done). With `opts.reopen == true` a *finished* lane spawned
    /// again (a replayed demo turn reuses its sub-session ids) is reset to a
    /// fresh spawned state so the panel shows the live tri-state glyphs
    /// instead of a stale `✔ done`.
    pub fn register(
        &mut self,
        session_id: &str,
        parent_id: Option<&str>,
        name: &str,
        opts: RegisterOptions,
    ) -> LaneRecord {
        let existing_key = self.resolve_id(session_id, None);
        let existing = existing_key.as_ref().and_then(|key| self.records.get(key)).cloned();
        if let Some(existing) = existing {
            if opts.reopen
                && existing.lane.state == LaneStateName::Done
                && opts.state != LaneStateName::Done
            {
                let fresh = LaneRecord {
                    started_at: opts.now,
                    lane: LaneState::for_state_with(
                        name,
                        opts.state,
                        &opts.activity,
                        0.0,
                        0,
                        Decimal::ZERO,
                    ),
                    ..existing
                };
                self.records.insert(session_id.to_string(), fresh.clone());
                return fresh;
            }
            return existing;
        }
        let parent = parent_id.and_then(|pid| self.records.get(pid));
        let record = LaneRecord {
            session_id: session_id.to_string(),
            parent_id: parent_id.map(str::to_string),
            depth: parent.map(|p| p.depth + 1).unwrap_or(1),
            started_at: opts.now,
            lane: LaneState::for_state_with(name, opts.state, &opts.activity, 0.0, 0, Decimal::ZERO),
        };
        self.records.insert(session_id.to_string(), record.clone());
        self.order.push(session_id.to_string());
        self.patch_child_depths(session_id);
        for (actual_id, actual_parent) in self.pending_sessions.clone() {
            if compatible_session_ids(session_id, &actual_id)
                && (actual_parent.is_none() || actual_parent.as_deref() == parent_id)
            {
                if let Some(rebound) = self.bind_session(&actual_id, actual_parent.as_deref()) {
                    return rebound;
                }
            }
        }
        record
    }

    /// Bind a real child session id to its possibly-redacted spawn lane.
    ///
    /// Foundation governance can redact the leading portion of
    /// `task:agent_spawned.sub_session_id` while the child's later
    /// `session:start` and usage events carry the usable id. Re-keying here
    /// restores exact telemetry routing and makes lane focus open the real
    /// child transcript. The redacted id remains an alias so the
    /// corresponding `task:agent_completed` still closes the lane.
    pub fn bind_session(
        &mut self,
        session_id: &str,
        parent_id: Option<&str>,
    ) -> Option<LaneRecord> {
        let Some(key) = self.resolve_id(session_id, parent_id) else {
            self.pending_insert(session_id, parent_id);
            return None;
        };
        self.pending_remove(session_id);
        if key == session_id {
            return self.records.get(&key).cloned();
        }
        if redacted_suffix(&key).is_none() || redacted_suffix(session_id).is_some() {
            self.aliases.insert(session_id.to_string(), key.clone());
            return self.records.get(&key).cloned();
        }
        Some(self.rekey(&key, session_id, parent_id))
    }

    /// Patch a lane's live fields; returns `None` for unknown lanes (events
    /// for sessions we never saw spawn are dropped, not fatal).
    pub fn update(&mut self, session_id: &str, patch: LaneUpdate) -> Option<LaneRecord> {
        let key = self.resolve_id(session_id, None)?;
        let record = self.records.get(&key)?.clone();
        let lane = &record.lane;
        let new_state = patch.state.unwrap_or(lane.state);
        let updated = LaneState::for_state_with(
            &lane.name,
            new_state,
            patch.activity.as_deref().unwrap_or(&lane.activity),
            patch.elapsed.unwrap_or(lane.elapsed),
            patch.tokens.unwrap_or(lane.tokens),
            patch.cost.unwrap_or(lane.cost),
        );
        let patched = LaneRecord { lane: updated, ..record };
        self.records.insert(key, patched.clone());
        Some(patched)
    }

    /// Bump each running lane's `elapsed` to `now - started_at`.
    ///
    /// Driven by the app's 1s heartbeat (via `reducer.tick`) so a subagent's
    /// per-lane clock ticks live between the sparse usage events. Done lanes
    /// are frozen; lanes with no `started_at` (never stamped at spawn) are
    /// left alone. Returns `true` if any lane moved.
    pub fn advance(&mut self, now: f64) -> bool {
        let mut changed = false;
        for record in self.records.values_mut() {
            if record.lane.state == LaneStateName::Done || record.started_at <= 0.0 {
                continue;
            }
            let elapsed = now - record.started_at;
            if elapsed < 0.0 || elapsed == record.lane.elapsed {
                continue;
            }
            record.lane.elapsed = elapsed;
            changed = true;
        }
        changed
    }

    /// Mark a lane done (`✔` dim), recording its result summary.
    pub fn complete(&mut self, session_id: &str, result: &str) -> Option<LaneRecord> {
        let activity = if result.is_empty() {
            "done".to_string()
        } else {
            format!("done · {result}")
        };
        self.update(
            session_id,
            LaneUpdate {
                state: Some(LaneStateName::Done),
                activity: Some(activity),
                ..LaneUpdate::default()
            },
        )
    }

    // -- lane tail focus (DESIGN-SPEC §8: live tail) ------------------------

    /// The lane whose stream feeds the live tail.
    ///
    /// An explicit ctrl-o choice wins while that lane still runs; then the
    /// most-recently-streaming running lane; then the first running lane.
    /// `None` when nothing is running (the tail goes dark).
    pub fn tail_lane(&self) -> Option<LaneRecord> {
        for candidate in [&self.tail_focus, &self.tail_recent] {
            let Some(candidate) = candidate else { continue };
            let record = self
                .resolve_id(candidate, None)
                .and_then(|key| self.records.get(&key));
            if let Some(record) = record {
                if record.lane.state != LaneStateName::Done {
                    return Some(record.clone());
                }
            }
        }
        self.active().into_iter().next()
    }

    /// Record *session_id* as the most-recently-streaming lane.
    ///
    /// Unknown or finished lanes are dropped, not fatal (same tolerance as
    /// [`LaneRegistry::update`]).
    pub fn note_stream_activity(&mut self, session_id: &str) {
        let Some(key) = self.resolve_id(session_id, None) else { return };
        if let Some(record) = self.records.get(&key) {
            if record.lane.state != LaneStateName::Done {
                self.tail_recent = Some(key);
            }
        }
    }

    /// Pin the tail to the next running lane (ctrl-o), in lane order.
    pub fn cycle_tail_focus(&mut self) -> Option<LaneRecord> {
        let active = self.active();
        if active.is_empty() {
            self.tail_focus = None;
            return None;
        }
        let ids: Vec<String> = active.iter().map(|r| r.session_id.clone()).collect();
        let index = match self.tail_lane() {
            Some(current) => ids
                .iter()
                .position(|id| *id == current.session_id)
                .map(|pos| (pos + 1) % ids.len())
                .unwrap_or(0),
            None => 0,
        };
        self.tail_focus = Some(ids[index].clone());
        self.records.get(&ids[index]).cloned()
    }

    /// Fix depths of children registered before their parent (spawn race).
    fn patch_child_depths(&mut self, parent_id: &str) {
        let parent_depth = self.records[parent_id].depth;
        let child_ids: Vec<String> = self
            .children_of(parent_id)
            .into_iter()
            .map(|r| r.session_id)
            .collect();
        for child_id in child_ids {
            let expected = parent_depth + 1;
            let child = &self.records[&child_id];
            if child.depth != expected {
                self.records.get_mut(&child_id).expect("child exists").depth = expected;
                self.patch_child_depths(&child_id);
            }
        }
    }

    fn resolve_id(&self, session_id: &str, parent_id: Option<&str>) -> Option<String> {
        if self.records.contains_key(session_id) {
            return Some(session_id.to_string());
        }
        if let Some(alias) = self.aliases.get(session_id) {
            if self.records.contains_key(alias) {
                return Some(alias.clone());
            }
        }
        let matches: Vec<&String> = self
            .order
            .iter()
            .filter(|key| {
                let record = &self.records[key.as_str()];
                compatible_session_ids(key, session_id)
                    && (parent_id.is_none() || record.parent_id.as_deref() == parent_id)
            })
            .collect();
        if matches.len() == 1 {
            Some(matches[0].clone())
        } else {
            None
        }
    }

    fn rekey(&mut self, old_id: &str, new_id: &str, parent_id: Option<&str>) -> LaneRecord {
        let record = self.records.remove(old_id).expect("rekey source exists");
        let rebound = LaneRecord {
            session_id: new_id.to_string(),
            parent_id: parent_id.map(str::to_string).or(record.parent_id.clone()),
            ..record
        };
        self.records.insert(new_id.to_string(), rebound.clone());
        if let Some(slot) = self.order.iter_mut().find(|id| *id == old_id) {
            *slot = new_id.to_string();
        }
        self.aliases.insert(old_id.to_string(), new_id.to_string());
        for target in self.aliases.values_mut() {
            if target == old_id {
                *target = new_id.to_string();
            }
        }
        for child in self.records.values_mut() {
            if child.parent_id.as_deref() == Some(old_id) {
                child.parent_id = Some(new_id.to_string());
            }
        }
        self.patch_child_depths(new_id);
        rebound
    }

    fn pending_insert(&mut self, session_id: &str, parent_id: Option<&str>) {
        let parent = parent_id.map(str::to_string);
        if let Some(slot) = self.pending_sessions.iter_mut().find(|(id, _)| id == session_id) {
            slot.1 = parent;
        } else {
            self.pending_sessions.push((session_id.to_string(), parent));
        }
    }

    fn pending_remove(&mut self, session_id: &str) {
        self.pending_sessions.retain(|(id, _)| id != session_id);
    }
}

/// A short, stable disambiguator drawn from a session id.
///
/// Governance redaction can wrap ids in `[REDACTED:…]` brackets; those (and
/// any other non-alphanumeric noise) are stripped, then the LAST four usable
/// characters are taken. Foundation prefixes sibling sub-sessions with a
/// shared timestamp, so the random tail disambiguates where the head would
/// not. Falls back to the whole cleaned id when shorter than four.
fn short_lane_id(session_id: &str) -> String {
    // Python `re.sub(r"\[[^\]]*\]", "", session_id)`: drop complete
    // bracketed spans; an unclosed `[` is left in place (then dropped by
    // the alphanumeric filter below anyway).
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
    let cleaned: Vec<char> = without_brackets.chars().filter(|ch| ch.is_alphanumeric()).collect();
    if cleaned.len() >= 4 {
        cleaned[cleaned.len() - 4..].iter().collect()
    } else {
        cleaned.iter().collect()
    }
}

/// Display labels for a lane listing, disambiguating same-named agents.
///
/// Two delegates of the same agent (e.g. two `test-writer` lanes) render
/// byte-identical rows — ambiguous the moment the supervisor tries to tell
/// them apart. Every lane whose `name` is shared gets a short session-id tag
/// appended (`test-writer #a1b2`); uniquely-named lanes are returned
/// unchanged. A rare tail collision (two ids ending the same four chars)
/// falls back to a stable 1-based ordinal within the group, so the labels
/// are always distinct and deterministic in registration order.
pub fn lane_labels(records: &[LaneRecord]) -> Vec<String> {
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for record in records {
        *counts.entry(record.lane.name.as_str()).or_insert(0) += 1;
    }
    let mut ordinals: HashMap<&str, usize> = HashMap::new();
    let mut used: Vec<String> = Vec::new();
    let mut labels: Vec<String> = Vec::new();
    for record in records {
        let name = record.lane.name.as_str();
        if name.is_empty() || counts[name] == 1 {
            labels.push(name.to_string());
            continue;
        }
        let ordinal = ordinals.entry(name).or_insert(0);
        *ordinal += 1;
        let ordinal = *ordinal;
        let tag = short_lane_id(&record.session_id);
        let mut label = if tag.is_empty() {
            format!("{name} #{ordinal}")
        } else {
            format!("{name} #{tag}")
        };
        if used.contains(&label) {
            label = format!("{name} #{ordinal}");
        }
        used.push(label.clone());
        labels.push(label);
    }
    labels
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dec(value: &str) -> Decimal {
        value.parse().expect("valid decimal literal")
    }

    fn opts() -> RegisterOptions {
        RegisterOptions::default()
    }

    // --- lanes (DESIGN-SPEC §8) -------------------------------------------

    /// Pins Python `test_lane_state_glyphs_per_spec`.
    #[test]
    fn test_lane_state_glyphs_per_spec() {
        let running = LaneState::for_state("a", LaneStateName::Running);
        let working = LaneState::for_state("a", LaneStateName::Working);
        let done = LaneState::for_state("a", LaneStateName::Done);
        assert_eq!((running.glyph.as_str(), running.color_token), ("◐", StyleToken::Teal));
        assert_eq!((working.glyph.as_str(), working.color_token), ("■", StyleToken::Fg));
        assert_eq!((done.glyph.as_str(), done.color_token), ("✔", StyleToken::Dim));
        // The color tokens carry the exact Python literal strings.
        assert_eq!(running.color_token.as_str(), "teal");
        assert_eq!(working.color_token.as_str(), "fg");
        assert_eq!(done.color_token.as_str(), "dim");
    }

    /// Pins Python `test_lane_registry_routing_and_completion`.
    #[test]
    fn test_lane_registry_routing_and_completion() {
        let mut registry = LaneRegistry::new();
        registry.register("root", None, "main", opts());
        registry.register(
            "root-abc_tester",
            Some("root"),
            "tester",
            RegisterOptions { activity: "writing tests".to_string(), ..opts() },
        );
        assert_eq!(registry.active_count(), 2);
        let record = registry.get("root-abc_tester");
        assert!(record.is_some());
        assert_eq!(record.unwrap().depth, 2);
        let updated = registry.update(
            "root-abc_tester",
            LaneUpdate {
                cost: Some(dec("0.05")),
                elapsed: Some(12.0),
                ..LaneUpdate::default()
            },
        );
        assert!(updated.is_some());
        assert_eq!(updated.unwrap().lane.cost, dec("0.05"));
        let done = registry.complete("root-abc_tester", "34 tests passing");
        assert!(done.is_some());
        let done = done.unwrap();
        assert_eq!(done.lane.state, LaneStateName::Done);
        assert_eq!(done.lane.activity, "done · 34 tests passing");
        assert_eq!(registry.active_count(), 1);
    }

    /// Pins Python `test_lane_registry_tolerates_child_before_parent`:
    /// session:start can race task:agent_spawned — depth is retro-patched.
    #[test]
    fn test_lane_registry_tolerates_child_before_parent() {
        let mut registry = LaneRegistry::new();
        registry.register("child", Some("parent"), "early-bird", opts());
        assert_eq!(registry.get("child").unwrap().depth, 1);
        registry.register("parent", None, "parent", opts());
        assert_eq!(registry.get("child").unwrap().depth, 2);
    }

    /// Pins Python `test_lane_registry_register_is_idempotent`.
    #[test]
    fn test_lane_registry_register_is_idempotent() {
        let mut registry = LaneRegistry::new();
        let first = registry.register("s1", None, "a", opts());
        let second = registry.register("s1", None, "renamed", opts());
        assert_eq!(first, second);
        assert_eq!(registry.lanes().len(), 1);
        // A done lane stays done by default (a completion that raced ahead
        // of its spawn must not be re-opened by the late spawn event).
        registry.complete("s1", "ok");
        let third = registry.register("s1", None, "a", opts());
        assert_eq!(third.lane.state, LaneStateName::Done);
    }

    /// Pins Python `test_lane_registry_reopen_resets_done_lane`: a replayed
    /// demo turn reuses sub-session ids: reopen=true resets the finished
    /// lane to a fresh spawned state so the panel shows live glyphs.
    #[test]
    fn test_lane_registry_reopen_resets_done_lane() {
        let mut registry = LaneRegistry::new();
        registry.register("s1", None, "researcher", opts());
        registry.update(
            "s1",
            LaneUpdate {
                elapsed: Some(30.0),
                cost: Some(dec("0.12")),
                ..LaneUpdate::default()
            },
        );
        registry.complete("s1", "3 findings");
        let reopened = registry.register(
            "s1",
            None,
            "researcher",
            RegisterOptions { activity: "running".to_string(), reopen: true, ..opts() },
        );
        assert_eq!(reopened.lane.state, LaneStateName::Running);
        assert_eq!(
            (reopened.lane.glyph.as_str(), reopened.lane.color_token),
            ("◐", StyleToken::Teal)
        );
        assert_eq!(reopened.lane.activity, "running");
        assert_eq!(reopened.lane.elapsed, 0.0);
        assert_eq!(reopened.lane.cost, dec("0"));
        assert_eq!(registry.lanes().len(), 1);
        assert_eq!(registry.active_count(), 1);
    }

    /// Pins Python `test_lane_update_unknown_session_is_dropped`.
    #[test]
    fn test_lane_update_unknown_session_is_dropped() {
        let updated = LaneRegistry::new().update(
            "ghost",
            LaneUpdate { activity: Some("x".to_string()), ..LaneUpdate::default() },
        );
        assert!(updated.is_none());
    }

    // -- lane tail focus (DESIGN-SPEC §8: live tail) --------------------------

    /// Pins Python `test_tail_lane_defaults_to_first_running_then_most_recent_stream`.
    #[test]
    fn test_tail_lane_defaults_to_first_running_then_most_recent_stream() {
        let mut lanes = LaneRegistry::new();
        assert!(lanes.tail_lane().is_none());
        lanes.register("s1", Some("root"), "researcher", opts());
        lanes.register("s2", Some("root"), "coder", opts());
        let tailed = lanes.tail_lane();
        assert!(tailed.is_some());
        assert_eq!(tailed.unwrap().session_id, "s1"); // fallback: first running
        lanes.note_stream_activity("s2");
        let tailed = lanes.tail_lane();
        assert!(tailed.is_some());
        assert_eq!(tailed.unwrap().session_id, "s2"); // most recent stream wins
    }

    /// Pins Python `test_cycle_tail_focus_pins_and_falls_back_when_lane_completes`.
    #[test]
    fn test_cycle_tail_focus_pins_and_falls_back_when_lane_completes() {
        let mut lanes = LaneRegistry::new();
        lanes.register("s1", Some("root"), "researcher", opts());
        lanes.register("s2", Some("root"), "coder", opts());
        lanes.note_stream_activity("s2");
        let pinned = lanes.cycle_tail_focus(); // from s2 → next running lane: s1
        assert!(pinned.is_some());
        assert_eq!(pinned.unwrap().session_id, "s1");
        lanes.note_stream_activity("s2"); // recent changes, but the pin holds
        let tailed = lanes.tail_lane();
        assert!(tailed.is_some());
        assert_eq!(tailed.unwrap().session_id, "s1");
        lanes.complete("s1", ""); // pinned lane done → falls back to most recent
        let tailed = lanes.tail_lane();
        assert!(tailed.is_some());
        assert_eq!(tailed.unwrap().session_id, "s2");
        lanes.complete("s2", "");
        assert!(lanes.tail_lane().is_none());
        assert!(lanes.cycle_tail_focus().is_none());
    }

    /// Oracle check (not a pinned pytest case): outputs of `_redacted_suffix`,
    /// `_compatible_session_ids`, `_short_lane_id`, `lane_labels`, and the
    /// bind_session rekey/pending paths captured from the real Python module
    /// (`uv run python -c ...` against `model/lanes.py`).
    #[test]
    fn oracle_fuzzy_routing_and_labels_match_python() {
        assert_eq!(redacted_suffix("[REDACTED:abc]tail-1234567890"), Some("tail-1234567890"));
        assert_eq!(redacted_suffix("[REDACTED:abc]short"), None);
        assert_eq!(redacted_suffix("[REDACTED:]tail-1234567890"), None);
        assert_eq!(redacted_suffix("plain-id"), None);
        assert!(compatible_session_ids("[REDACTED:x]abcdefghijkl", "real-abcdefghijkl"));
        assert!(compatible_session_ids("real-abcdefghijkl", "[REDACTED:x]abcdefghijkl"));
        assert!(!compatible_session_ids("a", "b"));
        assert_eq!(short_lane_id("[REDACTED:xyz]ab-c1d2"), "c1d2");
        assert_eq!(short_lane_id("ab!"), "ab");
        assert_eq!(short_lane_id("[unclosed-bracket-a1b2"), "a1b2");
        let rec = |sid: &str, name: &str| LaneRecord {
            session_id: sid.to_string(),
            parent_id: None,
            depth: 1,
            started_at: 0.0,
            lane: LaneState::for_state(name, LaneStateName::Running),
        };
        assert_eq!(
            lane_labels(&[rec("s-aaaa", "tw"), rec("s-bbbb", "tw"), rec("s-cccc", "rev")]),
            vec!["tw #aaaa", "tw #bbbb", "rev"]
        );
        assert_eq!(
            lane_labels(&[rec("x-aaaa", "tw"), rec("y-aaaa", "tw")]),
            vec!["tw #aaaa", "tw #2"]
        );
        assert_eq!(lane_labels(&[rec("!!", "tw"), rec("??", "tw")]), vec!["tw #1", "tw #2"]);
        // Redacted spawn then real session:start rekeys the lane; the
        // redacted id stays an alias so its completion still lands.
        let mut reg = LaneRegistry::new();
        reg.register("[REDACTED:head]-tail-abcdef123456", Some("root"), "child", opts());
        let rebound = reg.bind_session("real-tail-abcdef123456", Some("root")).unwrap();
        assert_eq!(rebound.session_id, "real-tail-abcdef123456");
        assert_eq!(
            reg.lanes().iter().map(|r| r.session_id.clone()).collect::<Vec<_>>(),
            vec!["real-tail-abcdef123456"]
        );
        let done = reg.complete("[REDACTED:head]-tail-abcdef123456", "ok").unwrap();
        assert_eq!(done.lane.state, LaneStateName::Done);
        assert_eq!(done.session_id, "real-tail-abcdef123456");
        // bind before register: parked pending, drained by the later spawn.
        let mut reg2 = LaneRegistry::new();
        assert!(reg2.bind_session("real-tail-abcdef123456", None).is_none());
        let record = reg2.register("[REDACTED:head]-tail-abcdef123456", None, "child", opts());
        assert_eq!(record.session_id, "real-tail-abcdef123456");
        assert_eq!(
            reg2.lanes().iter().map(|r| r.session_id.clone()).collect::<Vec<_>>(),
            vec!["real-tail-abcdef123456"]
        );
    }

    /// Pins Python `test_note_stream_activity_ignores_done_and_unknown_lanes`.
    #[test]
    fn test_note_stream_activity_ignores_done_and_unknown_lanes() {
        let mut lanes = LaneRegistry::new();
        lanes.register("s1", Some("root"), "researcher", opts());
        lanes.note_stream_activity("never-registered"); // dropped, not fatal
        lanes.complete("s1", "");
        lanes.note_stream_activity("s1"); // done lanes never become the tail
        assert!(lanes.tail_lane().is_none());
    }
}
