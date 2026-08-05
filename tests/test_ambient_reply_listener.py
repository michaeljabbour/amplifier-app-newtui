"""E7 listener ownership: boot, discovery, exact binding, and teardown."""

from __future__ import annotations

import json
import os
import socket
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_tui.kernel.ambient.reply import (
    CorrelationTable,
    DeviceRegistry,
    ReplyEnvelope,
    sign_reply,
)
from amplifier_app_tui.kernel.ambient.reply_listener import (
    STATUS_BIND_FAILED,
    STATUS_DISCOVERY_FAILED,
    STATUS_STARTED,
    STATUS_STOPPED,
    ReplyListenerEndpoint,
    ReplyListenerLifecycle,
    ReplyListenerRegistry,
    discover_reply_endpoints,
    discover_reply_endpoints_for_event,
)
from amplifier_app_tui.model.queues import NeedsYouQueue
from amplifier_app_tui.ui import runtime_adapter

NOW = 5_000.0


def _session_dir(tmp_path: Path) -> Path:
    path = tmp_path / "projects" / "p" / "sessions" / "s-1"
    path.mkdir(parents=True)
    return path


def _signed(secret: str, event_id: str, text: str) -> ReplyEnvelope:
    unsigned = ReplyEnvelope(
        event_id=event_id,
        text=text,
        device_id="phone-1",
        principal_id="mj",
        issued_at=NOW,
        nonce="nonce-1",
    )
    return ReplyEnvelope(**{**unsigned.__dict__, "signature": sign_reply(secret, unsigned)})


def test_lifecycle_publishes_private_endpoint_and_removes_it_on_close(tmp_path: Path) -> None:
    session_dir = _session_dir(tmp_path)
    lifecycle = ReplyListenerLifecycle(
        "s-1",
        session_dir,
        NeedsYouQueue(),
        ambient_root=tmp_path / "ambient",
        now=lambda: NOW,
    )

    status = lifecycle.start()

    assert status.active and status.reason == STATUS_STARTED
    assert status.endpoint is not None
    endpoint = status.endpoint
    assert endpoint.host == "127.0.0.1" and endpoint.port > 0
    assert endpoint.url == f"http://127.0.0.1:{endpoint.port}/reply"
    assert lifecycle.start() == status  # boot retries cannot spawn a second listener
    assert discover_reply_endpoints(session_dir, "s-1") == (endpoint,)
    registry = ReplyListenerRegistry(session_dir, "s-1")
    record_path = registry.registration_path(endpoint.owner_id)
    assert record_path.stat().st_mode & 0o777 == 0o600
    assert registry.root.stat().st_mode & 0o777 == 0o700

    lifecycle.close()
    lifecycle.close()  # shutdown is idempotent

    assert lifecycle.status.reason == STATUS_STOPPED
    assert discover_reply_endpoints(session_dir, "s-1") == ()
    assert not record_path.exists()


