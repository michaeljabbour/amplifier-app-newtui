//! `/doctor` — named setup checks with OK / finding rows (DESIGN-SPEC §6).
//!
//! Pattern ported from amplifier-app-opencode's `doctor` subcommand
//! (RESEARCH-BRIEF §5): a list of named checks, each returning an OK or a
//! finding; CI-friendly exit codes when run standalone. Mockup output:
//!
//! ```text
//! · Doctor  3 findings · nothing changed yet
//!   ✔ install healthy · PATH clean · settings parse
//!   1 2 MCP servers unused in 30 days · cost 4.1k tok/session
//!   2 14 identical read-only approvals this week · candidate allowlist
//! ```
//!
//! Healthy checks collapse into ONE green `✔` line (messages joined with
//! ` · `); each failing check becomes a numbered orange finding. /doctor
//! reports only — fixes happen on explicit confirm, elsewhere.
//!
//! Runnable standalone: [`run_standalone`] prints a plain-text report
//! and returns an exit code (0 = no findings, 1 = findings) so the
//! integrator can wire `amplifier-newtui doctor` straight to it.
//!
//! Python's `check_install` probes `importlib.metadata`, which has no
//! Rust equivalent — the install probe is injected instead (mirroring how
//! the anchors status is computed by the caller and injected in Python).

use std::path::{Path, PathBuf};

use crate::commands::improve::ApprovalTally;
use crate::model::blocks::{DoctorBlock, DoctorFinding};
use crate::model::formatting::format_tokens_compact;

pub const PACKAGE_NAME: &str = "amplifier-app-newtui";
pub const EXECUTABLE_NAME: &str = "amplifier-newtui";

pub const UNUSED_MCP_THRESHOLD_DAYS: f64 = 30.0;
/// Identical read-only approvals this session/week before /doctor flags
/// an allowlist candidate.
pub const REPEATED_APPROVAL_THRESHOLD: u64 = 10;

/// `~/.amplifier/settings.yaml` + `~/.amplifier/settings.json`
/// (Python: `DEFAULT_SETTINGS_PATHS`).
pub fn default_settings_paths() -> Vec<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
    vec![
        home.join(".amplifier").join("settings.yaml"),
        home.join(".amplifier").join("settings.json"),
    ]
}

/// One named check outcome: OK (joins the ✔ line) or a finding.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CheckResult {
    pub name: String,
    pub ok: bool,
    pub message: String,
}

impl CheckResult {
    fn new(name: &str, ok: bool, message: impl Into<String>) -> Self {
        Self {
            name: name.to_string(),
            ok,
            message: message.into(),
        }
    }
}

/// Usage stats for one configured MCP server (input to the unused check).
///
/// `last_used_days_ago` is `None` when the server has never been used;
/// `tokens_per_session` is its schema/handshake overhead cost. (Pydantic's
/// `ge=0` bounds are carried by the unsigned / non-negative field types.)
#[derive(Clone, Debug, PartialEq)]
pub struct McpServerStats {
    pub name: String,
    pub last_used_days_ago: Option<f64>,
    pub tokens_per_session: u64,
}

impl McpServerStats {
    pub fn unused_for(&self, days: f64) -> bool {
        self.last_used_days_ago.is_none_or(|ago| ago >= days)
    }
}

/// All check outcomes, split into the ✔ summary and numbered findings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DoctorReport {
    pub checks: Vec<CheckResult>,
}

impl DoctorReport {
    /// The single green line: OK messages joined with ` · `.
    pub fn healthy_summary(&self) -> String {
        self.checks
            .iter()
            .filter(|check| check.ok)
            .map(|check| check.message.as_str())
            .collect::<Vec<_>>()
            .join(" · ")
    }

    /// Failing checks as numbered orange findings, in check order.
    pub fn findings(&self) -> Vec<DoctorFinding> {
        self.checks
            .iter()
            .filter(|check| !check.ok)
            .enumerate()
            .map(|(index, check)| DoctorFinding::new(index as u32 + 1, check.message.clone()))
            .collect()
    }

    pub fn finding_count(&self) -> usize {
        self.checks.iter().filter(|check| !check.ok).count()
    }

    /// `3 findings · nothing changed yet` (mockup header suffix).
    pub fn headline(&self) -> String {
        let count = self.finding_count();
        let noun = if count == 1 { "finding" } else { "findings" };
        format!("{count} {noun} · nothing changed yet")
    }
}

