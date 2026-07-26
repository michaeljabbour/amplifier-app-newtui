//! Per-project persistent prompt history (cross-session `↑` recall).
//!
//! Port of the Python app's `kernel/prompt_history.py`. The composer keeps an
//! in-memory `↑` ring for the *current* session; this store persists submitted
//! prompts per working directory so a fresh session recalls prior ones.
//!
//! Persistence mirrors amplifier-app-cli's behavior exactly: submitted prompts
//! land in `~/.amplifier/projects/<project-slug>/repl_history`, keyed the same
//! way session storage is (`get_project_slug`), so newtui and app-cli share
//! one history file per directory. The on-disk format is prompt-toolkit's
//! `FileHistory` (a `# <timestamp>` comment line then one `+<line>` per prompt
//! line), reproduced here without importing prompt-toolkit so an entry written
//! by either app reads back identically.

use std::fs;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::model::redaction::scrub_text;

/// Per-project prompt-history file — the name amplifier-app-cli uses so the
/// two apps share one history per working directory.
pub const HISTORY_FILENAME: &str = "repl_history";

/// Cap on stored prompts (matches the composer's in-memory ring). When an
/// append pushes past it the file is rewritten to the most-recent slice.
pub const MAX_PROMPT_HISTORY_ENTRIES: usize = 500;

// ---------------------------------------------------------------------------
// get_project_slug — inline-ported from the Python app's `kernel/config.py`
// (config.py itself stays Python/backend; only this pure helper is needed
// here for the default history path).
// ---------------------------------------------------------------------------

/// Deterministic project slug from the project directory path.
///
/// `/Users/me/dev/proj` → `-Users-me-dev-proj` (matches the amplifier-app-cli
/// convention so session storage is shared).
pub fn get_project_slug(project_dir: Option<&Path>) -> String {
    let resolved = match project_dir {
        Some(dir) => resolve_path(dir),
        None => resolve_path(&std::env::current_dir().unwrap_or_default()),
    };
    let slug = resolved
        .to_string_lossy()
        .replace(['/', '\\'], "-")
        .replace(':', "");
    if slug.starts_with('-') {
        slug
    } else {
        format!("-{slug}")
    }
}

/// Mirror Python's non-strict `Path.resolve()`: make the path absolute and
/// resolve symlinks in the longest existing prefix, appending the
/// non-existent tail verbatim (canonicalize alone fails on missing paths).
fn resolve_path(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    if let Ok(canonical) = absolute.canonicalize() {
        return canonical;
    }
    let mut prefix = absolute.clone();
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    loop {
        match prefix.file_name() {
            Some(name) => {
                tail.push(name.to_os_string());
                prefix.pop();
            }
            None => return absolute,
        }
        if let Ok(canonical) = prefix.canonicalize() {
            let mut out = canonical;
            for component in tail.iter().rev() {
                out.push(component);
            }
            return out;
        }
    }
}

// ---------------------------------------------------------------------------
// FileHistory format (prompt-toolkit byte-compatible)
// ---------------------------------------------------------------------------

/// Parse prompt-toolkit `FileHistory` text into oldest-first prompts.
///
/// Content lines start with `+` (the marker stripped, joined by `\n`); any
/// other line (the `# timestamp` comment, a blank line) ends the current
/// entry. Byte-compatible with prompt-toolkit so an app-cli-written file
/// reads back verbatim.
pub fn parse_history(text: &str) -> Vec<String> {
    let mut strings: Vec<String> = Vec::new();
    let mut lines: Vec<&str> = Vec::new();

    fn flush(lines: &[&str], strings: &mut Vec<String>) {
        if !lines.is_empty() {
            let mut joined = lines.concat();
            // Python strips exactly one trailing "\n" (`joined[:-1]`).
            if joined.ends_with('\n') {
                joined.pop();
            }
            strings.push(joined);
        }
    }

    for raw in split_lines_keepends(text) {
        if let Some(rest) = raw.strip_prefix('+') {
            lines.push(rest);
        } else {
            flush(&lines, &mut strings);
            lines.clear();
        }
    }
    flush(&lines, &mut strings);
    strings
}

/// Render one prompt as a prompt-toolkit `FileHistory` record.
pub fn format_entry(prompt: &str) -> String {
    let body: String = prompt.split('\n').map(|line| format!("+{line}\n")).collect();
    format!("\n# {}\n{}", now_timestamp(), body)
}

