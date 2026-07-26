//! Width-aware surface hint injected at `provider:request` (issue #35).
//!
//! docs/BACKLOG.md section 2: the packaged bundle carries a *static* terminal
//! response contract, but nothing tells the model how WIDE the surface
//! currently is — and a project/user bundle override can silently drop the
//! static contract entirely. This app-level hook injects a per-request,
//! width-aware surface hint for every active bundle, so pathological output
//! (wide tables, deep nesting) is prevented rather than rendered badly.
//!
//! Mechanism mirrors the clipboard image injector, NOT the steering bridge:
//! it edits the root session's context messages directly and returns
//! `continue`, instead of returning `inject_context`. That is deliberate —
//! the hook registry merges every `provider:request` `inject_context` result
//! into ONE message governed by a single `ephemeral` flag, so a second
//! `inject_context` hook here would flip the steering bridge's *persistent*
//! steer to ephemeral and break rewind's turn accounting. Direct context
//! editing side-steps that collision entirely.
//!
//! It keeps exactly ONE hint present: a single `system` message tagged with
//! [`SURFACE_HINT_SOURCE`] in its metadata, refreshed in place whenever the
//! width changes (a resize therefore lands on the next turn's request) and
//! re-inserted if a `/clear` or compaction dropped it. Root session only —
//! subagents render through the root's summary, not the terminal. Because it
//! is app-level, it survives any bundle override.
//!
//! Ported from `src/amplifier_app_newtui/kernel/surface_hint.py`. The Python
//! hook is `async`; per the migration conventions the decision logic is
//! synchronous here (no async runtime in the crate).

use std::sync::Arc;

use serde_json::Value;

use crate::kernel::events::Payload;
use crate::model::terminal::TerminalSurface;

/// Metadata marker identifying the single managed surface-hint message.
pub const SURFACE_HINT_SOURCE: &str = "newtui-surface-hint";

/// Default registration priority for the surface-hint hook.
pub const SURFACE_HINT_PRIORITY: i64 = 940;

/// Minimal mirror of `amplifier_core.HookResult` — only the shape this unit
/// needs (the hook always continues). Private to this module by design; a
/// shared mirror can be unified later.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookResult {
    pub action: String,
}

impl HookResult {
    fn cont() -> Self {
        Self {
            action: "continue".to_string(),
        }
    }
}

/// The width-aware surface-hint line for a *cols*-wide terminal.
pub fn surface_hint_text(cols: u16) -> String {
    format!(
        "terminal, ~{cols} cols; markdown subset: no images, \
         tables \u{2264}4 columns, prefer fenced code with language tags, \
         short paragraphs."
    )
}

fn is_hint(message: &Payload) -> bool {
    message
        .get("metadata")
        .and_then(Value::as_object)
        .and_then(|metadata| metadata.get("source"))
        .and_then(Value::as_str)
        == Some(SURFACE_HINT_SOURCE)
}

fn hint_message(cols: u16) -> Payload {
    let mut metadata = Payload::new();
    metadata.insert("source".to_string(), Value::from(SURFACE_HINT_SOURCE));
    let mut message = Payload::new();
    message.insert("role".to_string(), Value::from("system"));
    message.insert("content".to_string(), Value::from(surface_hint_text(cols)));
    message.insert("metadata".to_string(), Value::Object(metadata));
    message
}

/// `str(data.get("session_id") or root)`: falsy values (missing, null, empty
/// string, `false`, zero, empty containers) fall back to the root id; any
/// other JSON value is stringified.
fn session_id_from(data: &Payload, root: &str) -> String {
    match data.get("session_id") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        Some(Value::Number(n)) if n.as_f64() != Some(0.0) => n.to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Array(items)) if !items.is_empty() => Value::from(items.clone()).to_string(),
        Some(Value::Object(map)) if !map.is_empty() => Value::from(map.clone()).to_string(),
        _ => root.to_string(),
    }
}

/// The message store the injector reconciles — the Rust face of the Python
/// duck-typed context object.
///
/// [`SurfaceHintContext::can_edit`] mirrors the Python `hasattr` guard for
/// `get_messages`/`set_messages`: a context that cannot both read and write
/// is left untouched (safe no-op).
pub trait SurfaceHintContext {
    /// Whether the context supports both reading and writing messages.
    fn can_edit(&self) -> bool {
        true
    }

    fn get_messages(&self) -> Vec<Payload>;

    fn set_messages(&mut self, messages: Vec<Payload>);
}

