"""Guard: no floating (branch-tracking) git dependency ships unpinned.

Compliance B9 gap 1, item 3: fail if an unpinned dependency is introduced.
The former Anchors ``@main`` exception is gone: the outer bundle is a full
SHA and its recursively floating descriptors are translated through the
reviewed packaged source lock.

Scope: every git source the app itself selects or recommends:

- the packaged bundle's ``includes`` / ``providers[].source`` /
  ``tools[].source`` (incl. nested ``tool-skills`` skill-source entries) /
  ``hooks[].source`` -- walked generically (any ``git+https://`` string
  anywhere in the parsed frontmatter), so a FUTURE module added to the
  bundle is covered automatically, not just today's known list;
- ``pyproject.toml``'s ``[tool.uv.sources]`` git overrides;
- ``.github/workflows/*.yml`` action refs (``uses: owner/repo@ref``).
- :data:`amplifier_app_tui.kernel.setup.PROVIDER_SOURCES`, whose selected URI
  is persisted into user settings by setup/provider-add; and
- :data:`amplifier_app_tui.kernel.config.ROUTING_MATRIX_BUNDLE_URI`, composed
  when routing is opted in; and
- the official README/install/settings/user-guide examples users are invited
  to copy into live configuration.

The latter two are optional at runtime, but they are still source choices
this app owns. A user making the same choice twice must not receive different
code merely because a branch advanced.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from amplifier_app_tui.kernel import updater
from amplifier_app_tui.kernel.config import ROUTING_MATRIX_BUNDLE_URI
from amplifier_app_tui.kernel.setup import PROVIDER_SOURCES
from amplifier_app_tui.kernel.source_lock import (
    ANCHORS_COMMIT,
    LOCKED_GIT_REFS,
    pin_git_uri,
    source_lock_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# There are intentionally no app-owned floating exceptions. Keep the mapping
# so future reviewers have one explicit location for a narrowly justified
# exception, but adding one must include a substantive reason and test evidence.
ALLOWED_FLOATING_REFS: dict[str, str] = {}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^v\d+(?:\.[\w-]+)*$")  # vX.Y.Z-style release tags
_GIT_URI_RE = re.compile(r"^git\+https://\S+?@([^\s#]+)(?:#.*)?$")
_GIT_URI_IN_TEXT_RE = re.compile(r"(git\+https://[^\s`\"']+?@([^\s#`\"']+)(?:#[^\s`\"']*)?)")


def _is_pinned_ref(ref: str) -> bool:
    """A ref is pinned when it is a full commit SHA or a version tag."""
    return bool(_SHA_RE.match(ref) or _TAG_RE.match(ref))


def _iter_strings(node: object) -> Any:
    """Yield every string leaf anywhere inside a nested dict/list structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_strings(item)


def git_refs_in(data: object) -> list[tuple[str, str]]:
    """``[(full_uri, ref), ...]`` for every ``git+https://`` URI in *data*.

    Walks the WHOLE structure recursively rather than naming specific keys,
    so a future field (a new tool's nested source list, say) is covered
    without this test needing an update to notice it.
    """
    found: list[tuple[str, str]] = []
    for value in _iter_strings(data):
        match = _GIT_URI_RE.match(value)
        if match:
            found.append((value, match.group(1)))
    return found


def _frontmatter(text: str) -> dict[str, Any]:
    assert text.startswith("---"), "bundle must open with a YAML frontmatter fence"
    data = yaml.safe_load(text.split("---", 2)[1])
    assert isinstance(data, dict)
    return data


# -- bundle.md / tui.md / anchors.md -----------------------------------------


def test_bundle_git_sources_are_pinned_or_allow_listed() -> None:
    """Every git dependency the packaged bundle composes is pinned, except
    the one justified, allow-listed float."""
    offenders: list[str] = []
    for path in updater.pin_files(REPO_ROOT):
        data = _frontmatter(path.read_text(encoding="utf-8"))
        for uri, ref in git_refs_in(data):
            if uri in ALLOWED_FLOATING_REFS:
                continue
            if not _is_pinned_ref(ref):
                offenders.append(f"{path.name}: {uri}")
    assert not offenders, (
        "unpinned (floating) git dependency introduced in the packaged bundle: "
        f"{offenders!r} -- pin to a release tag or a content-verified commit "
        "SHA, or add it to ALLOWED_FLOATING_REFS in this file with a "
        "justification (see the module docstring)."
    )


