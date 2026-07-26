//! The composer: mode badge + prompt glyph + auto-height input — mirrors
//! `ui/composer.py` (DESIGN-SPEC §2 item 5, §5).
//!
//! Input semantics are POSTED AS MESSAGES; the composer never executes
//! anything itself:
//!
//! - Enter        → [`ComposerMessage::Steer`] while `running` else
//!   [`ComposerMessage::Submit`] (the app owns the running flag and sets it
//!   on the composer — steer-vs-submit is the app's call, made through that
//!   flag).
//! - Shift+Enter  → [`ComposerMessage::QueueMessage`] (alt+enter is the
//!   always-registered legacy-terminal fallback; the `kitty_protocol` probe
//!   flag only changes which chord is *advertised*).
//! - Esc          → [`ComposerMessage::EscPressed`] (app resolves via
//!   `keymap::ESC_CHAIN`).
//! - `/` prefix   → [`ComposerMessage::OpenPalette`] with the live filter,
//!   re-posted on every edit while the text keeps the `/` prefix;
//!   [`ComposerMessage::PaletteFilterCleared`] when the prefix is deleted.
//!
//! Ratatui adaptation: Textual's `TextArea` becomes [`ComposerInput`], a
//! pure text buffer (text + char-offset cursor) with the exact editing
//! semantics the Python tests pin; widget mechanics (mount/compose, CSS,
//! auto-height layout, focus, the message pump) stay in the app assembly.
//! Python's `post_message` becomes an internal queue the event loop drains
//! via [`Composer::drain_messages`]. The clickable `[mode]` badge ports as
//! [`Composer::badge_clicked`] plus the pure `mode_class`/`badge_text`
//! surface the app renders.

use std::time::Instant;

use crate::model::modes::{profile, ModeProfile, DEFAULT_MODE};
use crate::ui::file_mentions::FileMentionIntent;
use crate::ui::keymap::{hint_label, COMPOSER_PLACEHOLDER};

/// Cap on the auto-growing input, in lines.
pub const MAX_INPUT_HEIGHT: usize = 6;

/// Bound the in-memory prompt ring without truncating individual prompts.
pub const MAX_PROMPT_HISTORY: usize = 500;

pub const PASTE_LINE_THRESHOLD: usize = 10;
/// A paste larger than either collapses to a stub (amplifier-app-cli
/// `LosslessTextPasteState` parity): the composer shows a compact
/// `[Pasted #N · … ]` placeholder while the full text is retained and
/// expanded verbatim at submit — so a big paste never floods the composer
/// (what read as 'truncated') and nothing is lost.
pub const PASTE_CHAR_THRESHOLD: usize = 800;

/// Ignore an identical terminal paste replayed immediately.
///
/// Some terminal/input stacks occasionally deliver the same bracketed-paste
/// sequence twice.  The fence is deliberately narrow and also requires the
/// composer text and cursor to be unchanged since the first insertion, so a
/// later intentional repeat or any intervening edit still works normally.
pub const PASTE_DUPLICATE_WINDOW_SECONDS: f64 = 0.15;

const MODE_CLASSES: [&str; 5] = [
    "mode-chat",
    "mode-plan",
    "mode-brainstorm",
    "mode-build",
    "mode-auto",
];

/// The pasted-path → image reader seam (see [`Composer::set_image_detector`]).
pub type ImageDetector = Box<dyn Fn(&str) -> Vec<ImageAttachment>>;

/// Validated image bytes read from the system clipboard.
///
/// Stand-in for `kernel.clipboard.ImageAttachment` — the `kernel/clipboard`
/// unit is not ported yet; when it lands this type moves there (size/type
/// validation included) and this module re-exports it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageAttachment {
    pub data: Vec<u8>,
    pub media_type: String,
}

/// Translate TextArea's `(row, column)` cursor into a text offset.
///
/// Offsets and columns are in characters (Python `str` indexing).
pub fn cursor_offset(text: &str, location: (usize, usize)) -> usize {
    let (row, column) = location;
    let mut offset = 0usize;
    for (index, line) in text.split_inclusive('\n').enumerate() {
        if index >= row {
            break;
        }
        offset += line.chars().count();
    }
    offset + column
}

/// Translate a text offset back into TextArea's cursor location.
pub fn cursor_location(text: &str, offset: usize) -> (usize, usize) {
    let prefix: String = text.chars().take(offset).collect();
    let row = prefix.matches('\n').count();
    let column = prefix.rsplit('\n').next().unwrap_or("").chars().count();
    (row, column)
}

/// Return `(query, start, end)` for the mention under the cursor.
///
/// Python matches `(?<!\S)@([^\s@]*)$` against the text before the cursor;
/// the `regex` crate has no look-behind, so the equivalent backward scan is
/// spelled out: the token after `@` holds no whitespace and no `@`, and the
/// char before `@` is whitespace or the start of the text.
pub fn active_file_mention(text: &str, location: (usize, usize)) -> Option<(String, usize, usize)> {
    let end = cursor_offset(text, location);
    let chars: Vec<char> = text.chars().collect();
    let prefix = &chars[..end.min(chars.len())];
    let mut i = prefix.len();
    while i > 0 {
        let ch = prefix[i - 1];
        if ch == '@' {
            if i >= 2 && !prefix[i - 2].is_whitespace() {
                return None;
            }
            let query: String = prefix[i..].iter().collect();
            return Some((query, i - 1, end));
        }
        if ch.is_whitespace() {
            return None;
        }
        i -= 1;
    }
    None
}

