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

Scope stays evidence-bounded (do not invent cross-surface behavior that
doesn't exist), but now covers six proven axes:

- provider/model identity from ONE ``_provider_and_model()`` resolution;
- resume-target resolution and deterministic exit codes;
- normalized tool-event ordering over one real offline turn; and
- the durable ``ui-events.jsonl`` sequence for that same turn;
- real child-routing fallback through Foundation's preference resolver; and
- cooperative live cancellation through each bidirectional runtime owner.

See the three ``*_fixture.py`` modules for the exact shipped call paths.
The one-shot ``run`` command has no live op channel, so cancellation applies
to the TUI adapter and serve wire; routing applies to all three surfaces.
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
from .test_cli_tui_serve_lifecycle_fixture import (
    cli_lifecycle,
    cli_routing_fallback,
    serve_live_cancellation,
    serve_lifecycle,
    serve_routing_fallback,
    tui_live_cancellation,
    tui_lifecycle,
    tui_routing_fallback,
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


@pytest.mark.asyncio
async def test_cli_tui_serve_agree_on_tool_events_and_durable_logging(
    offline_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real offline turn proves two more B9 behavior families.

    Each surface must expose the same ordered durable lifecycle and persist
    that same normalized sequence. Stream deltas are intentionally excluded:
    the ledger never stores per-token Channel-A traffic.
    """
    project: Path = offline_env["project"]
    cli = await cli_lifecycle(project, monkeypatch)
    tui = await tui_lifecycle(project, monkeypatch)
    serve = await serve_lifecycle(project)

    durable = (
        "prompt_submit",
        "provider_response_usage",
        "tool_pre",
        "tool_post",
        "content_block_end",
        "orchestrator_complete",
        "prompt_complete",
    )

    def _only_required(kinds: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(kind for kind in kinds if kind in durable)

    assert _only_required(cli.event_kinds) == durable
    assert _only_required(tui.event_kinds) == durable
    assert _only_required(serve.event_kinds) == durable
    assert _only_required(cli.logged_kinds) == durable
    assert _only_required(tui.logged_kinds) == durable
    assert _only_required(serve.logged_kinds) == durable


@pytest.mark.asyncio
async def test_cli_tui_serve_agree_on_real_routing_fallback(
    offline_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first model glob misses; the second preference must serve the child.

    The offline orchestrator only triggers the runtime's real SessionSpawner.
    Foundation performs the ordered model resolution and the mounted provider
    reports the model it actually received, so this does not copy routing
    selection into the fixture.
    """
    project: Path = offline_env["project"]
    cli = await cli_routing_fallback(project, monkeypatch)
    tui = await tui_routing_fallback(project, monkeypatch)
    serve = await serve_routing_fallback(project)

    expected = "routing-fallback=Hello from the fake provider via fake-routed."
    assert cli.response == tui.response == serve.response == expected
    for observed in (cli, tui, serve):
        assert "orchestrator_complete" in observed.event_kinds
        assert "prompt_complete" in observed.event_kinds
        assert "orchestrator_complete" in observed.logged_kinds
        assert "prompt_complete" in observed.logged_kinds


@pytest.mark.asyncio
async def test_tui_and_serve_agree_on_live_core_cancellation(
    offline_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every bidirectional owner cancels the same live amplifier-core token.

    ``run --output-format jsonl`` is a one-shot prompt stream and therefore
    has no live interrupt op.  The full-screen TUI adapter and serve protocol
    are the applicable owners; both must expose and persist core's real
    ``cancel:completed`` event before the synthesized prompt close-out.
    """
    project: Path = offline_env["project"]
    tui = await tui_live_cancellation(project, monkeypatch)
    serve = await serve_live_cancellation(project)

    assert tui.response == serve.response == "cancelled-by-core-token"
    for observed in (tui, serve):
        required = ("orchestrator_complete", "cancel_completed", "prompt_complete")
        outward = tuple(kind for kind in observed.event_kinds if kind in required)
        durable = tuple(kind for kind in observed.logged_kinds if kind in required)
        assert outward == required
        assert durable == required
