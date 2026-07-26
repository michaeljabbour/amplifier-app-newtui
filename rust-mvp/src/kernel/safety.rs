//! Two-axis safety resolution: approval policy and execution path policy.
//!
//! Port of `src/amplifier_app_newtui/kernel/safety.py`.
//!
//! Approval answers whether a tool call may proceed without a human decision.
//! Path policy independently answers where a recognizable action may operate.
//! Keeping both axes explicit prevents an allowlisted command from silently
//! bypassing configured directory boundaries and gives future OS sandboxes a
//! stable policy seam without claiming that one exists today.
//!
//! `DirectoryPolicy` below is an inline port of ONLY the surface of
//! `kernel/directory_permissions.py` that `safety.py` actually consumes
//! (constructor, `check_write`, `check_read`, `within_allowed`,
//! `shell_outside_target`); the persistence/settings/session-mutation half of
//! that module stays in the Python backend.

use std::collections::HashSet;
use std::fmt;
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::model::trust::{CapabilityClass, TrustDecision};

/// `ExecutionPolicyDecision = Literal["not-applicable", "within-policy",
/// "outside-policy", "blocked"]`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum ExecutionPolicyDecision {
    #[serde(rename = "not-applicable")]
    NotApplicable,
    #[serde(rename = "within-policy")]
    WithinPolicy,
    #[serde(rename = "outside-policy")]
    OutsidePolicy,
    #[serde(rename = "blocked")]
    Blocked,
}

impl ExecutionPolicyDecision {
    /// The exact Python literal string.
    pub fn value(self) -> &'static str {
        match self {
            ExecutionPolicyDecision::NotApplicable => "not-applicable",
            ExecutionPolicyDecision::WithinPolicy => "within-policy",
            ExecutionPolicyDecision::OutsidePolicy => "outside-policy",
            ExecutionPolicyDecision::Blocked => "blocked",
        }
    }
}

impl fmt::Display for ExecutionPolicyDecision {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.value())
    }
}

/// Independent approval and path-policy outcomes for one tool call.
///
/// Frozen dataclass in Python; treated as immutable by convention here.
#[derive(Clone, Debug, PartialEq)]
pub struct SafetyResolution {
    pub approval: TrustDecision,
    pub execution_policy: ExecutionPolicyDecision,
    pub policy_reason: String,
    pub target: String,
}

impl SafetyResolution {
    fn new(
        approval: TrustDecision,
        execution_policy: ExecutionPolicyDecision,
        policy_reason: impl Into<String>,
        target: impl Into<String>,
    ) -> Self {
        Self {
            approval,
            execution_policy,
            policy_reason: policy_reason.into(),
            target: target.into(),
        }
    }

    /// Python `blocked` property.
    pub fn blocked(&self) -> bool {
        self.execution_policy == ExecutionPolicyDecision::Blocked
    }
}

