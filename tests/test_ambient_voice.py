"""The voice-first ambient adapter, and the E1 consumption seam.

The adapter is deliberately thin, so these tests are mostly about what it
*refuses* to do on its own: it holds no policy, it cannot confirm an
irreversible action, and the payload it lets off the machine is an allowlist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.ambient.discovery import SessionDiscovery
from amplifier_app_tui.kernel.ambient.interpretation import InterpretationDesk
from amplifier_app_tui.kernel.ambient.principal import (
    LocalPrincipal,
    actor_for,
    auth_provenance,
    session_authz_available,
)
from amplifier_app_tui.kernel.ambient.voice import (
    AMBIENT_PUSH_FIELDS,
    AmbientVoiceAdapter,
    FollowOnPlan,
    PlanStep,
    RequestFacts,
    ambient_push_payload,
    classify_request,
    parse_response,
    sanitize_spoken,
)
from amplifier_app_tui.kernel.session_control import (
    AUTOMATION,
    HUMAN,
    UNKNOWN,
    Actor,
    SessionControl,
)


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


MJ = LocalPrincipal("mj", kind=HUMAN, verified=True, method="device-token")


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def control(tmp_path: Path, clock: _Clock) -> SessionControl:
    return SessionControl(tmp_path / "s-1", "s-1", now=clock)


@pytest.fixture
def adapter(tmp_path: Path, control: SessionControl, clock: _Clock) -> AmbientVoiceAdapter:
    desk = InterpretationDesk(tmp_path / "s-1", control, now=clock, ttl=600.0)
    return AmbientVoiceAdapter(desk, MJ, now=clock)


# -- E1 consumption seam -------------------------------------------------------


def test_the_layer_degrades_cleanly_when_session_authz_is_absent() -> None:
    """E1 lands separately; this package must not require it to exist."""
    available = session_authz_available()
    assert isinstance(available, bool)  # importable either way, never an ImportError


def test_a_verified_human_maps_straight_through() -> None:
    assert actor_for(MJ) == Actor(id="mj", kind=HUMAN, display="")


def test_an_unverified_human_claim_over_a_channel_cannot_outrank_anyone() -> None:
    """B6's human>automation rule becomes a privilege boundary over a network."""
    spoofer = LocalPrincipal("mj", kind=HUMAN, verified=False, method="device-token")
    actor = actor_for(spoofer)
    assert actor.kind == UNKNOWN
    assert actor.precedence == 0


def test_an_unverified_human_on_the_local_pipe_is_still_human() -> None:
    """The OS established that peer -- downgrading it would break the TUI."""
    local = LocalPrincipal("mj", kind=HUMAN, verified=False)
    assert actor_for(local).kind == HUMAN


def test_a_downgrade_is_recorded_rather_than_silent() -> None:
    spoofer = LocalPrincipal("mj", kind=HUMAN, verified=False, method="device-token")
    provenance = auth_provenance(spoofer)
    assert provenance["downgraded"] is True
    assert provenance["claimed"] == HUMAN
    assert provenance["verified"] is False


def test_an_unverified_automation_is_left_alone() -> None:
    bot = LocalPrincipal("bot", kind=AUTOMATION, verified=False, method="device-token")
    assert actor_for(bot).kind == AUTOMATION


# -- consequence classification -------------------------------------------------


def test_a_read_only_question_is_not_consequential() -> None:
    assert not classify_request(RequestFacts()).consequential


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (RequestFacts(writes_outside_transcript=True), "writes outside the transcript"),
        (RequestFacts(externally_visible=True), "externally visible"),
        (RequestFacts(irreversible=True), "irreversible or expensive to reverse"),
        (RequestFacts(session_count=3), "spans multiple sessions"),
        (RequestFacts(consumes_source_grant=True), "consumes a source grant"),
    ],
)
def test_each_of_the_five_rules_makes_a_request_consequential(
    facts: RequestFacts, expected: str
) -> None:
    outcome = classify_request(facts)
    assert outcome.consequential
    assert expected in outcome.reasons


def test_reading_a_source_is_consequential_even_though_it_is_a_read() -> None:
    """The non-obvious rule: reading someone's mail is itself a privacy act."""
    assert classify_request(RequestFacts(consumes_source_grant=True)).consequential


def test_the_reversibility_class_escalates_with_the_worst_fact() -> None:
    assert classify_request(RequestFacts(externally_visible=True)).reversibility == (
        "externally_visible"
    )
    assert (
        classify_request(RequestFacts(externally_visible=True, irreversible=True)).reversibility
        == "irreversible"
    )


