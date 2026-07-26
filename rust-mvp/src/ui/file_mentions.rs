//! Composer `@file` autocomplete strip — mirrors `ui/file_mentions.py`.
//!
//! The composer retains keyboard focus; this controlled overlay only presents
//! a ranked workspace index and yields a path when a row is accepted. The
//! Textual widget mechanics (mount/remove_children, CSS, message pump) do not
//! port; what ports is the pure state machine (`FileMentionStrip`), the intent
//! type and its dispatch (`handle_file_mention_intent`), and a
//! render-to-spans surface producing the exact text plus semantic style
//! tokens the Python rows and hint line render.

use crate::kernel::file_mentions::{filter_file_mentions, DEFAULT_FILTER_LIMIT};

/// `MentionAction = Literal["filter", "clear", "move", "accept", "select"]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MentionAction {
    Filter,
    Clear,
    Move,
    Accept,
    Select,
}

impl MentionAction {
    /// Exact Python literal for this action.
    pub fn as_str(self) -> &'static str {
        match self {
            MentionAction::Filter => "filter",
            MentionAction::Clear => "clear",
            MentionAction::Move => "move",
            MentionAction::Accept => "accept",
            MentionAction::Select => "select",
        }
    }
}

/// One composer/row intent, dispatched outside the app composition root.
///
/// Python's `FileMentionIntent(Message)` minus the Textual message plumbing
/// (`message.stop()` is the caller's concern in the ratatui event loop).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileMentionIntent {
    pub action: MentionAction,
    pub query: String,
    pub delta: isize,
    pub path: String,
}

impl FileMentionIntent {
    /// Mirror the Python constructor: keyword fields default to `""`/`0`/`""`.
    pub fn new(action: MentionAction) -> Self {
        FileMentionIntent {
            action,
            query: String::new(),
            delta: 0,
            path: String::new(),
        }
    }

    pub fn filter(query: impl Into<String>) -> Self {
        FileMentionIntent {
            query: query.into(),
            ..FileMentionIntent::new(MentionAction::Filter)
        }
    }

    pub fn clear() -> Self {
        FileMentionIntent::new(MentionAction::Clear)
    }

    pub fn move_by(delta: isize) -> Self {
        FileMentionIntent {
            delta,
            ..FileMentionIntent::new(MentionAction::Move)
        }
    }

    pub fn accept() -> Self {
        FileMentionIntent::new(MentionAction::Accept)
    }

    /// Row click in Python (`_MentionRow.on_click`) posts `select` with the
    /// row's path.
    pub fn select(path: impl Into<String>) -> Self {
        FileMentionIntent {
            path: path.into(),
            ..FileMentionIntent::new(MentionAction::Select)
        }
    }
}

/// Hint line shown above the ranked rows (exact Python `Static` text).
pub const FILE_MENTION_HINT: &str = "@ file  ·  ↑↓ select  ·  enter insert  ·  esc close";

/// Semantic style tokens for the strip's spans, keyed to the theme variables
/// the Python widgets resolve at render time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MentionStyle {
    /// `.file-mention-hint`: theme `$dimmer`.
    Hint,
    /// Leading `@` sigil: theme `green`, bold.
    Sigil,
    /// Unselected row path: theme `fg`.
    Path,
    /// Selected row path: theme `bright` (row background `$bg-tab`).
    PathSelected,
}

/// One styled fragment of a rendered strip line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MentionSpan {
    pub text: String,
    pub style: MentionStyle,
}

/// Ranked workspace paths shown immediately above the composer.
///
/// Pure state machine behind Python's `FileMentionStrip(VerticalScroll)`:
/// filtering, selection clamping, and open/closed display state. Scrolling,
/// row mounting, and click handling stay in the app-assembly layer.
#[derive(Debug, Default)]
pub struct FileMentionStrip {
    paths: Vec<String>,
    matches: Vec<String>,
    selected: usize,
    display: bool,
}

impl FileMentionStrip {
    pub fn new() -> Self {
        FileMentionStrip::default()
    }

    pub fn is_open(&self) -> bool {
        self.display
    }

    pub fn matches(&self) -> &[String] {
        &self.matches
    }

    pub fn selected_path(&self) -> Option<&str> {
        self.matches.get(self.selected).map(String::as_str)
    }

    /// Index of the currently selected row (Python `_selected`).
    pub fn selected_index(&self) -> usize {
        self.selected
    }

