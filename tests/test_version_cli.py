"""``amplifier-newtui version`` subcommand + preserved ``--version`` flag.

The subcommand reads packaging metadata only (no ``import amplifier_core``),
so it runs offline; missing distributions degrade to ``unknown`` rather than
raising.
"""

from __future__ import annotations

from importlib import metadata

from click.testing import CliRunner

import amplifier_app_newtui.main as main_mod
from amplifier_app_newtui import __version__
from amplifier_app_newtui.main import main


def test_version_subcommand_shows_app_core_foundation() -> None:
    result = CliRunner().invoke(main, ["version"])
    assert result.exit_code == 0
    assert f"amplifier-newtui {__version__}" in result.output
    assert "core" in result.output
    assert "foundation" in result.output


def test_version_flag_still_works() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_package_version_unknown_when_absent(monkeypatch) -> None:
    def _raise(_name: str) -> str:
        raise metadata.PackageNotFoundError

    # The helper does ``from importlib import metadata`` then ``metadata.version``;
    # patching the module attribute covers the lazy lookup.
    monkeypatch.setattr("importlib.metadata.version", _raise)
    assert main_mod._package_version("nope-not-installed") == "unknown"