/// Resolve path policy without changing approval-policy precedence.
///
/// `resolve_capability` is the callback the kernel passes so the mode table
/// stays the single policy source (usually
/// `|cap| model::trust::resolve_capability(mode, cap)`).
pub fn resolve_safety<F>(
    approval: TrustDecision,
    action: &str,
    target: &str,
    directory_policy: Option<&DirectoryPolicy>,
    resolve_capability: F,
) -> SafetyResolution
where
    F: Fn(CapabilityClass) -> TrustDecision,
{
    let policy = match directory_policy {
        Some(policy) => policy,
        None => {
            return SafetyResolution::new(
                approval,
                ExecutionPolicyDecision::NotApplicable,
                "",
                target,
            )
        }
    };

    let capability = approval.capability;
    if capability == CapabilityClass::Write && !target.is_empty() {
        let (allowed, reason) = policy.check_write(target);
        return SafetyResolution::new(
            approval,
            if allowed {
                ExecutionPolicyDecision::WithinPolicy
            } else {
                ExecutionPolicyDecision::Blocked
            },
            reason,
            target,
        );
    }

    if capability == CapabilityClass::Read && !target.is_empty() {
        if policy.within_allowed(target) {
            return SafetyResolution::new(
                approval,
                ExecutionPolicyDecision::WithinPolicy,
                "within allowed directories",
                target,
            );
        }
        let (allowed, reason) = policy.check_read(target);
        if !allowed {
            return SafetyResolution::new(approval, ExecutionPolicyDecision::Blocked, reason, target);
        }
        // Reads roam anywhere outside denied directories (within reason) —
        // matching amplifier-app-cli's permissive read defaults. The
        // outside-project gate applies to writes and write-shaped shell.
        return SafetyResolution::new(approval, ExecutionPolicyDecision::WithinPolicy, reason, target);
    }

    if capability == CapabilityClass::Exec {
        let outside = policy.shell_outside_target(action);
        let (path, reason) = match outside {
            None => {
                return SafetyResolution::new(
                    approval,
                    ExecutionPolicyDecision::WithinPolicy,
                    "no outside or protected path detected",
                    target,
                )
            }
            Some(found) => found,
        };
        if reason.starts_with("path is protected") || reason.starts_with("path is within denied") {
            return SafetyResolution::new(approval, ExecutionPolicyDecision::Blocked, reason, path);
        }
        return SafetyResolution::new(
            resolve_capability(CapabilityClass::OutsideProject),
            ExecutionPolicyDecision::OutsidePolicy,
            reason,
            path,
        );
    }

    SafetyResolution::new(approval, ExecutionPolicyDecision::NotApplicable, "", target)
}

// ---------------------------------------------------------------------------
// Minimal DirectoryPolicy surface, inline-ported from
// `src/amplifier_app_newtui/kernel/directory_permissions.py`. Only what
// `resolve_safety` consumes lives here; settings persistence, session
// mutation, and mount-plan plumbing stay Python-side.
// ---------------------------------------------------------------------------

/// `WriteBoundary = Literal["open", "guarded"]` — app-level write posture.
///
/// `open` (default) matches amplifier-app-cli: no governance pre-flight for
/// writes outside the project and no write-shaped shell gating — the mounted
/// filesystem tool remains the sole write-path enforcement. `guarded`
/// restores the app-level gate. Denied and protected paths are enforced in
/// both postures.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum WriteBoundary {
    #[serde(rename = "open")]
    #[default]
    Open,
    #[serde(rename = "guarded")]
    Guarded,
}

/// `PROTECTED_PROJECT_PATHS`: instruction and repository-control paths denied
/// inside writable roots.
pub const PROTECTED_PROJECT_PATHS: [&str; 4] = [".git", ".agents", ".codex", "AGENTS.md"];

/// `_WRITE_COMMANDS`: command heads that treat their path arguments as write
/// targets.
const WRITE_COMMANDS: [&str; 17] = [
    "chgrp", "chmod", "chown", "cp", "dd", "install", "ln", "mkdir", "mv", "rm", "rmdir",
    "rsync", "shred", "tee", "touch", "truncate", "unlink",
];

const COMMAND_SEPARATORS: [&str; 4] = ["&&", "||", ";", "|"];

/// Mutable effective write boundary shared by filesystem and governance
/// (read-only surface here — session mutation is not ported).
pub struct DirectoryPolicy {
    pub write_boundary: WriteBoundary,
    project_dir: PathBuf,
    allowed: Vec<PathBuf>,
    denied: Vec<PathBuf>,
    protected: Vec<PathBuf>,
}

impl DirectoryPolicy {
    /// Python `DirectoryPolicy(project_dir)` with keyword defaults.
    pub fn new(project_dir: &Path) -> Self {
        Self::with_options(project_dir, &[], &[], WriteBoundary::Open)
    }