    pub fn set_files<S: Into<String>, I: IntoIterator<Item = S>>(&mut self, paths: I) {
        self.paths = paths.into_iter().map(Into::into).collect();
    }

    /// `None` clears the strip; otherwise rank the workspace index against
    /// `query` and open the strip iff any path matched.
    pub fn apply_filter(&mut self, query: Option<&str>) {
        self.matches = match query {
            None => Vec::new(),
            Some(query) => filter_file_mentions(&self.paths, query, DEFAULT_FILTER_LIMIT),
        };
        self.selected = 0;
        self.display = !self.matches.is_empty();
    }

    /// Clamp-move the selection; no-op while the strip has no matches.
    pub fn move_selection(&mut self, delta: isize) {
        if self.matches.is_empty() {
            return;
        }
        let last = (self.matches.len() - 1) as isize;
        self.selected = (self.selected as isize + delta).clamp(0, last) as usize;
    }

    /// Render the open strip: the hint line, then one `@path` row per match
    /// with the selected row's path styled `bright`. Empty when closed —
    /// Python sets `display = bool(matches)` and unmounts all children.
    pub fn render_lines(&self) -> Vec<Vec<MentionSpan>> {
        if self.matches.is_empty() {
            return Vec::new();
        }
        let mut lines = vec![vec![MentionSpan {
            text: FILE_MENTION_HINT.to_owned(),
            style: MentionStyle::Hint,
        }]];
        for (index, path) in self.matches.iter().enumerate() {
            lines.push(vec![
                MentionSpan {
                    text: "@".to_owned(),
                    style: MentionStyle::Sigil,
                },
                MentionSpan {
                    text: path.clone(),
                    style: if index == self.selected {
                        MentionStyle::PathSelected
                    } else {
                        MentionStyle::Path
                    },
                },
            ]);
        }
        lines
    }
}

/// The slice of the app the mention handlers touch — Python duck-types `app`
/// (`app.file_mentions`, `app.palette`, `app.composer`); the ratatui assembly
/// layer implements this over its real widgets.
pub trait MentionHost {
    fn file_mentions(&mut self) -> &mut FileMentionStrip;
    /// `app.palette.apply_filter(None)` — close the command palette.
    fn clear_palette_filter(&mut self);
    /// `app.composer.mention_open = open`.
    fn set_composer_mention_open(&mut self, open: bool);
    /// `app.composer.apply_file_mention(path)`.
    fn apply_file_mention(&mut self, path: &str);
    /// `app.composer.focus_input()`.
    fn focus_composer_input(&mut self);
}

/// Close suggestions while leaving the composer and its text intact.
pub fn close_file_mentions(app: &mut dyn MentionHost) {
    app.file_mentions().apply_filter(None);
    app.set_composer_mention_open(false);
}