/// Split `text` like Python's `str.splitlines(keepends=True)` — the full set
/// of Unicode line boundaries Python recognizes, with `\r\n` kept as one.
fn split_lines_keepends(text: &str) -> Vec<&str> {
    fn is_line_break(c: char) -> bool {
        matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        )
    }
    let mut segments = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((idx, c)) = chars.next() {
        if is_line_break(c) {
            let mut end = idx + c.len_utf8();
            if c == '\r' {
                if let Some(&(next_idx, '\n')) = chars.peek() {
                    end = next_idx + 1;
                    chars.next();
                }
            }
            segments.push(&text[start..end]);
            start = end;
        }
    }
    if start < text.len() {
        segments.push(&text[start..]);
    }
    segments
}

/// `str(datetime.now())`-shaped timestamp for the `#` comment line.
///
/// Divergence noted honestly: Python renders local time; without a timezone
/// dependency this renders UTC. The line is a comment — `parse_history`
/// (here and in prompt-toolkit) ignores its content entirely.
fn now_timestamp() -> String {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = elapsed.as_secs();
    let micros = elapsed.subsec_micros();
    let (year, month, day) = civil_from_days((secs / 86_400) as i64);
    let rem = secs % 86_400;
    let (hour, minute, second) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    // Python's str(datetime) omits the fractional part when microsecond == 0.
    if micros == 0 {
        format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}")
    } else {
        format!(
            "{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}.{micros:06}"
        )
    }
}

/// Days-since-epoch → (year, month, day) — Howard Hinnant's civil_from_days.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Drop a prompt equal to the one immediately before it (composer parity).
fn dedup_consecutive(prompts: Vec<String>) -> Vec<String> {
    let mut deduped: Vec<String> = Vec::new();
    for prompt in prompts {
        if deduped.last() == Some(&prompt) {
            continue;
        }
        deduped.push(prompt);
    }
    deduped
}

// ---------------------------------------------------------------------------
// PromptHistoryStore
// ---------------------------------------------------------------------------

/// Filesystem store for one project's submitted prompts.
///
/// Contract:
/// - Inputs: prompt strings (secret-shaped substrings are scrubbed at the
///   sink via `model::redaction`, matching every other persistence sink).
/// - Side effects: reads/writes `<project>/repl_history`.
/// - Errors: swallowed — prompt history is best-effort and must never break
///   a submit or a boot (Python logs a warning; no logger here).
#[derive(Debug, Clone)]
pub struct PromptHistoryStore {
    pub path: PathBuf,
    pub max_entries: usize,
}

impl PromptHistoryStore {
    /// Mirror the Python constructor: an explicit `path` wins; otherwise the
    /// path is derived from `project_dir` (or the cwd) via the project slug.
    pub fn new(
        path: Option<PathBuf>,
        project_dir: Option<&Path>,
        max_entries: usize,
    ) -> Self {
        let path = path.unwrap_or_else(|| {
            home_dir()
                .join(".amplifier")
                .join("projects")
                .join(get_project_slug(project_dir))
                .join(HISTORY_FILENAME)
        });
        Self {
            path,
            max_entries: max_entries.max(1),
        }
    }

    /// Store backed by an explicit file path (default cap).
    pub fn at_path(path: PathBuf) -> Self {
        Self::new(Some(path), None, MAX_PROMPT_HISTORY_ENTRIES)
    }

    /// Store keyed by a project directory's slug (default cap).
    pub fn for_project_dir(project_dir: &Path) -> Self {
        Self::new(None, Some(project_dir), MAX_PROMPT_HISTORY_ENTRIES)
    }

    // -- read ---------------------------------------------------------------

    /// Oldest-first prompts (newest last), consecutive-deduped and capped to
    /// `max_entries` so a large shared file never floods the seed.
    ///
    /// Newest-last matches the composer's ring so `↑` walks
    /// most-recent-first.
    pub fn load(&self) -> Vec<String> {
        self.load_with_limit(self.max_entries)
    }

    /// [`Self::load`] with an explicit cap. (Python's negative-`limit`
    /// "return everything" escape hatch is not representable with `usize`;
    /// no caller or test uses it.)
    pub fn load_with_limit(&self, limit: usize) -> Vec<String> {
        let mut entries = dedup_consecutive(self.read_entries());
        entries.split_off(entries.len().saturating_sub(limit))
    }

    // -- write --------------------------------------------------------------