// --- named checks ------------------------------------------------------

/// The package resolves to an installed distribution.
///
/// `probe_installed` stands in for Python's `importlib.metadata.version`
/// (returns `false` where Python raises `PackageNotFoundError`).
pub fn check_install(package: &str, probe_installed: &dyn Fn(&str) -> bool) -> CheckResult {
    if !probe_installed(package) {
        return CheckResult::new(
            "install",
            false,
            format!("install broken · {package} not found"),
        );
    }
    CheckResult::new("install", true, "install healthy")
}

/// `shutil.which` equivalent: resolve `executable` against `PATH`
/// (or directly, when it already names a path).
fn which(executable: &str) -> bool {
    fn is_executable_file(path: &Path) -> bool {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            path.metadata()
                .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
                .unwrap_or(false)
        }
        #[cfg(not(unix))]
        {
            path.is_file()
        }
    }

    if executable.contains(std::path::MAIN_SEPARATOR) {
        return is_executable_file(Path::new(executable));
    }
    let Some(path_var) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&path_var).any(|dir| is_executable_file(&dir.join(executable)))
}

/// The console script is reachable on PATH.
pub fn check_path(executable: &str) -> CheckResult {
    if !which(executable) {
        return CheckResult::new("path", false, format!("{executable} not on PATH"));
    }
    CheckResult::new("path", true, "PATH clean")
}

/// Every existing settings file parses (YAML or JSON).
///
/// No settings file at all is healthy — defaults apply.
pub fn check_settings(paths: &[PathBuf]) -> CheckResult {
    for path in paths {
        if !path.exists() {
            continue;
        }
        let parsed: Result<(), String> = std::fs::read_to_string(path)
            .map_err(|err| err.to_string())
            .and_then(|text| {
                if path.extension().and_then(|ext| ext.to_str()) == Some("json") {
                    serde_json::from_str::<serde_json::Value>(&text)
                        .map(|_| ())
                        .map_err(|err| err.to_string())
                } else {
                    serde_yaml::from_str::<serde_yaml::Value>(&text)
                        .map(|_| ())
                        .map_err(|err| err.to_string())
                }
            });
        if let Err(exc) = parsed {
            let name = path
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default();
            return CheckResult::new(
                "settings",
                false,
                format!("settings parse failed · {name}: {exc}"),
            );
        }
    }
    CheckResult::new("settings", true, "settings parse")
}

/// Configured MCP servers nobody has used lately still cost tokens.
pub fn check_unused_mcp(stats: &[McpServerStats], threshold_days: f64) -> CheckResult {
    let unused: Vec<&McpServerStats> = stats
        .iter()
        .filter(|server| server.unused_for(threshold_days))
        .collect();
    if unused.is_empty() {
        return CheckResult::new("mcp", true, "MCP servers in use");
    }
    let cost: u64 = unused.iter().map(|server| server.tokens_per_session).sum();
    let count = unused.len();
    let noun = if count == 1 { "server" } else { "servers" };
    let days = threshold_days.round() as i64;
    CheckResult::new(
        "mcp",
        false,
        format!(
            "{count} MCP {noun} unused in {days} days · cost {} tok/session",
            format_tokens_compact(cost)
        ),
    )
}

/// Repeated identical read-only approvals are an allowlist candidate.
pub fn check_repeated_approvals(tallies: &[ApprovalTally], threshold: u64) -> CheckResult {
    let repeated: u64 = tallies
        .iter()
        .filter(|tally| tally.capability == "read" && tally.always_approved())
        .map(|tally| tally.asked)
        .sum();
    if repeated < threshold {
        return CheckResult::new("approvals", true, "no repeated approvals");
    }
    CheckResult::new(
        "approvals",
        false,
        format!("{repeated} identical read-only approvals this week · candidate allowlist"),
    )
}

/// Structural shape of `kernel.updater.AnchorsStatus` the check reads.
///
/// Kept as a trait so `commands/` never depends on `kernel/` (ADR-0007
/// layering); the CLI computes the status and injects it here.
pub trait AnchorsPinStatus {
    fn is_stale(&self) -> bool;
    fn error(&self) -> Option<&str>;
    fn describe(&self) -> String;
}

