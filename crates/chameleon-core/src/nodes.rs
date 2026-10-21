// Ported from Chameleon's src/chameleon/nodes.py, modified: a typed Rust tree. Several of
// Chameleon's generic nodes (`Cache`, `Alias`, `BinOp`, `Negate`, `Replace`, `Static`) are
// folded into the higher-level nodes that use them; each variant documents the Chameleon
// structure it stands for and what it means at render time (compiler.py).

//! The program tree built from the element tree: what a template means, before lowering to
//! bytecode.
//!
//! Expressions are kept as source text with Chameleon positions ([`Value`], [`Interp`]);
//! nothing here depends on the expression compiler. Each one has a [`ValueId`], and the
//! resolve pass ([`crate::resolve`]) compiles them in Chameleon's compile order into a table
//! indexed by that id.

use std::fmt::Write as _;

use crate::escape::{Escape, Quote};
use crate::source::Source;
use crate::tok::Tok;
use crate::vm::Visibility;

/// `tal:content` / `tal:replace` key: escaped text or `structure`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ContentKind {
    Text,
    Structure,
}

/// Whether a value is translated (`i18n:translate=""`, implicit translation).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Translation {
    Plain,
    Translated,
}

/// Index of an expression in a tree (assigned in build order).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ValueId(pub u32);

/// Index into [`Tree::static_attrs`] (one per element).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct AttrsId(pub u32);

/// Index into [`Tree::slot_lists`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct SlotsId(pub u32);

/// An expression (Chameleon's `Value(clause)`): TALES source after entity decoding, with
/// Chameleon's position.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Value {
    pub id: ValueId,
    pub source: Tok,
}

/// Text with `${...}` (Chameleon's `Interpolation(Substitution(text, char_escape))`). Where
/// each expression ends is decided by the resolve pass (the Interpolator's retry).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Interp {
    pub id: ValueId,
    /// The raw text, entities not decoded (each expression is decoded on its own).
    pub source: Tok,
    /// How each part is escaped: `Text` for text and comments in XML mode, `Structure` for
    /// CDATA and text mode, `Attr` in attributes.
    pub escape: Escape,
    /// Implicit translation (`implicit_i18n_translate`, `implicit_i18n_attributes`): the
    /// result is translated when every expression is a simple name.
    pub translate: Translation,
}

