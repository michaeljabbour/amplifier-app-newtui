//! Evidence links: grounding final-answer claims in tool calls.
//!
//! DESIGN-SPEC §10: clicking a final answer prints an evidence block whose
//! numbered teal claims read `¹ "quote" → <tool call that grounds it>`.
//!
//! Port of `src/amplifier_app_newtui/model/evidence.py`.

use serde::{Deserialize, Serialize};

/// One claim-to-tool grounding pair.
///
/// `claim_quote` is the verbatim answer excerpt (rendered quoted, teal);
/// `tool_ref` is a human-readable reference to the grounding tool call
/// (e.g. `pytest run · 34 passed`). `tool_call_id` optionally keeps
/// the machine correlation key so evidence can deep-link to the ToolLine.
///
/// Mirrors a frozen pydantic model (`frozen=True, extra="forbid"`):
/// immutable by convention (construct, never mutate), hashable/equatable,
/// and deserialization rejects unknown fields.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvidenceLink {
    pub claim_quote: String,
    pub tool_ref: String,
    #[serde(default)]
    pub tool_call_id: String,
}

impl EvidenceLink {
    /// Construct with the pydantic default `tool_call_id = ""`.
    pub fn new(claim_quote: impl Into<String>, tool_ref: impl Into<String>) -> Self {
        Self {
            claim_quote: claim_quote.into(),
            tool_ref: tool_ref.into(),
            tool_call_id: String::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // evidence.py has no dedicated Python test file; these pin the model's
    // construction, defaults, frozen-model semantics (eq/hash), and the
    // `extra="forbid"` wire contract straight from the source.

    #[test]
    fn test_construction_holds_required_fields_verbatim() {
        let link = EvidenceLink::new("34 passed", "pytest run · 34 passed");
        assert_eq!(link.claim_quote, "34 passed");
        assert_eq!(link.tool_ref, "pytest run · 34 passed");
    }

    #[test]
    fn test_tool_call_id_defaults_to_empty_string() {
        let link = EvidenceLink::new("quote", "ref");
        assert_eq!(link.tool_call_id, "");
    }

    #[test]
    fn test_frozen_semantics_equality_and_hash() {
        // Frozen pydantic models compare by value and are hashable.
        let a = EvidenceLink::new("q", "r");
        let b = EvidenceLink::new("q", "r");
        assert_eq!(a, b);

        let mut with_id = EvidenceLink::new("q", "r");
        with_id.tool_call_id = "toolu_01".to_string();
        assert_ne!(a, with_id);

        let set: std::collections::HashSet<EvidenceLink> = [a, b].into_iter().collect();
        assert_eq!(set.len(), 1);
    }

    #[test]
    fn test_deserialize_applies_default_and_roundtrips() {
        let link: EvidenceLink =
            serde_json::from_str(r#"{"claim_quote": "q", "tool_ref": "r"}"#).unwrap();
        assert_eq!(link.tool_call_id, "");

        let json = serde_json::to_string(&link).unwrap();
        let back: EvidenceLink = serde_json::from_str(&json).unwrap();
        assert_eq!(back, link);
    }

    #[test]
    fn test_extra_fields_are_forbidden() {
        // Mirrors pydantic `extra="forbid"` raising ValidationError.
        let result: Result<EvidenceLink, _> =
            serde_json::from_str(r#"{"claim_quote": "q", "tool_ref": "r", "surprise": 1}"#);
        assert!(result.is_err());
    }

    #[test]
    fn test_missing_required_field_is_an_error() {
        // Mirrors pydantic ValidationError for a missing required field.
        let result: Result<EvidenceLink, _> = serde_json::from_str(r#"{"claim_quote": "q"}"#);
        assert!(result.is_err());
    }
}
