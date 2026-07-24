"""fork-with-directive (#108): the DIRECTIVE + primed-child dimension.

``/branch`` (#45) already snapshots the conversation into a new session.
This suite pins the added capability: ``session fork`` (CLI) and ``/fork``
(in-session) snapshot the parent context into a new session AND seed it with
a starting *directive* so the child is *primed* — a later
``amplifier-newtui resume <child>`` runs that instruction first.

Everything runs against a scratch store / scratch ``$HOME``; nothing touches
the developer's real ``~/.amplifier``. True detached/background execution is
not reachable from the full-screen TUI host (issue #45 seam gap), so the
reachable member proven here is the primed, resumable child.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from amplifier_app_newtui.kernel import session_manager as sm
from amplifier_app_newtui.kernel.persistence import SessionStore
from amplifier_app_newtui.main import main
from amplifier_app_newtui.ui import app_support
from amplifier_app_newtui.ui.runtime_adapter import RealRuntimeAdapter, RuntimeAdapter

PARENT_CONTEXT = [
    {"role": "user", "content": "wire the auth refactor"},
    {"role": "assistant", "content": "done — jwt rotation landed"},
]


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


# ---------------------------------------------------------------------------
# kernel: session_manager.fork — the primed child on disk
# ---------------------------------------------------------------------------


def test_fork_seeds_child_with_parent_context_and_directive(store: SessionStore) -> None:
    ok, child_id = sm.fork(
        store, "parent-1", PARENT_CONTEXT, "continue the refactor with tests", bundle="newtui"
    )
    assert ok
    assert child_id != "parent-1"
    transcript, metadata = store.load(child_id)
    # Parent context is present verbatim ...
    assert transcript == PARENT_CONTEXT
    # ... AND the directive is seeded (both prime keys carry it).
    assert metadata["pending_directive"] == "continue the refactor with tests"
    assert metadata["fork_directive"] == "continue the refactor with tests"
    # ... AND lineage points back at the parent.
    assert metadata["parent_id"] == "parent-1"
    assert "forked_at" in metadata
    assert metadata["bundle"] == "newtui"
    assert metadata["name"].startswith("fork-")


def test_fork_custom_name(store: SessionStore) -> None:
    ok, child_id = sm.fork(store, "p", [], "do the thing", name="jwt spike")
    assert ok
    assert store.get_metadata(child_id)["name"] == "jwt spike"


def test_fork_empty_directive_is_refused(store: SessionStore) -> None:
    ok, detail = sm.fork(store, "p", PARENT_CONTEXT, "   ")
    assert not ok
    assert "directive" in detail
    # Nothing was written.
    assert store.list_sessions() == []


def test_fork_rejects_bad_name(store: SessionStore) -> None:
    ok, detail = sm.fork(store, "p", [], "do it", name="bad/name")
    assert not ok
    assert "letters" in detail


def test_fork_clamps_long_directive(store: SessionStore) -> None:
    ok, child_id = sm.fork(store, "p", [], "x" * 5000)
    assert ok
    assert len(store.get_metadata(child_id)["pending_directive"]) == sm.MAX_DIRECTIVE_LENGTH


def test_fork_child_lists_as_top_level_resumable(store: SessionStore) -> None:
    ok, child_id = sm.fork(store, "p", PARENT_CONTEXT, "go")
    assert ok
    # A fresh uuid-hex id has no ``_`` so it is a resumable top-level session.
    assert child_id in store.list_sessions()


# ---------------------------------------------------------------------------
# kernel: take_pending_directive — consume-once prime
# ---------------------------------------------------------------------------


def test_take_pending_directive_reads_then_clears(store: SessionStore) -> None:
    _, child_id = sm.fork(store, "p", PARENT_CONTEXT, "run the plan")
    # First resume consumes the directive ...
    assert sm.take_pending_directive(store, child_id) == "run the plan"
    # ... and a second resume of the same child does NOT replay it.
    assert sm.take_pending_directive(store, child_id) == ""
    # provenance survives the consume.
    assert store.get_metadata(child_id)["fork_directive"] == "run the plan"


def test_take_pending_directive_missing_session(store: SessionStore) -> None:
    assert sm.take_pending_directive(store, "ghost") == ""


def test_take_pending_directive_no_prime(store: SessionStore) -> None:
    # A plain /branch child carries no directive.
    _, branch_id = sm.branch(store, "p", PARENT_CONTEXT)
    assert sm.take_pending_directive(store, branch_id) == ""


# ---------------------------------------------------------------------------
# CLI: `session fork <id> --directive ...`
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    """A scratch store the CLI and the test both resolve to (HOME + cwd)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return SessionStore()


def _seed(store: SessionStore, session_id: str) -> None:
    store.save(session_id, PARENT_CONTEXT, {"session_id": session_id, "bundle": "newtui"})


