"""Prompt-stash: save/restore in-progress draft prompts (HGT from opencode).

Re-expression of the opencode ``prompt/stash.tsx`` + ``dialog-stash.tsx``
*behavioral contract* (see ``.ai/oc_donor.md``) — written from scratch, no
donor source copied. The capability: a user mid-typing stashes the draft
(the composer clears) and later recalls it — the most recent (LIFO ``pop``)
or a specific one picked from the list.

ADR-0007: this module is PURE (stdlib + pydantic + ``model.blocks.Segment``).
It imports neither Textual nor amplifier-core, so the store, the JSONL serde
and the list renderer are all unit-/golden-testable in isolation. The *when*
of stash/recall (keybind, command, composer clear) lives in ``ui/``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from .blocks import Segment

MAX_STASH_ENTRIES = 50
"""Bound on the stash (donor ``MAX_STASH_ENTRIES``); newest kept on overflow."""

STASH_PREVIEW_LIMIT = 50
"""List preview: first line truncated to 50 chars (donor ``getStashPreview``)."""


class StashEntry(BaseModel):
    """One stashed draft: the raw text plus the wall-clock it was stashed at.

    ``stamped_at`` is fractional epoch seconds (``time.time()``) — the
    donor's ``timestamp`` — driving the relative-age label. Kept at ``0.0``
    in pure tests so age rendering is deterministic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    stamped_at: float = 0.0


class PromptStash:
    """A bounded LIFO stack of draft prompts (donor ``usePromptStash``).

    Storage order is oldest → newest (``entries``); ``pop`` removes the most
    recent; ``recall`` addresses an entry by its 1-based **newest-first**
    display index (the order ``/stashes`` lists them). Over-cap pushes drop
    the oldest, matching the donor's ``slice(-MAX_STASH_ENTRIES)``.
    """

    def __init__(
        self,
        entries: Iterable[StashEntry] = (),
        *,
        max_entries: int = MAX_STASH_ENTRIES,
    ) -> None:
        self._entries: list[StashEntry] = list(entries)
        self._max = max(1, max_entries)
        self._trim()

    @property
    def entries(self) -> tuple[StashEntry, ...]:
        """All entries, oldest → newest (donor ``list()`` order)."""
        return tuple(self._entries)

    @property
    def count(self) -> int:
        return len(self._entries)

    @property
    def is_empty(self) -> bool:
        return not self._entries

    def push(self, text: str, *, now: float = 0.0) -> StashEntry | None:
        """Stash *text* (donor ``push``); ``None`` for an empty/blank draft.

        The donor's stash command is enabled only when the composer input is
        non-empty, so a blank draft is never stashed.
        """
        if not text.strip():
            return None
        entry = StashEntry(text=text, stamped_at=now)
        self._entries.append(entry)
        self._trim()
        return entry

    def pop(self) -> StashEntry | None:
        """Remove and return the most-recent entry — LIFO (donor ``pop``)."""
        if not self._entries:
            return None
        return self._entries.pop()

    def recall(self, display_index: int) -> StashEntry | None:
        """Remove and return the entry at *display_index* (1-based, newest-first).

        ``1`` is the most recent (same as ``pop``); out-of-range → ``None``.
        """
        storage_index = self._storage_index(display_index)
        if storage_index is None:
            return None
        return self._entries.pop(storage_index)

    def _storage_index(self, display_index: int) -> int | None:
        if display_index < 1 or display_index > len(self._entries):
            return None
        return len(self._entries) - display_index

    def _trim(self) -> None:
        overflow = len(self._entries) - self._max
        if overflow > 0:
            del self._entries[:overflow]


# -- JSONL serde (donor disk format; pure, ready for a kernel store) ----------


def parse_stash_jsonl(text: str) -> list[StashEntry]:
    """Parse the donor's ``prompt-stash.jsonl`` (one JSON entry per line).

    Malformed lines are dropped (donor parity: ``JSON.parse`` in a try),
    and the result is capped to the most-recent ``MAX_STASH_ENTRIES``.
    """
    parsed: list[StashEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        raw = data.get("text")
        if not isinstance(raw, str):
            continue
        stamp = data.get("stamped_at", 0.0)
        stamped_at = float(stamp) if isinstance(stamp, (int, float)) else 0.0
        parsed.append(StashEntry(text=raw, stamped_at=stamped_at))
    return parsed[-MAX_STASH_ENTRIES:]


def serialize_stash(entries: Iterable[StashEntry]) -> str:
    """Render entries as JSONL (empty string for no entries — donor parity)."""
    lines = [json.dumps({"text": e.text, "stamped_at": e.stamped_at}) for e in entries]
    return "\n".join(lines) + "\n" if lines else ""


# -- pure renderers -----------------------------------------------------------


def stash_preview(text: str, limit: int = STASH_PREVIEW_LIMIT) -> str:
    """First line, trimmed, truncated to *limit* chars (donor ``getStashPreview``)."""
    first_line = text.split("\n", 1)[0].strip()
    if len(first_line) <= limit:
        return first_line
    return first_line[: max(0, limit - 1)] + "…"


def format_relative_age(delta_seconds: float) -> str:
    """Donor ``getRelativeTime`` ladder (``just now`` / ``Nm`` / ``Nh`` / ``Nd``).

    The donor shows an absolute datetime beyond a week; we keep the pure
    function deterministic with ``Nd ago`` (in-session ages are ``just now``).
    """
    seconds = int(max(0.0, delta_seconds))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def stash_list_spans(entries: tuple[StashEntry, ...], *, now: float) -> tuple[Segment, ...]:
    """Styled ``Answer`` spans for ``/stashes`` — newest first (donor ``.toReversed``).

    Each row: ``N. <preview>  ·  <age>[  ·  ~K lines]``. Pure: ``now`` is
    injected so the golden is stable.
    """
    if not entries:
        return (Segment(text="  no stashed drafts · nothing to restore\n", style_token="dimmer"),)
    spans: list[Segment] = [
        Segment(text="Stashed drafts", style_token="bright", bold=True),
        Segment(
            text=f"  ·  {len(entries)} saved · /unstash <n> restores one\n",
            style_token="dim",
        ),
    ]
    for number, entry in enumerate(reversed(entries), start=1):
        age = format_relative_age(now - entry.stamped_at)
        line_count = entry.text.count("\n") + 1
        multiline = f"  ·  ~{line_count} lines" if line_count > 1 else ""
        spans.append(Segment(text=f"  {number}. ", style_token="dim"))
        spans.append(Segment(text=stash_preview(entry.text), style_token="teal"))
        spans.append(Segment(text=f"  ·  {age}{multiline}\n", style_token="dim"))
    return tuple(spans)


def render_stash_list(entries: tuple[StashEntry, ...], *, now: float) -> str:
    """Plain-text join of :func:`stash_list_spans` (the golden surface)."""
    return "".join(span.text for span in stash_list_spans(entries, now=now))


__all__ = [
    "MAX_STASH_ENTRIES",
    "STASH_PREVIEW_LIMIT",
    "PromptStash",
    "StashEntry",
    "format_relative_age",
    "parse_stash_jsonl",
    "render_stash_list",
    "serialize_stash",
    "stash_list_spans",
    "stash_preview",
]