/// Everything the composer posts — Python's nested `Message` classes plus
/// the [`FileMentionIntent`]s the composer forwards, unified into one enum
/// the app's event loop dispatches on.
#[derive(Debug, Clone, PartialEq)]
pub enum ComposerMessage {
    /// Idle Enter: send `text` as a new user turn, with any staged
    /// clipboard images whose `[Image #N]` token survives in `text`.
    Submit {
        text: String,
        attachments: Vec<ImageAttachment>,
    },
    /// Ctrl+V: the app reads the system clipboard image off-thread.
    PasteImage,
    /// Running Enter: steer the current turn with `text`.
    Steer { text: String },
    /// Shift+Enter (or alt+enter): queue `text` as the full next turn.
    QueueMessage { text: String },
    /// Composer text starts with `/` — open/refilter the palette.
    OpenPalette { filter: String },
    /// The `/` prefix was deleted — the palette filter is gone.
    PaletteFilterCleared,
    /// Esc in the composer; the app resolves it via `ESC_CHAIN`.
    EscPressed,
    /// ↑/↓ on an EMPTY composer — the app routes it to an open, unfocused
    /// overlay strip (auto-opened lanes panel, spec §8).
    NavKey { delta: isize },
    /// Enter on an EMPTY composer — focus the selected lane when the lanes
    /// panel is open (otherwise ignored, as before).
    EnterEmpty,
    /// The `[mode]` badge was clicked; the app cycles the mode.
    CycleModeRequested,
    /// Forwarded `@file` autocomplete intent (Python posts these through
    /// the same message pump).
    Mention(FileMentionIntent),
}

/// The text input: pure buffer with the spec placeholder and the paste
/// duplicate fence (Python `ComposerInput(TextArea)` minus the widget).
///
/// The cursor is a character offset; `cursor_location` exposes the
/// TextArea-style `(row, column)` view of it.
#[derive(Debug, Clone)]
pub struct ComposerInput {
    text: String,
    cursor: usize,
    pub placeholder: &'static str,
    last_paste: Option<(String, String, (usize, usize), Instant)>,
}

impl Default for ComposerInput {
    fn default() -> Self {
        ComposerInput::new()
    }
}

impl ComposerInput {
    pub fn new() -> Self {
        ComposerInput {
            text: String::new(),
            cursor: 0,
            placeholder: COMPOSER_PLACEHOLDER,
            last_paste: None,
        }
    }

    pub fn text(&self) -> &str {
        &self.text
    }

    fn char_len(&self) -> usize {
        self.text.chars().count()
    }

    fn byte_at(&self, char_index: usize) -> usize {
        self.text
            .char_indices()
            .nth(char_index)
            .map(|(byte, _)| byte)
            .unwrap_or(self.text.len())
    }

    pub fn cursor_location(&self) -> (usize, usize) {
        cursor_location(&self.text, self.cursor)
    }

    pub fn set_cursor_location(&mut self, location: (usize, usize)) {
        self.set_cursor_offset(cursor_offset(&self.text, location));
    }

    fn set_cursor_offset(&mut self, offset: usize) {
        self.cursor = offset.min(self.char_len());
    }

    /// Insert `text` at the cursor (TextArea `insert`).
    pub fn insert(&mut self, text: &str) {
        let at = self.byte_at(self.cursor);
        self.text.insert_str(at, text);
        self.cursor += text.chars().count();
    }

    /// Delete the character before the cursor (TextArea backspace).
    pub fn backspace(&mut self) {
        if self.cursor == 0 {
            return;
        }
        let start = self.byte_at(self.cursor - 1);
        let end = self.byte_at(self.cursor);
        self.text.replace_range(start..end, "");
        self.cursor -= 1;
    }

    pub fn clear(&mut self) {
        self.text.clear();
        self.cursor = 0;
    }

    /// Replace the whole buffer (TextArea `load_text`; cursor to the start).
    pub fn load_text(&mut self, text: &str) {
        self.text = text.to_string();
        self.cursor = 0;
    }

    /// TextArea's native vertical cursor movement (multi-line drafts keep it
    /// on ↑/↓ when history does not consume the key): move by whole rows,
    /// clamping the column to the target line's length.
    fn move_cursor_vertical(&mut self, delta: isize) {
        let (row, column) = self.cursor_location();
        let lines: Vec<&str> = self.text.split('\n').collect();
        let last = lines.len() as isize - 1;
        let target = (row as isize + delta).clamp(0, last) as usize;
        let width = lines[target].chars().count();
        self.set_cursor_location((target, column.min(width)));
    }

    /// True only for an unchanged, immediate replay of `payload`.
    fn is_duplicate_paste(&self, payload: &str) -> bool {
        let Some((previous_payload, result_text, result_cursor, accepted_at)) = &self.last_paste
        else {
            return false;
        };
        payload == previous_payload
            && accepted_at.elapsed().as_secs_f64() <= PASTE_DUPLICATE_WINDOW_SECONDS
            && self.text == *result_text
            && self.cursor_location() == *result_cursor
    }

    fn remember_paste(&mut self, payload: &str) {
        self.last_paste = Some((
            payload.to_string(),
            self.text.clone(),
            self.cursor_location(),
            Instant::now(),
        ));
    }
}

