"""Git-yield snapshots + tests-✔ tracker + RealRuntime enriched close-out.

The RealRuntime snapshots the project's git diffstat before/after each
turn and synthesizes ``PromptComplete`` with the delta (files N ·
+A/−D) plus the tests-✔ heuristic derived from the turn's tool results
(reference: amplifier-app-cli ``runtime/interactive_turn.py`` +
``ui/git_yield.py`` + ``ui/turn_outcomes.py``). All offline: a temp git
repo and a fake session, no provider, no Textual.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from amplifier_app_newtui.kernel.events import PromptComplete, ToolError, ToolPost, ToolPre
from amplifier_app_newtui.kernel.git_yield import (
    GitDiffSnapshot,
    GitFileStat,
    capture_git_diff,
)
from amplifier_app_newtui.kernel.runtime import RealRuntime
from amplifier_app_newtui.kernel.turn_yield import TurnYieldTracker

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# GitDiffSnapshot delta math (pure)
# --------------------------------------------------------------------------


async def test_delta_from_counts_changed_files_and_lines() -> None:
    before = GitDiffSnapshot(True, (GitFileStat("a.py", 2, 1),))
    after = GitDiffSnapshot(
        True,
        (
            GitFileStat("a.py", 10, 3),  # +8/−2 on top of the pre-turn dirt
            GitFileStat("b.py", 5, 0),  # new file this turn
        ),
    )
    delta = after.delta_from(before)
    assert delta is not None
    assert delta.files == 2
    assert delta.additions == 13
    assert delta.deletions == 2
    assert delta.diff_label == "+13/−2"


async def test_delta_from_none_when_either_snapshot_unavailable() -> None:
    ok = GitDiffSnapshot(True, ())
    missing = GitDiffSnapshot(False)
    assert ok.delta_from(missing) is None
    assert missing.delta_from(ok) is None
    unchanged = GitDiffSnapshot(True, (GitFileStat("a.py", 1, 1),))
    delta = unchanged.delta_from(GitDiffSnapshot(True, (GitFileStat("a.py", 1, 1),)))
    assert delta is not None and delta.files == 0


# --------------------------------------------------------------------------
# capture_git_diff against a real temp repo
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("one\ntwo\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


async def test_capture_git_diff_sees_tracked_and_untracked(git_repo: Path) -> None:
    clean = await capture_git_diff(git_repo)
    assert clean.available and clean.files == ()

    (git_repo / "tracked.txt").write_text("one\ntwo\nthree\n")
    (git_repo / "fresh.txt").write_text("a\nb\n")
    dirty = await capture_git_diff(git_repo)
    assert dirty.available
    assert dirty.files == (
        GitFileStat("fresh.txt", 2, 0),
        GitFileStat("tracked.txt", 1, 0),
    )
    delta = dirty.delta_from(clean)
    assert delta is not None
    assert (delta.files, delta.diff_label) == (2, "+3/−0")


async def test_capture_git_diff_unavailable_outside_a_repo(tmp_path: Path) -> None:
    snapshot = await capture_git_diff(tmp_path)
    assert not snapshot.available


# --------------------------------------------------------------------------
# tests-✔ tracker (turn_outcomes.py heuristic)
# --------------------------------------------------------------------------


async def test_tracker_reports_none_without_test_commands() -> None:
    tracker = TurnYieldTracker()
    tracker.start_turn()
    tracker.observe(
        ToolPost(tool_name="bash", tool_call_id="c1", tool_input={"command": "ls"}, result={})
    )
    assert tracker.tests_ok is None


async def test_tracker_marks_passing_pytest_run() -> None:
    tracker = TurnYieldTracker()
    tracker.start_turn()
    tracker.observe(
        ToolPost(
            tool_name="bash",
            tool_call_id="c1",
            tool_input={"command": "uv run pytest -q"},
            result={"exit_code": 0},
        )
    )
    assert tracker.tests_ok is True


async def test_tracker_marks_failing_and_errored_test_runs() -> None:
    tracker = TurnYieldTracker()
    tracker.start_turn()
    tracker.observe(
        ToolPost(
            tool_name="bash",
            tool_call_id="c1",
            tool_input={"command": "pytest tests/"},
            result={"exit_code": 1},
        )
    )
    assert tracker.tests_ok is False

    tracker.start_turn()  # tool:error path correlates via the tool:pre command
    tracker.observe(
        ToolPre(tool_name="bash", tool_call_id="c2", tool_input={"command": "npm test"})
    )
    tracker.observe(ToolError(tool_name="bash", tool_call_id="c2", error_message="boom"))
    assert tracker.tests_ok is False

    tracker.start_turn()  # reset clears prior evidence
    assert tracker.tests_ok is None


# --------------------------------------------------------------------------
# RealRuntime.submit: enriched close-out is the last event on the queue
# --------------------------------------------------------------------------


class _FakeSession:
    """Duck-typed session: 'changes files' and 'runs pytest' during execute."""

    def __init__(self, runtime: RealRuntime, repo: Path) -> None:
        self._runtime = runtime
        self._repo = repo

    async def execute(self, text: str) -> str:
        (self._repo / "written.txt").write_text("alpha\nbeta\ngamma\n")
        self._runtime.bridge.emit(
            ToolPost(
                session_id="sess-1",
                tool_name="bash",
                tool_call_id="c1",
                tool_input={"command": "uv run pytest -q"},
                result={"exit_code": 0},
            )
        )
        return "all done"


async def test_submit_close_out_carries_git_delta_and_tests_ok(git_repo: Path) -> None:
    runtime = RealRuntime(project_dir=git_repo)
    runtime._initialized = SimpleNamespace(  # duck-typed InitializedSession
        session=_FakeSession(runtime, git_repo), session_id="sess-1"
    )
    response = await runtime.submit("write a file and test it")
    assert response == "all done"

    events = []
    while not runtime.queue.empty():
        events.append(runtime.queue.get_nowait())
    closing = events[-1]
    assert isinstance(closing, PromptComplete)
    assert closing.session_id == "sess-1"
    assert closing.response == "all done"
    assert closing.files_changed == 1
    assert closing.diffstat == "+3/−0"
    assert closing.tests_ok is True


async def test_submit_close_out_defaults_when_not_a_git_repo(tmp_path: Path) -> None:
    runtime = RealRuntime(project_dir=tmp_path)

    async def execute(text: str) -> str:
        return "answer only"

    runtime._initialized = SimpleNamespace(
        session=SimpleNamespace(execute=execute), session_id="sess-2"
    )
    await runtime.submit("hello")
    events = []
    while not runtime.queue.empty():
        events.append(runtime.queue.get_nowait())
    closing = events[-1]
    assert isinstance(closing, PromptComplete)
    assert (closing.files_changed, closing.diffstat, closing.tests_ok) == (0, "", None)


async def test_submit_close_out_emitted_even_when_execute_raises(tmp_path: Path) -> None:
    runtime = RealRuntime(project_dir=tmp_path)

    async def execute(text: str) -> str:
        raise RuntimeError("provider exploded")

    runtime._initialized = SimpleNamespace(
        session=SimpleNamespace(execute=execute), session_id="sess-3"
    )
    with pytest.raises(RuntimeError):
        await runtime.submit("hello")
    closing = None
    while not runtime.queue.empty():
        closing = runtime.queue.get_nowait()
    assert isinstance(closing, PromptComplete)  # the turn still closes


async def test_bridge_does_not_register_raw_prompt_complete() -> None:
    """The hook-driven prompt:complete is suppressed — submit() owns it."""
    runtime = RealRuntime()
    registered: list[str] = []
    hooks = SimpleNamespace(
        register=lambda event, handler, priority=10, name="": registered.append(event)
    )
    runtime.bridge.register_hooks(hooks)
    assert "prompt:submit" in registered
    assert "prompt:complete" not in registered
    await asyncio.sleep(0)  # keep pytest-asyncio happy about the async test
