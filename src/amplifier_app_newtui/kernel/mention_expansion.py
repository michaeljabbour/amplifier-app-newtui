"""Runtime-path ``@mention`` expansion (issue #48 gene-transfer from app-cli).

Decision -- **expand**, not autocomplete-only. Composer ``@file`` autocomplete
(``kernel/file_mentions.py``) only helps the user *type* a token; the model
never sees the referenced content. amplifier-app-cli has always inlined mention
content in its runtime path, so this closes a real capability gap and restores
cli parity. We re-express it the amplifier-native way: reuse
``amplifier_foundation.mentions`` primitives directly rather than vendoring the
app-cli construct.

Donor construct (READ-ONLY reference, never imported):
  - ``amplifier_app_cli/main.py:_process_runtime_mentions`` -- the submit-time
    hook that reads the ``mention_resolver`` capability and calls foundation's
    ``expand_mentions_in_instruction`` before ``session.execute``.
  - ``amplifier_app_cli/lib/mention_loading/app_resolver.py:AppMentionResolver``
    -- app *policy* (``@user:``/``@project:``/``@~/`` shortcuts, ``..`` guard).
  - Mechanism lives in foundation: ``amplifier_foundation.mentions``
    (``expand_mentions_in_instruction``, ``BaseMentionResolver``,
    ``ContentDeduplicator``, ``parse_mentions``).

Behavioral contract inherited from foundation (documented so callers can rely
on it without re-reading the library):
  - Parse: ``@``-prefixed tokens ``[A-Za-z0-9_:./~-]+``; e-mail addresses,
    inline code and fenced code blocks are excluded.
  - Resolve: ``@bundle:name`` -> bundle context path; ``@path`` -> base_path/path
    (with a ``.md`` fallback -- this is how *agent* mentions resolve to an agent
    definition file); ``@~/path`` -> home. Missing paths resolve to ``None`` and
    are silently skipped (opportunistic).
  - Load: files are read; directories become a bounded listing; content is
    de-duplicated by SHA-256; nested mentions are followed to depth 3.
  - Expand: resolved content is prepended as ``<context_file paths="...">`` XML
    blocks; the original text (and its ``@mentions``) is preserved verbatim as
    semantic references. Text with no resolvable mentions is returned unchanged.

newtui-native policy this module ADDS over the donor: **size bounds**. The
foundation loader has no ceiling -- an ``@node_modules`` or a multi-megabyte log
would be inlined whole. A stateful :class:`_BoundingResolver` stats each
resolved path *before* the loader reads it and drops any mention that would
breach the per-file cap, the cumulative byte budget, or the file count, so
foundation's expansion runs unchanged while the injected context stays bounded
(cf. the 32-item/32KB SteeringQueue bound, ADR-0007 Steering).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from amplifier_foundation.mentions import (
    ContentDeduplicator,
    MentionResolverProtocol,
    expand_mentions_in_instruction,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_FILE_BYTES = 256 * 1024
"""Largest single file inlined verbatim (256 KiB). Bigger files are skipped."""

DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024
"""Cumulative expansion budget across all mentions in one turn (1 MiB)."""

DEFAULT_MAX_FILES = 32
"""Most files inlined in one turn (matches the SteeringQueue item bound)."""


@dataclass(frozen=True)
class MentionBudget:
    """Per-turn ceilings for ``@mention`` expansion (newtui app policy)."""

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_files: int = DEFAULT_MAX_FILES


@dataclass(frozen=True)
class MentionExpansion:
    """Result of one expansion pass.

    ``text`` is what the model should receive (the original when nothing
    resolved). ``included`` are the resolved file paths that made it into the
    context; ``skipped`` pairs each dropped mention with a reason
    (``too-large`` / ``file-limit`` / ``budget``) for a UI notice + tests.
    """

    text: str
    included: tuple[Path, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def expanded(self) -> bool:
        return bool(self.included)


@dataclass
class _BudgetTracker:
    budget: MentionBudget
    total_bytes: int = 0
    file_count: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)


class _BoundingResolver:
    """Wrap a resolver so oversized / over-budget mentions resolve to ``None``.

    The wrapped resolver does the real work (bundle namespaces, ``.md``
    fallback, path resolution); this layer only enforces the byte/count budget
    at resolve time -- before foundation's loader reads the file -- so the
    unchanged ``expand_mentions_in_instruction`` keeps recursion and dedup while
    never inlining more than the budget allows. Structurally satisfies
    ``MentionResolverProtocol`` (only ``resolve`` is required).
    """

    def __init__(self, inner: MentionResolverProtocol, tracker: _BudgetTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    def resolve(self, mention: str) -> Path | None:
        path = self._inner.resolve(mention)
        if path is None:
            return None
        try:
            is_file = path.is_file()
        except OSError:
            return None
        if not is_file:
            # Directories become foundation's immediate-children listing
            # (inherently small); non-existent paths fall through to the
            # loader's opportunistic skip. Neither counts against the budget.
            return path
        try:
            size = path.stat().st_size
        except OSError:
            return None
        tracker = self._tracker
        budget = tracker.budget
        if size > budget.max_file_bytes:
            tracker.skipped.append((mention, "too-large"))
            logger.debug("mention %s skipped: %d B over per-file cap", mention, size)
            return None
        if tracker.file_count >= budget.max_files:
            tracker.skipped.append((mention, "file-limit"))
            return None
        if tracker.total_bytes + size > budget.max_total_bytes:
            tracker.skipped.append((mention, "budget"))
            return None
        tracker.total_bytes += size
        tracker.file_count += 1
        return path


async def expand_mentions(
    text: str,
    *,
    resolver: MentionResolverProtocol | None,
    relative_to: Path | None = None,
    budget: MentionBudget | None = None,
) -> MentionExpansion:
    """Expand ``@mentions`` in *text* under a per-turn size budget.

    Returns the original text unchanged when it is empty, has no resolver, or
    resolves nothing. A fresh :class:`ContentDeduplicator` is used per call so
    every turn expands its own mentions deterministically (rather than silently
    suppressing a file that an earlier turn already sent).
    """
    if not text or resolver is None:
        return MentionExpansion(text)
    tracker = _BudgetTracker(budget or MentionBudget())
    bounding = _BoundingResolver(resolver, tracker)
    deduplicator = ContentDeduplicator()
    expanded = await expand_mentions_in_instruction(
        text,
        resolver=bounding,
        deduplicator=deduplicator,
        relative_to=relative_to,
    )
    included = tuple(
        path for context_file in deduplicator.get_unique_files() for path in context_file.paths
    )
    return MentionExpansion(
        text=expanded,
        included=included,
        skipped=tuple(tracker.skipped),
    )


__all__ = [
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "MentionBudget",
    "MentionExpansion",
    "expand_mentions",
]