/// The program: macros defined with `metal:define-macro` (in Chameleon's dictionary order:
/// where a name was first defined, with its last body), then the main body.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Tree {
    pub macros: Vec<(Tok, Node)>,
    pub body: Node,
    /// The `attrs` dictionaries: an element's static attributes (name to value as written,
    /// or the `tal:attributes` expression source for an attribute it adds).
    pub static_attrs: Vec<Vec<(String, String)>>,
    /// The slots of each `metal:use-macro` / `extend-macro`. A list can gain entries after
    /// its element was built (Chameleon never pops an `extend-macro` element's list, so a
    /// later `fill-slot` elsewhere joins it).
    pub slot_lists: Vec<Vec<FillSlot>>,
    /// Number of [`ValueId`]s.
    pub values: u32,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Node {
    /// Static output.
    Text(String),
    /// `${...}` in text, a comment, CDATA or a processing instruction.
    Interpolation(Interp),
    #[default]
    Empty,
    Sequence(Vec<Node>),
    Element(Box<Element>),
    /// `tal:content`, `tal:replace` and the `tal:on-error` fallback (Chameleon's
    /// `_make_content_node`).
    Content(Box<Content>),
    /// Every element's `Define`: the `attrs` alias, then the `tal:define` assignments.
    Define(Box<Define>),
    /// `tal:condition`: the body if the value is true.
    Condition {
        expr: Value,
        body: Box<Node>,
    },
    Repeat(Box<Repeat>),
    /// `tal:switch` (`Cache([switch])`): evaluates the value once for the `tal:case`
    /// elements inside.
    Switch {
        expr: Value,
        body: Box<Node>,
    },
    /// `tal:case`: `Define(default -> marker, Condition(switch is not CANCEL and (value ==
    /// switch or value == marker), Cancel([switch], body)))`. At render time the case value
    /// is evaluated for `== switch` and, only if that is `False` (identity), evaluated
    /// *again* for `== default`; the condition is then a truth test. A match cancels the
    /// switch, so later cases fail. `switch` is the nearest enclosing `tal:switch` (possibly
    /// on the same element); `default` is the marker in the case value and its whole body.
    Case {
        switch: Value,
        expr: Value,
        body: Box<Node>,
    },
    /// `tal:on-error`: run `body`; on an `Exception`, roll the output back, bind `error`,
    /// call the error handler and run `fallback` instead. The fallback is compiled before
    /// the body (compiler.py `visit_OnError`).
    OnError {
        fallback: Box<Node>,
        body: Box<Node>,
    },
    /// `<?python ... ?>`: the code between the name and `?>` (statements, compiled with
    /// `ExprCompiler::compile_code`).
    CodeBlock(Value),
    /// `i18n:translate` (`msgid` empty means "use the content as the message id"), and the
    /// implicit translation of a text node (`msgid` is the normalized text, body its text).
    Translate {
        msgid: Tok,
        body: Box<Node>,
    },
    /// `i18n:name`.
    Name {
        name: Tok,
        body: Box<Node>,
    },
    /// `i18n:domain`.
    Domain {
        name: Tok,
        body: Box<Node>,
    },
    /// `i18n:context`.
    TxContext {
        name: Tok,
        body: Box<Node>,
    },
    /// `i18n:target`: `Define(default -> target_language, Target(expr, body))`; `default`
    /// is the value of `target_language` at the element in the expression and the body.
    Target {
        expr: Value,
        body: Box<Node>,
    },
    /// `metal:define-slot`: the body, unless the caller filled the slot.
    DefineSlot {
        name: Tok,
        body: Box<Node>,
    },
    /// `metal:use-macro` / `metal:extend-macro`.
    UseMacro(Box<UseMacro>),
    /// Where a `metal:define-macro` element stood: renders that macro.
    UseInternalMacro(Tok),
}

/// An element (Chameleon's `Element(Start, End, content)`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Element {
    pub start: Start,
    pub end: Option<String>,
    pub content: Node,
    /// `tal:omit-tag` with an expression (`Cache([Negate(value)], ...)`): evaluated once,
    /// before the start tag; the tags are left out if it is true.
    pub omit: Option<Value>,
}

/// A start tag (Chameleon's `Start`): `prefix name attributes suffix`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Start {
    /// `<` and the name.
    pub open: String,
    pub attrs: Vec<Attr>,
    /// `>`, `/>` or whatever followed the last attribute.
    pub close: String,
}

impl Start {
    /// The dictionary expressions, in order. They are evaluated before the start tag
    /// (Chameleon's `Cache(filtering, ...)`), so filters can refer to them.
    pub fn dicts(&self) -> impl Iterator<Item = &Value> {
        self.attrs.iter().filter_map(|a| match a {
            Attr::Dict { expr, .. } => Some(expr),
            _ => None,
        })
    }
}

/// One attribute (Chameleon's `Attribute`, possibly inside a `Define` aliasing `default`,
/// or `DictAttributes`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Attr {
    /// Output as written (`space name eq quote value quote`), unless one of the dictionaries
    /// in `filters` has the name.
    Static { name: String, text: String, filters: Vec<u32> },
    /// `prefix VALUE suffix`, with `prefix = space name eq quote` and `suffix = quote`; left
    /// out if the value converts to `None`, or a dictionary in `filters` has the name (the
    /// value is evaluated first). `default` is the attribute's static value: what the
    /// `default` marker converts to (`None` drops the attribute).
    Dynamic {
        name: String,
        prefix: String,
        suffix: String,
        value: AttrValue,
        default: Option<String>,
        filters: Vec<u32>,
    },
    /// `tal:attributes` with a dictionary expression (`DictAttributes`): every item whose
    /// name is not in `exclude` (names written after it) and whose value is not `None`, as
    /// ` name="value"`; names in `bool_names` are left out when false and take their own
    /// name as value when true.
    Dict { expr: Value, exclude: Vec<String>, bool_names: Vec<String> },
}

