//! The reducer — mirrors `ui/reducer.py`. A stateful `UiEvent → mutation`
//! translator that never draws; `ui.rs` renders purely from this state.

use crate::event::UiEvent;
use crate::model::{Block, Mode, Tallies};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum TurnState {
    Idle,
    Running,
    AwaitingApproval,
}

pub struct App {
    pub blocks: Vec<Block>,
    pub live: Option<String>, // the single mutable streaming region (LiveTail)
    pub composer: String,
    pub mode: Mode,
    pub tallies: Tallies,
    pub state: TurnState,
    pub pending_action: Option<String>,
    pub pending_ticket: Option<String>,
    pub notice: Option<String>,
    pub spinner: usize,
    pub scroll: u16,
    pub should_quit: bool,
    pub bundle: String,
    pub session: String,
}

const SPINNER: [&str; 8] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧"];

impl App {
    pub fn new(bundle: &str, session: &str) -> Self {
        let mut app = Self {
            blocks: Vec::new(),
            live: None,
            composer: String::new(),
            mode: Mode::Chat,
            tallies: Tallies::default(),
            state: TurnState::Idle,
            pending_action: None,
            pending_ticket: None,
            notice: None,
            spinner: 0,
            scroll: 0,
            should_quit: false,
            bundle: bundle.into(),
            session: session.into(),
        };
        app.blocks.push(Block::SessionBanner {
            bundle: bundle.into(),
            session: session.into(),
        });
        app.blocks.push(Block::Narration(
            "Demo session — press Enter to run a scripted turn. Shift+Tab cycles mode. Ctrl+C quits.".into(),
        ));
        app
    }

    pub fn spinner_frame(&self) -> &'static str {
        SPINNER[self.spinner % SPINNER.len()]
    }

    pub fn state_label(&self) -> &'static str {
        match self.state {
            TurnState::Idle => "idle",
            TurnState::Running => "working",
            TurnState::AwaitingApproval => "needs you",
        }
    }

    /// The reducer: fold a normalized runtime event into UI state.
    pub fn on_event(&mut self, ev: UiEvent) {
        match ev {
            UiEvent::PromptSubmit(text) => {
                self.blocks.push(Block::User(text));
                self.state = TurnState::Running;
                self.notice = None;
            }
            UiEvent::Narration(text) => self.blocks.push(Block::Narration(text)),
            UiEvent::ToolLine { summary, ok } => self.blocks.push(Block::Tool { summary, ok }),
            UiEvent::ApprovalRequired { ticket_id, action } => {
                self.state = TurnState::AwaitingApproval;
                self.pending_action = Some(action);
                self.pending_ticket = Some(ticket_id);
            }
            UiEvent::StreamStart => self.live = Some(String::new()),
            UiEvent::StreamDelta(d) => {
                if let Some(buf) = self.live.as_mut() {
                    buf.push_str(&d);
                }
            }
            UiEvent::StreamEnd => {
                if let Some(buf) = self.live.take() {
                    self.blocks.push(Block::Answer(buf));
                }
            }
            UiEvent::TurnComplete { files, added, removed, tokens, cost } => {
                self.tallies.tokens += tokens;
                self.tallies.cost += cost;
                self.blocks.push(Block::TurnRule { files, added, removed, cost });
                self.state = TurnState::Idle;
            }
            UiEvent::Notice(n) => self.notice = Some(n),
        }
    }

    pub fn tick(&mut self) {
        if self.state == TurnState::Running {
            self.spinner = self.spinner.wrapping_add(1);
        }
    }
}