    /// Python `DirectoryPolicy(project_dir, allowed=…, denied=…,
    /// write_boundary=…)`.
    pub fn with_options(
        project_dir: &Path,
        allowed: &[&str],
        denied: &[&str],
        write_boundary: WriteBoundary,
    ) -> Self {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/"));
        let project_dir = normalize_path(&project_dir.to_string_lossy(), &cwd);
        let mut base_allowed: Vec<String> = vec![project_dir.to_string_lossy().into_owned()];
        base_allowed.extend(allowed.iter().map(|s| s.to_string()));
        let protected: Vec<String> = PROTECTED_PROJECT_PATHS
            .iter()
            .map(|relative| project_dir.join(relative).to_string_lossy().into_owned())
            .collect();
        let mut denied_all: Vec<String> = protected.clone();
        denied_all.extend(denied.iter().map(|s| s.to_string()));
        Self {
            write_boundary,
            allowed: stable(&base_allowed, &project_dir),
            denied: stable(&denied_all, &project_dir),
            protected: stable(&protected, &project_dir),
            project_dir,
        }
    }

    /// Python `check_write(path)` (the `cwd` keyword is never passed by
    /// `safety.py`, so relative paths resolve against the project dir).
    pub fn check_write(&self, path: &str) -> (bool, String) {
        let resolved = normalize_path(path, &self.project_dir);
        let shown = resolved.to_string_lossy();
        if within_any(&resolved, &self.protected) {
            return (false, format!("path is protected by default \u{b7} {shown}"));
        }
        if within_any(&resolved, &self.denied) {
            return (false, format!("path is within denied directories \u{b7} {shown}"));
        }
        if within_any(&resolved, &self.allowed) {
            return (true, "within allowed write directories".to_string());
        }
        if self.write_boundary == WriteBoundary::Open {
            // App-cli parity: no app-level gate outside the project. The
            // mounted filesystem tool stays the hard write enforcement, so
            // write tools get a graceful tool error there — never a
            // governance block or an approval.
            return (
                true,
                format!("outside project \u{b7} filesystem tool enforces writes \u{b7} {shown}"),
            );
        }
        (
            false,
            format!("path is outside allowed write directories \u{b7} {shown}"),
        )
    }

    /// Python `check_read(path)`: reads roam anywhere except denied
    /// directories — denylist-bounded, not allowlist-bounded.
    pub fn check_read(&self, path: &str) -> (bool, String) {
        let resolved = normalize_path(path, &self.project_dir);
        if within_any(&resolved, &self.denied) {
            return (
                false,
                format!(
                    "path is within denied directories \u{b7} {}",
                    resolved.to_string_lossy()
                ),
            );
        }
        (
            true,
            "read roams outside the project \u{b7} denylist-bounded".to_string(),
        )
    }

    /// Python `within_allowed(path)`.
    pub fn within_allowed(&self, path: &str) -> bool {
        within_any(&normalize_path(path, &self.project_dir), &self.allowed)
    }

