"""Runtime-path @mention expansion (issue #48): behavior + size bounds.

These exercise the amplifier-native re-expression in
``kernel/mention_expansion.py`` through foundation's real
``BaseMentionResolver`` (the same resolver ``create_session`` registers), plus
the ``RealRuntime.submit`` wiring that inlines mention content before
``session.execute`` while leaving the user echo untouched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from amplifier_foundation.mentions import BaseMentionResolver

from amplifier_app_newtui.kernel.events import Notification, PromptSubmit
from amplifier_app_newtui.kernel.git_yield import GitDiffSnapshot
from amplifier_app_newtui.kernel.mention_expansion import (
    MentionBudget,
    expand_mentions,
)
from amplifier_app_newtui.kernel.runtime import RealRuntime


def _resolver(base: Path) -> BaseMentionResolver:
    return BaseMentionResolver(base_path=base)


@pytest.mark.asyncio
async def test_file_mention_inlines_content_and_preserves_original(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("remember the milk", encoding="utf-8")
    result = await expand_mentions(
        "Please read @notes.md before you start.",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
    )
    assert result.expanded
    assert "<context_file" in result.text
    assert "remember the milk" in result.text
    # The @mention survives verbatim as a semantic reference after the block.
    assert result.text.rstrip().endswith("Please read @notes.md before you start.")
    assert (tmp_path / "notes.md").resolve() in result.included


@pytest.mark.asyncio
async def test_agent_mention_resolves_via_md_fallback(tmp_path: Path) -> None:
    # Agents are markdown definition files; @agent-name resolves through the
    # resolver's ``.md`` fallback (foundation BaseMentionResolver), same path
    # as any file mention.
    (tmp_path / "reviewer.md").write_text("You are a strict reviewer.", encoding="utf-8")
    result = await expand_mentions(
        "Delegate to @reviewer for the audit.",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
    )
    assert result.expanded
    assert "You are a strict reviewer." in result.text


@pytest.mark.asyncio
async def test_no_mentions_returns_text_unchanged(tmp_path: Path) -> None:
    result = await expand_mentions(
        "just a plain prompt, nothing to see",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
    )
    assert result.text == "just a plain prompt, nothing to see"
    assert not result.expanded
    assert result.included == ()


@pytest.mark.asyncio
async def test_unresolved_mention_is_opportunistically_skipped(tmp_path: Path) -> None:
    result = await expand_mentions(
        "read @does-not-exist.md and continue",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
    )
    assert result.text == "read @does-not-exist.md and continue"
    assert not result.expanded


@pytest.mark.asyncio
async def test_none_resolver_is_a_noop(tmp_path: Path) -> None:
    result = await expand_mentions("read @notes.md", resolver=None)
    assert result.text == "read @notes.md"
    assert not result.expanded


@pytest.mark.asyncio
async def test_mention_inside_code_fence_is_not_expanded(tmp_path: Path) -> None:
    (tmp_path / "secret.md").write_text("TOP SECRET", encoding="utf-8")
    result = await expand_mentions(
        "look at `@secret.md` literally",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
    )
    assert "TOP SECRET" not in result.text
    assert not result.expanded


@pytest.mark.asyncio
async def test_directory_mention_becomes_a_listing(tmp_path: Path) -> None:
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "a.py").write_text("", encoding="utf-8")
    result = await expand_mentions(
        "survey @pkg for me",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
    )
    assert result.expanded
    assert "Directory:" in result.text
    assert "a.py" in result.text


@pytest.mark.asyncio
async def test_per_file_size_bound_skips_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "big.log").write_text("x" * 5000, encoding="utf-8")
    result = await expand_mentions(
        "inline @big.log please",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
        budget=MentionBudget(max_file_bytes=100),
    )
    assert not result.expanded
    assert result.text == "inline @big.log please"
    assert result.skipped == (("@big.log", "too-large"),)


@pytest.mark.asyncio
async def test_total_budget_bound_stops_after_first_file(tmp_path: Path) -> None:
    (tmp_path / "one.md").write_text("a" * 80, encoding="utf-8")
    (tmp_path / "two.md").write_text("b" * 80, encoding="utf-8")
    result = await expand_mentions(
        "read @one.md and @two.md",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
        # First file (80 B) fits; the second would breach the 100 B ceiling.
        budget=MentionBudget(max_total_bytes=100),
    )
    assert "a" * 80 in result.text
    assert "b" * 80 not in result.text
    assert ("@two.md", "budget") in result.skipped


@pytest.mark.asyncio
async def test_max_files_bound_caps_count(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.md").write_text(name, encoding="utf-8")
    result = await expand_mentions(
        "read @a.md @b.md @c.md",
        resolver=_resolver(tmp_path),
        relative_to=tmp_path,
        budget=MentionBudget(max_files=2),
    )
    assert len(result.included) == 2
    assert ("@c.md", "file-limit") in result.skipped


# --------------------------------------------------------------------------
# RealRuntime.submit wiring
# --------------------------------------------------------------------------


def _runtime_with_resolver(tmp_path: Path) -> tuple[RealRuntime, SimpleNamespace]:
    resolver = _resolver(tmp_path)

    class Coordinator:
        def get_capability(self, name: str):  # noqa: ANN201 - focused fake
            return resolver if name == "mention_resolver" else None

        def get(self, name: str):  # noqa: ANN201 - focused fake
            del name
            return None

    session = SimpleNamespace(executed=[])

    async def execute(prompt: str) -> str:
        session.executed.append(prompt)
        return "done"

    session.execute = execute  # type: ignore[attr-defined]

    runtime = RealRuntime()
    runtime._project_dir = tmp_path
    runtime._initialized = SimpleNamespace(
        session_id="sid", coordinator=Coordinator(), session=session
    )

    async def no_diff() -> GitDiffSnapshot:
        return GitDiffSnapshot(False)

    runtime._capture_diff = no_diff  # type: ignore[method-assign]
    return runtime, session


@pytest.mark.asyncio
async def test_submit_expands_for_model_but_echoes_raw(tmp_path: Path) -> None:
    (tmp_path / "spec.md").write_text("the real spec", encoding="utf-8")
    runtime, session = _runtime_with_resolver(tmp_path)

    await runtime.submit("build from @spec.md")

    # The model saw the inlined content; the echo carried the raw prompt.
    assert session.executed and "the real spec" in session.executed[0]
    assert "build from @spec.md" in session.executed[0]
    submits = [e for e in _drain(runtime) if isinstance(e, PromptSubmit)]
    assert submits and submits[0].prompt == "build from @spec.md"


@pytest.mark.asyncio
async def test_submit_notifies_when_size_bounds_skip_a_mention(tmp_path: Path) -> None:
    (tmp_path / "huge.log").write_text("y" * 4000, encoding="utf-8")
    runtime, session = _runtime_with_resolver(tmp_path)
    runtime._mention_budget = MentionBudget(max_file_bytes=50)

    await runtime.submit("inline @huge.log")

    assert session.executed == ["inline @huge.log"]  # unexpanded
    notices = [e for e in _drain(runtime) if isinstance(e, Notification)]
    assert any("size bounds" in n.message for n in notices)


def _drain(runtime: RealRuntime) -> list[object]:
    events: list[object] = []
    while not runtime.queue.empty():
        events.append(runtime.queue.get_nowait())
    return events
