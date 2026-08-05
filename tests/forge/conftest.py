"""Fixtures for the forge capability tier.

Ordinary opt-in developer runs degrade to a clean ``skip`` when the PTY
substrate is missing.  Release/adoption runs set
``AMPLIFIER_FORGE_REQUIRED=1`` and turn a missing helper, unhealthy daemon,
or missing shipped binary into a hard failure.  Provider credentials remain
a separate, explicit opt-in gate for the paid real lane.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest

from ._forge import ForgeClient, ForgeSession, forge_required, resolve_forge

# One tag for every PTY the tier opens, so a crashed run is reaped whole
# via ``close-tag`` in the session finalizer (SKILL.md fan-out rule).
BATCH_TAG = "tui-forge-cap"

# tests/forge/conftest.py -> tests/forge -> tests -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
TUI_BINARY = REPO_ROOT / ".venv" / "bin" / "amplifier-tui"
TUI_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
CUSTOM_DECISION_FIXTURE = REPO_ROOT / "tests" / "forge" / "custom_decision_fixture.py"

# Composer placeholder -- a stable single-word boot anchor.
COMPOSER_ANCHOR = "Message"
# Fixed layout so rendered widths match the golden family (DEVELOPMENT.md).
COLS, ROWS = 120, 40
NARROW_COLS, NARROW_ROWS = 80, 18


def _forge_unavailable(reason: str) -> NoReturn:
    """Skip a developer run or fail a required release/adoption run."""
    if forge_required():
        pytest.fail(
            f"required Forge capability tier could not execute: {reason}",
            pytrace=False,
        )
    pytest.skip(reason)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark everything under ``tests/forge/`` as ``forge``.

    Belt-and-suspenders so no capability test can ever leak into the
    default ``-m "not forge"`` gate even if a module forgets the marker.
    """
    forge_root = Path(__file__).resolve().parent
    for item in items:
        try:
            in_tier = forge_root in Path(str(item.fspath)).resolve().parents
        except OSError:  # pragma: no cover - defensive
            in_tier = False
        if in_tier:
            item.add_marker("forge")


@pytest.fixture(scope="session")
def forge_client() -> Iterator[ForgeClient]:
    """A healthy daemon, or skip/fail according to the required-mode policy."""
    forge_path = resolve_forge()
    if forge_path is None:
        _forge_unavailable("amplifier-skill-forge not found (set $FORGE or install the skill)")
    client = ForgeClient(forge_path)
    if not client.doctor():
        _forge_unavailable("forge doctor unhealthy -- daemon/PTY unavailable")
    try:
        yield client
    finally:
        client.close_tag(BATCH_TAG)


@pytest.fixture(scope="session")
def tui_binary() -> Path:
    """The shipped console-script, or skip/fail when there is nothing to drive."""
    if not TUI_BINARY.exists():
        _forge_unavailable(f"amplifier-tui binary not found at {TUI_BINARY}")
    return TUI_BINARY


@pytest.fixture
def demo_session(forge_client: ForgeClient, tui_binary: Path) -> Iterator[ForgeSession]:
    """A freshly booted ``amplifier-tui --demo`` PTY at a fixed size.

    Function-scoped so each capability test gets a clean turn state (the
    demo advances build -> auto -> plan -> ... on each unmatched submit).
    """
    session = forge_client.new(
        program=str(tui_binary),
        args=("--demo",),
        cwd=str(REPO_ROOT),
        cols=COLS,
        rows=ROWS,
        tag=BATCH_TAG,
    )
    try:
        booted = session.wait(COMPOSER_ANCHOR, total_timeout_ms=60_000)
        assert booted, "demo runtime did not boot to the composer within 60s"
        yield session
    finally:
        session.close()


@pytest.fixture
def narrow_demo_session(forge_client: ForgeClient, tui_binary: Path) -> Iterator[ForgeSession]:
    """A fresh demo PTY at the smallest supported acceptance viewport."""
    session = forge_client.new(
        program=str(tui_binary),
        args=("--demo",),
        cwd=str(REPO_ROOT),
        cols=NARROW_COLS,
        rows=NARROW_ROWS,
        tag=BATCH_TAG,
        name="tui-cap-narrow",
    )
    try:
        booted = session.wait(COMPOSER_ANCHOR, total_timeout_ms=60_000)
        assert booted, "narrow demo runtime did not boot to the composer within 60s"
        yield session
    finally:
        session.close()


@pytest.fixture
def custom_decision_session(forge_client: ForgeClient, tui_binary: Path) -> Iterator[ForgeSession]:
    """The real Textual app with one deterministic, native-shaped question."""
    del tui_binary  # the installed console script proves this venv is runnable
    session = forge_client.new(
        program=str(TUI_PYTHON),
        args=(str(CUSTOM_DECISION_FIXTURE),),
        cwd=str(REPO_ROOT),
        cols=COLS,
        rows=ROWS,
        tag=BATCH_TAG,
        name="tui-cap-custom-decision",
    )
    try:
        booted = session.wait(COMPOSER_ANCHOR, total_timeout_ms=60_000)
        assert booted, "custom-decision fixture did not boot to the composer within 60s"
        yield session
    finally:
        session.close()


def real_lane_skip_reason() -> str | None:
    """Why the real lane should skip, or ``None`` when it may run.

    Two honest gates, distinct reasons:

    - No configured provider / stored key  -> the acceptance's
      "no credentials -> demo only" case (skips cleanly).
    - Credentials present but no explicit opt-in -> the real lane drives
      a real session (network + spend); require ``AMPLIFIER_FORGE_REAL=1``
      so the default ``-m forge`` run stays cheap, offline, and green.
    """
    try:
        from amplifier_app_tui.kernel import setup

        providers = setup.configured_providers()
        stored_keys = setup.setup_status().stored_keys
    except Exception as exc:  # noqa: BLE001 — defensive: unreadable provider config becomes a skip reason  # pragma: no cover
        return f"provider configuration unreadable: {exc!r}"
    if not providers or not stored_keys:
        return "no provider credentials configured (real lane skips per acceptance)"
    if os.environ.get("AMPLIFIER_FORGE_REAL", "").strip().lower() not in ("1", "true", "yes"):
        return (
            "real lane drives a real session (network + spend); "
            "set AMPLIFIER_FORGE_REAL=1 to enable"
        )
    return None
