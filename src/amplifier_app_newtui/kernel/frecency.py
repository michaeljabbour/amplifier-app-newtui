"""Frecency scoring over the prompt-history store (pure, stdlib-only).

Frecency = **frequency x recency**: a prompt used often *and* recently
outranks one used once, even a more recent once. Grafted from opencode's
file-picker frecency gene (``packages/tui/src/prompt/frecency.tsx``) and
applied to the *prompt* history the donor only ever kept chronologically
(``history.tsx``). See ``.ai/oc_donor.md`` for the donor study.

Donor curve (verbatim shape)::

    score = frequency / (1 + age)

The donor measures ``age`` in days from a per-entry ``lastOpen`` wall-clock.
Our host store (``kernel/prompt_history.py``) stores prompt strings
oldest-first and **discards** per-entry timestamps (prompt-toolkit
``FileHistory`` ``# <ts>`` lines are dropped on parse, and the file is shared
with app-cli so those stamps are unreliable). So recency is derived from
**position rank** instead: the newest entry has ``age = 0`` and each older
step adds 1 -- the deterministic, pure, timestamp-free analog of ``lastOpen``
that preserves the identical hyperbola while staying unit-testable.

ADR-0007: ``kernel/`` here is pure/stdlib -- this module imports nothing but
the standard library and is trivially testable in isolation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

__all__ = ["RankedPrompt", "frecency_score", "rank_history", "suggest_completion"]


@dataclass(frozen=True)
class RankedPrompt:
    """One ranked prompt: its frecency score and the inputs that produced it.

    - ``text``: the prompt string.
    - ``score``: ``frequency / (1 + age)`` (the donor curve).
    - ``frequency``: how many times the prompt occurs in the store.
    - ``age``: rank distance of its most-recent occurrence from the newest
      entry (``0`` = newest); the timestamp-free recency term.
    - ``last_index``: index of that most-recent occurrence in the source list.
    """

    text: str
    score: float
    frequency: int
    age: int
    last_index: int


def frecency_score(frequency: int, age: int) -> float:
    """The donor frecency curve: ``frequency / (1 + age)``.

    ``frequency`` is a lifetime occurrence count (weight 1, linear numerator);
    ``age`` is the recency term (``0`` = most recent). A larger ``age`` divides
    the score down hyperbolically -- never bucketed, never an exponential
    half-life. A negative ``age`` is clamped to ``0`` (defensive; the ranker
    never produces negatives).
    """
    if age < 0:
        age = 0
    return frequency / (1 + age)


def rank_history(
    entries: list[str],
    *,
    prefix: str = "",
    limit: int | None = None,
) -> list[RankedPrompt]:
    """Rank *entries* by frecency, best first.

    Args:
        entries: History prompts **oldest-first / newest-last** -- exactly the
            order ``PromptHistoryStore.load()`` returns (deduped, recency
            ordered). Frequency is counted over this list, so a non-consecutive
            repeat contributes to frequency; a consecutive dup was already
            folded to one at the store (composer parity).
        prefix: Literal ``str.startswith`` filter, case-sensitive. Empty ``""``
            matches every entry. (The donor has no prompt-prefix semantics --
            this is the one client-revisitable knob; a caller wanting
            case-insensitive recall lowercases both sides before querying.)
        limit: Cap on results. ``None`` returns all; ``<= 0`` returns none.

    Returns:
        ``RankedPrompt`` list sorted by: score **desc**, then age **asc**
        (more-recent wins ties -- mirrors the donor ``parseFrecency``
        most-recent-first sort), then text **asc** (stable final key).
    """
    if limit is not None and limit <= 0:
        return []

    total = len(entries)
    frequency = Counter(entries)
    # Most-recent occurrence index per distinct text (list is newest-last, so
    # the last position seen wins). ``age`` is the rank distance from newest.
    last_index: dict[str, int] = {}
    for index, text in enumerate(entries):
        last_index[text] = index

    ranked: list[RankedPrompt] = []
    for text, freq in frequency.items():
        if prefix and not text.startswith(prefix):
            continue
        idx = last_index[text]
        age = (total - 1) - idx
        ranked.append(
            RankedPrompt(
                text=text,
                score=frecency_score(freq, age),
                frequency=freq,
                age=age,
                last_index=idx,
            )
        )

    ranked.sort(key=lambda entry: (-entry.score, entry.age, entry.text))
    if limit is not None:
        ranked = ranked[:limit]
    return ranked


def suggest_completion(entries: list[str], prefix: str) -> str | None:
    """Best frecency-ranked prior prompt that *completes* ``prefix``.

    The client-side autosuggestion surface (fish/zsh-style ghost text): given
    the composer's recency-ordered history and the current draft ``prefix``,
    return the highest-frecency prompt that ``startswith(prefix)`` **and** is
    strictly longer than it (a real completion, never an echo of what was just
    typed). ``None`` when ``prefix`` is empty or nothing completes it.

    This is a thin read over :func:`rank_history` -- it never mutates and never
    touches the composer's chronological up-ring; it only *picks* from the same
    frecency order the recall op exposes.
    """
    if not prefix:
        return None
    for ranked in rank_history(entries, prefix=prefix):
        if ranked.text != prefix:
            return ranked.text
    return None
