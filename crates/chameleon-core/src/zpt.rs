// Ported from Chameleon's src/chameleon/zpt/program.py (`MacroProgram.visit_element`,
// `visit_text`, `visit_comment`, `_make_content_node`, `_create_attributes_nodes`) and
// tal.py (`prepare_attributes`), modified: rewritten in Rust, covering only static
// markup, `${...}`, `tal:content`, `tal:condition` and `tal:repeat` (Phase 4 ports the rest).

//! The ZPT program builder: element tree to program tree.

use crate::entities::decode_htmlentities;
use crate::error::{CompileError, ErrorKind};
use crate::escape::{Escape, Quote};
use crate::expr::{ExprCompileError, ExprCompiler, ExprContext, ExprId};
use crate::interpolate::interpolate;
use crate::nodes::{AttrNode, Node, Part, StartTag};
use crate::parser::{Element, I18N_NS, Item, META_NS, METAL_NS, NsAttr, TAL_NS, Tag, XMLNS_NS};
use crate::source::Span;
use crate::tal::{I18N_WHITELIST, METAL_WHITELIST, TAL_WHITELIST, parse_defines, parse_substitution};

/// Namespaces whose elements lose their tags and whose attributes are not output.
const DROP_NS: [&str; 4] = [TAL_NS, METAL_NS, I18N_NS, META_NS];

/// Names a template may not assign (`COMPILER_INTERNALS_OR_DISALLOWED`).
const DISALLOWED_NAMES: [&str; 2] = ["econtext", "rcontext"];

pub(crate) struct Builder<'a, C: ExprCompiler> {
    src: &'a str,
    compiler: &'a mut C,
    /// Code points on the last line of the most recent text node (`_last`); `None` right
    /// after a `tal:repeat` on a TAL element.
    last: Option<usize>,
    /// Whitespace placed between repeated items (`_whitespace`).
    whitespace: String,
    /// How many enclosing `tal:content` default bodies alias `default` to the marker.
    default_alias: usize,
}

type Result<T, E> = std::result::Result<T, CompileError<E>>;

impl<'a, C: ExprCompiler> Builder<'a, C> {
    pub(crate) fn new(src: &'a str, compiler: &'a mut C) -> Self {
        Self { src, compiler, last: Some(0), whitespace: "\n".to_owned(), default_alias: 0 }
    }

    pub(crate) fn build(&mut self, items: &[Item]) -> Result<Node, C::Error> {
        let mut body = Vec::new();
        for item in items {
            if let Some(node) = self.visit(item)? {
                body.push(node);
            }
        }
        Ok(Node::Sequence(body))
    }

    fn cx(&self, emitted: bool) -> ExprContext {
        ExprContext { default_is_marker: self.default_alias > 0, emitted, candidate: false }
    }

    fn visit(&mut self, item: &Item) -> Result<Option<Node>, C::Error> {
        Ok(Some(match item {
            Item::Text(span) => self.visit_text(*span)?,
            Item::Comment(span) => return self.visit_comment(*span),
            Item::Default(span) => Node::Text(span.text(self.src).to_owned()),
            Item::Unsupported { kind, span } => return Err(CompileError::unsupported(kind, *span)),
            Item::StartTag(tag) => self.visit_element(tag, None, &[])?,
            Item::Element(e) => {
                let Element { start, end, children } = &**e;
                self.visit_element(start, end.as_ref(), children)?
            }
        }))
    }

    fn visit_text(&mut self, span: Span) -> Result<Node, C::Error> {
        let text = span.text(self.src);
        self.last = Some(text.rsplit('\n').next().unwrap_or_default().chars().count());
        if text.contains("${") {
            let parts = interpolate(self.src, span, self.cx(true), self.compiler)?;
            return Ok(interpolation(parts, Escape::Text));
        }
        Ok(Node::Text(text.replace("$$", "$")))
    }

    fn visit_comment(&mut self, span: Span) -> Result<Option<Node>, C::Error> {
        let text = span.text(self.src);
        if text.starts_with("<!--!") {
            return Ok(None);
        }
        if text.starts_with("<!--?") {
            return Ok(Some(Node::Text(format!("<!--{}", text.trim_start_matches(['<', '!', '-', '?'])))));
        }
        if !text.contains("${") || text.len() < 7 {
            return Ok(Some(Node::Text(text.to_owned())));
        }
        let inner = span.sub(4, span.len() - 3);
        let parts = interpolate(self.src, inner, self.cx(true), self.compiler)?;
        Ok(Some(Node::Sequence(vec![
            Node::Text("<!--".to_owned()),
            interpolation(parts, Escape::Text),
            Node::Text("-->".to_owned()),
        ])))
    }

