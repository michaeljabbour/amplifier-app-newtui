//! Pure domain state — mirrors the Python app's `model/` layer (no rendering here).

pub mod config;
pub mod evidence;
pub mod formatting;
pub mod injection;
pub mod queues;
pub mod redaction;
pub mod terminal;
pub mod trust;

/// The five interaction modes, cycled with Shift+Tab (as in the real app).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Mode {
    Chat,
    Plan,
    Brainstorm,
    Build,
    Auto,
}

impl Mode {
    pub fn label(self) -> &'static str {
        match self {
            Mode::Chat => "chat",
            Mode::Plan => "plan",
            Mode::Brainstorm => "brainstorm",
            Mode::Build => "build",
            Mode::Auto => "auto",
        }
    }
    pub fn next(self) -> Mode {
        match self {
            Mode::Chat => Mode::Plan,
            Mode::Plan => Mode::Brainstorm,
            Mode::Brainstorm => Mode::Build,
            Mode::Build => Mode::Auto,
            Mode::Auto => Mode::Chat,
        }
    }
}

/// The transcript block vocabulary — a trimmed version of the app's discriminated
/// union (`model/blocks.py`). Each variant renders via a pure function in `ui.rs`.
#[derive(Clone, Debug)]
pub enum Block {
    SessionBanner { bundle: String, session: String },
    User(String),
    Narration(String),
    Tool { summary: String, ok: bool },
    Answer(String),
    TurnRule { files: u32, added: u32, removed: u32, cost: f64 },
}

/// Running session tallies shown in the footer.
#[derive(Clone, Debug, Default)]
pub struct Tallies {
    pub tokens: u64,
    pub cost: f64,
}
