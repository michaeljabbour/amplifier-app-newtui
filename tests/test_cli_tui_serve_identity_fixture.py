"""ONE shared fixture: booting the SAME offline bundle across CLI/TUI/serve.

Compliance B9 gap 2: "one shared fixture suite that asserts the three
surfaces (CLI, TUI, serve) agree on the behaviors they are supposed to
share" -- the same shape ``test_skill_alias_fixture.py`` establishes for
B2 (one shared fixture module several per-surface/parity tests import,
instead of each surface hand-rolling its own copy that merely happens to
agree today).

The shared behavior proven here: **provider/model identity resolution**.
``RealRuntime.__init__`` resolves the bundle's mount plan through ONE pure
function (``kernel.runtime._provider_and_model``) exactly once, and sets
``self.bundle_name`` / ``self.model_name``. Three call sites already read
those same two attributes back out verbatim, with no independent
recomputation:

- CLI -- ``main._run_once``'s ``jsonl`` branch (the real wire path behind
  ``amplifier-tui run --output-format jsonl``) emits
  ``JsonlRecords.session_started(bundle=runtime.bundle_name,
  model=runtime.model_name)`` as its first record.
- serve -- ``kernel.serve.serve_loop`` emits the byte-identical
  ``session_started(bundle=runtime.bundle_name, model=runtime.model_name)``
  as ITS first record (see the ``kernel/serve.py`` module docstring: "The
  runtime.event envelope is byte-identical to the run JSONL contract").
- TUI -- ``ui.runtime_adapter.RealRuntimeAdapter.start()`` copies
  ``self.bundle_name = runtime.bundle_name`` /
  ``self.model_name = runtime.model_name`` straight off the booted runtime
  for the real (non-demo) banner/footer -- proven in
  ``test_runtime_adapter_real.py::test_start_happy_path_copies_identity``.

Nothing here is invented: all three read sites already exist verbatim in
the shipped code. Before this file, nothing proved the three stay in
lockstep -- a future change to any ONE wire path (e.g. a refactor that
swaps in a stale/hardcoded value on just one surface) could silently
diverge without a single red test, exactly the gap B2 closed for skill
aliases. Each ``*_identity`` helper below drives ONE surface's REAL code
path against the SAME offline fake-module bundle
(``tests.test_runtime_offline``'s session-scoped ``offline_workspace`` /
``offline_env`` fixtures, already shared with the serve tests via
``conftest.py``) and returns its observed ``(bundle, model)`` pair;
``test_cli_tui_serve_parity.py`` drives all three and asserts they agree.

This file intentionally defines no tests (mirrors ``test_flow_helpers.py``
/ ``test_skill_alias_fixture.py``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import IO, Any, cast

import pytest
from click.testing import CliRunner

from amplifier_app_tui.kernel.serve import serve_loop
from amplifier_app_tui.main import main

from .test_runtime_offline import _started_runtime
from .test_serve_offline import _Capture, _PipeStdin, _wait_until

OFFLINE_BUNDLE = "offline"
"""The bundle name every leg below resolves. ``offline_workspace`` (session-
scoped, ``tests/test_runtime_offline.py``) writes exactly one bundle under
the fake project's ``.amplifier/bundles/offline.md``, named this."""


async def serve_identity(project: Path) -> tuple[str, str]:
    """Boot ``serve_loop`` against the shared offline bundle; return its
    first ``session.started`` record's ``(bundle, model)``.

    No ops are fed -- the identity record is emitted unconditionally before
    the protocol loop even reads stdin, so waiting for one captured line and
    then closing stdin is enough to observe it and let the loop exit clean.
    """
    runtime = await _started_runtime(project)
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))
    )
    await _wait_until(lambda: bool(out.lines))
    stdin.close()
    exit_code = await asyncio.wait_for(server, timeout=10)
    assert exit_code == 0
    started = out.lines[0]
    assert started["type"] == "session.started"
    return started["bundle"], started["model"]


async def cli_run_jsonl_identity(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Invoke the REAL ``amplifier-tui run --output-format jsonl`` command
    against the shared offline bundle; return the emitted
    ``session.started`` record's ``(bundle, model)``.

    ``CliRunner.invoke`` is synchronous (``main.run`` drives its own
    ``asyncio.run(...)`` internally), so it is moved off this coroutine's
    running event loop via ``asyncio.to_thread`` -- calling it directly
    here would raise "asyncio.run() cannot be called from a running event
    loop". ``monkeypatch.chdir`` points bundle discovery at the fake
    project: ``run`` has no ``--project-dir`` flag, it resolves relative to
    cwd exactly like the real CLI does.
    """
    monkeypatch.chdir(project)
    runner = CliRunner()

    def _invoke() -> Any:
        return runner.invoke(
            main,
            ["run", "--bundle", OFFLINE_BUNDLE, "--output-format", "jsonl", "hello"],
        )

    result = await asyncio.wait_for(asyncio.to_thread(_invoke), timeout=15)
    assert result.exit_code == 0, result.output
    first_line = result.output.splitlines()[0]
    record = json.loads(first_line)
    assert record["type"] == "session.started"
    return record["bundle"], record["model"]


async def real_runtime_identity(project: Path) -> tuple[str, str]:
    """Boot a bare ``RealRuntime`` against the shared offline bundle and
    read its identity attributes directly -- the same two attributes
    ``ui.runtime_adapter.RealRuntimeAdapter.start()`` copies verbatim onto
    itself for the real (non-demo) TUI's banner/footer (proven in
    ``test_runtime_adapter_real.py::test_start_happy_path_copies_identity``).
    A direct boot is the simplest honest stand-in for what the real TUI
    would show, given that copy-through is already covered elsewhere;
    ``RealRuntimeAdapter`` itself has no ``project_dir`` override to point
    it at this fixture (it always resolves relative to cwd on its own
    thread), so re-driving it here would need a global chdir on a
    background thread for no extra proof value.
    """
    runtime = await _started_runtime(project)
    try:
        return runtime.bundle_name, runtime.model_name
    finally:
        await runtime.cleanup()


__all__ = [
    "OFFLINE_BUNDLE",
    "cli_run_jsonl_identity",
    "real_runtime_identity",
    "serve_identity",
]
