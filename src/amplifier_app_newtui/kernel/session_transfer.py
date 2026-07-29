"""Portable session export / import — the round-tripping counterpart to /export.

The in-app ``/export`` renders a *human-readable markdown* transcript: great for
reading/sharing, but LOSSY — markdown cannot be turned back into a session. This
module adds the donor's *structured* session transfer: a JSON artifact carrying
the two files that actually persist a session (``transcript`` + ``metadata``) so
an exported session can be brought back as a viewable/resumable session.

Export optionally SANITIZES via :mod:`amplifier_app_newtui.model.sanitize`
(user filesystem paths, and — opt-in — tool inputs/outputs) on top of the
always-on secret scrub the store applies at rest. Import mints a NEW top-level
session id (safe, never clobbers an existing session) and records origin
provenance, then writes it through the normal :class:`SessionStore` so it lists
and resumes like any native session.

Layering (ADR-0007): kernel reads/writes the store; sanitization is a pure
``model/`` function. No Textual, no amplifier-core.

Honest round-trip:

- **Full export -> import** reconstructs transcript + metadata (new id +
  lineage) -> resumable with real content.
- **Sanitized export -> import** reconstructs roles/turns/structure; redacted
  content stays as placeholders (the real secrets/paths/tool-IO are gone by
  design — a share-safe artifact, not a content backup).
- **Not carried:** ``ui-events.jsonl`` (cost/telemetry re-seed) and live
  provider/model bindings — a resumed import re-mounts the bundle; an unknown
  bundle falls back to the default.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..model.sanitize import sanitize_metadata, sanitize_transcript
from .persistence import SessionStore

SCHEMA = "amplifier-newtui/session-export/v1"
"""Current portable-export schema id. Import accepts any ``.../session-export/*``."""

_SCHEMA_PREFIX = "amplifier-newtui/session-export/"

MAX_NAME_LENGTH = 50
"""Clamp an imported session's name (matches session_manager's rename clamp)."""


class SessionTransferError(ValueError):
    """Raised when an import payload is malformed or its schema unsupported."""


def export_session(
    store: SessionStore,
    session_id: str,
    *,
    sanitize: bool = False,
    redact_tool_io: bool = False,
    users: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a portable export dict for a stored session.

    Loads ``(transcript, metadata)`` from *store* (raising ``FileNotFoundError``
    for a missing session). When *sanitize* (implied by *redact_tool_io*), user
    paths are redacted across metadata + transcript and — if *redact_tool_io* —
    tool inputs/outputs are structurally blanked. *users* are extra usernames to
    redact whole-word (the CLI supplies the current account name).
    """
    transcript, metadata = store.load(session_id)
    if redact_tool_io:
        sanitize = True
    if sanitize:
        metadata = sanitize_metadata(metadata, users=users)
        transcript = sanitize_transcript(transcript, redact_tool_io=redact_tool_io, users=users)
    return {
        "schema": SCHEMA,
        "exported_at": datetime.now(UTC).isoformat(),
        "sanitized": sanitize,
        "tool_io_redacted": redact_tool_io,
        "session_id": session_id,
        "metadata": metadata,
        "transcript": transcript,
    }


def read_export_file(path: str | Path) -> dict[str, Any]:
    """Read + JSON-parse an export artifact, with friendly errors.

    Raises :class:`SessionTransferError` (never a bare ``OSError`` /
    ``JSONDecodeError``) so the CLI can render one clean message.
    """
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SessionTransferError(f"file not found: {file_path}") from None
    except OSError as error:
        raise SessionTransferError(f"could not read {file_path}: {error}") from error
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise SessionTransferError(f"invalid JSON in {file_path}: {error}") from error


def import_session(
    store: SessionStore,
    payload: Any,
    *,
    name: str | None = None,
    new_id: str | None = None,
) -> str:
    """Write an export *payload* into *store* as a NEW session; return its id.

    Validates the envelope (schema + ``transcript`` list), mints a fresh
    top-level uuid-hex id (unless *new_id* is given), stamps provenance
    (``imported_from`` / ``imported_at`` / ``source_schema`` / ``sanitized``)
    into the metadata, and persists via :meth:`SessionStore.save` (which applies
    its own write-time secret scrub — defense in depth). The session then lists
    and resumes like any native session.
    """
    if not isinstance(payload, dict):
        raise SessionTransferError("export payload must be a JSON object")
    schema = payload.get("schema")
    if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
        raise SessionTransferError(f"unrecognized export schema: {schema!r}")
    transcript = payload.get("transcript")
    if not isinstance(transcript, list):
        raise SessionTransferError("export payload missing a 'transcript' list")
    source_meta = payload.get("metadata")
    if not isinstance(source_meta, dict):
        source_meta = {}

    session_id = new_id or uuid.uuid4().hex
    origin = payload.get("session_id")
    metadata: dict[str, Any] = {
        **source_meta,
        "session_id": session_id,
        "imported_at": datetime.now(UTC).isoformat(),
        "imported_from": origin if isinstance(origin, str) else "",
        "source_schema": schema,
        "sanitized": bool(payload.get("sanitized")),
    }
    if name:
        clamped = name.strip()[:MAX_NAME_LENGTH]
        if clamped:
            metadata["name"] = clamped

    store.save(session_id, list(transcript), metadata)
    return session_id


def dumps(payload: dict[str, Any]) -> str:
    """Serialize an export payload to pretty JSON (stable, UTF-8, str fallback)."""
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


__all__ = [
    "MAX_NAME_LENGTH",
    "SCHEMA",
    "SessionTransferError",
    "dumps",
    "export_session",
    "import_session",
    "read_export_file",
]
