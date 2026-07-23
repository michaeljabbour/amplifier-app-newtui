"""The one public home for token formatting: two distinct display surfaces.

``model/formatting.py`` deliberately exposes TWO helpers that render the
same count differently (issue #29). These tests pin the divergence so a
future "dedup" cannot silently collapse them, and lock the re-exports the
older call sites still import under their historical names.
"""

from __future__ import annotations

from amplifier_app_newtui.model.formatting import (
    format_tokens_compact,
    format_tokens_k,
)


def test_format_tokens_k_is_fixed_one_decimal_thousands() -> None:
    # Sub-1k is shown, never rounded away; never switches to m-units.
    assert format_tokens_k(0) == "0.0k"
    assert format_tokens_k(608) == "0.6k"
    assert format_tokens_k(3_200) == "3.2k"
    assert format_tokens_k(52_000) == "52.0k"
    assert format_tokens_k(1_200_000) == "1200.0k"


def test_format_tokens_compact_is_adaptive_human_units() -> None:
    assert format_tokens_compact(742) == "742"
    assert format_tokens_compact(4_100) == "4.1k"
    assert format_tokens_compact(8_000) == "8k"
    assert format_tokens_compact(52_000) == "52k"
    assert format_tokens_compact(118_000) == "118k"
    assert format_tokens_compact(200_000) == "200k"
    assert format_tokens_compact(1_200_000) == "1.2m"


def test_surfaces_diverge_on_the_same_count() -> None:
    # Same input, two contracts: this difference is intentional, not a bug.
    assert format_tokens_k(52_000) == "52.0k"
    assert format_tokens_compact(52_000) == "52k"
    assert format_tokens_k(52_000) != format_tokens_compact(52_000)


def test_historical_reexports_share_the_one_implementation() -> None:
    from amplifier_app_newtui.commands.context import format_tokens
    from amplifier_app_newtui.kernel.demo import format_k_tokens

    assert format_tokens is format_tokens_compact
    assert format_k_tokens is format_tokens_k
