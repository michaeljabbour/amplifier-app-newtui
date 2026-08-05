"""Make a prepared bundle's instruction and context visible in a live session.

Foundation installs a bundle's system-prompt factory while creating a session.
Loading an additive bundle after that one-shot initialization therefore needs a
small app-layer bridge: render the *prepared* bundle with the same factory and
append the result to the already-mounted context.

The pinned Foundation release exposes the renderer as
``PreparedBundle._create_system_prompt_factory``.  This module prefers a future
public ``create_system_prompt_factory`` when one exists, feature-detects the
private compatibility seam otherwise, and fails honestly if neither is
available.  No Foundation imports or context internals are used.

``metadata.source`` is deliberately ``"hook"``.  context-simple replaces
ordinary stored system messages whenever its root system-prompt factory is
active, but explicitly preserves hook-origin system messages.  The additional
metadata gives cleanup a precise identity without changing that contract.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LIVE_BUNDLE_CONTENT_KIND = "amplifier-tui-live-bundle"
"""Metadata kind used to identify content added by this bridge."""


@dataclass(frozen=True)
class BundleContentActivation:
    """Outcome of activating one prepared bundle's behavioral content."""

    ok: bool
    added: bool
    rendered: str = ""
    reason: str = ""
    cleanup: Callable[[], Coroutine[Any, Any, None]] | None = None


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _factory_builder(prepared: Any) -> Callable[..., Any] | None:
    """Return the supported renderer builder, preferring a public API."""
    public = getattr(prepared, "create_system_prompt_factory", None)
    if callable(public):
        return public
    compatibility = getattr(prepared, "_create_system_prompt_factory", None)
    return compatibility if callable(compatibility) else None


def _accepts_session_cwd(builder: Callable[..., Any]) -> bool:
    """Whether *builder* advertises Foundation's ``session_cwd`` argument."""
    try:
        parameters = inspect.signature(builder).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "session_cwd" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _bundle_name(prepared: Any) -> str:
    bundle = getattr(prepared, "bundle", None)
    name = getattr(bundle, "name", None)
    return str(name).strip() if name else "unnamed"


def _is_activation(message: Any, activation_id: str) -> bool:
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata")
    return (
        isinstance(metadata, dict)
        and metadata.get("source") == "hook"
        and metadata.get("kind") == LIVE_BUNDLE_CONTENT_KIND
        and metadata.get("activation_id") == activation_id
    )


def _cleanup_for(
    context: Any, activation_id: str
) -> Callable[[], Coroutine[Any, Any, None]] | None:
    get_messages = getattr(context, "get_messages", None)
    set_messages = getattr(context, "set_messages", None)
    if not callable(get_messages) or not callable(set_messages):
        return None

    async def cleanup() -> None:
        """Remove only the message created by this activation, if still present."""
        try:
            current = list(await _await_if_needed(get_messages()))
            remaining = [
                message for message in current if not _is_activation(message, activation_id)
            ]
            if len(remaining) != len(current):
                await _await_if_needed(set_messages(remaining))
        except Exception:  # noqa: BLE001 - cleanup must not mask session teardown
            logger.warning(
                "Could not remove live bundle content activation %s",
                activation_id,
                exc_info=True,
            )

    return cleanup


async def activate_bundle_content(
    prepared: Any,
    coordinator: Any,
    session: Any,
    project_dir: str | Path,
) -> BundleContentActivation:
    """Render and add one prepared bundle instruction/context system message.

    The renderer is obtained from the prepared object so context files,
    ``@mentions``, namespace resolution, and Foundation's formatting semantics
    stay identical to normal session creation.  The live root factory itself is
    not replaced; the rendered overlay is an additive hook-origin message.

    Returns a successful no-op when the rendered bundle has no behavioral
    content.  All unsupported or failed surfaces return ``ok=False`` with a
    concrete reason and never claim that content entered model context.
    """
    bundle = getattr(prepared, "bundle", None)
    if bundle is None:
        return BundleContentActivation(
            False, False, reason="prepared bundle exposes no source bundle"
        )

    builder = _factory_builder(prepared)
    if builder is None:
        return BundleContentActivation(
            False,
            False,
            reason="prepared bundle exposes no compatible system-prompt factory",
        )

    getter = getattr(coordinator, "get", None)
    context = getter("context") if callable(getter) else None
    add_message = getattr(context, "add_message", None)
    if not callable(add_message):
        return BundleContentActivation(False, False, reason="live context cannot accept messages")

    try:
        kwargs: dict[str, Any] = {}
        if _accepts_session_cwd(builder):
            kwargs["session_cwd"] = Path(project_dir)
        factory = await _await_if_needed(builder(bundle, session, **kwargs))
        if not callable(factory):
            return BundleContentActivation(
                False,
                False,
                reason="system-prompt factory builder returned no renderer",
            )
        rendered_value = await _await_if_needed(factory())
        rendered = str(rendered_value or "")
    except Exception as error:  # noqa: BLE001 - convert optional integration failure
        return BundleContentActivation(
            False, False, reason=f"could not render bundle content: {error}"
        )

    if not rendered.strip():
        return BundleContentActivation(
            True,
            False,
            rendered=rendered,
            reason="bundle has no instruction or context content",
        )

    activation_id = uuid.uuid4().hex
    message = {
        "role": "system",
        "content": rendered,
        "metadata": {
            "source": "hook",
            "kind": LIVE_BUNDLE_CONTENT_KIND,
            "bundle": _bundle_name(prepared),
            "activation_id": activation_id,
        },
    }
    try:
        await _await_if_needed(add_message(message))
    except Exception as error:  # noqa: BLE001 - report partial activation honestly
        return BundleContentActivation(
            False,
            False,
            rendered=rendered,
            reason=f"could not add bundle content to live context: {error}",
        )

    cleanup = _cleanup_for(context, activation_id)
    reason = "" if cleanup is not None else "live context does not support message cleanup"
    return BundleContentActivation(
        True,
        True,
        rendered=rendered,
        reason=reason,
        cleanup=cleanup,
    )


__all__ = [
    "LIVE_BUNDLE_CONTENT_KIND",
    "BundleContentActivation",
    "activate_bundle_content",
]
