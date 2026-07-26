//! The wire protocol between the Rust front-end and a backend process — the
//! externalized form of the app's in-process seam (normalized `UIEvent`s out,
//! `submit`/`approve`/`interrupt` in). Mirrors the repo's schema-v1 JSONL
//! envelope, extended with an input direction so the UI is fully interactive.

use crate::event::UiEvent;
use serde_json::{json, Value};

/// Decode one backend stdout line into a `UiEvent`. Non-event records
/// (`session.started`, unknown kinds) return `None` and are ignored by the UI.
pub fn decode_event(line: &str) -> Option<UiEvent> {
    let v: Value = serde_json::from_str(line).ok()?;
    if v["type"].as_str()? != "runtime.event" {
        return None;
    }
    let e = &v["event"];
    let s = |k: &str| e[k].as_str().unwrap_or("").to_string();
    match e["kind"].as_str()? {
        "prompt_submit" => Some(UiEvent::PromptSubmit(s("text"))),
        "narration" => Some(UiEvent::Narration(s("text"))),
        "tool_line" => Some(UiEvent::ToolLine {
            summary: s("summary"),
            ok: e["ok"].as_bool().unwrap_or(true),
        }),
        "approval_required" => Some(UiEvent::ApprovalRequired { action: s("action") }),
        "stream_start" => Some(UiEvent::StreamStart),
        "stream_delta" => Some(UiEvent::StreamDelta(s("text"))),
        "stream_end" => Some(UiEvent::StreamEnd),
        "notice" => Some(UiEvent::Notice(s("text"))),
        "turn_complete" => Some(UiEvent::TurnComplete {
            files: e["files"].as_u64().unwrap_or(0) as u32,
            added: e["added"].as_u64().unwrap_or(0) as u32,
            removed: e["removed"].as_u64().unwrap_or(0) as u32,
            tokens: e["tokens"].as_u64().unwrap_or(0),
            cost: e["cost"].as_f64().unwrap_or(0.0),
        }),
        _ => None,
    }
}

/// Encode the three submission ops the UI can send back to the backend.
pub fn submit(text: &str) -> Value {
    json!({ "op": "submit", "text": text })
}
pub fn approve(granted: bool) -> Value {
    json!({ "op": "approve", "granted": granted })
}
pub fn interrupt() -> Value {
    json!({ "op": "interrupt" })
}
