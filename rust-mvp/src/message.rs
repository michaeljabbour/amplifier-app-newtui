//! The single event-loop message type: terminal input, runtime events, and a
//! spinner tick all funnel through one channel (the app-loop queue).

use crate::protocol::WireEvent;
use crossterm::event::Event as CEvent;

pub enum Msg {
    Term(CEvent),
    Rt(WireEvent),
    /// A line of backend boot/module chatter (the serve process's stderr) —
    /// surfaced on the splash status line so slow boots show what's loading.
    BootChatter(String),
    Tick,
}
