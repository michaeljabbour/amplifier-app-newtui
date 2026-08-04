"""Authorization for the control plane: principals, permissions, provenance.

The hole this closes, stated plainly: ``Actor.kind`` used to be a string the
client chose. Because the takeover rule says a ``human`` always outranks an
``automation``, typing ``{"kind": "human"}`` was enough to seize the write
lease from a real person's controller. Over a pipe the OS vouched for, that
was a courtesy; anywhere else it is privilege escalation.

These tests pin the two halves of the fix -- *who you are* (a principal
established from a credential) and *what you may do* (three permissions) --
plus the property that makes the audit trail probative afterwards: an
authenticated identity is distinguishable on the record from a claimed one.
"""

from __future__ import annotations

from pathlib import Path

from amplifier_app_tui.kernel.session_authz import (
    ALL_PERMISSIONS,
    AUTHZ_FILENAME,
    CONTROL,
    READ,
    TRUSTED_LOCAL,
    WRITE,
    Principal,
    StaticPolicy,
    TokenPolicy,
    TokenStore,
    normalize_permissions,
    policy_for,
)
from amplifier_app_tui.kernel.session_control import (
    REASON_IDENTITY_UNVERIFIED,
    REASON_PERMISSION_DENIED,
    REASON_UNAUTHENTICATED,
    Actor,
    SessionControl,
)


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _control(tmp_path: Path, **kwargs: object) -> SessionControl:
    return SessionControl(tmp_path / "s", "s" * 32, **kwargs)  # type: ignore[arg-type]


# -- the token store ----------------------------------------------------------


def test_a_token_is_never_stored_in_the_clear(tmp_path: Path) -> None:
    """A stolen store file must yield nothing usable."""
    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    plaintext, grant = store.issue("bot-1", kind="automation")

    on_disk = (tmp_path / AUTHZ_FILENAME).read_text()
    assert plaintext not in on_disk
    assert grant.token_hash in on_disk
    assert store.resolve(plaintext) is not None
    assert store.resolve(plaintext + "x") is None


def test_revoking_a_token_takes_effect_on_the_very_next_use(tmp_path: Path) -> None:
    """A cached grant is a revoke that did not happen, so nothing is cached."""
    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    plaintext, grant = store.issue("bot-1")
    policy = TokenPolicy(store)
    assert policy.resolve({"token": plaintext}) is not None

    store.revoke(grant.token_id)
    assert policy.resolve({"token": plaintext}) is None


def test_an_expired_token_stops_authenticating(tmp_path: Path) -> None:
    clock = _Clock()
    store = TokenStore(tmp_path / AUTHZ_FILENAME, now=clock)
    plaintext, _ = store.issue("bot-1", ttl=60)
    assert store.resolve(plaintext) is not None

    clock.now += 61
    assert store.resolve(plaintext) is None


def test_permissions_are_a_closed_vocabulary(tmp_path: Path) -> None:
    """An unknown verb is dropped, not honoured and not fatal -- a token from a
    newer build still works for the verbs this build understands."""
    assert normalize_permissions(["read", "sudo", "WRITE"]) == {READ, WRITE}
    assert normalize_permissions(None) == frozenset()

    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    plaintext, _ = store.issue("bot-1", permissions=["read", "nonsense"])
    grant = store.resolve(plaintext)
    assert grant is not None and grant.permissions == {READ}


def test_a_project_without_a_token_store_keeps_trusting_the_local_pipe(
    tmp_path: Path,
) -> None:
    """Authorization is opt-in: it exists the moment the first token is issued."""
    assert policy_for(tmp_path / AUTHZ_FILENAME) is TRUSTED_LOCAL

    TokenStore(tmp_path / AUTHZ_FILENAME).issue("bot-1")
    assert isinstance(policy_for(tmp_path / AUTHZ_FILENAME), TokenPolicy)


# -- the escalation this exists to stop ---------------------------------------


