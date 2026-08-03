"""CLI: `amplifier-tui session <verb>` + the `resume` picker.

Every test runs against a scratch ``$HOME`` (monkeypatched) so the stored
sessions live in a tmp dir, never the developer's real ``~/.amplifier``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

import amplifier_app_tui.main as main_mod
from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.main import main


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    """A scratch store the CLI and the test both resolve to (HOME + cwd)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return SessionStore()


def _seed(
    store: SessionStore,
    session_id: str,
    *,
    name: str = "",
    messages: int = 0,
    turns: int | None = None,
) -> None:
    transcript = [{"role": "user", "content": f"m{i}"} for i in range(messages)]
    metadata: dict[str, object] = {"session_id": session_id, "bundle": "tui"}
    if name:
        metadata["name"] = name
    if turns is not None:
        metadata["turn_count"] = turns
    store.save(session_id, transcript, metadata)


# -- session list -----------------------------------------------------------


def test_session_list_empty(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["session", "list"])
    assert result.exit_code == 0
    assert "no stored sessions" in result.output


def test_session_list_shows_rows(scratch: SessionStore) -> None:
    _seed(scratch, "abc12345", name="auth work", messages=3)
    result = CliRunner().invoke(main, ["session", "list"])
    assert result.exit_code == 0
    assert "auth work" in result.output
    assert "abc12345" in result.output


# -- sessions (top-level, shares session-list's renderer) -------------------


def test_sessions_empty(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["sessions"])
    assert result.exit_code == 0
    assert "no stored sessions" in result.output


def test_sessions_renders_named_table(scratch: SessionStore) -> None:
    _seed(scratch, "abc12345", name="auth work", messages=3, turns=2)
    result = CliRunner().invoke(main, ["sessions"])
    assert result.exit_code == 0
    # The rich table headers, not a bare wall of ids (S3).
    for header in ("Name", "Session", "Msgs", "Turns", "Age"):
        assert header in result.output
    assert "auth work" in result.output
    assert "abc12345" in result.output


def test_sessions_matches_session_list(scratch: SessionStore) -> None:
    _seed(scratch, "abc12345", name="auth work", messages=3, turns=2)
    _seed(scratch, "def67890", name="ui polish", messages=5, turns=4)
    top = CliRunner().invoke(main, ["sessions"])
    grouped = CliRunner().invoke(main, ["session", "list"])
    assert top.exit_code == 0
    assert grouped.exit_code == 0
    # One shared renderer: identical table for the same store.
    assert top.output == grouped.output


def test_sessions_shows_turns_when_recorded(scratch: SessionStore) -> None:
    _seed(scratch, "abc12345", name="auth work", messages=6, turns=3)
    result = CliRunner().invoke(main, ["sessions"])
    assert result.exit_code == 0
    assert "3" in result.output


def test_sessions_turns_dash_when_not_stored(scratch: SessionStore) -> None:
    # No turn_count in metadata → the Turns cell degrades to em dash, not 0.
    _seed(scratch, "abc12345", name="auth work", messages=6)
    result = CliRunner().invoke(main, ["sessions"])
    assert result.exit_code == 0
    assert "—" in result.output


