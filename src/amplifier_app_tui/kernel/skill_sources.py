"""Materialize immutable remote skill sources through Foundation.

``tool-skills`` is the native discovery/load surface, but its current git
fetcher passes every ``@ref`` to ``git clone --branch``.  Full commit SHAs are
not branch names, so an otherwise-correct immutable source fails on a cold
machine.  Foundation's source resolver already implements the proper
fetch-and-checkout path for SHAs.  This bridge resolves only those immutable
sources to local directories before native ``tool-skills`` mounts; tags,
workspace paths, user paths, discovery, parsing, and loading remain owned by
the native module.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PINNED_GIT_SOURCE_RE = re.compile(
    r"^git\+https://[^\s@#]+@[0-9a-fA-F]{40}(?:#subdirectory=[^\s]+)?$"
)


@dataclass(frozen=True)
class SkillSourceMaterialization:
    """Outcome of resolving full-SHA skill sources in one mount plan."""

    materialized: tuple[tuple[str, str], ...] = ()
    failures: tuple[tuple[str, str], ...] = ()


def _skill_source_lists(mount_plan: dict[str, Any]) -> tuple[list[Any], ...]:
    lists: list[list[Any]] = []
    tools = mount_plan.get("tools")
    if not isinstance(tools, list):
        return ()
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        module = str(entry.get("module") or "").removeprefix("amplifier-module-")
        if module != "tool-skills":
            continue
        config = entry.get("config")
        if not isinstance(config, dict):
            continue
        sources = config.get("skills")
        if isinstance(sources, list):
            lists.append(sources)
    return tuple(lists)


async def materialize_pinned_skill_sources(
    mount_plan: dict[str, Any],
    *,
    amplifier_home: Path,
    project_dir: Path,
    progress: Callable[[str, str], None] | None = None,
) -> SkillSourceMaterialization:
    """Replace full-SHA git skill sources with Foundation-resolved paths.

    Failures are returned and the original source is retained so callers can
    surface degraded discovery without corrupting the plan.  Duplicate source
    strings are resolved once and reused across composed tool entries.
    """

    slots: list[tuple[list[Any], int, str]] = []
    for sources in _skill_source_lists(mount_plan):
        for index, value in enumerate(sources):
            source = str(value) if isinstance(value, str) else ""
            if _PINNED_GIT_SOURCE_RE.fullmatch(source):
                slots.append((sources, index, source))
    if not slots:
        return SkillSourceMaterialization()

    from amplifier_foundation.sources import SimpleSourceResolver

    resolver = SimpleSourceResolver(
        cache_dir=amplifier_home / "cache" / "skill-sources",
        base_path=project_dir,
    )
    cache: dict[str, str | Exception] = {}
    for _sources, _index, source in slots:
        if source in cache:
            continue
        if progress is not None:
            progress("skills", source)
        try:
            resolved = await resolver.resolve(source)
            cache[source] = str(resolved.active_path)
        except Exception as error:  # noqa: BLE001 -- optional source degrades visibly
            cache[source] = error

    materialized: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    for sources, index, source in slots:
        outcome = cache[source]
        if isinstance(outcome, Exception):
            failure = (source, str(outcome) or type(outcome).__name__)
            if failure not in failures:
                failures.append(failure)
            continue
        sources[index] = outcome
        pair = (source, outcome)
        if pair not in materialized:
            materialized.append(pair)

    return SkillSourceMaterialization(tuple(materialized), tuple(failures))


__all__ = [
    "SkillSourceMaterialization",
    "materialize_pinned_skill_sources",
]
