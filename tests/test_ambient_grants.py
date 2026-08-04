"""E2 -- the permission-grant store (AC4).

The design doc's own test strategy asks for exactly three cases by name: an
empty-grants case proving deny-by-default, an expired-grant case, and a
revoked-mid-turn case. All three are here, plus the rules that keep a grant
from quietly widening: no wildcards, no verb bleed, no third-party minting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.ambient.grants import (
    REASON_EXPIRED,
    REASON_GRANTED,
    REASON_NO_GRANT,
    REASON_NO_SELECTOR,
    REASON_REVOKED,
    REASON_SELECTOR_MISMATCH,
    GrantError,
    GrantStore,
    authorize_source,
    consume_grant,
    parse_scope,
)
from amplifier_app_tui.kernel.ambient.principal import LocalPrincipal
from amplifier_app_tui.kernel.session_control import AUDIT_FILENAME, HUMAN, Actor, SessionControl


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


MJ = Actor(id="mj", kind=HUMAN)
INBOX = {"folder": "Inbox", "from": "dana@example.com"}


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(tmp_path: Path, clock: _Clock) -> GrantStore:
    return GrantStore(tmp_path / "ambient", now=clock)


def test_empty_store_denies_by_default(store: GrantStore) -> None:
    decision = store.authorize("mj", "source:outlook", "read", INBOX)
    assert not decision.allowed
    assert decision.reason == REASON_NO_GRANT


def test_a_denial_is_surfaced_not_silent(store: GrantStore) -> None:
    """A refusal must be reportable ("I can't see your mail"), never a skip."""
    decision = store.authorize("mj", "source:outlook", "read", INBOX)
    assert decision.reason  # always populated, allow or deny
    trail = [e["action"] for e in store.audit_entries()]
    assert "grant.denied" in trail


def test_a_matching_grant_allows(store: GrantStore) -> None:
    grant = store.create(
        principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ
    )
    decision = store.authorize("mj", "source:outlook", "read", INBOX)
    assert decision.allowed
    assert decision.grant_id == grant.grant_id
    assert decision.reason == REASON_GRANTED


def test_an_expired_grant_denies(store: GrantStore, clock: _Clock) -> None:
    store.create(
        principal="mj",
        scope="source:outlook",
        verb="read",
        selector=INBOX,
        granted_by=MJ,
        expires_at=clock.now + 60.0,
    )
    assert store.authorize("mj", "source:outlook", "read", INBOX).allowed
    clock.advance(61.0)
    decision = store.authorize("mj", "source:outlook", "read", INBOX)
    assert not decision.allowed
    assert decision.reason == REASON_EXPIRED


def test_a_revoke_lands_on_the_very_next_read_from_another_process(
    tmp_path: Path, clock: _Clock
) -> None:
    """The anti-caching guarantee: a cached grant is a revoke that didn't happen.

    The revoke is issued through a SECOND store object over the same files --
    the closest offline stand-in for another process -- so a store that cached
    grants at construction would still allow the read and fail this test.
    """
    root = tmp_path / "ambient"
    using = GrantStore(root, now=clock)
    revoking = GrantStore(root, now=clock)
    grant = using.create(
        principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ
    )
    assert using.authorize("mj", "source:outlook", "read", INBOX).allowed

    revoking.revoke(grant.grant_id, actor=MJ)

    decision = using.authorize("mj", "source:outlook", "read", INBOX)
    assert not decision.allowed
    assert decision.reason == REASON_REVOKED


def test_read_never_implies_send(store: GrantStore) -> None:
    store.create(principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ)
    assert not store.authorize("mj", "source:outlook", "send", INBOX).allowed


def test_a_selectorless_source_grant_is_invalid_not_everything(store: GrantStore) -> None:
    with pytest.raises(GrantError, match="wildcard"):
        store.create(principal="mj", scope="source:outlook", verb="read", granted_by=MJ)


def test_a_request_broader_than_its_grant_is_denied(store: GrantStore) -> None:
    store.create(principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ)
    decision = store.authorize("mj", "source:outlook", "read", {"folder": "Inbox"})
    assert not decision.allowed
    assert decision.reason == REASON_SELECTOR_MISMATCH


def test_a_request_narrower_than_its_grant_is_allowed(store: GrantStore) -> None:
    store.create(
        principal="mj",
        scope="source:outlook",
        verb="read",
        selector={"folder": "Inbox"},
        granted_by=MJ,
    )
    assert store.authorize("mj", "source:outlook", "read", {**INBOX}).allowed


def test_a_source_read_with_no_selector_is_refused(store: GrantStore) -> None:
    store.create(principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ)
    decision = store.authorize("mj", "source:outlook", "read", {})
    assert not decision.allowed
    assert decision.reason == REASON_NO_SELECTOR


def test_a_grant_belongs_to_one_principal(store: GrantStore) -> None:
    store.create(principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ)
    assert not store.authorize("someone-else", "source:outlook", "read", INBOX).allowed


def test_source_grants_get_a_mandatory_expiry(store: GrantStore, clock: _Clock) -> None:
    grant = store.create(
        principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ
    )
    assert grant.expires_at is not None and grant.expires_at > clock.now


def test_session_grants_may_be_open_ended(store: GrantStore) -> None:
    grant = store.create(principal="mj", scope="session:abc", verb="control", granted_by=MJ)
    assert grant.expires_at is None


def test_only_a_first_party_surface_may_mint(store: GrantStore) -> None:
    """A voice channel may request a grant; it may never create one."""
    with pytest.raises(GrantError, match="first-party"):
        store.create(
            principal="mj",
            scope="source:outlook",
            verb="read",
            selector=INBOX,
            granted_by=MJ,
            surface="voice",
        )


def test_an_unknown_scope_family_is_refused_at_creation(store: GrantStore) -> None:
    with pytest.raises(GrantError):
        store.create(principal="mj", scope="mailbox:everything", verb="read", granted_by=MJ)


def test_a_verb_a_family_does_not_offer_is_refused(store: GrantStore) -> None:
    with pytest.raises(GrantError, match="does not offer"):
        store.create(principal="mj", scope="project:demo", verb="send", granted_by=MJ)


def test_parse_scope_splits_family_and_target() -> None:
    assert parse_scope("source:outlook") == ("source", "outlook")
    with pytest.raises(GrantError):
        parse_scope("source:")


def test_authorize_source_is_pure_and_table_driven() -> None:
    """No I/O, no clock of its own -- the doc's stated test posture."""
    assert not authorize_source([], "mj", "source:teams", "read", {"team": "x"}, 0.0).allowed


