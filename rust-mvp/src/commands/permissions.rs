//! `/permissions` — the trust-slot listing/editing model surface.
//!
//! Port of `src/amplifier_app_newtui/commands/permissions.py`.
//!
//! Mockup description: *edit trust slots: boundary, blocks, exceptions*.
//! This module is the editable model behind that editor (DESIGN-SPEC §4/§6):
//!
//! - **slots** — one row per [`CapabilityClass`] showing the effective
//!   allow/ask/deny decision (mode default, or a user override);
//! - **boundary** — the project scope trust applies within
//!   (`within project` by default; widening it is the mockup's `add fork
//!   remote to boundary` move);
//! - **exceptions** — always-allow patterns (the allowlist that `Allow
//!   always` and `/improve` proposals feed);
//! - **blocks** — always-deny patterns that beat everything else.
//!
//! Resolution precedence for one tool call: blocks → exceptions → user slot
//! override → mode default ([`crate::model::trust::resolve`]). The UI edits
//! this surface; the kernel governance hook consults it on every `tool:pre`.
//! Nothing here imports the UI layer or amplifier-core.

use std::collections::HashMap;
use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::model::modes::DEFAULT_MODE;
use crate::model::trust::{
    classify_tool, resolve, resolve_capability, CapabilityClass, Decision, TrustDecision,
};

/// Display order of capability slots in the editor (safest first).
pub const SLOT_ORDER: [CapabilityClass; 7] = [
    CapabilityClass::Read,
    CapabilityClass::Test,
    CapabilityClass::Write,
    CapabilityClass::Net,
    CapabilityClass::Spend,
    CapabilityClass::Exec,
    CapabilityClass::OutsideProject,
];

pub const DEFAULT_BOUNDARY: &str = "within project";

/// Errors from surface edits (Python raises `ValueError`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PermissionsValueError(pub String);

impl fmt::Display for PermissionsValueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for PermissionsValueError {}

/// The mode's static decision for one capability slot.
pub fn mode_default(mode: &str, capability: CapabilityClass) -> TrustDecision {
    resolve_capability(mode, capability)
}

/// One editable capability slot as the editor lists it.
///
/// `overridden` is true when the user changed this slot away from the mode
/// default (the editor renders those distinctly and offers reset).
///
/// Frozen in Python (`frozen=True, extra="forbid"`): treated as immutable by
/// convention here; unknown fields are rejected on deserialization.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TrustSlot {
    pub capability: CapabilityClass,
    pub decision: Decision,
    pub default_decision: Decision,
    pub overridden: bool,
    #[serde(default)]
    pub classifier_gated: bool,
}

impl TrustSlot {
    /// Row text, e.g. `write · ask` or `net · deny (default ask)`.
    pub fn label(&self) -> String {
        let mut text = format!("{} · {}", self.capability.value(), self.decision.value());
        if self.overridden {
            text.push_str(&format!(" (default {})", self.default_decision.value()));
        }
        text
    }
}

/// Frozen view of the whole surface for rendering / persistence.
///
/// Frozen in Python; immutable by convention here.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PermissionsSnapshot {
    pub mode: String,
    pub boundary: String,
    pub slots: Vec<TrustSlot>,
    pub exceptions: Vec<String>,
    pub blocks: Vec<String>,
}

/// Python `_clean_pattern`: whitespace-collapse; empty is a `ValueError`.
fn clean_pattern(pattern: &str) -> Result<String, PermissionsValueError> {
    let clean = pattern.split_whitespace().collect::<Vec<_>>().join(" ");
    if clean.is_empty() {
        return Err(PermissionsValueError("pattern cannot be empty".to_string()));
    }
    Ok(clean)
}