/// What a dynamic attribute's value is.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AttrValue {
    /// `tal:attributes="name expr"` (`Substitution`): converted and escaped for the quote.
    Subst { expr: Value, quote: Quote },
    /// `tal:attributes` on a boolean attribute (`Boolean`): `default` if the value is the
    /// marker, else the name if it is true, else dropped.
    Boolean { expr: Value, name: String },
    /// `${...}` in the written value. For a boolean attribute (`boolean`), each part is `None`
    /// when false, and a true result becomes the attribute's name (`Replace`).
    Interp { interp: Interp, boolean: bool, name: String },
    /// A static value (only inside `Translate`).
    Static(String),
    /// `i18n:attributes` or implicit attribute translation (`Translate(msgid, value)`):
    /// `translate(msgid or value, default=value)`.
    Translate { msgid: Option<String>, value: Box<AttrValue> },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Content {
    pub expr: Value,
    /// `structure` (no escaping) rather than `text`.
    pub kind: ContentKind,
    /// `i18n:translate=""` with `tal:content` / `tal:replace`: the value is translated.
    pub translate: Translation,
    /// What the `default` marker renders: the element's children (`tal:content`), the whole
    /// element (`tal:replace`), or nothing for an `on-error` fallback. When present,
    /// `default` is the marker in the expression and in the default itself, and the value
    /// is evaluated once.
    pub default: Option<Node>,
}

