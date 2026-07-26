//! Evidence links for real sessions (DESIGN-SPEC §10, ADR-0007 resolution 9).
//!
//! The demo script ships hand-authored claims; a real session derives them
//! from the same normalized UIEvent stream that ui-events.jsonl records
//! (ADR-0007: the event log "powers … evidence links"). The collector taps
//! the queue bridge, keeps the running turn's completed top-level tool
//! calls, and when [`PromptComplete`] identifies the production final answer
//! it pairs the answer's leading sentences (verbatim excerpts) with the
//! turn's tool calls in order — rendering as the mockup's
//! `¹ "quote" → <tool call>` block.
//!
//! Port of `src/amplifier_app_newtui/kernel/evidence.py`.

use serde_json::{Map, Value};

use crate::kernel::events::UIEvent;
use crate::model::evidence::EvidenceLink;

/// Cap on derived claims per answer (the mockup block stays compact).
pub const MAX_CLAIMS: usize = 4;

/// Claim quotes stay short phrases; cut at a word boundary, verbatim.
pub const QUOTE_MAX_CHARS: usize = 60;

/// Tool refs are one-line human-readable references.
pub const REF_MAX_CHARS: usize = 60;

/// First present string input becomes the tool ref's detail hint.
const HINT_KEYS: [&str; 6] = ["command", "file_path", "path", "pattern", "url", "query"];

/// Whether a session id names the top-level session.
///
/// Inline port of `kernel/persistence.py::is_top_level_session` (the
/// persistence backend stays Python-side; only this pure helper is
/// needed here): spawned sub-sessions carry `_` (`{parent}-{hex}_{agent}`).
pub fn is_top_level_session(session_id: &str) -> bool {
    !session_id.contains('_')
}

/// Python `str(value)` for the JSON scalars this module reads out of
/// event payload maps (local copy of the events-module helper, plus the
/// `str(None) == "None"` case that `.get(key)` without a default hits).
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Null => "None".to_string(),
        Value::Number(n) => n.to_string(),
        // Containers: JSON text stands in for Python's repr (untested
        // shapes — evidence payloads carry scalars in practice).
        other => other.to_string(),
    }
}

/// Emulate Python's `re.split(r"(?<=[.!?])\s+|\n+", text)` (the `regex`
/// crate has no lookbehind): a whitespace run immediately after `.`/`!`/`?`
/// splits, as does any bare newline run. Alternation order matters — at a
/// position where both branches could start, the lookbehind branch wins
/// and consumes the whole `\s+` run (newlines included), exactly as
/// Python's left-to-right alternation does.
fn split_sentences(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut pieces: Vec<String> = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;
    while i < chars.len() {
        let after_terminal = i > 0 && matches!(chars[i - 1], '.' | '!' | '?');
        if after_terminal && chars[i].is_whitespace() {
            pieces.push(chars[start..i].iter().collect());
            let mut j = i + 1;
            while j < chars.len() && chars[j].is_whitespace() {
                j += 1;
            }
            start = j;
            i = j;
        } else if chars[i] == '\n' {
            pieces.push(chars[start..i].iter().collect());
            let mut j = i + 1;
            while j < chars.len() && chars[j] == '\n' {
                j += 1;
            }
            start = j;
            i = j;
        } else {
            i += 1;
        }
    }
    pieces.push(chars[start..].iter().collect());
    pieces
}

/// Python `text if len(text) <= limit else text[: limit - 1].rstrip() + "…"`
/// — lengths and slices are in characters, as in Python.
fn clip(text: &str, limit: usize) -> String {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= limit {
        return text.to_string();
    }
    let head: String = chars[..limit - 1].iter().collect();
    format!("{}…", head.trim_end())
}

/// A short verbatim excerpt of `sentence` (word-boundary prefix).
fn quote(sentence: &str) -> String {
    let stripped = sentence.trim();
    let chars: Vec<char> = stripped.chars().collect();
    let cut: String = if chars.len() > QUOTE_MAX_CHARS {
        // Python: `head, _, _ = sentence[: QUOTE_MAX_CHARS + 1].rpartition(" ")`
        // then `head or sentence[:QUOTE_MAX_CHARS]`.
        let window: String = chars[..QUOTE_MAX_CHARS + 1].iter().collect();
        match window.rfind(' ') {
            Some(idx) if idx > 0 => window[..idx].to_string(),
            _ => chars[..QUOTE_MAX_CHARS].iter().collect(),
        }
    } else {
        stripped.to_string()
    };
    // Python `rstrip(".!?,;: ")`.
    cut.trim_end_matches(['.', '!', '?', ',', ';', ':', ' '])
        .to_string()
}

