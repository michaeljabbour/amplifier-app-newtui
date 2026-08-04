"""ONE shared fixture: resume-target resolution across CLI/TUI/serve.

Compliance B9 gap 2 (second axis, added alongside the existing provider/model
identity fixture in ``test_cli_tui_serve_identity_fixture.py``): resume-target
resolution and its deterministic exit codes.

The shared behavior proven here: ``main._resolve_resume_target`` is the ONE
function behind every resume-family command -- its own docstring says so
verbatim: "Shared by ``resume``, ``session resume``, ``run --resume`` and
``serve --resume`` so all four commands agree." Three of those four are the
app's three surfaces:

- CLI   -- ``run --resume <id>``'s gate, before any prompt executes
  (``main.run``: ``resume_id = _resolve_resume_target(...)`` runs before
  ``_run_once``/``_interactive_launch``).
- serve -- ``serve --resume <id>``'s identical gate, before the protocol loop
  starts (``main.serve``: same call, before ``kernel.serve.serve`` is even
  imported).
- TUI   -- ``resume <id>``'s identical gate, before ``_launch_tui`` boots the
  full-screen app (``main.resume``).

All three exit through the SAME documented, deterministic codes
(``RESUME_EXIT_NOT_FOUND`` = 2, ``RESUME_EXIT_AMBIGUOUS`` = 3,
``RESUME_EXIT_CORRUPT`` = 4 -- USER-GUIDE.md's "Resume exit codes" table)
instead of the historical blanket 1. Each already has its OWN per-surface
test proving its exit code against these constants individually
(``test_run_invocation_flags.py``, ``test_serve_resume_cli.py``,
``test_session_cli.py``) -- what none of them prove is that the THREE
surfaces agree with EACH OTHER over the SAME crafted session-store state,
which is exactly what would catch a future refactor that threads the shared
gate correctly into two commands but not the third (each hand-rolled
per-surface test would still pass, comparing only against the constant it
was written against).

Resolution fails BEFORE ``RealRuntime`` / ``kernel.serve.serve`` /
``_launch_tui`` is ever reached on every one of these three commands (the
``SystemExit`` fires first), so nothing here needs a runtime, a Textual app,
or a protocol loop -- only ``main._session_store`` is redirected at the
crafted store, exactly like the existing per-surface tests already do.

This file intentionally defines no tests (mirrors ``test_flow_helpers.py`` /
``test_skill_alias_fixture.py`` / ``test_cli_tui_serve_identity_fixture.py``).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.main import main

NOT_FOUND_TARGET = "deadbeef"
"""Matches no session in a fresh, empty store."""

AMBIGUOUS_PREFIX = "aaaa"
"""Shared prefix of the two sessions :func:`seed_ambiguous_pair` writes."""

CORRUPT_TARGET = "cafef00d"
"""Resolves to exactly one session whose ``metadata.json`` is unreadable."""


def seed_ambiguous_pair(store: SessionStore) -> tuple[str, str]:
    """Write two sessions sharing :data:`AMBIGUOUS_PREFIX`; return their ids."""
    one, two = f"{AMBIGUOUS_PREFIX}1111", f"{AMBIGUOUS_PREFIX}2222"
    store.save(one, [], {"bundle": "tui", "name": "one"})
    store.save(two, [], {"bundle": "tui", "name": "two"})
    return one, two


def seed_corrupt_session(store: SessionStore) -> str:
    """Write one session, then corrupt its ``metadata.json`` (no ``.backup``
    to recover from -- the FIRST write of a session has none yet)."""
    store.save(CORRUPT_TARGET, [], {"bundle": "tui"})
    (store.session_dir(CORRUPT_TARGET) / "metadata.json").write_text("{not json", encoding="utf-8")
    return CORRUPT_TARGET


def cli_run_resume_exit(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, target: str
) -> tuple[int, str]:
    """Invoke the REAL ``amplifier-tui run --resume <target>`` command."""
    monkeypatch.setattr("amplifier_app_tui.main._session_store", lambda: store)
    result = CliRunner().invoke(main, ["run", "--resume", target, "hello"])
    return result.exit_code, result.output


def serve_resume_exit(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, target: str
) -> tuple[int, str]:
    """Invoke the REAL ``amplifier-tui serve --resume <target>`` command."""
    monkeypatch.setattr("amplifier_app_tui.main._session_store", lambda: store)
    result = CliRunner().invoke(main, ["serve", "--resume", target])
    return result.exit_code, result.output


def tui_resume_exit(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, target: str
) -> tuple[int, str]:
    """Invoke the REAL ``amplifier-tui resume <target>`` command (the TUI
    launcher's resume path)."""
    monkeypatch.setattr("amplifier_app_tui.main._session_store", lambda: store)
    result = CliRunner().invoke(main, ["resume", target])
    return result.exit_code, result.output


__all__ = [
    "AMBIGUOUS_PREFIX",
    "CORRUPT_TARGET",
    "NOT_FOUND_TARGET",
    "cli_run_resume_exit",
    "seed_ambiguous_pair",
    "seed_corrupt_session",
    "serve_resume_exit",
    "tui_resume_exit",
]
