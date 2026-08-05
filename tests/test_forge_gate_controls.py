"""Verification controls around the opt-in Forge capability tier."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.forge._forge import (  # pyright: ignore[reportMissingImports]
    forge_required,
    resolve_forge,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_forge_required_accepts_documented_truthy_values(value: str) -> None:
    assert forge_required({"AMPLIFIER_FORGE_REQUIRED": value})


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_forge_required_is_off_by_default(value: str) -> None:
    assert not forge_required({"AMPLIFIER_FORGE_REQUIRED": value})


def test_resolve_forge_prefers_explicit_helper(tmp_path: Path) -> None:
    explicit = tmp_path / "custom" / "forge.py"
    explicit.parent.mkdir()
    explicit.touch()

    assert resolve_forge(home=tmp_path, environ={"FORGE": str(explicit)}) == explicit


@pytest.mark.parametrize(
    "relative",
    [
        ".codex/skills/amplifier-skill-forge/tools/forge.py",
        ".claude/skills/amplifier-skill-forge/tools/forge.py",
        ".amplifier/skills/amplifier-skill-forge/tools/forge.py",
        "dev/amplifier-skill-forge/tools/forge.py",
    ],
)
def test_resolve_forge_supports_every_install_location(tmp_path: Path, relative: str) -> None:
    helper = tmp_path / relative
    helper.parent.mkdir(parents=True)
    helper.touch()

    assert resolve_forge(home=tmp_path, environ={}) == helper


def test_required_capability_wrapper_fails_when_forge_is_missing(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path), "FORGE": ""})

    result = subprocess.run(
        ["bash", "scripts/forge_capability.sh", "--require"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "forge.py not found during a required capability run" in result.stderr


def test_adoption_smoke_propagates_required_mode_to_forge(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    fake_forge = tmp_path / "forge.py"
    fake_forge.touch()

    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
printf 'required=%s args=%s\\n' "${AMPLIFIER_FORGE_REQUIRED:-}" "$*" >> "$FORGE_TEST_LOG"
exit 0
""",
    )
    _write_executable(
        fake_bin / "python3",
        """#!/usr/bin/env bash
if [[ "${2:-}" == "doctor" ]]; then printf 'forge: healthy\\n'; fi
exit 0
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "FORGE": str(fake_forge),
            "FORGE_TEST_LOG": str(log),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    result = subprocess.run(
        ["bash", "scripts/adoption_smoke.sh"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "adoption smoke PASS" in result.stdout
    assert "required=1 args=run pytest -q -m forge tests/forge/" in log.read_text(), (
        "adoption smoke did not make the Forge tier mandatory"
    )


def test_adoption_smoke_rejects_forge_bypass() -> None:
    result = subprocess.run(
        ["bash", "scripts/adoption_smoke.sh", "--no-forge"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "required Forge tier cannot be bypassed" in result.stderr
    assert "PASS" not in result.stdout


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)
