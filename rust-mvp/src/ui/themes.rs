//! The three spec themes (DESIGN-SPEC §1) — port of `ui/themes.py`.
//!
//! This is the ONLY module in the crate allowed to contain hex color
//! values. Every theme exposes ALL fourteen spec tokens as theme
//! *variables* named exactly after the spec tokens (`bg-page`,
//! `bg-term`, …), so widgets/styles reference tokens by name and a
//! runtime theme switch is a repaint, not a rebuild (ADR-0007
//! resolution 11).
//!
//! Textual mapped these tokens onto its semantic slots and TCSS
//! variables; here the same table rides on a plain [`Theme`] struct
//! with the identical slot names, plus a hex → [`ratatui::style::Color`]
//! bridge so the render layer never touches hex literals directly.
//!
//! Default theme: `slate`.

use ratatui::style::Color;

/// Every DESIGN-SPEC §1 token, in spec-table order.
pub const TOKEN_NAMES: [&str; 14] = [
    "bg-page", "bg-term", "bg-chrome", "bg-tab", "fg", "bright", "dim", "dimmer", "green",
    "orange", "red", "blue", "teal", "rule",
];

/// Theme name → (token name → exact spec hex), in spec order.
///
/// Exact hex values from the DESIGN-SPEC §1 table — do not adjust.
pub const THEME_TOKENS: [(&str, [(&str, &str); 14]); 3] = [
    (
        "slate",
        [
            ("bg-page", "#12151c"),
            ("bg-term", "#232937"),
            ("bg-chrome", "#191d27"),
            ("bg-tab", "#2b3243"),
            ("fg", "#c9d1e0"),
            ("bright", "#eef2f8"),
            ("dim", "#6b7487"),
            ("dimmer", "#4a5163"),
            ("green", "#7ec699"),
            ("orange", "#e0a458"),
            ("red", "#e06c75"),
            ("blue", "#7aa2f7"),
            ("teal", "#6fc3c3"),
            ("rule", "#333b4d"),
        ],
    ),
    (
        "graphite",
        [
            ("bg-page", "#131110"),
            ("bg-term", "#211e1a"),
            ("bg-chrome", "#181512"),
            ("bg-tab", "#2c2722"),
            ("fg", "#d6cfc4"),
            ("bright", "#f2ede4"),
            ("dim", "#8a8175"),
            ("dimmer", "#575047"),
            ("green", "#98c28b"),
            ("orange", "#dba15c"),
            ("red", "#d97371"),
            ("blue", "#90a4d8"),
            ("teal", "#80bcae"),
            ("rule", "#3a352e"),
        ],
    ),
    (
        "carbon",
        [
            ("bg-page", "#0c0e12"),
            ("bg-term", "#14171d"),
            ("bg-chrome", "#0f1116"),
            ("bg-tab", "#1f242e"),
            ("fg", "#cdd6e4"),
            ("bright", "#f4f7fc"),
            ("dim", "#65718a"),
            ("dimmer", "#3d4657"),
            ("green", "#6fd39c"),
            ("orange", "#e9b14f"),
            ("red", "#ef6e7b"),
            ("blue", "#6f9df2"),
            ("teal", "#57c8c8"),
            ("rule", "#2a3140"),
        ],
    ),
];

/// Title bar text color — hardcoded in the mockup's window chrome
/// (design-v3-cohesive.html line 39, `color: #aeb6c6; font-weight: 600`)
/// for every theme; deliberately NOT part of the §1 token table.
pub const TITLE_FG: &str = "#aeb6c6";

/// Mockup-mandated colors outside the §1 token table, exposed as theme
/// variables (`title-fg`) so hex still lives only in this module.
pub const EXTRA_VARIABLES: [(&str, &str); 1] = [("title-fg", TITLE_FG)];

pub const DEFAULT_THEME: &str = "slate";
pub const THEME_NAME_PREFIX: &str = "amplifier-";

/// Registered theme name for a spec theme (`amplifier-slate`).
pub fn theme_id(name: &str) -> String {
    format!("{THEME_NAME_PREFIX}{name}")
}

/// One spec theme, semantic slots + full token table.
///
/// Mirrors `textual.theme.Theme`: the semantic slots map onto spec
/// tokens (background/surface/panel/foreground etc.) so shared widgets
/// look right, and the full token table rides in `variables` so the
/// render layer uses `bg-page` … `rule` directly — the token names ARE
/// the variable names.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Theme {
    /// Registered id, e.g. `amplifier-slate` (see [`theme_id`]).
    pub name: String,
    pub primary: &'static str,
    pub secondary: &'static str,
    pub background: &'static str,
    pub surface: &'static str,
    pub panel: &'static str,
    pub foreground: &'static str,
    pub success: &'static str,
    pub warning: &'static str,
    pub error: &'static str,
    pub accent: &'static str,
    pub dark: bool,
    /// All fourteen §1 tokens plus [`EXTRA_VARIABLES`], in table order.
    pub variables: Vec<(&'static str, &'static str)>,
}

