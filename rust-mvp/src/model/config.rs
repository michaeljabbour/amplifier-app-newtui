//! Live session configuration state (the `/config` domain model).
//!
//! Port of `src/amplifier_app_newtui/model/config.py`: a pure,
//! rendering-free state object that both the demo and real runtimes drive
//! identically (ADR-0007 invariant 4). The state is the session's live view
//! of its bundle configuration:
//!
//! - **categories** — `context` / `tools` / `hooks` / `providers` / `agents`
//!   items, each enabled or disabled (`hooks` is read-only, matching the
//!   donor: a runtime hook suspend/resume API does not exist);
//! - **overrides** — `set <path> <value>` values with the donor's
//!   bool -> int -> float -> string type inference;
//! - **snapshot / diff** — an origin snapshot captured at startup so
//!   `/config diff` reports what changed this session.

use std::fmt;

use serde_json::{json, Map, Value};

/// The mount-plan sections `/config` surfaces (donor minus `behaviors`:
/// newtui's plan has no behavior-group layer).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ConfigCategory {
    Context,
    Tools,
    Hooks,
    Providers,
    Agents,
}

impl ConfigCategory {
    pub fn as_str(self) -> &'static str {
        match self {
            ConfigCategory::Context => "context",
            ConfigCategory::Tools => "tools",
            ConfigCategory::Hooks => "hooks",
            ConfigCategory::Providers => "providers",
            ConfigCategory::Agents => "agents",
        }
    }
}

impl fmt::Display for ConfigCategory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Display order of the categories in `/config show` (donor order).
pub const CONFIG_CATEGORIES: [ConfigCategory; 5] = [
    ConfigCategory::Context,
    ConfigCategory::Tools,
    ConfigCategory::Hooks,
    ConfigCategory::Providers,
    ConfigCategory::Agents,
];

/// Categories that render but cannot toggle. Donor parity: hook toggle
/// needs a core suspend/resume API that does not exist, so hooks are
/// inspection-only (`command_config_dashboard._handle_config_toggle`).
pub const READ_ONLY_CATEGORIES: [&str; 1] = ["hooks"];

fn is_read_only(category: &str) -> bool {
    READ_ONLY_CATEGORIES.contains(&category)
}

fn is_known_category(category: &str) -> bool {
    CONFIG_CATEGORIES.iter().any(|c| c.as_str() == category)
}

/// The parsed intent of a `/config ...` command line (`kind` field).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum InvocationKind {
    #[default]
    Help,
    Show,
    Category,
    Item,
    Toggle,
    Set,
    Diff,
    Save,
    Error,
}

impl InvocationKind {
    pub fn as_str(self) -> &'static str {
        match self {
            InvocationKind::Help => "help",
            InvocationKind::Show => "show",
            InvocationKind::Category => "category",
            InvocationKind::Item => "item",
            InvocationKind::Toggle => "toggle",
            InvocationKind::Set => "set",
            InvocationKind::Diff => "diff",
            InvocationKind::Save => "save",
            InvocationKind::Error => "error",
        }
    }
}

const SCOPES: [&str; 3] = ["global", "project", "local"];

/// An inferred `/config set` value: the donor's `bool | int | float | str`.
#[derive(Clone, Debug, PartialEq)]
pub enum ConfigValue {
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

impl ConfigValue {
    /// Python `repr()` of the value, as embedded in donor messages
    /// (`✓ Set path = 'high'`, diff actions `= 0.8`, ...).
    pub fn py_repr(&self) -> String {
        match self {
            ConfigValue::Bool(true) => "True".to_string(),
            ConfigValue::Bool(false) => "False".to_string(),
            ConfigValue::Int(i) => i.to_string(),
            ConfigValue::Float(f) => py_float_repr(*f),
            ConfigValue::Str(s) => py_str_repr(s),
        }
    }

    fn to_json(&self) -> Value {
        match self {
            ConfigValue::Bool(b) => Value::Bool(*b),
            ConfigValue::Int(i) => json!(i),
            ConfigValue::Float(f) => json!(f),
            ConfigValue::Str(s) => Value::String(s.clone()),
        }
    }
}

fn py_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "nan".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    let s = format!("{f}");
    if s.contains('.') || s.contains('e') || s.contains('E') {
        s
    } else {
        format!("{s}.0")
    }
}

