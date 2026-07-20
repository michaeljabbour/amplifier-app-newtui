from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.kernel.file_mentions import (
    discover_workspace_files,
    filter_file_mentions,
)


def test_discovery_is_relative_stable_and_prunes_generated_trees(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "workflows.yml").write_text("name: ci")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("ignored")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("ignored")

    assert discover_workspace_files(tmp_path) == (
        ".github/workflows.yml",
        "src/app.py",
    )


def test_discovery_is_bounded(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"file-{index}.txt").write_text("")
    assert len(discover_workspace_files(tmp_path, max_files=2)) == 2


def test_filter_prefers_basename_prefix_then_path_matches() -> None:
    paths = (
        "docs/guide.md",
        "src/guide_helpers.py",
        "guide.md",
        "notes/my-guide.txt",
        "src/app.py",
    )
    assert filter_file_mentions(paths, "guide") == (
        "guide.md",
        "docs/guide.md",
        "src/guide_helpers.py",
        "notes/my-guide.txt",
    )
    assert filter_file_mentions(paths, "src/a") == ("src/app.py",)


def test_filter_accepts_leading_at_and_limits_results() -> None:
    paths = tuple(f"file-{index}.txt" for index in range(20))
    assert len(filter_file_mentions(paths, "@file", limit=3)) == 3
