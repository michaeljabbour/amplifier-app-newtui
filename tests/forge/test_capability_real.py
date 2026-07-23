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

import time
from collections.abc import Iterator

import pytest

from amplifier_app_newtui.kernel.persistence import SessionStore

from ._forge import ForgeClient, ForgeSession
from ._ledger import ledger_cost, newest_session_id, poll_events, store_for
from .conftest import BATCH_TAG, NEWTUI_BINARY, REPO_ROOT, real_lane_skip_reason

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


@pytest.fixture
def real_session(forge_client: ForgeClient) -> Iterator[ForgeSession]:
    """A freshly booted real ``amplifier-newtui`` PTY (no --demo)."""
    session = forge_client.new(
        program=str(NEWTUI_BINARY),
        args=(),
        cwd=str(REPO_ROOT),
        cols=120,
        rows=40,
        tag=BATCH_TAG,
    )
    try:
        booted = session.wait("Message", total_timeout_ms=_BOOT_TIMEOUT_MS)
        assert booted, "real runtime did not boot to the composer"
        yield session
    finally:
        session.close()


def test_real_boot_to_composer(real_session: ForgeSession) -> None:
    """A(real): a real bundle prepare boots to the composer."""
    screen = real_session.screen()
    assert "Message" in screen, "composer prompt missing on real boot"
    assert "mode" in screen, "footer mode strip missing on real boot"


def test_real_resume_reseeds_cost_from_ledger(
    forge_client: ForgeClient, real_session: ForgeSession
) -> None:
    """E: resume rebuilds the transcript and re-seeds cost from the ledger."""
    store = store_for(REPO_ROOT)

    # One trivial governed turn so the ledger holds a priceable response.
    real_session.submit(_TRIVIAL_PROMPT)
    session_id = _wait_for_session(store, deadline_s=30.0)
    assert session_id is not None, "no session persisted after submit"
    assert poll_events(
        store,
        session_id,
        lambda events: any(e.get("kind") == "prompt_complete" for e in events),
        deadline_s=_TURN_TIMEOUT_MS / 1000.0,
    ), "turn never completed in the ledger"

    pre_exit_cost = ledger_cost(store, session_id)
    assert pre_exit_cost is not None, "ledger had no priceable cost"
    real_session.close()

    # Resume in a fresh PTY and assert the transcript + cost re-seed.
    resumed = forge_client.new(
        program=str(NEWTUI_BINARY),
        args=("resume", session_id),
        cwd=str(REPO_ROOT),
        cols=120,
        rows=40,
        tag=BATCH_TAG,
    )
    try:
        assert resumed.wait("Message", total_timeout_ms=_BOOT_TIMEOUT_MS), "resume did not boot"
        # Transcript rebuild: the original prompt re-appears.
        prompt_anchor = _TRIVIAL_PROMPT.split()[0]  # "reply"
        assert resumed.wait(prompt_anchor, total_timeout_ms=_BOOT_TIMEOUT_MS), (
            "resumed transcript did not rebuild"
        )
        # Cost re-seed: the footer total matches the pre-exit ledger sum.
        assert f"${pre_exit_cost:.2f}" in resumed.screen(), "resume cost re-seed mismatch"
    finally:
        resumed.close()


def _wait_for_session(store: SessionStore, *, deadline_s: float) -> str | None:
    """Bounded wait for a persisted top-level session id to appear."""
    deadline = time.monotonic() + deadline_s
    while True:
        session_id = newest_session_id(store)
        if session_id is not None:
            return session_id
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)
