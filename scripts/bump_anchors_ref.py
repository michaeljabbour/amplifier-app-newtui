"""Synchronize every outer Anchors include with the reviewed source lock.

The anchors include appears in THREE live files that must never drift
(``kernel.updater.pin_files``): repo-root ``bundle.md``, the byte-identical
packaged ``tui.md``, and the packaged ``anchors.md`` pointer. This rewrites
the ref across all three atomically, then re-asserts byte-identity and lockstep
before writing anything — so a partial rewrite can't ship.

Policy (see ``docs/DEVELOPMENT.md`` → "Anchors ref lifecycle"): Anchors is a
full commit SHA. The Foundation source handler pinned by this app now supports
non-tip SHA checkout from a cold cache, removing the historical ``@main``
exception. Nested sources are pinned separately by
``data/anchors-source-lock.json``; therefore this script refuses to write a ref
that disagrees with that reviewed lock.

Usage:
    uv run python scripts/bump_anchors_ref.py          # sync to reviewed lock
    uv run python scripts/bump_anchors_ref.py --check  # verify, change nothing

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
from amplifier_app_tui.kernel.source_lock import ANCHORS_COMMIT  # noqa: E402


def _rewrite_ref(text: str, new_ref: str) -> str:
    """Replace the ref inside the anchors include URI, leaving all else intact."""

    def _sub(match: "object") -> str:
        whole = match.group(0)  # type: ignore[attr-defined]
        old_ref = match.group(1)  # type: ignore[attr-defined]
        return whole.replace(f"@{old_ref}#", f"@{new_ref}#", 1)

    return _ANCHORS_REF_RE.sub(_sub, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "ref",
        nargs="?",
        default=ANCHORS_COMMIT,
        help="full Foundation commit (must match anchors-source-lock.json)",
    )
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
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

    if not _is_sha(args.ref):
        print(
            f"ERROR: Anchors must use a full 40-hex commit SHA, got {args.ref!r}",
            file=sys.stderr,
        )
        return 1
    if args.ref != ANCHORS_COMMIT:
        print(
            f"ERROR: requested {args.ref[:8]} but the reviewed recursive lock names "
            f"{ANCHORS_COMMIT[:8]}; update anchors-source-lock.json first",
            file=sys.stderr,
        )
        return 1

    if args.check:
        if refs != {ANCHORS_COMMIT}:
            print("ERROR: outer Anchors refs do not match the recursive lock", file=sys.stderr)
            return 1
        print(f"outer Anchors refs match recursive lock @{ANCHORS_COMMIT[:8]}")
        return 0

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
