//! Bounded Git snapshots for measuring per-turn file and diff yield.
//!
//! Port of `src/amplifier_app_newtui/kernel/git_yield.py` (itself ported
//! from amplifier-app-cli `ui/git_yield.py`, the reference implementation
//! for real-runtime turn outcomes). The runtime captures one snapshot
//! before and one after each turn; the delta between them is the turn's
//! concrete yield (`files N` / `+A/−D` on the turn rule — DESIGN-SPEC §3
//! shipped outcomes).
//!
//! Kernel-pure: git subprocess only, no UI, no amplifier-core. The Python
//! original drives git through asyncio subprocesses; here the same command
//! lines run through blocking [`std::process::Command`] with the same
//! bounded-output / timeout / non-zero-exit → unavailable decision logic.

use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

const MAX_OUTPUT_BYTES: usize = 2 * 1024 * 1024;
const MAX_FILES: usize = 10_000;
const MAX_UNTRACKED_READ_BYTES: usize = 1024 * 1024;

/// Default subprocess timeout, matching the Python keyword default.
pub const DEFAULT_TIMEOUT_SECONDS: f64 = 5.0;

/// Per-file numstat: `path`, lines added, lines deleted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitFileStat {
    pub path: String,
    pub additions: i64,
    pub deletions: i64,
}

impl GitFileStat {
    pub fn new(path: impl Into<String>, additions: i64, deletions: i64) -> Self {
        Self {
            path: path.into(),
            additions,
            deletions,
        }
    }
}

/// The turn's concrete yield: changed-file count plus signed line motion.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitTurnDelta {
    pub files: usize,
    pub additions: i64,
    pub deletions: i64,
}

impl GitTurnDelta {
    /// `+{additions}/−{deletions}` — U+2212 minus, exactly as Python.
    pub fn diff_label(&self) -> String {
        format!("+{}/\u{2212}{}", self.additions, self.deletions)
    }
}

/// One bounded snapshot of the working tree's tracked + untracked stats.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GitDiffSnapshot {
    pub available: bool,
    pub files: Vec<GitFileStat>,
}

impl GitDiffSnapshot {
    pub fn unavailable() -> Self {
        Self {
            available: false,
            files: Vec::new(),
        }
    }

    pub fn new(available: bool, files: Vec<GitFileStat>) -> Self {
        Self { available, files }
    }

    /// Delta of `self` (after) against `previous` (before); `None` when
    /// either snapshot is unavailable.
    pub fn delta_from(&self, previous: &GitDiffSnapshot) -> Option<GitTurnDelta> {
        if !self.available || !previous.available {
            return None;
        }
        let before: BTreeMap<&str, &GitFileStat> = previous
            .files
            .iter()
            .map(|item| (item.path.as_str(), item))
            .collect();
        let after: BTreeMap<&str, &GitFileStat> = self
            .files
            .iter()
            .map(|item| (item.path.as_str(), item))
            .collect();
        let paths: Vec<&str> = before
            .keys()
            .chain(after.keys())
            .copied()
            .collect::<std::collections::BTreeSet<&str>>()
            .into_iter()
            .filter(|path| before.get(path) != after.get(path))
            .collect();
        let mut additions: i64 = 0;
        let mut deletions: i64 = 0;
        for path in &paths {
            let (old_additions, old_deletions) = before
                .get(path)
                .map_or((0, 0), |stat| (stat.additions, stat.deletions));
            let (new_additions, new_deletions) = after
                .get(path)
                .map_or((0, 0), |stat| (stat.additions, stat.deletions));
            let added_delta = new_additions - old_additions;
            let deleted_delta = new_deletions - old_deletions;
            additions += added_delta.max(0) + (-deleted_delta).max(0);
            deletions += deleted_delta.max(0) + (-added_delta).max(0);
        }
        Some(GitTurnDelta {
            files: paths.len(),
            additions,
            deletions,
        })
    }
}

