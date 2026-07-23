"""Fixtures for the forge capability tier.

Everything here degrades to a clean ``skip`` (never a ``fail``) when the
substrate is missing -- no forge helper, an unhealthy daemon, a missing
binary, or absent credentials -- so the default gate (which runs with
``-m "not forge"`` and never reaches this file) and CI stay wholly
unaffected, and a dev machine without forge still reports the tier as
skipped rather than red.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from ._forge import ForgeClient, ForgeSession, resolve_forge

# One tag for every PTY the tier opens, so a crashed run is reaped whole
# via ``close-tag`` in the session finalizer (SKILL.md fan-out rule).
BATCH_TAG = "newtui-forge-cap"

# tests/forge/conftest.py -> tests/forge -> tests -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
NEWTUI_BINARY = REPO_ROOT / ".venv" / "bin" / "amplifier-newtui"

# Composer placeholder -- a stable single-word boot anchor.
COMPOSER_ANCHOR = "Message"
# Fixed layout so rendered widths match the golden family (DEVELOPMENT.md).
COLS, ROWS = 120, 40


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
    """A healthy forge daemon, or skip the whole tier."""
    forge_path = resolve_forge()
    if forge_path is None:
        pytest.skip("amplifier-skill-forge not found (set $FORGE or install the skill)")
    client = ForgeClient(forge_path)
    if not client.doctor():
        pytest.skip("forge doctor unhealthy -- daemon/PTY unavailable")
    try:
        yield client
    finally:
        client.close_tag(BATCH_TAG)


@pytest.fixture(scope="session")
def newtui_binary() -> Path:
    """The shipped console-script, or skip (nothing to drive)."""
    if not NEWTUI_BINARY.exists():
        pytest.skip(f"amplifier-newtui binary not found at {NEWTUI_BINARY}")
    return NEWTUI_BINARY


@pytest.fixture
def demo_session(forge_client: ForgeClient, newtui_binary: Path) -> Iterator[ForgeSession]:
    """A freshly booted ``amplifier-newtui --demo`` PTY at a fixed size.

    Function-scoped so each capability test gets a clean turn state (the
    demo advances build -> auto -> plan -> ... on each unmatched submit).
    """
    session = forge_client.new(
        program=str(newtui_binary),
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
        from amplifier_app_newtui.kernel import setup

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
