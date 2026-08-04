"""CLI/TUI/serve resume-resolution parity over ONE shared session store (B9).

Compliance B9 gap 2 (second axis): mirrors ``test_cli_tui_serve_parity.py``'s
shape for the identity axis, but for resume-target resolution -- see
``test_cli_tui_serve_resume_fixture.py``'s module docstring for why this is
genuinely shared (not invented: `_resolve_resume_target`'s own docstring
says "so all four commands agree") and exactly which three call sites this
drives.

Each state below (not-found / ambiguous / corrupt) is set up ONCE in a
shared store and driven through all three REAL command entry points; the
assertion is agreement with EACH OTHER, not a hardcoded literal repeated
three times. Each surface already has its OWN individual test asserting its
exit code against the ``RESUME_EXIT_*`` constant (``test_run_invocation_
flags.py``, ``test_serve_resume_cli.py``, ``test_session_cli.py``) -- that
proves each surface matches the constant it was written against, not that
the surfaces would still agree with EACH OTHER after a future change. This
file is that second, previously-missing proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.main import (
    RESUME_EXIT_AMBIGUOUS,
    RESUME_EXIT_CORRUPT,
    RESUME_EXIT_NOT_FOUND,
)

from .test_cli_tui_serve_resume_fixture import (
    AMBIGUOUS_PREFIX,
    NOT_FOUND_TARGET,
    cli_run_resume_exit,
    seed_ambiguous_pair,
    seed_corrupt_session,
    serve_resume_exit,
    tui_resume_exit,
)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


def test_cli_tui_serve_agree_on_not_found_exit_code(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty store: all three surfaces refuse the SAME bogus id alike --
    not just each individually matching the constant, but matching each
    other, which is the actual cross-surface guarantee."""
    cli_exit, cli_out = cli_run_resume_exit(store, monkeypatch, NOT_FOUND_TARGET)
    serve_exit, serve_out = serve_resume_exit(store, monkeypatch, NOT_FOUND_TARGET)
    tui_exit, tui_out = tui_resume_exit(store, monkeypatch, NOT_FOUND_TARGET)

    assert cli_exit == serve_exit == tui_exit
    assert cli_exit == RESUME_EXIT_NOT_FOUND
    for output in (cli_out, serve_out, tui_out):
        assert "no session found" in output


def test_cli_tui_serve_agree_on_ambiguous_exit_code(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sessions sharing a prefix: all three surfaces refuse alike."""
    one, two = seed_ambiguous_pair(store)

    cli_exit, cli_out = cli_run_resume_exit(store, monkeypatch, AMBIGUOUS_PREFIX)
    serve_exit, serve_out = serve_resume_exit(store, monkeypatch, AMBIGUOUS_PREFIX)
    tui_exit, tui_out = tui_resume_exit(store, monkeypatch, AMBIGUOUS_PREFIX)

    assert cli_exit == serve_exit == tui_exit
    assert cli_exit == RESUME_EXIT_AMBIGUOUS
    for output in (cli_out, serve_out, tui_out):
        assert one in output and two in output


def test_cli_tui_serve_agree_on_corrupt_exit_code(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session whose metadata.json is unreadable: all three refuse alike."""
    target = seed_corrupt_session(store)

    cli_exit, cli_out = cli_run_resume_exit(store, monkeypatch, target)
    serve_exit, serve_out = serve_resume_exit(store, monkeypatch, target)
    tui_exit, tui_out = tui_resume_exit(store, monkeypatch, target)

    assert cli_exit == serve_exit == tui_exit
    assert cli_exit == RESUME_EXIT_CORRUPT
    for output in (cli_out, serve_out, tui_out):
        assert "corrupt" in output.lower()
