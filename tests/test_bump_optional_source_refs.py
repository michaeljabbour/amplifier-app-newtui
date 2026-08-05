"""Offline contract for the optional-source pin maintenance helper."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "bump_optional_source_refs.py"
    spec = importlib.util.spec_from_file_location("bump_optional_source_refs", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


def test_source_url_and_ref_requires_a_full_sha() -> None:
    sha = "a" * 40
    assert script.source_url_and_ref(f"git+https://example.invalid/repo@{sha}#subdirectory=x") == (
        "https://example.invalid/repo",
        sha,
    )
    with pytest.raises(ValueError, match="not pinned"):
        script.source_url_and_ref("git+https://example.invalid/repo@main")


def test_rewritten_files_replaces_each_pin_once_without_writing(tmp_path: Path) -> None:
    source = tmp_path / "pins.py"
    old_a, old_b = "a" * 40, "b" * 40
    source.write_text(f'A = "repo@{old_a}"\nB = "repo@{old_b}"\n', encoding="utf-8")
    pins = (
        script.SourcePin("a", f"git+https://example.invalid/a@{old_a}", source),
        script.SourcePin("b", f"git+https://example.invalid/b@{old_b}", source),
    )

    result = script.rewritten_files(pins, {"a": "c" * 40, "b": "d" * 40})

    assert result[source] == f'A = "repo@{"c" * 40}"\nB = "repo@{"d" * 40}"\n'
    assert old_a in source.read_text(encoding="utf-8")  # pure until caller commits the rewrite


def test_rewritten_files_fails_closed_when_a_pin_is_ambiguous(tmp_path: Path) -> None:
    source = tmp_path / "pins.py"
    old = "a" * 40
    source.write_text(f'ONE = "{old}"\nTWO = "{old}"\n', encoding="utf-8")
    pin = script.SourcePin("one", f"git+https://example.invalid/one@{old}", source)

    with pytest.raises(RuntimeError, match="exactly one"):
        script.rewritten_files((pin,), {"one": "b" * 40})
