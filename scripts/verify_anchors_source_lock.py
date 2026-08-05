"""Verify the real composed Anchors graph against the packaged source lock.

This is a maintainer/network gate, not part of the offline default suite. It
loads the packaged TUI bundle through the same include resolver as production,
inspects the fully composed mount plan, and fails if an unknown floating source
survives or if the immutable outer Anchors file differs from its recorded hash.

Use ``--cold`` for release evidence that starts with an empty temporary cache:

    uv run python scripts/verify_anchors_source_lock.py --cold
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from amplifier_app_tui.kernel.config import (  # noqa: E402
    build_bundle_include_resolver,
    build_source_resolver,
)
from amplifier_app_tui.kernel.source_lock import (  # noqa: E402
    ANCHORS_BUNDLE_SHA256,
    LOCKED_GIT_REFS,
    is_floating_git_uri,
    iter_git_uris,
    pin_mount_plan_sources,
    unlocked_floating_git_uris,
)


def sha256_file(path: Path) -> str:
    """Hex SHA-256 for one file, streamed in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def verify(home: Path) -> tuple[int, str]:
    """Return ``(exit_code, evidence)`` for one real composition."""
    from amplifier_foundation import BundleRegistry, load_bundle

    registry = BundleRegistry(
        home=home,
        strict=True,
        include_source_resolver=build_bundle_include_resolver({}),
    )
    uri = str(REPO_ROOT / "src/amplifier_app_tui/data/bundles/tui.md")
    bundle = await load_bundle(uri, registry=registry)
    mount_plan = bundle.to_mount_plan()

    unknown = unlocked_floating_git_uris(mount_plan)
    if unknown:
        return 1, f"unknown recursive floating sources: {unknown!r}"

    pin_mount_plan_sources(mount_plan, build_source_resolver({}))
    floats = tuple(sorted({uri for uri in iter_git_uris(mount_plan) if is_floating_git_uri(uri)}))
    if floats:
        return 1, f"floating sources remain after lock application: {floats!r}"

    anchors_root = bundle.source_base_paths.get("anchors")
    if anchors_root is None:
        return 1, "composed bundle did not register the Anchors namespace"
    anchors_file = anchors_root / "bundle.md"
    actual_hash = sha256_file(anchors_file)
    if actual_hash != ANCHORS_BUNDLE_SHA256:
        return 1, (
            "outer Anchors content hash mismatch: "
            f"lock={ANCHORS_BUNDLE_SHA256} actual={actual_hash}"
        )

    used = {
        uri.split("@", 1)[0]
        for uri in iter_git_uris(mount_plan)
        if uri.split("@", 1)[0] in LOCKED_GIT_REFS
    }
    unused = sorted(set(LOCKED_GIT_REFS) - used)
    if unused:
        return 1, f"stale recursive lock entries not present in composed graph: {unused!r}"

    return 0, (
        f"PASS: outer hash {actual_hash[:12]} · "
        f"{len(LOCKED_GIT_REFS)} recursive repositories · 0 floating sources"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cold",
        action="store_true",
        help="use an empty temporary Amplifier home/cache",
    )
    args = parser.parse_args(argv)

    if args.cold:
        with tempfile.TemporaryDirectory(prefix="amplifier-tui-anchors-lock-") as temp:
            code, evidence = asyncio.run(verify(Path(temp)))
    else:
        code, evidence = asyncio.run(verify(Path.home() / ".amplifier"))
    print(evidence, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
