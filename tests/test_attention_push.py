"""Durable AttentionRecord -> ntfy destination contract (B7)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from amplifier_app_tui.kernel.attention_push import (
    NtfyAttentionConfig,
    NtfyAttentionDestination,
    ntfy_sequence_id,
    resolve_ntfy_attention_config,
)


class _Hooks:
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[str, dict[str, Any]], Awaitable[Any]]] = {}
        self.unregistered: list[str] = []

    def register(self, event: str, handler, *, priority: int, name: str):
        assert priority == 110
        self.handlers[event] = handler

        def unregister() -> None:
            self.unregistered.append(name)
            self.handlers.pop(event, None)

        return unregister

    async def emit(self, event: str, data: dict[str, Any]) -> Any:
        return await self.handlers[event](event, data)


def test_sequence_id_is_stable_safe_and_distinguishes_events() -> None:
    first = ntfy_sequence_id("session-1:awaiting_clarification:decision-1")
    again = ntfy_sequence_id("session-1:awaiting_clarification:decision-1")
    other = ntfy_sequence_id("session-1:awaiting_clarification:decision-2")

    assert first == again
    assert first != other
    assert re.fullmatch(r"[-_A-Za-z0-9]{1,64}", first)
    assert len(first) == 64


def test_config_uses_env_only_topic_and_explicit_env_precedence() -> None:
    settings = {
        "config": {
            "notifications": {
                "push": {
                    "enabled": True,
                    "server": "https://settings.example",
                    "priority": "high",
                    "tags": ["robot", "warning"],
                    "topic": "must-not-be-used",
                }
            }
        }
    }
    config = resolve_ntfy_attention_config(
        settings,
        {
            "AMPLIFIER_NTFY_TOPIC": "env-topic",
            "AMPLIFIER_NTFY_SERVER": "https://env.example/",
            "AMPLIFIER_NOTIFY_PUSH_ENABLED": "false",
        },
    )

    assert config.topic == "env-topic"
    assert config.server == "https://env.example"
    assert config.enabled is False
    assert config.priority == "high"
    assert config.tags == ("robot", "warning")


def test_global_suppression_disables_push_even_with_topic() -> None:
    config = resolve_ntfy_attention_config(
        {"config": {"notifications": {"suppress": True, "push": {"enabled": True}}}},
        {"AMPLIFIER_NTFY_TOPIC": "valid-topic"},
    )
    assert config.enabled is False
    assert config.ready is False


@pytest.mark.parametrize(
    ("server", "ready"),
    [
        ("https://ntfy.example", True),
        ("https://ntfy.example/base", True),
        ("http://localhost:8080", True),
        ("http://dev.localhost:8080", True),
        ("http://127.0.0.1:8080", True),
        ("http://[::1]:8080", True),
        ("http://ntfy.example", False),
        ("https://user:password@ntfy.example", False),
        ("https://ntfy.example?token=bad", False),
        ("file:///tmp/ntfy", False),
    ],
)
def test_remote_server_requires_https_with_explicit_loopback_dev_exception(
    server: str, ready: bool
) -> None:
    assert NtfyAttentionConfig(topic="valid-topic", server=server).ready is ready


@pytest.mark.asyncio
async def test_record_and_ack_use_one_sequence_id_in_fifo_order() -> None:
    requests: list[tuple[str, str, bytes, dict[str, str], float]] = []
    publish_sent = asyncio.Event()

    async def respond(
        method: str,
        url: str,
        body: bytes,
        headers,
        timeout_s: float,
    ) -> int:
        requests.append((method, url, body, dict(headers), timeout_s))
        if method == "POST":
            publish_sent.set()
        return 200

    destination = NtfyAttentionDestination(
        NtfyAttentionConfig(
            topic="private-topic",
            server="https://ntfy.example",
            priority="urgent",
            tags=("robot", "question"),
        ),
        sender=respond,
    )
    hooks = _Hooks()
    destination.register_hooks(hooks)
    event_id = "session-1:awaiting_clarification:decision-1"

    try:
        await hooks.emit(
            "attention:recorded",
            {
                "event_id": event_id,
                "title": "Amplifier needs you",
                "body": "Which label should I use?",
            },
        )
        await asyncio.wait_for(publish_sent.wait(), timeout=1.0)
        await hooks.emit(
            "attention:acknowledged",
            {"event_id": event_id, "acknowledged": True},
        )
        await destination.drain()
    finally:
        await destination.cleanup()

    sequence_id = ntfy_sequence_id(event_id)
    assert [request[0] for request in requests] == ["POST", "PUT"]
    assert requests[0][1] == "https://ntfy.example/private-topic"
    assert requests[0][3]["X-Sequence-ID"] == sequence_id
    assert requests[0][3]["Title"] == "Amplifier needs you"
    assert requests[0][3]["Priority"] == "urgent"
    assert requests[0][3]["Tags"] == "robot,question"
    assert requests[0][2] == b"Which label should I use?"
    assert requests[1][1] == f"https://ntfy.example/private-topic/{sequence_id}/clear"


@pytest.mark.asyncio
async def test_repeat_record_reuses_destination_identity_and_http_failure_is_contained() -> None:
    sequence_ids: list[str] = []

    async def unavailable(
        _method: str,
        _url: str,
        _body: bytes,
        headers,
        _timeout_s: float,
    ) -> int:
        sequence_ids.append(headers["X-Sequence-ID"])
        return 503

    destination = NtfyAttentionDestination(
        NtfyAttentionConfig(topic="private-topic"), sender=unavailable
    )
    hooks = _Hooks()
    destination.register_hooks(hooks)
    payload = {"event_id": "same-event", "title": "Amplifier", "body": "Ready"}

    try:
        # The durable producer normally emits only once. If delivery is
        # retried after a crash/reconnect, ntfy sees the same sequence ID and
        # updates the destination notification instead of creating a new ID.
        await hooks.emit("attention:recorded", payload)
        await hooks.emit("attention:recorded", payload)
        await destination.drain()
    finally:
        await destination.cleanup()

    assert len(sequence_ids) == 2
    assert set(sequence_ids) == {ntfy_sequence_id("same-event")}


@pytest.mark.asyncio
async def test_ack_clear_survives_a_saturated_publish_queue_and_runs_next() -> None:
    requests: list[tuple[str, str]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def blocked_sender(
        method: str,
        url: str,
        _body: bytes,
        _headers,
        _timeout_s: float,
    ) -> int:
        requests.append((method, url))
        if len(requests) == 1:
            first_started.set()
            await release_first.wait()
        return 200

    destination = NtfyAttentionDestination(
        NtfyAttentionConfig(topic="private-topic"), sender=blocked_sender
    )
    hooks = _Hooks()
    destination.register_hooks(hooks)
    event_id = "published-before-saturation"

    try:
        await hooks.emit(
            "attention:recorded",
            {"event_id": event_id, "title": "Amplifier", "body": "Needs you"},
        )
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        # The worker holds the first publish while 128 ordinary publishes fill
        # the bounded pending FIFO.
        for index in range(128):
            await hooks.emit(
                "attention:recorded",
                {
                    "event_id": f"queued-{index}",
                    "title": "Amplifier",
                    "body": "Queued",
                },
            )
        await hooks.emit(
            "attention:acknowledged",
            {"event_id": event_id, "acknowledged": True},
        )
        release_first.set()
        await destination.drain(timeout_s=5.0)
    finally:
        release_first.set()
        await destination.cleanup()

    sequence_id = ntfy_sequence_id(event_id)
    assert requests[0][0] == "POST"
    assert requests[1] == (
        "PUT",
        f"https://ntfy.sh/private-topic/{sequence_id}/clear",
    )


@pytest.mark.asyncio
async def test_late_recorded_replay_cannot_resurrect_an_acknowledged_notification() -> None:
    requests: list[tuple[str, str]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def blocked_sender(
        method: str,
        url: str,
        _body: bytes,
        _headers,
        _timeout_s: float,
    ) -> int:
        requests.append((method, url))
        if len(requests) == 1:
            first_started.set()
            await release_first.wait()
        return 200

    destination = NtfyAttentionDestination(
        NtfyAttentionConfig(topic="private-topic"), sender=blocked_sender
    )
    hooks = _Hooks()
    destination.register_hooks(hooks)
    event_id = "event-cleared-before-replay"

    try:
        await hooks.emit(
            "attention:recorded",
            {"event_id": event_id, "title": "Amplifier", "body": "Needs you"},
        )
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await hooks.emit(
            "attention:acknowledged",
            {"event_id": event_id, "acknowledged": True},
        )
        # Simulate a reconnect/replay after acknowledgement but before the
        # in-flight publish returns. The terminal clear must remain last.
        await hooks.emit(
            "attention:recorded",
            {"event_id": event_id, "title": "Amplifier", "body": "Replay"},
        )
        release_first.set()
        await destination.drain()
    finally:
        release_first.set()
        await destination.cleanup()

    assert requests == [
        ("POST", "https://ntfy.sh/private-topic"),
        (
            "PUT",
            f"https://ntfy.sh/private-topic/{ntfy_sequence_id(event_id)}/clear",
        ),
    ]


@pytest.mark.asyncio
async def test_all_distinct_clears_survive_beyond_the_publish_queue_limit() -> None:
    requests: list[tuple[str, str]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def blocked_sender(
        method: str,
        url: str,
        _body: bytes,
        _headers,
        _timeout_s: float,
    ) -> int:
        requests.append((method, url))
        if len(requests) == 1:
            first_started.set()
            await release_first.wait()
        return 200

    destination = NtfyAttentionDestination(
        NtfyAttentionConfig(topic="private-topic"), sender=blocked_sender
    )
    hooks = _Hooks()
    destination.register_hooks(hooks)

    try:
        await hooks.emit(
            "attention:acknowledged",
            {"event_id": "clear-0", "acknowledged": True},
        )
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        for index in range(1, 129):
            await hooks.emit(
                "attention:acknowledged",
                {"event_id": f"clear-{index}", "acknowledged": True},
            )
        release_first.set()
        await destination.drain(timeout_s=5.0)
    finally:
        release_first.set()
        await destination.cleanup()

    assert len(requests) == 129
    assert all(method == "PUT" for method, _url in requests)
    assert {url.rsplit("/", 2)[-2] for _method, url in requests} == {
        ntfy_sequence_id(f"clear-{index}") for index in range(129)
    }


@pytest.mark.asyncio
async def test_delivery_failure_logs_neither_secret_topic_nor_body(caplog) -> None:
    topic = "sentinel-secret-topic"
    body = "sentinel-private-body"

    async def leak_if_rendered(
        _method: str,
        _url: str,
        _body: bytes,
        _headers,
        _timeout_s: float,
    ) -> int:
        raise RuntimeError(f"request failed for {topic}: {body}")

    destination = NtfyAttentionDestination(
        NtfyAttentionConfig(topic=topic, debug=True), sender=leak_if_rendered
    )
    hooks = _Hooks()
    destination.register_hooks(hooks)

    with caplog.at_level(logging.DEBUG, logger="amplifier_app_tui.kernel.attention_push"):
        try:
            await hooks.emit(
                "attention:recorded",
                {"event_id": "event-1", "title": "Amplifier", "body": body},
            )
            await destination.drain()
        finally:
            await destination.cleanup()

    assert "attention push delivery failed" in caplog.text
    assert topic not in caplog.text
    assert body not in caplog.text


@pytest.mark.asyncio
async def test_missing_or_invalid_topic_registers_no_destination_handlers() -> None:
    for topic in ("", "contains/slash", "x" * 65):
        destination = NtfyAttentionDestination(NtfyAttentionConfig(topic=topic))
        hooks = _Hooks()
        destination.register_hooks(hooks)
        assert hooks.handlers == {}
        await destination.cleanup()