impl Theme {
    /// Hex value of a theme variable (`bg-page` … `rule`, `title-fg`).
    pub fn variable(&self, name: &str) -> Option<&'static str> {
        self.variables
            .iter()
            .find(|(token, _)| *token == name)
            .map(|(_, hex)| *hex)
    }

    /// A theme variable as a ratatui [`Color`].
    pub fn color(&self, name: &str) -> Option<Color> {
        self.variable(name).and_then(hex_color)
    }
}

/// Parse `#rrggbb` into a ratatui [`Color::Rgb`].
pub fn hex_color(hex: &str) -> Option<Color> {
    let digits = hex.strip_prefix('#')?;
    if digits.len() != 6 || !digits.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let r = u8::from_str_radix(&digits[0..2], 16).ok()?;
    let g = u8::from_str_radix(&digits[2..4], 16).ok()?;
    let b = u8::from_str_radix(&digits[4..6], 16).ok()?;
    Some(Color::Rgb(r, g, b))
}

/// Token table for one spec theme name (`slate`/`graphite`/`carbon`).
pub fn theme_tokens(name: &str) -> Option<&'static [(&'static str, &'static str); 14]> {
    THEME_TOKENS
        .iter()
        .find(|(theme, _)| *theme == name)
        .map(|(_, tokens)| tokens)
}

/// Exact spec hex for one token of one theme.
pub fn token_hex(theme: &str, token: &str) -> Option<&'static str> {
    theme_tokens(theme)?
        .iter()
        .find(|(name, _)| *name == token)
        .map(|(_, hex)| *hex)
}

/// Assemble one spec theme from its token table.
fn build_theme(name: &str, tokens: &'static [(&'static str, &'static str); 14]) -> Theme {
    let get = |token: &str| -> &'static str {
        tokens
            .iter()
            .find(|(t, _)| *t == token)
            .map(|(_, hex)| *hex)
            .expect("spec token present")
    };
    let mut variables: Vec<(&'static str, &'static str)> = tokens.to_vec();
    variables.extend(EXTRA_VARIABLES);
    Theme {
        name: theme_id(name),
        primary: get("blue"),
        secondary: get("teal"),
        background: get("bg-term"),
        surface: get("bg-chrome"),
        panel: get("bg-tab"),
        foreground: get("fg"),
        success: get("green"),
        warning: get("orange"),
        error: get("red"),
        accent: get("orange"),
        dark: true,
        variables,
    }
}

/// Spec theme name (`slate`/`graphite`/`carbon`) → [`Theme`], spec order.
pub fn themes() -> Vec<(&'static str, Theme)> {
    THEME_TOKENS
        .iter()
        .map(|(name, tokens)| (*name, build_theme(name, tokens)))
        .collect()
}

