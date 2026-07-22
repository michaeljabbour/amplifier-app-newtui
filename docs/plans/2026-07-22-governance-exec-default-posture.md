# Design / Decision — Governance: default posture for unrecognized EXEC in auto mode

Issue: #26 — *Governance: offline classifier allows unrecognized EXEC by default — decide the posture (audit, corrected)*
Slug: `governance-exec-default-posture`
Labels: `audit-2026-07`, `security`
Grounding rev (READ-ONLY checkout `/Users/michaeljabbour/dev/newtui-wt/base`): `git rev-parse HEAD` = `ac854eff12f05a2df528eb590b547068da799a60` — matches the issue's "verified against main @ ac854ef".
Donor parity repo (READ-ONLY): `/Users/michaeljabbour/dev/amplifier-app-cli`.

---

## Problem

The 2026-07-22 five-agent security audit flagged a *fail-open* in EXEC governance. Verification corrected the **mechanism** (there is no "nothing found" degradation string) but confirmed the **substance**: in `auto` mode, an EXEC action for which the offline classifier finds **no positive signal** — not destructive, not outside-project, not an unrequested outbound push — falls through to a catch-all **allow**. It runs silently, with no approval and no deferred decision.

The catch-all is only reachable in **auto** mode (the other four postures settle EXEC statically — see Evidence), so the decision is scoped to auto mode's offline fallback. Two things make it worth an explicit decision rather than a silent status quo:

1. **Divergence from the donor.** amplifier-app-cli's equivalent offline evaluator (`ReasoningBlindStageEvaluator`) does the *opposite*: an unmatched deliberative action **denies** ("outside-user-authorization"). newtui's `OfflineAutoClassifier` **allows**. This divergence is undocumented.
2. **The EXEC bucket is also the *unknown-tool* fail-safe.** `classify_tool` routes anything it cannot recognize to `CapabilityClass.EXEC` (trust.py:139). In auto mode EXEC is classifier-gated, so a genuinely *unrecognized capability* inherits the same catch-all allow as a recognized-but-benign `ls`. That is the sharpest edge of the fail-open: "unknown ⇒ EXEC ⇒ allowed."

The contract (issue Acceptance): **"Explicit, documented posture + tests either way."** The issue's "What to do" derives two concrete sub-criteria: (a) *decide* allow vs. ask for no-positive-signal EXEC under the gated postures; (b) if allow stays, document it in ADR-0005/ARCHITECTURE; if not, route unmatched EXEC to ask/defer in the gated postures **with tests**.

---

## Evidence (verified file:line, rev `ac854ef`)

**The catch-all allow (the thing to decide):**
- `src/amplifier_app_newtui/kernel/governance_hook.py:127-143` — `OfflineAutoClassifier.classify`: denies destructive shapes (135-136), allows explicit-request matches (137-138), denies OUTSIDE_PROJECT (139-140), denies unrequested `git push` (141-142), then **`return (True, "within amplifier's wide trust scope")`** at **line 143** — allow-by-default for everything else.
- `src/amplifier_app_newtui/kernel/governance_hook.py:383-405` — `_action_text`: when none of `command/cmd/instruction/query` are present it falls back to `path/file_path/directory` (395-403), else returns the bare `tool_name` (404). Always a non-empty action — so "no action text" is never the trigger; the permissiveness is purely the classifier catch-all, exactly as the corrected issue states.

**The fail-safe that funnels the unknown into EXEC:**
- `src/amplifier_app_newtui/model/trust.py:117-147` — `classify_tool`: explicit table → test-command sniff → name-substring heuristic → **`capability = CapabilityClass.EXEC`** (line 139) as the terminal default for unknown tools.
- `src/amplifier_app_newtui/model/trust.py:198-210` — `resolve_capability("auto", …)`: READ/WRITE/TEST static-allow (199-204); everything else returns `ask` + **`classifier_gated=True`** (205-210). So EXEC/NET/SPEND in auto are handed to the classifier.

