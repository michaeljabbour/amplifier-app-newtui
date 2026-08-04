"""The voice-first ambient adapter -- deliberately the LAST thing built.

The design doc's ordering principle, quoted because it is the reason this file
is so small: *"voice is last, because voice is an adapter. Build it first and
it will grow the permission, confirmation and history logic that belongs
underneath it -- precisely the failure this document exists to prevent."*

So this module holds **no** policy, **no** permission state, **no** session
ownership and **no** history. It authenticates nothing itself (it is handed a
principal), stores nothing of its own, and every consequential step it takes
is a call into a contract that already existed:

===================================== =======================================
the adapter wants to ...              ... which reduces to
===================================== =======================================
echo an interpretation and park work  :class:`~.interpretation.Interpretation
                                      Desk` over B6 ``session.pause``
accept "yes"                          ``handoff.claim`` (+ B6 ``idem``)
accept "change the target to ..."     ``interpretation.amend`` (new id)
accept "no"                           ``interpretation.cancel`` + ``resume``
read mail / send a message            :class:`~.sources.GrantedSource`, which
                                      consults E2 at use and audits
report on the fleet                   :class:`~.discovery.SessionDiscovery`
answer from a phone                   :class:`~.reply.ReplyChannel`
===================================== =======================================

-- What is NOT built here, stated plainly ------------------------------------

**There is no speech capture and no speech synthesis.** Microphone access,
wake-word detection, ASR and TTS are device capabilities; none of them can be
built or verified in an offline test environment, and a fake one would prove
nothing. The adapter's boundary is therefore **already-transcribed text in,
speakable text out** (:class:`VoiceTurn`). That is the honest seam: a real
voice client owns the microphone and the speaker, and calls exactly these two
methods. Everything downstream of the transcript -- which is where all the
safety lives -- is built and tested.

-- The two safety rules the adapter itself enforces -------------------------

1. **Voice raises the bar with no exemption.** Every voice-initiated
   *consequential* request is echoed (:func:`classify_request`). There is no
   trusted-speaker bypass, because ASR is lossy and the channel is eyes-free,
   so the same channel that misheard the request would also confirm it.
2. **Irreversible actions cannot be confirmed by voice at all.** An ASR error
   on a destructive verb is confirmed by the channel that made the error; so a
   class-3 request is echoed by voice and must be confirmed on a **visual**
   surface. :meth:`AmbientVoiceAdapter.respond` refuses it and says where to
   go instead.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..session_control import SessionControl, attach_command, attach_ref
from .discovery import SessionDiscovery
from .interpretation import (
    EDITABLE_FIELDS,
    Interpretation,
    InterpretationDesk,
    InterpretationError,
    ReversibilityClass,
)
from .principal import PrincipalLike, actor_for

MAX_SPOKEN_CHARS = 400
"""A one-breath cap on anything read aloud. Not a display limit -- a listener
cannot scroll back, so an echo they stop attending to is worse than a short
one."""

MAX_TITLE_CHARS = 80
"""Matches the local notification ladder's own title cap, so an ambient
notification is never *wider* than what the desktop rung already allows."""

AMBIENT_PUSH_FIELDS: frozenset[str] = frozenset({"event_id", "reason", "session_title", "ref"})
"""**Allowlist**, never a denylist -- a denylist passes every field you forgot.

Policy (design doc §2): an off-machine notification carries a **pointer, not
content**. Only the attention reason, the session title and the attach ref
leave the machine. Never message bodies, file contents, diffs, credentials or
model output; content appears only after the user opens the session on an
authenticated surface. Treat push as an untrusted broadcast channel and design
the payload as if a stranger will read it -- because on a public ntfy topic,
one can.

Note this is deliberately **narrower** than
``ui.notifications.attention_push_payload``, which also carries a ``body``:
that payload is the local ladder's, sanitized for a tray the user owns. It is
also not importable here (layering: ``kernel/`` never imports ``ui/``), which
is the happy accident that forced the stricter policy to be written down.
"""

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

ResponseKind = Literal["confirm", "cancel", "amend", "unclear"]

_CONFIRM_WORDS = frozenset(
    {"yes", "yep", "yeah", "confirm", "confirmed", "do it", "go ahead", "ok"}
)
_CANCEL_WORDS = frozenset({"no", "nope", "cancel", "stop", "forget it", "never mind", "abort"})
_AMEND_RE = re.compile(
    r"^\s*(?:change|set|make)\s+(?:the\s+)?(?P<field>[a-z_ ]+?)\s+to\s+(?P<value>.+?)\s*$",
    re.IGNORECASE,
)


def sanitize_spoken(text: str, *, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Strip control characters and cap. Used on everything spoken or pushed.

    Control characters matter here for the same reason they matter to the OSC
    777 rung: text that leaves this process reaches terminals and trays that
    interpret escape sequences.
    """
    cleaned = _CONTROL_CHARS.sub(" ", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit].rstrip()