/// Capture tracked and untracked line statistics without invoking a shell.
///
/// Python signature: `capture_git_diff(cwd, *, timeout_seconds=5.0)`.
pub fn capture_git_diff(cwd: &Path) -> GitDiffSnapshot {
    capture_git_diff_with_timeout(cwd, DEFAULT_TIMEOUT_SECONDS)
}

pub fn capture_git_diff_with_timeout(cwd: &Path, timeout_seconds: f64) -> GitDiffSnapshot {
    let root = resolve(cwd);
    let tracked = match git_output(
        &root,
        &["diff", "--numstat", "HEAD", "--", "."],
        timeout_seconds,
    ) {
        Some(output) => output,
        None => return GitDiffSnapshot::unavailable(),
    };
    let untracked = match git_output(
        &root,
        &["ls-files", "--others", "--exclude-standard", "-z"],
        timeout_seconds,
    ) {
        Some(output) => output,
        None => return GitDiffSnapshot::unavailable(),
    };
    let mut stats: BTreeMap<String, GitFileStat> = BTreeMap::new();
    let tracked_text = String::from_utf8_lossy(&tracked);
    for line in tracked_text.lines().take(MAX_FILES) {
        let Some((additions, remainder)) = line.split_once('\t') else {
            continue;
        };
        let Some((deletions, path)) = remainder.split_once('\t') else {
            continue;
        };
        if path.is_empty() {
            continue;
        }
        stats.insert(
            path.to_string(),
            GitFileStat::new(path, parse_count(additions), parse_count(deletions)),
        );
    }
    for raw_path in untracked.split(|byte| *byte == 0).take(MAX_FILES) {
        if raw_path.is_empty() {
            continue;
        }
        let path = String::from_utf8_lossy(raw_path).into_owned();
        if stats.contains_key(&path) {
            continue;
        }
        let count = line_count(&root, &path);
        stats.insert(path.clone(), GitFileStat::new(path, count, 0));
    }
    // BTreeMap iteration is already sorted by path (byte order == code-
    // point order for UTF-8, matching Python's `sorted(..., key=path)`).
    GitDiffSnapshot::new(true, stats.into_values().collect())
}

/// The working-tree (or `--cached`) diff patch text for `/diff`.
///
/// Mirrors amplifier-app-cli's `/diff` (`git diff --no-color`); a bounded,
/// shell-free subprocess. Returns `None` when git is unavailable / not a
/// repo / output exceeds the byte cap, and `""` when the tree is clean.
pub fn capture_git_patch(cwd: &Path, staged: bool, timeout_seconds: f64) -> Option<String> {
    let mut args = vec!["diff", "--no-color", "--stat", "-p", "--unified=3"];
    if staged {
        args.push("--cached");
    }
    let output = git_output(&resolve(cwd), &args, timeout_seconds)?;
    Some(String::from_utf8_lossy(&output).into_owned())
}

/// Python `int(text) if text.isdigit() else 0` (numstat prints `-` for
/// binary files).
fn parse_count(text: &str) -> i64 {
    if !text.is_empty() && text.chars().all(|c| c.is_ascii_digit()) {
        text.parse().unwrap_or(0)
    } else {
        0
    }
}

/// Python `Path.resolve()` — best-effort canonicalization (Python resolves
/// non-strictly; fall back to the path unchanged when it cannot resolve).
fn resolve(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

/// Run `git <args>` in `cwd`, stderr discarded; `None` on spawn failure,
/// timeout, non-zero exit, or output beyond the 2 MiB cap.
fn git_output(cwd: &Path, args: &[&str], timeout_seconds: f64) -> Option<Vec<u8>> {
    let mut child = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut stdout_pipe = child.stdout.take()?;
    let (sender, receiver) = mpsc::channel::<Vec<u8>>();
    let reader = thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = stdout_pipe.read_to_end(&mut buffer);
        let _ = sender.send(buffer);
    });
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_seconds.max(0.0));
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = reader.join();
                    return None;
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(_) => {
                let _ = reader.join();
                return None;
            }
        }
    };
    let stdout = receiver.recv().ok()?;
    let _ = reader.join();
    if !status.success() || stdout.len() > MAX_OUTPUT_BYTES {
        return None;
    }
    Some(stdout)
}