**The gate is fail-closed on crash (so only the *allow* deserves a decision):**
- `src/amplifier_app_newtui/kernel/governance_hook.py:303-330` — `_classify`: classifier exception → `allowed=False` ("classifier failed closed", 311-312); a deny becomes **deny-and-continue plus a deferred needs-you decision** (316-330). The crash path is safe; the catch-all allow is the gap.

**Other postures already settle EXEC statically (why this is auto-only):**
- `src/amplifier_app_newtui/model/trust.py:154-166` — `_MODE_POLICY`: `plan`/`brainstorm` deny EXEC (`_ALL_DENY`); `build` sets `EXEC: "ask"`; `chat` (default `.get(...,"ask")`) asks EXEC.

**Donor divergence (app-cli parity, READ-ONLY):**
- `/Users/michaeljabbour/dev/amplifier-app-cli/amplifier_app_cli/ui/authorization_stage.py:255-256` — `ReasoningBlindStageEvaluator` is the "deterministic fail-closed evaluator for sync callers and offline tests."
- `…/authorization_stage.py:325-335` — on the deliberative stage: explicit-authorization ⇒ ALLOW (326-330); **otherwise ⇒ DENY, `outside-user-authorization`, "action is not clearly within user authorization"** (331-335). This is the exact inverse of newtui's line 143.

**The standing product directive that chose the current behavior:**
- `docs/decisions/ADR-0007-newtui-ground-up-architecture.md:37-41` — Resolution 0 (amended 2026-07-16, user directive): auto boots wide; "net/spend/exec run through the classifier, whose offline fallback is **wide (allow)** except destructive shapes and unrequested `git push`, which deny-and-continue into the needs-you queue."
- `docs/DESIGN-SPEC.md:90-95` — auto trust string `auto read,write · asks if risky`; "deny reserved for destructive shapes and unrequested outbound pushes."
- `docs/decisions/ADR-0005-interaction-modes-and-trust-postures.md:4-9,29-36` — trust is a typed posture; default boot posture amended to `auto`; the ADR does **not** currently document auto's offline-classifier catch-all.

**Where the posture is documented today (and where it is silent):**
- `docs/ARCHITECTURE.md:440-471` (§7.1–7.2) — describes CapabilityClass, "EXEC as the fail-safe default," classifier-gating and deny-and-continue, but **does not state the offline catch-all is allow**. Security readers of §7.1 cannot learn the fail-open from the docs.

**The existing test that pins the current allow (any flip must update it):**
- `tests/test_kernel_approval_governance.py:254-302` — `test_offline_classifier_wide_scope_verdict_table` asserts `ls -la` (unmatched, non-destructive) ⇒ allow, reason `within amplifier's wide trust scope` (262-270).

**Config-seam precedent to mirror (an already-shipped two-value posture knob):**
- `src/amplifier_app_newtui/kernel/directory_permissions.py:203-205,275-282` — `write_boundary: "open" | "guarded"`, default `open` (app-cli parity), `open` branch returns allow with "filesystem tool enforces writes," `guarded` blocks pre-flight.
- `docs/SETTINGS.md:56` — user-facing doc row for `permissions.write_boundary` (open default, parity rationale). This is the template for documenting a new posture knob.
- `src/amplifier_app_newtui/kernel/runtime.py:634-645` — `GovernanceHook` is constructed with **no `classifier=`** argument, so the production gate is the `OfflineAutoClassifier` default (governance_hook.py:216). newtui ships **no** provider-backed classifier — the offline catch-all *is* the shipped behavior, not a test-only fallback.

**Classifier signature does not currently see the posture:**
- `src/amplifier_app_newtui/kernel/governance_hook.py:65-72` and `127-134` — `classify(action, capability, target, user_messages)`; no `mode`/posture argument. Any posture-sensitive behavior must be threaded in.

---

## Options considered

### Option A — Document allow as intentional; leave behavior unchanged
Add ADR-0005 + ARCHITECTURE §7.1 language stating that in `auto` the offline classifier is allow-by-default for non-destructive, in-project, non-outbound EXEC/NET/SPEND, and add a test asserting the documented posture.
- **Pros:** Zero behavior change; honors ADR-0007 §res 0 and DESIGN-SPEC §4 verbatim; smallest diff; keeps auto's "wide scope" product promise; the existing wide-scope test (254-302) becomes the codified posture.
- **Cons:** Leaves the sharpest edge — *unknown tool ⇒ EXEC ⇒ allowed* — unmitigated. A security audit asked for a *safer* option to exist, not just prose. No parity path for safety-conscious operators. "Explicit posture + tests" is satisfied only in the weakest sense.

