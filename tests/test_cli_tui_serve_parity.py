"""CLI/TUI/serve identity parity over the ONE shared offline fixture (B9).

Compliance B9 gap 2: "Create one shared fixture suite that asserts the
three surfaces (CLI, TUI, serve) agree on the behaviors they are supposed
to share." This file drives the SAME offline fake-module bundle
(``tests/test_cli_tui_serve_identity_fixture.py``, itself built on the
``offline_env``/``offline_workspace`` fixtures ``test_runtime_offline.py``
and the serve tests already share via ``conftest.py``) through three
independent boots -- one per surface -- and asserts the resolved
provider/model identity agrees, mirroring ``test_skill_alias_parity.py``'s
shape for B2 (one shared fixture, driven through every surface side by
side, asserting agreement rather than merely asserting each surface's own
hand-rolled expectation).

Scope, deliberately narrow (do not invent cross-surface behavior that
doesn't exist): all three surfaces already read ``RealRuntime.bundle_name``
/ ``model_name`` verbatim off ONE ``_provider_and_model()`` resolution --
see ``test_cli_tui_serve_identity_fixture.py``'s module docstring for the
three exact call sites. This suite is the proof that they stay in
lockstep; it is not a claim that CLI/TUI/serve share every surface (command
flags, settings precedence, skill/tool availability, etc. remain
single-surface or not-yet-proven today). One more axis IS now proven the
same way: resume-target resolution and its deterministic exit codes --
see ``test_cli_tui_serve_resume_fixture.py`` / ``test_cli_tui_serve_resume_
parity.py`` (compliance B9 gap 2's second axis).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .test_cli_tui_serve_identity_fixture import (
    OFFLINE_BUNDLE,
    cli_run_jsonl_identity,
    real_runtime_identity,
    serve_identity,
)


@pytest.mark.asyncio
async def test_serve_reports_the_resolved_bundle_and_model(offline_env) -> None:
    """serve's ``session.started`` names the offline bundle's real provider/model."""
    bundle, model = await serve_identity(offline_env["project"])
    assert bundle == OFFLINE_BUNDLE
    assert model == "fake/fake-model"


@pytest.mark.asyncio
async def test_cli_run_jsonl_reports_the_resolved_bundle_and_model(
    offline_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``amplifier-tui run --output-format jsonl``'s ``session.started`` matches."""
    bundle, model = await cli_run_jsonl_identity(offline_env["project"], monkeypatch)
    assert bundle == OFFLINE_BUNDLE
    assert model == "fake/fake-model"


@pytest.mark.asyncio
async def test_real_runtime_identity_matches_too(offline_env) -> None:
    """The real (non-demo) TUI adapter's copied identity attributes match."""
    bundle, model = await real_runtime_identity(offline_env["project"])
    assert bundle == OFFLINE_BUNDLE
    assert model == "fake/fake-model"


@pytest.mark.asyncio
async def test_cli_tui_serve_agree_on_the_same_offline_bundle(
    offline_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual cross-surface proof: three independent boots of the SAME
    bundle, through three different code paths, agree with EACH OTHER --
    not just with a hardcoded literal one file happens to repeat."""
    project: Path = offline_env["project"]

    serve_bundle, serve_model = await serve_identity(project)
    cli_bundle, cli_model = await cli_run_jsonl_identity(project, monkeypatch)
    tui_bundle, tui_model = await real_runtime_identity(project)

    assert serve_bundle == cli_bundle == tui_bundle
    assert serve_model == cli_model == tui_model
