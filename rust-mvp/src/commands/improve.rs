//! `/improve` — configuration proposals from the ledger + denial log.
//!
//! Mines two evidence streams (DESIGN-SPEC §6):
//!
//! - **Allowlist candidates** from repeated identical approvals: an action
//!   approved every single time it was asked (`N/N`) is a candidate for
//!   the auto allowlist — mockup: `allowlist: uv run pytest approved 22/22
//!   times · add to auto`.
//! - **Trust-slot suggestions** from overridden denials: an action denied by
//!   policy but overridden by the human every time is a candidate for a
//!   wider trust boundary — mockup: `trust slot: 3 denials on push-to-fork
//!   all overridden · add fork remote to boundary`.
//!
//! Everything here is pure data-in/data-out. `/improve` **proposes and
//! never applies silently**: the output is an
//! [`ImproveBlock`](crate::model::blocks::ImproveBlock) of
//! [`ImproveProposal`](crate::model::blocks::ImproveProposal) rows; acting
//! on one is a separate, explicit user step handled elsewhere.

use std::cmp::Reverse;
use std::collections::{BTreeMap, HashMap};
use std::fmt;

use serde::{Deserialize, Serialize};

use crate::model::blocks::{ImproveBlock, ImproveProposal};
use crate::model::trust::DenialLog;
use crate::model::turn::OutcomeLedger;

/// An action must be approved at least this many times (all N/N) to be
/// proposed for the allowlist.
pub const MIN_ALLOWLIST_APPROVALS: u64 = 3;

/// An action's denials must have been overridden at least this many times
/// (and every time) to earn a trust-slot suggestion.
pub const MIN_OVERRIDDEN_DENIALS: u64 = 2;

/// Errors from [`ApprovalJournal`] recording (Python raises `ValueError`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ImproveValueError(pub String);

impl fmt::Display for ImproveValueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ImproveValueError {}

/// Approval history for one identical action.
///
/// - `action`: the normalized action text (e.g. `uv run pytest`).
/// - `approved` / `asked`: approvals granted vs. approval prompts
///   shown. `approved == asked` means the human said yes every time.
/// - `capability`: capability-class name (`read`, `exec`, …) —
///   lets /doctor single out repeated *read-only* approvals.
///
/// Frozen in Python (pydantic `ge=0` on the counts is carried by the
/// unsigned field types); treated as immutable by convention here.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalTally {
    pub action: String,
    #[serde(default)]
    pub approved: u64,
    #[serde(default)]
    pub asked: u64,
    #[serde(default)]
    pub capability: String,
}

impl ApprovalTally {
    pub fn always_approved(&self) -> bool {
        self.asked > 0 && self.approved == self.asked
    }
}

/// Denials of one action that the human later overrode.
///
/// `overridden == denied` means every denial of this action was
/// reversed by the human — the policy is fighting the user.
///
/// Python constrains `denied >= 1` via pydantic; here the unsigned type
/// only enforces `>= 0`, and [`ApprovalJournal::overrides`] never emits a
/// row below 1.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OverriddenDenial {
    pub action: String,
    pub denied: u64,
    #[serde(default)]
    pub overridden: u64,
}

impl OverriddenDenial {
    pub fn all_overridden(&self) -> bool {
        self.overridden >= self.denied
    }
}

