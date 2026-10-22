// Ported from Chameleon's src/chameleon/zpt/program.py (`MacroProgram`: `visit_element`,
// `visit_text`, `visit_comment`, `visit_cdata`, `visit_processing_instruction`,
// `visit_default`, `_check_attributes`, `_make_content_node`, `_create_attributes_nodes`,
// `_create_static_attributes`, `_maybe_trim`, `convert_data_attributes`,
// `validate_attributes`), modified: rewritten in Rust, building the typed tree of
// `crate::nodes` without compiling expressions (that is `crate::resolve`).

//! The ZPT program builder: element tree to program tree.
//!
//! Everything happens in the order Chameleon's `visit_element` does it, so the first error
//! a template contains is the one Chameleon reports: attribute validation, then the checks
//! of each TAL / METAL / i18n attribute, then the children.

use std::convert::Infallible;
use std::sync::{Arc, LazyLock};

use crate::entities::{EntityError, decode_htmlentities};
use crate::error::{CompileError, ErrorKind};
use crate::escape::{Escape, Quote};
use crate::i18n::{I18N_WHITELIST, parse_attributes as parse_i18n_attributes};
use crate::nodes::{
    Assign, Attr, AttrValue, AttrsId, Content, ContentKind, Define, Element, FillSlot, Interp, Node, Repeat, SlotsId,
    Start, Translation, Tree, UseMacro, Value, ValueId,
};
use crate::options::Options;
use crate::parser::{AttrKind, Element as ParsedElement, I18N_NS, Item, META_NS, METAL_NS, TAL_NS, Tag, XMLNS_NS};
use crate::pychars::is_space;
use crate::source::Span;
use crate::tal::{
    METAL_WHITELIST, Prepared, RawAttr, TAL_WHITELIST, content_kind, parse_attributes, parse_defines,
    parse_substitution, prepare_attributes,
};
use crate::tok::Tok;

/// Namespaces whose elements lose their tags and whose attributes are not output.
const DROP_NS: [&str; 4] = [TAL_NS, METAL_NS, I18N_NS, META_NS];

/// Errors the builder raises; it never calls the expression compiler.
pub(crate) type BuildError = CompileError<Infallible>;
type Result<T> = std::result::Result<T, BuildError>;

/// `re_trim`: `($\s+|\s+^)` (MULTILINE), with Python's `\s`.
static TRIM: LazyLock<regex::Regex> = LazyLock::new(|| crate::pyre::compile_plain(r"(?m)($\s+|\s+^)", false));

/// A namespaced attribute (one entry of `ns_attrs`): its key's local name and its value.
#[derive(Debug, Clone)]
struct NsEntry {
    ns: Arc<str>,
    name: Tok,
    value: Tok,
}

/// Builds the program tree of `items` (parsed from `src`).
pub(crate) fn build(src: &str, items: &[Item], options: &Options) -> Result<Tree> {
    let mut b = Builder {
        src,
        options,
        last: Some(0),
        whitespace: "\n".to_owned(),
        switches: Vec::new(),
        use_macro: Vec::new(),
        interpolation: vec![true],
        macros: Vec::new(),
        registrations: 0,
        tree: Tree::default(),
    };
    let mut body = Vec::new();
    for item in items {
        if let Some(node) = b.visit(item)? {
            body.push(node);
        }
    }
    let mut tree = b.tree;
    tree.body = Node::Sequence(body);
    tree.macros = b.macros.into_iter().map(|(name, node, _)| (name, node.unwrap_or_default())).collect();
    Ok(tree)
}

struct Builder<'a> {
    src: &'a str,
    options: &'a Options,
    /// Code points on the last line of the most recent text node (`_last`); `None` right
    /// after a `tal:repeat` on a TAL element.
    last: Option<usize>,
    /// Whitespace placed between repeated items (`_whitespace`).
    whitespace: String,
    /// `_switches`: each open element's `tal:switch` value.
    switches: Vec<Option<Value>>,
    /// `_use_macro`: the slot list of each open `use-macro` / `extend-macro` element.
    use_macro: Vec<SlotsId>,
    /// `_interpolation`: whether `${...}` is interpolated in text (`meta:interpolation`).
    interpolation: Vec<bool>,
    /// `_macros`, in dictionary order: the first name token, the body, and which
    /// registration the body belongs to. Chameleon assigns `_macros[name] = slot` before
    /// visiting the element's children, so the *last registration* wins even when it is
    /// nested in the element registered first; the body is filled in once the element is
    /// complete, and only by that registration.
    macros: Vec<(Tok, Option<Node>, u32)>,
    /// Registrations so far (an id for each).
    registrations: u32,
    tree: Tree,
}

