//! The wire protocol between the Rust front-end and the Python `serve` backend.
//! Decodes the app's REAL normalized vocabulary (`kernel/events.py` kinds, in the
//! schema-v1 JSONL envelope) plus the one record `run` can't emit —
//! `approval.required` (carries the broker ticket id). Submissions go the other
//! way: `submit` / `approve` (ticket + broker choice) / `interrupt`.

use crate::event::UiEvent;
use crate::kernel::events::UIEvent as KernelEvent;
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
        // Provider telemetry: decode through the ported kernel event type so
        // the wire shape (pydantic JSON dump, Decimal-as-string `cost_usd`)
        // is parsed in exactly one place.
        "provider_response_usage" => match serde_json::from_value(e.clone()).ok()? {
            KernelEvent::ProviderResponseUsage(usage) => Some(UiEvent::Usage(usage)),
            _ => None,
        },
        // End-of-turn close-out with the shipped-outcome yield.
        "prompt_complete" => Some(UiEvent::TurnComplete {
            files: e["files_changed"].as_u64().unwrap_or(0) as u32,
            added: 0,
            removed: 0,
            tokens: 0,
            cost: 0.0,
        }),
        _ => None, // other telemetry/lifecycle kinds not surfaced in the MVP
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

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal::Decimal;
    use std::str::FromStr;

    /// Verbatim serve wire line, captured from the real Python side:
    /// `JsonlRecords().runtime_event(ProviderResponseUsage(...)).model_dump(mode="json")`
    /// serialized with `json.dumps(..., default=str)` exactly as
    /// `kernel/serve.py` does. `cost_usd` is null (pricing-table path).
    const USAGE_LINE: &str = r#"{"schema_version": 1, "sequence": 4, "timestamp": "2026-07-26T18:24:58.726014+00:00", "type": "runtime.event", "event": {"event_id": "ev1", "session_id": "core-01", "parent_id": null, "ts": 1785090298.719282, "kind": "provider_response_usage", "input_tokens": 1200, "output_tokens": 340, "cache_read": 800, "cache_write": 100, "model": "claude-sonnet-4-5", "cost_usd": null}}"#;

    /// Same wire shape with a provider-reported Decimal `cost_usd`, which
    /// pydantic/`default=str` puts on the wire as a STRING.
    const USAGE_LINE_WITH_COST: &str = r#"{"schema_version": 1, "sequence": 5, "timestamp": "2026-07-26T18:24:58.727181+00:00", "type": "runtime.event", "event": {"event_id": "ev2", "session_id": "core-01", "parent_id": null, "ts": 1785090298.727175, "kind": "provider_response_usage", "input_tokens": 10, "output_tokens": 5, "cache_read": 0, "cache_write": 0, "model": "", "cost_usd": "0.0123"}}"#;

    #[test]
    fn decodes_serve_shaped_provider_response_usage() {
        let ev = decode_event(USAGE_LINE).expect("usage line decodes");
        let UiEvent::Usage(usage) = ev else {
            panic!("expected UiEvent::Usage, got {ev:?}");
        };
        assert_eq!(usage.event_id, "ev1");
        assert_eq!(usage.session_id, "core-01");
        assert_eq!(usage.input_tokens, 1200);
        assert_eq!(usage.output_tokens, 340);
        assert_eq!(usage.cache_read, 800);
        assert_eq!(usage.cache_write, 100);
        assert_eq!(usage.model, "claude-sonnet-4-5");
        assert_eq!(usage.cost_usd, None);
    }

    #[test]
    fn decodes_provider_reported_cost_usd_string() {
        let ev = decode_event(USAGE_LINE_WITH_COST).expect("usage line decodes");
        let UiEvent::Usage(usage) = ev else {
            panic!("expected UiEvent::Usage, got {ev:?}");
        };
        assert_eq!(usage.output_tokens, 5);
        assert_eq!(usage.cost_usd, Some(Decimal::from_str("0.0123").unwrap()));
    }
}
