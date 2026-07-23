"""Base ``RuntimeAdapter`` contract: neutral stubs + optional data hooks.

The base adapter (``ui/runtime_adapter.py``) is the contract every
runtime implementation (real, demo) subclasses. Its async stubs must
return NEUTRAL values — the app renders them directly when no live
session backs the call. This file sweeps that surface table-style and
guards the table with introspection (pattern:
``test_command_context_contract.py``) so a new public async method on
the adapter fails HERE until the table learns about it.

Pure asyncio — no threads, no monkeypatching.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from amplifier_app_newtui.kernel.session_ops import ModelListing, StatusInfo
from amplifier_app_newtui.model.blocks import BlockIdAllocator
from amplifier_app_newtui.ui.runtime_adapter import RuntimeAdapter

# ---------------------------------------------------------------------------
# T1 — neutral-return table for every public async stub
# ---------------------------------------------------------------------------

# (method, call args, expected neutral return)
NEUTRAL_CASES: tuple[tuple[str, tuple[Any, ...], Any], ...] = (
    ("submit", ("hello", ()), None),
    ("interrupt", (), False),
    ("list_native_modes", (), ""),
    ("set_native_mode", ("plan",), (False, "native modes need a real session")),
    ("list_models", (), ModelListing(provider="", current="")),
    ("set_model", ("gpt",), (False, "switching models needs a real session")),
    ("get_effort", (), None),
    ("set_effort", ("high",), (False, "reasoning effort needs a real session")),
    ("compact", ("focus",), (False, "compaction needs a real session")),
    ("clear_context", (), (False, 0)),
    ("status", (), StatusInfo()),
    ("list_tools", (), ()),
    ("list_agents", (), ()),
    ("diff", (True,), None),
    ("workspace_files", (), ()),
    ("list_skills", (), ()),
    ("load_skill", ("brainstorming",), (False, "skills need a real session")),
    ("mcp_tools", (), ()),
    ("rename_session", ("auth work",), (False, "renaming needs a real session")),
    ("session_summaries", (), ()),
    ("branch_session", ("spike",), (False, "branching needs a real session")),
    ("directory_entries", ("allowed",), ()),
    (
        "update_directory",
        ("allowed", "add", "/tmp/p"),
        (False, "directory management needs a real session"),
    ),
)

# Async methods deliberately NOT in the neutral table — each has its own
# behavioral test below (they do work rather than return a neutral value).
COVERED_ELSEWHERE: frozenset[str] = frozenset(
    {
        "start",  # T4: invokes ready()
        "submit_queued",  # T3: delegates to submit()
        "fork",  # T4: trims the ledger (confirm-then-trim)
        # T5: /config methods do real work over the in-memory config state.
        "config_view",
        "config_toggle",
        "config_set",
        "config_diff",
        "config_save",
    }
)

PUBLIC_ASYNC_METHODS: frozenset[str] = frozenset(
    name
    for name, member in vars(RuntimeAdapter).items()
    if not name.startswith("_") and inspect.iscoroutinefunction(member)
)


def test_neutral_table_covers_every_public_async_method() -> None:
    """Introspection guard: adding a public async method to the base
    adapter without teaching this table (or COVERED_ELSEWHERE) fails
    here, not in production."""
    table = {method for method, _, _ in NEUTRAL_CASES}
    assert not table & COVERED_ELSEWHERE, "a method is listed twice"
    assert table | COVERED_ELSEWHERE == PUBLIC_ASYNC_METHODS


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "args", "expected"), NEUTRAL_CASES)
async def test_base_stub_neutral_returns(method: str, args: tuple[Any, ...], expected: Any) -> None:
    adapter = RuntimeAdapter()
    result = await getattr(adapter, method)(*args)
    assert result == expected


# ---------------------------------------------------------------------------
# T2 — sync data hooks stay neutral
# ---------------------------------------------------------------------------


def test_base_sync_hooks_neutral() -> None:
    adapter = RuntimeAdapter()
    assert adapter.turn_spec("prompt") is None
    assert adapter.lane_seed("agent") is None
    assert adapter.lane_blocks("lane", "s1", BlockIdAllocator()) is None
    assert adapter.evidence_links("answer") == ()
    assert adapter.deferred_decision("msg") == ("msg", "", (), "", "")
    assert adapter.decision_narration("ship it") == "Applying decision: ship it"
    assert adapter.answer_approval("t1", "allow") is None  # no-op


def test_base_defer_approval_parks_into_needs_you_without_resolving() -> None:
    """ctrl-y park (issue #41): the base/demo runtime has no broker, so a
    deferred live ticket is parked directly in the needs-you queue —
    retro-answerable, its choices preserved, no answer recorded."""
    adapter = RuntimeAdapter()
    assert adapter.needs_you.pending_count == 0
    options = ("Allow once", "Allow always", "Deny")
    adapter.defer_approval("t1", "Run `pytest -q`?", options)
    assert adapter.needs_you.pending_count == 1
    item = adapter.needs_you.pending[0]
    assert item.question == "Run `pytest -q`?"
    assert item.choices == options
    assert item.status == "pending"  # parked, not answered
    # Empty/whitespace prompts never park a ghost decision.
    adapter.defer_approval("t2", "   ", options)
    assert adapter.needs_you.pending_count == 1


# ---------------------------------------------------------------------------
# T3 — submit_queued delegates to submit
# ---------------------------------------------------------------------------


class _RecordingSubmit(RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[tuple[str, tuple[Any, ...]]] = []

    async def submit(self, text: str, attachments: tuple[Any, ...] = ()) -> None:
        self.submitted.append((text, attachments))


@pytest.mark.asyncio
async def test_submit_queued_delegates_to_submit() -> None:
    adapter = _RecordingSubmit()
    await adapter.submit_queued("x")
    assert adapter.submitted == [("x", ())]


# ---------------------------------------------------------------------------
# T4 — start() invokes ready(); fork() trims the ledger immediately
# ---------------------------------------------------------------------------


class _FakeLedger:
    def __init__(self) -> None:
        self.trimmed: list[str] = []

    def trim_to(self, checkpoint_id: str) -> None:
        self.trimmed.append(checkpoint_id)


# ---------------------------------------------------------------------------
# T5 — /config surface works on the base adapter (demo == real, invariant 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_config_surface_round_trips(monkeypatch, tmp_path) -> None:
    """The base adapter fully implements /config over an in-memory state:
    view lists items, toggle/set round-trip into diff, and save persists to
    a settings scope (redirected to tmp so no real config is touched)."""
    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path))
    adapter = RuntimeAdapter()
    adapter._config_project_dir = tmp_path

    view = await adapter.config_view()
    assert any(i.category == "tools" for i in view.items)

    ok, message = await adapter.config_toggle("tools", "bash", False)
    assert ok and "Disabled bash" in message
    ok, message = await adapter.config_set("session.effort", "high")
    assert ok and "session.effort" in message

    changes = await adapter.config_diff()
    assert {(c.category, c.name) for c in changes} >= {
        ("tools", "bash"),
        ("set", "session.effort"),
    }

    ok, message = await adapter.config_save("global")
    assert ok and "global scope" in message
    written = (tmp_path / "settings.yaml").read_text()
    assert "configurator" in written and "bash" in written


@pytest.mark.asyncio
async def test_base_config_toggle_hooks_read_only() -> None:
    adapter = RuntimeAdapter()
    ok, message = await adapter.config_toggle("hooks", "hooks-mode", False)
    assert not ok and "read-only" in message


@pytest.mark.asyncio
async def test_base_start_calls_ready_and_fork_trims() -> None:
    adapter = RuntimeAdapter()
    ready_calls: list[int] = []
    await adapter.start(lambda: ready_calls.append(1))
    assert ready_calls == [1]

    ledger = _FakeLedger()
    await adapter.fork("cp-3", ledger)  # in-memory: confirmation is immediate
    assert ledger.trimmed == ["cp-3"]
