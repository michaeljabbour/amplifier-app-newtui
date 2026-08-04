"""E8 -- the external-source port and its local implementation.

**No real Teams/Outlook connector is tested here because none is built.** See
``kernel/ambient/sources.py`` for why (Graph credentials, tenant consent and
network access, none of which exist offline). What IS tested is the part that
does not move when a real connector arrives: the port's shape, and the
permission/attribution wrapper every connector is reached through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.ambient.grants import GrantStore
from amplifier_app_tui.kernel.ambient.principal import LocalPrincipal
from amplifier_app_tui.kernel.ambient.sources import (
    GrantedSource,
    LocalFileSource,
    SourceItem,
    SourcePort,
)
from amplifier_app_tui.kernel.session_control import HUMAN, Actor, SessionControl

MJ_ACTOR = Actor(id="mj", kind=HUMAN)
MJ = LocalPrincipal("mj", kind=HUMAN, verified=True)
THREAD = {"folder": "Inbox", "from": "dana@example.com"}


@pytest.fixture
def source(tmp_path: Path) -> LocalFileSource:
    root = tmp_path / "mail"
    root.mkdir()
    (root / "items.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "item_id": "m-1",
                    "sender": "dana@example.com",
                    "from": "dana@example.com",
                    "folder": "Inbox",
                    "subject": "Thursday?",
                    "preview": "Are we still on for Thursday",
                },
                {
                    "item_id": "m-2",
                    "sender": "someone@example.com",
                    "from": "someone@example.com",
                    "folder": "Inbox",
                    "subject": "Unrelated",
                    "preview": "hello",
                },
            ]
        ),
        encoding="utf-8",
    )
    return LocalFileSource(root, scope_name="source:local")


@pytest.fixture
def grants(tmp_path: Path) -> GrantStore:
    return GrantStore(tmp_path / "ambient")


@pytest.fixture
def control(tmp_path: Path) -> SessionControl:
    return SessionControl(tmp_path / "s-1", "s-1")


def test_the_local_source_satisfies_the_port(source: LocalFileSource) -> None:
    assert isinstance(source, SourcePort)


def test_a_source_item_carries_a_preview_and_has_no_body_field() -> None:
    """Reading someone's mail is itself a privacy act -- previews only."""
    assert not hasattr(SourceItem("m-1"), "body")
    assert "body" not in SourceItem("m-1").as_dict()


def test_the_selector_narrows_what_comes_back(source: LocalFileSource) -> None:
    items = source.fetch(THREAD)
    assert [item.item_id for item in items] == ["m-1"]


def test_without_a_grant_nothing_is_read_and_the_refusal_is_reported(
    source: LocalFileSource, grants: GrantStore, control: SessionControl
) -> None:
    guarded = GrantedSource(source, grants, MJ, control=control)

    items, decision = guarded.fetch(THREAD)

    assert items == ()
    assert not decision.allowed
    assert decision.reason == "no_grant"  # sayable: "I can't see your mail"


def test_with_a_grant_the_read_goes_through_and_is_attributed(
    source: LocalFileSource, grants: GrantStore, control: SessionControl
) -> None:
    grant = grants.create(
        principal="mj", scope="source:local", verb="read", selector=THREAD, granted_by=MJ_ACTOR
    )
    guarded = GrantedSource(source, grants, MJ, control=control)

    items, decision = guarded.fetch(THREAD)

    assert [item.item_id for item in items] == ["m-1"]
    entry = next(e for e in control.audit_entries(limit=10) if e["action"] == "source.read")
    assert entry["detail"]["grant_id"] == grant.grant_id


def test_a_read_grant_does_not_authorize_a_send(
    source: LocalFileSource, grants: GrantStore, control: SessionControl
) -> None:
    grants.create(
        principal="mj", scope="source:local", verb="read", selector=THREAD, granted_by=MJ_ACTOR
    )
    guarded = GrantedSource(source, grants, MJ, control=control)

    result, decision = guarded.send(THREAD, "on for Thursday")

    assert not result.ok
    assert not decision.allowed
    assert not (Path(source.root) / "outbox.jsonl").exists()  # nothing left the machine


def test_a_send_grant_authorizes_a_send_and_is_audited_as_one(
    source: LocalFileSource, grants: GrantStore, control: SessionControl
) -> None:
    grants.create(
        principal="mj", scope="source:local", verb="send", selector=THREAD, granted_by=MJ_ACTOR
    )
    guarded = GrantedSource(source, grants, MJ, control=control)

    result, decision = guarded.send(THREAD, "on for Thursday")

    assert result.ok and decision.allowed
    assert "source.send" in [e["action"] for e in control.audit_entries(limit=10)]


def test_a_revoke_stops_the_very_next_fetch(
    source: LocalFileSource, grants: GrantStore, control: SessionControl
) -> None:
    """The connector is reached through the check EVERY time, not once."""
    grant = grants.create(
        principal="mj", scope="source:local", verb="read", selector=THREAD, granted_by=MJ_ACTOR
    )
    guarded = GrantedSource(source, grants, MJ, control=control)
    assert guarded.fetch(THREAD)[1].allowed

    grants.revoke(grant.grant_id, actor=MJ_ACTOR)

    items, decision = guarded.fetch(THREAD)
    assert items == () and decision.reason == "revoked"


def test_previews_are_capped_at_the_source_boundary(tmp_path: Path) -> None:
    root = tmp_path / "mail"
    root.mkdir()
    (root / "items.jsonl").write_text(
        json.dumps({"item_id": "m", "folder": "Inbox", "preview": "x" * 500}), encoding="utf-8"
    )
    item = LocalFileSource(root).fetch({"folder": "Inbox"})[0]
    assert len(item.preview) == 160


def test_a_missing_source_file_returns_nothing_rather_than_raising(tmp_path: Path) -> None:
    assert LocalFileSource(tmp_path / "absent").fetch({"folder": "Inbox"}) == []