def test_discovered_listener_answers_only_the_correlated_decision(tmp_path: Path) -> None:
    session_dir = _session_dir(tmp_path)
    ambient_root = tmp_path / "ambient"
    needs_you = NeedsYouQueue()
    first = needs_you.defer("First question?", custom=True)
    target = needs_you.defer("Target question?", custom=True)
    event_id = "s-1:awaiting_clarification:target"
    CorrelationTable(ambient_root, now=lambda: NOW).bind_clarification(
        event_id=event_id,
        session_id="s-1",
        decision_id=target.decision_id,
        session_dir=session_dir,
        project="p",
    )
    secret = DeviceRegistry(ambient_root, now=lambda: NOW).enroll("phone-1", "mj")
    lifecycle = ReplyListenerLifecycle(
        "s-1",
        session_dir,
        needs_you,
        ambient_root=ambient_root,
        now=lambda: NOW,
    )

    try:
        endpoint = lifecycle.start().endpoint
        assert endpoint is not None
        assert discover_reply_endpoints_for_event(
            event_id,
            ambient_root=ambient_root,
        ) == (endpoint,)
        envelope = _signed(secret, event_id, "violet-otter")
        connection = HTTPConnection(endpoint.host, endpoint.port, timeout=2.0)
        connection.request(
            "POST",
            "/reply",
            body=json.dumps(envelope.__dict__),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
    finally:
        lifecycle.close()

    assert response.status == 200
    assert payload["accepted"] is True
    assert payload["session_id"] == "s-1"
    by_id = {item.decision_id: item for item in needs_you.items}
    assert by_id[first.decision_id].status == "pending"
    assert by_id[target.decision_id].status == "answered"
    assert by_id[target.decision_id].answer == "violet-otter"


def test_bind_failure_is_non_fatal_and_publishes_nothing(tmp_path: Path) -> None:
    session_dir = _session_dir(tmp_path)

    def fail_to_bind(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OSError("port unavailable")

    lifecycle = ReplyListenerLifecycle(
        "s-1",
        session_dir,
        NeedsYouQueue(),
        ambient_root=tmp_path / "ambient",
        listener_factory=fail_to_bind,
    )

    status = lifecycle.start()

    assert not status.active and status.reason == STATUS_BIND_FAILED
    assert discover_reply_endpoints(session_dir, "s-1") == ()


def test_discovery_failure_closes_the_undiscoverable_socket(tmp_path: Path) -> None:
    session_dir = _session_dir(tmp_path)

    class RecordingListener:
        address = ("127.0.0.1", 32123)

        def __init__(self) -> None:
            self.started = False
            self.closed = False

        def start(self) -> RecordingListener:
            self.started = True
            return self

        def close(self) -> None:
            self.closed = True

    class BrokenRegistry:
        def publish(self, endpoint: ReplyListenerEndpoint) -> Path:
            del endpoint
            raise PermissionError("read-only discovery directory")

        def remove(self, owner_id: str) -> None:
            del owner_id

    listener = RecordingListener()
    lifecycle = ReplyListenerLifecycle(
        "s-1",
        session_dir,
        NeedsYouQueue(),
        ambient_root=tmp_path / "ambient",
        listener_factory=lambda *args, **kwargs: listener,
        registry=BrokenRegistry(),  # type: ignore[arg-type]
    )

    status = lifecycle.start()

    assert listener.started and listener.closed
    assert not status.active and status.reason == STATUS_DISCOVERY_FAILED


def test_registry_keeps_other_live_owners_and_prunes_stale_records(tmp_path: Path) -> None:
    session_dir = _session_dir(tmp_path)
    registry = ReplyListenerRegistry(
        session_dir,
        "s-1",
        process_alive=lambda pid: pid != 999,
        endpoint_alive=lambda endpoint: endpoint.owner_id != "owner-stale",
    )
    older = ReplyListenerEndpoint("s-1", "owner-a", "127.0.0.1", 31001, 101, 1.0)
    newer = ReplyListenerEndpoint("s-1", "owner-b", "127.0.0.1", 31002, 102, 2.0)
    stale = ReplyListenerEndpoint("s-1", "owner-stale", "127.0.0.1", 31003, 999, 3.0)
    registry.publish(older)
    registry.publish(newer)
    registry.publish(stale)

    assert registry.discover(prune_stale=True) == (newer, older)
    assert not registry.registration_path(stale.owner_id).exists()

    registry.remove(newer.owner_id)

    assert registry.discover() == (older,)
    assert registry.registration_path(older.owner_id).exists()


def test_registry_prunes_live_pid_when_listener_socket_is_gone(tmp_path: Path) -> None:
    session_dir = _session_dir(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        unused_port = int(probe.getsockname()[1])
    registry = ReplyListenerRegistry(session_dir, "s-1")
    stale = ReplyListenerEndpoint(
        "s-1",
        "owner-without-listener",
        "127.0.0.1",
        unused_port,
        os.getpid(),
        NOW,
    )
    path = registry.publish(stale)

    assert registry.discover(prune_stale=True) == ()
    assert not path.exists()


def test_real_adapter_starts_after_identity_and_closes_before_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeLifecycle:
        def __init__(self, session_id: str, session_dir: Path, needs_you: object) -> None:
            calls.append(("init", (session_id, session_dir, needs_you)))

        def start(self) -> Any:
            calls.append(("start", None))
            return type("Status", (), {"active": True, "reason": STATUS_STARTED})()

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(runtime_adapter, "ReplyListenerLifecycle", FakeLifecycle)
    adapter = runtime_adapter.RealRuntimeAdapter(bundle="x")
    session_dir = _session_dir(tmp_path)
    adapter.session_id = "s-1"
    adapter.session_dir = session_dir

    adapter._start_ambient_reply_listener()
    adapter._start_ambient_reply_listener()  # one lifecycle per adapter
    adapter.shutdown()
    adapter.shutdown()  # cleanup is idempotent

    assert calls == [
        ("init", ("s-1", session_dir, adapter.needs_you)),
        ("start", None),
        ("close", None),
    ]


def test_real_adapter_contains_lifecycle_construction_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("optional surface failed")

    monkeypatch.setattr(runtime_adapter, "ReplyListenerLifecycle", fail)
    adapter = runtime_adapter.RealRuntimeAdapter(bundle="x")
    adapter.session_id = "s-1"
    adapter.session_dir = _session_dir(tmp_path)

    adapter._start_ambient_reply_listener()  # must not raise

    assert adapter._ambient_reply is None
    adapter.shutdown()


def test_registry_rejects_a_non_loopback_discovery_record(tmp_path: Path) -> None:
    registry = ReplyListenerRegistry(_session_dir(tmp_path), "s-1")
    endpoint = ReplyListenerEndpoint("s-1", "owner", "0.0.0.0", 31001, os.getpid(), NOW)

    with pytest.raises(ValueError, match="invalid reply listener endpoint"):
        registry.publish(endpoint)