    /// Persist `prompt`; return whether it was recorded.
    ///
    /// Empty/whitespace-only prompts and immediate consecutive duplicates are
    /// skipped (composer parity). Secret-shaped substrings are scrubbed
    /// before anything hits disk. The file is trimmed to `max_entries`.
    pub fn append(&self, prompt: &str) -> bool {
        let cleaned = scrub_text(prompt).trim().to_string();
        if cleaned.is_empty() {
            return false;
        }
        let mut entries = self.read_entries();
        if entries.last() == Some(&cleaned) {
            return false;
        }
        entries.push(cleaned.clone());
        let result = if entries.len() > self.max_entries {
            let recent = entries.split_off(entries.len() - self.max_entries);
            self.write_all(&recent)
        } else {
            self.append_one(&cleaned)
        };
        result.is_ok()
    }

    // -- internals ----------------------------------------------------------

    fn read_entries(&self) -> Vec<String> {
        match fs::read_to_string(&self.path) {
            Ok(text) => parse_history(&text),
            // Missing file and any other read error alike: best-effort empty.
            Err(_) => Vec::new(),
        }
    }

    fn append_one(&self, prompt: &str) -> std::io::Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut handle = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        handle.write_all(format_entry(prompt).as_bytes())
    }

    fn write_all(&self, prompts: &[String]) -> std::io::Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let content: String = prompts.iter().map(|p| format_entry(p)).collect();
        let tmp = self.path.with_file_name(format!(
            "{}.tmp",
            self.path
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default()
        ));
        fs::write(&tmp, content)?;
        fs::rename(&tmp, &self.path)
    }
}

