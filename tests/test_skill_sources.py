"""Immutable skill-source materialization through Foundation's resolver."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import yaml

from amplifier_app_tui.kernel.skill_sources import materialize_pinned_skill_sources

SHA = "a" * 40
PINNED = f"git+https://github.com/example/skills@{SHA}#subdirectory=skills"
APP_CLI_SHA = "5462f1e04099269e6487519676875fccd0980bd5"
GOALIFY_SOURCE = (
    "git+https://github.com/microsoft/amplifier-app-cli@"
    f"{APP_CLI_SHA}#subdirectory=amplifier_app_cli/data/skills"
)


def _bundle_frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text.split("---", 2)[1])
    assert isinstance(parsed, dict)
    return parsed


def test_packaged_bundle_sources_native_goalify_and_workspace_user_skills() -> None:
    root = Path(__file__).resolve().parents[1]
    source_bundle = root / "bundle.md"
    packaged_bundle = root / "src/amplifier_app_tui/data/bundles/tui.md"
    assert source_bundle.read_bytes() == packaged_bundle.read_bytes()

    frontmatter = _bundle_frontmatter(source_bundle)
    tools = frontmatter["tools"]
    assert isinstance(tools, list)
    skills_entry = next(
        entry for entry in tools if isinstance(entry, dict) and entry.get("module") == "tool-skills"
    )
    skill_sources = skills_entry["config"]["skills"]

    assert GOALIFY_SOURCE in skill_sources
    assert skill_sources[-2:] == [".amplifier/skills", "~/.amplifier/skills"]


def test_full_sha_source_is_resolved_once_and_replaced(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    resolved_path = tmp_path / "resolved-skills"
    resolved_path.mkdir()

    class _Resolver:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def resolve(self, source: str) -> SimpleNamespace:
            calls.append(source)
            return SimpleNamespace(active_path=resolved_path)

    monkeypatch.setattr("amplifier_foundation.sources.SimpleSourceResolver", _Resolver)
    plan = {
        "tools": [
            {
                "module": "tool-skills",
                "config": {"skills": [PINNED, PINNED, "~/.amplifier/skills"]},
            }
        ]
    }

    result = asyncio.run(
        materialize_pinned_skill_sources(
            plan,
            amplifier_home=tmp_path / "home",
            project_dir=tmp_path,
        )
    )

    assert calls == [PINNED]
    assert plan["tools"][0]["config"]["skills"] == [
        str(resolved_path),
        str(resolved_path),
        "~/.amplifier/skills",
    ]
    assert result.materialized == ((PINNED, str(resolved_path)),)
    assert result.failures == ()


def test_branch_tag_and_local_sources_remain_native_tool_skills_inputs(tmp_path: Path) -> None:
    original = [
        "git+https://github.com/example/skills@main#subdirectory=skills",
        "git+https://github.com/example/skills@v1.2.0#subdirectory=skills",
        ".amplifier/skills",
    ]
    plan = {"tools": [{"module": "tool-skills", "config": {"skills": list(original)}}]}

    result = asyncio.run(
        materialize_pinned_skill_sources(
            plan,
            amplifier_home=tmp_path / "home",
            project_dir=tmp_path,
        )
    )

    assert plan["tools"][0]["config"]["skills"] == original
    assert result.materialized == () and result.failures == ()


def test_failure_is_reported_and_original_source_is_retained(monkeypatch, tmp_path: Path) -> None:
    class _Resolver:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def resolve(self, _source: str) -> SimpleNamespace:
            raise RuntimeError("offline")

    monkeypatch.setattr("amplifier_foundation.sources.SimpleSourceResolver", _Resolver)
    plan = {"tools": [{"module": "tool-skills", "config": {"skills": [PINNED]}}]}

    result = asyncio.run(
        materialize_pinned_skill_sources(
            plan,
            amplifier_home=tmp_path / "home",
            project_dir=tmp_path,
        )
    )

    assert plan["tools"][0]["config"]["skills"] == [PINNED]
    assert result.materialized == ()
    assert result.failures == ((PINNED, "offline"),)
