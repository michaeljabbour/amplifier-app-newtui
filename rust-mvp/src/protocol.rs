//! The wire protocol between the Rust front-end and the Python `serve` backend.
//!
//! Decodes the app's REAL normalized vocabulary (`kernel/events.py` kinds, in
//! the schema-v1 JSONL envelope) into the ported [`crate::kernel::events`]
//! types via [`crate::kernel::events::parse_event`] — one decode path for the
//! wire AND resume replay — plus the two records `run` can't emit as runtime
//! events: `approval.required` (carries the broker ticket id) and
//! `session.started` (session identity for the title/footer). Submissions go
//! the other way: `submit` / `approve` (ticket + broker choice) / `interrupt`.

use crate::kernel::events::{parse_event, UIEvent};
use serde_json::{json, Value};

/// One decoded backend line, as the app-loop queue carries it.
#[derive(Clone, Debug, PartialEq)]
pub enum WireEvent {
    /// A normalized `runtime.event` (real kinds from kernel/events.py).
    Event(UIEvent),
    /// The interactive-only record: an approval with its broker ticket id.
    Approval {
        ticket_id: String,
        prompt: String,
        options: Vec<String>,
    },
    /// Session identity landed (`session.started`) — fills the title bar
    /// and footer; the analogue of the Python adapter's banner fields.
    SessionStarted {
        session_id: String,
        bundle: String,
        model: String,
    },
    /// A boot phase before `session.started` (`boot.progress`): the
    /// `(action, detail)` pairs RealRuntime reports through `on_progress`
    /// while modules load — the splash shows them instead of a blank screen.
    BootProgress { action: String, detail: String },
    /// serve's terminal `error` record (`kernel/serve.py`): a boot failure
    /// (emitted instead of `session.started`, then exit 1) or a failed turn
    /// (`_run_turn`, which also carries `session_id`). Dropping these left
    /// the splash hanging forever on a dead backend.
    Error { error: String, error_type: String },
}

/// Decode one backend stdout line into a [`WireEvent`] (or `None` to ignore).
///
/// Unknown record types and unknown/foreign `runtime.event` kinds are
/// ignored, exactly as `parse_event` treats foreign log lines.
pub fn decode_wire(line: &str) -> Option<WireEvent> {
    let v: Value = serde_json::from_str(line).ok()?;
    match v["type"].as_str()? {
        "runtime.event" => parse_event(&v["event"]).map(WireEvent::Event),
        "approval.required" => Some(WireEvent::Approval {
            ticket_id: v["ticket_id"].as_str().unwrap_or("").to_string(),
            prompt: v["prompt"].as_str().unwrap_or("").to_string(),
            options: v["options"]
                .as_array()
                .map(|options| {
                    options
                        .iter()
                        .filter_map(|option| option.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default(),
        }),
        "session.started" => Some(WireEvent::SessionStarted {
            session_id: v["session_id"].as_str().unwrap_or("").to_string(),
            bundle: v["bundle"].as_str().unwrap_or("").to_string(),
            model: v["model"].as_str().unwrap_or("").to_string(),
        }),
        "boot.progress" => Some(WireEvent::BootProgress {
            action: v["action"].as_str().unwrap_or("").to_string(),
            detail: v["detail"].as_str().unwrap_or("").to_string(),
        }),
        "error" => Some(WireEvent::Error {
            error: v["error"].as_str().unwrap_or("").to_string(),
            error_type: v["error_type"].as_str().unwrap_or("").to_string(),
        }),
        // turn.completed is lifecycle, not transcript.
        _ => None,
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

    // Adapted from the pre-assembly `decodes_serve_shaped_provider_response_usage`:
    // the wire now decodes into the ported kernel UIEvent directly (one
    // decode path with resume replay) instead of the legacy thin enum.
    #[test]
    fn decodes_serve_shaped_provider_response_usage() {
        let ev = decode_wire(USAGE_LINE).expect("usage line decodes");
        let WireEvent::Event(UIEvent::ProviderResponseUsage(usage)) = ev else {
            panic!("expected ProviderResponseUsage, got {ev:?}");
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
        let ev = decode_wire(USAGE_LINE_WITH_COST).expect("usage line decodes");
        let WireEvent::Event(UIEvent::ProviderResponseUsage(usage)) = ev else {
            panic!("expected ProviderResponseUsage, got {ev:?}");
        };
        assert_eq!(usage.output_tokens, 5);
        assert_eq!(usage.cost_usd, Some(Decimal::from_str("0.0123").unwrap()));
    }

    #[test]
    fn decodes_approval_required_with_ticket_and_options() {
        let line = r#"{"schema_version": 1, "type": "approval.required", "ticket_id": "approval-1", "prompt": "write_file src/health.py", "options": ["Allow once", "Allow always", "Deny"]}"#;
        let ev = decode_wire(line).expect("approval line decodes");
        assert_eq!(
            ev,
            WireEvent::Approval {
                ticket_id: "approval-1".into(),
                prompt: "write_file src/health.py".into(),
                options: vec!["Allow once".into(), "Allow always".into(), "Deny".into()],
            }
        );
    }

    #[test]
    fn decodes_session_started_identity() {
        let line = r#"{"schema_version": 1, "sequence": 1, "timestamp": 1.0, "type": "session.started", "session_id": "core-01", "bundle": "newtui", "model": "claude-sonnet-4-5"}"#;
        let ev = decode_wire(line).expect("session line decodes");
        assert_eq!(
            ev,
            WireEvent::SessionStarted {
                session_id: "core-01".into(),
                bundle: "newtui".into(),
                model: "claude-sonnet-4-5".into(),
            }
        );
    }

    // Pins the record tests/test_serve_offline.py::test_serve_emits_boot_
    // progress_records_before_session_started puts on the wire.
    #[test]
    fn decodes_boot_progress_action_and_detail() {
        let line = r#"{"schema_version": 1, "type": "boot.progress", "action": "installing_package", "detail": "tool-bash"}"#;
        let ev = decode_wire(line).expect("boot.progress line decodes");
        assert_eq!(
            ev,
            WireEvent::BootProgress {
                action: "installing_package".into(),
                detail: "tool-bash".into(),
            }
        );
    }

    // Pins the two `error` shapes kernel/serve.py puts on the wire: the
    // boot-failure terminal record (serve() except-arm, then exit 1) and a
    // failed turn (_run_turn, which adds session_id).
    #[test]
    fn decodes_serve_error_records() {
        let boot = r#"{"schema_version": 1, "type": "error", "error": "no provider configured", "error_type": "RuntimeError"}"#;
        assert_eq!(
            decode_wire(boot),
            Some(WireEvent::Error {
                error: "no provider configured".into(),
                error_type: "RuntimeError".into(),
            })
        );
        let turn = r#"{"schema_version": 1, "type": "error", "session_id": "core-01", "error": "provider auth expired", "error_type": "APIStatusError"}"#;
        assert_eq!(
            decode_wire(turn),
            Some(WireEvent::Error {
                error: "provider auth expired".into(),
                error_type: "APIStatusError".into(),
            })
        );
    }

    #[test]
    fn ignores_lifecycle_and_foreign_lines() {
        assert_eq!(decode_wire(r#"{"type": "turn.completed"}"#), None);
        assert_eq!(decode_wire("not json"), None);
        // Unknown runtime-event kinds fail parse_event validation → skipped.
        assert_eq!(
            decode_wire(r#"{"type": "runtime.event", "event": {"kind": "notice", "text": "x"}}"#),
            None
        );
    }
}