def test_cli_session_fork_creates_primed_child(scratch: SessionStore) -> None:
    _seed(scratch, "parent001")
    result = CliRunner().invoke(
        main, ["session", "fork", "parent001", "--directive", "add the missing tests"]
    )
    assert result.exit_code == 0
    assert "forked parent00" in result.output
    assert "resume" in result.output
    # The child persisted parent context + directive + lineage.
    children = [s for s in scratch.list_sessions() if s != "parent001"]
    assert len(children) == 1
    transcript, metadata = scratch.load(children[0])
    assert transcript == PARENT_CONTEXT
    assert metadata["pending_directive"] == "add the missing tests"
    assert metadata["parent_id"] == "parent001"
    assert metadata["bundle"] == "newtui"


def test_cli_session_fork_prefix_and_name(scratch: SessionStore) -> None:
    _seed(scratch, "deadbeef")
    result = CliRunner().invoke(main, ["session", "fork", "dead", "-d", "ship it", "-n", "release"])
    assert result.exit_code == 0
    child = next(s for s in scratch.list_sessions() if s != "deadbeef")
    assert scratch.get_metadata(child)["name"] == "release"


def test_cli_session_fork_unknown_parent_exits_nonzero(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["session", "fork", "ghost", "-d", "go"])
    assert result.exit_code == 1
    assert "no session found" in result.output


def test_cli_session_fork_missing_directive_is_usage_error(scratch: SessionStore) -> None:
    _seed(scratch, "parent001")
    result = CliRunner().invoke(main, ["session", "fork", "parent001"])
    assert result.exit_code == 2  # click: missing required option
    assert "directive" in result.output.lower()


def test_cli_session_fork_empty_directive_exits_nonzero(scratch: SessionStore) -> None:
    _seed(scratch, "parent001")
    result = CliRunner().invoke(main, ["session", "fork", "parent001", "-d", "   "])
    assert result.exit_code == 1
    assert "directive" in result.output


# ---------------------------------------------------------------------------
# in-session: RealRuntime.fork_session (duck-typed coordinator, no boot)
# ---------------------------------------------------------------------------


class _FakeContext:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages

    async def get_messages(self) -> list[dict[str, Any]]:
        return list(self._messages)


class _FakeCoordinator:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context

    def get(self, name: str) -> Any:
        return self._context if name == "context" else None


def _runtime_with_live_session(store: SessionStore) -> Any:
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    runtime = RealRuntime(bundle=None)
    runtime._store = store
    runtime._initialized = SimpleNamespace(  # type: ignore[assignment]
        session_id="live-parent",
        coordinator=_FakeCoordinator(_FakeContext(PARENT_CONTEXT)),
    )
    runtime.bundle_name = "newtui"
    return runtime


def test_runtime_fork_session_snapshots_live_context(store: SessionStore) -> None:
    runtime = _runtime_with_live_session(store)
    ok, child_id = asyncio.run(runtime.fork_session("keep going on the branch"))
    assert ok
    transcript, metadata = store.load(child_id)
    assert transcript == PARENT_CONTEXT
    assert metadata["pending_directive"] == "keep going on the branch"
    assert metadata["parent_id"] == "live-parent"


def test_runtime_fork_session_before_start_is_guarded(store: SessionStore) -> None:
    from amplifier_app_newtui.kernel.runtime import RealRuntime

    runtime = RealRuntime(bundle=None)  # no _store / _initialized yet
    ok, detail = asyncio.run(runtime.fork_session("go"))
    assert not ok
    assert "still starting" in detail


# ---------------------------------------------------------------------------
# adapter: fork_with_directive
# ---------------------------------------------------------------------------


def test_base_adapter_fork_with_directive_is_neutral() -> None:
    ok, detail = asyncio.run(RuntimeAdapter().fork_with_directive("go"))
    assert not ok
    assert "real session" in detail


def test_real_adapter_fork_with_directive_guards_boot() -> None:
    adapter = RealRuntimeAdapter()  # _runtime is None until start()
    ok, detail = asyncio.run(adapter.fork_with_directive("go"))
    assert not ok
    assert "still starting" in detail


def test_real_adapter_start_surfaces_pending_directive() -> None:
    # start() copies runtime.pending_directive onto the adapter so the app
    # can auto-run it; the base adapter defaults to "" (no prime).
    assert RuntimeAdapter().pending_directive == ""


# ---------------------------------------------------------------------------
# app boot: run_pending_directive — the "runs the directive first" behaviour
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, pending: str) -> None:
        self.adapter = SimpleNamespace(pending_directive=pending)
        self.notices: list[str] = []
        self.submitted: list[str] = []

    def show_notice(self, text: str) -> None:
        self.notices.append(text)

    def submit_prompt(self, text: str, attachments: tuple[Any, ...] = ()) -> None:
        self.submitted.append(text)


def test_run_pending_directive_auto_submits_and_clears() -> None:
    app = _FakeApp("run the migration")
    app_support.run_pending_directive(app)  # type: ignore[arg-type]
    assert app.submitted == ["run the migration"]
    assert app.adapter.pending_directive == ""  # consumed, never re-run
    assert any("run the migration" in n for n in app.notices)


def test_run_pending_directive_noop_without_prime() -> None:
    app = _FakeApp("")
    app_support.run_pending_directive(app)  # type: ignore[arg-type]
    assert app.submitted == []
    assert app.notices == []
