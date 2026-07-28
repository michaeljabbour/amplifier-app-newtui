"""Tests for kernel/external_editor.py — the pure compose-in-$EDITOR logic.

Behavioral-equivalence pins for the donor contract (see .ai/oc_donor.md): a
temp .md file seeded with the draft, the editor invoked via an injected
runner, the file read back (normalized), and always cleaned up. No real
editor and no terminal are involved — the runner is a fake.
"""

from __future__ import annotations

import os

import pytest

from amplifier_app_newtui.kernel.external_editor import (
    EditorOutcome,
    compose_in_editor,
    normalize_prompt_content,
    resolve_editor,
)

FAKE_ENV = {"EDITOR": "fake-editor"}


# -- normalize_prompt_content: the donor vector table (.ai/oc_donor.md) --------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("hello\n", "hello"),  # single line, strip one LF
        ("hello\r\n", "hello"),  # single line, strip one CRLF
        ("hello", "hello"),  # no trailing newline
        ("a\nb\n", "a\nb\n"),  # multi-line -> keep trailing
        ("a\nb", "a\nb"),  # multi-line, no trailing
        ("hello\n\n", "hello\n\n"),  # body still has a newline -> keep
        ("\n", ""),  # lone LF strips to empty
        ("", ""),  # empty stays empty
        ("hi\r", "hi\r"),  # lone CR is not a handled suffix
    ],
)
def test_normalize_matches_donor_vectors(content: str, expected: str) -> None:
    assert normalize_prompt_content(content) == expected


# -- resolve_editor: $VISUAL -> $EDITOR -> None --------------------------------


def test_resolve_editor_prefers_visual_then_editor_then_none() -> None:
    assert resolve_editor({"VISUAL": "vis", "EDITOR": "ed"}) == "vis"
    assert resolve_editor({"EDITOR": "ed"}) == "ed"
    assert resolve_editor({}) is None
    # whitespace-only values are ignored (fall through to the next source)
    assert resolve_editor({"VISUAL": "   ", "EDITOR": "ed"}) == "ed"
    assert resolve_editor({"VISUAL": "", "EDITOR": ""}) is None


# -- compose_in_editor: the full round-trip via a fake runner ------------------


def test_compose_ok_seeds_draft_and_reads_back_normalized() -> None:
    seen: dict[str, object] = {}

    def runner(argv: list[str], cwd: str | None) -> int:
        path = argv[-1]
        seen["argv"] = argv
        seen["path"] = path
        with open(path, encoding="utf-8", newline="") as handle:
            seen["seed"] = handle.read()
        # the "editor" appends a marker AND a trailing newline (as editors do)
        with open(path, "a", encoding="utf-8", newline="") as handle:
            handle.write("MARK\n")
        return 0

    outcome = compose_in_editor("hello", runner=runner, environ=FAKE_ENV)
    assert outcome == EditorOutcome("ok", text="helloMARK")
    assert seen["seed"] == "hello"  # seeded verbatim
    assert str(seen["path"]).endswith(".md")  # markdown temp file
    assert seen["argv"] == ["fake-editor", seen["path"]]
    assert not os.path.exists(str(seen["path"]))  # temp file cleaned up


def test_compose_splits_editor_command_into_argv() -> None:
    seen: dict[str, object] = {}

    def runner(argv: list[str], cwd: str | None) -> int:
        seen["argv"] = argv
        with open(argv[-1], "a", encoding="utf-8") as handle:
            handle.write("x")
        return 0

    compose_in_editor("d", runner=runner, environ={"EDITOR": "code -w --foo"})
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == ["code", "-w", "--foo"]
    assert argv[-1].endswith(".md")


def test_compose_multiline_keeps_trailing_newline() -> None:
    def runner(argv: list[str], cwd: str | None) -> int:
        with open(argv[-1], "w", encoding="utf-8", newline="") as handle:
            handle.write("one\ntwo\n")
        return 0

    outcome = compose_in_editor("seed", runner=runner, environ=FAKE_ENV)
    assert outcome.status == "ok"
    assert outcome.text == "one\ntwo\n"  # multi-line -> trailing newline kept


def test_compose_crlf_single_line_is_stripped_on_readback() -> None:
    def runner(argv: list[str], cwd: str | None) -> int:
        with open(argv[-1], "w", encoding="utf-8", newline="") as handle:
            handle.write("windows\r\n")  # a real CRLF must survive to normalize
        return 0

    outcome = compose_in_editor("seed", runner=runner, environ=FAKE_ENV)
    assert outcome == EditorOutcome("ok", text="windows")


def test_compose_empty_file_is_empty_outcome() -> None:
    def runner(argv: list[str], cwd: str | None) -> int:
        open(argv[-1], "w", encoding="utf-8").close()  # truncate to empty
        return 0

    assert compose_in_editor("hello", runner=runner, environ=FAKE_ENV).status == "empty"


def test_compose_nonzero_exit_is_exit_error() -> None:
    outcome = compose_in_editor("x", runner=lambda argv, cwd: 3, environ=FAKE_ENV)
    assert outcome.status == "exit_error"
    assert "3" in outcome.detail


def test_compose_spawn_failure_is_spawn_error() -> None:
    def runner(argv: list[str], cwd: str | None) -> int:
        raise FileNotFoundError("no such editor")

    outcome = compose_in_editor("x", runner=runner, environ=FAKE_ENV)
    assert outcome.status == "spawn_error"
    assert "no such editor" in outcome.detail


def test_compose_no_editor_never_runs_the_runner() -> None:
    def runner(argv: list[str], cwd: str | None) -> int:
        raise AssertionError("runner must not run without an editor")

    outcome = compose_in_editor("x", runner=runner, environ={})
    assert outcome.status == "no_editor"
    assert "$EDITOR" in outcome.detail


def test_compose_validates_cwd_and_falls_back_when_missing(tmp_path) -> None:
    seen: list[str | None] = []

    def runner(argv: list[str], cwd: str | None) -> int:
        seen.append(cwd)
        with open(argv[-1], "a", encoding="utf-8") as handle:
            handle.write("x")
        return 0

    compose_in_editor("d", runner=runner, environ=FAKE_ENV, cwd=str(tmp_path))
    compose_in_editor("d", runner=runner, environ=FAKE_ENV, cwd="/no/such/dir/xyz-42")
    assert seen == [str(tmp_path), None]  # existing dir kept; missing -> process cwd


def test_compose_removes_temp_file_even_on_error() -> None:
    captured: list[str] = []

    def runner(argv: list[str], cwd: str | None) -> int:
        captured.append(argv[-1])
        return 7  # non-zero -> exit_error, but finally still unlinks

    compose_in_editor("x", runner=runner, environ=FAKE_ENV)
    assert captured and not os.path.exists(captured[0])
