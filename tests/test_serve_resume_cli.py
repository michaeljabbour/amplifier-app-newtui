"""CLI wiring: ``serve --resume`` (S3).

Before S3, ``serve --resume`` called ``session_manager.resolve`` with no
try/except at all -- an unknown or ambiguous id crashed with an uncaught
traceback instead of a clean, deterministic exit. These tests drive the CLI
resolution up front (the same ``_resolve_resume_target`` gate ``resume`` and
``run --resume`` use) without booting the real protocol loop: ``kernel.serve
.serve`` is monkeypatched to a stub so no real runtime/session is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.main import (
    RESUME_EXIT_AMBIGUOUS,
    RESUME_EXIT_CORRUPT,
    RESUME_EXIT_NOT_FOUND,
    main,
)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    store = SessionStore(base_dir=tmp_path / "sessions")
    monkeypatch.setattr("amplifier_app_tui.main._session_store", lambda: store)

    async def fake_serve(*args: object, **kwargs: object) -> int:
        return 0

    # ``serve``'s command body does a LAZY ``from .kernel.serve import serve
    # as _serve`` at call time, so patching the module attribute here is
    # picked up fresh on every invocation below.
    monkeypatch.setattr("amplifier_app_tui.kernel.serve.serve", fake_serve)
    return store


def test_serve_resume_unknown_id_no_longer_crashes(store: SessionStore) -> None:
    """Previously an uncaught ``FileNotFoundError`` traceback; now a clean,
    deterministic exit."""
    result = CliRunner().invoke(main, ["serve", "--resume", "deadbeef"])
    assert result.exit_code == RESUME_EXIT_NOT_FOUND
    # A controlled SystemExit(code), never an unhandled traceback -- Click's
    # CliRunner always attaches the SystemExit as `.exception` for a nonzero
    # exit, so "no crash" is `isinstance(..., SystemExit)`, not `is None`.
    assert isinstance(result.exception, SystemExit)
    assert "no session found" in result.output


def test_serve_resume_ambiguous_prefix_no_longer_crashes(store: SessionStore) -> None:
    """Previously an uncaught ``AmbiguousSessionError`` (a ``ValueError``
    subclass) traceback; now the same actionable table ``resume`` prints."""
    store.save("aaaa1111", [], {"bundle": "tui", "name": "one"})
    store.save("aaaa2222", [], {"bundle": "tui", "name": "two"})
    result = CliRunner().invoke(main, ["serve", "--resume", "aaaa"])
    assert result.exit_code == RESUME_EXIT_AMBIGUOUS
    assert isinstance(result.exception, SystemExit)
    assert "matches 2 sessions" in result.output
    assert "aaaa1111" in result.output
    assert "aaaa2222" in result.output


def test_serve_resume_corrupt_session_no_longer_crashes(store: SessionStore) -> None:
    store.save("deadbeef" + "0" * 24, [], {"bundle": "tui"})
    (store.session_dir("deadbeef" + "0" * 24) / "metadata.json").write_text(
        "{not json", encoding="utf-8"
    )
    result = CliRunner().invoke(main, ["serve", "--resume", "deadbeef"])
    assert result.exit_code == RESUME_EXIT_CORRUPT
    assert isinstance(result.exception, SystemExit)
    assert "corrupt" in result.output.lower()


def test_serve_resume_success_reaches_serve(store: SessionStore) -> None:
    full_id = "cafef00d" + "0" * 24
    store.save(full_id, [], {"bundle": "tui"})
    result = CliRunner().invoke(main, ["serve", "--resume", "cafef00d"])
    assert result.exit_code == 0
