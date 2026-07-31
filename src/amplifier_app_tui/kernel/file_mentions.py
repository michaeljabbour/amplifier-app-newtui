"""Workspace file discovery and ranking for composer ``@file`` mentions.

This module owns filesystem access; the Textual layer receives only relative
paths and filtered results. Discovery is deliberately bounded and never
follows symlinks, so opening autocomplete cannot wander outside the project or
stall forever in generated dependency trees.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
    }
)
MAX_DISCOVERED_FILES = 20_000


def discover_workspace_files(
    project_dir: Path,
    *,
    max_files: int = MAX_DISCOVERED_FILES,
) -> tuple[str, ...]:
    """Return stable POSIX-style paths beneath *project_dir*.

    Generated/dependency directories are pruned, symlinked files are skipped,
    and traversal stops at *max_files*. Permission races are ignored because
    autocomplete is an optional convenience, never a session-start gate.
    """
    root = project_dir.resolve()
    found: list[str] = []
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for current, directories, filenames in walker:
            directories[:] = sorted(
                name
                for name in directories
                if name not in IGNORED_DIRECTORIES and not (Path(current) / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(current) / filename
                if path.is_symlink():
                    continue
                try:
                    found.append(path.relative_to(root).as_posix())
                except (OSError, ValueError):
                    continue
                if len(found) >= max_files:
                    return tuple(found)
    except OSError:
        return tuple(found)
    return tuple(found)


def filter_file_mentions(paths: Sequence[str], query: str, *, limit: int = 8) -> tuple[str, ...]:
    """Rank file paths for a case-insensitive composer query.

    Basename prefix matches lead, then path prefix, basename substring, and
    path substring. Shorter paths win within a tier; the original path breaks
    ties deterministically.
    """
    needle = query.casefold().lstrip("@")
    ranked: list[tuple[int, int, str, str]] = []
    for path in paths:
        folded = path.casefold()
        basename = path.rsplit("/", 1)[-1].casefold()
        if not needle:
            tier = 0
        elif basename.startswith(needle):
            tier = 0
        elif folded.startswith(needle):
            tier = 1
        elif needle in basename:
            tier = 2
        elif needle in folded:
            tier = 3
        else:
            continue
        ranked.append((tier, len(path), folded, path))
    ranked.sort()
    return tuple(item[3] for item in ranked[:limit])


__all__ = [
    "IGNORED_DIRECTORIES",
    "MAX_DISCOVERED_FILES",
    "discover_workspace_files",
    "filter_file_mentions",
]