def test_allow_list_entries_are_justified_and_still_relevant() -> None:
    """The allow-list is the one place a float is excused; guard it from
    silently drifting out of sync with what it excuses."""
    for uri, reason in ALLOWED_FLOATING_REFS.items():
        assert len(reason.strip()) > 40, f"allow-listed float has no real justification: {uri!r}"
    texts = "\n".join(p.read_text(encoding="utf-8") for p in updater.pin_files(REPO_ROOT))
    for uri in ALLOWED_FLOATING_REFS:
        assert uri in texts, (
            f"allow-listed float {uri!r} no longer appears in any pinned bundle "
            "copy -- remove it from ALLOWED_FLOATING_REFS (the exception it "
            "covered is gone) rather than leaving a stale, unused excuse that "
            "could silently cover for a NEW, different float later."
        )


def test_recursive_anchors_source_lock_is_full_sha_and_matches_outer_pin() -> None:
    """The runtime lock is a shipped artifact, not an in-memory convention."""
    assert source_lock_path().is_file()
    assert LOCKED_GIT_REFS
    assert all(_SHA_RE.fullmatch(ref) for ref in LOCKED_GIT_REFS.values())
    foundation = "git+https://github.com/microsoft/amplifier-foundation"
    assert LOCKED_GIT_REFS[foundation] == ANCHORS_COMMIT
    refs = {
        updater.read_anchors_ref(path.read_text(encoding="utf-8"))
        for path in updater.pin_files(REPO_ROOT)
    }
    assert refs == {ANCHORS_COMMIT}


def test_every_reviewed_recursive_repository_pins_branch_and_implicit_refs() -> None:
    """Both ``@main`` and Anchors' one ref-less source become exact commits."""
    for repository, commit in LOCKED_GIT_REFS.items():
        for raw in (repository, f"{repository}@main", f"{repository}@main#subdirectory=x"):
            pinned = pin_git_uri(raw)
            assert f"@{commit}" in pinned
            assert not pinned.endswith("@main")
        assert pin_git_uri(f"{repository}@v1.2.3") == f"{repository}@v1.2.3"


def test_no_bundle_git_source_is_a_bare_branch_other_than_the_allowed_float() -> None:
    """Belt-and-suspenders: no OTHER include/module ever tracks a bare branch
    name (``main``/``master``/``HEAD``), even one this test's generic pinned-
    ref check would not catch (e.g. a future short-lived tag-shaped branch)."""
    branch_like = {"main", "master", "head", "trunk"}
    offenders: list[str] = []
    for path in updater.pin_files(REPO_ROOT):
        data = _frontmatter(path.read_text(encoding="utf-8"))
        for uri, ref in git_refs_in(data):
            if uri in ALLOWED_FLOATING_REFS:
                continue
            if ref.lower() in branch_like:
                offenders.append(f"{path.name}: {uri}")
    assert not offenders, f"bare branch float(s) beyond the allow-list: {offenders!r}"


# -- pyproject.toml [tool.uv.sources] ----------------------------------------


def test_pyproject_uv_sources_are_pinned_to_a_sha() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    assert sources, "expected at least one [tool.uv.sources] override to check"
    offenders = {
        name: entry.get("rev")
        for name, entry in sources.items()
        if isinstance(entry, dict)
        and "git" in entry
        and not _SHA_RE.match(str(entry.get("rev", "")))
    }
    assert not offenders, (
        f"pyproject.toml [tool.uv.sources] has an unpinned git rev: {offenders!r} "
        "-- pin `rev` to a full 40-hex commit SHA."
    )


