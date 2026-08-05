"""Reviewed recursive source lock for the packaged Anchors composition.

The outer Anchors bundle is immutable, but its frontmatter and the behavior
partials it includes still spell many module sources as ``@main`` (and one as
an implicit default branch).  Foundation exposes two app-owned policy seams:
an include resolver while composing and a module resolver while preparing.
This module feeds the same reviewed lock to both seams and also rewrites
source-like strings nested in module configuration (notably skill sources).

Explicit user ``sources`` overrides remain authoritative in ``config.py``;
the lock is the fallback for the upstream defaults inherited from Anchors.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

_LOCK_PATH = Path(__file__).resolve().parent.parent / "data" / "anchors-source-lock.json"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^v\d+(?:\.[\w-]+)*$")
_GIT_URI_RE = re.compile(
    r"^(?P<repository>git\+https://[^\s@#]+?)(?:@(?P<ref>[^\s#]+))?(?P<fragment>#.*)?$"
)


def _read_lock(path: Path = _LOCK_PATH) -> tuple[str, str, Mapping[str, str]]:
    """Load and validate the shipped lock, failing closed on corrupt metadata."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise RuntimeError(f"unsupported anchors source-lock schema in {path}")
    anchors = raw.get("anchors")
    repositories = raw.get("repositories")
    if not isinstance(anchors, dict) or not isinstance(repositories, dict):
        raise RuntimeError(f"malformed anchors source lock in {path}")
    commit = str(anchors.get("commit", ""))
    bundle_sha256 = str(anchors.get("bundle_sha256", ""))
    normalized = {str(repository): str(ref) for repository, ref in repositories.items()}
    if not _SHA_RE.fullmatch(commit) or not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
        raise RuntimeError(f"invalid anchors provenance in {path}")
    invalid = {
        repository: ref for repository, ref in normalized.items() if not _SHA_RE.fullmatch(ref)
    }
    if invalid:
        raise RuntimeError(f"non-SHA entries in anchors source lock: {invalid!r}")
    foundation = "git+https://github.com/microsoft/amplifier-foundation"
    if normalized.get(foundation) != commit:
        raise RuntimeError("anchors commit and locked amplifier-foundation ref disagree")
    return commit, bundle_sha256, MappingProxyType(normalized)


ANCHORS_COMMIT, ANCHORS_BUNDLE_SHA256, LOCKED_GIT_REFS = _read_lock()


def source_lock_path() -> Path:
    """Path to the packaged, reviewable lock artifact."""
    return _LOCK_PATH


def _parts(uri: str) -> tuple[str, str | None, str] | None:
    match = _GIT_URI_RE.fullmatch(uri)
    if match is None:
        return None
    repository = match.group("repository")
    canonical = repository[:-4] if repository.endswith(".git") else repository
    return canonical, match.group("ref"), match.group("fragment") or ""


def is_floating_git_uri(uri: str) -> bool:
    """Whether *uri* names a branch (or implicit branch), rather than SHA/tag."""
    parsed = _parts(uri)
    if parsed is None:
        return False
    _repository, ref, _fragment = parsed
    return ref is None or not (_SHA_RE.fullmatch(ref) or _TAG_RE.fullmatch(ref))


def pin_git_uri(uri: str) -> str:
    """Replace a known floating Anchors source with its reviewed full SHA."""
    parsed = _parts(uri)
    if parsed is None:
        return uri
    repository, ref, fragment = parsed
    locked = LOCKED_GIT_REFS.get(repository)
    if locked is None or (ref is not None and not is_floating_git_uri(uri)):
        return uri
    return f"{repository}@{locked}{fragment}"


def iter_git_uris(value: object) -> Iterator[str]:
    """Yield every git URI string nested anywhere in dictionaries/lists."""
    if isinstance(value, str):
        if _parts(value) is not None:
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_git_uris(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from iter_git_uris(item)


def unlocked_floating_git_uris(value: object) -> tuple[str, ...]:
    """Known/unknown floats that remain after applying this lock."""
    return tuple(
        sorted(
            {
                uri
                for uri in iter_git_uris(value)
                if is_floating_git_uri(uri) and pin_git_uri(uri) == uri
            }
        )
    )


def pin_mount_plan_sources(
    value: Any,
    module_resolver: Callable[[str, str], str],
) -> Any:
    """Apply effective source policy throughout a prepared mount plan in place.

    Module specs go through ``module_resolver`` so explicit settings overrides
    win. Other string leaves use the Anchors lock directly; this catches source
    URIs embedded in module configuration, which Foundation's module resolver
    intentionally does not visit.
    """
    if isinstance(value, dict):
        module = value.get("module")
        source = value.get("source")
        resolved_source = False
        if isinstance(module, str) and isinstance(source, str):
            value["source"] = module_resolver(module, source)
            resolved_source = True
        for key, item in tuple(value.items()):
            if resolved_source and key == "source":
                continue
            value[key] = pin_mount_plan_sources(item, module_resolver)
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = pin_mount_plan_sources(item, module_resolver)
        return value
    if isinstance(value, tuple):
        return tuple(pin_mount_plan_sources(item, module_resolver) for item in value)
    if isinstance(value, str):
        return pin_git_uri(value)
    return value


__all__ = [
    "ANCHORS_BUNDLE_SHA256",
    "ANCHORS_COMMIT",
    "LOCKED_GIT_REFS",
    "is_floating_git_uri",
    "iter_git_uris",
    "pin_git_uri",
    "pin_mount_plan_sources",
    "source_lock_path",
    "unlocked_floating_git_uris",
]
