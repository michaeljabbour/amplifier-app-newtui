"""Historical diagnostic: do Foundation release tags now ship Anchors?

This check explained the former ``@main`` exception. It no longer controls the
app's source policy: Anchors and its recursive repositories are full-SHA locked
by ``data/anchors-source-lock.json``. Keep this script only as an upstream
release-packaging signal; a tag gaining ``bundles/anchors`` does not authorize
replacing the app's immutable commit pin.

This is deliberately NOT part of the default (offline, no-credentials) test
gate: answering "has upstream shipped a new tag" needs the network. Run it
by hand (or from a scheduled/maintainer CI job) when re-auditing the pin:

    uv run python scripts/verify_anchors_constraint.py

Exit codes: 0 -- latest tag still lacks Anchors. 1 -- network/API failure.
2 -- a tag now ships Anchors (upstream packaging changed; app stays SHA-locked).

Pure decision logic (:func:`latest_release_tag`, :func:`anchors_shipped_at`)
is exercised offline in ``tests/test_verify_anchors_constraint.py`` against
fixed inputs; only :func:`main` touches the network.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

FOUNDATION_REPO = "microsoft/amplifier-foundation"
ANCHORS_PATH = "bundles/anchors"
TRACKED_REF = "full commit SHA"
"""The app policy; kept as display text to avoid importing repo-local code."""

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def latest_release_tag(tag_names: list[str]) -> str | None:
    """The highest ``vX.Y.Z`` tag name, or ``None`` if none match.

    Sorts by parsed (major, minor, patch) rather than trusting API/string
    order, which is not a documented contract for GitHub's ``/tags``
    endpoint. Non-``vX.Y.Z`` tags (pre-releases, other conventions) are
    ignored -- foundation has shipped only plain ``vX.Y.Z`` tags to date.
    """
    parsed: list[tuple[tuple[int, int, int], str]] = []
    for name in tag_names:
        match = _TAG_RE.match(name)
        if match:
            parsed.append((tuple(int(part) for part in match.groups()), name))  # type: ignore[arg-type]
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[0])[1]


def anchors_shipped_at(status_code: int) -> bool:
    """Does the GitHub contents API say ``bundles/anchors`` exists at a ref?

    ``200`` -- present. ``404`` -- absent (today's status for every release
    tag). Any other code is treated as inconclusive by the caller, which
    only calls this for a 200/404 response.
    """
    return status_code == 200


def _get_status(url: str, timeout: float = 10.0) -> int:
    """HTTP status for *url*; 404 is a normal, expected response (not raised
    as an error) since that IS the signal this script checks for."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _get_json(url: str, timeout: float = 10.0) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags today; kept for parity with the other scripts/ entries
    try:
        tags_raw = _get_json(f"https://api.github.com/repos/{FOUNDATION_REPO}/tags")
    except Exception as error:  # noqa: BLE001 -- network/API failure is inconclusive, not "fixed"
        print(f"could not reach the GitHub API: {error}", file=sys.stderr)
        return 1
    if not isinstance(tags_raw, list):
        print(f"unexpected /tags response shape: {tags_raw!r}", file=sys.stderr)
        return 1
    tag_names = [entry["name"] for entry in tags_raw if isinstance(entry, dict) and "name" in entry]
    latest = latest_release_tag(tag_names)
    if latest is None:
        print(f"no vX.Y.Z tags found on {FOUNDATION_REPO} -- nothing to compare", file=sys.stderr)
        return 1

    try:
        status = _get_status(
            f"https://api.github.com/repos/{FOUNDATION_REPO}/contents/{ANCHORS_PATH}?ref={latest}"
        )
    except Exception as error:  # noqa: BLE001 -- see above
        print(f"could not reach the GitHub API: {error}", file=sys.stderr)
        return 1

    if anchors_shipped_at(status):
        print(
            f"CONSTRAINT MAY HAVE CHANGED: {FOUNDATION_REPO}@{latest} now ships "
            f"{ANCHORS_PATH} (HTTP {status}). Upstream packaging improved; the app "
            "remains full-SHA locked. Review a deliberate recursive lock bump if desired."
        )
        return 2

    print(
        f"constraint still holds: {FOUNDATION_REPO}@{latest} (latest tag) does not "
        f"ship {ANCHORS_PATH} (HTTP {status}). The app remains pinned to a {TRACKED_REF}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
