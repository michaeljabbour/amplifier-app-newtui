"""App-owned ntfy destination for durable attention records.

The TUI's notification correctness boundary is the persisted
``AttentionRecord``.  This adapter consumes only the normalized
``attention:recorded`` and ``attention:acknowledged`` hook events; it never
derives a second notification from ``orchestrator:complete``.

ntfy sequence identifiers are restricted to ``[-_A-Za-z0-9]{1,64}``, while
the application's event IDs deliberately retain readable session/reason
components.  :func:`ntfy_sequence_id` therefore maps an event ID to a stable
64-character SHA-256 hex identifier.  Publishing with that identifier lets
ntfy de-duplicate/update the destination notification; acknowledging the
same event issues ntfy's correlated ``/<sequence-id>/clear`` operation.

The topic is always read from ``AMPLIFIER_NTFY_TOPIC`` and is never accepted
from settings, included in logs, or echoed in an error. Hook handlers enqueue
onto a bounded publish FIFO plus a coalesced terminal-clear lane, so a slow or
unavailable push service can never stall the Amplifier hooks bus or the live
session. Once an event is acknowledged, its terminal state also suppresses
any late ``attention:recorded`` replay for the lifetime of this destination.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import urllib.request
from urllib.parse import urlsplit
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from amplifier_core import HookResult

from .config import (
    merged_push_settings,
    notification_settings,
    notifications_globally_suppressed,
)

logger = logging.getLogger(__name__)

_NTFY_TOPIC_ENV = "AMPLIFIER_NTFY_TOPIC"
_NTFY_SERVER_ENV = "AMPLIFIER_NTFY_SERVER"
_NOTIFY_PUSH_ENABLED_ENV = "AMPLIFIER_NOTIFY_PUSH_ENABLED"
_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})
_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
_PRIORITIES = frozenset({"min", "low", "default", "high", "urgent"})
_DEFAULT_SERVER = "https://ntfy.sh"
_QUEUE_LIMIT = 128

_Sender = Callable[[str, str, bytes, Mapping[str, str], float], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class NtfyAttentionConfig:
    """Resolved non-secret destination settings plus the env-only topic."""

    enabled: bool = True
    server: str = _DEFAULT_SERVER
    topic: str = ""
    priority: str = "default"
    tags: tuple[str, ...] = ("robot",)
    timeout_s: float = 5.0
    debug: bool = False

    @property
    def ready(self) -> bool:
        """Whether delivery is enabled and the secret topic is valid."""

        return (
            self.enabled
            and _TOPIC_RE.fullmatch(self.topic) is not None
            and _server_allowed(self.server)
        )


@dataclass(frozen=True, slots=True)
class _Delivery:
    operation: Literal["publish", "clear"]
    event_id: str
    title: str = "Amplifier"
    body: str = "Ready for input"


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
    return None


def _clean_tags(raw: Any) -> tuple[str, ...]:
    values: list[Any]
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        return ("robot",)
    cleaned: list[str] = []
    for value in values:
        tag = str(value).strip()
        # A tag becomes one comma-delimited HTTP header value.  Drop control
        # characters and embedded delimiters rather than risking an invalid
        # or ambiguous request.
        if tag and "," not in tag and "\r" not in tag and "\n" not in tag:
            cleaned.append(tag)
    return tuple(cleaned)


def _server_allowed(server: str) -> bool:
    """Require encrypted remote delivery; permit plaintext loopback for dev."""

    try:
        parsed = urlsplit(server)
        host = parsed.hostname
        # Accessing ``port`` performs its own malformed/range validation.
        _ = parsed.port
    except ValueError:
        return False
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    normalized_host = host.rstrip(".").lower()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        return False


def resolve_ntfy_attention_config(
    settings: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> NtfyAttentionConfig:
    """Resolve the app-owned push sink with explicit-env precedence.

    ``config.notifications.push`` and ``.ntfy`` provide non-secret defaults.
    The topic is read from the environment only.  An explicit environment
    ``enabled`` or ``server`` value wins over settings, and the persisted
    global suppression switch disables this destination together with the
    local ladder.
    """

    env = os.environ if environ is None else environ
    push = merged_push_settings(notification_settings(settings))

    enabled = _coerce_bool(env.get(_NOTIFY_PUSH_ENABLED_ENV))
    if enabled is None:
        enabled = _coerce_bool(push.get("enabled"))
    if enabled is None:
        enabled = True
    if notifications_globally_suppressed(settings):
        enabled = False

    service = str(push.get("service", "ntfy")).strip().lower()
    if service != "ntfy":
        enabled = False

    server_value = env.get(_NTFY_SERVER_ENV)
    if not server_value:
        server_value = push.get("server") if isinstance(push.get("server"), str) else None
    server = str(server_value or _DEFAULT_SERVER).strip().rstrip("/") or _DEFAULT_SERVER

    priority = str(push.get("priority", "default")).strip().lower()
    if priority not in _PRIORITIES:
        priority = "default"

    timeout_value = push.get("timeout_s", 5.0)
    try:
        timeout_s = float(timeout_value)
    except (TypeError, ValueError):
        timeout_s = 5.0
    timeout_s = min(max(timeout_s, 0.25), 30.0)

    return NtfyAttentionConfig(
        enabled=enabled,
        server=server,
        topic=str(env.get(_NTFY_TOPIC_ENV, "")).strip(),
        priority=priority,
        tags=_clean_tags(push.get("tags", ["robot"])),
        timeout_s=timeout_s,
        debug=_coerce_bool(push.get("debug")) is True,
    )


def ntfy_sequence_id(event_id: str) -> str:
    """Return ntfy's stable, URL/header-safe identity for *event_id*."""

    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()