    /// Return the first shell path that escapes the write boundary.
    ///
    /// This is a governance signal, not a shell sandbox. Deny-listed and
    /// protected paths are flagged wherever they appear; merely-outside paths
    /// are flagged only in write contexts (write-command heads and
    /// redirection targets). Read-shaped commands may roam outside the
    /// project, while the mounted bash tool's own safety validator stays in
    /// charge of command form.
    pub fn shell_outside_target(&self, command: &str) -> Option<(String, String)> {
        let tokens = shlex_split(command)
            .unwrap_or_else(|| command.split_whitespace().map(str::to_string).collect());
        let cleaned: Vec<String> = tokens
            .iter()
            .map(|raw| {
                raw.trim_matches(|c| "'\";,(){}[]".contains(c)).to_string()
            })
            .collect();
        let mut heads: HashSet<String> = HashSet::new();
        if let Some(first) = cleaned.first() {
            heads.insert(path_name(first));
        }
        for (index, token) in cleaned.iter().enumerate() {
            if index + 1 < cleaned.len() && COMMAND_SEPARATORS.contains(&token.as_str()) {
                heads.insert(path_name(&cleaned[index + 1]));
            }
        }
        let write_head = heads.iter().any(|head| WRITE_COMMANDS.contains(&head.as_str()));
        let redirect_targets: HashSet<usize> = cleaned
            .iter()
            .enumerate()
            .filter(|(_, token)| token.as_str() == ">" || token.as_str() == ">>")
            .map(|(index, _)| index + 1)
            .collect();
        for (index, token) in cleaned.iter().enumerate() {
            if token.starts_with("http://")
                || token.starts_with("https://")
                || token.starts_with("/dev/")
            {
                continue;
            }
            if (token.contains('*') || token.contains('?'))
                && !write_head
                && !redirect_targets.contains(&index)
            {
                // A glob in a read-shaped command is a filter pattern, not a
                // concrete target — `find -not -path "./.git/*"` names .git
                // precisely to AVOID it. Write-shaped commands keep strict
                // flagging: `rm -rf .git/*` and `> .git/*` must still stop.
                continue;
            }
            let protected_relative = PROTECTED_PROJECT_PATHS
                .iter()
                .any(|relative| token == relative || token.starts_with(&format!("{relative}/")));
            let pathish = token.starts_with('/')
                || token.starts_with("~/")
                || token.starts_with("./")
                || token.starts_with("../");
            if !protected_relative && !pathish && !redirect_targets.contains(&index) {
                continue;
            }
            let (allowed, reason) = self.check_write(token);
            if allowed {
                continue;
            }
            if reason.starts_with("path is protected") || reason.starts_with("path is within denied")
            {
                return Some((token.clone(), reason));
            }
            if write_head || redirect_targets.contains(&index) {
                return Some((token.clone(), reason));
            }
        }
        // Fail-closed fallback (audit H1): the token pass above is
        // command-list based — writes via `python3 -c`, `sed -i`, `curl -o`,
        // or a directory-prefixed path hide the target from
        // write-head/redirect detection. Scan the raw string for a protected
        // reference and escalate to *ask*.
        self.embedded_protected_reference(command)
    }

    /// Return a protected path referenced anywhere in an EXEC command.
    ///
    /// An embedded reference is lower confidence than a target token — it may
    /// be a harmless mention — so the reason routes to *ask* (the human
    /// adjudicates) rather than a silent allow. `.gitignore`/`.github` never
    /// match `.git`; glob filters (`./.git/*`) stay exempt.
    fn embedded_protected_reference(&self, command: &str) -> Option<(String, String)> {
        for relative in PROTECTED_PROJECT_PATHS {
            if protected_reference_matches(command, relative) {
                return Some((
                    relative.to_string(),
                    format!(
                        "protected path referenced in command \u{b7} {relative} \u{b7} review before exec"
                    ),
                ));
            }
        }
        None
    }
}

/// Python `\w` approximation (Unicode alphanumerics plus underscore).
fn is_word_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Manual equivalent of `_compile_protected_pattern(relative).search(command)`
/// (`regex` has no lookbehind, and every pattern is a boundary-checked
/// literal, so a scan is exact).
///
/// Protected *files* (`AGENTS.md`) match as a whole path segment
/// (`(?<![\w.\-])AGENTS\.md(?![\w])`). Protected *directories* match only
/// when a concrete subpath follows and are exempt when a glob metacharacter
/// follows the slash (`(?<![\w.\-])\.git/(?![*?])`).
fn protected_reference_matches(command: &str, relative: &str) -> bool {
    let is_file = Path::new(relative).extension().is_some();
    let needle = if is_file {
        relative.to_string()
    } else {
        format!("{relative}/")
    };
    let mut search_from = 0;
    while let Some(found) = command[search_from..].find(&needle) {
        let start = search_from + found;
        let end = start + needle.len();
        let prev_ok = command[..start]
            .chars()
            .next_back()
            .is_none_or(|c| !(is_word_char(c) || c == '.' || c == '-'));
        let next = command[end..].chars().next();
        let next_ok = if is_file {
            next.is_none_or(|c| !is_word_char(c))
        } else {
            next.is_none_or(|c| c != '*' && c != '?')
        };
        if prev_ok && next_ok {
            return true;
        }
        search_from = start + 1;
    }
    false
}

