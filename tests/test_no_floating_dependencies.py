"""Guard: no floating (branch-tracking) git dependency ships unpinned.

Compliance B9 gap 1, item 3: "add a test that FAILS if an unpinned
dependency is introduced in future, with the anchors exception explicitly
allow-listed and justified in one place." :data:`ALLOWED_FLOATING_REFS` is
that one place -- every other git dependency this app ships with must
resolve to a release tag or a content-verified commit SHA.

Scope, deliberately: the app's DEFAULT dependency surface -- what a clean
install actually fetches without the user opting into anything further:

- the packaged bundle's ``includes`` / ``providers[].source`` /
  ``tools[].source`` (incl. nested ``tool-skills`` skill-source entries) /
  ``hooks[].source`` -- walked generically (any ``git+https://`` string
  anywhere in the parsed frontmatter), so a FUTURE module added to the
  bundle is covered automatically, not just today's known list;
- ``pyproject.toml``'s ``[tool.uv.sources]`` git overrides;
- ``.github/workflows/*.yml`` action refs (``uses: owner/repo@ref``).

Explicitly OUT of scope, and why: :data:`amplifier_app_tui.kernel.setup.
PROVIDER_SOURCES` (a catalog of OPTIONAL provider modules offered by
``amplifier-tui setup`` / ``provider add`` -- nothing a clean install
fetches on its own, and it deliberately mirrors amplifier-app-cli's own
``DEFAULT_PROVIDER_SOURCES`` convention of floating ``@main`` for the same
reason: these are living integrations a user opts into, not a pin this repo
owns) and :data:`amplifier_app_tui.kernel.config.ROUTING_MATRIX_BUNDLE_URI`
(the routing-matrix overlay, composed only when a user opts into
``routing.enabled`` -- donor parity with amplifier-app-cli's own
``WELL_KNOWN_BUNDLES``). Both are catalogs of choices, not shipped defaults;
pinning them is a judgment call left for a future pass if/when they grow
their own reproducibility requirements.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

from amplifier_app_tui.kernel import updater

REPO_ROOT = Path(__file__).resolve().parents[1]

# -- the ONE allow-listed float, justified in this ONE place ----------------

ALLOWED_FLOATING_REFS: dict[str, str] = {
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=bundles/anchors/bundle.md": (
        "issue #96: a bare-SHA pin here was tried and reverted -- the "
        "bundle loader's fetch cannot resolve a non-tip SHA once foundation "
        "advances, so clean installs failed with 'Include Failed (skipping): "
        "amplifier-foundation'. Foundation's release tags (through v2.1.2, "
        "re-verified 2026-08-04 via the GitHub contents API -- see "
        "docs/DEVELOPMENT.md 'Anchors ref lifecycle') do not ship "
        "bundles/anchors -- only @main does -- so @main is the only "
        "fetchable source today. Mitigated two ways: (1) kernel/updater.py's "
        "anchors_status() live-compares the cached commit against the "
        "remote tip and surfaces staleness in `doctor` / `update "
        "--check-only` (never a silent float); (2) "
        "scripts/verify_anchors_constraint.py re-checks whether THIS "
        "specific constraint (tags lack bundles/anchors) still holds, so a "
        "future foundation release that finally ships it is caught instead "
        "of discovered by accident. Re-run that script before ever removing "
        "this allow-list entry; do not just re-pin a bare SHA (that is "
        "exactly what #96 reverted)."
    ),
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^v\d+(?:\.[\w-]+)*$")  # vX.Y.Z-style release tags
_GIT_URI_RE = re.compile(r"^git\+https://\S+?@([^\s#]+)(?:#.*)?$")


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
    assert ALLOWED_FLOATING_REFS, "expected exactly the anchors float while it exists"
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