/// What `_make_content_node` needs: the key, the expression and whether to translate.
struct ContentSpec {
    kind: ContentKind,
    expr: Value,
    translate: Translation,
}

/// `omit` in `visit_element`.
enum Omit {
    No,
    Always,
    Expr(Value),
}

impl Builder<'_> {
    fn value(&mut self, source: Tok) -> Value {
        Value { id: self.next_id(), source }
    }

    fn next_id(&mut self) -> ValueId {
        let id = ValueId(self.tree.values);
        self.tree.values += 1;
        id
    }

    fn interp(&mut self, source: Tok, escape: Escape, translate: Translation) -> Interp {
        Interp { id: self.next_id(), source, escape, translate }
    }

    fn tok(&self, span: Span) -> Tok {
        Tok::span(self.src, span)
    }

    /// Characters on the last line of `text`, for `_last`.
    fn remember_last(&mut self, text: &str) {
        self.last = Some(text.rsplit('\n').next().unwrap_or_default().chars().count());
    }

    fn visit(&mut self, item: &Item) -> Result<Option<Node>> {
        // Recursive over nested elements: a stack budget, not an overflow (`crate::stack`).
        let _deep = crate::stack::enter()?;
        Ok(Some(match item {
            Item::Text(span) => self.visit_text(self.tok(*span)),
            Item::Comment(span) => return Ok(self.visit_comment(*span)),
            Item::Default(span) => Node::Text(span.text(self.src).to_owned()),
            Item::Cdata(span) => {
                let text = span.text(self.src);
                if !self.interpolating() || !text.contains("${") {
                    Node::Text(text.to_owned())
                } else {
                    Node::Interpolation(self.interp(self.tok(*span), Escape::Structure, Translation::Plain))
                }
            }
            Item::ProcessingInstruction { name, text, .. } => {
                let name = name.text(self.src);
                if name == "python" {
                    Node::CodeBlock(self.value(self.tok(*text)))
                } else {
                    // `'<?' + name + text + '?>'` is a plain `str`: no position.
                    self.visit_text(Tok::plain(format!("<?{name}{}?>", text.text(self.src))))
                }
            }
            Item::StartTag(tag) => self.visit_element(tag, None, &[])?,
            Item::Element(e) => {
                let ParsedElement { start, end, children } = &**e;
                self.visit_element(start, end.as_ref(), children)?
            }
        }))
    }

    fn interpolating(&self) -> bool {
        self.interpolation.last().copied().unwrap_or(true)
    }

    fn text_escape(&self) -> Escape {
        if self.options.escape { Escape::Text } else { Escape::Structure }
    }

    fn visit_text(&mut self, text: Tok) -> Node {
        self.remember_last(&text.text);
        let translate = if self.options.implicit_i18n_translate { Translation::Translated } else { Translation::Plain };
        if self.interpolating() && text.text.contains("${") {
            return Node::Interpolation(self.interp(text, self.text_escape(), translate));
        }
        let text = text.text.replace("$$", "$");
        if translate == Translation::Plain {
            return Node::Text(text);
        }
        // `re.search(r'(\s*)(.*\S)(\s*)', node, flags=re.DOTALL)`.
        let trimmed = text.trim_matches(is_space);
        if trimmed.is_empty() {
            return Node::Text(text);
        }
        let start = text.len() - text.trim_start_matches(is_space).len();
        let prefix = &text[..start];
        let suffix = &text[start + trimmed.len()..];
        let normalized = trimmed.split(is_space).filter(|s| !s.is_empty()).collect::<Vec<_>>().join(" ");
        Node::Sequence(vec![
            Node::Text(prefix.to_owned()),
            Node::Translate { msgid: Tok::plain(normalized.clone()), body: Box::new(Node::Text(normalized)) },
            Node::Text(suffix.to_owned()),
        ])
    }

    fn visit_comment(&mut self, span: Span) -> Option<Node> {
        let text = span.text(self.src);
        if text.starts_with("<!--!") {
            return None;
        }
        if !self.options.enable_comment_interpolation {
            return Some(Node::Text(text.to_owned()));
        }
        if text.starts_with("<!--?") {
            return Some(Node::Text(format!("<!--{}", text.trim_start_matches(['<', '!', '-', '?']))));
        }
        if !self.interpolating() || !text.contains("${") {
            return Some(Node::Text(text.to_owned()));
        }
        // `node[4:-3]` (a comment token is at least `<!---->`).
        let inner = self.tok(span.sub(4, span.len().saturating_sub(3).max(4)));
        let escape = self.text_escape();
        Some(Node::Sequence(vec![
            Node::Text(text[..4].to_owned()),
            Node::Interpolation(self.interp(inner, escape, Translation::Plain)),
            Node::Text(text[text.len().saturating_sub(3).max(4)..].to_owned()),
        ]))
    }

    /// `_maybe_trim`. Chameleon applies the regex to whatever it gets; for a tag part that is
    /// `None` that is a `TypeError`.
    fn trim(&self, s: Option<&str>, token: Span) -> Result<String> {
        if !self.options.trim_attribute_space {
            return Ok(s.unwrap_or_default().to_owned());
        }
        let Some(s) = s else {
            return Err(CompileError::new(
                ErrorKind::Type,
                "expected string or bytes-like object, got 'NoneType'",
                token,
            ));
        };
        Ok(TRIM.replace_all(s, " ").into_owned())
    }

    fn opt_text(&self, span: Option<Span>) -> Option<&str> {
        span.map(|s| s.text(self.src))
    }

    /// The namespaced attributes (`ns_attrs`) and the attribute list, as Chameleon sees them
    /// after `convert_data_attributes` and entity decoding.
    fn attributes(&self, start: &Tag) -> Result<(Vec<NsEntry>, Vec<RawAttr>)> {
        let value_of = |kind: AttrKind, span: Span| {
            if kind == AttrKind::Valueless { Tok::plain("") } else { self.tok(span) }
        };
        let mut ns: Vec<NsEntry> = start
            .ns_attrs
            .iter()
            .map(|a| NsEntry { ns: a.ns.clone(), name: self.tok(a.name_span), value: value_of(a.kind, a.value) })
            .collect();
        let mut attrs: Vec<RawAttr> = start
            .attrs
            .iter()
            .map(|a| RawAttr {
                name: self.tok(a.name),
                value: value_of(a.kind, a.value),
                quote: if a.kind == AttrKind::Quoted { a.quote.text(self.src).to_owned() } else { String::new() },
                space: a.space.text(self.src).to_owned(),
                eq: if a.kind == AttrKind::Valueless { String::new() } else { a.eq.text(self.src).to_owned() },
            })
            .collect();
        if self.options.enable_data_attributes {
            // `convert_data_attributes`: `data-tal-content` is `tal:content`, and so on.
            let mut kept = Vec::with_capacity(attrs.len());
            for attr in attrs {
                let Some(rest) = attr.name.text.strip_prefix("data-") else {
                    kept.push(attr);
                    continue;
                };
                if !rest.contains('-') {
                    kept.push(attr);
                    continue;
                }
                let after = attr.name.slice_from(5);
                let Some((prefix, _)) = after.text.split_once('-') else {
                    kept.push(attr);
                    continue;
                };
                // `name.split('-', 1)`: the second part's position skips the prefix but not
                // the separator.
                let prefix = prefix.to_owned();
                let name = Tok {
                    text: after.text[prefix.len() + 1..].to_owned(),
                    pos: after.pos.map(|q| q.plus(prefix.chars().count())),
                };
                let uri = start
                    .ns_map
                    .get(Some(&prefix))
                    .ok_or_else(|| CompileError::new(ErrorKind::Key, prefix.clone(), Tok::plain(prefix.clone())))?;
                match ns.iter_mut().find(|e| e.ns == uri.uri && e.name.text == name.text) {
                    Some(entry) => entry.value = attr.value.clone(),
                    None => ns.push(NsEntry { ns: uri.uri.clone(), name, value: attr.value.clone() }),
                }
            }
            attrs = kept;
        }
        for entry in &mut ns {
            if &*entry.ns == TAL_NS || &*entry.ns == METAL_NS {
                let decoded = decode_htmlentities(&entry.value.text, self.options.surrogate_base)
                    .map_err(|e| entity_error(e, &entry.value))?;
                if let std::borrow::Cow::Owned(decoded) = decoded {
                    entry.value = entry.value.with_text(decoded);
                }
            }
        }
        for (namespace, whitelist) in [(TAL_NS, TAL_WHITELIST), (METAL_NS, METAL_WHITELIST), (I18N_NS, I18N_WHITELIST)]
        {
            if let Some(bad) = ns.iter().find(|e| &*e.ns == namespace && !whitelist.contains(&e.name.text.as_str())) {
                let message = format!("Bad attribute for namespace '{namespace}'");
                return Err(CompileError::new(ErrorKind::Compilation, message, &bad.name));
            }
        }
        Ok((ns, attrs))
    }

    #[allow(clippy::too_many_lines)] // `visit_element` is one long sequence of steps
    fn visit_element(&mut self, start: &Tag, end: Option<&Tag>, children: &[Item]) -> Result<Node> {
        let _deep = crate::stack::enter()?;
        let (ns, attrs) = self.attributes(start)?;
        let get = |namespace: &str, name: &str| -> Option<Tok> {
            ns.iter().find(|e| &*e.ns == namespace && e.name.text == name).map(|e| e.value.clone())
        };
        let truthy = |t: &Option<Tok>| t.as_ref().is_some_and(|t| !t.is_empty());
        let namespace = &*start.namespace.uri;
        let drop_tags = DROP_NS.contains(&namespace);

        // `_check_attributes`.
        if drop_tags && let Some(clause) = get(TAL_NS, "attributes").filter(|c| !c.is_empty()) {
            let message = format!("Dynamic attributes not allowed on elements of the namespace: {namespace}.");
            return Err(CompileError::new(ErrorKind::Language, message, clause));
        }
        if let Some(script) = get(TAL_NS, "script") {
            return Err(CompileError::new(ErrorKind::Language, "The script attribute is unsupported.", script));
        }
        let tal_content = get(TAL_NS, "content");
        if truthy(&tal_content) && truthy(&get(TAL_NS, "replace")) {
            let message = "You cannot use tal:content and tal:replace at the same time.";
            return Err(CompileError::new(ErrorKind::Language, message, tal_content.unwrap_or_else(|| Tok::plain(""))));
        }
        if truthy(&tal_content) && truthy(&get(I18N_NS, "translate")) {
            let message = "You cannot use tal:content with non-trivial i18n:translate.";
            return Err(CompileError::new(ErrorKind::Language, message, tal_content.unwrap_or_else(|| Tok::plain(""))));
        }

        // Remember whitespace for item repetition.
        if let Some(n) = self.last {
            self.whitespace = format!("\n{}", " ".repeat(n));
        }
        let mut whitespace = self.whitespace.clone();

        let switch = get(TAL_NS, "switch").map(|clause| self.value(clause));
        self.switches.push(switch.clone());

        let translate_empty = if get(I18N_NS, "translate").is_some_and(|t| t.is_empty()) {
            Translation::Translated
        } else {
            Translation::Plain
        };
        let use_macro = get(METAL_NS, "use-macro").filter(|t| !t.is_empty());
        let extend_macro = get(METAL_NS, "extend-macro").filter(|t| !t.is_empty());

        // The element itself, before the wrappers: either a macro call or the tag with its
        // content. The body (`Sequence(body)`) is filled in after the children are visited.
        let mut omit = Omit::No;
        let mut static_attrs: Option<Vec<(String, String)>> = None;
        let mut prepared_attrs: Vec<Attr> = Vec::new();
        let inner: Shape;
        if let Some(clause) = use_macro.clone().or_else(|| extend_macro.clone()) {
            omit = Omit::Always;
            let slots = SlotsId(u32::try_from(self.tree.slot_lists.len()).unwrap_or(u32::MAX));
            self.tree.slot_lists.push(Vec::new());
            self.use_macro.push(slots);
            let macroname = clause.text.rsplit('/').next().unwrap_or_default().to_owned();
            let expr = self.value(clause);
            inner = Shape::Macro(UseMacro { expr, slots, extend: use_macro.is_none(), macroname });
        } else {
            let mut start_close = start.suffix;
            let mut end_text = end.map(|e| (e.prefix, e.name, e.space, e.suffix));
            let content = match tal_content.clone() {
                Some(clause) => {
                    let (key, expr) = parse_substitution(&clause);
                    let spec = ContentSpec {
                        kind: content_kind(key.as_ref()),
                        expr: self.value(expr),
                        translate: translate_empty,
                    };
                    if end.is_none() {
                        start_close = None;
                        end_text = None;
                    }
                    Some(spec)
                }
                None => None,
            };
            let forced = content.is_some() && end.is_none();

            let translate = get(I18N_NS, "translate").filter(|_| {
                let dynamic = truthy(&get(TAL_NS, "content")) || truthy(&get(TAL_NS, "replace"));
                !dynamic
            });

            let tal_attributes = match get(TAL_NS, "attributes") {
                Some(clause) => parse_attributes(&clause).map_err(|part| {
                    CompileError::new(ErrorKind::Language, "Duplicate attribute name in attributes.", part)
                })?,
                None => Vec::new(),
            };
            let i18n_attributes = match get(I18N_NS, "attributes") {
                Some(clause) => parse_i18n_attributes(&clause)
                    .map_err(|(message, token)| CompileError::new(ErrorKind::Compilation, message, token))?,
                None => Vec::new(),
            };

            // `prepare_attributes` pairs the attributes with the namespaced keys by position.
            let drop: Vec<String> = attrs
                .iter()
                .zip(&ns)
                .filter(|(attr, entry)| {
                    DROP_NS.contains(&&*entry.ns)
                        || (&*entry.ns == XMLNS_NS && DROP_NS.contains(&attr.value.text.as_str()))
                })
                .map(|(attr, _)| attr.name.text.clone())
                .collect();
            let prepared = prepare_attributes(&attrs, tal_attributes, &i18n_attributes, &drop);
            static_attrs = static_attributes(&prepared);
            prepared_attrs = self.attribute_nodes(&prepared, &i18n_attributes)?;

            let open =
                format!("{}{}", self.trim(Some(start.prefix.text(self.src)), start.span)?, start.name.text(self.src));
            let close = if forced { ">".to_owned() } else { self.trim(self.opt_text(start_close), start.span)? };
            let end_tag = match (forced, end_text) {
                (true, _) => Some(format!("</{}>", start.name.text(self.src))),
                (false, Some((prefix, name, space, suffix))) => {
                    let end_span = end.map_or(start.span, |e| e.span);
                    Some(format!(
                        "{}{}{}{}",
                        self.trim(Some(prefix.text(self.src)), end_span)?,
                        name.text(self.src),
                        self.opt_text(space).unwrap_or_default(),
                        self.trim(self.opt_text(suffix), end_span)?
                    ))
                }
                (false, None) => None,
            };

            if let Some(clause) = get(TAL_NS, "omit-tag") {
                let clause = clause.strip();
                omit = if clause.is_empty() { Omit::Always } else { Omit::Expr(self.value(clause)) };
            }

            let replace = match get(TAL_NS, "replace") {
                Some(clause) => {
                    let (key, expr) = parse_substitution(&clause);
                    Some(ContentSpec {
                        kind: content_kind(key.as_ref()),
                        expr: self.value(expr),
                        translate: translate_empty,
                    })
                }
                None => None,
            };
            inner = Shape::Tag {
                start: Start { open, attrs: prepared_attrs.clone(), close },
                end: end_tag,
                content,
                translate,
                replace,
                keep_tags: !matches!(omit, Omit::Always) && !drop_tags,
            };
        }

        let define_slot = get(METAL_NS, "define-slot");

        let assigns = match get(TAL_NS, "define") {
            Some(clause) => parse_defines(&clause)
                .map_err(|part| CompileError::new(ErrorKind::Language, "Invalid define syntax", part))?
                .into_iter()
                .map(|d| Assign { names: d.names, visibility: d.visibility, expr: self.value(d.expr) })
                .collect(),
            None => Vec::new(),
        };
        let attrs_id = self.static_attrs_id(static_attrs);

        // Chameleon's search loop rebinds `switch`, so an element with `tal:case` and no
        // switch of its own also gets `Cache([parent switch])`; that cache is a no-op (the
        // value is cached already, or the case fails its assertion first), so it is left out.
        let case = match get(TAL_NS, "case") {
            Some(clause) => {
                let Some(switch) = self.switches.iter().rev().find_map(Clone::clone) else {
                    return Err(CompileError::new(
                        ErrorKind::Language,
                        "Must define switch on a parent element.",
                        clause,
                    ));
                };
                Some((switch, self.value(clause)))
            }
            None => None,
        };

        let repeat = match get(TAL_NS, "repeat") {
            Some(clause) => {
                let mut defines = parse_defines(&clause)
                    .map_err(|part| CompileError::new(ErrorKind::Language, "Invalid define syntax", part))?;
                if defines.len() != 1 {
                    return Err(CompileError::new(ErrorKind::Assertion, "", Tok::plain("")));
                }
                let define = defines.remove(0);
                let expr = self.value(define.expr);
                if namespace == TAL_NS {
                    self.last = None;
                    whitespace.trim_start_matches('\n').clone_into(&mut self.whitespace);
                    whitespace = String::new();
                }
                Some(Repeat { names: define.names, expr, visibility: define.visibility, whitespace, body: Node::Empty })
            }
            None => None,
        };

        let condition = get(TAL_NS, "condition").map(|clause| self.value(clause));
        let domain = get(I18N_NS, "domain");
        let tx_context = get(I18N_NS, "context");
        // `TARGET` is a lambda over `clause`, which the `i18n:name` lookup below reassigns
        // before `wrap()` calls it: with both attributes the target expression is the
        // `i18n:name` value (verified against Chameleon).
        let name_clause = get(I18N_NS, "name");
        let target = get(I18N_NS, "target").map(|clause| self.value(name_clause.clone().unwrap_or(clause)));
        let name = name_clause.filter(|c| !c.strip().is_empty());

        // metal:fill-slot: registered now, its body filled in once the element is complete.
        let mut fill_slot_at = None;
        let fill_slot = get(METAL_NS, "fill-slot");
        if let Some(clause) = &fill_slot {
            if clause.strip().is_empty() {
                let message = "Must provide a non-trivial string for metal:fill-slot.";
                return Err(CompileError::new(ErrorKind::Language, message, clause));
            }
            let back = 1 + usize::from(use_macro.is_some() || extend_macro.is_some());
            let Some(&slots) = self.use_macro.len().checked_sub(back).and_then(|i| self.use_macro.get(i)) else {
                let message = "Cannot use metal:fill-slot without metal:use-macro.";
                return Err(CompileError::new(ErrorKind::Language, message, clause));
            };
            let Some(list) = self.tree.slot_lists.get_mut(slots.0 as usize) else {
                return Err(CompileError::new(ErrorKind::Unsupported, "internal: slot list missing", clause));
            };
            fill_slot_at = Some((slots, list.len()));
            list.push(FillSlot { name: clause.clone(), body: Node::Empty });
        }

        let mut macro_at = None;
        if let Some(clause) = get(METAL_NS, "define-macro") {
            if fill_slot.is_some() {
                let message = "Can't have 'fill-slot' and 'define-macro' on the same element.";
                return Err(CompileError::new(ErrorKind::Language, message, clause));
            }
            self.registrations += 1;
            let id = self.registrations;
            let at = if let Some(i) = self.macros.iter().position(|(n, _, _)| n.text == clause.text) {
                self.macros[i].2 = id;
                i
            } else {
                self.macros.push((clause.clone(), None, id));
                self.macros.len() - 1
            };
            macro_at = Some((at, id, clause));
        }

        let on_error = match get(TAL_NS, "on-error") {
            Some(clause) => {
                let (key, expr) = parse_substitution(&clause);
                let expr = self.value(expr);
                let content = Node::Content(Box::new(Content {
                    expr,
                    kind: content_kind(key.as_ref()),
                    translate: translate_empty,
                    default: None,
                }));
                let fallback = match &inner {
                    Shape::Tag { start: tag, end: end_tag, keep_tags, .. }
                        if *keep_tags && matches!(omit, Omit::No) =>
                    {
                        let attrs =
                            prepared_attrs.iter().filter(|a| matches!(a, Attr::Static { .. })).cloned().collect();
                        let (close, end_tag) = match end_tag {
                            Some(e) => (tag.close.clone(), e.clone()),
                            None => (">".to_owned(), format!("</{}>", start.name.text(self.src))),
                        };
                        Node::Element(Box::new(Element {
                            start: Start { open: tag.open.clone(), attrs, close },
                            end: Some(end_tag),
                            content,
                            omit: None,
                        }))
                    }
                    _ => content,
                };
                Some(fallback)
            }
            None => None,
        };

        let interpolation = match get(META_NS, "interpolation") {
            Some(c) if c.text == "false" || c.text == "off" => false,
            Some(c) if c.text == "true" || c.text == "on" => true,
            None => self.interpolating(),
            Some(c) => return Err(CompileError::new(ErrorKind::Language, "Bad interpolation setting.", c)),
        };
        self.interpolation.push(interpolation);

        let mut body = Vec::with_capacity(children.len());
        for child in children {
            if let Some(node) = self.visit(child)? {
                body.push(node);
            }
        }

        self.switches.pop();
        self.interpolation.pop();
        if use_macro.is_some() {
            // Chameleon pops only for `use-macro`: an `extend-macro` element's slot list
            // stays on the stack.
            self.use_macro.pop();
        }

        // Assemble, innermost first, in `wrap` order.
        let mut node = match inner {
            Shape::Macro(m) => Node::UseMacro(Box::new(m)),
            Shape::Tag { start, end, content, translate, replace, keep_tags } => {
                let mut content_node = Node::Sequence(body);
                if let Some(spec) = content {
                    content_node = content_node_of(spec, Some(content_node));
                }
                if let Some(clause) = translate {
                    content_node = Node::Translate { msgid: clause, body: Box::new(content_node) };
                }
                let mut node = if keep_tags {
                    let omit = if let Omit::Expr(v) = omit { Some(v) } else { None };
                    Node::Element(Box::new(Element { start, end, content: content_node, omit }))
                } else {
                    content_node
                };
                if let Some(spec) = replace {
                    node = content_node_of(spec, Some(node));
                }
                node
            }
        };
        if let Some(target) = target {
            node = Node::Target { expr: target, body: Box::new(node) };
        }
        if let Some(name) = tx_context {
            node = Node::TxContext { name, body: Box::new(node) };
        }
        if let Some(name) = domain {
            node = Node::Domain { name, body: Box::new(node) };
        }
        if let Some(expr) = switch {
            node = Node::Switch { expr, body: Box::new(node) };
        }
        if let Some(mut r) = repeat {
            r.body = node;
            node = Node::Repeat(Box::new(r));
        }
        if let Some(expr) = condition {
            node = Node::Condition { expr, body: Box::new(node) };
        }
        if let Some((switch, expr)) = case {
            node = Node::Case { switch, expr, body: Box::new(node) };
        }
        node = Node::Define(Box::new(Define { attrs: attrs_id, assigns, body: node }));
        if let Some(name) = define_slot {
            node = Node::DefineSlot { name, body: Box::new(node) };
        }
        if let Some((slots, i)) = fill_slot_at
            && let Some(slot) = self.tree.slot_lists.get_mut(slots.0 as usize).and_then(|l| l.get_mut(i))
        {
            slot.body = node.clone();
        }
        if let Some((at, id, clause)) = macro_at {
            if let Some(entry) = self.macros.get_mut(at)
                && entry.2 == id
            {
                entry.1 = Some(node);
            }
            node = Node::UseInternalMacro(clause);
        }
        if let Some(name) = name {
            node = Node::Name { name, body: Box::new(node) };
        }
        if let Some(fallback) = on_error {
            node = Node::OnError { fallback: Box::new(fallback), body: Box::new(node) };
        }
        Ok(node)
    }

    /// Every element gets its own `attrs` dictionary, even an empty one (Chameleon's
    /// `Static` is one object per element: verified that two elements never share it).
    fn static_attrs_id(&mut self, attrs: Option<Vec<(String, String)>>) -> AttrsId {
        self.tree.static_attrs.push(attrs.unwrap_or_default());
        AttrsId(u32::try_from(self.tree.static_attrs.len() - 1).unwrap_or(u32::MAX))
    }

    /// `_create_attributes_nodes`.
    fn attribute_nodes(&mut self, prepared: &[Prepared], i18n: &[(Tok, Option<Tok>)]) -> Result<Vec<Attr>> {
        let mut out = Vec::with_capacity(prepared.len());
        // Each attribute is filtered by the dictionaries that come after it.
        let dict_positions: Vec<usize> =
            prepared.iter().enumerate().filter(|(_, p)| p.name.is_none()).map(|(i, _)| i).collect();
        let filters_after = |i: usize| -> Vec<u32> {
            dict_positions
                .iter()
                .enumerate()
                .filter(|&(_, &pos)| pos > i)
                .map(|(n, _)| u32::try_from(n).unwrap_or(u32::MAX))
                .collect()
        };
        for (i, p) in prepared.iter().enumerate() {
            let name = p.name.as_ref().map(|n| n.text.clone());
            let implicit = name.as_deref().is_some_and(|n| self.options.is_implicit_i18n(n));
            let quote = match p.quote.as_str() {
                "\"" => Quote::Double,
                "'" => Quote::Single,
                _ => Quote::Bare,
            };
            let mut msgid: Option<Option<String>> = name
                .as_deref()
                .and_then(|n| i18n.iter().find(|(a, _)| a.text == n))
                .map(|(_, m)| m.as_ref().map(|m| m.text.clone()));
            let boolean = name.as_deref().is_some_and(|n| self.options.is_boolean(n));
            let (value, default) = match (&p.expr, &p.text) {
                (None, Some(text)) if text.text.contains("${") => {
                    let translate = if implicit && msgid.is_none() && !boolean {
                        Translation::Translated
                    } else {
                        Translation::Plain
                    };
                    let interp = self.interp(text.clone(), Escape::Attr(quote), translate);
                    (AttrValue::Interp { interp, boolean, name: name.clone().unwrap_or_default() }, None)
                }
                (Some(expr), text) => {
                    let default = text.as_ref().map(|t| t.text.clone());
                    match &name {
                        None => {
                            let decoded = decoded(expr, self.options.surrogate_base)?;
                            let exclude =
                                prepared[i..].iter().filter_map(|q| q.name.as_ref().map(|n| n.text.clone())).collect();
                            out.push(Attr::Dict {
                                expr: self.value(decoded),
                                exclude,
                                bool_names: self.options.boolean_attributes.clone(),
                            });
                            continue;
                        }
                        Some(n) if boolean => {
                            (AttrValue::Boolean { expr: self.value(expr.clone()), name: n.clone() }, default)
                        }
                        Some(_) => {
                            let decoded = decoded(expr, self.options.surrogate_base)?;
                            (AttrValue::Subst { expr: self.value(decoded), quote }, default)
                        }
                    }
                }
                (None, text) => {
                    let text = text.as_ref().map(|t| t.text.clone()).unwrap_or_default();
                    if msgid.is_none() && implicit {
                        msgid = Some(Some(text.clone()));
                    }
                    (AttrValue::Static(text.clone()), Some(text))
                }
            };
            let Some(name) = name else { continue };
            let value = match msgid {
                Some(msgid) => AttrValue::Translate { msgid, value: Box::new(value) },
                None => value,
            };
            let space = self.trim(Some(&p.space), Span::default())?;
            let prefix = format!("{space}{name}{}{}", p.eq, p.quote);
            let filters = filters_after(i);
            out.push(match value {
                AttrValue::Static(text) => Attr::Static { text: format!("{prefix}{text}{}", p.quote), name, filters },
                value => Attr::Dynamic { name, prefix, suffix: p.quote.clone(), value, default, filters },
            });
        }
        Ok(out)
    }
}

