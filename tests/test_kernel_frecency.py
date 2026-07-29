"""Table-driven tests for kernel/frecency.py -- the transplanted donor curve.

The frecency model is pure (stdlib-only), so every case is a plain
list-in / ranking-out assertion. Formula under test (donor
``calculateFrecency`` re-expressed with rank-age; see ``.ai/oc_donor.md``):

    score = frequency / (1 + age)

where ``age`` is the rank distance of a prompt's most-recent occurrence from
the newest entry (0 = newest), over an oldest-first / newest-last list.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.frecency import (
    RankedPrompt,
    frecency_score,
    rank_history,
)


# --------------------------------------------------------------------------
# frecency_score -- the raw donor curve
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frequency", "age", "expected"),
    [
        (1, 0, 1.0),  # newest, once -> undecayed
        (1, 1, 0.5),  # one step older halves it (donor: 1 day)
        (1, 2, 1 / 3),  # two steps -> a third
        (1, 3, 0.25),  # hyperbolic, never exponential
        (3, 1, 1.5),  # frequency 3, slightly old -> beats a fresh single
        (5, 0, 5.0),  # frequency scales the numerator linearly
        (2, 4, 0.4),  # 2 / (1 + 4)
        (0, 0, 0.0),  # no frequency -> no score
    ],
)
def test_frecency_score_curve(frequency: int, age: int, expected: float) -> None:
    assert frecency_score(frequency, age) == pytest.approx(expected)


def test_frecency_score_clamps_negative_age() -> None:
    # Defensive: a negative age never divides below the age-0 value.
    assert frecency_score(2, -5) == pytest.approx(2.0)


def test_decay_is_monotonic_in_age() -> None:
    scores = [frecency_score(1, age) for age in range(6)]
    assert scores == sorted(scores, reverse=True)
    assert all(a > b for a, b in zip(scores, scores[1:], strict=False))


# --------------------------------------------------------------------------
# rank_history -- frequency dominance (the headline behavior)
# --------------------------------------------------------------------------


def test_frequency_beats_pure_recency() -> None:
    """A frequent-but-older prompt outranks a once-used newer one.

    Chronological recall would put ``delete branch`` (newest) first; frecency
    puts ``deploy app`` first (3x used, only one step older). This is the exact
    inversion the forge probe asserts (.ai/oc_donor.md worked example).
    """
    entries = [
        "deploy app",
        "run tests",
        "deploy app",
        "check logs",
        "deploy app",
        "delete branch",
    ]
    ranked = rank_history(entries)
    assert [r.text for r in ranked] == [
        "deploy app",  # freq 3, age 1 -> 1.5
        "delete branch",  # freq 1, age 0 -> 1.0
        "check logs",  # freq 1, age 2 -> 0.333
        "run tests",  # freq 1, age 4 -> 0.2
    ]
    top = ranked[0]
    assert (top.frequency, top.age) == (3, 1)
    assert top.score == pytest.approx(1.5)
    assert top.score > ranked[1].score  # frequency dominates recency


def test_recency_breaks_frequency_ties() -> None:
    """Equal-frequency prompts fall back to most-recent-first (donor order)."""
    # a and b both appear once; b is newer -> b ranks first.
    ranked = rank_history(["a", "b"])
    assert [r.text for r in ranked] == ["b", "a"]
    assert ranked[0].age == 0
    assert ranked[1].age == 1


def test_score_tie_resolved_by_recency_not_text() -> None:
    """When two DISTINCT prompts tie on score, the more-recent one wins.

    ``alpha`` (freq 2, age 3) and ``bravo`` (freq 1, age 1) both score 0.5.
    Distinct texts can never share an ``age`` (a position holds one text), so
    recency is the effective secondary key; ``bravo`` (age 1) beats ``alpha``
    (age 3) despite equal score, and text order never gets consulted here.
    """
    entries = ["alpha", "f1", "alpha", "f3", "bravo", "charlie"]
    ranked = rank_history(entries)
    assert [r.text for r in ranked] == ["charlie", "bravo", "alpha", "f3", "f1"]
    alpha = next(r for r in ranked if r.text == "alpha")
    bravo = next(r for r in ranked if r.text == "bravo")
    assert alpha.score == pytest.approx(0.5)
    assert bravo.score == pytest.approx(0.5)
    assert ranked.index(bravo) < ranked.index(alpha)  # recency, not "a" < "b"


# --------------------------------------------------------------------------
# rank_history -- prefix filter
# --------------------------------------------------------------------------


def test_prefix_filters_case_sensitive_startswith() -> None:
    entries = ["deploy app", "delete branch", "run tests", "Deploy CAPS"]
    ranked = rank_history(entries, prefix="de")
    texts = [r.text for r in ranked]
    assert "deploy app" in texts
    assert "delete branch" in texts
    assert "run tests" not in texts
    assert "Deploy CAPS" not in texts  # case-sensitive: capital D excluded


def test_empty_prefix_matches_all() -> None:
    entries = ["alpha", "beta", "gamma"]
    ranked = rank_history(entries, prefix="")
    assert {r.text for r in ranked} == {"alpha", "beta", "gamma"}


def test_prefix_with_no_match_is_empty() -> None:
    assert rank_history(["alpha", "beta"], prefix="zzz") == []


def test_prefix_still_ranks_by_frecency() -> None:
    # Two 'de' prompts: the thrice-used older one still beats the fresh single.
    entries = ["deploy", "x", "deploy", "y", "deploy", "delete"]
    ranked = rank_history(entries, prefix="de")
    assert [r.text for r in ranked] == ["deploy", "delete"]
    assert ranked[0].frequency == 3


# --------------------------------------------------------------------------
# rank_history -- empty / limit / edges
# --------------------------------------------------------------------------


def test_empty_history_is_empty() -> None:
    assert rank_history([]) == []


def test_empty_history_with_prefix_is_empty() -> None:
    assert rank_history([], prefix="de") == []


def test_limit_caps_results_to_top_n() -> None:
    entries = ["a", "b", "c", "d", "e"]
    ranked = rank_history(entries, limit=2)
    assert len(ranked) == 2
    # newest-first among equal frequencies: e (age0), d (age1)
    assert [r.text for r in ranked] == ["e", "d"]


def test_limit_zero_returns_nothing() -> None:
    assert rank_history(["a", "b"], limit=0) == []


def test_limit_negative_returns_nothing() -> None:
    assert rank_history(["a", "b"], limit=-3) == []


def test_limit_none_returns_all_distinct() -> None:
    entries = ["a", "a", "b", "c"]
    ranked = rank_history(entries, limit=None)
    assert len(ranked) == 3  # distinct prompts only


def test_single_entry() -> None:
    ranked = rank_history(["only"])
    assert ranked == [RankedPrompt(text="only", score=1.0, frequency=1, age=0, last_index=0)]


def test_ranked_prompt_fields_are_exact() -> None:
    ranked = rank_history(["x", "y", "x"])
    by_text = {r.text: r for r in ranked}
    # x: freq 2, last_index 2, age 0 -> score 2.0
    assert by_text["x"] == RankedPrompt(text="x", score=2.0, frequency=2, age=0, last_index=2)
    # y: freq 1, last_index 1, age 1 -> score 0.5
    assert by_text["y"] == RankedPrompt(text="y", score=0.5, frequency=1, age=1, last_index=1)