# -- the echo loop over the contracts ------------------------------------------


def test_a_harmless_request_is_not_echoed_and_does_not_park_the_session(
    adapter: AmbientVoiceAdapter, control: SessionControl
) -> None:
    """Every extra confirmation step is a tax on the whole point of delegation."""
    turn = adapter.hear("what's running", summary="report status", facts=RequestFacts())
    assert not turn.awaiting
    assert turn.interpretation is None
    assert not control.paused()


def test_a_consequential_request_is_echoed_and_parks_the_session(
    adapter: AmbientVoiceAdapter, control: SessionControl
) -> None:
    turn = adapter.hear(
        "reply to Dana confirming Thursday",
        summary="Reply to Dana's thread confirming Thursday",
        facts=RequestFacts(externally_visible=True, consumes_source_grant=True),
        grants=("your Outlook inbox, read only, Dana's thread",),
    )
    assert turn.awaiting
    assert turn.interpretation is not None
    assert control.paused()
    assert "Dana" in turn.speak


def test_confirming_by_voice_returns_the_lease_to_the_speaker(
    adapter: AmbientVoiceAdapter, control: SessionControl
) -> None:
    turn = adapter.hear(
        "reply to Dana",
        summary="Reply to Dana",
        facts=RequestFacts(externally_visible=True),
    )
    assert turn.interpretation is not None

    answer = adapter.respond(turn.interpretation.interpretation_id, "yes")

    assert "Confirmed" in answer.speak
    assert not control.paused()
    lease = control.active_lease()
    assert lease is not None and lease.actor.id == "mj"


def test_amending_by_voice_mints_a_new_echo(adapter: AmbientVoiceAdapter) -> None:
    turn = adapter.hear(
        "reply to Dana", summary="Reply to Dana", facts=RequestFacts(externally_visible=True)
    )
    assert turn.interpretation is not None

    answer = adapter.respond(
        turn.interpretation.interpretation_id, "change the summary to Reply to Sam"
    )

    assert answer.awaiting
    assert answer.interpretation is not None
    assert answer.interpretation.interpretation_id != turn.interpretation.interpretation_id
    assert "Reply to Sam" in answer.speak


def test_cancelling_by_voice_does_nothing_and_unparks(
    adapter: AmbientVoiceAdapter, control: SessionControl
) -> None:
    turn = adapter.hear("reply", summary="Reply", facts=RequestFacts(externally_visible=True))
    assert turn.interpretation is not None
    answer = adapter.respond(turn.interpretation.interpretation_id, "cancel")
    assert "Cancelled" in answer.speak
    assert not control.paused()


def test_an_irreversible_action_cannot_be_confirmed_by_voice(
    adapter: AmbientVoiceAdapter, control: SessionControl
) -> None:
    """The echo travels the same lossy channel that produced the request."""
    turn = adapter.hear(
        "delete the branch",
        summary="Force-delete the release branch",
        facts=RequestFacts(irreversible=True, writes_outside_transcript=True),
    )
    assert turn.interpretation is not None

    answer = adapter.respond(turn.interpretation.interpretation_id, "yes")

    assert answer.needs_visual_confirmation
    assert control.paused()  # nothing was granted
    assert "amplifier-tui serve --attach" in answer.speak


def test_an_unclear_response_re_asks_rather_than_guessing(
    adapter: AmbientVoiceAdapter, control: SessionControl
) -> None:
    turn = adapter.hear("reply", summary="Reply", facts=RequestFacts(externally_visible=True))
    assert turn.interpretation is not None

    answer = adapter.respond(turn.interpretation.interpretation_id, "uhh maybe later I guess")

    assert answer.awaiting
    assert control.paused()


@pytest.mark.parametrize("utterance", ["yes", "confirm", "go ahead", "OK"])
def test_confirm_words_parse(utterance: str) -> None:
    assert parse_response(utterance).kind == "confirm"


@pytest.mark.parametrize("utterance", ["no", "cancel", "stop", "never mind"])
def test_cancel_words_parse(utterance: str) -> None:
    assert parse_response(utterance).kind == "cancel"


def test_an_amend_names_a_field_from_the_closed_vocabulary() -> None:
    parsed = parse_response("change the negative scope to nothing else")
    assert parsed.kind == "amend"
    assert parsed.field_name == "negative_scope"


def test_an_amend_naming_an_unknown_field_is_unclear_not_a_guess() -> None:
    assert parse_response("change the password to hunter2").kind == "unclear"