/// One pattern matches a call by exact tool name or command prefix.
///
/// Command prefix matching is whole-token (`git push` matches
/// `git push origin` but not `git pushx`) — the 2-token-prefix scoping
/// ADR-0007 uses for "Allow always" on bash.
fn matches(pattern: &str, tool_name: &str, command: &str) -> bool {
    if pattern == tool_name {
        return true;
    }
    if !command.is_empty() {
        return command == pattern || command.starts_with(&format!("{pattern} "));
    }
    false
}

/// Python truthiness for a JSON value (mirrors `tool_input.get(...) or ...`).
fn json_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `str(value)` for the command lookup (only strings matter for matching).
fn json_to_command_string(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        other => other.to_string(),
    }
}

/// Mutable trust-slot editor state — one instance per session.
///
/// The UI mutates it through the methods below; the governance hook calls
/// [`PermissionSurface::resolve_call`] on every `tool:pre`. Edits here are
/// explicit user actions (`/improve` proposes; only this surface, driven by
/// the human, applies).
pub struct PermissionSurface {
    mode: String,
    boundary: String,
    overrides: HashMap<CapabilityClass, Decision>,
    exceptions: Vec<String>,
    blocks: Vec<String>,
}

impl PermissionSurface {
    /// Python default constructor: `PermissionSurface(mode=DEFAULT_MODE)`.
    pub fn new() -> Self {
        Self::with_mode(DEFAULT_MODE.as_str())
    }

    /// Python `PermissionSurface(mode=...)`.
    pub fn with_mode(mode: &str) -> Self {
        Self {
            mode: mode.to_string(),
            boundary: DEFAULT_BOUNDARY.to_string(),
            overrides: HashMap::new(),
            exceptions: Vec::new(),
            blocks: Vec::new(),
        }
    }

    // --- mode ----------------------------------------------------------

    pub fn mode(&self) -> &str {
        &self.mode
    }

    /// Track the active mode; user slot overrides survive mode changes
    /// (they are the user's word against any mode's default).
    pub fn set_mode(&mut self, mode: &str) {
        self.mode = mode.to_string();
    }

    // --- boundary ------------------------------------------------------

    pub fn boundary(&self) -> &str {
        &self.boundary
    }

    pub fn set_boundary(&mut self, boundary: &str) -> Result<(), PermissionsValueError> {
        self.boundary = clean_pattern(boundary)?;
        Ok(())
    }

    // --- slots ----------------------------------------------------------

    /// Copy of the user overrides (Python's `overrides` property).
    pub fn overrides(&self) -> HashMap<CapabilityClass, Decision> {
        self.overrides.clone()
    }

    /// Override one capability slot; setting the mode default clears it.
    pub fn set_slot(&mut self, capability: CapabilityClass, decision: Decision) {
        if mode_default(&self.mode, capability).decision == decision {
            self.overrides.remove(&capability);
        } else {
            self.overrides.insert(capability, decision);
        }
    }

    pub fn clear_slot(&mut self, capability: CapabilityClass) {
        self.overrides.remove(&capability);
    }

    /// Effective decision for a capability classified by the kernel.
    pub fn resolve_capability(&self, capability: CapabilityClass) -> TrustDecision {
        if let Some(&override_decision) = self.overrides.get(&capability) {
            return TrustDecision {
                decision: override_decision,
                capability,
                reason: format!(
                    "user trust slot · {} {}",
                    capability.value(),
                    override_decision.value()
                ),
                classifier_gated: false,
            };
        }
        mode_default(&self.mode, capability)
    }

    /// All capability slots with effective decisions, in display order.
    pub fn slots(&self) -> Vec<TrustSlot> {
        SLOT_ORDER
            .iter()
            .map(|&capability| {
                let default = mode_default(&self.mode, capability);
                let override_decision = self.overrides.get(&capability).copied();
                TrustSlot {
                    capability,
                    decision: override_decision.unwrap_or(default.decision),
                    default_decision: default.decision,
                    overridden: override_decision.is_some(),
                    classifier_gated: if override_decision.is_none() {
                        default.classifier_gated
                    } else {
                        false
                    },
                }
            })
            .collect()
    }