/// The composed anchors bundle is not behind its upstream ref.
///
/// Anchors is included (not a direct source), so `update`'s per-bundle
/// check skips it — this surfaces its freshness instead of leaving it silent.
/// Green when current, when offline (`error` set — never a false finding),
/// or when no status was supplied. A confirmed-behind cache is the finding.
pub fn check_anchors_pin(status: Option<&dyn AnchorsPinStatus>) -> CheckResult {
    let Some(status) = status else {
        return CheckResult::new("anchors", true, "anchors ref check skipped");
    };
    if status.error().is_some() {
        return CheckResult::new("anchors", true, status.describe());
    }
    if status.is_stale() {
        return CheckResult::new("anchors", false, status.describe());
    }
    CheckResult::new("anchors", true, status.describe())
}

/// Inputs to the full named-check suite (Python `run_checks` keyword args).
///
/// `probe_installed` carries the injected install-probe result source (see
/// [`check_install`]); everything else mirrors the Python defaults-by-caller.
pub struct DoctorInputs<'a> {
    pub mcp_stats: &'a [McpServerStats],
    pub approval_tallies: &'a [ApprovalTally],
    pub settings_paths: &'a [PathBuf],
    pub package: &'a str,
    pub executable: &'a str,
    pub anchors_status: Option<&'a dyn AnchorsPinStatus>,
    pub probe_installed: &'a dyn Fn(&str) -> bool,
}

/// Run the full named-check suite and return the report.
pub fn run_checks(inputs: &DoctorInputs) -> DoctorReport {
    DoctorReport {
        checks: vec![
            check_install(inputs.package, inputs.probe_installed),
            check_path(inputs.executable),
            check_settings(inputs.settings_paths),
            check_unused_mcp(inputs.mcp_stats, UNUSED_MCP_THRESHOLD_DAYS),
            check_repeated_approvals(inputs.approval_tallies, REPEATED_APPROVAL_THRESHOLD),
            check_anchors_pin(inputs.anchors_status),
        ],
    }
}

/// Assemble the `/doctor` transcript block: the `Doctor  <headline>`
/// header, one joined ✔ healthy line, plus the numbered findings.
pub fn build_doctor_block(block_id: &str, report: &DoctorReport) -> DoctorBlock {
    let summary = report.healthy_summary();
    let healthy = if summary.is_empty() {
        Vec::new()
    } else {
        vec![summary]
    };
    DoctorBlock {
        id: block_id.to_string(),
        headline: report.headline(),
        healthy,
        findings: report.findings(),
    }
}

// --- standalone CLI surface ---------------------------------------------

/// Plain-text report for the `amplifier-newtui doctor` subcommand.
pub fn render_text(report: &DoctorReport) -> String {
    let mut lines = vec![
        format!("{EXECUTABLE_NAME} doctor"),
        String::new(),
        format!("Doctor  {}", report.headline()),
    ];
    let summary = report.healthy_summary();
    if !summary.is_empty() {
        lines.push(format!("  ✔ {summary}"));
    }
    for finding in report.findings() {
        lines.push(format!("  {} {}", finding.number, finding.text));
    }
    lines.join("\n")
}

