"""Bump the foundation ref the anchors include tracks, in lockstep.

The anchors include appears in THREE live files that must never drift
(``kernel.updater.pin_files``): repo-root ``bundle.md``, the byte-identical
packaged ``tui.md``, and the packaged ``anchors.md`` pointer. This rewrites
the ref across all three atomically, then re-asserts byte-identity and lockstep
before writing anything — so a partial rewrite can't ship.

Policy (see ``docs/DEVELOPMENT.md`` → "Anchors pin lifecycle"): anchors tracks
foundation ``@main`` (floating). A bare 40-hex SHA was tried and abandoned —
GitHub stops serving a non-tip SHA once foundation advances, which broke clean
installs (#96). So the default and recommended ref is ``main``; pass a tag once
foundation publishes tagged releases that ship ``bundles/anchors`` (Option B).
Pinning a bare SHA is refused unless ``--allow-sha`` is given.

Usage:
    uv run python scripts/bump_anchors_ref.py            # -> main (default)
    uv run python scripts/bump_anchors_ref.py v2.2.0     # -> a release tag
    uv run python scripts/bump_anchors_ref.py --check     # report, change nothing

This is a repo-maintenance script (like ``scripts/regen_screenshot.py``); it
rewrites repo source only and does NOT commit. Rewriting a ref inside an
installed wheel is meaningless (lost on reinstall), so there is no user command.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from amplifier_app_tui.kernel.updater import (  # noqa: E402
    _ANCHORS_REF_RE,
    _is_sha,
    pin_files,
    read_anchors_ref,
)


def _rewrite_ref(text: str, new_ref: str) -> str:
    """Replace the ref inside the anchors include URI, leaving all else intact."""

    def _sub(match: "object") -> str:
        whole = match.group(0)  # type: ignore[attr-defined]
        old_ref = match.group(1)  # type: ignore[attr-defined]
        return whole.replace(f"@{old_ref}#", f"@{new_ref}#", 1)

    return _ANCHORS_REF_RE.sub(_sub, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", nargs="?", default="main", help="foundation ref (default: main)")
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
    parser.add_argument("--allow-sha", action="store_true", help="permit a bare 40-hex SHA")
    args = parser.parse_args(argv)

    files = pin_files(REPO_ROOT)
    current = {path: read_anchors_ref(path.read_text(encoding="utf-8")) for path in files}
    refs = {ref for ref in current.values() if ref is not None}
    print("anchors ref lifecycle")
    for path, ref in current.items():
        print(f"  {path.relative_to(REPO_ROOT)}: @{ref}")

    if len(refs) != 1:
        print(f"ERROR: pin copies already drifted: {sorted(refs)}", file=sys.stderr)
        return 1

    if args.check:
        return 0

    if _is_sha(args.ref) and not args.allow_sha:
        print(
            f"ERROR: refusing to pin a bare SHA ({args.ref[:8]}) — GitHub stops serving "
            "non-tip SHAs and this broke clean installs (#96). Pass --allow-sha to override.",
            file=sys.stderr,
        )
        return 1

    if refs == {args.ref}:
        print(f"already tracking @{args.ref} — nothing to do")
        return 0

    rewritten = {path: _rewrite_ref(path.read_text(encoding="utf-8"), args.ref) for path in files}

    # Re-verify BEFORE writing: byte-identity (bundle.md ↔ tui.md) + lockstep.
    root_bundle, packaged_tui, packaged_anchors = files
    if rewritten[root_bundle] != rewritten[packaged_tui]:
        print("ERROR: bundle.md and tui.md would not be byte-identical", file=sys.stderr)
        return 1
    new_refs = {read_anchors_ref(text) for text in rewritten.values()}
    if new_refs != {args.ref}:
        print(f"ERROR: rewrite did not land in every copy: {sorted(new_refs)}", file=sys.stderr)
        return 1
    _ = packaged_anchors  # covered by new_refs lockstep above

    for path, text in rewritten.items():
        path.write_text(text, encoding="utf-8")
    print(f"bumped anchors ref → @{args.ref} across {len(files)} files")
    print("run `uv run pytest tests/test_kernel_session_config.py -q` to confirm anti-drift")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