    // --- exceptions / blocks --------------------------------------------

    pub fn exceptions(&self) -> &[String] {
        &self.exceptions
    }

    pub fn blocks(&self) -> &[String] {
        &self.blocks
    }

    pub fn add_exception(&mut self, pattern: &str) -> Result<(), PermissionsValueError> {
        let clean = clean_pattern(pattern)?;
        if !self.exceptions.contains(&clean) {
            self.exceptions.push(clean);
        }
        Ok(())
    }

    pub fn remove_exception(&mut self, pattern: &str) -> Result<(), PermissionsValueError> {
        let clean = clean_pattern(pattern)?;
        match self.exceptions.iter().position(|p| p == &clean) {
            Some(index) => {
                self.exceptions.remove(index);
                Ok(())
            }
            // Python `list.remove` raises ValueError with this message.
            None => Err(PermissionsValueError(
                "list.remove(x): x not in list".to_string(),
            )),
        }
    }

    pub fn add_block(&mut self, pattern: &str) -> Result<(), PermissionsValueError> {
        let clean = clean_pattern(pattern)?;
        if !self.blocks.contains(&clean) {
            self.blocks.push(clean);
        }
        Ok(())
    }

    pub fn remove_block(&mut self, pattern: &str) -> Result<(), PermissionsValueError> {
        let clean = clean_pattern(pattern)?;
        match self.blocks.iter().position(|p| p == &clean) {
            Some(index) => {
                self.blocks.remove(index);
                Ok(())
            }
            None => Err(PermissionsValueError(
                "list.remove(x): x not in list".to_string(),
            )),
        }
    }

    // --- resolution -----------------------------------------------------

    /// Effective decision for one tool call.
    ///
    /// Precedence: blocks → exceptions → slot override → mode default.
    /// Blocks beat exceptions: an always-deny the user wrote wins over an
    /// old allowlist entry (fail-closed on conflict).
    pub fn resolve_call(
        &self,
        tool_name: &str,
        tool_input: Option<&Map<String, Value>>,
    ) -> TrustDecision {
        let mut command = String::new();
        // Python: `if tool_input:` — an empty mapping is falsy and skipped.
        if let Some(input) = tool_input.filter(|map| !map.is_empty()) {
            let raw = input
                .get("command")
                .filter(|v| json_truthy(v))
                .or_else(|| input.get("cmd"))
                .map(json_to_command_string)
                .unwrap_or_default();
            command = raw.trim().to_string();
        }
        let capability = classify_tool(tool_name, tool_input);
        for pattern in &self.blocks {
            if matches(pattern, tool_name, &command) {
                return TrustDecision {
                    decision: Decision::Deny,
                    capability,
                    reason: format!("blocked by permissions blocklist · {pattern}"),
                    classifier_gated: false,
                };
            }
        }
        for pattern in &self.exceptions {
            if matches(pattern, tool_name, &command) {
                return TrustDecision {
                    decision: Decision::Allow,
                    capability,
                    reason: format!("allowlisted · {pattern}"),
                    classifier_gated: false,
                };
            }
        }
        if let Some(&override_decision) = self.overrides.get(&capability) {
            return TrustDecision {
                decision: override_decision,
                capability,
                reason: format!(
                    "user trust slot · {} {}",
                    capability.value(),
                    override_decision.value()
                ),
                classifier_gated: false,
            };
        }
        resolve(&self.mode, tool_name, tool_input)
    }

    // --- snapshot ---------------------------------------------------------

    pub fn snapshot(&self) -> PermissionsSnapshot {
        PermissionsSnapshot {
            mode: self.mode.clone(),
            boundary: self.boundary.clone(),
            slots: self.slots(),
            exceptions: self.exceptions.clone(),
            blocks: self.blocks.clone(),
        }
    }
}

