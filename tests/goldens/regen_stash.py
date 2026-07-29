"""Regenerate the prompt-stash list golden (tests/goldens/stash_list.txt).

    cd /Users/michaeljabbour/dev/amplifier-app-newtui
    uv run python tests/goldens/regen_stash.py

A golden change IS a renderer change — review the diff.
"""

from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.model.prompt_stash import StashEntry, render_stash_list

GOLDEN = Path(__file__).resolve().parent / "stash_list.txt"

ENTRIES = (
    StashEntry(text="first stashed draft", stamped_at=10000.0 - 3 * 3600),
    StashEntry(text="a multiline draft\nsecond line\nthird line", stamped_at=10000.0 - 120),
    StashEntry(
        text="a very long single line draft that should be truncated to fifty chars",
        stamped_at=10000.0 - 5,
    ),
)


def main() -> None:
    GOLDEN.write_text(render_stash_list(ENTRIES, now=10000.0), encoding="utf-8")
    print(f"wrote {GOLDEN}")


if __name__ == "__main__":
    main()