fn py_str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Infer `bool -> int -> float -> str` from a raw `/config set` value.
///
/// Verbatim port of the donor's `_handle_config_set` inference
/// (`command_config_dashboard.py`): `true`/`false` (case-insensitive)
/// become booleans, then integer, then float, else the string is kept.
pub fn parse_value(text: &str) -> ConfigValue {
    let lowered = text.trim().to_lowercase();
    if lowered == "true" {
        return ConfigValue::Bool(true);
    }
    if lowered == "false" {
        return ConfigValue::Bool(false);
    }
    let trimmed = text.trim();
    if let Ok(int) = trimmed.parse::<i64>() {
        return ConfigValue::Int(int);
    }
    if let Ok(float) = trimmed.parse::<f64>() {
        return ConfigValue::Float(float);
    }
    ConfigValue::Str(text.to_string())
}

/// One toggleable configuration entry within a category.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigItem {
    pub category: String,
    pub name: String,
    pub enabled: bool,
    /// A short right-hand descriptor (module id, source, ...) for display.
    pub detail: String,
}

impl ConfigItem {
    pub fn new(category: &str, name: &str, enabled: bool, detail: &str) -> Self {
        Self {
            category: category.to_string(),
            name: name.to_string(),
            enabled,
            detail: detail.to_string(),
        }
    }

    pub fn read_only(&self) -> bool {
        is_read_only(&self.category)
    }
}

/// One line of `/config diff` — what changed since startup.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigChange {
    pub category: String,
    pub name: String,
    pub action: String,
}

impl ConfigChange {
    fn new(category: &str, name: &str, action: String) -> Self {
        Self {
            category: category.to_string(),
            name: name.to_string(),
            action,
        }
    }
}

/// The parsed intent of a `/config ...` command line.
#[derive(Clone, Debug, PartialEq)]
pub struct ConfigInvocation {
    pub kind: InvocationKind,
    pub category: String,
    pub name: String,
    pub enable: bool,
    pub path: String,
    pub value: String,
    pub scope: String,
    pub message: String,
}

impl Default for ConfigInvocation {
    fn default() -> Self {
        Self {
            kind: InvocationKind::Help,
            category: String::new(),
            name: String::new(),
            enable: false,
            path: String::new(),
            value: String::new(),
            scope: "global".to_string(),
            message: String::new(),
        }
    }
}

impl ConfigInvocation {
    fn of_kind(kind: InvocationKind) -> Self {
        Self {
            kind,
            ..Self::default()
        }
    }

    fn error(message: String) -> Self {
        Self {
            kind: InvocationKind::Error,
            message,
            ..Self::default()
        }
    }
}

