"""E6 -- cross-project session discovery (AC2 / AC5).

Built against a hand-made ``tmp_path`` tree of fake session directories, as
the design doc's test strategy asks: no store, no runtime, no session. The
load-bearing assertion is the one about partial views -- an unreadable session
directory must degrade to a partial row, never an exception.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.ambient.discovery import (
    STATE_AWAITING_YOU,
    STATE_FAILED,
    STATE_IDLE,
    STATE_RUNNING,
    SessionDiscovery,
    discover_sessions,
    project_row,
)

NOW = 1000.0


def _session(
    root: Path,
    project: str,
    session_id: str,
    *,
    control: dict | None = None,
    attention: dict | None = None,
    audit: list[dict] | None = None,
) -> Path:
    session_dir = root / project / "sessions" / session_id
    session_dir.mkdir(parents=True)
    if control is not None:
        (session_dir / "control.json").write_text(json.dumps(control), encoding="utf-8")
    if attention is not None:
        (session_dir / "attention.json").write_text(json.dumps(attention), encoding="utf-8")
    if audit is not None:
        (session_dir / "control-audit.jsonl").write_text(
            "\n".join(json.dumps(entry) for entry in audit) + "\n", encoding="utf-8"
        )
    return session_dir


def _lease(actor_id: str = "bot-1", expires_at: float = NOW + 60) -> dict:
    return {
        "lease_id": "l-1",
        "actor": {"id": actor_id, "kind": "automation"},
        "expires_at": expires_at,
        "granted_at": NOW,
        "epoch": 1,
    }


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    _session(root, "-Users-mj-alpha", "s-running", control={"lease": _lease()})
    _session(
        root,
        "-Users-mj-alpha",
        "s-parked",
        control={"paused": True, "handoffs": [{"handoff_id": "ho-1"}]},
        audit=[
            {"seq": 1, "action": "session.paused", "detail": {"why": "needs human judgment"}},
        ],
    )
    _session(root, "-Users-mj-beta", "s-idle", control={})
    _session(
        root,
        "-Users-mj-beta",
        "s-broken",
        attention={
            "by_id": {"e-1": {"session_id": "s-broken", "reason": "error", "event_id": "e-1"}},
            "current": {"s-broken": "e-1"},
        },
    )
    return root


def test_discovery_spans_projects(tree: Path) -> None:
    """The one thing SessionStore.list_sessions structurally cannot do."""
    rows = discover_sessions(tree, now=NOW)
    assert {row.session_id for row in rows} == {"s-running", "s-parked", "s-idle", "s-broken"}
    assert {row.project for row in rows} == {"-Users-mj-alpha", "-Users-mj-beta"}


def test_states_are_projected_from_the_files_b6_and_b7_already_write(tree: Path) -> None:
    rows = {row.session_id: row for row in discover_sessions(tree, now=NOW)}
    assert rows["s-running"].state == STATE_RUNNING
    assert rows["s-parked"].state == STATE_AWAITING_YOU
    assert rows["s-idle"].state == STATE_IDLE
    assert rows["s-broken"].state == STATE_FAILED


def test_an_expired_lease_is_not_a_running_session(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _session(root, "p", "s", control={"lease": _lease(expires_at=NOW - 1)})
    assert discover_sessions(root, now=NOW)[0].state == STATE_IDLE


def test_why_it_paused_comes_from_the_audit_trail(tree: Path) -> None:
    rows = {row.session_id: row for row in discover_sessions(tree, now=NOW)}
    assert rows["s-parked"].why_paused == "needs human judgment"
    assert rows["s-parked"].handoff_ids == ("ho-1",)


def test_a_resumed_session_no_longer_reports_why_it_paused(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _session(
        root,
        "p",
        "s",
        control={},
        audit=[
            {"seq": 1, "action": "session.paused", "detail": {"why": "old reason"}},
            {"seq": 2, "action": "session.resumed"},
        ],
    )
    assert discover_sessions(root, now=NOW)[0].why_paused == ""


def test_an_acknowledged_attention_record_no_longer_asks_for_you(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _session(
        root,
        "p",
        "s",
        attention={
            "by_id": {
                "e-1": {
                    "session_id": "s",
                    "reason": "awaiting_clarification",
                    "event_id": "e-1",
                    "acknowledged": True,
                }
            },
            "current": {"s": "e-1"},
        },
    )
    row = discover_sessions(root, now=NOW)[0]
    assert row.needs_you == ""
    assert row.attention_event_id == ""


def test_an_unreadable_session_degrades_to_a_partial_row_never_an_exception(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    session_dir = _session(root, "p", "s", control={})
    (session_dir / "control.json").write_text("{ this is not json", encoding="utf-8")

    rows = discover_sessions(root, now=NOW)

    assert len(rows) == 1
    assert rows[0].partial is True
    assert rows[0].state == STATE_IDLE  # still reportable, just incomplete


def test_a_session_with_no_control_plane_is_normal_not_partial(tmp_path: Path) -> None:
    """Opt-in means most sessions have no control.json at all."""
    root = tmp_path / "projects"
    _session(root, "p", "s")
    assert discover_sessions(root, now=NOW)[0].partial is False


def test_a_missing_root_is_empty_not_an_error(tmp_path: Path) -> None:
    assert discover_sessions(tmp_path / "nope", now=NOW) == []


def test_every_row_carries_a_runnable_attach_command(tree: Path) -> None:
    """AC5 reduces to zero extensions -- the row just has to route to it."""
    row = next(r for r in discover_sessions(tree, now=NOW) if r.session_id == "s-parked")
    assert row.ref == "amplifier-session:s-parked"
    assert row.attach_command.startswith("amplifier-tui serve --attach amplifier-session:s-parked")


def test_project_row_is_callable_on_one_directory(tree: Path) -> None:
    session_dir = tree / "-Users-mj-alpha" / "sessions" / "s-running"
    assert project_row(session_dir, "-Users-mj-alpha", now=NOW).state == STATE_RUNNING


# -- the mtime cache ----------------------------------------------------------


def test_rows_are_cached_on_the_session_directory_mtime(tree: Path, monkeypatch) -> None:
    calls: list[str] = []
    import amplifier_app_tui.kernel.ambient.discovery as module

    original = module.project_row

    def counting(session_dir: Path, project: str, *, now: float):
        calls.append(session_dir.name)
        return original(session_dir, project, now=now)

    monkeypatch.setattr(module, "project_row", counting)
    discovery = SessionDiscovery(tree, now=lambda: NOW)

    first = discovery.rows()
    calls.clear()
    second = discovery.rows()

    assert calls == []  # nothing changed -> nothing re-read
    assert [r.session_id for r in first] == [r.session_id for r in second]


def test_a_changed_session_is_re_read(tree: Path) -> None:
    discovery = SessionDiscovery(tree, now=lambda: NOW)
    discovery.rows()
    parked = tree / "-Users-mj-alpha" / "sessions" / "s-parked"
    (parked / "control.json").write_text(json.dumps({"paused": False}), encoding="utf-8")
    import os

    os.utime(parked, (NOW + 10, NOW + 10))

    row = next(r for r in discovery.rows() if r.session_id == "s-parked")
    assert row.state == STATE_IDLE


def test_a_removed_session_leaves_the_cache(tree: Path) -> None:
    discovery = SessionDiscovery(tree, now=lambda: NOW)
    discovery.rows()
    import shutil

    shutil.rmtree(tree / "-Users-mj-beta" / "sessions" / "s-idle")
    assert "s-idle" not in {row.session_id for row in discovery.rows()}


def test_needing_attention_is_the_fleet_shortlist(tree: Path) -> None:
    discovery = SessionDiscovery(tree, now=lambda: NOW)
    assert {row.session_id for row in discovery.needing_attention()} == {"s-parked", "s-broken"}


def test_an_event_id_resolves_to_its_session_across_projects(tree: Path) -> None:
    """What makes a notification tappable: the payload knows only event_id."""
    discovery = SessionDiscovery(tree, now=lambda: NOW)
    row = discovery.find_by_event_id("e-1")
    assert row is not None and row.session_id == "s-broken"
    assert discovery.find_by_event_id("nope") is None
