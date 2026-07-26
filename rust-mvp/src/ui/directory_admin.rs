//! In-session `/allowed-dirs` and `/denied-dirs` UI controller.
//!
//! Port of `src/amplifier_app_newtui/ui/directory_admin.py`. The Python
//! module is a pure controller: it parses the slash-command arguments,
//! validates `add` targets against the filesystem, and drives a host
//! protocol (`adapter` + `allocator` + `append_block` + `show_notice`).
//!
//! Adaptation notes:
//! - The Python `DirectoryAdminHost` protocol exposes `adapter` and
//!   `allocator` attributes; here those are flattened into trait methods
//!   on [`DirectoryAdminHost`] (same pattern as `ui/file_mentions.rs`
//!   `MentionHost`). App assembly must forward `directory_entries` /
//!   `update_directory` to the session adapter (which lives on the Python
//!   side of the protocol boundary) and `next_id` to the transcript's
//!   `BlockIdAllocator`.
//! - Python `manage` is `async` only because the adapter calls are
//!   awaited; the control flow is strictly sequential, so the port is a
//!   synchronous function.

use std::path::Path;

use crate::model::blocks::{Answer, Segment, StyleToken};

/// `DirectoryKind = Literal["allowed", "denied"]` from
/// `kernel/directory_permissions.py` (that kernel unit is not ported;
/// only this literal surface is needed client-side).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DirectoryKind {
    Allowed,
    Denied,
}

impl DirectoryKind {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            DirectoryKind::Allowed => "allowed",
            DirectoryKind::Denied => "denied",
        }
    }
}

/// `kernel.directory_permissions.DirectoryEntry` — configured path with
/// most-specific-scope provenance. Mirrored here because only the two
/// display fields cross the adapter boundary.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DirectoryEntry {
    pub path: String,
    pub scope: String,
}

/// Host surface `manage` drives (Python `DirectoryAdminHost` protocol with
/// the `adapter`/`allocator` attributes flattened into methods).
pub trait DirectoryAdminHost {
    /// `host.adapter.directory_entries(kind)`.
    fn directory_entries(&mut self, kind: DirectoryKind) -> Vec<DirectoryEntry>;
    /// `host.adapter.update_directory(kind, operation, path)` →
    /// `(ok, detail)`.
    fn update_directory(
        &mut self,
        kind: DirectoryKind,
        operation: &str,
        path: &str,
    ) -> (bool, String);
    /// `host.allocator.next_id()`.
    fn next_id(&mut self) -> String;
    /// `host.append_block(block)` — only [`Answer`] blocks flow through
    /// this controller.
    fn append_block(&mut self, block: Answer);
    /// `host.show_notice(text, duration)`.
    fn show_notice(&mut self, text: &str, duration: Option<f64>);
}

/// Python `_spans(kind, entries)`.
fn spans(kind: DirectoryKind, entries: &[DirectoryEntry]) -> Vec<Segment> {
    let title = if kind == DirectoryKind::Allowed {
        "Allowed write directories"
    } else {
        "Denied write directories"
    };
    let color = if kind == DirectoryKind::Allowed {
        StyleToken::Green
    } else {
        StyleToken::Red
    };
    let mut out = vec![
        Segment {
            style_token: color,
            ..Segment::new("· ")
        },
        Segment {
            style_token: StyleToken::Bright,
            bold: true,
            ..Segment::new(title)
        },
        Segment {
            style_token: StyleToken::Dim,
            ..Segment::new("\n")
        },
    ];
    if entries.is_empty() {
        out.push(Segment {
            style_token: StyleToken::Dimmer,
            ..Segment::new("  none configured")
        });
    } else {
        for entry in entries {
            out.push(Segment::new(format!(
                "  {}  ({})\n",
                entry.path, entry.scope
            )));
        }
    }
    out.push(Segment {
        style_token: StyleToken::Dimmer,
        ..Segment::new(format!(
            "  /{}-dirs add <path> · remove <path>",
            kind.as_str()
        ))
    });
    out
}

/// Python `Path(path).expanduser()` (bare `~` and `~/…` only; same
/// semantics as the private helper in `kernel/safety.rs`).
fn expand_user(path: &str) -> String {
    if path == "~" || path.starts_with("~/") {
        if let Ok(home) = std::env::var("HOME") {
            if path == "~" {
                return home;
            }
            return format!("{}/{}", home.trim_end_matches('/'), &path[2..]);
        }
    }
    path.to_string()
}

/// `str(pathlib.Path(p))` — drop `.` components, collapse repeated
/// slashes, trim the trailing slash; empty → `"."`. (`..` is kept, and a
/// double leading slash is preserved per POSIX/pathlib.)
fn python_path_display(path: &str) -> String {
    let root = if path.starts_with("//") && !path.starts_with("///") {
        "//"
    } else if path.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts: Vec<&str> = path
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect();
    if parts.is_empty() {
        if root.is_empty() {
            ".".to_string()
        } else {
            root.to_string()
        }
    } else {
        format!("{}{}", root, parts.join("/"))
    }
}

/// Emit the current entries as an [`Answer`] block (shared list tail of
/// the `list` and successful-mutation branches).
fn append_entries_block(host: &mut dyn DirectoryAdminHost, kind: DirectoryKind) {
    let entries = host.directory_entries(kind);
    let id = host.next_id();
    host.append_block(Answer::new(id, spans(kind, &entries)));
}