    fn visit_element(&mut self, start: &Tag, end: Option<&Tag>, children: &[Item]) -> Result<Node, C::Error> {
        let src = self.src;
        let ns = self.ns_attrs(start)?;
        let get = |namespace: &str, name: &str| {
            ns.iter().find(|(a, _)| &*a.ns == namespace && a.name == name).map(|(a, v)| (v.as_str(), a.value))
        };

        if let Some((_, span)) = get(TAL_NS, "script") {
            return Err(CompileError::new(ErrorKind::Language, "The script attribute is unsupported.", span));
        }
        for (attr, _) in &ns {
            let supported = &*attr.ns == TAL_NS && matches!(attr.name.as_str(), "content" | "condition" | "repeat");
            if DROP_NS.contains(&&*attr.ns) && !supported {
                let prefix = attr_prefix(src, start, attr);
                return Err(CompileError::unsupported(format!("The attribute {prefix}{}", attr.name), attr.value));
            }
        }

        // Remember whitespace for item repetition.
        if let Some(n) = self.last {
            self.whitespace = format!("\n{}", " ".repeat(n));
        }
        let mut whitespace = self.whitespace.clone();

        let drop_tags = DROP_NS.contains(&&*start.namespace);

        let content = get(TAL_NS, "content")
            .map(|(clause, span)| {
                let (structure, expr) = parse_substitution(clause);
                let esc = if structure { Escape::Structure } else { Escape::Content };
                let id =
                    self.value(expr, span, ExprContext { default_is_marker: true, emitted: true, candidate: false })?;
                Ok::<_, CompileError<C::Error>>((id, esc))
            })
            .transpose()?;

        let start_tag =
            if drop_tags { None } else { Some(self.start_tag(start, &ns, content.is_some() && end.is_none())?) };
        let end_tag = match (drop_tags, end) {
            (false, Some(end)) => Some(end_tag_text(src, end)?),
            // tal:content on an empty or unclosed tag gets an explicit end tag.
            (false, None) if content.is_some() => Some(format!("</{}>", start.name.text(src))),
            _ => None,
        };

        let repeat = match get(TAL_NS, "repeat") {
            None => None,
            Some((clause, span)) => {
                let defines = parse_defines(clause).map_err(|part| {
                    CompileError::new(ErrorKind::Language, format!("Invalid define syntax: {part}"), span)
                })?;
                let [define] = defines.as_slice() else {
                    return Err(CompileError::new(ErrorKind::Language, "Invalid repeat syntax.", span));
                };
                if define.global {
                    return Err(CompileError::unsupported("A global tal:repeat", span));
                }
                if let Some(name) = define.names.iter().find(|n| DISALLOWED_NAMES.contains(&n.as_str())) {
                    return Err(CompileError::new(
                        ErrorKind::Translation,
                        format!("Name disallowed by compiler: {name}"),
                        span,
                    ));
                }
                let expr = self.value(&define.expr, span, self.cx(false))?;
                if &*start.namespace == TAL_NS {
                    self.last = None;
                    whitespace.trim_start_matches('\n').clone_into(&mut self.whitespace);
                    whitespace = String::new();
                }
                Some((define.names.clone(), expr, whitespace))
            }
        };

        let condition =
            get(TAL_NS, "condition").map(|(clause, span)| self.value(clause, span, self.cx(false))).transpose()?;

        // The body: tal:content's default (in which `default` is the marker) or the children.
        if content.is_some() {
            self.default_alias += 1;
        }
        let mut body = Vec::new();
        for child in children {
            if let Some(node) = self.visit(child)? {
                body.push(node);
            }
        }
        if content.is_some() {
            self.default_alias -= 1;
        }
        let mut inner = Node::Sequence(body);
        if let Some((expr, esc)) = content {
            inner = Node::Content { expr, esc, default: Box::new(inner) };
        }
        if let Some(start) = start_tag {
            inner = Node::Element { start, body: Box::new(inner), end: end_tag };
        }
        if let Some((names, expr, whitespace)) = repeat {
            inner = Node::Repeat { names, expr, whitespace, body: Box::new(inner) };
        }
        if let Some(expr) = condition {
            inner = Node::Condition { expr, body: Box::new(inner) };
        }
        Ok(inner)
    }

