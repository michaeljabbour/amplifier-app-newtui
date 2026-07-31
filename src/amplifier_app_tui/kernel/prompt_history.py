"""Per-project persistent prompt history (cross-session ↑ recall).

The composer keeps an in-memory ``↑`` ring for the *current* session
(``ui/composer.py``). That ring was seeded only from a resumed session's
transcript, so a **fresh** session in a directory with prior sessions
recalled nothing — the bug this module fixes.

Persistence mirrors amplifier-app-cli's behavior exactly: submitted
prompts land in a per-working-directory history file

    ~/.amplifier/projects/<project-slug>/repl_history

keyed the same way session storage is (``kernel/persistence.py``,
``get_project_slug``), so tui and app-cli **share** one history file
per directory. The on-disk format is prompt-toolkit's ``FileHistory``
(a ``# <timestamp>`` comment line then one ``+<line>`` per prompt line),
reproduced here without importing prompt-toolkit — tui does not depend
on it — so an entry written by either app reads back identically.

ADR-0007: this store is pure/stdlib (plus ``model.redaction`` and
``get_project_slug``) and touches no amplifier-core; the adapter seam
(``ui/runtime_adapter.py``) owns *when* it is read/written so ``ui/`` and
``model/`` stay core-free.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..model.redaction import scrub_text
from .config import get_project_slug
from .frecency import RankedPrompt, rank_history

logger = logging.getLogger(__name__)

HISTORY_FILENAME = "repl_history"
"""Per-project prompt-history file — the name amplifier-app-cli uses so
the two apps share one history per working directory."""

MAX_PROMPT_HISTORY_ENTRIES = 500
"""Cap on stored prompts (matches the composer's in-memory ring). When an
append pushes past it the file is rewritten to the most-recent slice."""


def parse_history(text: str) -> list[str]:
    """Parse prompt-toolkit ``FileHistory`` text into oldest-first prompts.

    Content lines start with ``+`` (the marker stripped, joined by
    ``\\n``); any other line (the ``# timestamp`` comment, a blank line)
    ends the current entry. Byte-compatible with prompt-toolkit so an
    app-cli-written file reads back verbatim.
    """
    strings: list[str] = []
    lines: list[str] = []

    def flush() -> None:
        if lines:
            joined = "".join(lines)
            strings.append(joined[:-1] if joined.endswith("\n") else joined)

    for raw in text.splitlines(keepends=True):
        if raw.startswith("+"):
            lines.append(raw[1:])
        else:
            flush()
            lines = []
    flush()
    return strings


def format_entry(prompt: str) -> str:
    """Render one prompt as a prompt-toolkit ``FileHistory`` record."""
    body = "".join(f"+{line}\n" for line in prompt.split("\n"))
    return f"\n# {datetime.now()}\n{body}"


def _dedup_consecutive(prompts: list[str]) -> list[str]:
    """Drop a prompt equal to the one immediately before it (composer parity)."""
    deduped: list[str] = []
    for prompt in prompts:
        if deduped and deduped[-1] == prompt:
            continue
        deduped.append(prompt)
    return deduped


class PromptHistoryStore:
    """Filesystem store for one project's submitted prompts.

    Contract:
    - Inputs: prompt strings (secret-shaped substrings are scrubbed at the
      sink via ``model.redaction``, matching every other persistence sink).
    - Side effects: reads/writes ``<project>/repl_history``.
    - Errors: swallowed and logged — prompt history is best-effort and must
      never break a submit or a boot.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        project_dir: Path | None = None,
        max_entries: int = MAX_PROMPT_HISTORY_ENTRIES,
    ) -> None:
        if path is None:
            path = (
                Path.home()
                / ".amplifier"
                / "projects"
                / get_project_slug(project_dir)
                / HISTORY_FILENAME
            )
        self.path = path
        self.max_entries = max(1, max_entries)

    # -- read --------------------------------------------------------------

    def load(self, *, limit: int | None = None) -> list[str]:
        """Oldest-first prompts (newest last), consecutive-deduped and capped.

        Newest-last matches the composer's ring so ``↑`` walks
        most-recent-first. ``limit`` defaults to :attr:`max_entries` so a
        large shared file never floods the seed.
        """
        if limit is None:
            limit = self.max_entries
        entries = _dedup_consecutive(self._read_entries())
        return entries[-limit:] if limit >= 0 else entries

    def ranked_history(
        self,
        prefix: str = "",
        *,
        limit: int | None = None,
    ) -> list[RankedPrompt]:
        """Frecency-ranked recall over this project's prompt history.

        Ranks the deduped, recency-ordered store (:meth:`load`) by
        ``frequency / (1 + age)`` -- a prompt used often *and* recently
        beats one used once, even a more recent once (see :mod:`.frecency`
        and ``.ai/oc_donor.md``). This is the query surface an autocomplete
        UI ranks by; it does **not** change the composer's chronological
        up-ring (:meth:`load`), which stays the default walk the client
        lane builds on.

        Args:
            prefix: Literal case-sensitive ``startswith`` filter; ``\"\"``
                matches all.
            limit: Cap on results (``None`` = all, ``<= 0`` = none).
        """
        return rank_history(self.load(), prefix=prefix, limit=limit)

    # -- write -------------------------------------------------------------

    def append(self, prompt: str) -> bool:
        """Persist *prompt*; return whether it was recorded.

        Empty/whitespace-only prompts and immediate consecutive duplicates
        are skipped (composer parity). Secret-shaped substrings are scrubbed
        before anything hits disk. The file is trimmed to :attr:`max_entries`.
        """
        cleaned = scrub_text(prompt).strip()
        if not cleaned:
            return False
        try:
            entries = self._read_entries()
            if entries and entries[-1] == cleaned:
                return False
            entries.append(cleaned)
            if len(entries) > self.max_entries:
                self._write_all(entries[-self.max_entries :])
            else:
                self._append_one(cleaned)
            return True
        except OSError:
            logger.warning("Failed to persist prompt history to %s", self.path, exc_info=True)
            return False

    # -- internals ---------------------------------------------------------

    def _read_entries(self) -> list[str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Failed to read prompt history from %s", self.path, exc_info=True)
            return []
        return parse_history(text)

    def _append_one(self, prompt: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(format_entry(prompt))

    def _write_all(self, prompts: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(format_entry(prompt) for prompt in prompts)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(self.path)


__all__ = [
    "HISTORY_FILENAME",
    "MAX_PROMPT_HISTORY_ENTRIES",
    "PromptHistoryStore",
    "format_entry",
    "parse_history",
]