/// Route a raw `/config` argument string to a [`ConfigInvocation`].
///
/// Port of the donor's `_get_config_display` dispatch (minus the Rich
/// display `--compact/--detailed/--trees/--format` flags, which the TUI
/// renders natively rather than as text views):
///
/// - no args -> `help`
/// - `show` \[`<category>` \[`<name>`\]\] -> `show` / `category` / `item`
/// - `diff` -> `diff`
/// - `save` \[`--scope global|project|local`\] -> `save`
/// - `set <path> <value>` -> `set`
/// - `<category>` -> `category`
/// - `<category> enable|disable <name>` -> `toggle`
/// - `<category> <name>` -> `item`
/// - anything else -> `error` with a usage line
pub fn parse_config_command(args: &str) -> ConfigInvocation {
    let parts: Vec<&str> = args.split_whitespace().collect();
    let Some(&first) = parts.first() else {
        return ConfigInvocation::of_kind(InvocationKind::Help);
    };

    let head = first.to_lowercase();

    if head == "show" {
        let rest = &parts[1..];
        let Some(&raw_category) = rest.first() else {
            return ConfigInvocation::of_kind(InvocationKind::Show);
        };
        let category = raw_category.to_lowercase();
        if !is_known_category(&category) {
            return ConfigInvocation::error(format!(
                "unknown category '{raw_category}' \u{b7} {}",
                category_hint()
            ));
        }
        if rest.len() == 1 {
            return ConfigInvocation {
                kind: InvocationKind::Category,
                category,
                ..ConfigInvocation::default()
            };
        }
        return ConfigInvocation {
            kind: InvocationKind::Item,
            category,
            name: rest[1].to_string(),
            ..ConfigInvocation::default()
        };
    }

    if head == "diff" {
        return ConfigInvocation::of_kind(InvocationKind::Diff);
    }

    if head == "save" {
        return parse_save(&parts[1..]);
    }

    if head == "set" {
        if parts.len() < 3 {
            return ConfigInvocation::error("usage: /config set <path> <value>".to_string());
        }
        return ConfigInvocation {
            kind: InvocationKind::Set,
            path: parts[1].to_string(),
            value: parts[2].to_string(),
            ..ConfigInvocation::default()
        };
    }

    if is_known_category(&head) {
        let rest = &parts[1..];
        let Some(&action_raw) = rest.first() else {
            return ConfigInvocation {
                kind: InvocationKind::Category,
                category: head,
                ..ConfigInvocation::default()
            };
        };
        let action = action_raw.to_lowercase();
        if action == "enable" || action == "disable" {
            if rest.len() < 2 {
                return ConfigInvocation::error(format!("usage: /config {head} {action} <name>"));
            }
            return ConfigInvocation {
                kind: InvocationKind::Toggle,
                category: head,
                name: rest[1].to_string(),
                enable: action == "enable",
                ..ConfigInvocation::default()
            };
        }
        return ConfigInvocation {
            kind: InvocationKind::Item,
            category: head,
            name: rest[0].to_string(),
            ..ConfigInvocation::default()
        };
    }

    ConfigInvocation::error(format!(
        "unknown /config subcommand '{first}' \u{b7} try /config"
    ))
}

fn parse_save(rest: &[&str]) -> ConfigInvocation {
    let mut scope = "global".to_string();
    let mut index = 0;
    while index < rest.len() {
        let token = rest[index];
        if token == "--scope" && index + 1 < rest.len() {
            scope = rest[index + 1].to_lowercase();
            index += 2;
            continue;
        }
        if let Some(value) = token.strip_prefix("--scope=") {
            scope = value.to_lowercase();
        } else if SCOPES.contains(&token.to_lowercase().as_str()) {
            scope = token.to_lowercase();
        }
        index += 1;
    }
    if !SCOPES.contains(&scope.as_str()) {
        return ConfigInvocation::error(format!(
            "unknown scope '{scope}' \u{b7} use global | project | local"
        ));
    }
    ConfigInvocation {
        kind: InvocationKind::Save,
        scope,
        ..ConfigInvocation::default()
    }
}

fn category_hint() -> String {
    let names: Vec<&str> = CONFIG_CATEGORIES.iter().map(|c| c.as_str()).collect();
    format!("categories: {}", names.join(", "))
}

fn get_value<'a>(values: &'a [(String, ConfigValue)], path: &str) -> Option<&'a ConfigValue> {
    values.iter().find(|(p, _)| p == path).map(|(_, v)| v)
}

fn set_value_raw(values: &mut Vec<(String, ConfigValue)>, path: &str, value: ConfigValue) {
    if let Some(slot) = values.iter_mut().find(|(p, _)| p == path) {
        slot.1 = value;
    } else {
        values.push((path.to_string(), value));
    }
}

/// The live, mutable configuration state for one session.
///
/// Seeded from the resolved mount plan (real) or a representative demo
/// snapshot, then mutated by `/config <category> disable|enable` and
/// `/config set`. [`SessionConfigState::snapshot`] freezes the startup
/// state so [`SessionConfigState::diff`] can report the session's changes;
/// [`SessionConfigState::to_settings`] serializes those changes for
/// `/config save`.
#[derive(Clone, Debug)]
pub struct SessionConfigState {
    pub bundle: String,
    /// Items in display order, unique by `(category, name)` — a later
    /// duplicate replaces the entry while keeping its original position
    /// (Python dict-insert semantics).
    items: Vec<ConfigItem>,
    /// Overrides in insertion order (Python dict semantics).
    values: Vec<(String, ConfigValue)>,
    origin_enabled: Vec<(String, String, bool)>,
    origin_values: Vec<(String, ConfigValue)>,
}