/// One spec theme by name.
pub fn theme(name: &str) -> Option<Theme> {
    THEME_TOKENS
        .iter()
        .find(|(theme, _)| *theme == name)
        .map(|(name, tokens)| build_theme(name, tokens))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    /// The DESIGN-SPEC §1 table, verbatim (mirrors the Python test's own copy).
    fn spec_table() -> Vec<(&'static str, Vec<(&'static str, &'static str)>)> {
        vec![
            (
                "slate",
                vec![
                    ("bg-page", "#12151c"),
                    ("bg-term", "#232937"),
                    ("bg-chrome", "#191d27"),
                    ("bg-tab", "#2b3243"),
                    ("fg", "#c9d1e0"),
                    ("bright", "#eef2f8"),
                    ("dim", "#6b7487"),
                    ("dimmer", "#4a5163"),
                    ("green", "#7ec699"),
                    ("orange", "#e0a458"),
                    ("red", "#e06c75"),
                    ("blue", "#7aa2f7"),
                    ("teal", "#6fc3c3"),
                    ("rule", "#333b4d"),
                ],
            ),
            (
                "graphite",
                vec![
                    ("bg-page", "#131110"),
                    ("bg-term", "#211e1a"),
                    ("bg-chrome", "#181512"),
                    ("bg-tab", "#2c2722"),
                    ("fg", "#d6cfc4"),
                    ("bright", "#f2ede4"),
                    ("dim", "#8a8175"),
                    ("dimmer", "#575047"),
                    ("green", "#98c28b"),
                    ("orange", "#dba15c"),
                    ("red", "#d97371"),
                    ("blue", "#90a4d8"),
                    ("teal", "#80bcae"),
                    ("rule", "#3a352e"),
                ],
            ),
            (
                "carbon",
                vec![
                    ("bg-page", "#0c0e12"),
                    ("bg-term", "#14171d"),
                    ("bg-chrome", "#0f1116"),
                    ("bg-tab", "#1f242e"),
                    ("fg", "#cdd6e4"),
                    ("bright", "#f4f7fc"),
                    ("dim", "#65718a"),
                    ("dimmer", "#3d4657"),
                    ("green", "#6fd39c"),
                    ("orange", "#e9b14f"),
                    ("red", "#ef6e7b"),
                    ("blue", "#6f9df2"),
                    ("teal", "#57c8c8"),
                    ("rule", "#2a3140"),
                ],
            ),
        ]
    }

    #[test]
    fn test_three_themes_exist() {
        let names: BTreeSet<&str> = themes().iter().map(|(name, _)| *name).collect();
        let expected: BTreeSet<&str> = ["slate", "graphite", "carbon"].into();
        assert_eq!(names, expected);
        assert_eq!(DEFAULT_THEME, "slate");
    }

    #[test]
    fn test_every_token_hex_matches_spec_exactly() {
        for (theme_name, tokens) in spec_table() {
            for (token, hex_value) in tokens {
                assert_eq!(
                    token_hex(theme_name, token),
                    Some(hex_value),
                    "{theme_name} {token}"
                );
            }
        }
    }

    #[test]
    fn test_no_extra_or_missing_tokens() {
        for (theme_name, _) in spec_table() {
            let actual: BTreeSet<&str> = theme_tokens(theme_name)
                .expect("theme exists")
                .iter()
                .map(|(token, _)| *token)
                .collect();
            let expected: BTreeSet<&str> = TOKEN_NAMES.into();
            assert_eq!(actual, expected, "{theme_name}");
        }
        assert_eq!(TOKEN_NAMES.len(), 14);
    }

    /// Widgets style via `$<token>` — every spec token must be a theme variable.
    #[test]
    fn test_textual_theme_variables_expose_every_token() {
        let spec = spec_table();
        for (theme_name, theme) in themes() {
            let (_, tokens) = spec
                .iter()
                .find(|(name, _)| *name == theme_name)
                .expect("spec theme");
            for token in TOKEN_NAMES {
                let expected = tokens
                    .iter()
                    .find(|(name, _)| *name == token)
                    .map(|(_, hex)| *hex);
                assert_eq!(theme.variable(token), expected, "{theme_name} {token}");
            }
        }
    }

    #[test]
    fn test_theme_names_are_registered_ids() {
        for (theme_name, theme) in themes() {
            assert_eq!(theme.name, theme_id(theme_name));
            assert_eq!(theme.name, format!("amplifier-{theme_name}"));
            assert!(theme.dark);
        }
    }

    /// The semantic slots must reuse spec tokens, not invent colors.
    #[test]
    fn test_semantic_slots_come_from_tokens() {
        let spec = spec_table();
        for (theme_name, theme) in themes() {
            let (_, tokens) = spec
                .iter()
                .find(|(name, _)| *name == theme_name)
                .expect("spec theme");
            let get = |token: &str| -> &str {
                tokens
                    .iter()
                    .find(|(name, _)| *name == token)
                    .map(|(_, hex)| *hex)
                    .expect("spec token")
            };
            assert_eq!(theme.background, get("bg-term"));
            assert_eq!(theme.surface, get("bg-chrome"));
            assert_eq!(theme.panel, get("bg-tab"));
            assert_eq!(theme.foreground, get("fg"));
            assert_eq!(theme.success, get("green"));
            assert_eq!(theme.warning, get("orange"));
            assert_eq!(theme.error, get("red"));
        }
    }

    /// No hard-coded hex colors anywhere outside ui/themes.rs.
    ///
    /// Mirrors the Python test, which scans only package (non-test)
    /// source: inline `#[cfg(test)]` modules are excluded here because
    /// the Python test never scanned `tests/`.
    #[test]
    fn test_hex_values_live_only_in_themes_module() {
        let package_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let hex_pattern = regex::Regex::new(r"#[0-9a-fA-F]{6}\b").unwrap();

        fn rust_files(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
            for entry in std::fs::read_dir(dir).expect("readable src dir") {
                let path = entry.expect("dir entry").path();
                if path.is_dir() {
                    rust_files(&path, out);
                } else if path.extension().is_some_and(|ext| ext == "rs") {
                    out.push(path);
                }
            }
        }

        let mut paths = Vec::new();
        rust_files(&package_root, &mut paths);
        paths.sort();

        let mut offenders: Vec<String> = Vec::new();
        for path in paths {
            if path.file_name().is_some_and(|name| name == "themes.rs") {
                continue;
            }
            let text = std::fs::read_to_string(&path).expect("readable source");
            for (number, line) in text.lines().enumerate() {
                if line.contains("#[cfg(test)]") {
                    break; // inline test module — Python never scanned tests
                }
                if hex_pattern.is_match(line) {
                    offenders.push(format!(
                        "{}:{}: {}",
                        path.strip_prefix(&package_root).unwrap().display(),
                        number + 1,
                        line.trim()
                    ));
                }
            }
        }
        assert!(
            offenders.is_empty(),
            "hex colors outside themes.rs: {offenders:?}"
        );
    }

    #[test]
    fn hex_color_parses_spec_hex() {
        assert_eq!(hex_color("#7aa2f7"), Some(Color::Rgb(0x7a, 0xa2, 0xf7)));
        assert_eq!(hex_color("#12151c"), Some(Color::Rgb(0x12, 0x15, 0x1c)));
        assert_eq!(hex_color("7aa2f7"), None);
        assert_eq!(hex_color("#7aa2f"), None);
        assert_eq!(hex_color("#7aa2fg"), None);
    }
}