def test_the_grant_trail_records_creation_and_revocation(store: GrantStore) -> None:
    grant = store.create(
        principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ
    )
    store.revoke(grant.grant_id, actor=MJ)
    actions = [entry["action"] for entry in store.audit_entries()]
    assert actions == ["grant.created", "grant.revoked"]


def test_a_repeated_revoke_is_a_no_op_not_an_error(store: GrantStore) -> None:
    grant = store.create(
        principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ
    )
    assert store.revoke(grant.grant_id, actor=MJ) is not None
    assert store.revoke(grant.grant_id, actor=MJ) is None


def test_a_corrupt_grants_file_denies_rather_than_crashing(tmp_path: Path) -> None:
    root = tmp_path / "ambient"
    root.mkdir(parents=True)
    (root / "grants.json").write_text("{not json", encoding="utf-8")
    store = GrantStore(root)
    assert not store.authorize("mj", "source:outlook", "read", INBOX).allowed


# -- attribution into the SESSION trail (the other half of E2) ---------------


def _control(tmp_path: Path, clock: _Clock) -> SessionControl:
    session_dir = tmp_path / "sessions" / "s-1"
    session_dir.mkdir(parents=True)
    return SessionControl(session_dir, "s-1", now=clock)


def _audit_actions(session_dir: Path) -> list[str]:
    lines = (session_dir / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["action"] for line in lines if line.strip()]


def test_an_allowed_use_is_attributed_into_the_session_trail(
    tmp_path: Path, clock: _Clock, store: GrantStore
) -> None:
    control = _control(tmp_path, clock)
    grant = store.create(
        principal="mj", scope="source:outlook", verb="read", selector=INBOX, granted_by=MJ
    )
    principal = LocalPrincipal("mj", kind=HUMAN, verified=True)

    decision = consume_grant(
        store, control, principal, scope="source:outlook", verb="read", selector=INBOX
    )

    assert decision.allowed
    entries = control.audit_entries(limit=10)
    read_entry = next(e for e in entries if e["action"] == "source.read")
    assert read_entry["detail"]["grant_id"] == grant.grant_id
    assert read_entry["actor"]["id"] == "mj"


def test_a_denied_use_is_attributed_too(tmp_path: Path, clock: _Clock, store: GrantStore) -> None:
    control = _control(tmp_path, clock)
    principal = LocalPrincipal("mj", kind=HUMAN, verified=True)

    consume_grant(store, control, principal, scope="source:outlook", verb="read", selector=INBOX)

    assert "source.denied" in _audit_actions(control.session_dir)


def test_a_send_is_audited_as_a_send(tmp_path: Path, clock: _Clock, store: GrantStore) -> None:
    control = _control(tmp_path, clock)
    store.create(
        principal="mj", scope="source:teams", verb="send", selector={"team": "core"}, granted_by=MJ
    )
    principal = LocalPrincipal("mj", kind=HUMAN, verified=True)

    consume_grant(
        store, control, principal, scope="source:teams", verb="send", selector={"team": "core"}
    )

    assert "source.send" in _audit_actions(control.session_dir)


def test_the_ambient_vocabulary_cannot_forge_a_control_action(
    tmp_path: Path, clock: _Clock
) -> None:
    """An ambient caller may add to the account, never fake a lease decision."""
    control = _control(tmp_path, clock)
    with pytest.raises(ValueError, match="not an ambient audit action"):
        control.note_ambient("lease.granted", MJ)