impl SessionConfigState {
    pub fn new(items: Vec<ConfigItem>, bundle: &str) -> Self {
        Self::with_values(items, bundle, Vec::new())
    }

    pub fn with_values(
        items: Vec<ConfigItem>,
        bundle: &str,
        values: Vec<(String, ConfigValue)>,
    ) -> Self {
        let mut stored: Vec<ConfigItem> = Vec::new();
        for item in items {
            if let Some(existing) = stored
                .iter_mut()
                .find(|i| i.category == item.category && i.name == item.name)
            {
                *existing = item;
            } else {
                stored.push(item);
            }
        }
        let mut vals: Vec<(String, ConfigValue)> = Vec::new();
        for (path, value) in values {
            set_value_raw(&mut vals, &path, value);
        }
        let mut state = Self {
            bundle: bundle.to_string(),
            items: stored,
            values: vals,
            origin_enabled: Vec::new(),
            origin_values: Vec::new(),
        };
        state.snapshot();
        state
    }

    // -- snapshot / diff ----------------------------------------------------

    /// Freeze the current enabled-state + overrides as the diff origin.
    pub fn snapshot(&mut self) {
        self.origin_enabled = self
            .items
            .iter()
            .map(|item| (item.category.clone(), item.name.clone(), item.enabled))
            .collect();
        self.origin_values = self.values.clone();
    }

    /// Every enabled-state flip and override change since [`Self::snapshot`].
    pub fn diff(&self) -> Vec<ConfigChange> {
        let mut changes: Vec<ConfigChange> = Vec::new();
        for item in &self.items {
            let origin = self
                .origin_enabled
                .iter()
                .find(|(category, name, _)| *category == item.category && *name == item.name)
                .map(|(_, _, enabled)| *enabled)
                .unwrap_or(item.enabled);
            if item.enabled != origin {
                let action = if item.enabled { "enabled" } else { "disabled" };
                changes.push(ConfigChange::new(
                    &item.category,
                    &item.name,
                    action.to_string(),
                ));
            }
        }
        let mut paths: Vec<&str> = self
            .values
            .iter()
            .chain(self.origin_values.iter())
            .map(|(path, _)| path.as_str())
            .collect();
        paths.sort_unstable();
        paths.dedup();
        for path in paths {
            let new = get_value(&self.values, path);
            let old = get_value(&self.origin_values, path);
            if new == old {
                continue;
            }
            match new {
                None => changes.push(ConfigChange::new("set", path, "removed".to_string())),
                Some(value) => {
                    changes.push(ConfigChange::new("set", path, format!("= {}", value.py_repr())))
                }
            }
        }
        changes
    }

    // -- queries ------------------------------------------------------------

    /// All items, optionally filtered to `category`, in display order.
    pub fn items(&self, category: Option<&str>) -> Vec<ConfigItem> {
        match category {
            None => self.items.clone(),
            Some(category) => self
                .items
                .iter()
                .filter(|item| item.category == category)
                .cloned()
                .collect(),
        }
    }

    pub fn find(&self, category: &str, name: &str) -> Option<&ConfigItem> {
        self.items
            .iter()
            .find(|item| item.category == category && item.name == name)
    }

    pub fn overrides(&self) -> Vec<(String, ConfigValue)> {
        self.values.clone()
    }

    pub fn value(&self, path: &str) -> Option<&ConfigValue> {
        get_value(&self.values, path)
    }

    // -- mutations ----------------------------------------------------------