/// `Path.home()` equivalent — `$HOME` (this client targets Unix terminals).
fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Tests — port of tests/test_kernel_prompt_history.py (each Rust test is
// named after the Python test it pins), plus the get_project_slug case from
// tests/test_kernel_session_config.py.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, MutexGuard, OnceLock};
    use tempfile::TempDir;

    fn store_in(dir: &TempDir) -> PromptHistoryStore {
        PromptHistoryStore::at_path(dir.path().join("repl_history"))
    }

    /// Serialize the tests that monkeypatch `$HOME` (the Python tests
    /// monkeypatch `Path.home`); restores the prior value on drop.
    struct HomeGuard {
        _lock: MutexGuard<'static, ()>,
        previous: Option<std::ffi::OsString>,
    }

    fn set_home(dir: &Path) -> HomeGuard {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        let lock = LOCK
            .get_or_init(|| Mutex::new(()))
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let previous = std::env::var_os("HOME");
        std::env::set_var("HOME", dir);
        HomeGuard {
            _lock: lock,
            previous,
        }
    }

    impl Drop for HomeGuard {
        fn drop(&mut self) {
            match &self.previous {
                Some(value) => std::env::set_var("HOME", value),
                None => std::env::remove_var("HOME"),
            }
        }
    }

    // -- format round-trip (prompt-toolkit FileHistory compatibility) -------

    #[test]
    fn test_format_and_parse_roundtrip_single_line() {
        let text = format_entry("hello world");
        assert_eq!(parse_history(&text), vec!["hello world"]);
    }

    #[test]
    fn test_format_and_parse_roundtrip_multiline() {
        let text = format_entry("line one\nline two");
        assert_eq!(parse_history(&text), vec!["line one\nline two"]);
    }

    #[test]
    fn test_parse_reads_appcli_written_file() {
        // A file in prompt-toolkit's on-disk format (what app-cli writes)
        // reads back verbatim, so the two apps share one history file.
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("repl_history");
        fs::write(
            &path,
            "\n# 2026-07-24 10:00:00.000001\n+first prompt\n\
             \n# 2026-07-24 10:01:00.000002\n+second\n+multi\n",
        )
        .unwrap();
        let store = PromptHistoryStore::at_path(path);
        assert_eq!(store.load(), vec!["first prompt", "second\nmulti"]);
    }

    // -- append / load -------------------------------------------------------

    #[test]
    fn test_append_then_load_is_oldest_first() {
        let dir = TempDir::new().unwrap();
        let store = store_in(&dir);
        assert!(store.append("first"));
        assert!(store.append("second"));
        // Newest last so ↑ walks most-recent-first.
        assert_eq!(store.load(), vec!["first", "second"]);
    }

    #[test]
    fn test_load_missing_file_is_empty() {
        let dir = TempDir::new().unwrap();
        let store = PromptHistoryStore::at_path(dir.path().join("absent"));
        assert_eq!(store.load(), Vec::<String>::new());
    }

    #[test]
    fn test_append_skips_empty_and_whitespace() {
        let dir = TempDir::new().unwrap();
        let store = store_in(&dir);
        assert!(!store.append(""));
        assert!(!store.append("   \n  "));
        assert_eq!(store.load(), Vec::<String>::new());
    }

    #[test]
    fn test_append_strips_surrounding_whitespace() {
        let dir = TempDir::new().unwrap();
        let store = store_in(&dir);
        store.append("  padded  ");
        assert_eq!(store.load(), vec!["padded"]);
    }

    // -- dedup (consecutive only — composer parity) --------------------------

    #[test]
    fn test_append_skips_consecutive_duplicate() {
        let dir = TempDir::new().unwrap();
        let store = store_in(&dir);
        assert!(store.append("same"));
        assert!(!store.append("same"));
        assert_eq!(store.load(), vec!["same"]);
    }

    #[test]
    fn test_non_consecutive_duplicate_is_kept() {
        let dir = TempDir::new().unwrap();
        let store = store_in(&dir);
        store.append("a");
        store.append("b");
        store.append("a");
        assert_eq!(store.load(), vec!["a", "b", "a"]);
    }

    #[test]
    fn test_load_dedups_consecutive_from_disk() {
        // A file with consecutive dupes (e.g. app-cli, which does not dedup)
        // is deduped on load to mirror the composer ring.
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("repl_history");
        fs::write(&path, format_entry("dup") + &format_entry("dup")).unwrap();
        assert_eq!(PromptHistoryStore::at_path(path).load(), vec!["dup"]);
    }

    // -- secret scrubbing (model.redaction policy at the sink) ---------------

    #[test]
    fn test_append_scrubs_secret_shaped_values() {
        let dir = TempDir::new().unwrap();
        let store = store_in(&dir);
        store.append("my key is AKIAIOSFODNN7EXAMPLE ok");
        let loaded = store.load();
        assert_eq!(loaded.len(), 1);
        let stored = &loaded[0];
        assert!(!stored.contains("AKIAIOSFODNN7EXAMPLE"));
        assert!(stored.contains("[REDACTED]"));
    }

    // -- cap / bound ----------------------------------------------------------

    #[test]
    fn test_cap_bounds_stored_and_loaded_entries() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("repl_history");
        let store = PromptHistoryStore::new(Some(path.clone()), None, 3);
        for i in 0..6 {
            store.append(&format!("p{i}"));
        }
        // Most recent kept, oldest dropped.
        assert_eq!(store.load(), vec!["p3", "p4", "p5"]);
        // The file itself was trimmed, not just the load view.
        assert_eq!(
            PromptHistoryStore::at_path(path).load(),
            vec!["p3", "p4", "p5"]
        );
    }

    #[test]
    fn test_load_limit_caps_to_most_recent() {
        let dir = TempDir::new().unwrap();
        let store = store_in(&dir);
        for i in 0..5 {
            store.append(&format!("p{i}"));
        }
        assert_eq!(store.load_with_limit(2), vec!["p3", "p4"]);
    }

    // -- per-directory isolation (slug keying, HOME pointed at a tmp dir) ----

    #[test]
    fn test_default_path_is_project_slug_keyed() {
        let dir = TempDir::new().unwrap();
        let _home = set_home(dir.path());
        let project = dir.path().join("work").join("proj-x");
        let store = PromptHistoryStore::for_project_dir(&project);
        let expected = dir
            .path()
            .join(".amplifier")
            .join("projects")
            .join(get_project_slug(Some(&project)))
            .join(HISTORY_FILENAME);
        assert_eq!(store.path, expected);
    }

    #[test]
    fn test_history_is_isolated_per_directory() {
        let dir = TempDir::new().unwrap();
        let _home = set_home(dir.path());
        let dir_x = dir.path().join("work").join("x");
        let dir_y = dir.path().join("work").join("y");
        fs::create_dir_all(&dir_x).unwrap();
        fs::create_dir_all(&dir_y).unwrap();

        PromptHistoryStore::for_project_dir(&dir_x).append("command A");

        // A fresh store for the SAME dir recalls it; a different dir does not.
        assert_eq!(
            PromptHistoryStore::for_project_dir(&dir_x).load(),
            vec!["command A"]
        );
        assert_eq!(
            PromptHistoryStore::for_project_dir(&dir_y).load(),
            Vec::<String>::new()
        );
    }

    // -- get_project_slug (pinned from tests/test_kernel_session_config.py) --

    #[test]
    fn test_get_project_slug() {
        let dir = TempDir::new().unwrap();
        let slug = get_project_slug(Some(dir.path()));
        assert!(slug.starts_with('-'));
        assert!(!slug.contains('/') && !slug.contains(':'));
    }
}
