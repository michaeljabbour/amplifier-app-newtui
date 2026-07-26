//! Live provider runtime — a real turn against the Anthropic Messages API,
//! streamed, in pure Rust. No Python, no amplifier-core. This is the seam where
//! a real engine plugs in; today it talks to the provider directly. It is an
//! illustrative shortcut, NOT the target architecture (that is
//! `core_client.rs`); it now speaks the same normalized kernel vocabulary so
//! the assembled reducer renders it (pricing via `kernel::cost`, answer made
//! durable through `prompt_complete.response`).
//!
//! The SSE→UIEvent normalization (`SseNormalizer`) is the actual integration
//! logic and is unit-tested offline against captured stream fixtures, so it is
//! verified even without a key or network.

use crate::kernel::events as ev;
use crate::message::Msg;
use crate::protocol::WireEvent;
use crate::runtime::Runtime;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Stateful SSE decoder: fold Anthropic stream events into normalized
/// kernel [`ev::UIEvent`]s. Cost is NOT computed here — the usage event is
/// priced by `kernel::cost` in the reducer, exactly like serve traffic.
pub struct SseNormalizer {
    pub input_tokens: i64,
    pub output_tokens: i64,
    model: String,
    session_id: String,
    answer: String,
}

impl SseNormalizer {
    pub fn new(model: &str, session_id: &str) -> Self {
        Self {
            input_tokens: 0,
            output_tokens: 0,
            model: model.to_string(),
            session_id: session_id.to_string(),
            answer: String::new(),
        }
    }

    /// The accumulated answer text (becomes `prompt_complete.response`).
    pub fn answer(&self) -> &str {
        &self.answer
    }

    /// One parsed `data:` JSON object → zero or more UI events.
    pub fn on_data(&mut self, v: &Value) -> Vec<ev::UIEvent> {
        let session = self.session_id.clone();
        match v["type"].as_str().unwrap_or("") {
            "message_start" => {
                self.input_tokens = v["message"]["usage"]["input_tokens"].as_i64().unwrap_or(0);
                vec![ev::UIEvent::StreamBlockStart(ev::StreamBlockStart {
                    session_id: session,
                    ts: now_ts(),
                    ..ev::StreamBlockStart::default()
                })]
            }
            "content_block_delta" => {
                if v["delta"]["type"].as_str() == Some("text_delta") {
                    if let Some(t) = v["delta"]["text"].as_str() {
                        self.answer.push_str(t);
                        return vec![ev::UIEvent::StreamBlockDelta(ev::StreamBlockDelta {
                            session_id: session,
                            ts: now_ts(),
                            text: t.to_string(),
                            ..ev::StreamBlockDelta::default()
                        })];
                    }
                }
                vec![]
            }
            "message_delta" => {
                if let Some(o) = v["usage"]["output_tokens"].as_i64() {
                    self.output_tokens = o;
                }
                vec![]
            }
            "message_stop" => {
                vec![
                    ev::UIEvent::StreamBlockEnd(ev::StreamBlockEnd {
                        session_id: session.clone(),
                        ts: now_ts(),
                        ..ev::StreamBlockEnd::default()
                    }),
                    ev::UIEvent::ProviderResponseUsage(ev::ProviderResponseUsage {
                        session_id: session.clone(),
                        ts: now_ts(),
                        input_tokens: self.input_tokens,
                        output_tokens: self.output_tokens,
                        model: self.model.clone(),
                        ..ev::ProviderResponseUsage::default()
                    }),
                    ev::UIEvent::PromptComplete(ev::PromptComplete {
                        session_id: session,
                        ts: now_ts(),
                        response: self.answer.clone(),
                        ..ev::PromptComplete::default()
                    }),
                ]
            }
            _ => vec![],
        }
    }
}

pub struct LiveRuntime {
    tx: Sender<Msg>,
    history: Arc<Mutex<Vec<Value>>>,
    model: String,
    api_key: String,
}

pub const LIVE_SESSION_ID: &str = "live-01";

impl LiveRuntime {
    /// Build from environment; errors (falls back to demo) if no key is present.
    pub fn from_env(tx: Sender<Msg>) -> Result<Self, String> {
        let api_key = std::env::var("ANTHROPIC_API_KEY")
            .map_err(|_| "ANTHROPIC_API_KEY not set".to_string())?;
        let model = std::env::var("AMPLIFIER_MODEL")
            .unwrap_or_else(|_| "claude-sonnet-4-5".to_string());
        Ok(Self { tx, history: Arc::new(Mutex::new(Vec::new())), model, api_key })
    }

    pub fn model(&self) -> &str {
        &self.model
    }
}

fn build_agent() -> ureq::Agent {
    // Honor an outbound proxy if the environment sets one.
    if let Ok(proxy) = std::env::var("HTTPS_PROXY").or_else(|_| std::env::var("https_proxy")) {
        if let Ok(p) = ureq::Proxy::new(proxy) {
            return ureq::AgentBuilder::new().proxy(p).build();
        }
    }
    ureq::AgentBuilder::new().build()
}