# -- consequence classification (design doc §2) -------------------------------


@dataclass(frozen=True)
class RequestFacts:
    """What the caller knows about a request, before any policy is applied.

    Plain facts on purpose: the *classification* is a pure function of these
    (:func:`classify_request`), so the rule table is testable without a
    session, a runtime, or a model.
    """

    writes_outside_transcript: bool = False
    externally_visible: bool = False
    irreversible: bool = False
    session_count: int = 1
    consumes_source_grant: bool = False


@dataclass(frozen=True)
class Consequence:
    consequential: bool
    reversibility: ReversibilityClass
    reasons: tuple[str, ...] = ()


def classify_request(facts: RequestFacts) -> Consequence:
    """The five rules, verbatim from the design doc, as one pure function.

    A request is consequential if it writes outside the transcript, is
    externally visible, is irreversible or expensive to reverse, spans
    sessions, or **consumes a source grant -- including reads**. That last one
    is the non-obvious rule and the one worth keeping: reading someone's mail
    is itself a privacy act, and it is the step where a misheard selector does
    quiet damage.

    Read-only inspection of the user's own sessions, status questions and
    ``history.replay`` are deliberately *not* consequential -- B6 already
    guarantees replay never touches the lease or the transcript.
    """
    reasons: list[str] = []
    if facts.writes_outside_transcript:
        reasons.append("writes outside the transcript")
    if facts.externally_visible:
        reasons.append("externally visible")
    if facts.irreversible:
        reasons.append("irreversible or expensive to reverse")
    if facts.session_count > 1:
        reasons.append("spans multiple sessions")
    if facts.consumes_source_grant:
        reasons.append("consumes a source grant")
    reversibility: ReversibilityClass = "reversible"
    if facts.externally_visible:
        reversibility = "externally_visible"
    if facts.irreversible:
        reversibility = "irreversible"
    return Consequence(bool(reasons), reversibility, tuple(reasons))


# -- the redacted ambient notification ---------------------------------------


def ambient_push_payload(
    *, event_id: str, reason: str, session_title: str, session_id: str, handoff_id: str = ""
) -> dict[str, str]:
    """A pointer-only push payload built from an allowlist.

    Constructed field-by-field rather than filtered from a larger dict: a
    filter can only remove fields someone remembered to name, while a literal
    cannot leak a field that was never written. See
    :data:`AMBIENT_PUSH_FIELDS`.
    """
    return {
        "event_id": sanitize_spoken(event_id, limit=MAX_TITLE_CHARS),
        "reason": sanitize_spoken(reason, limit=MAX_TITLE_CHARS),
        "session_title": sanitize_spoken(session_title, limit=MAX_TITLE_CHARS),
        "ref": attach_ref(session_id, handoff_id or None),
    }


# -- response parsing ---------------------------------------------------------


@dataclass(frozen=True)
class ParsedResponse:
    kind: ResponseKind
    field_name: str = ""
    value: str = ""


def parse_response(text: str) -> ParsedResponse:
    """Map a transcribed utterance onto the three legal responses.

    Deliberately a small, deterministic matcher rather than an intent model:
    the closed response vocabulary is `confirm` / `amend(field, value)` /
    `cancel`, and anything it cannot match with confidence returns
    ``unclear`` so the adapter re-asks. Guessing here would defeat the entire
    point of the confirmation echo.
    """
    cleaned = sanitize_spoken(text, limit=200).rstrip(".!")
    lowered = cleaned.lower()
    if not cleaned:
        return ParsedResponse("unclear")
    if lowered in _CONFIRM_WORDS:
        return ParsedResponse("confirm")
    if lowered in _CANCEL_WORDS:
        return ParsedResponse("cancel")
    # Matched against the ORIGINAL casing: the field name is normalized, but the
    # value is the user's, and lower-casing "Reply to Sam" would quietly edit it.
    match = _AMEND_RE.match(cleaned)
    if match:
        field_name = match.group("field").strip().lower().replace(" ", "_")
        if field_name in EDITABLE_FIELDS:
            return ParsedResponse("amend", field_name, match.group("value").strip())
    return ParsedResponse("unclear")