/// `decode_htmlentities(expr)` (a second decoding: TAL values are decoded once already).
fn decoded(expr: &Tok, surrogate_base: Option<u32>) -> Result<Tok> {
    let decoded = decode_htmlentities(&expr.text, surrogate_base).map_err(|e| entity_error(e, expr))?;
    Ok(expr.with_text(decoded.into_owned()))
}

/// The element before its wrappers.
enum Shape {
    Macro(UseMacro),
    Tag {
        start: Start,
        end: Option<String>,
        content: Option<ContentSpec>,
        /// `i18n:translate` without `tal:content` / `tal:replace`.
        translate: Option<Tok>,
        replace: Option<ContentSpec>,
        /// `omit is not True and namespace not in DROP_NS`.
        keep_tags: bool,
    },
}

fn content_node_of(spec: ContentSpec, default: Option<Node>) -> Node {
    Node::Content(Box::new(Content { expr: spec.expr, kind: spec.kind, translate: spec.translate, default }))
}

/// `_create_static_attributes`: name to text (or the `tal:attributes` expression).
fn static_attributes(prepared: &[Prepared]) -> Option<Vec<(String, String)>> {
    let mut attrs: Vec<(String, String)> = Vec::new();
    for p in prepared {
        let Some(name) = &p.name else { continue };
        let value = p.text.as_ref().or(p.expr.as_ref()).map(|t| t.text.clone()).unwrap_or_default();
        match attrs.iter_mut().find(|(n, _)| *n == name.text) {
            Some(entry) => entry.1 = value,
            None => attrs.push((name.text.clone(), value)),
        }
    }
    (!attrs.is_empty()).then_some(attrs)
}

fn entity_error(e: EntityError, token: &Tok) -> BuildError {
    if e.surrogate {
        CompileError::unsupported(format!("A lone surrogate from an entity ({})", e.message), token)
    } else {
        CompileError::new(ErrorKind::Value, e.message, Tok::plain(""))
    }
}