/// `[mode] ❯ <input>` — the bottom input strip.
pub struct Composer {
    /// Terminal probe flag: changes which queue chord is *advertised*.
    pub kitty_protocol: bool,
    /// The app owns this flag; it decides steer-vs-submit on Enter.
    pub running: bool,
    /// Set by the app while the `@file` strip is open; redirects ↑/↓/enter.
    pub mention_open: bool,
    mode: &'static ModeProfile,
    palette_open: bool,
    mention_filter_active: bool,
    input: ComposerInput,
    /// stub → full retained payload (insertion order, Python dict parity).
    pastes: Vec<(String, String)>,
    paste_seq: usize,
    /// (placeholder, image) staged clipboard attachments.
    attachments: Vec<(String, ImageAttachment)>,
    image_seq: usize,
    history: Vec<String>,
    history_index: Option<usize>,
    history_draft: String,
    /// Seam for `kernel.clipboard.pasted_image_attachments` (unported unit):
    /// the app assembly installs the real path-to-image reader; the default
    /// detects nothing, so pasted paths stay plain text until it lands.
    image_detector: ImageDetector,
    messages: Vec<ComposerMessage>,
}

impl Default for Composer {
    fn default() -> Self {
        Composer::new(true)
    }
}

impl Composer {
    pub fn new(kitty_protocol: bool) -> Self {
        Composer {
            kitty_protocol,
            running: false,
            mention_open: false,
            mode: profile(DEFAULT_MODE),
            palette_open: false,
            mention_filter_active: false,
            input: ComposerInput::new(),
            pastes: Vec::new(),
            paste_seq: 0,
            attachments: Vec::new(),
            image_seq: 0,
            history: Vec::new(),
            history_index: None,
            history_draft: String::new(),
            image_detector: Box::new(|_| Vec::new()),
            messages: Vec::new(),
        }
    }

    // -- message pump ---------------------------------------------------------

    fn post(&mut self, message: ComposerMessage) {
        self.messages.push(message);
    }

    /// Messages posted so far, oldest first (Python `post_message` order).
    pub fn messages(&self) -> &[ComposerMessage] {
        &self.messages
    }

    /// Take the queued messages for dispatch by the app's event loop.
    pub fn drain_messages(&mut self) -> Vec<ComposerMessage> {
        std::mem::take(&mut self.messages)
    }

    // -- public API -------------------------------------------------------------