# -- the adapter --------------------------------------------------------------


@dataclass(frozen=True)
class VoiceTurn:
    """What the adapter hands back to be spoken, plus the state it refers to.

    ``speak`` is the whole user-facing output: a real voice client renders it
    with a TTS engine this package does not own (module docstring).
    """

    speak: str
    interpretation: Interpretation | None = None
    awaiting: bool = False
    needs_visual_confirmation: bool = False
    records: tuple[dict[str, Any], ...] = field(default=())


class AmbientVoiceAdapter:
    """Thin voice adapter over the ambient contracts. Holds no state itself.

    Every field on this object is a *collaborator*, never a cache: the desk,
    the discovery projection and the principal. There is nothing here a second
    adapter (a phone, a chat bridge) would have to re-implement.
    """

    def __init__(
        self,
        desk: InterpretationDesk,
        principal: PrincipalLike,
        *,
        discovery: SessionDiscovery | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._desk = desk
        self._principal = principal
        self._discovery = discovery
        self._now = now

    # -- delegation spine (Brian's framing) --------------------------------

    def hear(
        self,
        utterance: str,
        *,
        summary: str,
        facts: RequestFacts,
        targets: Sequence[str] = (),
        grants: Sequence[str] = (),
        negative_scope: Sequence[str] = (),
    ) -> VoiceTurn:
        """Take a transcribed request; echo an interpretation if it matters.

        A non-consequential request (a status question, a read of the user's
        own sessions) is *not* echoed -- every extra confirmation step is a tax
        on the whole point of delegation, and the doc is explicit that
        read-only inspection does not earn one.
        """
        heard = sanitize_spoken(utterance)
        consequence = classify_request(facts)
        if not consequence.consequential:
            return VoiceTurn(
                sanitize_spoken(f"Working on it: {summary}."),
                None,
                awaiting=False,
            )
        outcome = self._desk.propose(
            self._principal,
            summary=summary,
            targets=tuple(targets),
            grants=tuple(grants),
            reversibility=consequence.reversibility,
            negative_scope=tuple(negative_scope) or ("anything not named above",),
            pause_reason=f"voice request: {heard[:120]}",
        )
        row = outcome.interpretation
        speak = sanitize_spoken(row.spoken())
        if row.reversibility == "irreversible":
            speak = sanitize_spoken(
                row.spoken() + " This one is irreversible, so I need you to confirm it on screen."
            )
        return VoiceTurn(speak, row, awaiting=True, records=outcome.records)

    def respond(self, interpretation_id: str, utterance: str) -> VoiceTurn:
        """Apply ``confirm`` / ``amend`` / ``cancel`` to a pending echo.

        Refuses a *voice* confirm on an irreversible request (rule 2 in the
        module docstring) -- and refuses it before touching the control plane,
        so the refusal cannot be a half-applied state.
        """
        parsed = parse_response(utterance)
        row = self._desk.get(interpretation_id)
        if row is None:
            return VoiceTurn("I couldn't find that request any more. Say it again?")
        if parsed.kind == "unclear":
            return VoiceTurn(
                sanitize_spoken(
                    "I didn't catch that. Say confirm, cancel, or change "
                    + ", ".join(EDITABLE_FIELDS)
                    + "."
                ),
                row,
                awaiting=True,
            )
        try:
            if parsed.kind == "cancel":
                outcome = self._desk.cancel(interpretation_id, self._principal, why="voice cancel")
                return VoiceTurn("Cancelled. Nothing was done.", outcome.interpretation)
            if parsed.kind == "amend":
                outcome = self._desk.amend(
                    interpretation_id, parsed.field_name, parsed.value, self._principal
                )
                return VoiceTurn(
                    sanitize_spoken(outcome.interpretation.spoken()),
                    outcome.interpretation,
                    awaiting=True,
                )
            if row.reversibility == "irreversible":
                return VoiceTurn(
                    sanitize_spoken(
                        "That one is irreversible, so I can't take a spoken confirmation. "
                        f"Open it with: {attach_command(row.session_id, row.handoff_id)}"
                    ),
                    row,
                    awaiting=True,
                    needs_visual_confirmation=True,
                )
            outcome = self._desk.confirm(interpretation_id, self._principal)
            if any(r.get("type") == "control.conflict" for r in outcome.records):
                return VoiceTurn(
                    "Someone else already picked that up, so I left it alone.",
                    outcome.interpretation,
                    records=outcome.records,
                )
            return VoiceTurn(
                "Confirmed. I have the session.", outcome.interpretation, records=outcome.records
            )
        except InterpretationError as error:
            return VoiceTurn(sanitize_spoken(f"I couldn't do that: {error}"), row)

    # -- visibility spine (MJ's framing) -----------------------------------

    def fleet_report(self, *, limit: int = 5) -> VoiceTurn:
        """Speak the state of the fleet: who needs you, who is running.

        Read-only and therefore never echoed for confirmation -- inspecting
        your own sessions is explicitly not consequential.
        """
        if self._discovery is None:
            return VoiceTurn("I can't see your other sessions from here.")
        rows = self._discovery.rows()
        if not rows:
            return VoiceTurn("Nothing is running.")
        waiting = [r for r in rows if r.state == "paused-awaiting-you" or r.needs_you]
        running = [r for r in rows if r.state == "running"]
        parts = [f"{len(rows)} sessions."]
        if waiting:
            parts.append(f"{len(waiting)} need you:")
            for row in waiting[:limit]:
                why = row.why_paused or row.needs_you or "waiting"
                parts.append(f"{row.session_id[:8]} in {row.project}, {why}.")
        if running:
            parts.append(f"{len(running)} still running.")
        if not waiting and not running:
            parts.append("All idle.")
        return VoiceTurn(sanitize_spoken(" ".join(parts)))


# -- AC2: sequencing follow-on actions across sessions ------------------------


@dataclass(frozen=True)
class PlanStep:
    session_dir: Path
    session_id: str
    text: str
    step_id: str


@dataclass(frozen=True)
class StepResult:
    step_id: str
    ok: bool
    reason: str = ""
    lease_id: str = ""


class FollowOnPlan:
    """An ordered, one-at-a-time fan-out across sessions (AC2).

    Every step is ``lease.acquire`` -> ``submit(idem=step_id)`` ->
    ``lease.release``: individually gated, attributed and idempotent by ops
    that already exist. The only new state is the queue itself.

    **On any ``control.conflict`` the plan stops.** It does not retry blindly
    and it does not route around the conflict -- B6's own guidance is to treat
    a conflict as authoritative, and a human who grabbed the pen mid-plan is a
    *signal*, not an obstacle.

    ``submit`` is injected because actually pushing text into a live session
    is a runtime concern, not a kernel one; the plan owns the sequencing, the
    gating and the stop rule, which is the part that must be right.
    """

    def __init__(
        self,
        steps: Sequence[PlanStep],
        principal: PrincipalLike,
        *,
        submit: Callable[[PlanStep, str], bool] | None = None,
        control_factory: Callable[[Path, str], SessionControl] | None = None,
    ) -> None:
        self.steps = tuple(steps)
        self._principal = principal
        self._submit = submit
        self._control_factory = control_factory or (lambda d, s: SessionControl(d, s))

    def run(self) -> list[StepResult]:
        actor = actor_for(self._principal)
        results: list[StepResult] = []
        for step in self.steps:
            control = self._control_factory(step.session_dir, step.session_id)
            records = control.acquire(actor)
            conflict = next((r for r in records if r.get("type") == "control.conflict"), None)
            if conflict is not None:
                results.append(
                    StepResult(step.step_id, False, str(conflict.get("reason", "conflict")))
                )
                break  # stop the plan -- never retry blindly
            lease_id = _lease_id(records)
            ok = True if self._submit is None else self._submit(step, lease_id)
            control.release(lease_id, actor=actor)
            results.append(StepResult(step.step_id, ok, "" if ok else "submit_failed", lease_id))
            if not ok:
                break
        return results


def _lease_id(records: Sequence[Mapping[str, Any]]) -> str:
    for record in records:
        lease = record.get("lease")
        if isinstance(lease, Mapping):
            return str(lease.get("lease_id", ""))
    return ""


__all__ = [
    "AMBIENT_PUSH_FIELDS",
    "MAX_SPOKEN_CHARS",
    "AmbientVoiceAdapter",
    "Consequence",
    "FollowOnPlan",
    "ParsedResponse",
    "PlanStep",
    "RequestFacts",
    "StepResult",
    "VoiceTurn",
    "ambient_push_payload",
    "classify_request",
    "parse_response",
    "sanitize_spoken",
]
