"""Regression guard: the packaged tui bundle composes ``tool-team-pulse``,
which does ``from team_pulse_lib import ...`` at mount(). ``team_pulse_lib`` is
an unpublished sibling package of the team-pulse bundle; tui pulls only the
tool-module subdirectory, so nothing installs the lib unless tui declares it
as a dependency. Without it, tool-team-pulse fails to mount on a clean env
(degraded start) instead of degrading gracefully. This asserts the invariant
that made that bug possible — the lib is importable in tui's environment.

If team-pulse is ever dropped from ``data/bundles/tui.md``, drop this test
and the ``team-pulse-lib`` dependency together.
"""

from __future__ import annotations

import importlib.util


def test_team_pulse_lib_importable() -> None:
    assert importlib.util.find_spec("team_pulse_lib") is not None, (
        "team_pulse_lib missing — tool-team-pulse (composed by the packaged tui "
        "bundle) will fail to mount. Ensure the `team-pulse-lib` dependency in "
        "pyproject.toml (with its [tool.uv.sources] git subdir entry) is present."
    )