/// Apply a mention intent; extracted to keep the app assembly composition-only.
pub fn handle_file_mention_intent(app: &mut dyn MentionHost, message: &FileMentionIntent) {
    match message.action {
        MentionAction::Filter => {
            app.clear_palette_filter();
            app.file_mentions().apply_filter(Some(&message.query));
            let open = app.file_mentions().is_open();
            app.set_composer_mention_open(open);
        }
        MentionAction::Move => {
            app.file_mentions().move_selection(message.delta);
        }
        MentionAction::Accept | MentionAction::Select => {
            let path = if message.path.is_empty() {
                app.file_mentions().selected_path().map(str::to_owned)
            } else {
                Some(message.path.clone())
            };
            if let Some(path) = path {
                app.apply_file_mention(&path);
            }
            close_file_mentions(app);
            app.focus_composer_input();
        }
        MentionAction::Clear => close_file_mentions(app),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Pins `tests/test_ui_file_mentions.py::test_strip_filters_and_clamps_selection`
    /// (the pure state assertions; the Textual `run_test` harness and
    /// `pilot.pause` DOM settling do not port).
    #[test]
    fn test_strip_filters_and_clamps_selection() {
        let mut strip = FileMentionStrip::new();
        strip.set_files(["README.md", "docs/README-dev.md", "src/app.py"]);
        strip.apply_filter(Some("read"));
        assert!(strip.is_open());
        assert_eq!(
            strip.matches(),
            ["README.md".to_owned(), "docs/README-dev.md".to_owned()],
        );
        assert_eq!(strip.selected_path(), Some("README.md"));

        strip.move_selection(20);
        assert_eq!(strip.selected_path(), Some("docs/README-dev.md"));
        strip.apply_filter(None);
        assert!(!strip.is_open());
    }

    /// Rust-only: the strip renders the hint line plus `@path` rows with the
    /// selected row highlighted (pins `_MentionRow.render` + hint `Static`).
    #[test]
    fn test_render_lines_hint_rows_and_selection_styles() {
        let mut strip = FileMentionStrip::new();
        assert!(strip.render_lines().is_empty());

        strip.set_files(["README.md", "docs/README-dev.md"]);
        strip.apply_filter(Some("read"));
        strip.move_selection(1);
        let lines = strip.render_lines();
        assert_eq!(lines.len(), 3);
        assert_eq!(
            lines[0],
            vec![MentionSpan {
                text: "@ file  ·  ↑↓ select  ·  enter insert  ·  esc close".to_owned(),
                style: MentionStyle::Hint,
            }],
        );
        assert_eq!(
            lines[1],
            vec![
                MentionSpan {
                    text: "@".to_owned(),
                    style: MentionStyle::Sigil,
                },
                MentionSpan {
                    text: "README.md".to_owned(),
                    style: MentionStyle::Path,
                },
            ],
        );
        assert_eq!(
            lines[2],
            vec![
                MentionSpan {
                    text: "@".to_owned(),
                    style: MentionStyle::Sigil,
                },
                MentionSpan {
                    text: "docs/README-dev.md".to_owned(),
                    style: MentionStyle::PathSelected,
                },
            ],
        );
    }

    /// Host double recording the composer/palette side effects the Python
    /// handler drives on the real app.
    #[derive(Default)]
    struct RecordingHost {
        strip: FileMentionStrip,
        mention_open: Option<bool>,
        palette_cleared: bool,
        applied: Vec<String>,
        focused: bool,
    }

    impl MentionHost for RecordingHost {
        fn file_mentions(&mut self) -> &mut FileMentionStrip {
            &mut self.strip
        }
        fn clear_palette_filter(&mut self) {
            self.palette_cleared = true;
        }
        fn set_composer_mention_open(&mut self, open: bool) {
            self.mention_open = Some(open);
        }
        fn apply_file_mention(&mut self, path: &str) {
            self.applied.push(path.to_owned());
        }
        fn focus_composer_input(&mut self) {
            self.focused = true;
        }
    }

    /// Rust-only: pins the `handle_file_mention_intent` dispatch table —
    /// filter opens (clearing the palette), move steps, accept inserts the
    /// selected path and closes, clear closes without inserting.
    #[test]
    fn test_handle_file_mention_intent_dispatch() {
        let mut host = RecordingHost::default();
        host.strip
            .set_files(["README.md", "docs/README-dev.md", "src/app.py"]);

        handle_file_mention_intent(&mut host, &FileMentionIntent::filter("read"));
        assert!(host.palette_cleared);
        assert!(host.strip.is_open());
        assert_eq!(host.mention_open, Some(true));

        handle_file_mention_intent(&mut host, &FileMentionIntent::move_by(1));
        assert_eq!(host.strip.selected_path(), Some("docs/README-dev.md"));

        handle_file_mention_intent(&mut host, &FileMentionIntent::accept());
        assert_eq!(host.applied, ["docs/README-dev.md".to_owned()]);
        assert!(!host.strip.is_open());
        assert_eq!(host.mention_open, Some(false));
        assert!(host.focused);

        // Row click carries an explicit path that wins over the selection.
        handle_file_mention_intent(&mut host, &FileMentionIntent::filter("read"));
        handle_file_mention_intent(&mut host, &FileMentionIntent::select("src/app.py"));
        assert_eq!(host.applied.last().map(String::as_str), Some("src/app.py"));

        host.focused = false;
        handle_file_mention_intent(&mut host, &FileMentionIntent::filter("read"));
        handle_file_mention_intent(&mut host, &FileMentionIntent::clear());
        assert!(!host.strip.is_open());
        assert_eq!(host.mention_open, Some(false));
        assert!(!host.focused);
        assert_eq!(host.applied.len(), 2);
    }

    /// Rust-only: exact Python literals for `MentionAction`.
    #[test]
    fn test_mention_action_literals() {
        assert_eq!(MentionAction::Filter.as_str(), "filter");
        assert_eq!(MentionAction::Clear.as_str(), "clear");
        assert_eq!(MentionAction::Move.as_str(), "move");
        assert_eq!(MentionAction::Accept.as_str(), "accept");
        assert_eq!(MentionAction::Select.as_str(), "select");
    }
}
