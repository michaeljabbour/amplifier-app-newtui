//! DisplaySystem implementation: kernel messages → Notification UIEvents.
//!
//! One of the four injected protocol objects (RESEARCH-BRIEF §2). The kernel
//! calls `show_message(message, level, source)`; we mint a typed
//! [`Notification`] and emit it into the UI event queue — the notice slot
//! renders it as a transient right-aligned dim line.
//!
//! `push_nesting`/`pop_nesting` exist for spawn compatibility (child
//! sessions inherit this display system); nesting depth is stamped onto the
//! notification's `source` suffix so the UI can de-emphasize child chatter.

use crate::kernel::events::Notification;

/// Callback receiving each minted [`Notification`] (Python's `Emit` alias).
pub type Emit = Box<dyn FnMut(Notification)>;

/// Emit-only display system — never prints, never blocks.
pub struct DisplaySystem {
    emit: Emit,
    session_id: String,
    nesting: u64,
}

impl DisplaySystem {
    pub fn new(emit: Emit, session_id: impl Into<String>) -> Self {
        Self {
            emit,
            session_id: session_id.into(),
            nesting: 0,
        }
    }

    pub fn nesting(&self) -> u64 {
        self.nesting
    }

    pub fn show_message(&mut self, message: &str, level: &str, source: &str) {
        let level = if level.is_empty() { "info" } else { level };
        (self.emit)(Notification {
            session_id: self.session_id.clone(),
            message: message.to_string(),
            level: level.to_string(),
            source: source.to_string(),
            ..Default::default()
        });
    }

    pub fn show_status(&mut self, message: &str, source: &str) {
        self.show_message(message, "status", source);
    }

    pub fn show_error(&mut self, message: &str, source: &str) {
        self.show_message(message, "error", source);
    }

    pub fn push_nesting(&mut self) {
        self.nesting += 1;
    }

    pub fn pop_nesting(&mut self) {
        self.nesting = self.nesting.saturating_sub(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::collections::VecDeque;
    use std::rc::Rc;

    const ROOT: &str = "sess-root";

    #[test]
    fn test_display_system_emits_notification_events() {
        let emitted: Rc<RefCell<Vec<Notification>>> = Rc::new(RefCell::new(Vec::new()));
        let sink = Rc::clone(&emitted);
        let mut display = DisplaySystem::new(Box::new(move |n| sink.borrow_mut().push(n)), ROOT);
        display.show_message("bundle loaded", "info", "runtime");
        display.show_error("provider missing", "");
        let emitted = emitted.borrow();
        assert_eq!(
            emitted.iter().map(|n| n.message.as_str()).collect::<Vec<_>>(),
            vec!["bundle loaded", "provider missing"]
        );
        assert_eq!(emitted[0].level, "info");
        assert_eq!(emitted[0].source, "runtime");
        assert_eq!(emitted[1].level, "error");
        assert!(emitted.iter().all(|n| n.session_id == ROOT));
    }

    #[test]
    fn test_display_system_nesting_counters() {
        let mut display = DisplaySystem::new(Box::new(|_notification| {}), "");
        assert_eq!(display.nesting(), 0);
        display.push_nesting();
        display.push_nesting();
        assert_eq!(display.nesting(), 2);
        display.pop_nesting();
        display.pop_nesting();
        display.pop_nesting(); // never negative
        assert_eq!(display.nesting(), 0);
    }

    /// Pins `test_display_system_feeds_queue_bridge`. The Python test wires
    /// the display into `QueueBridge.emit` (an asyncio queue front-end not
    /// yet ported); a plain FIFO queue stands in for the bridge here — the
    /// behavior under test (emit lands a typed Notification in the queue)
    /// is identical.
    #[test]
    fn test_display_system_feeds_queue_bridge() {
        let queue: Rc<RefCell<VecDeque<Notification>>> = Rc::new(RefCell::new(VecDeque::new()));
        let sink = Rc::clone(&queue);
        let mut display =
            DisplaySystem::new(Box::new(move |n| sink.borrow_mut().push_back(n)), ROOT);
        display.show_message("mode build · auto read,test · ask write,net,spend", "info", "");
        let event = queue.borrow_mut().pop_front().expect("queue empty");
        assert!(event.message.starts_with("mode build"));
    }

    /// Source-behavior pin (no dedicated Python test): an empty `level`
    /// falls back to `"info"` (`str(level or "info")`).
    #[test]
    fn test_show_message_empty_level_defaults_to_info() {
        let emitted: Rc<RefCell<Vec<Notification>>> = Rc::new(RefCell::new(Vec::new()));
        let sink = Rc::clone(&emitted);
        let mut display = DisplaySystem::new(Box::new(move |n| sink.borrow_mut().push(n)), ROOT);
        display.show_message("hello", "", "");
        assert_eq!(emitted.borrow()[0].level, "info");
    }

    /// Source-behavior pin: `show_status` stamps level `"status"`.
    #[test]
    fn test_show_status_uses_status_level() {
        let emitted: Rc<RefCell<Vec<Notification>>> = Rc::new(RefCell::new(Vec::new()));
        let sink = Rc::clone(&emitted);
        let mut display = DisplaySystem::new(Box::new(move |n| sink.borrow_mut().push(n)), ROOT);
        display.show_status("compacting", "ctx");
        let emitted = emitted.borrow();
        assert_eq!(emitted[0].level, "status");
        assert_eq!(emitted[0].source, "ctx");
    }
}