    /// Enable/disable an item; returns `(ok, message)`.
    ///
    /// Hooks are read-only (donor parity); an unknown item is refused.
    pub fn toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String) {
        if is_read_only(category) {
            return (
                false,
                format!("{category} are read-only \u{b7} visible for inspection, not toggleable"),
            );
        }
        let Some(item) = self
            .items
            .iter_mut()
            .find(|item| item.category == category && item.name == name)
        else {
            return (false, format!("no {category} item named '{name}'"));
        };
        if item.enabled == enable {
            let state = if enable { "enabled" } else { "disabled" };
            return (false, format!("{name} already {state}"));
        }
        item.enabled = enable;
        let verb = if enable { "Enabled" } else { "Disabled" };
        (true, format!("\u{2713} {verb} {name}"))
    }

    /// Set an override with the donor's type inference; `(ok, message)`.
    pub fn set_value(&mut self, path: &str, raw_value: &str) -> (bool, String) {
        if path.is_empty() {
            return (false, "usage: /config set <path> <value>".to_string());
        }
        let parsed = parse_value(raw_value);
        let message = format!("\u{2713} Set {path} = {}", parsed.py_repr());
        set_value_raw(&mut self.values, path, parsed);
        (true, message)
    }

    // -- persistence --------------------------------------------------------

    /// Serialize the session's changes for `/config save`.
    ///
    /// Shape (stored under a `configurator:` settings key, donor parity):
    /// `{"disabled": {category: [names...]}, "overrides": {path: value}}`.
    /// Only items the session actively disabled and any overrides are
    /// recorded — an untouched default is not re-listed.
    pub fn to_settings(&self) -> Value {
        let mut disabled: Vec<(String, Vec<String>)> = Vec::new();
        for item in &self.items {
            if !item.enabled && !is_read_only(&item.category) {
                if let Some((_, names)) = disabled
                    .iter_mut()
                    .find(|(category, _)| *category == item.category)
                {
                    names.push(item.name.clone());
                } else {
                    disabled.push((item.category.clone(), vec![item.name.clone()]));
                }
            }
        }
        let mut settings = Map::new();
        if !disabled.is_empty() {
            let mut map = Map::new();
            for (category, names) in disabled {
                map.insert(category, json!(names));
            }
            settings.insert("disabled".to_string(), Value::Object(map));
        }
        if !self.values.is_empty() {
            let mut map = Map::new();
            for (path, value) in &self.values {
                map.insert(path.clone(), value.to_json());
            }
            settings.insert("overrides".to_string(), Value::Object(map));
        }
        Value::Object(settings)
    }

    pub fn change_count(&self) -> usize {
        self.diff().len()
    }
}

fn plan_entries<'a>(mount_plan: &'a Value, section: &str) -> &'a [Value] {
    match mount_plan.get(section) {
        Some(Value::Array(entries)) => entries,
        _ => &[],
    }
}

fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `str()` of a JSON scalar (the plan values are strings in practice).
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Null => "None".to_string(),
        other => other.to_string(),
    }
}

fn entry_name(entry: &Value) -> String {
    if let Value::Object(map) = entry {
        for key in ["id", "instance_id", "name", "module"] {
            if let Some(value) = map.get(key) {
                if is_truthy(value) {
                    return py_str(value);
                }
            }
        }
        return String::new();
    }
    py_str(entry)
}

fn entry_detail(entry: &Value) -> String {
    if let Value::Object(map) = entry {
        let module = map
            .get("module")
            .filter(|value| is_truthy(value))
            .map(py_str)
            .unwrap_or_default();
        let name = entry_name(entry);
        if !module.is_empty() && module != name {
            return module;
        }
    }
    String::new()
}

/// Build a [`SessionConfigState`] from a resolved mount plan.
///
/// Reads the plan's `providers` / `tools` / `hooks` / `agents` lists plus
/// the singular `session.context` module. Every mounted entry starts
/// enabled (it is, in fact, mounted); the app's own disable actions ride
/// on top. Pure JSON work — no amplifier import.
pub fn state_from_mount_plan(mount_plan: &Value, bundle: &str) -> SessionConfigState {
    let mut items: Vec<ConfigItem> = Vec::new();

    if let Some(Value::Object(session)) = mount_plan.get("session") {
        if let Some(Value::Object(context)) = session.get("context") {
            let module = context
                .get("module")
                .filter(|value| is_truthy(value))
                .map(py_str)
                .unwrap_or_else(|| "context".to_string());
            items.push(ConfigItem::new("context", &module, true, "session.context"));
        }
    }

    let section_map: [(ConfigCategory, &str); 5] = [
        (ConfigCategory::Context, "context"),
        (ConfigCategory::Tools, "tools"),
        (ConfigCategory::Hooks, "hooks"),
        (ConfigCategory::Providers, "providers"),
        (ConfigCategory::Agents, "agents"),
    ];
    for (category, section) in section_map {
        for entry in plan_entries(mount_plan, section) {
            let name = entry_name(entry);
            if name.is_empty() {
                continue;
            }
            items.push(ConfigItem::new(
                category.as_str(),
                &name,
                true,
                &entry_detail(entry),
            ));
        }
    }

    if let Some(Value::Object(agents)) = mount_plan.get("agents") {
        for name in agents.keys() {
            if matches!(name.as_str(), "dirs" | "include" | "inline") {
                continue;
            }
            items.push(ConfigItem::new("agents", name, true, ""));
        }
    }

    SessionConfigState::new(dedupe(items), bundle)
}