/// Bounded line count of one untracked file; 0 for anything unreadable,
/// escaping the repo root, over the 1 MiB cap, or binary (NUL byte).
fn line_count(root: &Path, relative_path: &str) -> i64 {
    let candidate = match root.join(relative_path).canonicalize() {
        Ok(candidate) => candidate,
        Err(_) => return 0,
    };
    if candidate.strip_prefix(root).is_err() {
        return 0;
    }
    let mut data = Vec::new();
    let mut file = match std::fs::File::open(&candidate) {
        Ok(file) => file,
        Err(_) => return 0,
    };
    if file
        .by_ref()
        .take((MAX_UNTRACKED_READ_BYTES + 1) as u64)
        .read_to_end(&mut data)
        .is_err()
    {
        return 0;
    }
    if data.len() > MAX_UNTRACKED_READ_BYTES || data.contains(&0) {
        return 0;
    }
    let newline_count = data.iter().filter(|byte| **byte == b'\n').count() as i64;
    let trailing = i64::from(!data.is_empty() && !data.ends_with(b"\n"));
    newline_count + trailing
}

#[cfg(test)]
mod tests {
    //! Pins the git_yield cases from `tests/test_kernel_turn_yield.py`
    //! (the tracker / RealRuntime cases there belong to the turn_yield
    //! and runtime units).

    use super::*;
    use std::fs;

    // ------------------------------------------------------------------
    // GitDiffSnapshot delta math (pure)
    // ------------------------------------------------------------------

    #[test]
    fn test_delta_from_counts_changed_files_and_lines() {
        let before = GitDiffSnapshot::new(true, vec![GitFileStat::new("a.py", 2, 1)]);
        let after = GitDiffSnapshot::new(
            true,
            vec![
                GitFileStat::new("a.py", 10, 3), // +8/−2 on top of the pre-turn dirt
                GitFileStat::new("b.py", 5, 0),  // new file this turn
            ],
        );
        let delta = after.delta_from(&before).expect("delta available");
        assert_eq!(delta.files, 2);
        assert_eq!(delta.additions, 13);
        assert_eq!(delta.deletions, 2);
        assert_eq!(delta.diff_label(), "+13/\u{2212}2");
    }

    #[test]
    fn test_delta_from_none_when_either_snapshot_unavailable() {
        let ok = GitDiffSnapshot::new(true, vec![]);
        let missing = GitDiffSnapshot::unavailable();
        assert!(ok.delta_from(&missing).is_none());
        assert!(missing.delta_from(&ok).is_none());
        let unchanged = GitDiffSnapshot::new(true, vec![GitFileStat::new("a.py", 1, 1)]);
        let delta = unchanged
            .delta_from(&GitDiffSnapshot::new(
                true,
                vec![GitFileStat::new("a.py", 1, 1)],
            ))
            .expect("delta available");
        assert_eq!(delta.files, 0);
    }

    // ------------------------------------------------------------------
    // capture_git_diff against a real temp repo
    // ------------------------------------------------------------------

    fn git(repo: &Path, args: &[&str]) {
        let status = Command::new("git")
            .args(args)
            .current_dir(repo)
            .env_clear()
            .env("GIT_AUTHOR_NAME", "t")
            .env("GIT_AUTHOR_EMAIL", "t@t")
            .env("GIT_COMMITTER_NAME", "t")
            .env("GIT_COMMITTER_EMAIL", "t@t")
            .env("HOME", repo)
            .env("PATH", "/usr/bin:/bin:/usr/local/bin")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("git spawns");
        assert!(status.success(), "git {args:?} failed");
    }

