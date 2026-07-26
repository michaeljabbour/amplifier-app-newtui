//! Live provider runtime — a real turn against the Anthropic Messages API,
//! streamed, in pure Rust. No Python, no amplifier-core. This is the seam where
//! a real engine plugs in; today it talks to the provider directly.
//!
//! The SSE→`UiEvent` normalization (`SseNormalizer`) is the actual integration
//! logic and is unit-tested offline against captured stream fixtures, so it is
//! verified even without a key or network.

use crate::event::UiEvent;
use crate::message::Msg;
use crate::runtime::Runtime;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::thread;

/// Approximate provider pricing (USD per 1M tokens) — the app's `kernel/cost.py`
/// has the authoritative live table; this is a static fallback for the MVP.
fn price_for(model: &str) -> (f64, f64) {
    if model.contains("opus") {
        (15.0, 75.0)
    } else if model.contains("haiku") {
        (0.80, 4.0)
    } else {
        (3.0, 15.0) // sonnet family (default)
    }
}

/// Stateful SSE decoder: fold Anthropic stream events into normalized `UiEvent`s.
pub struct SseNormalizer {
    pub input_tokens: u64,
    pub output_tokens: u64,
    in_price: f64,
    out_price: f64,
}

impl SseNormalizer {
    pub fn new(model: &str) -> Self {
        let (in_price, out_price) = price_for(model);
        Self { input_tokens: 0, output_tokens: 0, in_price, out_price }
    }

    /// One parsed `data:` JSON object → zero or more UI events.
    pub fn on_data(&mut self, v: &Value) -> Vec<UiEvent> {
        match v["type"].as_str().unwrap_or("") {
            "message_start" => {
                self.input_tokens = v["message"]["usage"]["input_tokens"].as_u64().unwrap_or(0);
                vec![UiEvent::StreamStart]
            }
            "content_block_delta" => {
                if v["delta"]["type"].as_str() == Some("text_delta") {
                    if let Some(t) = v["delta"]["text"].as_str() {
                        return vec![UiEvent::StreamDelta(t.to_string())];
                    }
                }
                vec![]
            }
            "message_delta" => {
                if let Some(o) = v["usage"]["output_tokens"].as_u64() {
                    self.output_tokens = o;
                }
                vec![]
            }
            "message_stop" => {
                let cost = self.input_tokens as f64 / 1e6 * self.in_price
                    + self.output_tokens as f64 / 1e6 * self.out_price;
                vec![
                    UiEvent::StreamEnd,
                    UiEvent::TurnComplete {
                        files: 0,
                        added: 0,
                        removed: 0,
                        tokens: self.input_tokens + self.output_tokens,
                        cost,
                    },
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
        let _ = self.tx.send(Msg::Rt(UiEvent::PromptSubmit(prompt)));

        let tx = self.tx.clone();
        let history = self.history.clone();
        let model = self.model.clone();
        let api_key = self.api_key.clone();

        thread::spawn(move || {
            let send = |e: UiEvent| {
                let _ = tx.send(Msg::Rt(e));
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
                    send(UiEvent::Notice(format!("request failed: {e}")));
                    send(UiEvent::TurnComplete { files: 0, added: 0, removed: 0, tokens: 0, cost: 0.0 });
                    return;
                }
            };

            let mut norm = SseNormalizer::new(&model);
            let mut answer = String::new();
            let reader = BufReader::new(resp.into_reader());
            for line in reader.lines() {
                let Ok(line) = line else { break };
                let Some(data) = line.strip_prefix("data: ") else { continue };
                if data == "[DONE]" {
                    continue;
                }
                if let Ok(v) = serde_json::from_str::<Value>(data) {
                    for ev in norm.on_data(&v) {
                        if let UiEvent::StreamDelta(ref d) = ev {
                            answer.push_str(d);
                        }
                        send(ev);
                    }
                }
            }
            history
                .lock()
                .unwrap()
                .push(json!({"role": "assistant", "content": answer}));
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::UiEvent;

    /// Feed a captured Anthropic SSE stream through the normalizer and assert it
    /// produces the right event arc + priced cost — fully offline, no key/network.
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

        let mut norm = SseNormalizer::new("claude-sonnet-4-5");
        let mut events = Vec::new();
        for line in fixture {
            let v: Value = serde_json::from_str(line).unwrap();
            events.extend(norm.on_data(&v));
        }

        // Expected arc: StreamStart, two text deltas, StreamEnd, TurnComplete.
        assert!(matches!(events[0], UiEvent::StreamStart));
        let text: String = events
            .iter()
            .filter_map(|e| match e {
                UiEvent::StreamDelta(d) => Some(d.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(text, "Hello, world");
        assert!(matches!(events[events.len() - 2], UiEvent::StreamEnd));
        match events.last().unwrap() {
            UiEvent::TurnComplete { tokens, cost, .. } => {
                assert_eq!(*tokens, 52); // 40 in + 12 out
                // 40/1e6*3 + 12/1e6*15 = 0.00012 + 0.00018 = 0.0003
                assert!((cost - 0.0003).abs() < 1e-9, "cost was {cost}");
            }
            other => panic!("expected TurnComplete, got {other:?}"),
        }
    }
}