/// Python `manage(host, kind, args)`.
///
/// `kind` stays a plain string at this boundary (slash-command plumbing
/// passes it through verbatim); anything but `"allowed"`/`"denied"` gets
/// the `unknown directory policy` notice, exactly like Python.
pub fn manage(host: &mut dyn DirectoryAdminHost, kind: &str, args: &str) {
    let typed_kind = match kind {
        "allowed" => DirectoryKind::Allowed,
        "denied" => DirectoryKind::Denied,
        other => {
            host.show_notice(&format!("unknown directory policy · {other}"), None);
            return;
        }
    };
    // Python `args.strip().split(maxsplit=1)`.
    let trimmed = args.trim();
    let (first, rest) = match trimmed.split_once(char::is_whitespace) {
        Some((head, tail)) => (head, Some(tail)),
        None => (trimmed, None),
    };
    let operation = first.to_lowercase();
    if operation.is_empty() || operation == "list" {
        append_entries_block(host, typed_kind);
        return;
    }
    if (operation != "add" && operation != "remove") || rest.is_none() {
        host.show_notice(
            &format!("usage: /{kind}-dirs list | add <path> | remove <path>"),
            None,
        );
        return;
    }
    let raw_path = rest
        .unwrap_or_default()
        .trim()
        .trim_matches(|c| c == '\'' || c == '"');
    let expanded = expand_user(raw_path);
    if operation == "add" && !Path::new(&expanded).is_dir() {
        // Catches typos and doubled pastes (e.g. "add ~/x add ~/x" swallowed
        // as one garbage path) before they poison the session allowlist.
        // ``remove`` stays unvalidated so stale/garbage entries can be removed.
        host.show_notice(
            &format!(
                "not an existing directory · {} — nothing added",
                python_path_display(&expanded)
            ),
            None,
        );
        return;
    }
    let (ok, detail) = host.update_directory(typed_kind, &operation, raw_path);
    host.show_notice(&detail, None);
    if ok {
        append_entries_block(host, typed_kind);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeHost {
        calls: Vec<(String, String, String)>,
        blocks: Vec<Answer>,
        notices: Vec<String>,
        next: u64,
    }

    impl FakeHost {
        fn new() -> Self {
            Self {
                calls: Vec::new(),
                blocks: Vec::new(),
                notices: Vec::new(),
                next: 0,
            }
        }
    }

    impl DirectoryAdminHost for FakeHost {
        fn directory_entries(&mut self, _kind: DirectoryKind) -> Vec<DirectoryEntry> {
            Vec::new()
        }

        fn update_directory(
            &mut self,
            kind: DirectoryKind,
            operation: &str,
            path: &str,
        ) -> (bool, String) {
            self.calls.push((
                kind.as_str().to_string(),
                operation.to_string(),
                path.to_string(),
            ));
            (true, format!("session {} · {}", kind.as_str(), path))
        }

        fn next_id(&mut self) -> String {
            self.next += 1;
            format!("b{}", self.next)
        }

        fn append_block(&mut self, block: Answer) {
            self.blocks.push(block);
        }

        fn show_notice(&mut self, text: &str, _duration: Option<f64>) {
            self.notices.push(text.to_string());
        }
    }

    fn call(kind: &str, op: &str, path: &str) -> (String, String, String) {
        (kind.to_string(), op.to_string(), path.to_string())
    }

    #[test]
    fn test_add_existing_directory_reaches_adapter() {
        let tmp = tempfile::tempdir().unwrap();
        let tmp_path = tmp.path().to_string_lossy().into_owned();
        let mut host = FakeHost::new();
        manage(&mut host, "allowed", &format!("add {tmp_path}"));
        assert_eq!(host.calls, vec![call("allowed", "add", &tmp_path)]);
    }

    /// Regression: a doubled paste ("add ~/x/allowed-dirs add ~/x") used to
    /// be swallowed verbatim as one garbage path and stored in the session
    /// allowlist. Nonexistent directories must be refused before the
    /// adapter is called.
    #[test]
    fn test_add_nonexistent_path_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let tmp_path = tmp.path().to_string_lossy().into_owned();
        let mut host = FakeHost::new();
        let garbage = format!("{tmp_path}/allowed-dirs add {tmp_path}");
        manage(&mut host, "allowed", &format!("add {garbage}"));
        assert!(host.calls.is_empty());
        assert!(!host.notices.is_empty());
        assert!(host.notices[0].contains("not an existing directory"));
    }

    #[test]
    fn test_add_strips_surrounding_quotes() {
        let tmp = tempfile::tempdir().unwrap();
        let tmp_path = tmp.path().to_string_lossy().into_owned();
        let mut host = FakeHost::new();
        manage(&mut host, "allowed", &format!("add \"{tmp_path}\""));
        assert_eq!(host.calls, vec![call("allowed", "add", &tmp_path)]);
    }

    /// Stale or garbage entries must remain removable even though the path
    /// does not exist on disk.
    #[test]
    fn test_remove_skips_existence_validation() {
        let tmp = tempfile::tempdir().unwrap();
        let tmp_path = tmp.path().to_string_lossy().into_owned();
        let mut host = FakeHost::new();
        let garbage = format!("{tmp_path}/allowed-dirs add {tmp_path}");
        manage(&mut host, "allowed", &format!("remove {garbage}"));
        assert_eq!(host.calls, vec![call("allowed", "remove", &garbage)]);
    }

    /// Allowed/denied entries are directories; a file path is a mistake.
    #[test]
    fn test_add_file_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let target = tmp.path().join("notes.txt");
        std::fs::write(&target, "x").unwrap();
        let mut host = FakeHost::new();
        manage(
            &mut host,
            "allowed",
            &format!("add {}", target.to_string_lossy()),
        );
        assert!(host.calls.is_empty());
        assert!(!host.notices.is_empty());
        assert!(host.notices[0].contains("not an existing directory"));
    }
}