    /// Port of the `git_repo` pytest fixture: seeded repo with one commit.
    fn git_repo(tmp: &Path) -> PathBuf {
        let repo = tmp.join("repo");
        fs::create_dir(&repo).unwrap();
        git(&repo, &["init", "-q"]);
        fs::write(repo.join("tracked.txt"), "one\ntwo\n").unwrap();
        git(&repo, &["add", "tracked.txt"]);
        git(&repo, &["commit", "-q", "-m", "seed"]);
        repo
    }

    #[test]
    fn test_capture_git_diff_sees_tracked_and_untracked() {
        let tmp = tempfile::tempdir().unwrap();
        let repo = git_repo(tmp.path());

        let clean = capture_git_diff(&repo);
        assert!(clean.available);
        assert!(clean.files.is_empty());

        fs::write(repo.join("tracked.txt"), "one\ntwo\nthree\n").unwrap();
        fs::write(repo.join("fresh.txt"), "a\nb\n").unwrap();
        let dirty = capture_git_diff(&repo);
        assert!(dirty.available);
        assert_eq!(
            dirty.files,
            vec![
                GitFileStat::new("fresh.txt", 2, 0),
                GitFileStat::new("tracked.txt", 1, 0),
            ]
        );
        let delta = dirty.delta_from(&clean).expect("delta available");
        assert_eq!((delta.files, delta.diff_label()), (2, "+3/\u{2212}0".to_string()));
    }

    #[test]
    fn test_capture_git_diff_unavailable_outside_a_repo() {
        let tmp = tempfile::tempdir().unwrap();
        let snapshot = capture_git_diff(tmp.path());
        assert!(!snapshot.available);
    }

    // ------------------------------------------------------------------
    // capture_git_patch (no direct Python test pins this; behaviors
    // oracle-checked against the Python source contract: "" when clean,
    // patch text when dirty, None outside a repo)
    // ------------------------------------------------------------------

    #[test]
    fn capture_git_patch_empty_when_clean_text_when_dirty_none_outside_repo() {
        let tmp = tempfile::tempdir().unwrap();
        let repo = git_repo(tmp.path());

        assert_eq!(
            capture_git_patch(&repo, false, DEFAULT_TIMEOUT_SECONDS),
            Some(String::new())
        );

        fs::write(repo.join("tracked.txt"), "one\ntwo\nthree\n").unwrap();
        let patch = capture_git_patch(&repo, false, DEFAULT_TIMEOUT_SECONDS).expect("patch");
        assert!(patch.contains("tracked.txt"));
        assert!(patch.contains("+three"));
        // Working-tree change is unstaged, so the --cached patch is empty.
        assert_eq!(
            capture_git_patch(&repo, true, DEFAULT_TIMEOUT_SECONDS),
            Some(String::new())
        );

        let outside = tmp.path().join("not-a-repo");
        fs::create_dir(&outside).unwrap();
        assert_eq!(capture_git_patch(&outside, false, DEFAULT_TIMEOUT_SECONDS), None);
    }

    // ------------------------------------------------------------------
    // _line_count helper (oracle-checked against the Python source: cap,
    // binary, trailing-newline, and escape rules)
    // ------------------------------------------------------------------

    #[test]
    fn line_count_matches_python_helper_rules() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().canonicalize().unwrap();
        fs::write(root.join("nl.txt"), "a\nb\n").unwrap();
        fs::write(root.join("no-trailing.txt"), "a\nb").unwrap();
        fs::write(root.join("empty.txt"), "").unwrap();
        fs::write(root.join("binary.bin"), b"a\x00b\n").unwrap();
        assert_eq!(line_count(&root, "nl.txt"), 2);
        assert_eq!(line_count(&root, "no-trailing.txt"), 2);
        assert_eq!(line_count(&root, "empty.txt"), 0);
        assert_eq!(line_count(&root, "binary.bin"), 0);
        assert_eq!(line_count(&root, "missing.txt"), 0);
        assert_eq!(line_count(&root, "../escape.txt"), 0);
    }
}