def test_an_automation_credential_cannot_claim_to_be_human(tmp_path: Path) -> None:
    """The headline: a real credential, a false claim, a hard refusal.

    ``force=True`` and ``kind:"human"`` together would have beaten a human
    holder before this check existed.
    """
    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    plaintext, _ = store.issue("bot-1", kind="automation")
    control = _control(tmp_path, policy=TokenPolicy(store))

    decision = control.authenticate(
        "lease.takeover",
        {"actor": {"id": "bot-1", "kind": "human"}, "auth": {"token": plaintext}},
        CONTROL,
    )
    assert not decision.allowed
    assert decision.reason == REASON_IDENTITY_UNVERIFIED
    assert decision.records[0]["type"] == "control.conflict"
    assert decision.records[1]["entry"]["action"] == "auth.denied"


def test_a_principal_may_act_below_itself_but_never_above(tmp_path: Path) -> None:
    """A verified human running a bot lane may present ``automation`` and take
    the weaker precedence that comes with it. The reverse is the escalation."""
    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    plaintext, _ = store.issue("mj", kind="human")
    control = _control(tmp_path, policy=TokenPolicy(store))

    downshift = control.authenticate(
        "submit",
        {"actor": {"id": "mj", "kind": "automation"}, "auth": {"token": plaintext}},
        WRITE,
    )
    assert downshift.allowed
    assert downshift.actor.kind == "automation"


def test_an_identity_cannot_be_borrowed_from_another_principal(tmp_path: Path) -> None:
    """Attribution must be truthful: a valid token does not let you sign as
    someone else."""
    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    plaintext, _ = store.issue("bot-1", kind="automation")
    control = _control(tmp_path, policy=TokenPolicy(store))

    decision = control.authenticate(
        "submit",
        {"actor": {"id": "someone-else", "kind": "automation"}, "auth": {"token": plaintext}},
        WRITE,
    )
    assert not decision.allowed
    assert decision.reason == REASON_IDENTITY_UNVERIFIED