impl Runtime for LiveRuntime {
    fn submit(&mut self, prompt: String) {
        self.history
            .lock()
            .unwrap()
            .push(json!({"role": "user", "content": prompt}));
        let _ = self.tx.send(Msg::Rt(WireEvent::Event(ev::UIEvent::PromptSubmit(
            ev::PromptSubmit {
                session_id: LIVE_SESSION_ID.into(),
                ts: now_ts(),
                prompt,
                ..ev::PromptSubmit::default()
            },
        ))));

        let tx = self.tx.clone();
        let history = self.history.clone();
        let model = self.model.clone();
        let api_key = self.api_key.clone();

        thread::spawn(move || {
            let send = |e: ev::UIEvent| {
                let _ = tx.send(Msg::Rt(WireEvent::Event(e)));
            };
            let messages = history.lock().unwrap().clone();
            let body = json!({
                "model": model,
                "max_tokens": 1024,
                "stream": true,
                "messages": messages,
            });

            let resp = build_agent()
                .post("https://api.anthropic.com/v1/messages")
                .set("x-api-key", &api_key)
                .set("anthropic-version", "2023-06-01")
                .set("content-type", "application/json")
                .send_string(&body.to_string());

            let resp = match resp {
                Ok(r) => r,
                Err(e) => {
                    send(ev::UIEvent::Notification(ev::Notification {
                        session_id: LIVE_SESSION_ID.into(),
                        ts: now_ts(),
                        message: format!("request failed: {e}"),
                        level: "warn".into(),
                        ..ev::Notification::default()
                    }));
                    send(ev::UIEvent::PromptComplete(ev::PromptComplete {
                        session_id: LIVE_SESSION_ID.into(),
                        ts: now_ts(),
                        ..ev::PromptComplete::default()
                    }));
                    return;
                }
            };

            let mut norm = SseNormalizer::new(&model, LIVE_SESSION_ID);
            let reader = BufReader::new(resp.into_reader());
            for line in reader.lines() {
                let Ok(line) = line else { break };
                let Some(data) = line.strip_prefix("data: ") else { continue };
                if data == "[DONE]" {
                    continue;
                }
                if let Ok(v) = serde_json::from_str::<Value>(data) {
                    for event in norm.on_data(&v) {
                        send(event);
                    }
                }
            }
            history
                .lock()
                .unwrap()
                .push(json!({"role": "assistant", "content": norm.answer().to_string()}));
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Feed a captured Anthropic SSE stream through the normalizer and assert
    /// it produces the right normalized event arc — fully offline, no
    /// key/network. Adapted from the pre-assembly test: the normalizer now
    /// emits kernel UIEvents and no longer computes f64 cost itself (the
    /// usage event is priced exactly by `kernel::cost` downstream).
    #[test]
    fn normalizes_a_captured_stream() {
        let fixture = [
            r#"{"type":"message_start","message":{"usage":{"input_tokens":40}}}"#,
            r#"{"type":"content_block_start","index":0}"#,
            r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}"#,
            r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":", world"}}"#,
            r#"{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"ignored"}}"#,
            r#"{"type":"message_delta","usage":{"output_tokens":12}}"#,
            r#"{"type":"message_stop"}"#,
        ];

        let mut norm = SseNormalizer::new("claude-sonnet-4-5", "live-01");
        let mut events = Vec::new();
        for line in fixture {
            let v: Value = serde_json::from_str(line).unwrap();
            events.extend(norm.on_data(&v));
        }

        // Expected arc: stream start, two text deltas, stream end, usage,
        // prompt_complete carrying the full answer.
        assert!(matches!(events[0], ev::UIEvent::StreamBlockStart(_)));
        let text: String = events
            .iter()
            .filter_map(|e| match e {
                ev::UIEvent::StreamBlockDelta(d) => Some(d.text.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(text, "Hello, world");
        assert!(matches!(events[events.len() - 3], ev::UIEvent::StreamBlockEnd(_)));
        let ev::UIEvent::ProviderResponseUsage(usage) = &events[events.len() - 2] else {
            panic!("expected usage, got {:?}", events[events.len() - 2]);
        };
        assert_eq!(usage.input_tokens, 40);
        assert_eq!(usage.output_tokens, 12);
        assert_eq!(usage.model, "claude-sonnet-4-5");
        // Priced downstream via the ported kernel::cost fallback table —
        // (40, 12, 0, 0, "claude-sonnet-4-5") → exactly $0.0003 (the same
        // figure the old inline f64 math produced, now exact Decimal).
        use crate::kernel::cost::CostTracker;
        let mut tracker = CostTracker::new();
        tracker.start_turn();
        tracker.record(usage);
        assert_eq!(
            tracker.session_cost(),
            rust_decimal::Decimal::from_str_exact("0.0003").unwrap()
        );
        let ev::UIEvent::PromptComplete(done) = events.last().unwrap() else {
            panic!("expected prompt_complete, got {:?}", events.last());
        };
        assert_eq!(done.response, "Hello, world");
    }
}
