// Ported from Chameleon's src/chameleon/parser.py (`identify`, `match_tag`, `parse_tag`,
// `update_namespace`, `unpack_attributes`, `ElementParser`), modified: rewritten in Rust,
// with the tag and attribute regexes matched by hand (same preference order and
// backtracking). Phase 3 completes declarations, CDATA and processing instructions.

//! Turns tokens into an element tree, resolving namespaces like Chameleon's `ElementParser`.

use std::rc::Rc;

use crate::pychars::{is_digit, is_space, run_len};
use crate::source::Span;

pub(crate) const XML_NS: &str = "http://www.w3.org/XML/1998/namespace";
pub(crate) const XMLNS_NS: &str = "http://www.w3.org/2000/xmlns/";
pub(crate) const TAL_NS: &str = "http://xml.zope.org/namespaces/tal";
pub(crate) const METAL_NS: &str = "http://xml.zope.org/namespaces/metal";
pub(crate) const I18N_NS: &str = "http://xml.zope.org/namespaces/i18n";
pub(crate) const META_NS: &str = "http://xml.zope.org/namespaces/meta";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub(crate) struct ParseError {
    pub message: String,
    pub span: Span,
}

impl ParseError {
    fn new(message: impl Into<String>, span: Span) -> Self {
        Self { message: message.into(), span }
    }
}

/// One attribute as written in a tag. Every part is a span of the source; `quote` is empty
/// for unquoted and valueless attributes, `eq` is empty for valueless ones.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Attr {
    pub space: Span,
    pub name: Span,
    pub eq: Span,
    pub quote: Span,
    pub value: Span,
}

/// An attribute resolved to its namespace (`ns_attrs` in Chameleon).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct NsAttr {
    pub ns: Rc<str>,
    pub name: String,
    pub value: Span,
}