/// `tal:define`, and the `attrs` alias every element has.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Define {
    /// Lexically, `attrs` is this element's static attributes in the assignments and body.
    pub attrs: AttrsId,
    pub assigns: Vec<Assign>,
    pub body: Node,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Assign {
    pub names: Vec<Tok>,
    pub expr: Value,
    pub visibility: Visibility,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Repeat {
    pub names: Vec<Tok>,
    pub expr: Value,
    pub visibility: Visibility,
    /// Written between items.
    pub whitespace: String,
    pub body: Node,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UseMacro {
    /// The macro expression.
    pub expr: Value,
    pub slots: SlotsId,
    /// `metal:extend-macro`.
    pub extend: bool,
    /// `macroname`, bound locally while the macro runs (the expression's last `/` part).
    pub macroname: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FillSlot {
    pub name: Tok,
    pub body: Node,
}

/// A readable dump of a tree for snapshots: one node per line, positions as code-point
/// offsets (what Chameleon's `Token.pos` would be).
#[must_use]
pub fn dump(tree: &Tree, src: &str) -> String {
    let mut d = Dumper { out: String::new(), source: Source::new(src) };
    for (name, node) in &tree.macros {
        let line = format!("macro {}", d.tok(name));
        d.line(0, &line);
        d.node(1, node);
    }
    d.line(0, "body");
    d.node(1, &tree.body);
    for (i, attrs) in tree.static_attrs.iter().enumerate().filter(|(_, a)| !a.is_empty()) {
        let line = format!("attrs #{i} {attrs:?}");
        d.line(0, &line);
    }
    for (i, slots) in tree.slot_lists.iter().enumerate() {
        d.line(0, &format!("slots #{i}"));
        for slot in slots {
            let line = format!("fill-slot {}", d.tok(&slot.name));
            d.line(1, &line);
            d.node(2, &slot.body);
        }
    }
    d.out
}

struct Dumper<'a> {
    out: String,
    source: Source<'a>,
}

impl Dumper<'_> {
    fn line(&mut self, depth: usize, s: &str) {
        let _ = writeln!(self.out, "{}{s}", "  ".repeat(depth));
    }

    fn tok(&self, t: &Tok) -> String {
        match t.pos {
            Some(p) => format!("{:?}@{}", t.text, p.offset(&self.source)),
            None => format!("{:?}@-", t.text),
        }
    }

    fn value(&self, v: &Value) -> String {
        format!("v{} {}", v.id.0, self.tok(&v.source))
    }

    fn interp(&self, i: &Interp) -> String {
        format!(
            "v{} {:?}{} {}",
            i.id.0,
            i.escape,
            if i.translate == Translation::Translated { " translate" } else { "" },
            self.tok(&i.source)
        )
    }

    fn attr_value(&self, v: &AttrValue) -> String {
        match v {
            AttrValue::Subst { expr, quote } => format!("subst {quote:?} {}", self.value(expr)),
            AttrValue::Boolean { expr, name } => format!("boolean {name:?} {}", self.value(expr)),
            AttrValue::Interp { interp, boolean, name } => {
                format!(
                    "interp{} {}",
                    if *boolean { format!(" boolean {name:?}") } else { String::new() },
                    self.interp(interp)
                )
            }
            AttrValue::Static(s) => format!("static {s:?}"),
            AttrValue::Translate { msgid, value } => format!("translate {msgid:?} ({})", self.attr_value(value)),
        }
    }

    #[allow(clippy::too_many_lines)] // one arm per node kind
    fn node(&mut self, depth: usize, node: &Node) {
        match node {
            Node::Text(s) => self.line(depth, &format!("text {s:?}")),
            Node::Interpolation(i) => {
                let line = format!("interpolation {}", self.interp(i));
                self.line(depth, &line);
            }
            Node::Empty => self.line(depth, "empty"),
            Node::Sequence(items) => {
                // Sequences are flattened in the dump; an empty one shows as `empty`.
                if items.is_empty() {
                    self.line(depth, "empty");
                }
                for item in items {
                    self.node(depth, item);
                }
            }
            Node::Element(e) => {
                let omit = e.omit.as_ref().map(|v| format!(" omit-unless-false {}", self.value(v))).unwrap_or_default();
                self.line(depth, &format!("element {:?}{omit}", e.start.open));
                for attr in &e.start.attrs {
                    let line = match attr {
                        Attr::Static { text, filters, .. } => format!("attr {text:?}{}", filters_text(filters)),
                        Attr::Dynamic { name, prefix, suffix, value, default, filters } => format!(
                            "attr {name:?} {prefix:?}..{suffix:?} default={default:?}{} {}",
                            filters_text(filters),
                            self.attr_value(value)
                        ),
                        Attr::Dict { expr, exclude, bool_names } => {
                            format!("attr-dict exclude={exclude:?} bool={} {}", bool_names.len(), self.value(expr))
                        }
                    };
                    self.line(depth + 1, &line);
                }
                self.line(depth + 1, &format!("close {:?}", e.start.close));
                self.node(depth + 1, &e.content);
                if let Some(end) = &e.end {
                    self.line(depth, &format!("end {end:?}"));
                }
            }
            Node::Content(c) => {
                let flags = format!(
                    "{}{}",
                    if c.kind == ContentKind::Structure { " structure" } else { "" },
                    if c.translate == Translation::Translated { " translate" } else { "" }
                );
                let line = format!("content{flags} {}", self.value(&c.expr));
                self.line(depth, &line);
                if let Some(default) = &c.default {
                    self.line(depth + 1, "default");
                    self.node(depth + 2, default);
                }
            }
            Node::Define(d) => {
                if d.assigns.is_empty() {
                    self.line(depth, &format!("scope attrs=#{}", d.attrs.0));
                } else {
                    self.line(depth, &format!("define attrs=#{}", d.attrs.0));
                    for a in &d.assigns {
                        let names: Vec<_> = a.names.iter().map(|n| self.tok(n)).collect();
                        let line = format!(
                            "{} {} = {}",
                            if a.visibility == Visibility::Local { "local" } else { "global" },
                            names.join(", "),
                            self.value(&a.expr)
                        );
                        self.line(depth + 1, &line);
                    }
                }
                self.node(depth + 1, &d.body);
            }
            Node::Condition { expr, body } => {
                let line = format!("condition {}", self.value(expr));
                self.line(depth, &line);
                self.node(depth + 1, body);
            }
            Node::Repeat(r) => {
                let names: Vec<_> = r.names.iter().map(|n| self.tok(n)).collect();
                let line = format!(
                    "repeat{} {} in {} ws={:?}",
                    if r.visibility == Visibility::Local { "" } else { " global" },
                    names.join(", "),
                    self.value(&r.expr),
                    r.whitespace
                );
                self.line(depth, &line);
                self.node(depth + 1, &r.body);
            }
            Node::Switch { expr, body } => {
                let line = format!("switch {}", self.value(expr));
                self.line(depth, &line);
                self.node(depth + 1, body);
            }
            Node::Case { switch, expr, body } => {
                let line = format!("case of v{} {}", switch.id.0, self.value(expr));
                self.line(depth, &line);
                self.node(depth + 1, body);
            }
            Node::OnError { fallback, body } => {
                self.line(depth, "on-error");
                self.line(depth + 1, "fallback");
                self.node(depth + 2, fallback);
                self.line(depth + 1, "body");
                self.node(depth + 2, body);
            }
            Node::CodeBlock(code) => {
                let line = format!("code {}", self.value(code));
                self.line(depth, &line);
            }
            Node::Translate { msgid, body } => self.named(depth, "translate", msgid, body),
            Node::Name { name, body } => self.named(depth, "i18n-name", name, body),
            Node::Domain { name, body } => self.named(depth, "domain", name, body),
            Node::TxContext { name, body } => self.named(depth, "i18n-context", name, body),
            Node::DefineSlot { name, body } => self.named(depth, "define-slot", name, body),
            Node::Target { expr, body } => {
                let line = format!("target {}", self.value(expr));
                self.line(depth, &line);
                self.node(depth + 1, body);
            }
            Node::UseMacro(m) => {
                let line = format!(
                    "{} slots=#{} macroname={:?} {}",
                    if m.extend { "extend-macro" } else { "use-macro" },
                    m.slots.0,
                    m.macroname,
                    self.value(&m.expr)
                );
                self.line(depth, &line);
            }
            Node::UseInternalMacro(name) => {
                let line = format!("use-internal-macro {}", self.tok(name));
                self.line(depth, &line);
            }
        }
    }

    fn named(&mut self, depth: usize, what: &str, name: &Tok, body: &Node) {
        let line = format!("{what} {}", self.tok(name));
        self.line(depth, &line);
        self.node(depth + 1, body);
    }
}

fn filters_text(filters: &[u32]) -> String {
    if filters.is_empty() { String::new() } else { format!(" filters={filters:?}") }
}

/// Every expression (`Value`, interpolated text, `i18n:target`, code block) and every name a
/// `tal:define` / `tal:repeat` assigns, as `(kind, text, code-point position)`, each
/// expression once. The differential tests compare this multiset with the `Value` objects
/// and assignment names in Chameleon's own program tree.
#[must_use]
pub fn inventory(tree: &Tree, src: &str) -> Vec<(&'static str, String, Option<usize>)> {
    let source = Source::new(src);
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    let mut ids = Vec::new();
    let mut walk = Inventory { tree, source: &source, seen: &mut seen, out: &mut out, ids: &mut ids };
    for (_, node) in &tree.macros {
        walk.node(node);
    }
    walk.node(&tree.body);
    out
}

/// Every expression site once, in tree order: its id and source text.
pub(crate) fn value_sites(tree: &Tree, src: &str) -> Vec<(ValueId, String)> {
    let source = Source::new(src);
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    let mut ids = Vec::new();
    let mut walk = Inventory { tree, source: &source, seen: &mut seen, out: &mut out, ids: &mut ids };
    for (_, node) in &tree.macros {
        walk.node(node);
    }
    walk.node(&tree.body);
    let texts = out.into_iter().filter(|(kind, ..)| *kind == "value").map(|(_, text, _)| text);
    ids.into_iter().zip(texts).collect()
}

struct Inventory<'a, 's> {
    tree: &'a Tree,
    source: &'a Source<'s>,
    seen: &'a mut std::collections::HashSet<ValueId>,
    out: &'a mut Vec<(&'static str, String, Option<usize>)>,
    /// The id of each `value` entry of `out`, in order.
    ids: &'a mut Vec<ValueId>,
}