impl Default for PermissionSurface {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn input(value: Value) -> Map<String, Value> {
        value.as_object().expect("test input is an object").clone()
    }

    fn slot_for(surface: &PermissionSurface, capability: CapabilityClass) -> TrustSlot {
        surface
            .slots()
            .into_iter()
            .find(|slot| slot.capability == capability)
            .expect("slot present for capability")
    }

    #[test]
    fn test_mode_defaults_match_spec_table() {
        // build: auto read,test · ask write,net,spend (DESIGN-SPEC §4)
        assert_eq!(
            mode_default("build", CapabilityClass::Read).decision,
            Decision::Allow
        );
        assert_eq!(
            mode_default("build", CapabilityClass::Test).decision,
            Decision::Allow
        );
        assert_eq!(
            mode_default("build", CapabilityClass::Write).decision,
            Decision::Ask
        );
        assert_eq!(
            mode_default("build", CapabilityClass::Net).decision,
            Decision::Ask
        );
        assert_eq!(
            mode_default("build", CapabilityClass::Spend).decision,
            Decision::Ask
        );
        // plan: read-only
        assert_eq!(
            mode_default("plan", CapabilityClass::Read).decision,
            Decision::Allow
        );
        assert_eq!(
            mode_default("plan", CapabilityClass::Write).decision,
            Decision::Deny
        );
        // brainstorm: no tools
        assert_eq!(
            mode_default("brainstorm", CapabilityClass::Read).decision,
            Decision::Deny
        );
        // auto: auto read,write · asks if risky elsewhere
        assert_eq!(
            mode_default("auto", CapabilityClass::Write).decision,
            Decision::Allow
        );
        assert!(mode_default("auto", CapabilityClass::Net).classifier_gated);
        assert!(mode_default("auto", CapabilityClass::OutsideProject).classifier_gated);
        assert_eq!(
            mode_default("build", CapabilityClass::OutsideProject).decision,
            Decision::Ask
        );
    }

    #[test]
    fn test_slots_listing_order_and_defaults() {
        let surface = PermissionSurface::with_mode("build");
        let slots = surface.slots();
        let order: Vec<CapabilityClass> = slots.iter().map(|slot| slot.capability).collect();
        assert_eq!(order, SLOT_ORDER.to_vec());
        assert_eq!(
            slot_for(&surface, CapabilityClass::Read).decision,
            Decision::Allow
        );
        assert_eq!(
            slot_for(&surface, CapabilityClass::Write).decision,
            Decision::Ask
        );
        assert!(!slots.iter().any(|slot| slot.overridden));
    }

    #[test]
    fn test_override_set_clear_and_labels() {
        let mut surface = PermissionSurface::with_mode("build");
        surface.set_slot(CapabilityClass::Net, Decision::Deny);
        let slot = slot_for(&surface, CapabilityClass::Net);
        assert!(slot.overridden);
        assert_eq!(slot.decision, Decision::Deny);
        assert_eq!(slot.default_decision, Decision::Ask);
        assert_eq!(slot.label(), "net · deny (default ask)");
        surface.clear_slot(CapabilityClass::Net);
        assert!(!slot_for(&surface, CapabilityClass::Net).overridden);
    }

    #[test]
    fn test_setting_slot_to_mode_default_clears_override() {
        let mut surface = PermissionSurface::with_mode("build");
        surface.set_slot(CapabilityClass::Write, Decision::Ask); // already the default
        assert!(surface.overrides().is_empty());
    }

