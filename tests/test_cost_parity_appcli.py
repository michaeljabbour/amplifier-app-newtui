"""Pricing parity with amplifier-app-cli's estimator (BACKLOG item 1).

Fixture provenance
------------------
The expected totals below were generated on 2026-07-19 by running the
reference estimator **offline** (Helicone fetch disabled → its hardcoded
fallback table, which ``FALLBACK_PRICING`` here mirrors):

- amplifier-app-cli @ ``8e7bcf3c9d6ed00fa590be498425de0eea119fbf``
  (2026-07-16). The CLI itself contains no pricing math — its cost
  figures come from its mounted streaming-UI hook module:
- amplifier-module-hooks-streaming-ui @
  ``9a0a5c6f36b69118027cfad50dc22684320f9541`` (2026-02-22),
  ``amplifier_module_hooks_streaming_ui/cost.py::estimate_cost()``.

The values are hard-coded so this test is self-contained at runtime (CI
has no ``~/dev/amplifier-app-cli`` checkout). The reference computes in
floats; newtui computes in Decimal — parity is asserted far tighter than
the backlog's "to the cent" (within $0.000001).

Deliberately excluded: ``gpt-4o-mini``-family models. The reference's
offline fallback matcher is first-match, so ``gpt-4o`` shadows
``gpt-4o-mini`` there (models are priced at gpt-4o rates); newtui's
longest-prefix lookup prices them correctly. Live Helicone data uses
exact-match operators, so the divergence is fallback-only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from amplifier_app_newtui.kernel.cost import estimate_cost

_TOLERANCE = Decimal("0.000001")

# (provider, model, input, output, cache_read, cache_write, app-cli total)
# Totals are the reference estimator's float results, verbatim (repr).
PARITY_FIXTURES: tuple[tuple[str, str, int, int, int, int, str], ...] = (
    ("anthropic", "claude-sonnet-4-5", 12_345, 6_789, 100_000, 2_048, "0.175014"),
    ("anthropic", "claude-opus-4-1", 50_000, 10_000, 0, 25_000, "1.875"),
    ("anthropic", "claude-3-5-haiku-20241022", 400_000, 100_000, 350_000, 0, "0.748"),
    ("openai", "gpt-4o", 100_000, 50_000, 0, 10_000, "0.775"),
    ("openai", "o3-mini", 8_192, 4_096, 0, 0, "0.0270336"),
    ("openai", "o1-preview", 5_000, 15_000, 0, 0, "0.9749999999999999"),
    ("google", "gemini-2.0-flash-exp", 2_000_000, 500_000, 1_000_000, 0, "0.41000000000000003"),
    ("google", "gemini-1.5-pro", 123_456, 65_432, 0, 0, "0.48148"),
    ("azure", "gpt-4o", 250_000, 125_000, 80_000, 0, "1.895"),
)


@pytest.mark.parametrize(
    ("provider", "model", "input_tokens", "output_tokens", "cache_read", "cache_write", "expected"),
    PARITY_FIXTURES,
    ids=[f"{provider}-{model}" for provider, model, *_ in PARITY_FIXTURES],
)
def test_estimate_cost_matches_appcli_estimator(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    expected: str,
) -> None:
    cost = estimate_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        provider=provider,
        model=model,
    )
    assert cost is not None, f"{provider}/{model} must be priceable on the fallback table"
    delta = abs(cost - Decimal(expected))
    assert delta <= _TOLERANCE, (
        f"{provider}/{model}: newtui ${cost} vs app-cli ${expected} (Δ={delta})"
    )


def test_provider_inference_matches_explicit_provider() -> None:
    """app-cli passes provider explicitly; newtui may infer it from the
    model name — both paths must price identically for the fixtures."""
    for provider, model, inp, out, cread, cwrite, _expected in PARITY_FIXTURES:
        if provider == "azure":
            continue  # azure is never inferable from a model name
        explicit = estimate_cost(
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cread,
            cache_write_tokens=cwrite,
            provider=provider,
            model=model,
        )
        inferred = estimate_cost(
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cread,
            cache_write_tokens=cwrite,
            model=model,
        )
        assert explicit == inferred