### Option B — Flip the default to deny/defer unmatched EXEC (full app-cli parity)
Change governance_hook.py:143 to deny-and-continue+defer, matching `ReasoningBlindStageEvaluator` (331-335).
- **Pros:** Closes the fail-open outright; identical to the donor's offline evaluator; strongest security posture.
- **Cons:** **Directly contradicts a standing 2026-07-16 user directive** recorded in ADR-0007 §res 0 and DESIGN-SPEC §4 (auto = wide scope; deny reserved for destructive + unrequested push). Flipping the default silently would make auto behave like build, dissolving the mode's reason to exist and breaking test 254-302 and likely `test_reasoning_blind_evidence_comes_from_prompt_submit` expectations. A doc-only worker should not overturn an explicit product directive.

### Option C (recommended) — Keep `allow` as the *documented default*, add an opt-in `gated` posture with tests for both branches
Introduce a posture knob mirroring `write_boundary`: `permissions.exec_posture: "open" | "gated"`, default `open` (= current allow, parity with ADR-0007 §res 0). `gated` makes no-positive-signal EXEC/NET/SPEND **deny-and-continue into the needs-you queue** (the async "ask") — app-cli parity, opt-in. Simultaneously **narrow the fail-open** so a genuinely *unrecognized capability* (the `classify_tool` EXEC fallback for unknown tools) defers even under `open`, because "unknown" is not "recognized-benign."
- **Pros:** Satisfies the Acceptance *literally* — the posture is **explicit** (a named setting), **documented** (ADR-0005 + ARCHITECTURE + SETTINGS), and **tested either way** (open-allow branch + gated-defer branch). Honors the standing wide-scope directive as the default while giving security-conscious operators a first-class, supported safer mode. Reuses a proven config seam (`write_boundary`). Closes the worst edge (unknown-tool allow) without overriding product intent.
- **Cons:** Larger than a doc-only change (new setting + threading posture into `_classify`); adds one configuration axis; the "narrow unknown-tool" refinement needs the hook to distinguish recognized-shell EXEC from unknown-tool EXEC (a small signal added at classify time).

---

## Decision / Recommendation

**Adopt Option C.** Keep `allow` as the **default, documented** posture for `auto` mode's offline classifier (honoring ADR-0007 §resolution 0 and DESIGN-SPEC §4 — auto *is* the wide-scope mode), but make that posture **explicit and configurable** and **close the unknown-tool edge**:

1. **Document the posture** (removes the "undocumented fail-open" finding): state plainly in ADR-0005 and ARCHITECTURE §7.1 that in `auto`, an EXEC/NET/SPEND action with no positive classifier signal is **allowed by default** as amplifier's wide trust scope, that this **intentionally diverges** from app-cli's deny-unmatched offline evaluator, and that deny remains reserved for destructive shapes, outside-project, and unrequested outbound pushes.
2. **Add `permissions.exec_posture: open | gated`** (default `open`), mirroring `write_boundary` (directory_permissions.py:203-205; SETTINGS.md:56). Under `gated`, no-positive-signal EXEC/NET/SPEND becomes **deny-and-continue + needs-you deferral** (the existing async ask path, governance_hook.py:316-330) — full app-cli parity, opt-in.
3. **Narrow the catch-all even under `open`:** a capability that reached EXEC *only* via the unknown-tool fallback (trust.py:139) is **not** "recognized-benign" and defers rather than silently running. Recognized benign shells (`ls`, `cat`, …) keep today's allow.

This is the honest middle: it does not overturn a standing user directive, it eliminates the undocumented divergence, it gives the security audience a supported safer mode, and it removes the truly indefensible case (unknown tool ⇒ silent exec). It satisfies "explicit, documented posture + tests either way" in full.

---

## Implementation plan (phased, concrete paths)