def test_optional_app_owned_sources_are_pinned_to_a_sha() -> None:
    """Routing/provider setup choices are reproducible, not moving branches.

    They are not fetched by an untouched default boot, but once selected the
    app persists or composes these exact URIs. That makes their immutability
    an app-owned contract just like the default bundle's sources.
    """
    sources = {"routing-matrix": ROUTING_MATRIX_BUNDLE_URI, **PROVIDER_SOURCES}
    offenders: dict[str, str] = {}
    for name, uri in sources.items():
        match = _GIT_URI_RE.match(uri)
        if match is None or not _SHA_RE.match(match.group(1)):
            offenders[name] = uri
    assert not offenders, (
        "optional routing/provider catalog source(s) are not pinned to a full commit SHA: "
        f"{offenders!r}"
    )


def test_user_facing_git_source_examples_are_pinned() -> None:
    """Copy/paste configuration is an app-owned source choice too."""
    paths = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "INSTALL.md",
        REPO_ROOT / "docs" / "SETTINGS.md",
        REPO_ROOT / "docs" / "USER-GUIDE.md",
    )
    offenders: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for uri, ref in _GIT_URI_IN_TEXT_RE.findall(text):
            if uri in ALLOWED_FLOATING_REFS or _is_pinned_ref(ref):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {uri}")
    assert not offenders, (
        "floating git source in user-facing copy/paste documentation: "
        f"{offenders!r} -- pin it or document a narrowly reviewed exception"
    )


@pytest.mark.asyncio
async def test_foundation_resolves_a_non_tip_full_sha_from_a_cold_cache(tmp_path: Path) -> None:
    """The runtime dependency must support the bundle's full-SHA pins.

    Foundation versions before 1a408839 passed a SHA to ``git clone
    --branch``.  The bundle pinning guard above therefore gave a false sense
    of safety: every clean install failed before the first provider mounted.
    Exercise the installed handler against a local two-commit repository so
    this compatibility boundary stays network-free and deterministic.
    """
    from amplifier_foundation.paths.resolution import parse_uri
    from amplifier_foundation.sources.git import GitSourceHandler

    remote = tmp_path / "module"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    for key, value in (("user.name", "TUI test"), ("user.email", "tui@example.invalid")):
        subprocess.run(
            ["git", "-C", str(remote), "config", key, value],
            check=True,
            capture_output=True,
            text=True,
        )

    (remote / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    payload = remote / "payload.txt"
    payload.write_text("first\n")
    subprocess.run(
        ["git", "-C", str(remote), "add", "."],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(remote), "commit", "-m", "first"],
        check=True,
        capture_output=True,
        text=True,
    )
    pinned = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    payload.write_text("second\n")
    subprocess.run(
        ["git", "-C", str(remote), "commit", "-am", "second"],
        check=True,
        capture_output=True,
        text=True,
    )

    resolved = await GitSourceHandler().resolve(
        parse_uri(f"git+file://{remote}@{pinned}"), tmp_path / "cold-cache"
    )
    checked_out = subprocess.run(
        ["git", "-C", str(resolved.source_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert checked_out == pinned
    assert (resolved.active_path / "payload.txt").read_text() == "first\n"


# -- CI workflow action versions ---------------------------------------------


def test_ci_workflow_actions_are_pinned_to_a_commit_sha() -> None:
    """``uses: owner/repo@ref`` steps must pin ``ref`` to a full commit SHA,
    not a movable tag (``@v4``) -- a tag can be retargeted by its owner; a
    SHA cannot. (A trailing ``# vX.Y.Z`` comment stays human-readable.)"""
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    offenders: list[str] = []
    for path in sorted(workflows_dir.glob("*.yml")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = re.search(r"uses:\s*([\w.-]+/[\w.-]+)@([^\s#]+)", line)
            if match is None:
                continue
            action, ref = match.groups()
            if not _SHA_RE.match(ref):
                offenders.append(f"{path.name}:{line_no}: {action}@{ref}")
    assert not offenders, f"CI workflow action(s) not pinned to a commit SHA: {offenders!r}"