    #[test]
    fn test_resolution_precedence_blocks_beat_exceptions_beat_overrides() {
        let mut surface = PermissionSurface::with_mode("build");
        surface.set_slot(CapabilityClass::Exec, Decision::Allow);
        surface.add_exception("git push").expect("valid pattern");
        surface.add_block("git push").expect("valid pattern");
        let call_input = input(json!({"command": "git push origin main"}));
        let decision = surface.resolve_call("bash", Some(&call_input));
        assert_eq!(decision.decision, Decision::Deny);
        assert!(decision.reason.contains("blocklist"));

        surface.remove_block("git push").expect("block present");
        let decision = surface.resolve_call("bash", Some(&call_input));
        assert_eq!(decision.decision, Decision::Allow);
        assert!(decision.reason.contains("allowlisted"));

        surface.remove_exception("git push").expect("exception present");
        let decision = surface.resolve_call("bash", Some(&call_input));
        assert_eq!(decision.decision, Decision::Allow);
        assert!(decision.reason.contains("user trust slot"));

        surface.clear_slot(CapabilityClass::Exec);
        let decision = surface.resolve_call("bash", Some(&call_input));
        assert_eq!(decision.decision, Decision::Ask); // build mode default for exec
    }

    #[test]
    fn test_command_prefix_matching_is_whole_token() {
        let mut surface = PermissionSurface::with_mode("build");
        surface.add_exception("git push").expect("valid pattern");
        assert_eq!(
            surface
                .resolve_call("bash", Some(&input(json!({"command": "git push origin"}))))
                .decision,
            Decision::Allow
        );
        assert_eq!(
            surface
                .resolve_call("bash", Some(&input(json!({"command": "git pushx origin"}))))
                .decision,
            Decision::Ask
        );
    }

    #[test]
    fn test_exception_matches_tool_name_exactly() {
        let mut surface = PermissionSurface::with_mode("chat");
        surface.add_exception("web_fetch").expect("valid pattern");
        assert_eq!(
            surface
                .resolve_call("web_fetch", Some(&input(json!({"url": "https://x"}))))
                .decision,
            Decision::Allow
        );
        assert_eq!(
            surface
                .resolve_call("web_search", Some(&input(json!({}))))
                .decision,
            Decision::Ask
        );
    }

    #[test]
    fn test_mode_change_keeps_user_overrides() {
        let mut surface = PermissionSurface::with_mode("build");
        surface.set_slot(CapabilityClass::Net, Decision::Deny);
        surface.set_mode("auto");
        assert_eq!(surface.mode(), "auto");
        assert_eq!(
            surface
                .resolve_call("web_fetch", Some(&input(json!({}))))
                .decision,
            Decision::Deny
        );
    }

    #[test]
    fn test_boundary_editing() {
        let mut surface = PermissionSurface::new();
        assert_eq!(surface.boundary(), DEFAULT_BOUNDARY);
        surface
            .set_boundary("within project + fork remote")
            .expect("valid boundary");
        assert_eq!(surface.boundary(), "within project + fork remote");
        assert_eq!(
            surface.set_boundary("   "),
            Err(PermissionsValueError("pattern cannot be empty".to_string()))
        );
    }

    #[test]
    fn test_snapshot_is_frozen_and_complete() {
        // Python's `frozen=True` mutation check (`snap.mode = "chat"` raises)
        // has no runtime counterpart: the snapshot is immutable by convention
        // (an owned value; mutation requires `mut`, enforced at compile time).
        let mut surface = PermissionSurface::with_mode("plan");
        surface.add_exception("uv run pytest").expect("valid pattern");
        surface.add_block("rm -rf").expect("valid pattern");
        let snap = surface.snapshot();
        assert_eq!(snap.mode, "plan");
        assert_eq!(snap.boundary, DEFAULT_BOUNDARY);
        assert_eq!(snap.exceptions, vec!["uv run pytest".to_string()]);
        assert_eq!(snap.blocks, vec!["rm -rf".to_string()]);
        assert_eq!(snap.slots.len(), SLOT_ORDER.len());
    }

    #[test]
    fn test_duplicate_patterns_not_added_twice() {
        let mut surface = PermissionSurface::new();
        surface.add_exception("git push").expect("valid pattern");
        surface.add_exception("git  push").expect("valid pattern");
        assert_eq!(surface.exceptions(), &["git push".to_string()]);
    }
}