/// Human-readable reference to one grounding tool call (spec §10).
pub fn tool_ref(tool_name: &str, tool_input: &Map<String, Value>) -> String {
    let mut hint = String::new();
    for key in HINT_KEYS {
        if let Some(Value::String(value)) = tool_input.get(key) {
            if !value.trim().is_empty() {
                // Python `" ".join(value.split())` — collapse whitespace runs.
                hint = value.split_whitespace().collect::<Vec<_>>().join(" ");
                break;
            }
        }
    }
    if tool_name == "bash" && !hint.is_empty() {
        return clip(&format!("$ {hint}"), REF_MAX_CHARS);
    }
    if !hint.is_empty() {
        return clip(&format!("{tool_name} · {hint}"), REF_MAX_CHARS);
    }
    tool_name.to_string()
}

/// Pair the answer's leading sentences with the turn's tool calls.
///
/// `calls` is `(tool_ref, tool_call_id)` in completion order. The
/// pairing is positional (sentence i ↔ call i) — deterministic, and
/// every claim quote is a verbatim excerpt of `answer_text`.
pub fn derive_links(answer_text: &str, calls: &[(String, String)]) -> Vec<EvidenceLink> {
    let sentences: Vec<String> = split_sentences(answer_text)
        .into_iter()
        .filter(|s| !s.trim().is_empty())
        .collect();
    let mut links: Vec<EvidenceLink> = Vec::new();
    for (sentence, (tool_ref, call_id)) in sentences.iter().zip(calls.iter()) {
        let claim_quote = quote(sentence);
        if claim_quote.is_empty() {
            continue;
        }
        links.push(EvidenceLink {
            claim_quote,
            tool_ref: tool_ref.clone(),
            tool_call_id: call_id.clone(),
        });
        if links.len() >= MAX_CLAIMS {
            break;
        }
    }
    links
}

/// Queue-bridge tap: the turn's tool calls → per-answer evidence.
///
/// `observe` sees every normalized UIEvent at emit time — strictly
/// before the reducer consumes it from the queue — so by the time the
/// reducer finalizes an Answer block and asks [`links_for`], the links
/// for that exact final response are already derived. Explicit demo
/// answers retain their immediate content-block binding.
///
/// [`links_for`]: EvidenceCollector::links_for
#[derive(Debug, Default)]
pub struct EvidenceCollector {
    calls: Vec<(String, String)>,
    by_answer: std::collections::HashMap<String, Vec<EvidenceLink>>,
}

impl EvidenceCollector {
    pub fn new() -> Self {
        Self::default()
    }

    /// Track one emitted event (top-level session only, spec §8).
    pub fn observe(&mut self, event: &UIEvent) {
        if !is_top_level_session(event.session_id()) {
            return; // subagent lanes ground their own transcripts
        }
        match event {
            UIEvent::PromptSubmit(_) => self.calls.clear(),
            UIEvent::ToolPost(post) => {
                if post.tool_name == "update_plan" {
                    return; // plan updates are not grounding evidence
                }
                let status = post.result.get("status").map(py_str).unwrap_or_default();
                if status == "denied" {
                    return; // a denied call ran nothing — grounds no claim
                }
                self.calls.push((
                    tool_ref(&post.tool_name, &post.tool_input),
                    post.tool_call_id.clone(),
                ));
            }
            UIEvent::ContentBlockEnd(end) => {
                if end.block_type != "text" {
                    return;
                }
                let text = end.block.get("text").map(py_str).unwrap_or_default();
                let role = end.block.get("demo_role").and_then(Value::as_str);
                if text.is_empty() || role != Some("answer") {
                    return; // production text is provisional; demo non-answers are not targets
                }
                let links = derive_links(&text, &self.calls);
                self.by_answer.insert(text, links);
            }
            UIEvent::PromptComplete(complete) => {
                let text = complete.response.trim();
                if !text.is_empty() {
                    let links = derive_links(text, &self.calls);
                    self.by_answer.insert(text.to_string(), links);
                }
            }
            _ => {}
        }
    }

