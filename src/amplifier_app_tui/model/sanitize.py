"""Broader export sanitization: filesystem-path + tool-IO redaction.

The host already scrubs *secret-shaped values* at every persistence sink
(:mod:`amplifier_app_tui.model.redaction` — AWS keys, bearer tokens, PEM
blocks, provider tokens). That is always on and never removed. This module
adds the *wider* sanitization the donor's ``export --sanitize`` applies, as an
opt-in export mode:

- **Path redaction** — user-identifying filesystem paths (home directories and
  the usernames embedded in them) are the classic "who/where am I" leak that a
  secret scrub never touches. :func:`redact_home_paths` rewrites the *username*
  segment of ``/Users/<u>/``, ``/home/<u>/`` and ``C:\\Users\\<u>\\`` to
  :data:`USER_PLACEHOLDER`, keeping the path *shape* (so the transcript still
  reads) while dropping the identity.
- **Tool-IO redaction** — tool inputs and outputs routinely carry file bodies,
  command output and arguments no share-safe artifact should ship.
  :func:`redact_tool_io` structurally blanks them to :data:`TOOL_IO_PLACEHOLDER`
  across both transcript message shapes (OpenAI ``tool_calls`` and Anthropic
  ``tool_use`` / ``tool_result`` content blocks).

Everything is a pure function over plain ``dict``/``list``/``str`` values
(stdlib only, ADR-0007 ``model/`` layer): no Textual, no amplifier-core, no
environment reads — the caller supplies any extra usernames. Composes over
:func:`amplifier_app_tui.model.redaction.scrub_text` so a sanitized value is
*also* secret-scrubbed. All operations are idempotent: the placeholders match
no rule and re-sanitizing already-sanitized data is a no-op.
"""

from __future__ import annotations

import re
from typing import Any

from .redaction import scrub_text

USER_PLACEHOLDER = "[user]"
"""Replaces the username segment of a home-directory path."""

TOOL_IO_PLACEHOLDER = "[redacted:tool-io]"
"""Replaces a redacted tool input/output (donor ``[redacted:tool-*]`` analog)."""

# Home-dir roots whose next path segment is a username: POSIX ``/Users/<u>/``
# and ``/home/<u>/`` plus Windows ``<drive>:\Users\<u>\``. The user segment
# stops at the next separator/space/quote so only the identity is rewritten;
# ``[user]`` itself re-matches to ``[user]`` (idempotent).
_HOME_PATH_RE = re.compile(r"(?P<root>/Users/|/home/|[A-Za-z]:\\Users\\)(?P<user>[^/\\\s\"']+)")


def redact_home_paths(text: str, *, users: tuple[str, ...] = ()) -> str:
    """Rewrite home-dir usernames in *text* to :data:`USER_PLACEHOLDER`.

    Structural: ``/Users/alice/src`` -> ``/Users/[user]/src`` (path shape kept,
    identity dropped). Any explicit *users* are additionally replaced
    whole-word, catching usernames that appear outside a path (emails, URLs).
    Idempotent.
    """
    out = _HOME_PATH_RE.sub(lambda m: m.group("root") + USER_PLACEHOLDER, text)
    for user in users:
        if user and user != USER_PLACEHOLDER:
            out = re.sub(rf"\b{re.escape(user)}\b", USER_PLACEHOLDER, out)
    return out


def sanitize_value(value: Any, *, users: tuple[str, ...] = ()) -> Any:
    """Recursively path-redact **and** secret-scrub every string leaf.

    The path-aware sibling of
    :func:`amplifier_app_tui.model.redaction.scrub_value`: walks
    dict/list/tuple containers and applies :func:`redact_home_paths` then
    :func:`~amplifier_app_tui.model.redaction.scrub_text` to each ``str``.
    Non-string leaves and dict keys pass through unchanged.
    """
    if isinstance(value, str):
        return scrub_text(redact_home_paths(value, users=users))
    if isinstance(value, dict):
        return {key: sanitize_value(item, users=users) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item, users=users) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(item, users=users) for item in value)
    return value


def _redact_call(call: Any) -> Any:
    """Blank a single OpenAI-style ``tool_calls`` entry's arguments."""
    if not isinstance(call, dict):
        return call
    redacted = dict(call)
    function = redacted.get("function")
    if isinstance(function, dict) and "arguments" in function:
        redacted["function"] = {**function, "arguments": TOOL_IO_PLACEHOLDER}
    for key in ("input", "arguments"):
        if key in redacted:
            redacted[key] = TOOL_IO_PLACEHOLDER
    return redacted


def _redact_block(block: Any) -> Any:
    """Blank an Anthropic-style ``tool_use`` input / ``tool_result`` content."""
    if not isinstance(block, dict):
        return block
    kind = block.get("type")
    if kind == "tool_use" and "input" in block:
        return {**block, "input": TOOL_IO_PLACEHOLDER}
    if kind == "tool_result" and "content" in block:
        return {**block, "content": TOOL_IO_PLACEHOLDER}
    return block


def redact_tool_io(message: Any) -> Any:
    """Return *message* with every tool input/output blanked (structural).

    Covers both transcript shapes: OpenAI assistant ``tool_calls`` +
    ``role=="tool"`` output messages, and Anthropic ``tool_use`` / ``tool_result``
    content blocks. Free assistant/user prose is untouched — only tool traffic
    is blanked. Returns a new object; the input is not mutated.
    """
    if not isinstance(message, dict):
        return message
    redacted = dict(message)
    if isinstance(redacted.get("tool_calls"), list):
        redacted["tool_calls"] = [_redact_call(call) for call in redacted["tool_calls"]]
    content = redacted.get("content")
    if isinstance(content, list):
        redacted["content"] = [_redact_block(block) for block in content]
    if redacted.get("role") == "tool" and isinstance(redacted.get("content"), str):
        redacted["content"] = TOOL_IO_PLACEHOLDER
    return redacted


# Stable private alias so ``sanitize_transcript``'s boolean parameter can share
# the public name ``redact_tool_io`` without shadowing the function.
_redact_tool_io_message = redact_tool_io


def sanitize_transcript(
    messages: list[Any],
    *,
    redact_tool_io: bool = False,
    users: tuple[str, ...] = (),
) -> list[Any]:
    """Sanitize a list of transcript messages.

    When *redact_tool_io* is set, tool inputs/outputs are structurally blanked
    first; then path + secret redaction runs over every remaining string leaf.
    """
    out: list[Any] = []
    for message in messages:
        staged = _redact_tool_io_message(message) if redact_tool_io else message
        out.append(sanitize_value(staged, users=users))
    return out


def sanitize_metadata(metadata: dict[str, Any], *, users: tuple[str, ...] = ()) -> dict[str, Any]:
    """Path-redact + secret-scrub a session metadata dict (e.g. ``working_dir``)."""
    result = sanitize_value(metadata, users=users)
    return result if isinstance(result, dict) else dict(metadata)


__all__ = [
    "TOOL_IO_PLACEHOLDER",
    "USER_PLACEHOLDER",
    "redact_home_paths",
    "redact_tool_io",
    "sanitize_metadata",
    "sanitize_transcript",
    "sanitize_value",
]