def test_sessions_plain_prints_bare_ids(scratch: SessionStore) -> None:
    _seed(scratch, "abc12345", name="auth work", messages=3, turns=2)
    _seed(scratch, "def67890", name="ui polish", messages=5, turns=4)
    result = CliRunner().invoke(main, ["sessions", "--plain"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert set(lines) == {"abc12345", "def67890"}
    # Plain output is ids-only: no table chrome.
    assert "Name" not in result.output
    assert "Session" not in result.output


# -- session rename ---------------------------------------------------------


def test_session_rename_updates_metadata(scratch: SessionStore) -> None:
    _seed(scratch, "sess0001")
    result = CliRunner().invoke(main, ["session", "rename", "sess0001", "big", "refactor"])
    assert result.exit_code == 0
    assert "renamed" in result.output
    assert scratch.get_metadata("sess0001")["name"] == "big refactor"


def test_session_rename_prefix(scratch: SessionStore) -> None:
    _seed(scratch, "deadbeef")
    result = CliRunner().invoke(main, ["session", "rename", "dead", "shipped"])
    assert result.exit_code == 0
    assert scratch.get_metadata("deadbeef")["name"] == "shipped"


def test_session_rename_unknown_exits_nonzero(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["session", "rename", "ghost", "x"])
    assert result.exit_code == 1
    assert "no session found" in result.output


# -- session delete ---------------------------------------------------------


def test_session_delete_force(scratch: SessionStore) -> None:
    _seed(scratch, "victim01")
    result = CliRunner().invoke(main, ["session", "delete", "victim01", "--force"])
    assert result.exit_code == 0
    assert "deleted victim01" in result.output
    assert not scratch.exists("victim01")


def test_session_delete_confirm_no_keeps_it(scratch: SessionStore) -> None:
    _seed(scratch, "keepme01")
    result = CliRunner().invoke(main, ["session", "delete", "keepme01"], input="n\n")
    assert result.exit_code == 0
    assert "cancelled" in result.output
    assert scratch.exists("keepme01")


def test_session_delete_unknown_exits_nonzero(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["session", "delete", "ghost", "--force"])
    assert result.exit_code == 1
    assert "no session found" in result.output


# -- session cleanup --------------------------------------------------------


def test_session_cleanup_removes_old(scratch: SessionStore) -> None:
    _seed(scratch, "fresh001")
    _seed(scratch, "stale001")
    old = (datetime.now(UTC) - timedelta(days=45)).timestamp()
    os.utime(scratch.session_dir("stale001"), (old, old))
    result = CliRunner().invoke(main, ["session", "cleanup", "--days", "30", "--force"])
    assert result.exit_code == 0
    assert "removed 1" in result.output
    assert scratch.exists("fresh001")
    assert not scratch.exists("stale001")


# -- resume picker ----------------------------------------------------------


def test_resume_empty_store(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["resume"])
    assert result.exit_code == 0
    assert "no stored sessions" in result.output


def test_resume_picker_cancel(scratch: SessionStore) -> None:
    _seed(scratch, "aaaa1111")
    _seed(scratch, "bbbb2222")
    result = CliRunner().invoke(main, ["resume"], input="q\n")
    assert result.exit_code == 0
    assert "cancelled" in result.output


def test_resume_picker_selects_and_launches(
    scratch: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: dict[str, object] = {}

    async def fake_launch(
        *, demo: bool, bundle: str | None = None, resume_id: str | None = None
    ) -> int:
        launched["resume_id"] = resume_id
        return 0

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    _seed(scratch, "aaaa1111", name="one")
    _seed(scratch, "bbbb2222", name="two")
    # Newest-first: bbbb2222 was saved last, so [1] is bbbb2222.
    os.utime(scratch.session_dir("bbbb2222"), None)
    result = CliRunner().invoke(main, ["resume"], input="1\n")
    assert result.exit_code == 0
    assert launched["resume_id"] in {"aaaa1111", "bbbb2222"}


def test_resume_direct_id_resolves_prefix(
    scratch: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_launch(
        *, demo: bool, bundle: str | None = None, resume_id: str | None = None
    ) -> int:
        assert resume_id == "cafef00d"
        return 0

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    _seed(scratch, "cafef00d")
    result = CliRunner().invoke(main, ["resume", "cafe"])
    assert result.exit_code == 0


def test_resume_unknown_id_exits_nonzero(scratch: SessionStore) -> None:
    _seed(scratch, "cafef00d")
    result = CliRunner().invoke(main, ["resume", "zzz"])
    assert result.exit_code == main_mod.RESUME_EXIT_NOT_FOUND
    assert result.exit_code == 2
    assert "no session found" in result.output


def test_resume_hints_at_cross_project_session(scratch: SessionStore, tmp_path: Path) -> None:
    """A ``resume SESSION_ID`` that misses the current dir but exists in another
    project points the user at the dir it lives in (per-dir store confusion)."""
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    other = SessionStore(project_dir=other_dir)
    other.save(
        "beefcafe",
        [],
        {"session_id": "beefcafe", "bundle": "tui", "working_dir": str(other_dir)},
    )
    # cwd (scratch) has a different, unrelated session — the id is not here.
    _seed(scratch, "cafef00d")
    result = CliRunner().invoke(main, ["resume", "beef"])
    assert result.exit_code == main_mod.RESUME_EXIT_NOT_FOUND
    assert "no session found" in result.output
    assert "another project" in result.output
    assert f"cd {other_dir}" in result.output
    assert "resume beefcafe" in result.output


# -- session resume (alias to top-level resume) ----------------------------


def test_session_resume_direct_id_matches_top_level(
    scratch: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``session resume SESSION_ID`` reuses the top-level ``resume`` handler."""
    seen: list[str | None] = []

    async def fake_launch(
        *, demo: bool, bundle: str | None = None, resume_id: str | None = None
    ) -> int:
        seen.append(resume_id)
        return 0

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    _seed(scratch, "cafef00d")

    top = CliRunner().invoke(main, ["resume", "cafe"])
    alias = CliRunner().invoke(main, ["session", "resume", "cafe"])

    assert top.exit_code == 0
    assert alias.exit_code == 0
    # Both spellings resolve the same prefix to the same full id.
    assert seen == ["cafef00d", "cafef00d"]


def test_session_resume_unknown_id_exits_nonzero(scratch: SessionStore) -> None:
    _seed(scratch, "cafef00d")
    result = CliRunner().invoke(main, ["session", "resume", "zzz"])
    assert result.exit_code == main_mod.RESUME_EXIT_NOT_FOUND
    assert "no session found" in result.output


def test_session_resume_is_the_same_command_object() -> None:
    """The alias registers the one Command, not a forked reimplementation."""
    assert main.commands["session"].commands["resume"] is main.commands["resume"]


# -- exit hint (how to resume, printed on TUI exit) ------------------------


def test_exit_hint_prints_resume_command(capsys: pytest.CaptureFixture[str]) -> None:
    """Prints the SHORT (8-char) id -- the one canonical form every other
    resume hint uses (S3); this was the one holdout printing the full id."""
    main_mod._print_resume_hint("cafef00d1234")
    printed = capsys.readouterr().out
    assert "amplifier-tui resume cafef00d" in printed
    assert "amplifier-tui resume cafef00d1234" not in printed
    assert "amplifier-tui sessions" in printed


def test_exit_hint_skipped_without_session_id(capsys: pytest.CaptureFixture[str]) -> None:
    main_mod._print_resume_hint("")
    assert capsys.readouterr().out == ""


def test_launch_tui_prints_hint_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interactive TUI exit path surfaces the resume hint (S4)."""
    import amplifier_app_tui.ui.app as app_mod
    import amplifier_app_tui.ui.runtime_adapter as adapter_mod
    import amplifier_app_tui.ui.term_probe as probe_mod

    class FakeAdapter:
        def __init__(
            self,
            *,
            bundle: str | None = None,
            resume_id: str | None = None,
            provider_override: str | None = None,
            model_override: str | None = None,
        ) -> None:
            self.session_id = "feedface5678"

    class FakeApp:
        def __init__(
            self, adapter: object, *, kitty_protocol: bool, initial_mode: str | None = None
        ) -> None:
            self.return_code = 0

        async def run_async(self) -> None:
            return None

    monkeypatch.setattr(adapter_mod, "RealRuntimeAdapter", FakeAdapter)
    monkeypatch.setattr(app_mod, "TuiApp", FakeApp)
    monkeypatch.setattr(probe_mod, "patch_legacy_alt_named_keys", lambda: None)
    monkeypatch.setattr(probe_mod, "probe_kitty_protocol", lambda: False)

    printed: list[str] = []
    monkeypatch.setattr(main_mod, "_print_resume_hint", lambda sid: printed.append(sid))

    code = asyncio.run(main_mod._launch_tui(demo=False))
    assert code == 0
    assert printed == ["feedface5678"]


# -- continue (most-recent shortcut) ---------------------------------------


def test_resume_ambiguous_prefix_exits_distinct_code(scratch: SessionStore) -> None:
    _seed(scratch, "aaaa1111", name="one", messages=2)
    _seed(scratch, "aaaa2222", name="two", messages=4)
    result = CliRunner().invoke(main, ["resume", "aaaa"])
    assert result.exit_code == main_mod.RESUME_EXIT_AMBIGUOUS
    assert result.exit_code == 3
    assert "matches 2 sessions" in result.output
    # An actionable table, not a 3-item truncated id preview: both full short
    # ids and their distinguishing names are visible.
    assert "Matching sessions" in result.output
    assert "aaaa1111" in result.output
    assert "aaaa2222" in result.output
    assert "one" in result.output
    assert "two" in result.output
    # A concrete next command using a REAL id, not just "try again" -- the
    # example is whichever candidate the ambiguity resolver lists first
    # (newest-first), so accept either rather than assuming save order.
    assert any(f"amplifier-tui resume {sid}" in result.output for sid in ("aaaa1111", "aaaa2222"))


def test_resume_ambiguous_prefix_via_alias_matches_top_level(scratch: SessionStore) -> None:
    """``session resume`` is the same Command object, so it agrees for free."""
    _seed(scratch, "aaaa1111", name="one")
    _seed(scratch, "aaaa2222", name="two")
    result = CliRunner().invoke(main, ["session", "resume", "aaaa"])
    assert result.exit_code == main_mod.RESUME_EXIT_AMBIGUOUS


def test_resume_corrupt_session_exits_distinct_code(scratch: SessionStore) -> None:
    _seed(scratch, "deadbeef")
    # Corrupt metadata.json with no .backup to recover from: SessionStore
    # degrades this to a synthesized ``recovered`` stub rather than raising
    # (persistence._load_metadata) -- the resume path probes for that stub.
    (scratch.session_dir("deadbeef") / "metadata.json").write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(main, ["resume", "deadbeef"])
    assert result.exit_code == main_mod.RESUME_EXIT_CORRUPT
    assert result.exit_code == 4
    assert "corrupt" in result.output.lower()
    assert "deadbeef" in result.output
    assert "session delete deadbeef --force" in result.output


def test_resume_success_exits_zero_and_is_distinct_from_errors(
    scratch: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_launch(
        *, demo: bool, bundle: str | None = None, resume_id: str | None = None
    ) -> int:
        return 0

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    _seed(scratch, "cafef00d")
    result = CliRunner().invoke(main, ["resume", "cafef00d"])
    assert result.exit_code == 0
    assert result.exit_code not in (
        main_mod.RESUME_EXIT_NOT_FOUND,
        main_mod.RESUME_EXIT_AMBIGUOUS,
        main_mod.RESUME_EXIT_CORRUPT,
    )


# -- canonical syntax: help / completion / exit guidance must agree (S3) ----


def test_ambiguity_and_exit_hint_use_the_same_short_id_form(
    scratch: SessionStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ambiguous-prefix table and the TUI-exit hint print the exact same
    8-char short-id form for a resumable id -- copy-pasting either hint
    produces the identical command."""
    _seed(scratch, "aaaa1111", name="one")
    _seed(scratch, "aaaa2222", name="two")
    ambiguous_output = CliRunner().invoke(main, ["resume", "aaaa"]).output
    # The ambiguity resolver lists newest-first; either seeded id is a valid
    # example -- what matters is the SAME one round-trips through the hint.
    if "amplifier-tui resume aaaa1111" in ambiguous_output:
        example_id = "aaaa1111"
    else:
        assert "amplifier-tui resume aaaa2222" in ambiguous_output
        example_id = "aaaa2222"

    main_mod._print_resume_hint(example_id)
    hinted = capsys.readouterr().out
    assert f"amplifier-tui resume {example_id}" in hinted


def test_resume_help_shows_optional_bracketed_metavar() -> None:
    """``--help`` still shows ``[SESSION_ID]`` (optional): the explicit
    ``shell_complete`` wiring must not regress Click's own required/optional
    usage rendering."""
    result = CliRunner().invoke(main, ["resume", "--help"])
    assert "[SESSION_ID]" in result.output


def test_shell_completion_offers_the_same_short_ids(scratch: SessionStore) -> None:
    """Tab-completion candidates are the SAME short-id form used everywhere
    else (S3), sourced live from the store -- not a static/no-op stub."""
    _seed(scratch, "cafef00d", name="auth work")
    _seed(scratch, "beefcafe", name="ui polish")
    ctx = main.make_context("amplifier-tui", ["resume"])
    session_id_arg = main.commands["resume"].params[0]
    assert session_id_arg.name == "session_id"

    all_items = main_mod._complete_session_id(ctx, session_id_arg, "")
    assert {item.value for item in all_items} == {"cafef00d", "beefcafe"}

    prefixed = main_mod._complete_session_id(ctx, session_id_arg, "cafe")
    assert {item.value for item in prefixed} == {"cafef00d"}


def test_run_and_serve_resume_share_the_same_completion_function() -> None:
    """``run --resume`` / ``serve --resume`` complete exactly like ``resume``
    (S3): one function wired to all three params, so they cannot drift."""
    run_option = next(p for p in main.commands["run"].params if p.name == "resume")
    serve_option = next(p for p in main.commands["serve"].params if p.name == "resume")
    resume_arg = main.commands["resume"].params[0]
    # Click's own ``.shell_complete`` is a dispatcher METHOD on Parameter, not
    # the callback we passed in -- the callback itself is stashed on the
    # private ``_custom_shell_complete`` slot, which is what must be identical
    # across all three params for them to be provably wired to one function.
    assert run_option._custom_shell_complete is main_mod._complete_session_id
    assert serve_option._custom_shell_complete is main_mod._complete_session_id
    assert resume_arg._custom_shell_complete is main_mod._complete_session_id


# -- continue (most-recent shortcut) -----------------------------------------


def test_continue_empty_store(scratch: SessionStore) -> None:
    result = CliRunner().invoke(main, ["continue"])
    assert result.exit_code == 0
    assert "no stored sessions" in result.output


def test_continue_launches_most_recent(
    scratch: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: dict[str, object] = {}

    async def fake_launch(
        *, demo: bool, bundle: str | None = None, resume_id: str | None = None
    ) -> int:
        launched["resume_id"] = resume_id
        return 0

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    _seed(scratch, "aaaa1111", name="one")
    _seed(scratch, "bbbb2222", name="two")
    # Make bbbb2222 the newest by touching it last.
    os.utime(scratch.session_dir("bbbb2222"), None)
    result = CliRunner().invoke(main, ["continue"])
    assert result.exit_code == 0
    assert launched["resume_id"] == "bbbb2222"
    assert "continuing bbbb2222" in result.output