fn normalize_action(action: &str) -> String {
    action.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Session-scope recorder feeding /improve and /doctor.
///
/// The approval broker calls [`ApprovalJournal::record_ask`] on every
/// approval prompt; the governance hook calls
/// [`ApprovalJournal::record_override`] whenever a policy denial is later
/// reversed by the human (retro-answered needs-you decision or immediate
/// re-allow).
#[derive(Default)]
pub struct ApprovalJournal {
    /// First-ask order of actions (Python `Counter` preserves insertion
    /// order when iterated).
    ask_order: Vec<String>,
    asked: HashMap<String, u64>,
    approved: HashMap<String, u64>,
    capability: HashMap<String, String>,
    overridden: BTreeMap<String, u64>,
}

impl ApprovalJournal {
    pub fn new() -> Self {
        Self::default()
    }

    /// Errors (Python `ValueError`) when the action is empty/whitespace.
    pub fn record_ask(
        &mut self,
        action: &str,
        approved: bool,
        capability: &str,
    ) -> Result<(), ImproveValueError> {
        let clean = normalize_action(action);
        if clean.is_empty() {
            return Err(ImproveValueError("approval action is required".to_string()));
        }
        if !self.asked.contains_key(&clean) {
            self.ask_order.push(clean.clone());
        }
        *self.asked.entry(clean.clone()).or_insert(0) += 1;
        if approved {
            *self.approved.entry(clean.clone()).or_insert(0) += 1;
        }
        if !capability.is_empty() {
            self.capability.insert(clean, capability.to_string());
        }
        Ok(())
    }

    /// Errors (Python `ValueError`) when the action is empty/whitespace.
    pub fn record_override(&mut self, action: &str) -> Result<(), ImproveValueError> {
        let clean = normalize_action(action);
        if clean.is_empty() {
            return Err(ImproveValueError("override action is required".to_string()));
        }
        *self.overridden.entry(clean).or_insert(0) += 1;
        Ok(())
    }

    pub fn tallies(&self) -> Vec<ApprovalTally> {
        self.ask_order
            .iter()
            .map(|action| ApprovalTally {
                action: action.clone(),
                approved: self.approved.get(action).copied().unwrap_or(0),
                asked: self.asked.get(action).copied().unwrap_or(0),
                capability: self.capability.get(action).cloned().unwrap_or_default(),
            })
            .collect()
    }

    /// Overridden-denial rows, denial counts taken from *denial_log*
    /// when provided (the log is the authority on how often policy said
    /// no); actions with zero overrides are omitted.
    pub fn overrides(&self, denial_log: Option<&DenialLog>) -> Vec<OverriddenDenial> {
        let mut denied_counts: HashMap<&str, u64> = HashMap::new();
        if let Some(log) = denial_log {
            for record in log.records() {
                *denied_counts.entry(record.action.as_str()).or_insert(0) += 1;
            }
        }
        // BTreeMap iteration matches Python's `sorted(self._overridden.items())`.
        self.overridden
            .iter()
            .map(|(action, &overridden)| OverriddenDenial {
                action: action.clone(),
                denied: denied_counts
                    .get(action.as_str())
                    .copied()
                    .unwrap_or(0)
                    .max(overridden),
                overridden,
            })
            .collect()
    }
}

/// `N/N` approval candidates: always approved, asked >= threshold.
///
/// Python default `min_approvals` is [`MIN_ALLOWLIST_APPROVALS`].
pub fn allowlist_proposals(
    tallies: &[ApprovalTally],
    min_approvals: u64,
) -> Vec<ImproveProposal> {
    let mut sorted: Vec<&ApprovalTally> = tallies.iter().collect();
    sorted.sort_by_key(|t| (Reverse(t.asked), t.action.clone()));
    sorted
        .into_iter()
        .filter(|tally| tally.always_approved() && tally.asked >= min_approvals)
        .map(|tally| ImproveProposal {
            title: "allowlist:".to_string(),
            rationale: format!(
                "approved {}/{} times · add to auto",
                tally.approved, tally.asked
            ),
            action: tally.action.clone(),
        })
        .collect()
}

/// Action-specific remedy tail for a trust-slot suggestion.
///
/// Overridden denials on pushing to a remote mean that remote belongs in
/// the trust boundary — mockup (design-v3-cohesive.html:514):
/// `push-to-fork` → `add fork remote to boundary`. Anything else
/// falls back to the generic `add to trust boundary`.
fn trust_remedy(action: &str) -> String {
    if let Some(target) = action.strip_prefix("push-to-") {
        if !target.is_empty() {
            return format!("add {target} remote to boundary");
        }
    }
    "add to trust boundary".to_string()
}

/// Trust-slot suggestions: every denial overridden, count >= threshold.
///
/// Python default `min_overridden` is [`MIN_OVERRIDDEN_DENIALS`].
pub fn trust_slot_proposals(
    overrides: &[OverriddenDenial],
    min_overridden: u64,
) -> Vec<ImproveProposal> {
    let mut sorted: Vec<&OverriddenDenial> = overrides.iter().collect();
    sorted.sort_by_key(|o| (Reverse(o.overridden), o.action.clone()));
    sorted
        .into_iter()
        .filter(|row| row.all_overridden() && row.overridden >= min_overridden)
        .map(|row| {
            ImproveProposal::new(
                "trust slot:",
                format!(
                    "{} denials on {} all overridden · {}",
                    row.denied,
                    row.action,
                    trust_remedy(&row.action)
                ),
            )
        })
        .collect()
}

/// All /improve proposals: allowlist candidates first, then trust slots.
///
/// *ledger* is accepted for future spend/yield-driven proposals; today
/// the two spec'd proposal kinds are approval- and denial-derived (we
/// never invent proposals the evidence does not support). Python's
/// keyword defaults are [`MIN_ALLOWLIST_APPROVALS`] and
/// [`MIN_OVERRIDDEN_DENIALS`].
pub fn improve_proposals(
    tallies: &[ApprovalTally],
    overrides: &[OverriddenDenial],
    ledger: Option<&OutcomeLedger>,
    min_approvals: u64,
    min_overridden: u64,
) -> Vec<ImproveProposal> {
    let _ = ledger; // reserved: spend-vs-yield proposals are not spec'd yet
    let mut proposals = allowlist_proposals(tallies, min_approvals);
    proposals.extend(trust_slot_proposals(overrides, min_overridden));
    proposals
}

/// Assemble the `/improve` transcript block (proposals only — the
/// header line `Improve  from ledger + denial log · proposes, never
/// applies silently` is the renderer's).
pub fn build_improve_block(block_id: &str, proposals: Vec<ImproveProposal>) -> ImproveBlock {
    ImproveBlock {
        id: block_id.to_string(),
        proposals,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::blocks::TranscriptBlock;
    use crate::model::trust::CapabilityClass;

    fn tally(action: &str, approved: u64, asked: u64) -> ApprovalTally {
        ApprovalTally {
            action: action.to_string(),
            approved,
            asked,
            capability: String::new(),
        }
    }

    fn overridden(action: &str, denied: u64, overridden: u64) -> OverriddenDenial {
        OverriddenDenial {
            action: action.to_string(),
            denied,
            overridden,
        }
    }

    #[test]
    fn test_allowlist_requires_every_ask_approved() {
        let tallies = [
            tally("uv run pytest", 22, 22),
            tally("git push", 9, 10), // one deny → out
            tally("rare thing", 2, 2), // below min → out
        ];
        let proposals = allowlist_proposals(&tallies, MIN_ALLOWLIST_APPROVALS);
        // Mockup row: dim 'allowlist: ' title + the action named once in green.
        assert_eq!(
            proposals
                .iter()
                .map(|p| (p.title.as_str(), p.action.as_str()))
                .collect::<Vec<_>>(),
            vec![("allowlist:", "uv run pytest")]
        );
        assert_eq!(proposals[0].rationale, "approved 22/22 times · add to auto");
    }

    #[test]
    fn test_allowlist_orders_by_ask_volume() {
        let tallies = [tally("a", 5, 5), tally("b", 9, 9)];
        let proposals = allowlist_proposals(&tallies, MIN_ALLOWLIST_APPROVALS);
        assert_eq!(
            proposals.iter().map(|p| p.action.as_str()).collect::<Vec<_>>(),
            vec!["b", "a"]
        );
    }

    #[test]
    fn test_trust_slot_requires_all_denials_overridden() {
        let overrides = [
            overridden("push-to-fork", 3, 3),
            overridden("net fetch", 4, 2), // not all → out
            overridden("once", 1, 1),      // below min → out
        ];
        let proposals = trust_slot_proposals(&overrides, MIN_OVERRIDDEN_DENIALS);
        // Trust-slot rows name the action once, inside the rationale.
        assert_eq!(
            proposals.iter().map(|p| p.title.as_str()).collect::<Vec<_>>(),
            vec!["trust slot:"]
        );
        assert_eq!(proposals[0].action, "");
        assert_eq!(
            proposals[0].rationale,
            "3 denials on push-to-fork all overridden · add fork remote to boundary"
        );
    }

    #[test]
    fn test_improve_proposals_combines_both_kinds_allowlist_first() {
        let proposals = improve_proposals(
            &[tally("uv run pytest", 3, 3)],
            &[overridden("push-to-fork", 2, 2)],
            None,
            MIN_ALLOWLIST_APPROVALS,
            MIN_OVERRIDDEN_DENIALS,
        );
        assert_eq!(
            proposals.iter().map(|p| p.title.as_str()).collect::<Vec<_>>(),
            vec!["allowlist:", "trust slot:"]
        );
        assert_eq!(
            proposals.iter().map(|p| p.action.as_str()).collect::<Vec<_>>(),
            vec!["uv run pytest", ""]
        );
    }

    #[test]
    fn test_no_evidence_no_proposals() {
        assert_eq!(
            improve_proposals(
                &[],
                &[],
                None,
                MIN_ALLOWLIST_APPROVALS,
                MIN_OVERRIDDEN_DENIALS
            ),
            Vec::new()
        );
    }

    #[test]
    fn test_build_improve_block() {
        let proposals = improve_proposals(
            &[tally("uv run pytest", 3, 3)],
            &[],
            None,
            MIN_ALLOWLIST_APPROVALS,
            MIN_OVERRIDDEN_DENIALS,
        );
        let block = build_improve_block("b9", proposals);
        assert_eq!(TranscriptBlock::from(block.clone()).kind(), "improve");
        assert_eq!(block.id, "b9");
        assert_eq!(block.proposals.len(), 1);
    }

    #[test]
    fn test_journal_tallies_and_capabilities() {
        let mut journal = ApprovalJournal::new();
        for _ in 0..3 {
            journal
                .record_ask("uv run pytest", true, "test")
                .expect("valid action");
        }
        journal
            .record_ask("git push", false, "net")
            .expect("valid action");
        let tallies: std::collections::HashMap<String, ApprovalTally> = journal
            .tallies()
            .into_iter()
            .map(|t| (t.action.clone(), t))
            .collect();
        assert_eq!(tallies["uv run pytest"].asked, 3);
        assert!(tallies["uv run pytest"].always_approved());
        assert_eq!(tallies["uv run pytest"].capability, "test");
        assert!(!tallies["git push"].always_approved());
    }

    #[test]
    fn test_journal_overrides_use_denial_log_counts() {
        let mut journal = ApprovalJournal::new();
        let mut log = DenialLog::new();
        for _ in 0..3 {
            log.record_denial(
                CapabilityClass::Net,
                "push-to-fork",
                "net has real downside",
            )
            .expect("valid denial");
            journal
                .record_override("push-to-fork")
                .expect("valid action");
        }
        let rows = journal.overrides(Some(&log));
        assert_eq!(rows, vec![overridden("push-to-fork", 3, 3)]);
        assert!(rows[0].all_overridden());
    }

    #[test]
    fn test_journal_rejects_empty_action() {
        let mut journal = ApprovalJournal::new();
        assert_eq!(
            journal.record_ask("   ", true, ""),
            Err(ImproveValueError("approval action is required".to_string()))
        );
        assert_eq!(
            journal.record_override(""),
            Err(ImproveValueError("override action is required".to_string()))
        );
    }

    #[test]
    fn test_journal_normalizes_whitespace() {
        let mut journal = ApprovalJournal::new();
        journal
            .record_ask("uv  run   pytest", true, "")
            .expect("valid action");
        journal
            .record_ask("uv run pytest", true, "")
            .expect("valid action");
        let tallies = journal.tallies();
        assert_eq!(tallies.len(), 1);
        assert_eq!(tallies[0].action, "uv run pytest");
        assert_eq!(tallies[0].asked, 2);
    }
}