fn dedupe(items: Vec<ConfigItem>) -> Vec<ConfigItem> {
    let mut result: Vec<ConfigItem> = Vec::new();
    for item in items {
        if result
            .iter()
            .any(|seen| seen.category == item.category && seen.name == item.name)
        {
            continue;
        }
        result.push(item);
    }
    result
}

/// Representative snapshot for the offline demo runtime (DESIGN-SPEC:
/// the demo must be a faithful stand-in the UI cannot distinguish).
fn demo_items() -> Vec<ConfigItem> {
    vec![
        ConfigItem::new("context", "context-window", true, "session.context"),
        ConfigItem::new("tools", "read_file", true, "tool-filesystem"),
        ConfigItem::new("tools", "write_file", true, "tool-filesystem"),
        ConfigItem::new("tools", "bash", true, "tool-shell"),
        ConfigItem::new("tools", "load_skill", true, "tool-skills"),
        ConfigItem::new("hooks", "hooks-logging", true, "hooks-logging"),
        ConfigItem::new("hooks", "hooks-mode", true, "hooks-mode"),
        ConfigItem::new("hooks", "hooks-approval", true, "hooks-approval"),
        ConfigItem::new("providers", "anthropic", true, "provider-anthropic"),
        ConfigItem::new("agents", "general", true, ""),
        ConfigItem::new("agents", "coding", true, ""),
        ConfigItem::new("agents", "reasoning", true, ""),
    ]
}

/// A representative state for the demo / base runtime (no live session).
pub fn default_config_state(bundle: &str) -> SessionConfigState {
    SessionConfigState::new(demo_items(), bundle)
}

/// An immutable, thread-hop-safe snapshot of the config state for the UI.
///
/// The runtime lives on its own thread; the adapter marshals this frozen
/// view out rather than the mutable [`SessionConfigState`].
#[derive(Clone, Debug, PartialEq)]
pub struct ConfigSnapshotView {
    pub bundle: String,
    pub items: Vec<ConfigItem>,
    /// `(path, repr(value))` pairs in override insertion order.
    pub overrides: Vec<(String, String)>,
    pub changes: Vec<ConfigChange>,
}

impl ConfigSnapshotView {
    pub fn of(state: &SessionConfigState) -> Self {
        Self {
            bundle: state.bundle.clone(),
            items: state.items(None),
            overrides: state
                .overrides()
                .iter()
                .map(|(path, value)| (path.clone(), value.py_repr()))
                .collect(),
            changes: state.diff(),
        }
    }