    /// Evidence links derived for the answer with this exact text.
    pub fn links_for(&self, answer_text: &str) -> &[EvidenceLink] {
        self.by_answer
            .get(answer_text)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

// --------------------------------------------------------------------------
// Tests — ports of tests/test_kernel_evidence.py. The two bridge-tap tests
// there (`test_bridge_tap_sees_events_before_queue`,
// `test_bridge_tap_failure_never_blocks_the_queue`) pin the
// kernel/queue_bridge unit, which is not ported yet — they are not
// evidence.py behavior and are skipped here.
// --------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kernel::events::{ContentBlockEnd, PromptComplete, PromptSubmit, ToolPost};
    use serde_json::json;

    const SID: &str = "sess-1";

    fn obj(value: Value) -> Map<String, Value> {
        match value {
            Value::Object(map) => map,
            _ => panic!("payload literal must be a JSON object"),
        }
    }

    fn calls_of(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(r, c)| (r.to_string(), c.to_string()))
            .collect()
    }

    /// Python test helper `_tool_post` (defaults: name="bash", call_id="c1",
    /// command="uv run pytest -q", result={"status": "success"}, session=SID).
    fn tool_post(
        name: &str,
        call_id: &str,
        command: &str,
        result: Option<Value>,
        session_id: &str,
    ) -> UIEvent {
        let tool_input = if command.is_empty() {
            Map::new()
        } else {
            obj(json!({"command": command}))
        };
        let result = obj(result.unwrap_or_else(|| json!({"status": "success"})));
        UIEvent::ToolPost(ToolPost {
            session_id: session_id.to_string(),
            tool_name: name.to_string(),
            tool_call_id: call_id.to_string(),
            tool_input,
            result,
            ..Default::default()
        })
    }

    fn default_tool_post() -> UIEvent {
        tool_post("bash", "c1", "uv run pytest -q", None, SID)
    }

    /// Python test helper `_answer` (block_type="text", block carries
    /// `type`/`text` plus any extra keys such as `demo_role`).
    fn answer(text: &str, demo_role: Option<&str>) -> UIEvent {
        let mut block = obj(json!({"type": "text", "text": text}));
        if let Some(role) = demo_role {
            block.insert("demo_role".to_string(), json!(role));
        }
        UIEvent::ContentBlockEnd(ContentBlockEnd {
            session_id: SID.to_string(),
            block_type: "text".to_string(),
            block,
            ..Default::default()
        })
    }

    fn prompt_submit(prompt: &str) -> UIEvent {
        UIEvent::PromptSubmit(PromptSubmit {
            session_id: SID.to_string(),
            prompt: prompt.to_string(),
            ..Default::default()
        })
    }

    fn prompt_complete(response: &str) -> UIEvent {
        UIEvent::PromptComplete(PromptComplete {
            session_id: SID.to_string(),
            response: response.to_string(),
            ..Default::default()
        })
    }

    // -- derive_links / tool_ref (pure derivation) ---------------------------

    #[test]
    fn test_derive_pairs_sentences_with_calls_in_order() {
        let answer = "All 41 tests pass. The store migration is verified.";
        let calls = calls_of(&[
            ("$ uv run pytest -q", "c1"),
            ("read_file · store.py", "c2"),
            ("grep", "c3"),
        ]);
        let links = derive_links(answer, &calls);
        assert_eq!(links.len(), 2); // bounded by sentence count
        assert_eq!(links[0].claim_quote, "All 41 tests pass");
        assert_eq!(links[0].tool_ref, "$ uv run pytest -q");
        assert_eq!(links[0].tool_call_id, "c1");
        assert_eq!(links[1].tool_ref, "read_file · store.py");
        // Every claim quote is a verbatim excerpt of the answer (spec §10).
        for link in &links {
            assert!(answer.contains(&link.claim_quote));
        }
    }

    #[test]
    fn test_derive_bounded_by_calls_and_cap() {
        let answer = (0..10)
            .map(|i| format!("Sentence number {i} here"))
            .collect::<Vec<_>>()
            .join(". ")
            + ".";
        assert!(derive_links(&answer, &[]).is_empty());
        let many: Vec<(String, String)> =
            (0..10).map(|i| (format!("tool-{i}"), format!("c{i}"))).collect();
        assert_eq!(derive_links(&answer, &many).len(), MAX_CLAIMS);
    }