/// Run checks, print the plain report, return the CI exit code.
///
/// 0 = no findings; 1 = findings present (opencode doctor convention).
pub fn run_standalone(inputs: &DoctorInputs, echo: &mut dyn FnMut(&str)) -> i32 {
    let report = run_checks(inputs);
    echo(&render_text(&report));
    if report.finding_count() == 0 {
        0
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::blocks::TranscriptBlock;

    fn ok(name: &str, message: &str) -> CheckResult {
        CheckResult::new(name, true, message)
    }

    fn finding(name: &str, message: &str) -> CheckResult {
        CheckResult::new(name, false, message)
    }

    /// Stand-in for Python's real `importlib.metadata` probe: the package
    /// under its real distribution name resolves; anything else does not.
    fn fake_install_probe(package: &str) -> bool {
        package == "amplifier-app-newtui"
    }

    /// Test mirror of `kernel.updater.AnchorsStatus` (not yet ported to the
    /// Rust kernel) — fields and `describe()` strings match the Python
    /// dataclass exactly so the pinned assertions stay meaningful.
    #[derive(Default)]
    struct AnchorsStatus {
        r#ref: Option<String>,
        has_update: Option<bool>,
        cached_commit: Option<String>,
        remote_commit: Option<String>,
        error: Option<String>,
    }

    fn is_sha(r#ref: &str) -> bool {
        r#ref.len() == 40
            && r#ref
                .to_lowercase()
                .chars()
                .all(|c| "0123456789abcdef".contains(c))
    }

    fn first8(text: &str) -> &str {
        &text[..text.len().min(8)]
    }

    impl AnchorsPinStatus for AnchorsStatus {
        fn is_stale(&self) -> bool {
            self.has_update == Some(true)
        }

        fn error(&self) -> Option<&str> {
            self.error.as_deref()
        }

        fn describe(&self) -> String {
            let Some(r#ref) = self.r#ref.as_deref() else {
                return "anchors include not found in bundle".to_string();
            };
            if let Some(error) = &self.error {
                return format!("anchors ref check unavailable · tracking @{ref} ({error})");
            }
            if is_sha(r#ref) {
                return format!(
                    "anchors pinned to {} · no auto-updates (bump via update tooling)",
                    first8(r#ref)
                );
            }
            match self.has_update {
                Some(true) => {
                    let cached = first8(self.cached_commit.as_deref().unwrap_or("unknown"));
                    let remote = first8(self.remote_commit.as_deref().unwrap_or("unknown"));
                    format!(
                        "anchors (@{ref}) is behind upstream · {cached} → {remote} · \
                         run `amplifier-newtui update`"
                    )
                }
                Some(false) => {
                    let cached = first8(
                        self.cached_commit
                            .as_deref()
                            .or(self.remote_commit.as_deref())
                            .unwrap_or(""),
                    );
                    let suffix = if cached.is_empty() {
                        String::new()
                    } else {
                        format!(" ({cached})")
                    };
                    format!("anchors up to date · tracking @{ref}{suffix}")
                }
                None => format!("anchors ref check unavailable · tracking @{ref}"),
            }
        }
    }

    fn stats(name: &str, last_used_days_ago: Option<f64>, tokens_per_session: u64) -> McpServerStats {
        McpServerStats {
            name: name.to_string(),
            last_used_days_ago,
            tokens_per_session,
        }
    }

    fn tally(action: &str, approved: u64, asked: u64, capability: &str) -> ApprovalTally {
        ApprovalTally {
            action: action.to_string(),
            approved,
            asked,
            capability: capability.to_string(),
        }
    }

    /// Pins `test_check_install_healthy_and_broken`.
    #[test]
    fn test_check_install_healthy_and_broken() {
        assert!(check_install("amplifier-app-newtui", &fake_install_probe).ok);
        assert_eq!(
            check_install("amplifier-app-newtui", &fake_install_probe).message,
            "install healthy"
        );
        let broken = check_install("definitely-not-a-package-xyz", &fake_install_probe);
        assert!(!broken.ok);
        assert!(broken.message.contains("not found"));
    }

    /// Pins `test_check_path`.
    #[test]
    fn test_check_path() {
        assert!(check_path("python3").ok);
        assert_eq!(check_path("python3").message, "PATH clean");
        let missing = check_path("no-such-binary-xyz");
        assert!(!missing.ok);
        assert_eq!(missing.message, "no-such-binary-xyz not on PATH");
    }

    /// Pins `test_check_settings_parses_yaml_and_json`.
    #[test]
    fn test_check_settings_parses_yaml_and_json() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let good_yaml = tmp.path().join("settings.yaml");
        std::fs::write(&good_yaml, "theme: slate\n").expect("write yaml");
        let good_json = tmp.path().join("settings.json");
        std::fs::write(&good_json, "{\"theme\": \"slate\"}").expect("write json");
        let paths = vec![good_yaml, good_json];
        assert!(check_settings(&paths).ok);
        assert_eq!(check_settings(&paths).message, "settings parse");
    }

    /// Pins `test_check_settings_missing_file_is_healthy`.
    #[test]
    fn test_check_settings_missing_file_is_healthy() {
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(check_settings(&[tmp.path().join("nope.yaml")]).ok);
    }

    /// Pins `test_check_settings_flags_broken_file`.
    #[test]
    fn test_check_settings_flags_broken_file() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let bad = tmp.path().join("settings.json");
        std::fs::write(&bad, "{not json").expect("write bad json");
        let result = check_settings(&[bad]);
        assert!(!result.ok);
        assert!(result.message.contains("settings parse failed"));
    }

    /// Pins `test_check_unused_mcp_finding_matches_mockup_shape`.
    #[test]
    fn test_check_unused_mcp_finding_matches_mockup_shape() {
        let servers = vec![
            stats("alpha", Some(45.0), 2_100),
            stats("beta", None, 2_000),
            stats("live", Some(2.0), 900),
        ];
        let result = check_unused_mcp(&servers, UNUSED_MCP_THRESHOLD_DAYS);
        assert!(!result.ok);
        assert_eq!(
            result.message,
            "2 MCP servers unused in 30 days · cost 4.1k tok/session"
        );
    }

    /// Pins `test_check_unused_mcp_all_in_use`.
    #[test]
    fn test_check_unused_mcp_all_in_use() {
        let servers = vec![stats("live", Some(1.0), 0)];
        assert!(check_unused_mcp(&servers, UNUSED_MCP_THRESHOLD_DAYS).ok);
    }

    /// Pins `test_check_repeated_approvals`.
    #[test]
    fn test_check_repeated_approvals() {
        let tallies = vec![
            tally("read docs/", 14, 14, "read"),
            tally("rm -rf /", 0, 3, "exec"),
        ];
        let result = check_repeated_approvals(&tallies, REPEATED_APPROVAL_THRESHOLD);
        assert!(!result.ok);
        assert_eq!(
            result.message,
            "14 identical read-only approvals this week · candidate allowlist"
        );
        // Below threshold, or not read-only, or not always approved → healthy.
        assert!(
            check_repeated_approvals(&[tally("read x", 2, 2, "read")], REPEATED_APPROVAL_THRESHOLD)
                .ok
        );
        assert!(check_repeated_approvals(
            &[tally("write x", 20, 20, "write")],
            REPEATED_APPROVAL_THRESHOLD
        )
        .ok);
        assert!(check_repeated_approvals(
            &[tally("read x", 11, 12, "read")],
            REPEATED_APPROVAL_THRESHOLD
        )
        .ok);
    }

    /// Pins `test_check_anchors_pin_stale_is_finding`.
    #[test]
    fn test_check_anchors_pin_stale_is_finding() {
        let status = AnchorsStatus {
            r#ref: Some("main".to_string()),
            has_update: Some(true),
            cached_commit: Some("aaaa1111".to_string()),
            remote_commit: Some("bbbb2222".to_string()),
            ..Default::default()
        };
        let result = check_anchors_pin(Some(&status));
        assert!(!result.ok);
        assert!(result.message.contains("behind upstream"));
    }

    /// Pins `test_check_anchors_pin_current_is_ok`.
    #[test]
    fn test_check_anchors_pin_current_is_ok() {
        let status = AnchorsStatus {
            r#ref: Some("main".to_string()),
            has_update: Some(false),
            cached_commit: Some("cccc3333".to_string()),
            ..Default::default()
        };
        let result = check_anchors_pin(Some(&status));
        assert!(result.ok);
        assert!(result.message.contains("up to date"));
    }

    /// Pins `test_check_anchors_pin_offline_is_ok_no_false_finding`.
    #[test]
    fn test_check_anchors_pin_offline_is_ok_no_false_finding() {
        let status = AnchorsStatus {
            r#ref: Some("main".to_string()),
            error: Some("network down".to_string()),
            ..Default::default()
        };
        let result = check_anchors_pin(Some(&status));
        assert!(result.ok); // offline never fabricates a finding
    }

    /// Pins `test_check_anchors_pin_none_is_skipped_ok`.
    #[test]
    fn test_check_anchors_pin_none_is_skipped_ok() {
        let result = check_anchors_pin(None);
        assert!(result.ok);
        assert!(result.message.contains("skipped"));
    }

    /// Pins `test_run_checks_includes_stale_anchors_finding`.
    #[test]
    fn test_run_checks_includes_stale_anchors_finding() {
        let stale = AnchorsStatus {
            r#ref: Some("main".to_string()),
            has_update: Some(true),
            cached_commit: Some("a1".to_string()),
            remote_commit: Some("b2".to_string()),
            ..Default::default()
        };
        let report = run_checks(&DoctorInputs {
            mcp_stats: &[],
            approval_tallies: &[],
            settings_paths: &[],
            package: "amplifier-app-newtui",
            executable: "python3",
            anchors_status: Some(&stale),
            probe_installed: &fake_install_probe,
        });
        assert!(report.findings().iter().any(|f| f.text.contains("anchors")));
    }

    /// Pins `test_report_headline_and_healthy_join`.
    #[test]
    fn test_report_headline_and_healthy_join() {
        let report = DoctorReport {
            checks: vec![
                ok("install", "install healthy"),
                ok("path", "PATH clean"),
                ok("settings", "settings parse"),
                finding("mcp", "2 MCP servers unused in 30 days · cost 4.1k tok/session"),
                finding(
                    "approvals",
                    "14 identical read-only approvals this week · candidate allowlist",
                ),
            ],
        };
        assert_eq!(report.headline(), "2 findings · nothing changed yet");
        assert_eq!(
            report.healthy_summary(),
            "install healthy · PATH clean · settings parse"
        );
        let numbers: Vec<u32> = report.findings().iter().map(|f| f.number).collect();
        assert_eq!(numbers, vec![1, 2]);
    }

    /// Pins `test_single_finding_headline_singular`.
    #[test]
    fn test_single_finding_headline_singular() {
        let report = DoctorReport {
            checks: vec![finding("mcp", "x")],
        };
        assert_eq!(report.headline(), "1 finding · nothing changed yet");
    }

    /// Pins `test_build_doctor_block`.
    #[test]
    fn test_build_doctor_block() {
        let report = DoctorReport {
            checks: vec![ok("install", "install healthy"), finding("mcp", "unused")],
        };
        let block = build_doctor_block("b3", &report);
        assert_eq!(TranscriptBlock::from(block.clone()).kind(), "doctor");
        assert_eq!(block.headline, "1 finding · nothing changed yet");
        assert_eq!(block.healthy, vec!["install healthy".to_string()]);
        assert_eq!(block.findings[0].number, 1);
        assert_eq!(block.findings[0].text, "unused");
    }

    /// Pins `test_run_checks_end_to_end`.
    #[test]
    fn test_run_checks_end_to_end() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let report = run_checks(&DoctorInputs {
            mcp_stats: &[],
            approval_tallies: &[],
            settings_paths: &[tmp.path().join("settings.yaml")],
            package: "amplifier-app-newtui",
            executable: "python3",
            anchors_status: None,
            probe_installed: &fake_install_probe,
        });
        assert_eq!(report.finding_count(), 0);
        assert!(report.healthy_summary().contains("install healthy"));
        assert!(report.healthy_summary().contains("PATH clean"));
        assert!(report.healthy_summary().contains("settings parse"));
    }

    /// Pins `test_render_text_matches_mockup_row_shapes`.
    #[test]
    fn test_render_text_matches_mockup_row_shapes() {
        let report = DoctorReport {
            checks: vec![
                ok("install", "install healthy"),
                ok("path", "PATH clean"),
                ok("settings", "settings parse"),
                finding("mcp", "2 MCP servers unused in 30 days · cost 4.1k tok/session"),
            ],
        };
        let text = render_text(&report);
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines[0], "amplifier-newtui doctor");
        assert!(lines.contains(&"Doctor  1 finding · nothing changed yet"));
        assert!(lines.contains(&"  ✔ install healthy · PATH clean · settings parse"));
        assert!(lines.contains(&"  1 2 MCP servers unused in 30 days · cost 4.1k tok/session"));
    }

    /// Pins `test_run_standalone_exit_codes`.
    #[test]
    fn test_run_standalone_exit_codes() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let settings_paths = vec![tmp.path().join("settings.yaml")];
        let dead = vec![stats("dead", None, 500)];
        let mut printed: Vec<String> = Vec::new();
        let code = run_standalone(
            &DoctorInputs {
                mcp_stats: &dead,
                approval_tallies: &[],
                settings_paths: &settings_paths,
                package: "amplifier-app-newtui",
                executable: "python3",
                anchors_status: None,
                probe_installed: &fake_install_probe,
            },
            &mut |text| printed.push(text.to_string()),
        );
        assert_eq!(code, 1);
        assert!(printed[0].contains("amplifier-newtui doctor"));
        assert!(printed[0].contains("✔"));

        printed.clear();
        let code = run_standalone(
            &DoctorInputs {
                mcp_stats: &[],
                approval_tallies: &[],
                settings_paths: &settings_paths,
                package: "amplifier-app-newtui",
                executable: "python3",
                anchors_status: None,
                probe_installed: &fake_install_probe,
            },
            &mut |text| printed.push(text.to_string()),
        );
        assert_eq!(code, 0);
        assert!(printed[0].contains("0 findings · nothing changed yet"));
    }
}
