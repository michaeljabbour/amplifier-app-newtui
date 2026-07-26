//! The wire protocol between the Rust front-end and the Python `serve` backend.
//! Decodes the app's REAL normalized vocabulary (`kernel/events.py` kinds, in the
//! schema-v1 JSONL envelope) plus the one record `run` can't emit —
//! `approval.required` (carries the broker ticket id). Submissions go the other
//! way: `submit` / `approve` (ticket + broker choice) / `interrupt`.

use crate::event::UiEvent;
use serde_json::{json, Value};

/// Decode one backend stdout line into a `UiEvent` (or `None` to ignore).
pub fn decode_event(line: &str) -> Option<UiEvent> {
    let v: Value = serde_json::from_str(line).ok()?;
    match v["type"].as_str()? {
        // The interactive-only record: an approval with its broker ticket id.
        "approval.required" => Some(UiEvent::ApprovalRequired {
            ticket_id: v["ticket_id"].as_str().unwrap_or("").to_string(),
            action: v["prompt"].as_str().unwrap_or("").to_string(),
        }),
        // A normalized UIEvent, real kinds from kernel/events.py.
        "runtime.event" => decode_runtime_event(&v["event"]),
        // session.started / turn.completed / error are lifecycle, not transcript.
        _ => None,
    }
}

fn decode_runtime_event(e: &Value) -> Option<UiEvent> {
    let s = |k: &str| e[k].as_str().unwrap_or("").to_string();
    match e["kind"].as_str()? {
        "prompt_submit" => Some(UiEvent::PromptSubmit(s("prompt"))),
        // Channel A — live streaming deltas.
        "stream_block_start" => Some(UiEvent::StreamStart),
        "stream_block_delta" => Some(UiEvent::StreamDelta(s("text"))),
        "stream_block_end" => Some(UiEvent::StreamEnd),
        // Channel B — durable tool records.
        "tool_post" => Some(UiEvent::ToolLine {
            summary: tool_summary(e),
            ok: true,
        }),
        "tool_error" => Some(UiEvent::ToolLine {
            summary: format!("{} ({})", s("tool_name"), s("error_type")),
            ok: false,
        }),
        "notification" => Some(UiEvent::Notice(s("message"))),
        // End-of-turn close-out with the shipped-outcome yield.
        "prompt_complete" => Some(UiEvent::TurnComplete {
            files: e["files_changed"].as_u64().unwrap_or(0) as u32,
            added: 0,
            removed: 0,
            tokens: 0,
            cost: 0.0,
        }),
        _ => None, // usage/telemetry/lifecycle kinds not surfaced in the MVP
    }
}

/// A compact one-line summary from a `tool_post` (name + a hint of the input).
fn tool_summary(e: &Value) -> String {
    let name = e["tool_name"].as_str().unwrap_or("tool");
    if let Some(summary) = e["result"]["summary"].as_str() {
        format!("{name} · {summary}")
    } else {
        name.to_string()
    }
}

pub fn submit(text: &str) -> Value {
    json!({ "op": "submit", "text": text })
}
pub fn approve(ticket_id: &str, choice: &str) -> Value {
    json!({ "op": "approve", "ticket_id": ticket_id, "choice": choice })
}
pub fn interrupt() -> Value {
    json!({ "op": "interrupt" })
}