/// Python `Path(token).name` (empty for `""`, `"."`, `".."`, and `/`).
fn path_name(token: &str) -> String {
    Path::new(token)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Python `str(Path(raw).expanduser().resolve(strict=False))` applied to each
/// value, deduplicated preserving order (`DirectoryPolicy._stable`).
fn stable(values: &[String], base: &Path) -> Vec<PathBuf> {
    let mut result: Vec<PathBuf> = Vec::new();
    for raw in values {
        // Python resolves relative config paths against the process cwd; the
        // policy surface here only ever receives absolute roots plus the
        // already-absolute project dir, so `base` is a stand-in that never
        // fires for the ported call sites.
        let value = normalize_path(raw, base);
        if !result.contains(&value) {
            result.push(value);
        }
    }
    result
}

/// Python `DirectoryPolicy._within_any`: candidate equals a root or has it as
/// an ancestor.
fn within_any(candidate: &Path, roots: &[PathBuf]) -> bool {
    roots.iter().any(|root| candidate.starts_with(root))
}

/// Python `Path(path).expanduser()` (bare `~` and `~/…` only).
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

/// Approximation of Python `Path.resolve(strict=False)`: absolutize against
/// `base`, then lexically normalize `.` and `..`.
///
/// Divergence (recorded): Python additionally resolves symlinks for existing
/// path prefixes; this port normalizes lexically. Containment checks stay
/// consistent because every root and candidate flows through this same
/// function.
fn normalize_path(path: &str, base: &Path) -> PathBuf {
    let expanded = expand_user(path);
    let candidate = Path::new(&expanded);
    let joined: PathBuf = if candidate.is_absolute() {
        candidate.to_path_buf()
    } else {
        base.join(candidate)
    };
    let mut result = PathBuf::new();
    for component in joined.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                // `PathBuf::pop` refuses to pop the root, matching Python's
                // `/..` → `/`.
                result.pop();
            }
            other => result.push(other),
        }
    }
    result
}

const PUNCTUATION_CHARS: &str = "();<>|&";

