//! The single event-loop message type: terminal input, runtime events, and a
//! spinner tick all funnel through one channel (the app-loop queue).

use crate::event::UiEvent;
use crossterm::event::Event as CEvent;

pub enum Msg {
    Term(CEvent),
    Rt(UiEvent),
    Tick,
}