/// A hook registry the injector can attach to. `register` returns the
/// unregister callback if the registry provides one (mirrors Python
/// registries that may hand back a non-callable).
///
/// The handler itself is the injector's [`SurfaceHintInjector::handle_event`];
/// the Rust registry dispatches to it by name/event rather than by a captured
/// bound method, so `register` carries only the routing metadata.
pub trait HookRegistry {
    fn register(&mut self, event: &str, priority: i64, name: &str) -> Option<Box<dyn FnOnce()>>;
}

/// Keep one width-aware surface hint in the root session's context.
///
/// Registered on `provider:request` (root only). `prepare`-free: it reads the
/// live width from the shared [`TerminalSurface`] on every root request and
/// reconciles the context to hold exactly one current hint.
pub struct SurfaceHintInjector<C: SurfaceHintContext> {
    root_session_id: String,
    surface: Arc<TerminalSurface>,
    context: C,
}

impl<C: SurfaceHintContext> SurfaceHintInjector<C> {
    /// Events this hook subscribes to.
    pub const EVENTS: &'static [&'static str] = &["provider:request"];

    pub fn new(
        root_session_id: impl Into<String>,
        surface: Arc<TerminalSurface>,
        context: C,
    ) -> Self {
        Self {
            root_session_id: root_session_id.into(),
            surface,
            context,
        }
    }

    /// Handle one hook event; always continues, editing context as a side
    /// effect when a hint must be inserted or refreshed.
    pub fn handle_event(&mut self, event: &str, data: &Payload) -> HookResult {
        if event != "provider:request" || !self.context.can_edit() {
            return HookResult::cont();
        }
        let session_id = session_id_from(data, &self.root_session_id);
        if session_id != self.root_session_id {
            // Subagents render through the root's summary, not the terminal.
            return HookResult::cont();
        }
        let cols = self.surface.cols();
        let desired = surface_hint_text(cols);
        let mut messages = self.context.get_messages();
        if let Some(index) = messages.iter().position(is_hint) {
            if messages[index].get("content").and_then(Value::as_str) == Some(desired.as_str()) {
                return HookResult::cont(); // already current: no write
            }
            messages[index] = hint_message(cols);
            self.context.set_messages(messages);
            return HookResult::cont();
        }
        // No hint present (fresh turn, or dropped by /clear or compaction):
        // insert it right after the leading system block, before the dialogue.
        let mut insert_at = 0;
        while insert_at < messages.len()
            && messages[insert_at].get("role").and_then(Value::as_str) == Some("system")
        {
            insert_at += 1;
        }
        messages.insert(insert_at, hint_message(cols));
        self.context.set_messages(messages);
        HookResult::cont()
    }

    /// Register on `provider:request` at [`SURFACE_HINT_PRIORITY`] (the
    /// Python default `priority=940`).
    pub fn register_hooks(&self, hooks: &mut dyn HookRegistry) -> Box<dyn FnOnce()> {
        self.register_hooks_with_priority(hooks, SURFACE_HINT_PRIORITY)
    }

    /// Register with an explicit priority; hands back a no-op unregister when
    /// the registry did not provide a callable one.
    pub fn register_hooks_with_priority(
        &self,
        hooks: &mut dyn HookRegistry,
        priority: i64,
    ) -> Box<dyn FnOnce()> {
        match hooks.register("provider:request", priority, "newtui-surface-hint") {
            Some(unregister) => unregister,
            None => Box::new(|| {}),
        }
    }

    /// The context this injector reconciles (primarily for inspection).
    pub fn context(&self) -> &C {
        &self.context
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::rc::Rc;

    use serde_json::json;

    use super::*;

    const ROOT: &str = "sess-root";

    fn msg(value: Value) -> Payload {
        value.as_object().expect("message literal").clone()
    }

    /// Minimal get/set message store mirroring the offline FakeContext.
    /// A cloneable handle so the test can inspect/mutate the store the
    /// injector owns (the Python test shares the object by reference).
    #[derive(Clone, Default)]
    struct FakeContext {
        inner: Rc<RefCell<FakeContextInner>>,
    }

    #[derive(Default)]
    struct FakeContextInner {
        messages: Vec<Payload>,
        set_calls: usize,
    }

    impl FakeContext {
        fn with_messages(messages: Vec<Payload>) -> Self {
            let context = Self::default();
            context.inner.borrow_mut().messages = messages;
            context
        }

        fn messages(&self) -> Vec<Payload> {
            self.inner.borrow().messages.clone()
        }

        fn set_calls(&self) -> usize {
            self.inner.borrow().set_calls
        }

        fn replace_messages(&self, messages: Vec<Payload>) {
            self.inner.borrow_mut().messages = messages;
        }
    }

    impl SurfaceHintContext for FakeContext {
        fn get_messages(&self) -> Vec<Payload> {
            self.inner.borrow().messages.clone()
        }

        fn set_messages(&mut self, messages: Vec<Payload>) {
            let mut inner = self.inner.borrow_mut();
            inner.set_calls += 1;
            inner.messages = messages;
        }
    }

    #[derive(Clone, Default)]
    struct FakeHooks {
        registered: Rc<RefCell<Vec<(String, i64, String)>>>,
        unregistered: Rc<RefCell<Vec<String>>>,
    }

    impl HookRegistry for FakeHooks {
        fn register(
            &mut self,
            event: &str,
            priority: i64,
            name: &str,
        ) -> Option<Box<dyn FnOnce()>> {
            self.registered
                .borrow_mut()
                .push((event.to_string(), priority, name.to_string()));
            let unregistered = Rc::clone(&self.unregistered);
            let name = name.to_string();
            Some(Box::new(move || unregistered.borrow_mut().push(name)))
        }
    }

    fn hints(context: &FakeContext) -> Vec<Payload> {
        context
            .messages()
            .into_iter()
            .filter(is_hint)
            .collect()
    }

    fn content_of(message: &Payload) -> &str {
        message
            .get("content")
            .and_then(Value::as_str)
            .expect("string content")
    }

    #[test]
    fn test_hint_text_carries_width_and_markdown_subset() {
        let text = surface_hint_text(97);
        assert!(text.contains("~97 cols"));
        assert!(text.contains("no images"));
        assert!(text.contains("tables \u{2264}4 columns"));
        assert!(text.contains("fenced code with language tags"));
    }

    #[test]
    fn test_injects_current_width_as_one_system_message() {
        let context =
            FakeContext::with_messages(vec![msg(json!({"role": "system", "content": "system prompt"}))]);
        let mut injector = SurfaceHintInjector::new(
            ROOT,
            Arc::new(TerminalSurface::new(120)),
            context.clone(),
        );

        let result = injector.handle_event("provider:request", &msg(json!({"session_id": ROOT})));
        assert_eq!(result.action, "continue");

        let hint_messages = hints(&context);
        assert_eq!(hint_messages.len(), 1);
        assert_eq!(
            hint_messages[0].get("role").and_then(Value::as_str),
            Some("system")
        );
        assert!(content_of(&hint_messages[0]).contains("~120 cols"));
        // Placed right after the leading system prompt, before the dialogue.
        let messages = context.messages();
        assert_eq!(content_of(&messages[0]), "system prompt");
        assert_eq!(content_of(&messages[1]), content_of(&hint_messages[0]));
    }

    #[test]
    fn test_hint_tracks_a_resize_in_place_without_duplicating() {
        let context = FakeContext::with_messages(vec![
            msg(json!({"role": "system", "content": "sp"})),
            msg(json!({"role": "user", "content": "hi"})),
        ]);
        let surface = Arc::new(TerminalSurface::new(80));
        let mut injector =
            SurfaceHintInjector::new(ROOT, Arc::clone(&surface), context.clone());

        injector.handle_event("provider:request", &msg(json!({"session_id": ROOT})));
        assert!(content_of(&hints(&context)[0]).contains("~80 cols"));

        surface.set_cols(40); // the user narrows the terminal mid-session
        injector.handle_event("provider:request", &msg(json!({"session_id": ROOT})));
        let hint_messages = hints(&context);
        assert_eq!(hint_messages.len(), 1); // updated in place, never a second hint
        assert!(content_of(&hint_messages[0]).contains("~40 cols"));
        assert!(!content_of(&hint_messages[0]).contains("~80 cols"));
        // The user prompt is untouched.
        assert!(context
            .messages()
            .iter()
            .any(|m| m.get("content").and_then(Value::as_str) == Some("hi")));
    }

    #[test]
    fn test_already_current_hint_is_not_rewritten() {
        let context =
            FakeContext::with_messages(vec![msg(json!({"role": "system", "content": "sp"}))]);
        let mut injector = SurfaceHintInjector::new(
            ROOT,
            Arc::new(TerminalSurface::new(100)),
            context.clone(),
        );

        injector.handle_event("provider:request", &msg(json!({"session_id": ROOT})));
        let writes_after_first = context.set_calls();
        injector.handle_event("provider:request", &msg(json!({"session_id": ROOT})));
        // Width unchanged and hint present -> no redundant set_messages.
        assert_eq!(context.set_calls(), writes_after_first);
    }

    #[test]
    fn test_reinserts_hint_if_context_was_cleared() {
        let context =
            FakeContext::with_messages(vec![msg(json!({"role": "system", "content": "sp"}))]);
        let mut injector = SurfaceHintInjector::new(
            ROOT,
            Arc::new(TerminalSurface::new(90)),
            context.clone(),
        );
        injector.handle_event("provider:request", &msg(json!({"session_id": ROOT})));
        assert_eq!(hints(&context).len(), 1);

        // Simulate /clear or compaction dropping the managed message.
        context.replace_messages(vec![msg(json!({"role": "system", "content": "sp"}))]);
        injector.handle_event("provider:request", &msg(json!({"session_id": ROOT})));
        assert_eq!(hints(&context).len(), 1);
        assert!(content_of(&hints(&context)[0]).contains("~90 cols"));
    }

    #[test]
    fn test_child_session_is_left_alone() {
        // Subagents render through the root's summary, not the terminal surface.
        let context =
            FakeContext::with_messages(vec![msg(json!({"role": "system", "content": "sp"}))]);
        let mut injector = SurfaceHintInjector::new(
            ROOT,
            Arc::new(TerminalSurface::new(120)),
            context.clone(),
        );
        let result = injector.handle_event(
            "provider:request",
            &msg(json!({"session_id": "sess-child_worker"})),
        );
        assert_eq!(result.action, "continue");
        assert!(hints(&context).is_empty());
        assert_eq!(context.set_calls(), 0);
    }

    #[test]
    fn test_missing_session_id_defaults_to_root_and_injects() {
        let context = FakeContext::default();
        let mut injector =
            SurfaceHintInjector::new(ROOT, Arc::new(TerminalSurface::new(64)), context.clone());
        injector.handle_event("provider:request", &Payload::new());
        assert!(content_of(&hints(&context)[0]).contains("~64 cols"));
    }

    #[test]
    fn test_non_provider_request_events_are_ignored() {
        let context =
            FakeContext::with_messages(vec![msg(json!({"role": "system", "content": "sp"}))]);
        let mut injector =
            SurfaceHintInjector::new(ROOT, Arc::new(TerminalSurface::default()), context.clone());
        let result = injector.handle_event("tool:pre", &msg(json!({"session_id": ROOT})));
        assert_eq!(result.action, "continue");
        assert_eq!(context.set_calls(), 0);
    }

    #[test]
    fn test_context_without_set_messages_is_a_safe_noop() {
        struct ReadOnly;

        impl SurfaceHintContext for ReadOnly {
            fn can_edit(&self) -> bool {
                false // Python: no `set_messages` attribute
            }

            fn get_messages(&self) -> Vec<Payload> {
                Vec::new()
            }

            fn set_messages(&mut self, _messages: Vec<Payload>) {
                panic!("set_messages must never be called on a read-only context");
            }
        }

        let mut injector =
            SurfaceHintInjector::new(ROOT, Arc::new(TerminalSurface::new(80)), ReadOnly);
        let result = injector.handle_event("provider:request", &msg(json!({"session_id": ROOT})));
        assert_eq!(result.action, "continue");
    }

    #[test]
    fn test_register_hooks_priority_and_name() {
        let mut hooks = FakeHooks::default();
        let injector = SurfaceHintInjector::new(
            ROOT,
            Arc::new(TerminalSurface::default()),
            FakeContext::default(),
        );
        let unregister = injector.register_hooks(&mut hooks);
        assert_eq!(
            *hooks.registered.borrow(),
            vec![(
                "provider:request".to_string(),
                940,
                "newtui-surface-hint".to_string()
            )]
        );
        unregister();
        assert_eq!(
            *hooks.unregistered.borrow(),
            vec!["newtui-surface-hint".to_string()]
        );
    }

    #[test]
    fn test_register_hooks_tolerates_non_callable_unregister() {
        struct NullHooks;

        impl HookRegistry for NullHooks {
            fn register(
                &mut self,
                _event: &str,
                _priority: i64,
                _name: &str,
            ) -> Option<Box<dyn FnOnce()>> {
                None
            }
        }

        let injector = SurfaceHintInjector::new(
            ROOT,
            Arc::new(TerminalSurface::default()),
            FakeContext::default(),
        );
        injector.register_hooks(&mut NullHooks)(); // must hand back a no-op, never crash
    }
}