**Phase 0 — Documentation (satisfies Acceptance on its own; ship first).**
- `docs/decisions/ADR-0005-interaction-modes-and-trust-postures.md`: add an "Amendment: auto-mode offline EXEC posture" section stating the default-allow posture, the deliberate divergence from app-cli `ReasoningBlindStageEvaluator`, and the new `exec_posture` knob.
- `docs/ARCHITECTURE.md` §7.1 (around lines 440-449): add one paragraph: "In auto, the offline classifier is allow-by-default for non-destructive, in-project, non-outbound actions (`within amplifier's wide trust scope`, governance_hook.py:143). Unknown-tool EXEC and, under `permissions.exec_posture: gated`, all unmatched EXEC/NET/SPEND, deny-and-continue into needs-you."
- `docs/SETTINGS.md`: add a `permissions.exec_posture` row modeled on the `write_boundary` row (line 56).
- Keep `bundle.md` ↔ `data/bundles/newtui.md` byte-identical only if touched (this phase does not touch them).

**Phase 1 — Posture plumbing (`open` default = no behavior change).**
- `src/amplifier_app_newtui/kernel/directory_permissions.py`: add `exec_posture_setting(settings)` beside `write_boundary_setting` (39-47) resolving `permissions.exec_posture` → `"open" | "gated"`, default `"open"`.
- `src/amplifier_app_newtui/kernel/governance_hook.py`:
  - Add an `exec_posture: Callable[[], str] | str = "open"` param to `GovernanceHook.__init__` (196-221).
  - In `_classify` (303-330), pass the resolved posture into the classifier call.
  - Extend `AutoClassifier.classify` / `OfflineAutoClassifier.classify` signature (65-72, 127-134) with `posture: str` and an `unknown_capability: bool` flag (or a distinct capability marker) so the catch-all at line 143 becomes: if `posture == "gated"` **or** `unknown_capability` → `return (False, "no positive signal · deferring under exec posture")`; else keep `(True, "within amplifier's wide trust scope")`.
  - Thread `unknown_capability` from `classify_tool`: add an out-of-band signal (e.g. `classify_tool` returns whether it hit the terminal EXEC fallback at trust.py:139) surfaced through `resolve`/`TrustDecision` (a new optional `fallback: bool` field on `TrustDecision`, trust.py:47-65) and read in `_govern_tool` (262-296).
- `src/amplifier_app_newtui/kernel/runtime.py:634-645`: pass `exec_posture=exec_posture_setting(resolved.settings)` into `GovernanceHook(...)`, mirroring the `write_boundary` wiring at runtime.py:588.

**Phase 2 — `gated` behavior + narrowed `open`.**
- Implement the branch logic above so: `open` + recognized-benign ⇒ allow (unchanged); `open` + unknown-tool EXEC ⇒ defer; `gated` + any unmatched EXEC/NET/SPEND ⇒ defer. Destructive / outside-project / unrequested-push denials remain first (governance_hook.py:135-142), unchanged.

---

## Test & validation strategy

Add to `tests/test_kernel_approval_governance.py` (async, offline, no network — same harness as 32-48):
1. **Documented default (open) — allow branch:** keep/extend `test_offline_classifier_wide_scope_verdict_table` (254-302); assert `ls -la`, unmatched, `open` posture ⇒ allow, reason `within amplifier's wide trust scope`. Codifies the documented posture (Acceptance "explicit/documented + test").
2. **Gated posture — defer branch (app-cli parity):** unmatched benign EXEC (`ls -la`) under `exec_posture="gated"` ⇒ classifier deny ⇒ `_classify` returns `action=="deny"`, `needs_you.pending_count == 1`, `denial_log.total_count == 1` (parity with 153-165).
3. **Unknown-tool edge under open:** a tool name absent from `_TOOL_CAPABILITIES` with no hint (classify_tool ⇒ EXEC fallback, trust.py:139), no matching prompt evidence, `open` posture ⇒ defer (not silent allow). Guards the "unknown ⇒ EXEC ⇒ allowed" hole.
4. **No regression to the safe invariants:** destructive shapes still deny in both postures (reuse 282-291); explicit-request match still allows in both (293-302); crash still fails closed (169-177).
5. **Wiring test:** `exec_posture_setting` resolves `open`/`gated`/missing→`open` (new unit test beside directory-policy tests).
- **Full gates (run locally, per AGENTS.md):** `uv run ruff check .`, `uv run pyright src/`, `uv run pytest -q`. If any transcript golden is touched (it should not be), regenerate per AGENTS.md.