class NtfyAttentionDestination:
    """Non-blocking FIFO consumer for normalized attention hook events."""

    def __init__(
        self,
        config: NtfyAttentionConfig,
        *,
        sender: _Sender | None = None,
    ) -> None:
        self.config = config
        self._sender = sender or _send_http
        # Ordinary publishes are advisory and bounded. Clears are correctness
        # operations: keep a coalesced lane and a terminal-state set instead
        # of dropping the 129th distinct acknowledgement.
        self._pending: deque[_Delivery] = deque()
        self._pending_clears: deque[str] = deque()
        self._queued_clears: set[str] = set()
        self._cleared: set[str] = set()
        self._wakeup = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._inflight = False
        self._worker: asyncio.Task[None] | None = None
        self._unregister: list[Callable[[], Any]] = []
        self._closed = False

    def register_hooks(self, hooks: Any) -> Callable[[], None]:
        """Register canonical record/ack consumers and return an idempotent undo."""

        if not self.config.ready:
            if self.config.enabled and self.config.topic:
                logger.warning("attention push disabled: invalid ntfy destination config")
            return self.unregister_hooks
        for event, handler, name in (
            ("attention:recorded", self.handle_recorded, "tui-attention-push-recorded"),
            (
                "attention:acknowledged",
                self.handle_acknowledged,
                "tui-attention-push-acknowledged",
            ),
        ):
            unregister = hooks.register(event, handler, priority=110, name=name)
            if callable(unregister):
                self._unregister.append(unregister)
        return self.unregister_hooks

    def unregister_hooks(self) -> None:
        while self._unregister:
            unregister = self._unregister.pop()
            try:
                unregister()
            except Exception:  # noqa: BLE001 -- teardown must never cascade
                logger.debug("attention push hook unregister failed")

    async def handle_recorded(self, _event: str, data: dict[str, Any]) -> HookResult:
        event_id = data.get("event_id")
        if isinstance(event_id, str) and event_id:
            self._enqueue(
                _Delivery(
                    operation="publish",
                    event_id=event_id,
                    title=str(data.get("title") or "Amplifier"),
                    body=str(data.get("body") or "Ready for input"),
                )
            )
        return HookResult(action="continue")

    async def handle_acknowledged(self, _event: str, data: dict[str, Any]) -> HookResult:
        event_id = data.get("event_id")
        if data.get("acknowledged") is True and isinstance(event_id, str) and event_id:
            self._enqueue(_Delivery(operation="clear", event_id=event_id))
        return HookResult(action="continue")

    def _enqueue(self, delivery: _Delivery) -> None:
        if self._closed or not self.config.ready:
            return
        if delivery.operation == "clear":
            # A clear is terminal for this event. It supersedes queued
            # publishes and prevents any later replay from resurrecting the
            # notification. Distinct clears use their own coalesced lane, so
            # publish saturation can never discard acknowledgement state.
            self._cleared.add(delivery.event_id)
            self._pending = deque(
                item for item in self._pending if item.event_id != delivery.event_id
            )
            if delivery.event_id not in self._queued_clears:
                self._pending_clears.append(delivery.event_id)
                self._queued_clears.add(delivery.event_id)
        else:
            if delivery.event_id in self._cleared:
                return
            if len(self._pending) >= _QUEUE_LIMIT:
                # Push is advisory; never block the session or expose
                # message/topic data while reporting saturation.
                logger.warning("attention push queue full; publish dropped")
                return
            self._pending.append(delivery)
        self._drained.clear()
        self._wakeup.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="tui-attention-push")

    async def _run(self) -> None:
        while True:
            await self._wakeup.wait()
            while self._pending_clears or self._pending:
                if self._pending_clears:
                    event_id = self._pending_clears.popleft()
                    self._queued_clears.discard(event_id)
                    delivery = _Delivery(operation="clear", event_id=event_id)
                else:
                    delivery = self._pending.popleft()
                if not self._pending_clears and not self._pending:
                    self._wakeup.clear()
                self._inflight = True
                try:
                    await self._deliver(delivery)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- destination failures stay contained
                    # Never attach the exception: HTTP client errors commonly
                    # include the full request URL (and therefore the secret
                    # ntfy topic), while test transports may echo the body.
                    if self.config.debug:
                        logger.debug("attention push delivery failed")
                finally:
                    self._inflight = False
                    if not self._pending_clears and not self._pending:
                        self._drained.set()

    async def _deliver(self, delivery: _Delivery) -> None:
        sequence_id = ntfy_sequence_id(delivery.event_id)
        topic_url = f"{self.config.server}/{self.config.topic}"
        if delivery.operation == "publish":
            headers = {
                "Title": delivery.title,
                "Priority": self.config.priority,
                "X-Sequence-ID": sequence_id,
            }
            if self.config.tags:
                headers["Tags"] = ",".join(self.config.tags)
            status = await self._sender(
                "POST",
                topic_url,
                delivery.body.encode(),
                headers,
                self.config.timeout_s,
            )
        else:
            status = await self._sender(
                "PUT",
                f"{topic_url}/{sequence_id}/clear",
                b"",
                {},
                self.config.timeout_s,
            )
        if not 200 <= status < 300:
            # Status only: never retain or render the URL/topic/body.
            raise RuntimeError(f"ntfy returned HTTP {status}")

    async def drain(self, *, timeout_s: float = 2.0) -> None:
        """Wait for queued deliveries (test/controlled-shutdown seam)."""

        if self._pending_clears or self._pending or self._inflight:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout_s)

    async def cleanup(self) -> None:
        """Stop accepting events, bounded-flush the FIFO, and close HTTP state."""

        if self._closed:
            return
        self._closed = True
        self.unregister_hooks()
        if self._worker is not None:
            try:
                await self.drain(timeout_s=1.0)
            except TimeoutError:
                pass
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None


async def _send_http(
    method: str,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout_s: float,
) -> int:
    """Perform one HTTP request without a URL-logging client layer."""

    def send() -> int:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        # urllib does not log request URLs or bodies unless a caller installs
        # an explicit debug handler. Redirects are intentionally left at its
        # defaults for compatibility with self-hosted ntfy endpoints; errors
        # are swallowed by the content-free worker boundary above.
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return int(response.status)

    return await asyncio.to_thread(send)


__all__ = [
    "NtfyAttentionConfig",
    "NtfyAttentionDestination",
    "ntfy_sequence_id",
    "resolve_ntfy_attention_config",
]