impl Inventory<'_, '_> {
    fn tok(&mut self, kind: &'static str, t: &Tok) {
        self.out.push((kind, t.text.clone(), t.pos.map(|p| p.offset(self.source))));
    }

    fn value(&mut self, v: &Value) {
        if self.seen.insert(v.id) {
            self.tok("value", &v.source);
            self.ids.push(v.id);
        }
    }

    fn interp(&mut self, i: &Interp) {
        if self.seen.insert(i.id) {
            self.tok("value", &i.source);
            self.ids.push(i.id);
        }
    }

    fn attr_value(&mut self, v: &AttrValue) {
        match v {
            AttrValue::Subst { expr, .. } | AttrValue::Boolean { expr, .. } => self.value(expr),
            AttrValue::Interp { interp, .. } => self.interp(interp),
            AttrValue::Static(_) => {}
            AttrValue::Translate { value, .. } => self.attr_value(value),
        }
    }

    fn node(&mut self, node: &Node) {
        match node {
            Node::Text(_) | Node::Empty | Node::UseInternalMacro(_) => {}
            Node::Interpolation(i) => self.interp(i),
            Node::Sequence(items) => items.iter().for_each(|n| self.node(n)),
            Node::Element(e) => {
                if let Some(omit) = &e.omit {
                    self.value(omit);
                }
                for attr in &e.start.attrs {
                    match attr {
                        Attr::Static { .. } => {}
                        Attr::Dynamic { value, .. } => self.attr_value(value),
                        Attr::Dict { expr, .. } => self.value(expr),
                    }
                }
                self.node(&e.content);
            }
            Node::Content(c) => {
                self.value(&c.expr);
                if let Some(d) = &c.default {
                    self.node(d);
                }
            }
            // A node can be reached twice (a slot filled in place and in a slot list): names
            // are counted once, with their assignment's expression.
            Node::Define(d) => {
                for a in &d.assigns {
                    if !self.seen.contains(&a.expr.id) {
                        a.names.iter().for_each(|n| self.tok("name", n));
                    }
                    self.value(&a.expr);
                }
                self.node(&d.body);
            }
            Node::Repeat(r) => {
                if !self.seen.contains(&r.expr.id) {
                    r.names.iter().for_each(|n| self.tok("name", n));
                }
                self.value(&r.expr);
                self.node(&r.body);
            }
            Node::Condition { expr, body } | Node::Switch { expr, body } | Node::Target { expr, body } => {
                self.value(expr);
                self.node(body);
            }
            Node::Case { switch, expr, body } => {
                self.value(switch);
                self.value(expr);
                self.node(body);
            }
            Node::OnError { fallback, body } => {
                self.node(fallback);
                self.node(body);
            }
            Node::CodeBlock(code) => self.value(code),
            Node::Translate { body, .. }
            | Node::Name { body, .. }
            | Node::Domain { body, .. }
            | Node::TxContext { body, .. }
            | Node::DefineSlot { body, .. } => self.node(body),
            Node::UseMacro(m) => {
                // Only slot lists of reachable macro calls are part of the program.
                if let Some(slots) = self.tree.slot_lists.get(m.slots.0 as usize) {
                    for slot in slots {
                        self.node(&slot.body);
                    }
                }
                self.value(&m.expr);
            }
        }
    }
}