/// A start, end or empty tag.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Tag {
    pub span: Span,
    /// `<` or `</`.
    pub prefix: Span,
    pub name: Span,
    pub attrs: Vec<Attr>,
    /// The rest of the tag after the name or the last attribute (`>`, ` />`, ...). `None`
    /// when Chameleon's regex leaves it unset (malformed tags).
    pub suffix: Option<Span>,
    /// Whitespace between the name and `/?>` when there are no attributes. Chameleon emits
    /// it *and* the suffix (which includes it) for end tags, so `</p >` renders `</p  >`.
    pub space: Span,
    /// The element's namespace URI.
    pub namespace: Rc<str>,
    pub ns_attrs: Vec<NsAttr>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Item {
    Text(Span),
    Comment(Span),
    /// A token Chameleon could not classify (for example a lone `<`); emitted verbatim.
    Default(Span),
    /// Markup the skeleton does not handle yet (declarations, CDATA, processing instructions).
    Unsupported {
        kind: &'static str,
        span: Span,
    },
    /// A start tag that was never closed: an element with no end tag and no children.
    StartTag(Box<Tag>),
    Element(Box<Element>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Element {
    pub start: Tag,
    pub end: Option<Tag>,
    pub children: Vec<Item>,
}

/// Prefix to namespace URI. Cloned per start tag, as Chameleon copies its dict.
#[derive(Debug, Clone)]
struct Namespaces(Vec<(Option<String>, Rc<str>)>);

impl Namespaces {
    fn get(&self, prefix: Option<&str>) -> Option<&Rc<str>> {
        self.0.iter().rev().find(|(p, _)| p.as_deref() == prefix).map(|(_, ns)| ns)
    }

    fn set(&mut self, prefix: Option<&str>, ns: &str) {
        self.0.retain(|(p, _)| p.as_deref() != prefix);
        self.0.push((prefix.map(str::to_owned), Rc::from(ns)));
    }
}

/// The namespaces a ZPT template starts with (`MacroProgram.DEFAULT_NAMESPACES`).
fn default_namespaces() -> Namespaces {
    Namespaces(
        [
            ("xmlns", XMLNS_NS),
            ("xml", XML_NS),
            ("tal", TAL_NS),
            ("metal", METAL_NS),
            ("i18n", I18N_NS),
            ("meta", META_NS),
        ]
        .into_iter()
        .map(|(p, ns)| (Some(p.to_owned()), Rc::from(ns)))
        .collect(),
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Text,
    Comment,
    Cdata,
    Declaration,
    XmlDeclaration,
    ProcessingInstruction,
    EndTag,
    EmptyTag,
    StartTag,
    Error,
}

/// Chameleon's `identify()`, including the `--` in comments check.
fn identify(token: &str, span: Span) -> Result<Kind, ParseError> {
    if !token.starts_with('<') {
        return Ok(Kind::Text);
    }
    if let Some(body) = token.strip_prefix("<!--") {
        // match_double_hyphen: `--(?!(-)*>)`.
        let mut from = 0;
        while let Some(i) = body.get(from..).and_then(|s| s.find("--")) {
            let at = from + i;
            let after = body.get(at + 2..).unwrap_or_default();
            if !after.trim_start_matches('-').starts_with('>') {
                let start = span.start as usize + 4 + at;
                return Err(ParseError::new(
                    "The string '--' is not allowed in a comment.",
                    Span::new(start, start + 2),
                ));
            }
            from = at + 1;
        }
        return Ok(Kind::Comment);
    }
    Ok(if token.starts_with("<![CDATA[") {
        Kind::Cdata
    } else if token.starts_with("<!") {
        Kind::Declaration
    } else if token.starts_with("<?xml") {
        Kind::XmlDeclaration
    } else if token.starts_with("<?") {
        Kind::ProcessingInstruction
    } else if token.starts_with("</") {
        Kind::EndTag
    } else if token.ends_with("/>") {
        Kind::EmptyTag
    } else if token.ends_with('>') {
        Kind::StartTag
    } else {
        Kind::Error
    })
}

/// Parses tokens (from [`crate::tokenize::tokenize`]) into top-level items.
pub(crate) fn parse(src: &str, tokens: &[Span]) -> Result<Vec<Item>, ParseError> {
    let mut queue: Vec<Item> = Vec::new();
    let mut index: Vec<(Span, usize)> = Vec::new();
    let mut namespaces = vec![default_namespaces()];
    for &span in tokens {
        let token = span.text(src);
        let item = match identify(token, span)? {
            Kind::Text => Item::Text(span),
            Kind::Comment => Item::Comment(span),
            Kind::Error => Item::Default(span),
            Kind::Cdata => Item::Unsupported { kind: "CDATA section", span },
            Kind::Declaration => Item::Unsupported { kind: "declaration", span },
            Kind::XmlDeclaration => Item::Unsupported { kind: "XML declaration", span },
            Kind::ProcessingInstruction => Item::Unsupported { kind: "processing instruction", span },
            Kind::StartTag => {
                let mut ns = namespaces.last().cloned().unwrap_or_else(default_namespaces);
                let tag = parse_tag(src, span, &mut ns)?;
                namespaces.push(ns);
                index.push((tag.name, queue.len()));
                Item::StartTag(Box::new(tag))
            }
            Kind::EmptyTag => {
                let mut ns = namespaces.last().cloned().unwrap_or_else(default_namespaces);
                let tag = parse_tag(src, span, &mut ns)?;
                Item::Element(Box::new(Element { start: tag, end: None, children: Vec::new() }))
            }
            Kind::EndTag => {
                let mut ns = namespaces.pop().ok_or_else(|| ParseError::new("Unexpected end tag.", span))?;
                let end = parse_tag(src, span, &mut ns)?;
                let name = end.name.text(src);
                let mut element = None;
                while let Some((start_name, pos)) = index.pop() {
                    if start_name.text(src) == name {
                        let children = queue.split_off(pos + 1);
                        if let Some(Item::StartTag(start)) = queue.pop() {
                            element = Some(Element { start: *start, end: Some(end), children });
                        }
                        break;
                    }
                }
                Item::Element(Box::new(element.ok_or_else(|| ParseError::new("Unexpected end tag.", span))?))
            }
        };
        queue.push(item);
    }
    Ok(queue)
}

/// `parse_tag`: `match_tag`, then namespace updates and resolution.
fn parse_tag(src: &str, span: Span, namespaces: &mut Namespaces) -> Result<Tag, ParseError> {
    let mut tag = match_tag(src, span)?;
    for attr in &tag.attrs {
        let name = attr.name.text(src);
        if name == "xmlns" {
            namespaces.set(None, attr.value.text(src));
        } else if let Some(prefix) = name.strip_prefix("xmlns:") {
            namespaces.set(Some(prefix), attr.value.text(src));
        }
    }
    let tag_name = tag.name.text(src);
    let prefix = tag_name.split_once(':').map(|(p, _)| p);
    let default: Rc<str> = namespaces.get(prefix).cloned().unwrap_or_else(|| Rc::from(XML_NS));
    let mut ns_attrs: Vec<NsAttr> = Vec::new();
    for attr in &tag.attrs {
        let name = attr.name.text(src);
        let (ns, local) = match name.split_once(':') {
            Some((prefix, local)) => match namespaces.get(Some(prefix)) {
                Some(ns) => (ns.clone(), local),
                None => {
                    // Chameleon raises a bare KeyError here (restricted_namespace).
                    return Err(ParseError::new(format!("Undefined namespace prefix: {prefix}."), attr.name));
                }
            },
            None => (default.clone(), name),
        };
        // An OrderedDict keyed by (namespace, name): a repeat keeps its place, takes the value.
        match ns_attrs.iter_mut().find(|a| a.ns == ns && a.name == local) {
            Some(existing) => existing.value = attr.value,
            None => ns_attrs.push(NsAttr { ns, name: local.to_owned(), value: attr.value }),
        }
    }
    tag.namespace = default;
    tag.ns_attrs = ns_attrs;
    Ok(tag)
}

/// `match_tag`: the `match_tag_prefix_and_name` regex, then `match_single_attribute` with
/// `finditer` over the rest of the token.
fn match_tag(src: &str, span: Span) -> Result<Tag, ParseError> {
    let token = span.text(src);
    let base = span.start as usize;
    let at = |a: usize, b: usize| Span::new(base + a, base + b);
    let prefix_len = if token.starts_with("</") { 2 } else { 1 };
    let unsupported = || ParseError::new("Unable to parse tag.", span);

    // `(?P<name>([^:\n\t\r ]+:)?[^ \n\t\r>/]+)`
    let rest_class = |c: char| !matches!(c, ' ' | '\n' | '\t' | '\r' | '>' | '/');
    let after_prefix = token.get(prefix_len..).ok_or_else(unsupported)?;
    let group = run_len(after_prefix, |c| !matches!(c, ':' | '\n' | '\t' | '\r' | ' '));
    let mut name_end = None;
    if group > 0 && after_prefix.get(group..).is_some_and(|s| s.starts_with(':')) {
        let n = run_len(after_prefix.get(group + 1..).unwrap_or_default(), rest_class);
        if n > 0 {
            name_end = Some(group + 1 + n);
        }
    }
    let name_end = match name_end {
        Some(end) => end,
        None => match run_len(after_prefix, rest_class) {
            0 => return Err(unsupported()),
            n => n,
        },
    } + prefix_len;

    // `(?P<suffix>(?P<space>\s*)/?>)?`
    let after_name = token.get(name_end..).unwrap_or_default();
    let ws = run_len(after_name, is_space);
    let mut p = ws;
    if after_name.get(p..).is_some_and(|s| s.starts_with('/')) {
        p += 1;
    }
    let (end, mut suffix, space) = if after_name.get(p..).is_some_and(|s| s.starts_with('>')) {
        (name_end + p + 1, Some(at(name_end, name_end + p + 1)), at(name_end, name_end + ws))
    } else {
        (name_end, None, at(name_end, name_end))
    };

    let mut attrs = Vec::new();
    let rest = token.get(end..).unwrap_or_default();
    let mut pos = 0;
    while pos < rest.len() {
        match match_attribute(rest, pos) {
            Some((m_end, a)) => {
                let off = end;
                attrs.push(Attr {
                    space: at(off + a.space.0, off + a.space.1),
                    name: at(off + a.name.0, off + a.name.1),
                    eq: at(off + a.eq.0, off + a.eq.1),
                    quote: at(off + a.quote.0, off + a.quote.1),
                    value: at(off + a.value.0, off + a.value.1),
                });
                suffix = Some(at(end + m_end, token.len()));
                pos = m_end;
            }
            None => pos += rest.get(pos..).and_then(|s| s.chars().next()).map_or(1, char::len_utf8),
        }
    }

    Ok(Tag {
        span,
        prefix: at(0, prefix_len),
        name: at(prefix_len, name_end),
        attrs,
        suffix,
        space,
        namespace: Rc::from(XML_NS),
        ns_attrs: Vec::new(),
    })
}

/// Offsets (relative to the matched string) of one attribute's groups.
struct AttrMatch {
    space: (usize, usize),
    name: (usize, usize),
    eq: (usize, usize),
    quote: (usize, usize),
    value: (usize, usize),
}

/// `match_single_attribute` at byte offset `at` of `s`:
/// `(?P<space>\s+)(?!\d)(?P<name>[^ =/>\n\t\r]+)((?P<eq>\s*=\s*)((?P<quote>['"])(?P<value>.*?)(?P=quote)|(?P<alt_value>[^\s'">/]+))|(?P<simple_value>(?![ \\n\\t\\r]*=)))`.
/// The raw string makes the last class `[ \\ntr]` (a backslash and three letters), which is
/// reproduced. Greedy groups backtrack longest-first, as the regex engine does.
fn match_attribute(s: &str, at: usize) -> Option<(usize, AttrMatch)> {
    let tail = s.get(at..)?;
    let spaces: Vec<usize> = char_ends(tail, is_space);
    for &sp in spaces.iter().rev() {
        let name_start = at + sp;
        let after_space = s.get(name_start..)?;
        if after_space.chars().next().is_some_and(is_digit) {
            continue;
        }
        let names = char_ends(after_space, |c| !matches!(c, ' ' | '=' | '/' | '>' | '\n' | '\t' | '\r'));
        for &n in names.iter().rev() {
            let name_end = name_start + n;
            let rest = s.get(name_end..)?;
            // Alternative 1: `\s*=\s*` then a quoted or an unquoted value.
            let ws1 = run_len(rest, is_space);
            if rest.get(ws1..).is_some_and(|r| r.starts_with('=')) {
                let ws2 = run_len(rest.get(ws1 + 1..).unwrap_or_default(), is_space);
                let v = name_end + ws1 + 1 + ws2;
                let value_text = s.get(v..).unwrap_or_default();
                let eq = (name_end, v);
                if let Some(q @ ('"' | '\'')) = value_text.chars().next()
                    && let Some(close) = value_text.get(1..).and_then(|r| r.find(q))
                {
                    let value = (v + 1, v + 1 + close);
                    let m = AttrMatch {
                        space: (at, name_start),
                        name: (name_start, name_end),
                        eq,
                        quote: (v, v + 1),
                        value,
                    };
                    return Some((v + 1 + close + 1, m));
                }
                let alt = run_len(value_text, |c| !is_space(c) && !matches!(c, '\'' | '"' | '>' | '/'));
                if alt > 0 {
                    let m = AttrMatch {
                        space: (at, name_start),
                        name: (name_start, name_end),
                        eq,
                        quote: (v, v),
                        value: (v, v + alt),
                    };
                    return Some((v + alt, m));
                }
            }
            // Alternative 2: a valueless attribute, unless `[ \\ntr]*=` follows.
            let skipped = run_len(rest, |c| matches!(c, ' ' | '\\' | 'n' | 't' | 'r'));
            if !rest.get(skipped..).is_some_and(|r| r.starts_with('=')) {
                let e = (name_end, name_end);
                let m = AttrMatch { space: (at, name_start), name: (name_start, name_end), eq: e, quote: e, value: e };
                return Some((name_end, m));
            }
        }
    }
    None
}

/// Byte offsets after each char of the leading run of chars matching `pred` (the possible
/// ends of a `+` repetition).
fn char_ends(s: &str, pred: impl Fn(char) -> bool) -> Vec<usize> {
    s.char_indices().take_while(|&(_, c)| pred(c)).map(|(i, c)| i + c.len_utf8()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tokenize::tokenize;

    fn tree(src: &str) -> Vec<Item> {
        parse(src, &tokenize(src)).unwrap()
    }

    fn first_tag(src: &str) -> Tag {
        match tree(src).into_iter().next() {
            Some(Item::Element(e)) => e.start,
            Some(Item::StartTag(t)) => *t,
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn attributes_keep_their_exact_text() {
        let src = "<p class=\"a\"  id='b' c\nd=e>x</p>";
        let tag = first_tag(src);
        let parts: Vec<_> = tag
            .attrs
            .iter()
            .map(|a| (a.space.text(src), a.name.text(src), a.eq.text(src), a.quote.text(src), a.value.text(src)))
            .collect();
        assert_eq!(
            parts,
            [
                (" ", "class", "=", "\"", "a"),
                ("  ", "id", "=", "'", "b"),
                (" ", "c", "", "", ""),
                ("\n", "d", "=", "", "e")
            ]
        );
        assert_eq!(tag.suffix.map(|s| s.text(src)), Some(">"));
    }

    #[test]
    fn namespaces_resolve_with_tal_predeclared() {
        let src = "<tal:block repeat=\"x xs\" tal:content=\"y\" class=\"c\"/>";
        let tag = first_tag(src);
        assert_eq!(&*tag.namespace, TAL_NS);
        let ns: Vec<_> = tag.ns_attrs.iter().map(|a| (&*a.ns, a.name.as_str())).collect();
        assert_eq!(ns, [(TAL_NS, "repeat"), (TAL_NS, "content"), (TAL_NS, "class")]);
    }

    #[test]
    fn undefined_prefix_is_an_error() {
        let src = "<svg xlink:href=\"a\"/>";
        assert_eq!(parse(src, &tokenize(src)).unwrap_err().message, "Undefined namespace prefix: xlink.");
    }

    #[test]
    fn end_tags_close_through_unclosed_start_tags() {
        let items = tree("<p>a<b>c</p>d");
        let [Item::Element(p), Item::Text(_)] = items.as_slice() else { panic!("{items:?}") };
        assert!(matches!(p.children.as_slice(), [Item::Text(_), Item::StartTag(_), Item::Text(_)]));
    }

    #[test]
    fn unexpected_end_tag() {
        let src = "<p>a</b>";
        assert_eq!(parse(src, &tokenize(src)).unwrap_err().message, "Unexpected end tag.");
    }

    #[test]
    fn double_hyphen_in_comment() {
        let src = "<!-- a -- b -->";
        assert!(parse(src, &tokenize(src)).is_err());
        assert!(parse("<!-- ok --->", &tokenize("<!-- ok --->")).is_err());
        assert!(parse("<!-- ok -->", &tokenize("<!-- ok -->")).is_ok());
    }
}