    pub fn mode(&self) -> &'static ModeProfile {
        self.mode
    }

    /// Adopt `profile`: badge text/color and left-edge accent update.
    pub fn set_mode(&mut self, profile: &'static ModeProfile) {
        self.mode = profile;
    }

    /// The single active accent class (Python `_apply_mode` toggles exactly
    /// one of `_MODE_CLASSES` on both the composer and the badge).
    pub fn mode_class(&self) -> String {
        format!("mode-{}", self.mode.id.as_str())
    }

    /// `has_class` over the mode accent classes.
    pub fn has_class(&self, class: &str) -> bool {
        MODE_CLASSES.contains(&class) && self.mode_class() == class
    }

    /// The `[mode]` badge text (rendered literally, never as markup).
    pub fn badge_text(&self) -> String {
        format!("[{}]", self.mode.id.as_str())
    }

    /// The clickable badge was clicked (Python `ModeBadge.on_click`).
    pub fn badge_clicked(&mut self) {
        self.post(ComposerMessage::CycleModeRequested);
    }

    pub fn text(&self) -> &str {
        self.input.text()
    }

    /// The input buffer, for read-side app assembly (cursor, placeholder).
    pub fn input(&self) -> &ComposerInput {
        &self.input
    }

    /// Install the pasted-path → image reader (app assembly seam; see
    /// [`ImageAttachment`]).
    pub fn set_image_detector(&mut self, detector: ImageDetector) {
        self.image_detector = detector;
    }

    pub fn clear(&mut self) {
        let before = self.input.text().to_string();
        self.input.clear();
        self.notify_if_changed(&before);
        self.end_history_navigation();
        self.mention_open = false;
        self.pastes.clear();
        self.attachments.clear();
        self.image_seq = 0;
    }

    /// Stage a clipboard image and insert its `[Image #N]` placeholder
    /// (deleting the placeholder before submit drops the image).
    pub fn add_image(&mut self, attachment: ImageAttachment) {
        self.image_seq += 1;
        self.end_history_navigation();
        let placeholder = format!("[Image #{}]", self.image_seq);
        self.attachments.push((placeholder.clone(), attachment));
        let text = self.input.text();
        let prefix = if text.is_empty() || text.ends_with(' ') || text.ends_with('\n') {
            ""
        } else {
            " "
        };
        self.insert_and_notify(&format!("{prefix}{placeholder} "));
    }

    /// Images whose placeholder survives in `text` (spec: a deleted
    /// `[Image #N]` token drops that attachment).
    fn staged_attachments(&self, text: &str) -> Vec<ImageAttachment> {
        self.attachments
            .iter()
            .filter(|(placeholder, _)| text.contains(placeholder))
            .map(|(_, image)| image.clone())
            .collect()
    }

    /// Retain a long paste and return its stub; `None` to insert `text`
    /// inline (short pastes stay verbatim in the composer).
    pub fn register_paste(&mut self, text: &str) -> Option<String> {
        let line_count = text.matches('\n').count() + 1;
        let char_count = text.chars().count();
        if line_count <= PASTE_LINE_THRESHOLD && char_count <= PASTE_CHAR_THRESHOLD {
            return None;
        }
        self.paste_seq += 1;
        let measure = if line_count > PASTE_LINE_THRESHOLD {
            format!("{line_count} lines")
        } else {
            format!("{char_count} chars")
        };
        let stub = format!("[Pasted #{} · {measure}]", self.paste_seq);
        self.pastes.push((stub.clone(), text.to_string()));
        Some(stub)
    }

    /// Replace retained paste stubs with their full payloads (Python
    /// `_expand`; public here for the same submit-side callers and tests).
    pub fn expand(&self, text: &str) -> String {
        let mut text = text.to_string();
        for (stub, payload) in &self.pastes {
            text = text.replace(stub.as_str(), payload);
        }
        text
    }

    /// Insert `text` at the cursor (key pass-through from overlay strips —
    /// e.g. typing while the lanes panel holds focus).
    pub fn insert_text(&mut self, text: &str) {
        self.end_history_navigation();
        self.insert_and_notify(text);
    }

    /// Load persisted user prompts so resumed sessions keep ↑ history.
    pub fn seed_history<S: AsRef<str>, I: IntoIterator<Item = S>>(&mut self, prompts: I) {
        for prompt in prompts {
            self.remember_prompt(prompt.as_ref());
        }
        self.end_history_navigation();
    }

    pub fn history_browsing(&self) -> bool {
        self.history_index.is_some()
    }

    /// Recall the previous prompt, preserving the current draft.
    pub fn history_previous(&mut self) -> bool {
        if self.history.is_empty() {
            return false;
        }
        match self.history_index {
            None => {
                self.history_draft = self.input.text().to_string();
                self.history_index = Some(self.history.len() - 1);
            }
            Some(index) if index > 0 => self.history_index = Some(index - 1),
            Some(_) => {}
        }
        let text = self.history[self.history_index.expect("just set")].clone();
        self.load_history_text(&text);
        true
    }

    /// Move toward newer prompts and finally restore the saved draft.
    pub fn history_next(&mut self) -> bool {
        let Some(index) = self.history_index else {
            return false;
        };
        if index < self.history.len() - 1 {
            self.history_index = Some(index + 1);
            let text = self.history[index + 1].clone();
            self.load_history_text(&text);
        } else {
            let draft = std::mem::take(&mut self.history_draft);
            self.end_history_navigation();
            self.load_history_text(&draft);
        }
        true
    }

    pub fn end_history_navigation(&mut self) {
        self.history_index = None;
        self.history_draft.clear();
    }

    /// Replace the active `@query` with `path` and keep typing.
    pub fn apply_file_mention(&mut self, path: &str) -> bool {
        self.end_history_navigation();
        let text = self.input.text().to_string();
        let Some((_, start, end)) = active_file_mention(&text, self.input.cursor_location()) else {
            return false;
        };
        let rendered = if path.chars().any(char::is_whitespace) {
            format!("@\"{path}\"")
        } else {
            format!("@{path}")
        };
        let replacement = format!("{rendered} ");
        let chars: Vec<char> = text.chars().collect();
        let head: String = chars[..start].iter().collect();
        let tail: String = chars[end.min(chars.len())..].iter().collect();
        let updated = format!("{head}{replacement}{tail}");
        let cursor = start + replacement.chars().count();
        self.input.load_text(&updated);
        self.input.set_cursor_offset(cursor);
        self.mention_open = false;
        self.mention_filter_active = false;
        self.post(ComposerMessage::Mention(FileMentionIntent::clear()));
        self.notify_if_changed(&text);
        true
    }

    /// The advertised queue chord: shift+enter, or alt+enter when the kitty
    /// keyboard protocol is absent (terminal probe flag).
    pub fn queue_hint(&self) -> String {
        let overrides: Option<&[(&str, &str)]> = if self.kitty_protocol {
            None
        } else {
            Some(&[("queue_message", "alt+enter")])
        };
        hint_label("queue_message", overrides).expect("queue_message is in KEYMAP")
    }

    // -- input semantics -----------------------------------------------------------

    /// One key press routed through the composer (Python
    /// `ComposerInput._on_key`). Key names are Textual chords (`"enter"`,
    /// `"shift+enter"`, `"ctrl+j"`, …); single-character keys insert
    /// themselves and `"backspace"` deletes, standing in for the stock
    /// TextArea fall-through.
    pub fn handle_key(&mut self, key: &str) {
        if self.mention_open && (key == "up" || key == "down") {
            let delta = if key == "up" { -1 } else { 1 };
            self.post(ComposerMessage::Mention(FileMentionIntent::move_by(delta)));
        } else if self.mention_open && (key == "enter" || key == "tab") {
            self.post(ComposerMessage::Mention(FileMentionIntent::accept()));
        } else if self.mention_open && key == "escape" {
            self.post(ComposerMessage::Mention(FileMentionIntent::clear()));
        } else if key == "enter" {
            self.handle_enter();
        } else if key == "shift+enter" || key == "alt+enter" {
            self.handle_queue();
        } else if key == "ctrl+j" || key == "ctrl+enter" {
            // Multi-line input, amplifier-app-cli parity (its banner:
            // "Multi-line: Ctrl-J"). Ctrl+Enter is a terminal-supported
            // alternate. Ignored while empty: automation that sends Enter
            // as CRLF must not leave a phantom newline in the just-cleared
            // composer.
            if !self.input.text().is_empty() {
                self.end_history_navigation();
                self.insert_and_notify("\n");
            }
        } else if key == "up" {
            // Shell-style prompt history wins for a single-line draft (or
            // while already browsing). Multi-line drafts retain the native
            // vertical cursor movement.
            let history_eligible = self.history_browsing() || !self.input.text().contains('\n');
            if history_eligible && self.history_previous() {
                // consumed by history
            } else if self.input.text().is_empty() {
                // With no history, preserve lanes-panel navigation.
                self.post(ComposerMessage::NavKey { delta: -1 });
            } else {
                self.input.move_cursor_vertical(-1);
            }
        } else if key == "down" {
            if self.history_next() {
                // consumed by history
            } else if self.input.text().is_empty() {
                self.post(ComposerMessage::NavKey { delta: 1 });
            } else {
                self.input.move_cursor_vertical(1);
            }
        } else if key == "ctrl+v" {
            // Clipboard image paste (amplifier-app-cli parity): the app
            // reads the system clipboard off-thread; text paste stays on
            // the terminal's bracketed-paste path (handle_paste).
            self.post(ComposerMessage::PasteImage);
        } else if key == "escape" {
            self.post(ComposerMessage::EscPressed);
        } else {
            self.end_history_navigation();
            if key == "backspace" {
                let before = self.input.text().to_string();
                self.input.backspace();
                self.notify_if_changed(&before);
            } else if key.chars().count() == 1 {
                self.insert_and_notify(key);
            }
            // Other named keys: stock TextArea behavior not modeled here.
        }
    }

    /// A bracketed text paste arrived (Python `ComposerInput._on_paste`):
    /// dedupe an immediate replay, attach pasted image paths, collapse a
    /// big block to a stub, insert small pastes verbatim.
    pub fn handle_paste(&mut self, payload: &str) {
        if payload.is_empty() {
            return;
        }
        if self.input.is_duplicate_paste(payload) {
            return;
        }
        self.end_history_navigation();
        // Cmd+V of an image file and drag-and-drop both arrive here as a
        // bracketed paste of the file path — attach them, don't insert text.
        let images = (self.image_detector)(payload);
        if !images.is_empty() {
            for image in images {
                self.add_image(image);
            }
            self.input.remember_paste(payload);
            return;
        }
        match self.register_paste(payload) {
            None => {
                self.insert_and_notify(payload);
                self.input.remember_paste(payload);
            }
            Some(stub) => {
                self.insert_and_notify(&stub);
                self.input.remember_paste(payload);
            }
        }
    }

    pub fn handle_enter(&mut self) {
        // Stubs are expanded to their full payloads for submission while
        // the composer only ever showed the compact placeholder.
        let raw = self.input.text().to_string();
        let text = self.expand(&raw).trim().to_string();
        if text.is_empty() {
            self.post(ComposerMessage::EnterEmpty);
            return;
        }
        self.remember_prompt(&text);
        if self.running {
            // Steering is text-only (images ride a fresh submit only).
            self.post(ComposerMessage::Steer { text });
        } else {
            let attachments = self.staged_attachments(&raw);
            self.post(ComposerMessage::Submit { text, attachments });
        }
        self.clear();
    }

    pub fn handle_queue(&mut self) {
        let text = self.expand(self.input.text()).trim().to_string();
        if text.is_empty() {
            return;
        }
        self.remember_prompt(&text);
        self.post(ComposerMessage::QueueMessage { text });
        self.clear();
    }

    /// Python `on_text_area_changed` — invoked after every buffer mutation.
    fn on_text_changed(&mut self) {
        let text = self.input.text().to_string();
        if text.starts_with('/') {
            self.palette_open = true;
            // Mockup onInput: the live filter is the TRIMMED value, so
            // "/mode " (trailing space) still matches /mode.
            self.post(ComposerMessage::OpenPalette {
                filter: text.trim().to_string(),
            });
            if self.mention_filter_active {
                self.mention_filter_active = false;
                self.post(ComposerMessage::Mention(FileMentionIntent::clear()));
            }
            return;
        }
        if self.palette_open {
            self.palette_open = false;
            self.post(ComposerMessage::PaletteFilterCleared);
        }
        match active_file_mention(&text, self.input.cursor_location()) {
            Some((query, _, _)) => {
                self.mention_filter_active = true;
                self.post(ComposerMessage::Mention(FileMentionIntent::filter(query)));
            }
            None if self.mention_filter_active => {
                self.mention_filter_active = false;
                self.mention_open = false;
                self.post(ComposerMessage::Mention(FileMentionIntent::clear()));
            }
            None => {}
        }
    }

    // -- internals ---------------------------------------------------------------

    fn insert_and_notify(&mut self, text: &str) {
        let before = self.input.text().to_string();
        self.input.insert(text);
        self.notify_if_changed(&before);
    }

    fn notify_if_changed(&mut self, before: &str) {
        if self.input.text() != before {
            self.on_text_changed();
        }
    }

    fn remember_prompt(&mut self, text: &str) {
        let prompt = text.trim();
        if prompt.is_empty() || self.history.last().map(String::as_str) == Some(prompt) {
            return;
        }
        self.history.push(prompt.to_string());
        if self.history.len() > MAX_PROMPT_HISTORY {
            let excess = self.history.len() - MAX_PROMPT_HISTORY;
            self.history.drain(..excess);
        }
    }

    fn load_history_text(&mut self, text: &str) {
        let before = self.input.text().to_string();
        self.input.load_text(text);
        let end = text.chars().count();
        self.input.set_cursor_offset(end);
        self.notify_if_changed(&before);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kernel::prompt_history::PromptHistoryStore;
    use crate::model::modes::get_mode;
    use crate::ui::file_mentions::MentionAction;

    fn type_str(composer: &mut Composer, text: &str) {
        for ch in text.chars() {
            composer.handle_key(&ch.to_string());
        }
    }

    fn submits(composer: &Composer) -> Vec<(String, Vec<ImageAttachment>)> {
        composer
            .messages()
            .iter()
            .filter_map(|m| match m {
                ComposerMessage::Submit { text, attachments } => {
                    Some((text.clone(), attachments.clone()))
                }
                _ => None,
            })
            .collect()
    }

    fn steers(composer: &Composer) -> Vec<String> {
        composer
            .messages()
            .iter()
            .filter_map(|m| match m {
                ComposerMessage::Steer { text } => Some(text.clone()),
                _ => None,
            })
            .collect()
    }

    fn queued(composer: &Composer) -> Vec<String> {
        composer
            .messages()
            .iter()
            .filter_map(|m| match m {
                ComposerMessage::QueueMessage { text } => Some(text.clone()),
                _ => None,
            })
            .collect()
    }

    fn palette_opens(composer: &Composer) -> Vec<String> {
        composer
            .messages()
            .iter()
            .filter_map(|m| match m {
                ComposerMessage::OpenPalette { filter } => Some(filter.clone()),
                _ => None,
            })
            .collect()
    }

    fn mention_intents(composer: &Composer) -> Vec<FileMentionIntent> {
        composer
            .messages()
            .iter()
            .filter_map(|m| match m {
                ComposerMessage::Mention(intent) => Some(intent.clone()),
                _ => None,
            })
            .collect()
    }

    fn count(composer: &Composer, matcher: impl Fn(&ComposerMessage) -> bool) -> usize {
        composer.messages().iter().filter(|m| matcher(m)).count()
    }

    /// Pins `test_placeholder_is_exact_spec_string`.
    #[test]
    fn test_placeholder_is_exact_spec_string() {
        let composer_input = ComposerInput::new();
        assert_eq!(composer_input.placeholder, COMPOSER_PLACEHOLDER);
        assert_eq!(
            COMPOSER_PLACEHOLDER,
            "Message Amplifier…  ( ↑ history · ctrl+j newline · enter send · / commands )"
        );
    }

    /// Pins `test_idle_enter_posts_submit_and_clears`.
    #[test]
    fn test_idle_enter_posts_submit_and_clears() {
        let mut composer = Composer::default();
        type_str(&mut composer, "hi");
        composer.handle_key("enter");
        let submits = submits(&composer);
        assert_eq!(submits.len(), 1);
        assert_eq!(submits[0].0, "hi");
        assert!(steers(&composer).is_empty());
        assert_eq!(composer.text(), "");
    }

    /// Pins `test_running_enter_posts_steer_not_submit`.
    #[test]
    fn test_running_enter_posts_steer_not_submit() {
        let mut composer = Composer::new(true);
        composer.running = true;
        type_str(&mut composer, "go");
        composer.handle_key("enter");
        let steers = steers(&composer);
        assert_eq!(steers.len(), 1);
        assert_eq!(steers[0], "go");
        assert!(submits(&composer).is_empty());
    }

    /// Pins `test_empty_enter_posts_nothing`.
    #[test]
    fn test_empty_enter_posts_nothing() {
        let mut composer = Composer::default();
        composer.handle_key("enter");
        assert!(submits(&composer).is_empty());
        assert!(steers(&composer).is_empty());
        // The empty Enter still posts EnterEmpty for the lanes panel.
        assert_eq!(count(&composer, |m| *m == ComposerMessage::EnterEmpty), 1);
    }

    /// Pins `test_ctrl_j_and_ctrl_enter_insert_newlines_before_submit`.
    #[test]
    fn test_ctrl_j_and_ctrl_enter_insert_newlines_before_submit() {
        let mut composer = Composer::default();
        type_str(&mut composer, "first");
        composer.handle_key("ctrl+j");
        type_str(&mut composer, "second");
        composer.handle_key("ctrl+enter");
        type_str(&mut composer, "third");
        assert_eq!(composer.text(), "first\nsecond\nthird");
        composer.handle_key("enter");
        assert_eq!(submits(&composer)[0].0, "first\nsecond\nthird");
    }

    /// Pins `test_up_down_recall_prompts_and_restore_current_draft`.
    #[test]
    fn test_up_down_recall_prompts_and_restore_current_draft() {
        let mut composer = Composer::default();
        type_str(&mut composer, "first");
        composer.handle_key("enter");
        type_str(&mut composer, "second");
        composer.handle_key("enter");
        type_str(&mut composer, "draft");

        composer.handle_key("up");
        assert_eq!(composer.text(), "second");
        composer.handle_key("up");
        assert_eq!(composer.text(), "first");
        composer.handle_key("down");
        assert_eq!(composer.text(), "second");
        composer.handle_key("down");
        assert_eq!(composer.text(), "draft");
    }

    /// Pins `test_resumed_prompt_history_is_seeded_and_deduplicated`.
    #[test]
    fn test_resumed_prompt_history_is_seeded_and_deduplicated() {
        let mut composer = Composer::default();
        composer.seed_history(["older prompt", "latest prompt", "latest prompt"]);
        composer.handle_key("up");
        assert_eq!(composer.text(), "latest prompt");
        composer.handle_key("up");
        assert_eq!(composer.text(), "older prompt");
    }

    /// Pins `test_shift_enter_posts_queue_message`.
    #[test]
    fn test_shift_enter_posts_queue_message() {
        let mut composer = Composer::default();
        type_str(&mut composer, "later");
        composer.handle_key("shift+enter");
        let queued = queued(&composer);
        assert_eq!(queued.len(), 1);
        assert_eq!(queued[0], "later");
    }

    /// Pins `test_alt_enter_fallback_posts_queue_message`.
    #[test]
    fn test_alt_enter_fallback_posts_queue_message() {
        let mut composer = Composer::new(false);
        type_str(&mut composer, "x");
        composer.handle_key("alt+enter");
        let queued = queued(&composer);
        assert_eq!(queued.len(), 1);
        assert_eq!(queued[0], "x");
    }

    /// Pins `test_queue_hint_swaps_on_missing_kitty_protocol`.
    #[test]
    fn test_queue_hint_swaps_on_missing_kitty_protocol() {
        assert_eq!(Composer::new(true).queue_hint(), "shift+enter");
        assert_eq!(Composer::new(false).queue_hint(), "alt+enter");
    }

    /// Pins `test_active_file_mention_only_matches_token_at_cursor`.
    #[test]
    fn test_active_file_mention_only_matches_token_at_cursor() {
        let text = "review @src/ap then later";
        assert_eq!(
            active_file_mention(text, (0, 14)),
            Some(("src/ap".to_string(), 7, 14))
        );
        assert_eq!(active_file_mention(text, (0, text.chars().count())), None);
        assert_eq!(active_file_mention("mail@example.com", (0, 16)), None);
    }

    /// Pins `test_file_mention_posts_filter_and_intercepts_navigation`.
    #[test]
    fn test_file_mention_posts_filter_and_intercepts_navigation() {
        let mut composer = Composer::default();
        type_str(&mut composer, "@src");
        let filters: Vec<String> = mention_intents(&composer)
            .into_iter()
            .filter(|m| m.action == MentionAction::Filter)
            .map(|m| m.query)
            .collect();
        assert_eq!(filters, ["", "s", "sr", "src"]);

        composer.mention_open = true;
        composer.handle_key("down");
        composer.handle_key("enter");
        let intents = mention_intents(&composer);
        let moves: Vec<isize> = intents
            .iter()
            .filter(|m| m.action == MentionAction::Move)
            .map(|m| m.delta)
            .collect();
        assert_eq!(moves, [1]);
        let accepts = intents
            .iter()
            .filter(|m| m.action == MentionAction::Accept)
            .count();
        assert_eq!(accepts, 1);
        assert!(submits(&composer).is_empty());
    }

    /// Pins `test_apply_file_mention_replaces_query_and_quotes_spaces`.
    #[test]
    fn test_apply_file_mention_replaces_query_and_quotes_spaces() {
        let mut composer = Composer::default();
        type_str(&mut composer, "open @rea");
        assert!(composer.apply_file_mention("docs/read me.md"));
        assert_eq!(composer.text(), "open @\"docs/read me.md\" ");
    }

    /// Pins `test_short_paste_stays_inline`.
    #[test]
    fn test_short_paste_stays_inline() {
        let mut c = Composer::default();
        assert_eq!(c.register_paste("a short paste\nwith two lines"), None);
    }

    /// Pins `test_long_paste_collapses_to_stub_and_expands`.
    #[test]
    fn test_long_paste_collapses_to_stub_and_expands() {
        let mut c = Composer::default();
        let payload = (0..30)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n"); // > 10 lines
        let stub = c.register_paste(&payload).unwrap();
        assert!(stub.starts_with("[Pasted #1"));
        assert!(stub.contains("30 lines"));
        // composer shows only the stub, but it expands to the full text
        let typed = format!("here is the code: {stub} — please review");
        assert_eq!(
            c.expand(&typed),
            format!("here is the code: {payload} — please review")
        );
        // a big single-line paste (> char threshold) also collapses
        let big = "x".repeat(900);
        let stub2 = c.register_paste(&big).unwrap();
        assert!(stub2.contains("900 chars"));
    }

    /// Pins `test_staged_image_rides_submit_and_drops_when_placeholder_deleted`.
    #[test]
    fn test_staged_image_rides_submit_and_drops_when_placeholder_deleted() {
        let mut png = b"\x89PNG\r\n\x1a\n".to_vec();
        png.extend(std::iter::repeat_n(0u8, 32));

        let mut composer = Composer::default();
        composer.add_image(ImageAttachment {
            data: png.clone(),
            media_type: "image/png".to_string(),
        });
        assert!(composer.text().contains("[Image #1]"));
        type_str(&mut composer, "hi");
        composer.handle_key("enter");
        let submits1 = submits(&composer);
        assert_eq!(submits1.len(), 1);
        assert_eq!(submits1[0].1.len(), 1); // carried with the surviving placeholder
        assert!(submits1[0].0.contains("[Image #1]"));

        // Deleting the placeholder drops the attachment.
        let mut composer2 = Composer::default();
        composer2.add_image(ImageAttachment {
            data: png,
            media_type: "image/png".to_string(),
        });
        // "[Image #1] " is 11 chars; delete them all through the key path.
        for _ in 0.."[Image #1] ".chars().count() {
            composer2.handle_key("backspace");
        }
        assert_eq!(composer2.text(), "");
        type_str(&mut composer2, "just text");
        composer2.handle_key("enter");
        let submits2 = submits(&composer2);
        assert_eq!(submits2.len(), 1);
        assert!(submits2[0].1.is_empty());
    }

    /// Covers the composer-side routing of
    /// `test_pasting_an_image_file_path_attaches_it`: the real path→image
    /// reader lives in the unported `kernel/clipboard`; the detector seam
    /// stands in for it here.
    #[test]
    fn test_pasted_image_path_routes_through_detector() {
        let mut composer = Composer::default();
        composer.set_image_detector(Box::new(|payload| {
            if payload.ends_with(".png") {
                vec![ImageAttachment {
                    data: b"\x89PNG\r\n\x1a\n".to_vec(),
                    media_type: "image/png".to_string(),
                }]
            } else {
                Vec::new()
            }
        }));
        composer.handle_paste("/tmp/shot.png");
        assert!(composer.text().contains("[Image #1]"));
        assert!(!composer.text().contains("/tmp/shot.png")); // path not left as literal text
        composer.handle_key("enter");
        let submits = submits(&composer);
        assert_eq!(submits.len(), 1);
        assert_eq!(submits[0].1.len(), 1);
    }

    /// Pins `test_paste_event_collapses_long_block_and_submits_full_text`.
    #[test]
    fn test_paste_event_collapses_long_block_and_submits_full_text() {
        let mut composer = Composer::default();
        let payload = (0..20)
            .map(|i| format!("row {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        composer.handle_paste(&payload);
        let shown = composer.text().to_string();
        assert!(shown.contains("[Pasted #1"));
        assert!(!shown.contains("row 19")); // collapsed, not flooded
        composer.handle_key("enter");
        let submits = submits(&composer);
        assert_eq!(submits.len(), 1);
        assert_eq!(submits[0].0, payload); // full text restored on submit
        assert_eq!(composer.text(), ""); // cleared, stubs forgotten
    }

    /// Pins `test_immediate_identical_paste_replay_is_suppressed`.
    #[test]
    fn test_immediate_identical_paste_replay_is_suppressed() {
        let mut composer = Composer::default();
        let payload = "investigate the ~/dev/amplifier-runpodsetup setup";
        composer.handle_paste(payload);
        composer.handle_paste(payload);
        assert_eq!(composer.text(), payload);

        std::thread::sleep(std::time::Duration::from_secs_f64(
            PASTE_DUPLICATE_WINDOW_SECONDS + 0.05,
        ));
        composer.handle_paste(payload);
        assert_eq!(composer.text(), format!("{payload}{payload}"));
    }

    /// Pins `test_slash_prefix_posts_live_palette_filters`.
    #[test]
    fn test_slash_prefix_posts_live_palette_filters() {
        let mut composer = Composer::default();
        type_str(&mut composer, "/mo");
        assert_eq!(palette_opens(&composer), ["/", "/m", "/mo"]);
    }

    /// Pins `test_deleting_slash_prefix_clears_palette_filter`.
    #[test]
    fn test_deleting_slash_prefix_clears_palette_filter() {
        let mut composer = Composer::default();
        type_str(&mut composer, "/m");
        composer.handle_key("backspace");
        composer.handle_key("backspace");
        assert_eq!(
            count(&composer, |m| *m == ComposerMessage::PaletteFilterCleared),
            1
        );
    }

    /// Pins `test_escape_posts_esc_pressed`.
    #[test]
    fn test_escape_posts_esc_pressed() {
        let mut composer = Composer::default();
        composer.handle_key("escape");
        assert_eq!(count(&composer, |m| *m == ComposerMessage::EscPressed), 1);
    }

    /// Pins `test_mode_badge_click_requests_cycle` (the Textual click event
    /// becomes the `badge_clicked` method; the message is the contract).
    #[test]
    fn test_mode_badge_click_requests_cycle() {
        let mut composer = Composer::default();
        composer.badge_clicked();
        assert_eq!(
            count(&composer, |m| *m == ComposerMessage::CycleModeRequested),
            1
        );
    }

    /// Pins `test_set_mode_updates_badge_and_accent_classes` (the pure
    /// class/text state; CSS application is the app assembly's job).
    #[test]
    fn test_set_mode_updates_badge_and_accent_classes() {
        let mut composer = Composer::default();
        // Default: auto — the boot posture (§4 amendment), orange accent.
        assert!(composer.has_class("mode-auto"));
        assert_eq!(composer.badge_text(), "[auto]");
        // chat's accent uses the rule token via the mode-chat class.
        composer.set_mode(get_mode(Some("chat")));
        assert!(composer.has_class("mode-chat"));
        assert!(!composer.has_class("mode-auto"));
        assert_eq!(composer.badge_text(), "[chat]");
        composer.set_mode(get_mode(Some("build")));
        assert!(composer.has_class("mode-build"));
        assert!(!composer.has_class("mode-chat"));
        assert_eq!(composer.badge_text(), "[build]");
    }

    /// Pins `test_palette_filter_is_trimmed_of_trailing_whitespace`.
    #[test]
    fn test_palette_filter_is_trimmed_of_trailing_whitespace() {
        let mut composer = Composer::default();
        type_str(&mut composer, "/m ");
        assert_eq!(palette_opens(&composer), ["/", "/m", "/m"]);
    }

    /// Pins the composer half of
    /// `test_ui_prompt_history.py::test_fresh_session_recalls_prior_session_prompts`
    /// (the adapter/app boot wiring is app assembly).
    #[test]
    fn test_fresh_session_recalls_prior_session_prompts() {
        let mut composer = Composer::default();
        composer.seed_history(["older prompt", "command A"]);
        composer.handle_key("up");
        assert_eq!(composer.text(), "command A"); // most recent first
        composer.handle_key("up");
        assert_eq!(composer.text(), "older prompt");
    }

    /// Pins `test_ui_prompt_history.py::test_end_to_end_kill_then_fresh_session_same_dir`
    /// through the real store: submit in session 1, then a fresh session in
    /// the same dir recalls it on ↑.
    #[test]
    fn test_end_to_end_kill_then_fresh_session_same_dir() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("repl_history");

        // Session 1 submits "command A" (what the app does on submit).
        PromptHistoryStore::at_path(path.clone()).append("command A");

        // Session 2 boots fresh in the same dir and seeds ↑ from the store.
        let prior = PromptHistoryStore::at_path(path).load();
        let mut composer = Composer::default();
        composer.seed_history(&prior);
        composer.handle_key("up");
        assert_eq!(composer.text(), "command A");
    }

    /// Rust-only: cursor round-trip helpers behind `active_file_mention`
    /// (Python `_cursor_offset` / `_cursor_location`).
    #[test]
    fn test_cursor_offset_location_round_trip() {
        let text = "ab\ncd\nef";
        assert_eq!(cursor_offset(text, (0, 0)), 0);
        assert_eq!(cursor_offset(text, (1, 1)), 4);
        assert_eq!(cursor_offset(text, (2, 2)), 8);
        assert_eq!(cursor_location(text, 4), (1, 1));
        assert_eq!(cursor_location(text, 8), (2, 2));
        assert_eq!(cursor_location(text, 0), (0, 0));
    }
}