/// Subset of `shlex.shlex(command, posix=True, punctuation_chars=True)` with
/// `whitespace_split=True`: whitespace-split words, POSIX quote/escape
/// removal, runs of `();<>|&` as standalone tokens, `#` comments. Returns
/// `None` on a lexing error (unbalanced quote / trailing escape), mirroring
/// the Python `ValueError` fallback to `command.split()`.
fn shlex_split(command: &str) -> Option<Vec<String>> {
    let mut tokens: Vec<String> = Vec::new();
    let mut chars = command.chars().peekable();
    let mut current = String::new();
    let mut has_token = false; // distinguishes an explicit empty token ('')
    while let Some(c) = chars.next() {
        match c {
            '\'' => {
                has_token = true;
                loop {
                    match chars.next() {
                        Some('\'') => break,
                        Some(inner) => current.push(inner),
                        None => return None, // "No closing quotation"
                    }
                }
            }
            '"' => {
                has_token = true;
                loop {
                    match chars.next() {
                        Some('"') => break,
                        Some('\\') => match chars.next() {
                            Some(escaped @ ('"' | '\\' | '$' | '`')) => current.push(escaped),
                            Some(other) => {
                                current.push('\\');
                                current.push(other);
                            }
                            None => return None,
                        },
                        Some(inner) => current.push(inner),
                        None => return None,
                    }
                }
            }
            '\\' => match chars.next() {
                Some(escaped) => {
                    has_token = true;
                    current.push(escaped);
                }
                None => return None, // "No escaped character"
            },
            '#' => {
                // Comment to end of line (shlex commenters); flush any token.
                if has_token || !current.is_empty() {
                    tokens.push(std::mem::take(&mut current));
                    has_token = false;
                }
                for skipped in chars.by_ref() {
                    if skipped == '\n' {
                        break;
                    }
                }
            }
            _ if c.is_whitespace() => {
                if has_token || !current.is_empty() {
                    tokens.push(std::mem::take(&mut current));
                    has_token = false;
                }
            }
            _ if PUNCTUATION_CHARS.contains(c) => {
                if has_token || !current.is_empty() {
                    tokens.push(std::mem::take(&mut current));
                    has_token = false;
                }
                let mut punctuation = String::from(c);
                while let Some(&next) = chars.peek() {
                    if PUNCTUATION_CHARS.contains(next) {
                        punctuation.push(next);
                        chars.next();
                    } else {
                        break;
                    }
                }
                tokens.push(punctuation);
            }
            _ => current.push(c),
        }
    }
    if has_token || !current.is_empty() {
        tokens.push(current);
    }
    Some(tokens)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::trust::{resolve, resolve_capability, Decision};
    use serde_json::{json, Map, Value};

    fn input(value: Value) -> Map<String, Value> {
        value.as_object().expect("test input is an object").clone()
    }

    fn build_capability(capability: CapabilityClass) -> TrustDecision {
        resolve_capability("build", capability)
    }

    // tests/test_kernel_safety.py::test_guarded_boundary_blocks_outside_write_preflight
    #[test]
    fn test_guarded_boundary_blocks_outside_write_preflight() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::with_options(
            &tmp.path().join("project"),
            &[],
            &[],
            WriteBoundary::Guarded,
        );
        let approval = resolve(
            "auto",
            "write_file",
            Some(&input(json!({"path": "../outside.txt"}))),
        );
        assert_eq!(approval.decision, Decision::Allow);
        let safety = resolve_safety(
            approval,
            "write_file \u{b7} ../outside.txt",
            "../outside.txt",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.approval.decision, Decision::Allow);
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::Blocked);
    }

    // tests/test_kernel_safety.py::test_open_boundary_defers_outside_write_to_filesystem_tool
    //
    // Default posture (app-cli parity): no governance pre-flight block for an
    // outside write — the mounted filesystem tool remains the hard enforcement
    // point and returns a graceful tool error instead of a governance denial.
    #[test]
    fn test_open_boundary_defers_outside_write_to_filesystem_tool() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::new(&tmp.path().join("project"));
        let approval = resolve(
            "auto",
            "write_file",
            Some(&input(json!({"path": "../outside.txt"}))),
        );
        let safety = resolve_safety(
            approval,
            "write_file \u{b7} ../outside.txt",
            "../outside.txt",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::WithinPolicy);
        assert!(safety.policy_reason.contains("filesystem tool"));
    }

    // tests/test_kernel_safety.py::test_inside_write_preserves_approval_and_satisfies_path_policy
    #[test]
    fn test_inside_write_preserves_approval_and_satisfies_path_policy() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let project = tmp.path().join("project");
        let policy = DirectoryPolicy::new(&project);
        let approval = resolve(
            "build",
            "write_file",
            Some(&input(json!({"path": "src/app.py"}))),
        );
        let safety = resolve_safety(
            approval,
            "write_file \u{b7} src/app.py",
            "src/app.py",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.approval.decision, Decision::Ask);
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::WithinPolicy);
    }

    // tests/test_kernel_safety.py::test_outside_read_roams_within_denylist
    //
    // Reads are denylist-bounded, not allowlist-bounded — amplifier may read
    // wherever it needs without the outside-project gate.
    #[test]
    fn test_outside_read_roams_within_denylist() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::new(&tmp.path().join("project"));
        let approval = resolve(
            "chat",
            "read_file",
            Some(&input(json!({"path": "/tmp/outside.txt"}))),
        );
        let safety = resolve_safety(
            approval,
            "read_file \u{b7} /tmp/outside.txt",
            "/tmp/outside.txt",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::WithinPolicy);
        assert_eq!(safety.approval.capability, CapabilityClass::Read);
        assert_eq!(safety.approval.decision, Decision::Allow);
    }

    // tests/test_kernel_safety.py::test_denied_directory_read_is_blocked
    //
    // The "within reason" boundary: user-denied directories gate reads too.
    #[test]
    fn test_denied_directory_read_is_blocked() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let secrets = tmp.path().join("secrets");
        let secrets_str = secrets.to_string_lossy().into_owned();
        let policy = DirectoryPolicy::with_options(
            &tmp.path().join("project"),
            &[],
            &[secrets_str.as_str()],
            WriteBoundary::Open,
        );
        let target = secrets.join("k.txt").to_string_lossy().into_owned();
        let approval = resolve("chat", "read_file", Some(&input(json!({"path": target}))));
        let safety = resolve_safety(
            approval,
            &format!("read_file \u{b7} {target}"),
            &target,
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::Blocked);
        assert!(safety.policy_reason.contains("denied"));
    }

    // tests/test_kernel_safety.py::test_read_shaped_shell_outside_project_roams
    //
    // Read-shaped commands may roam outside the project; only write-shaped
    // commands (write heads, redirect targets) inherit the outside-project gate.
    #[test]
    fn test_read_shaped_shell_outside_project_roams() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::new(&tmp.path().join("project"));
        let approval = resolve(
            "auto",
            "bash",
            Some(&input(json!({"command": "ls -la /tmp/elsewhere 2>/dev/null"}))),
        );
        let safety = resolve_safety(
            approval,
            "ls -la /tmp/elsewhere 2>/dev/null",
            "",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::WithinPolicy);
    }

    // tests/test_kernel_safety.py::test_write_shaped_shell_outside_project_is_gated_when_guarded
    #[test]
    fn test_write_shaped_shell_outside_project_is_gated_when_guarded() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::with_options(
            &tmp.path().join("project"),
            &[],
            &[],
            WriteBoundary::Guarded,
        );
        let approval = resolve(
            "auto",
            "bash",
            Some(&input(json!({"command": "rm /tmp/elsewhere/x.txt"}))),
        );
        let safety = resolve_safety(
            approval,
            "rm /tmp/elsewhere/x.txt",
            "",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::OutsidePolicy);
        assert_eq!(safety.approval.capability, CapabilityClass::OutsideProject);
    }

    // tests/test_kernel_safety.py::test_write_shaped_shell_outside_project_roams_when_open
    //
    // Default posture (app-cli parity): bash writes are not path-confined —
    // like amplifier-app-cli's unconfined bash tool.
    #[test]
    fn test_write_shaped_shell_outside_project_roams_when_open() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::new(&tmp.path().join("project"));
        let approval = resolve(
            "auto",
            "bash",
            Some(&input(json!({"command": "rm /tmp/elsewhere/x.txt"}))),
        );
        let safety = resolve_safety(
            approval,
            "rm /tmp/elsewhere/x.txt",
            "",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::WithinPolicy);
    }

    // tests/test_kernel_safety.py::test_protected_shell_target_is_blocked_even_when_exec_is_allowlisted
    #[test]
    fn test_protected_shell_target_is_blocked_even_when_exec_is_allowlisted() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let project = tmp.path().join("project");
        let policy = DirectoryPolicy::new(&project);
        let approval = resolve(
            "auto",
            "bash",
            Some(&input(json!({"command": "echo bad > ./AGENTS.md"}))),
        );
        let safety = resolve_safety(
            approval,
            "echo bad > ./AGENTS.md",
            "",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::Blocked);
        assert!(safety.policy_reason.contains("protected"));
    }

    // tests/test_kernel_safety.py::test_bare_protected_shell_target_is_also_blocked
    #[test]
    fn test_bare_protected_shell_target_is_also_blocked() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::new(&tmp.path().join("project"));
        let approval = resolve(
            "auto",
            "bash",
            Some(&input(json!({"command": "git config -f .git/config x y"}))),
        );
        let safety = resolve_safety(
            approval,
            "git config -f .git/config x y",
            "",
            Some(&policy),
            build_capability,
        );
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::Blocked);
    }

    // tests/test_kernel_safety.py::test_embedded_protected_shell_target_escalates_to_ask
    //
    // Audit H1 fail-closed: a protected path buried inside `python3 -c`
    // escapes the command-list token pass, so governance escalates it to
    // *ask* rather than silently allowing it — even in the default open
    // posture.
    #[test]
    fn test_embedded_protected_shell_target_escalates_to_ask() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::new(&tmp.path().join("project")); // open posture (default)
        let command = "python3 -c \"open('.git/config','w').write('x')\"";
        let approval = resolve("build", "bash", Some(&input(json!({"command": command}))));
        let safety = resolve_safety(approval, command, "", Some(&policy), build_capability);
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::OutsidePolicy);
        assert_eq!(safety.approval.decision, Decision::Ask);
        assert_eq!(safety.approval.capability, CapabilityClass::OutsideProject);
        assert_eq!(safety.target, ".git");
    }

    // tests/test_kernel_safety.py::test_embedded_protected_shell_target_in_auto_is_classifier_gated
    //
    // In auto mode the escalated ask is classifier-gated — the reasoning-blind
    // classifier adjudicates and denies-and-continue on refusal.
    #[test]
    fn test_embedded_protected_shell_target_in_auto_is_classifier_gated() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::new(&tmp.path().join("project"));
        let auto_capability =
            |capability: CapabilityClass| resolve_capability("auto", capability);
        let command = "sed -i 's/a/b/' vendored/.git/config";
        let approval = resolve("auto", "bash", Some(&input(json!({"command": command}))));
        let safety = resolve_safety(approval, command, "", Some(&policy), auto_capability);
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::OutsidePolicy);
        assert!(safety.approval.classifier_gated);
    }

    // Not a pinned Python test: edge-case parity for the hand-rolled shlex
    // subset and protected-reference matcher, oracle-checked against the real
    // Python `DirectoryPolicy.shell_outside_target` on 2026-07-26.
    #[test]
    fn oracle_parity_shell_outside_target_edge_cases() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let open = DirectoryPolicy::new(&tmp.path().join("project"));
        // .gitignore/.github/foo.git never match .git; a glob filter in a
        // read-shaped command is exempt.
        for command in [
            "cat .gitignore",
            "ls .github/workflows",
            "git clone foo.git",
            "find . -not -path './.git/*'",
            "cat AGENTS.mdx",
            "touch ~/x.txt",
            "ls; rm /tmp/foo.txt",
            "ls && rm /tmp/foo.txt",
        ] {
            assert_eq!(open.shell_outside_target(command), None, "{command}");
        }
        // Write-shaped globs and redirects onto protected paths still stop.
        let (target, reason) = open.shell_outside_target("rm -rf .git/*").expect("flagged");
        assert_eq!(target, ".git/*");
        assert!(reason.starts_with("path is protected by default"));
        let (target, _) = open.shell_outside_target("echo hi > AGENTS.md").expect("flagged");
        assert_eq!(target, "AGENTS.md");
        // Unbalanced quote falls back to whitespace split and still catches
        // the redirect target.
        let (target, _) = open
            .shell_outside_target("echo unbalanced \"quote > .git/config")
            .expect("flagged");
        assert_eq!(target, ".git/config");
        let guarded = DirectoryPolicy::with_options(
            &tmp.path().join("project"),
            &[],
            &[],
            WriteBoundary::Guarded,
        );
        // Python strips ';' from tokens before the separator check, so a
        // ';'-joined write head is NOT detected — faithful quirk.
        assert_eq!(guarded.shell_outside_target("ls; rm /tmp/foo.txt"), None);
        let (target, reason) = guarded
            .shell_outside_target("ls && rm /tmp/foo.txt")
            .expect("flagged");
        assert_eq!(target, "/tmp/foo.txt");
        assert!(reason.starts_with("path is outside allowed write directories"));
        let (target, _) = guarded.shell_outside_target("touch ~/x.txt").expect("flagged");
        assert_eq!(target, "~/x.txt");
    }

    // tests/test_kernel_safety.py::test_embedded_outside_write_still_roams_when_open
    //
    // Documented residual: a merely-outside write via python3 -c is not
    // path-confined in the default open posture (the filesystem tool cannot
    // see interpreter code). Only protected paths fail closed here.
    #[test]
    fn test_embedded_outside_write_still_roams_when_open() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let policy = DirectoryPolicy::new(&tmp.path().join("project"));
        let command = "python3 -c \"open('/tmp/outside.txt','w')\"";
        let approval = resolve("auto", "bash", Some(&input(json!({"command": command}))));
        let safety = resolve_safety(approval, command, "", Some(&policy), build_capability);
        assert_eq!(safety.execution_policy, ExecutionPolicyDecision::WithinPolicy);
    }
}