# -- redaction (allowlist golden) ----------------------------------------------


def test_the_ambient_push_payload_carries_only_the_allowlist() -> None:
    """Allowlist, never a denylist -- a denylist passes every field you forgot."""
    payload = ambient_push_payload(
        event_id="s-1:awaiting_clarification:t1",
        reason="awaiting_clarification",
        session_title="refactor the parser",
        session_id="s-1",
        handoff_id="ho-9",
    )
    assert set(payload) == AMBIENT_PUSH_FIELDS
    assert payload["ref"] == "amplifier-session:s-1#ho-9"


def test_the_ambient_push_payload_carries_no_content(tmp_path: Path) -> None:
    """A pointer, not content: treat push as an untrusted broadcast channel."""
    payload = ambient_push_payload(event_id="e", reason="error", session_title="t", session_id="s")
    blob = json.dumps(payload)
    for forbidden in ("body", "detail", "message", "diff", "output", "token"):
        assert forbidden not in blob


def test_pushed_text_is_sanitized_and_capped() -> None:
    payload = ambient_push_payload(
        event_id="e",
        reason="error",
        session_title="\x1b]777;notify;x\x07" + "y" * 300,
        session_id="s",
    )
    assert "\x1b" not in payload["session_title"]
    assert len(payload["session_title"]) <= 80


def test_sanitize_spoken_collapses_control_characters() -> None:
    assert sanitize_spoken("a\x00b\nc") == "a b c"


# -- the fleet report (MJ's framing) -------------------------------------------


def test_the_fleet_report_speaks_what_needs_you(tmp_path: Path, clock: _Clock) -> None:
    projects = tmp_path / "projects"
    parked = projects / "proj" / "sessions" / "s-parked"
    parked.mkdir(parents=True)
    (parked / "control.json").write_text(json.dumps({"paused": True}), encoding="utf-8")
    (parked / "control-audit.jsonl").write_text(
        json.dumps({"action": "session.paused", "detail": {"why": "needs a decision"}}) + "\n",
        encoding="utf-8",
    )
    control = SessionControl(tmp_path / "s-1", "s-1", now=clock)
    desk = InterpretationDesk(tmp_path / "s-1", control, now=clock)
    adapter = AmbientVoiceAdapter(
        desk, MJ, discovery=SessionDiscovery(projects, now=clock), now=clock
    )

    turn = adapter.fleet_report()

    assert "need you" in turn.speak
    assert "needs a decision" in turn.speak


def test_the_fleet_report_says_so_when_it_cannot_see(adapter: AmbientVoiceAdapter) -> None:
    assert "can't see" in adapter.fleet_report().speak


# -- AC2: sequencing across sessions -------------------------------------------


def _plan_sessions(tmp_path: Path, count: int) -> list[PlanStep]:
    steps: list[PlanStep] = []
    for index in range(count):
        session_dir = tmp_path / f"s-{index}"
        session_dir.mkdir()
        steps.append(PlanStep(session_dir, f"s-{index}", f"step {index}", f"step-{index}"))
    return steps


def test_a_plan_runs_one_step_at_a_time_under_its_own_lease(tmp_path: Path) -> None:
    steps = _plan_sessions(tmp_path, 3)
    seen: list[str] = []
    plan = FollowOnPlan(
        steps, MJ, submit=lambda step, lease: bool(seen.append(step.step_id)) or True
    )

    results = plan.run()

    assert [r.step_id for r in results] == ["step-0", "step-1", "step-2"]
    assert all(r.ok for r in results)
    assert seen == ["step-0", "step-1", "step-2"]


def test_a_plan_releases_each_lease_so_nothing_stays_locked(tmp_path: Path) -> None:
    steps = _plan_sessions(tmp_path, 1)
    FollowOnPlan(steps, MJ).run()
    assert SessionControl(steps[0].session_dir, steps[0].session_id).active_lease() is None


def test_a_plan_stops_on_a_control_conflict_rather_than_retrying_blindly(
    tmp_path: Path,
) -> None:
    """A human who grabbed the pen mid-plan is a signal, not an obstacle."""
    steps = _plan_sessions(tmp_path, 3)
    # A person is already holding the pen on the second session.
    SessionControl(steps[1].session_dir, steps[1].session_id).acquire(Actor(id="sam", kind=HUMAN))

    results = FollowOnPlan(steps, MJ).run()

    assert [r.step_id for r in results] == ["step-0", "step-1"]
    assert results[-1].ok is False
    assert results[-1].reason == "lease_held"