    /// The tag's namespaced attributes, validated, with TAL and METAL values entity-decoded.
    fn ns_attrs<'t>(&self, tag: &'t Tag) -> Result<Vec<(&'t NsAttr, String)>, C::Error> {
        let mut out = Vec::with_capacity(tag.ns_attrs.len());
        for attr in &tag.ns_attrs {
            let raw = attr.value.text(self.src);
            let value = if &*attr.ns == TAL_NS || &*attr.ns == METAL_NS {
                decode_htmlentities(raw)
                    .map_err(|e| CompileError::new(ErrorKind::Language, e.to_string(), attr.value))?
                    .into_owned()
            } else {
                raw.to_owned()
            };
            let whitelist = match &*attr.ns {
                TAL_NS => Some(TAL_WHITELIST),
                METAL_NS => Some(METAL_WHITELIST),
                I18N_NS => Some(I18N_WHITELIST),
                _ => None,
            };
            if let Some(list) = whitelist
                && !list.contains(&attr.name.as_str())
            {
                let message = format!("Bad attribute for namespace '{}'", attr.ns);
                return Err(CompileError::new(ErrorKind::Compilation, message, attr.value));
            }
            out.push((attr, value));
        }
        Ok(out)
    }

    /// Compiles a TAL attribute's expression; a syntax error is an `ExpressionError`.
    fn value(&mut self, expr: &str, span: Span, cx: ExprContext) -> Result<ExprId, C::Error> {
        match self.compiler.compile(expr, span, cx) {
            Ok(id) => Ok(id),
            Err(ExprCompileError::Invalid { message }) => Err(CompileError::new(ErrorKind::Expression, message, span)),
            Err(ExprCompileError::Other(e)) => Err(CompileError::Expr(e)),
        }
    }

    fn start_tag(&mut self, tag: &Tag, ns: &[(&NsAttr, String)], force_open: bool) -> Result<StartTag, C::Error> {
        let src = self.src;
        let open = format!("{}{}", tag.prefix.text(src), tag.name.text(src));
        let close = if force_open {
            ">".to_owned()
        } else {
            tag.suffix
                .ok_or_else(|| CompileError::unsupported("A tag without a closing `>`", tag.span))?
                .text(src)
                .to_owned()
        };
        let mut attrs = Vec::with_capacity(tag.attrs.len());
        // prepare_attributes pairs attributes with ns_attrs keys by position (`zip`).
        for (i, attr) in tag.attrs.iter().enumerate() {
            if let Some((ns_attr, _)) = ns.get(i) {
                let value = attr.value.text(src);
                if DROP_NS.contains(&&*ns_attr.ns) || (&*ns_attr.ns == XMLNS_NS && DROP_NS.contains(&value)) {
                    continue;
                }
            }
            let quote_text = attr.quote.text(src);
            let prefix = format!("{}{}{}{}", attr.space.text(src), attr.name.text(src), attr.eq.text(src), quote_text);
            let value = attr.value.text(src);
            if !value.contains("${") {
                attrs.push(AttrNode::Static(format!("{prefix}{value}{quote_text}")));
                continue;
            }
            let quote = match quote_text {
                "\"" => Quote::Double,
                "'" => Quote::Single,
                _ => Quote::Bare,
            };
            let cx = ExprContext { default_is_marker: true, emitted: true, candidate: false };
            let parts = interpolate(src, attr.value, cx, self.compiler)?;
            let suffix = quote_text.to_owned();
            attrs.push(match parts.as_slice() {
                [Part::Expr(expr)] => AttrNode::Single { prefix, expr: *expr, quote, suffix },
                [] => AttrNode::Static(format!("{prefix}{suffix}")),
                [Part::Text(text)] => AttrNode::Static(format!("{prefix}{text}{suffix}")),
                _ => AttrNode::Parts { prefix, parts, quote, suffix },
            });
        }
        Ok(StartTag { open, attrs, close })
    }
}

fn interpolation(parts: Vec<Part>, esc: Escape) -> Node {
    match parts.as_slice() {
        [] => Node::Text(String::new()),
        [Part::Text(text)] => Node::Text(text.clone()),
        _ => Node::Interpolation { parts, esc },
    }
}

fn end_tag_text<E>(src: &str, end: &Tag) -> Result<String, E> {
    let suffix = end.suffix.ok_or_else(|| CompileError::unsupported("An end tag without a closing `>`", end.span))?;
    Ok(format!("{}{}{}{}", end.prefix.text(src), end.name.text(src), end.space.text(src), suffix.text(src)))
}

/// The prefix an attribute was written with (`tal:`), for messages.
fn attr_prefix(src: &str, tag: &Tag, attr: &NsAttr) -> String {
    tag.attrs
        .iter()
        .map(|a| a.name.text(src))
        .find(|n| n.ends_with(&format!(":{}", attr.name)))
        .and_then(|n| n.split_once(':'))
        .map(|(p, _)| format!("{p}:"))
        .unwrap_or_default()
}