def test_no_credential_is_default_deny_once_a_store_exists(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    store.issue("bot-1")
    control = _control(tmp_path, policy=TokenPolicy(store))

    decision = control.authenticate("lease.acquire", {"actor": {"id": "bot-1"}}, CONTROL)
    assert not decision.allowed
    assert decision.reason == REASON_UNAUTHENTICATED


# -- permissions --------------------------------------------------------------


def test_permissions_separate_observing_from_driving_from_seizing(
    tmp_path: Path,
) -> None:
    """Three verbs, and they really are independent: an observer bot can watch
    a session it may not drive, and drive one it may not seize."""
    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    watcher, _ = store.issue("watcher", permissions=["read"])
    driver, _ = store.issue("driver", permissions=["read", "write"])
    control = _control(tmp_path, policy=TokenPolicy(store))

    assert control.authenticate("lease.status", {"auth": {"token": watcher}}, READ).allowed
    denied = control.authenticate("submit", {"auth": {"token": watcher}}, WRITE)
    assert not denied.allowed and denied.reason == REASON_PERMISSION_DENIED

    assert control.authenticate("submit", {"auth": {"token": driver}}, WRITE).allowed
    seizure = control.authenticate("lease.takeover", {"auth": {"token": driver}}, CONTROL)
    assert not seizure.allowed and seizure.reason == REASON_PERMISSION_DENIED


# -- provenance ---------------------------------------------------------------


def test_a_verified_identity_is_distinguishable_from_a_claimed_one(
    tmp_path: Path,
) -> None:
    """What makes the trail probative.

    Unverified (local pipe) records are byte-identical to what this plane
    always wrote -- no ``auth`` block, so its absence honestly means "the OS
    peer, and nothing stronger". A verified principal carries provenance.
    """
    plain = _control(tmp_path / "a")
    unverified = plain.authenticate("submit", {"actor": {"id": "mj", "kind": "human"}}, WRITE)
    assert unverified.allowed
    assert unverified.actor.as_dict() == {"id": "mj", "kind": "human"}

    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    token, _ = store.issue("mj", kind="human", display="MJ")
    verified = _control(tmp_path / "b", policy=TokenPolicy(store)).authenticate(
        "submit", {"actor": {"id": "mj", "kind": "human"}, "auth": {"token": token}}, WRITE
    )
    assert verified.allowed
    assert verified.actor.as_dict()["auth"] == {
        "method": "token",
        "verified": True,
        "principal": "mj",
    }


def test_provenance_survives_a_round_trip_through_durable_state(tmp_path: Path) -> None:
    """A lease granted to a verified principal still names one after a restart
    -- otherwise the trail would forget how the holder proved itself."""
    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    token, _ = store.issue("mj", kind="human")
    session_dir = tmp_path / "s"
    first = SessionControl(session_dir, "s" * 32, policy=TokenPolicy(store))
    decision = first.authenticate("lease.acquire", {"auth": {"token": token}}, CONTROL)
    first.acquire(decision.actor, ttl=600)

    reopened = SessionControl(session_dir, "s" * 32, policy=TokenPolicy(store))
    lease = reopened.active_lease()
    assert lease is not None
    assert lease.actor.auth is not None and lease.actor.auth.verified


def test_client_supplied_provenance_is_ignored(tmp_path: Path) -> None:
    """You cannot mint your own proof: an ``auth`` block on the wire is not
    read back as provenance, only the policy's verdict is."""
    control = _control(tmp_path)
    decision = control.authenticate(
        "submit",
        {"actor": {"id": "mj", "kind": "human", "auth": {"method": "sso", "verified": True}}},
        WRITE,
    )
    assert decision.allowed
    assert decision.actor.auth is None
    assert "auth" not in decision.actor.as_dict()


# -- the networked-adapter shape ----------------------------------------------


def test_a_networked_adapter_maps_its_own_principal_onto_the_plane(
    tmp_path: Path,
) -> None:
    """B8's "E1" in one call.

    An adapter that authenticated its peer some other way (OIDC, mTLS, device
    token) builds the principal itself and every control semantic holds --
    including the refusal of a claim above it. The adapter authenticates; it
    holds no policy.
    """
    principal = Principal(
        principal_id="mj@contoso",
        kind="human",
        permissions=ALL_PERMISSIONS,
        method="oidc",
        verified=True,
        display="MJ",
    )
    control = _control(tmp_path, policy=StaticPolicy(principal))

    granted = control.authenticate("lease.acquire", {}, CONTROL)
    assert granted.allowed
    assert granted.actor.id == "mj@contoso"
    assert granted.actor.as_dict()["auth"]["method"] == "oidc"

    bot = Principal(principal_id="bot", kind="automation", method="oidc", verified=True)
    bot_control = _control(tmp_path / "bot", policy=StaticPolicy(bot))
    refused = bot_control.authenticate(
        "lease.takeover", {"actor": {"id": "bot", "kind": "human"}}, CONTROL
    )
    assert not refused.allowed and refused.reason == REASON_IDENTITY_UNVERIFIED


def test_status_reports_which_policy_is_in_force(tmp_path: Path) -> None:
    """A controller must be able to tell whether it is trusted or verified."""
    assert _control(tmp_path / "a").control_status()["authz"] == {
        "policy": "local-pipe",
        "requires_credential": False,
        "verified": False,
    }

    store = TokenStore(tmp_path / AUTHZ_FILENAME)
    store.issue("bot-1")
    assert _control(tmp_path / "b", policy=TokenPolicy(store)).control_status()["authz"] == {
        "policy": "token",
        "requires_credential": True,
        "verified": True,
    }


def test_unattributed_ops_still_reach_the_no_actor_refusal(tmp_path: Path) -> None:
    """Authentication must not accidentally invent an identity: an acquire with
    no actor under the local policy is still ``no_actor``, not a grant to
    "anonymous"."""
    control = _control(tmp_path)
    decision = control.authenticate("lease.acquire", {}, CONTROL)
    assert decision.allowed
    assert decision.attributed is None

    records = control.acquire(decision.attributed)
    assert records[0]["reason"] == "no_actor"
    assert control.active_lease() is None


def test_an_actor_dataclass_round_trips_through_json(tmp_path: Path) -> None:
    del tmp_path
    actor = Actor.from_dict(
        {
            "id": "mj",
            "kind": "human",
            "display": "MJ",
            "auth": {"method": "token", "verified": True, "principal": "mj"},
        }
    )
    assert actor.auth is not None and actor.auth.method == "token"
    assert Actor.from_dict(actor.as_dict()) == actor