---

## Risks & mitigations

- **Overriding a standing user directive.** *Mitigation:* default stays `open` (allow) = ADR-0007 §res 0 / DESIGN-SPEC §4; only an explicit opt-in changes behavior. The directive is honored, not overturned.
- **Threading `unknown_capability`/posture widens signatures across layers.** *Mitigation:* additive optional params with safe defaults (`posture="open"`, `fallback=False`); `TrustDecision` is frozen pydantic — add a field with a default so existing constructors keep working (trust.py:47-65).
- **`kernel` must not import Textual / layering (ADR-0007:15-18).** *Mitigation:* all changes live in `kernel/` + `model/`; the posture is a plain string resolved from settings, no UI import.
- **Existing wide-scope test breaks.** *Mitigation:* it is intentionally kept as the *open*-posture assertion; the gated behavior is a new, separate test — both branches covered ("either way").
- **Scope creep into NET/SPEND.** *Mitigation:* apply the same catch-all rule uniformly to EXEC/NET/SPEND (they share the classifier-gated path, trust.py:205-210); document that scope explicitly so it is a decision, not an accident.
- **This doc is the only deliverable; no code is changed here.** *Mitigation:* plan is phased so Phase 0 (docs) alone already satisfies Acceptance if the team prefers the minimal path; Phases 1–2 are the recommended, safer completion.

---

## Acceptance mapping

The issue's Acceptance is one line — **"Explicit, documented posture + tests either way."** Broken into the concrete obligations the body ("What to do") sets:

| Acceptance obligation (from body/Acceptance) | How this plan satisfies it |
|---|---|
| **Decide** allow vs. ask for no-positive-signal EXEC under the gated postures | Decision section: default **allow** in `auto` (honoring ADR-0007 §res 0 / DESIGN-SPEC §4), **plus** an opt-in `gated` posture that routes unmatched EXEC to defer/ask, **plus** unknown-tool EXEC deferred even under `open`. An explicit, reasoned decision — not a punt. |
| **Explicit** posture | New named setting `permissions.exec_posture: open \| gated` (Phase 1), resolved via `exec_posture_setting`, wired at runtime.py:634-645 — the posture becomes a first-class, inspectable configuration value, not an implicit code path. |
| **Documented** — "if allow stays, document it in ADR-0005/ARCHITECTURE" | Phase 0 edits ADR-0005 (new amendment), ARCHITECTURE §7.1 (governance_hook.py:143 posture + divergence from app-cli `ReasoningBlindStageEvaluator`), and SETTINGS.md (new row). Removes the undocumented-fail-open finding. |
| "if not, **route unmatched EXEC to ask** in the gated postures **with tests**" | Phase 2 implements deny-and-continue + needs-you deferral (governance_hook.py:316-330) for unmatched EXEC/NET/SPEND under `gated`; test #2 asserts it. |
| **Tests either way** | Test #1 (open ⇒ allow, documented posture) **and** Test #2 (gated ⇒ defer) — both branches covered, plus #3 unknown-tool edge, #4 safety-invariant regressions, #5 settings wiring. |
| Mechanism accuracy (issue's correction: no "nothing found" string; catch-all at :143 is the cause; crash path fails closed at :311-312) | Evidence section verifies each cite against `ac854ef`: `_action_text` always non-empty (383-405), catch-all allow at governance_hook.py:143, fail-closed on exception (311-312). The plan targets the real mechanism (the :143 catch-all), not the disproven string. |

**Self-review:** Re-read the issue. Background, Evidence (all three cites), "What to do" (allow-vs-ask decision; document-if-allow; route-and-test-if-not), and the one-line Acceptance are each mapped above. Every acceptance obligation is addressed. No obligation is left unmet.
