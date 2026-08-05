"""Real lane: capability A(real) + E through a real PTY + the durable ledger.

Credential-adaptive and opt-in.  It **skips cleanly** when no provider
credentials are configured (the acceptance's "no credentials -> demo
only" case) and also when credentials exist but the operator has not set
``AMPLIFIER_FORGE_REAL=1`` -- because the real lane boots a real session
(network + provider spend), which must never fire on a default ``-m forge``
run.  See ``real_lane_skip_reason`` in ``conftest.py``.

Where a real session exists it is observed **ledger-primary**: the
append-only ``ui-events.jsonl`` (ADR-0007 §9) is ANSI-free and race-free,
so the resume cost re-seed is asserted against ``sum_prior_cost`` -- the
exact "ledger state" the acceptance names -- rather than scraped glyphs.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator

import pytest

from amplifier_app_tui.kernel.persistence import SessionStore

from ._forge import ForgeClient, ForgeSession
from ._ledger import ledger_cost, poll_events, store_for
from .conftest import BATCH_TAG, TUI_BINARY, REPO_ROOT, real_lane_skip_reason

_SKIP_REASON = real_lane_skip_reason()

pytestmark = [
    pytest.mark.forge,
    pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or ""),
]

# Real bundle prepare is cold-cache slow; loop bounded waits well past the
# forge ~30 s cap (design doc: "loop wait past the cap; keep the action trivial").
_BOOT_TIMEOUT_MS = 180_000
_TURN_TIMEOUT_MS = 180_000
_TRIVIAL_PROMPT = "reply with the single word: ready"
_STREAMING_PROMPT = "Use extended thinking to calculate 317 times 29. Reply with only the number."


@pytest.fixture
def real_session(forge_client: ForgeClient) -> Iterator[ForgeSession]:
    """A freshly booted real ``amplifier-tui`` PTY (no --demo)."""
    session = forge_client.new(
        program=str(TUI_BINARY),
        args=(),
        cwd=str(REPO_ROOT),
        cols=120,
        rows=40,
        tag=BATCH_TAG,
    )
    try:
        # The composer widget exists while the provider/bundle still boots,
        # so "Message" can match before input is admissible.  The resolved
        # session banner is appended only by announce_ready().
        booted = session.wait("Bundle:", total_timeout_ms=_BOOT_TIMEOUT_MS)
        assert booted, "real runtime did not finish booting"
        assert "Message" in session.screen(), "real runtime did not boot to the composer"
        yield session
    finally:
        session.close()


def test_real_boot_to_composer(real_session: ForgeSession) -> None:
    """A(real): a real bundle prepare boots to the composer."""
    screen = real_session.screen()
    assert "Message" in screen, "composer prompt missing on real boot"
    assert "mode" in screen, "footer mode strip missing on real boot"


@pytest.mark.asyncio
async def test_real_anthropic_streams_thinking_and_text_before_durable_close() -> None:
    """#129: the paid provider reaches the TUI queue incrementally.

    ``ui-events.jsonl`` deliberately excludes Channel-A stream records to
    avoid a disk open/write/close for every token, so it cannot prove or
    disprove live streaming.  This opt-in real lane observes the exact
    in-memory queue consumed by Textual instead.  The Anthropic provider is
    selected explicitly because extended-thinking output is part of this
    regression's contract; ordinary Forge runs still skip the entire module
    unless credentials and ``AMPLIFIER_FORGE_REAL=1`` are both present.
    """
    from amplifier_app_tui.kernel import setup
    from amplifier_app_tui.kernel.runtime import RealRuntime

    configured = {provider.name for provider in setup.configured_providers()}
    stored = set(setup.setup_status().stored_keys)
    anthropic_keys = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_PROVIDER_ANTHROPIC_API_KEY",
    }
    if "anthropic" not in configured or not (stored & anthropic_keys):
        pytest.skip("Anthropic credentials are required for the real thinking-stream lane")

    runtime = RealRuntime(
        provider_override="anthropic",
        project_dir=REPO_ROOT,
    )
    try:
        await runtime.start()
        events = []
        streamed_before_return: set[str] = set()
        turn = asyncio.create_task(runtime.submit(_STREAMING_PROMPT))
        async with asyncio.timeout(_TURN_TIMEOUT_MS / 1000.0):
            while not turn.done() or not runtime.queue.empty():
                try:
                    event = await asyncio.wait_for(runtime.queue.get(), timeout=0.25)
                except TimeoutError:
                    continue
                events.append(event)
                if event.kind == "stream_block_delta" and not turn.done():
                    streamed_before_return.add(getattr(event, "block_type", ""))
            response = await turn
    finally:
        await runtime.cleanup()

    assert response.strip() == "9193"
    kinds = [event.kind for event in events]

    def positions(kind: str, block_type: str = "") -> list[int]:
        return [
            index
            for index, event in enumerate(events)
            if event.kind == kind
            and (not block_type or getattr(event, "block_type", "") == block_type)
        ]

    thinking_start = positions("stream_block_start", "thinking")
    thinking_delta = positions("stream_block_delta", "thinking")
    thinking_end = positions("stream_block_end", "thinking")
    text_start = positions("stream_block_start", "text")
    text_delta = positions("stream_block_delta", "text")
    text_end = positions("stream_block_end", "text")
    durable_end = positions("content_block_end")
    prompt_complete = positions("prompt_complete")

    assert thinking_start and thinking_delta and thinking_end, kinds
    assert text_start and text_delta and text_end, kinds
    assert durable_end and prompt_complete, kinds
    assert streamed_before_return == {"thinking", "text"}
    assert thinking_start[0] < thinking_delta[0] < thinking_end[0]
    assert text_start[0] < text_delta[0] < text_end[0]
    assert text_end[-1] < durable_end[0] < prompt_complete[-1]

    thinking_text = "".join(getattr(events[index], "text", "") for index in thinking_delta)
    streamed_answer = "".join(getattr(events[index], "text", "") for index in text_delta)
    assert thinking_text.strip(), "thinking stream carried no progressive content"
    assert streamed_answer.strip() == response.strip()


def test_real_resume_reseeds_cost_from_ledger(
    forge_client: ForgeClient, real_session: ForgeSession
) -> None:
    """E: resume rebuilds the transcript and re-seeds cost from the ledger."""
    store = store_for(REPO_ROOT)
    session_id = _runtime_session_id(store, real_session.screen())

    # One trivial governed turn so the ledger holds a priceable response.
    real_session.submit(_TRIVIAL_PROMPT)
    assert poll_events(
        store,
        session_id,
        lambda events: _prompt_completed(list(events), _TRIVIAL_PROMPT),
        deadline_s=_TURN_TIMEOUT_MS / 1000.0,
    ), "turn never completed in the ledger"

    pre_exit_cost = ledger_cost(store, session_id)
    assert pre_exit_cost is not None, "ledger had no priceable cost"
    real_session.close()

    # Resume in a fresh PTY and assert the transcript + cost re-seed.
    resumed = forge_client.new(
        program=str(TUI_BINARY),
        args=("resume", session_id),
        cwd=str(REPO_ROOT),
        cols=120,
        rows=40,
        tag=BATCH_TAG,
    )
    try:
        assert resumed.wait("Bundle:", total_timeout_ms=_BOOT_TIMEOUT_MS), "resume did not boot"
        assert "Message" in resumed.screen(), "resumed composer missing"
        # Transcript rebuild: the original prompt re-appears.
        prompt_anchor = _TRIVIAL_PROMPT.split()[0]  # "reply"
        assert resumed.wait(prompt_anchor, total_timeout_ms=_BOOT_TIMEOUT_MS), (
            "resumed transcript did not rebuild"
        )
        # Cost re-seed: the footer total matches the pre-exit ledger sum.
        assert f"${pre_exit_cost:.2f}" in resumed.screen(), "resume cost re-seed mismatch"
    finally:
        resumed.close()


def _runtime_session_id(store: SessionStore, screen: str) -> str:
    """Resolve the exact runtime session advertised by the ready banner."""
    match = re.search(r"\bsession\s+([0-9a-f]{6})\b", screen)
    assert match is not None, "ready banner did not expose a session id"
    return store.find_session(match.group(1))


def _prompt_completed(events: list[dict[str, object]], prompt: str) -> bool:
    """Whether the exact submitted prompt has a later close-out event."""
    for index, event in enumerate(events):
        if event.get("kind") == "prompt_submit" and event.get("prompt") == prompt:
            return any(record.get("kind") == "prompt_complete" for record in events[index + 1 :])
    return False