    pub fn items_in(&self, category: &str) -> Vec<ConfigItem> {
        self.items
            .iter()
            .filter(|item| item.category == category)
            .cloned()
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    // -- value type inference (donor _handle_config_set) --------------------

    #[test]
    fn test_parse_value_infers_type() {
        assert_eq!(parse_value("true"), ConfigValue::Bool(true));
        assert_eq!(parse_value("TRUE"), ConfigValue::Bool(true));
        assert_eq!(parse_value("false"), ConfigValue::Bool(false));
        assert_eq!(parse_value("False"), ConfigValue::Bool(false));
        assert_eq!(parse_value("42"), ConfigValue::Int(42));
        assert_eq!(parse_value("-3"), ConfigValue::Int(-3));
        assert_eq!(parse_value("0.8"), ConfigValue::Float(0.8));
        assert_eq!(
            parse_value("claude-opus"),
            ConfigValue::Str("claude-opus".to_string())
        );
        assert_eq!(parse_value(""), ConfigValue::Str(String::new()));
    }

    // -- argument routing (donor _get_config_display) -----------------------

    #[test]
    fn test_parse_empty_is_help() {
        assert_eq!(parse_config_command("").kind, InvocationKind::Help);
        assert_eq!(parse_config_command("   ").kind, InvocationKind::Help);
    }

    #[test]
    fn test_parse_show_variants() {
        assert_eq!(parse_config_command("show").kind, InvocationKind::Show);
        let cat = parse_config_command("show tools");
        assert_eq!(
            (cat.kind, cat.category.as_str()),
            (InvocationKind::Category, "tools")
        );
        let item = parse_config_command("show tools bash");
        assert_eq!(
            (item.kind, item.category.as_str(), item.name.as_str()),
            (InvocationKind::Item, "tools", "bash")
        );
    }

    #[test]
    fn test_parse_bare_category_and_item() {
        assert_eq!(parse_config_command("hooks").kind, InvocationKind::Category);
        let item = parse_config_command("providers anthropic");
        assert_eq!(
            (item.kind, item.category.as_str(), item.name.as_str()),
            (InvocationKind::Item, "providers", "anthropic")
        );
    }

    #[test]
    fn test_parse_toggle() {
        let off = parse_config_command("tools disable bash");
        assert_eq!(
            (off.kind, off.category.as_str(), off.name.as_str(), off.enable),
            (InvocationKind::Toggle, "tools", "bash", false)
        );
        let on = parse_config_command("tools enable bash");
        assert!(on.enable);
    }

    #[test]
    fn test_parse_set_requires_path_and_value() {
        assert_eq!(
            parse_config_command("set default_model claude").kind,
            InvocationKind::Set
        );
        let err = parse_config_command("set default_model");
        assert_eq!(err.kind, InvocationKind::Error);
        assert!(err.message.contains("usage"));
    }

    #[test]
    fn test_parse_diff_and_save_scope() {
        assert_eq!(parse_config_command("diff").kind, InvocationKind::Diff);
        assert_eq!(parse_config_command("save").scope, "global");
        assert_eq!(parse_config_command("save --scope project").scope, "project");
        assert_eq!(parse_config_command("save local").scope, "local");
        assert_eq!(parse_config_command("save --scope=global").scope, "global");
    }

    #[test]
    fn test_parse_unknown_scope_and_subcommand_error() {
        assert_eq!(
            parse_config_command("save --scope bogus").kind,
            InvocationKind::Error
        );
        assert_eq!(
            parse_config_command("frobnicate").kind,
            InvocationKind::Error
        );
        assert_eq!(
            parse_config_command("show boguscat").kind,
            InvocationKind::Error
        );
    }

    // -- state: toggle / set / diff / snapshot -------------------------------

    fn state() -> SessionConfigState {
        SessionConfigState::new(
            vec![
                ConfigItem::new("tools", "bash", true, "tool-shell"),
                ConfigItem::new("tools", "read_file", true, "tool-filesystem"),
                ConfigItem::new("hooks", "hooks-mode", true, "hooks-mode"),
                ConfigItem::new("providers", "anthropic", true, "provider-anthropic"),
            ],
            "anchors",
        )
    }

    #[test]
    fn test_toggle_round_trips_and_shows_in_diff() {
        let mut state = state();
        assert_eq!(state.diff(), vec![]);
        let (ok, msg) = state.toggle("tools", "bash", false);
        assert!(ok);
        assert_eq!(msg, "\u{2713} Disabled bash");
        let item = state.find("tools", "bash").expect("bash item exists");
        assert!(!item.enabled);
        let changes = state.diff();
        assert_eq!(changes.len(), 1);
        let change = &changes[0];
        assert_eq!(
            (
                change.category.as_str(),
                change.name.as_str(),
                change.action.as_str()
            ),
            ("tools", "bash", "disabled")
        );
        // Re-enable returns to origin -> no diff.
        state.toggle("tools", "bash", true);
        assert_eq!(state.diff(), vec![]);
    }

    #[test]
    fn test_toggle_hooks_is_read_only() {
        let mut state = state();
        let (ok, msg) = state.toggle("hooks", "hooks-mode", false);
        assert!(!ok);
        assert!(msg.contains("read-only"));
        assert_eq!(state.diff(), vec![]);
    }

    #[test]
    fn test_toggle_unknown_item_refused() {
        let mut state = state();
        let (ok, msg) = state.toggle("tools", "nope", false);
        assert!(!ok);
        assert!(msg.contains("no tools item"));
    }

    #[test]
    fn test_toggle_noop_when_already_in_state() {
        let mut state = state();
        let (ok, msg) = state.toggle("tools", "bash", true);
        assert!(!ok);
        assert!(msg.contains("already enabled"));
    }

    #[test]
    fn test_set_value_round_trips_and_diffs() {
        let mut state = state();
        let (ok, msg) = state.set_value("session.reasoning_effort", "high");
        assert!(ok);
        assert_eq!(msg, "\u{2713} Set session.reasoning_effort = 'high'");
        assert_eq!(
            state.value("session.reasoning_effort"),
            Some(&ConfigValue::Str("high".to_string()))
        );
        let changes = state.diff();
        assert_eq!(changes.len(), 1);
        assert_eq!(changes[0].category, "set");
        assert_eq!(changes[0].name, "session.reasoning_effort");
    }

    #[test]
    fn test_snapshot_resets_diff_origin() {
        let mut state = state();
        state.toggle("tools", "bash", false);
        state.set_value("x", "1");
        assert_eq!(state.diff().len(), 2);
        state.snapshot(); // adopt the mutated state as the new origin
        assert_eq!(state.diff(), vec![]);
    }

    #[test]
    fn test_to_settings_serializes_disables_and_overrides() {
        let mut state = state();
        state.toggle("tools", "bash", false);
        state.set_value("session.reasoning_effort", "high");
        assert_eq!(
            state.to_settings(),
            json!({
                "disabled": {"tools": ["bash"]},
                "overrides": {"session.reasoning_effort": "high"},
            })
        );
        // Read-only hooks never land in the serialized disable set.
        assert!(state
            .to_settings()
            .get("disabled")
            .and_then(|disabled| disabled.get("hooks"))
            .is_none());
    }

    #[test]
    fn test_to_settings_empty_when_unchanged() {
        assert_eq!(state().to_settings(), json!({}));
    }

    // -- seeding from a mount plan -------------------------------------------

    #[test]
    fn test_state_from_mount_plan_reads_every_section() {
        let plan = json!({
            "session": {"context": {"module": "context-window"}},
            "providers": [{"module": "provider-anthropic", "id": "anthropic"}],
            "tools": [{"module": "tool-filesystem", "name": "read_file"}, "bash"],
            "hooks": [{"module": "hooks-mode"}],
            "agents": [{"name": "coding"}],
        });
        let state = state_from_mount_plan(&plan, "anchors");
        let names: HashSet<(String, String)> = state
            .items(None)
            .iter()
            .map(|item| (item.category.clone(), item.name.clone()))
            .collect();
        assert!(names.contains(&("context".to_string(), "context-window".to_string())));
        assert!(names.contains(&("providers".to_string(), "anthropic".to_string())));
        assert!(names.contains(&("tools".to_string(), "read_file".to_string())));
        assert!(names.contains(&("tools".to_string(), "bash".to_string())));
        assert!(names.contains(&("hooks".to_string(), "hooks-mode".to_string())));
        assert!(names.contains(&("agents".to_string(), "coding".to_string())));
        // Every category the model advertises renders in a fixed order.
        for item in state.items(None) {
            assert!(is_known_category(&item.category));
        }
    }

    #[test]
    fn test_snapshot_view_is_frozen_and_filterable() {
        let mut state = default_config_state("anchors");
        state.toggle("tools", "bash", false);
        let view = ConfigSnapshotView::of(&state);
        assert_eq!(view.bundle, "anchors");
        assert_eq!(view.changes.len(), 1);
        let tool_names: HashSet<String> = view
            .items_in("tools")
            .iter()
            .map(|item| item.name.clone())
            .collect();
        assert!(tool_names.contains("bash"));
        // Mutating the state afterwards does not mutate the captured view.
        state.toggle("tools", "read_file", false);
        assert_eq!(view.changes.len(), 1);
    }
}
