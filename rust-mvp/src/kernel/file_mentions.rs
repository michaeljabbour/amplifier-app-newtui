//! Workspace file discovery and ranking for composer `@file` mentions.
//!
//! This module owns filesystem access; the UI layer receives only relative
//! paths and filtered results. Discovery is deliberately bounded and never
//! follows symlinks, so opening autocomplete cannot wander outside the
//! project or stall forever in generated dependency trees.

use std::fs;
use std::path::{Path, PathBuf};

/// Directory names pruned from discovery (generated/dependency trees).
pub const IGNORED_DIRECTORIES: [&str; 11] = [
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
];

/// Traversal stops once this many files have been discovered.
pub const MAX_DISCOVERED_FILES: usize = 20_000;

/// Default number of results returned by [`filter_file_mentions`].
pub const DEFAULT_FILTER_LIMIT: usize = 8;

fn is_ignored_directory(name: &str) -> bool {
    IGNORED_DIRECTORIES.contains(&name)
}

/// Return stable POSIX-style paths beneath `project_dir`.
///
/// Generated/dependency directories are pruned, symlinked files are skipped,
/// and traversal stops at `max_files`. Permission races are ignored because
/// autocomplete is an optional convenience, never a session-start gate.
pub fn discover_workspace_files(project_dir: &Path, max_files: usize) -> Vec<String> {
    let root = project_dir
        .canonicalize()
        .unwrap_or_else(|_| project_dir.to_path_buf());
    let mut found: Vec<String> = Vec::new();
    walk(&root, &root, max_files, &mut found);
    found
}

/// Depth-first pre-order walk mirroring `os.walk(topdown=True, followlinks=False)`
/// with `onerror=None`: unreadable directories are skipped silently. Returns
/// `false` once `max_files` has been reached so callers stop recursing.
fn walk(root: &Path, current: &Path, max_files: usize, found: &mut Vec<String>) -> bool {
    let entries = match fs::read_dir(current) {
        Ok(entries) => entries,
        Err(_) => return true,
    };
    let mut directories: Vec<PathBuf> = Vec::new();
    let mut filenames: Vec<PathBuf> = Vec::new();
    for entry in entries.flatten() {
        let file_type = match entry.file_type() {
            Ok(file_type) => file_type,
            Err(_) => continue,
        };
        // `file_type()` never follows symlinks, so symlinked files and
        // symlinked directories both land here and are skipped — matching the
        // Python pruning of symlink dirs plus the per-file symlink skip.
        if file_type.is_symlink() {
            continue;
        }
        let path = entry.path();
        if file_type.is_dir() {
            if entry
                .file_name()
                .to_str()
                .is_none_or(|name| !is_ignored_directory(name))
            {
                directories.push(path);
            }
        } else {
            filenames.push(path);
        }
    }
    directories.sort();
    filenames.sort();
    for path in filenames {
        let Ok(relative) = path.strip_prefix(root) else {
            continue;
        };
        found.push(
            relative
                .components()
                .map(|component| component.as_os_str().to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join("/"),
        );
        if found.len() >= max_files {
            return false;
        }
    }
    for directory in directories {
        if !walk(root, &directory, max_files, found) {
            return false;
        }
    }
    true
}

/// Rank file paths for a case-insensitive composer query.
///
/// Basename prefix matches lead, then path prefix, basename substring, and
/// path substring. Shorter paths win within a tier; the original path breaks
/// ties deterministically.
pub fn filter_file_mentions<S: AsRef<str>>(paths: &[S], query: &str, limit: usize) -> Vec<String> {
    let needle = query.to_lowercase();
    let needle = needle.trim_start_matches('@');
    let mut ranked: Vec<(u8, usize, String, String)> = Vec::new();
    for path in paths {
        let path = path.as_ref();
        let folded = path.to_lowercase();
        let basename = path.rsplit('/').next().unwrap_or(path).to_lowercase();
        let tier = if needle.is_empty() || basename.starts_with(needle) {
            0
        } else if folded.starts_with(needle) {
            1
        } else if basename.contains(needle) {
            2
        } else if folded.contains(needle) {
            3
        } else {
            continue;
        };
        ranked.push((tier, path.chars().count(), folded, path.to_owned()));
    }
    ranked.sort();
    ranked
        .into_iter()
        .take(limit)
        .map(|(_, _, _, path)| path)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Tests-only tempdir: unique directory under the OS temp dir, removed on drop.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            static COUNTER: AtomicUsize = AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "file_mentions_test_{}_{}",
                std::process::id(),
                COUNTER.fetch_add(1, Ordering::Relaxed),
            ));
            fs::create_dir_all(&path).expect("create tempdir");
            TempDir(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn write(path: PathBuf, contents: &str) {
        fs::write(path, contents).expect("write file");
    }

    #[test]
    fn test_discovery_is_relative_stable_and_prunes_generated_trees() {
        let tmp = TempDir::new();
        let tmp_path = tmp.path();
        fs::create_dir(tmp_path.join("src")).unwrap();
        write(tmp_path.join("src").join("app.py"), "pass");
        fs::create_dir(tmp_path.join(".github")).unwrap();
        write(tmp_path.join(".github").join("workflows.yml"), "name: ci");
        fs::create_dir(tmp_path.join(".git")).unwrap();
        write(tmp_path.join(".git").join("index"), "ignored");
        fs::create_dir(tmp_path.join("node_modules")).unwrap();
        write(tmp_path.join("node_modules").join("pkg.js"), "ignored");

        assert_eq!(
            discover_workspace_files(tmp_path, MAX_DISCOVERED_FILES),
            vec![".github/workflows.yml".to_owned(), "src/app.py".to_owned()],
        );
    }

    #[test]
    fn test_discovery_is_bounded() {
        let tmp = TempDir::new();
        for index in 0..5 {
            write(tmp.path().join(format!("file-{index}.txt")), "");
        }
        assert_eq!(discover_workspace_files(tmp.path(), 2).len(), 2);
    }

    #[test]
    fn test_filter_prefers_basename_prefix_then_path_matches() {
        let paths = [
            "docs/guide.md",
            "src/guide_helpers.py",
            "guide.md",
            "notes/my-guide.txt",
            "src/app.py",
        ];
        assert_eq!(
            filter_file_mentions(&paths, "guide", DEFAULT_FILTER_LIMIT),
            vec![
                "guide.md".to_owned(),
                "docs/guide.md".to_owned(),
                "src/guide_helpers.py".to_owned(),
                "notes/my-guide.txt".to_owned(),
            ],
        );
        assert_eq!(
            filter_file_mentions(&paths, "src/a", DEFAULT_FILTER_LIMIT),
            vec!["src/app.py".to_owned()],
        );
    }

    #[test]
    fn test_filter_accepts_leading_at_and_limits_results() {
        let paths: Vec<String> = (0..20).map(|index| format!("file-{index}.txt")).collect();
        assert_eq!(filter_file_mentions(&paths, "@file", 3).len(), 3);
    }
}