    #[test]
    fn test_quote_cut_at_word_boundary_stays_verbatim() {
        let long_sentence = "word ".repeat(40); // single sentence far beyond the cap
        let links = derive_links(&long_sentence, &calls_of(&[("$ ls", "c1")]));
        assert_eq!(links.len(), 1);
        assert!(links[0].claim_quote.chars().count() <= QUOTE_MAX_CHARS);
        assert!(long_sentence.contains(&links[0].claim_quote));
        assert!(!links[0].claim_quote.ends_with(' '));
    }

    #[test]
    fn test_tool_ref_shapes() {
        assert_eq!(
            tool_ref("bash", &obj(json!({"command": "git  status"}))),
            "$ git status"
        );
        assert_eq!(
            tool_ref("read_file", &obj(json!({"file_path": "src/app.py"}))),
            "read_file · src/app.py"
        );
        assert_eq!(tool_ref("web_search", &Map::new()), "web_search");
        let clipped = tool_ref("bash", &obj(json!({"command": "x".repeat(200)})));
        assert_eq!(clipped.chars().count(), 60);
        assert!(clipped.ends_with('…'));
    }

    // -- EvidenceCollector (event-stream behavior) ---------------------------

    #[test]
    fn test_collector_derives_links_for_answer() {
        let mut collector = EvidenceCollector::new();
        collector.observe(&prompt_submit("check the tests"));
        collector.observe(&default_tool_post());
        let answer_text = "All 41 tests pass with no flakes.";
        // Production content blocks are provisional narration.  The synthesized
        // prompt close-out identifies the authoritative final answer.
        collector.observe(&answer("I am checking the tests now.", None));
        assert!(collector.links_for("I am checking the tests now.").is_empty());
        collector.observe(&prompt_complete(answer_text));
        let links = collector.links_for(answer_text);
        assert_eq!(links.len(), 1);
        assert_eq!(links[0].claim_quote, "All 41 tests pass with no flakes");
        assert_eq!(links[0].tool_ref, "$ uv run pytest -q");
        assert_eq!(links[0].tool_call_id, "c1");
        assert!(collector.links_for("some other answer").is_empty());
    }

    #[test]
    fn test_collector_resets_calls_each_turn() {
        let mut collector = EvidenceCollector::new();
        collector.observe(&prompt_submit("one"));
        collector.observe(&default_tool_post());
        collector.observe(&prompt_submit("two"));
        let answer_text = "Nothing ran this turn.";
        collector.observe(&prompt_complete(answer_text));
        assert!(collector.links_for(answer_text).is_empty());
    }

    #[test]
    fn test_collector_skips_non_grounding_events() {
        let mut collector = EvidenceCollector::new();
        collector.observe(&prompt_submit("go"));
        // Denied calls, plan updates and subagent-lane calls ground nothing.
        collector.observe(&tool_post(
            "bash",
            "c1",
            "uv run pytest -q",
            Some(json!({"status": "denied", "reason": "trust"})),
            SID,
        ));
        collector.observe(&tool_post("update_plan", "c2", "", None, SID));
        collector.observe(&tool_post(
            "bash",
            "c3",
            "uv run pytest -q",
            None,
            "sess-1-ab_researcher",
        ));
        let answer_text = "Nothing was actually executed.";
        collector.observe(&prompt_complete(answer_text));
        assert!(collector.links_for(answer_text).is_empty());
    }

    #[test]
    fn test_collector_ignores_narration_and_non_text() {
        let mut collector = EvidenceCollector::new();
        collector.observe(&prompt_submit("go"));
        collector.observe(&default_tool_post());
        let narration = "Applying steer: keep the journal";
        collector.observe(&answer(narration, Some("narration")));
        assert!(collector.links_for(narration).is_empty());
        collector.observe(&UIEvent::ContentBlockEnd(ContentBlockEnd {
            session_id: SID.to_string(),
            block_type: "thinking".to_string(),
            block: obj(json!({"text": "hmm"})),
            ..Default::default()
        }));
        assert!(collector.links_for("hmm").is_empty());
    }

    #[test]
    fn test_collector_preserves_explicit_demo_answer_binding() {
        let mut collector = EvidenceCollector::new();
        collector.observe(&prompt_submit("demo"));
        collector.observe(&default_tool_post());
        let answer_text = "The scripted answer is final.";
        collector.observe(&answer(answer_text, Some("answer")));
        assert_eq!(collector.links_for(answer_text).len(), 1);
    }

    // test_bridge_tap_sees_events_before_queue and
    // test_bridge_tap_failure_never_blocks_the_queue pin
    // kernel/queue_bridge.py (QueueBridge), not evidence.py — ported with
    // that unit, not here.
}
