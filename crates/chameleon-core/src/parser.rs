// Ported from Chameleon's src/chameleon/parser.py (`identify`, `match_tag`, `parse_tag`,
// `update_namespace`, `unpack_attributes`, `ElementParser`), modified: rewritten in Rust over
// byte spans; the lookaround-free regexes run verbatim on the `regex` crate (crate::pyre),
// the two with lookaheads are matched by hand and checked against fancy-regex in tests.

//! Turns tokens into an element tree, resolving namespaces like Chameleon's `ElementParser`.
//!
//! The output mirrors Chameleon's `(kind, args)` items: every piece of text is a [`Span`] of
//! the source (a `Token` in Chameleon), and the few values Chameleon creates as plain strings
//! (an unquoted attribute's empty quote, a valueless attribute's empty `=`, quote and value)
//! are marked by [`AttrKind`] so tests can tell them apart.

use std::collections::HashMap;
use std::sync::{Arc, LazyLock};

use crate::pychars::{is_digit, is_space};
use crate::pyre;
use crate::source::Span;

pub(crate) const XML_NS: &str = "http://www.w3.org/XML/1998/namespace";
pub(crate) const XMLNS_NS: &str = "http://www.w3.org/2000/xmlns/";
pub(crate) const TAL_NS: &str = "http://xml.zope.org/namespaces/tal";
pub(crate) const METAL_NS: &str = "http://xml.zope.org/namespaces/metal";
pub(crate) const I18N_NS: &str = "http://xml.zope.org/namespaces/i18n";
pub(crate) const META_NS: &str = "http://xml.zope.org/namespaces/meta";

/// `match_double_hyphen`, for the reference test.
#[cfg(test)]
const DOUBLE_HYPHEN_RE: &str = r"--(?!(-)*>)";
/// `match_tag_prefix_and_name` (`re.UNICODE | re.DOTALL`).
const TAG_PREFIX_AND_NAME_RE: &str =
    concat!(r"^(?P<prefix></?)(?P<name>([^:\n\t\r ]+:)?[^ \n\t\r>/]+)", r"(?P<suffix>(?P<space>\s*)/?>)?");
/// `match_single_attribute` (`re.UNICODE | re.DOTALL`), for the reference test. The raw string
/// makes the last class `[ \\n\\t\\r]`: a space, backslashes and the letters `n`, `t`, `r`.
#[cfg(test)]
const SINGLE_ATTRIBUTE_RE: &str = concat!(
    r"(?P<space>\s+)(?!\d)",
    r"(?P<name>[^ =/>\n\t\r]+)",
    r"((?P<eq>\s*=\s*)",
    r#"((?P<quote>['"])(?P<value>.*?)(?P=quote)|"#,
    r#"(?P<alt_value>[^\s'">/]+))|"#,
    r"(?P<simple_value>(?![ \\n\\t\\r]*=)))",
);
/// `match_processing_instruction` (`re.DOTALL`).
const PROCESSING_INSTRUCTION_RE: &str = r"^<\?(?P<name>\w+)(?P<text>.*?)\?>";

static TAG_PREFIX_AND_NAME: LazyLock<regex::Regex> =
    LazyLock::new(|| pyre::compile_plain(TAG_PREFIX_AND_NAME_RE, true));
static PROCESSING_INSTRUCTION: LazyLock<regex::Regex> =
    LazyLock::new(|| pyre::compile_plain(PROCESSING_INSTRUCTION_RE, true));

/// Which exception Chameleon raises for a parse failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ParseErrorKind {
    /// `chameleon.exc.ParseError`, with the offending token.
    Syntax,
    /// A bare `KeyError("Undefined namespace prefix: x.")` (`restricted_namespace`).
    UndefinedPrefix,
    /// A tag with no name (`</`): Chameleon's `match_tag` calls `groupdict()` on a failed
    /// match and raises `AttributeError`.
    NoTagName,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct ParseError {
    pub kind: ParseErrorKind,
    pub message: String,
    /// The token Chameleon attaches (for `Syntax`), or the closest one (for the others).
    pub span: Span,
}

impl ParseError {
    fn syntax(message: &str, span: Span) -> Self {
        Self { kind: ParseErrorKind::Syntax, message: message.to_owned(), span }
    }
}

/// How an attribute's value was written. Chameleon's `match_tag` replaces the groups an
/// unquoted or valueless attribute leaves unset with plain empty strings (no position).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AttrKind {
    /// `name="value"` or `name='value'`.
    Quoted,
    /// `name=value`: `quote` is a plain `''`.
    Unquoted,
    /// `name`: `eq`, `quote` and `value` are plain `''`.
    Valueless,
}

/// One attribute as written in a tag. Empty plain-string parts are empty spans (at the value
/// for `Unquoted`, after the name for `Valueless`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Attr {
    pub space: Span,
    pub name: Span,
    pub eq: Span,
    pub quote: Span,
    pub value: Span,
    pub kind: AttrKind,
}

/// A namespace URI: from the defaults (a plain string) or from an `xmlns` attribute (a token).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NsValue {
    pub uri: Arc<str>,
    pub span: Option<Span>,
}

impl NsValue {
    fn plain(uri: &str) -> Self {
        Self { uri: Arc::from(uri), span: None }
    }
}

/// An attribute resolved to its namespace (one entry of `ns_attrs`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NsAttr {
    pub ns: Arc<str>,
    /// Where the namespace URI was declared (an `xmlns` attribute's value), if it was.
    pub ns_span: Option<Span>,
    /// The local name (after the prefix).
    pub name: String,
    pub name_span: Span,
    pub value: Span,
    /// How the attribute that gave the value was written (a valueless one's is a plain `''`).
    pub kind: AttrKind,
}

/// A start, end or empty tag (Chameleon's tag dict).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tag {
    pub span: Span,
    /// `<` or `</`.
    pub prefix: Span,
    pub name: Span,
    pub attrs: Vec<Attr>,
    /// The rest of the tag after the name or the last attribute (`>`, ` />`, ...). `None`
    /// when the regex leaves it unset (a tag without `>` right after its name and no
    /// attributes).
    pub suffix: Option<Span>,
    /// Whitespace between the name and `/?>`, when that group matched.
    pub space: Option<Span>,
    /// The element's namespace URI.
    pub namespace: NsValue,
    pub ns_attrs: Vec<NsAttr>,
    /// The prefix map in effect for this tag (`ns_map`).
    pub ns_map: Arc<Namespaces>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Item {
    Text(Span),
    Comment(Span),
    Cdata(Span),
    /// A declaration (`<!DOCTYPE ...>`), a token Chameleon cannot classify (a lone `<`, a tag
    /// without `>`), or a processing instruction its regex rejects: output verbatim.
    Default(Span),
    ProcessingInstruction {
        span: Span,
        name: Span,
        text: Span,
    },
    /// A start tag that was never closed: an element with no end tag and no children.
    StartTag(Box<Tag>),
    Element(Box<Element>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Element {
    pub start: Tag,
    pub end: Option<Tag>,
    pub children: Vec<Item>,
}

/// Iterative: the parser builds trees of any depth without recursing, and the default drop
/// would recurse once per level (100,000 nested elements overflowed the native stack, which
/// kills the process). Each element's children move to a heap stack before it drops.
impl Drop for Element {
    fn drop(&mut self) {
        let mut pending = std::mem::take(&mut self.children);
        while let Some(item) = pending.pop() {
            if let Item::Element(mut element) = item {
                pending.append(&mut element.children);
            }
        }
    }
}

/// Prefix (`None` for `xmlns="..."`) to namespace URI, in insertion order like the dict
/// Chameleon copies for every start tag.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Namespaces(Vec<(Option<Arc<str>>, Option<Span>, NsValue)>);

impl Namespaces {
    pub fn new<'a>(entries: impl IntoIterator<Item = (Option<&'a str>, &'a str)>) -> Self {
        let mut ns = Self::default();
        for (prefix, uri) in entries {
            ns.set(prefix, None, NsValue::plain(uri));
        }
        ns
    }

    /// `MacroProgram.DEFAULT_NAMESPACES`, what a ZPT template starts with.
    #[must_use]
    pub fn zpt() -> Self {
        Self::new([
            (Some("xmlns"), XMLNS_NS),
            (Some("xml"), XML_NS),
            (Some("tal"), TAL_NS),
            (Some("metal"), METAL_NS),
            (Some("i18n"), I18N_NS),
            (Some("meta"), META_NS),
        ])
    }

    #[must_use]
    pub fn get(&self, prefix: Option<&str>) -> Option<&NsValue> {
        self.0.iter().find(|(p, _, _)| p.as_deref() == prefix).map(|(_, _, ns)| ns)
    }

    /// `namespace[prefix] = value`: an existing key keeps its place (and the key object that
    /// was first used: `span`, the prefix's position in its `xmlns:` attribute).
    fn set(&mut self, prefix: Option<&str>, span: Option<Span>, value: NsValue) {
        match self.0.iter_mut().find(|(p, _, _)| p.as_deref() == prefix) {
            Some((_, _, v)) => *v = value,
            None => self.0.push((prefix.map(Arc::from), span, value)),
        }
    }

    pub fn iter(&self) -> impl Iterator<Item = (Option<&str>, &NsValue)> {
        self.0.iter().map(|(p, _, v)| (p.as_deref(), v))
    }

    /// Each prefix with where it was declared (`None`: a default namespace), and its value.
    pub fn iter_spans(&self) -> impl Iterator<Item = (Option<&str>, Option<Span>, &NsValue)> {
        self.0.iter().map(|(p, s, v)| (p.as_deref(), *s, v))
    }
}

/// What `identify()` returns.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Kind {
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
pub(crate) fn identify(src: &str, span: Span) -> Result<Kind, ParseError> {
    let token = span.text(src);
    if !token.starts_with('<') {
        return Ok(Kind::Text);
    }
    if let Some(body) = token.strip_prefix("<!--") {
        if let Some(at) = double_hyphen(body) {
            let start = span.start as usize + 4 + at;
            return Err(ParseError::syntax(
                "The string '--' is not allowed in a comment.",
                Span::new(start, start + 2),
            ));
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

/// `match_double_hyphen.search(body)`, `--(?!(-)*>)`: the first `--` not followed by a run of
/// hyphens and a `>` (the lookahead's `(-)*` can only help by taking every hyphen, since a
/// hyphen is not a `>`). Returns its offset.
fn double_hyphen(body: &str) -> Option<usize> {
    let b = body.as_bytes();
    let mut from = 0;
    while let Some(i) = memchr::memmem::find(&b[from..], b"--").map(|i| from + i) {
        let dashes = b[i + 2..].iter().take_while(|&&c| c == b'-').count();
        if b.get(i + 2 + dashes) != Some(&b'>') {
            return Some(i);
        }
        from = i + 1;
    }
    None
}

/// Options for [`parse`] (`ElementParser`'s constructor arguments).
#[derive(Debug, Clone)]
pub struct ParseOptions {
    pub namespaces: Namespaces,
    pub restricted_namespace: bool,
}

impl Default for ParseOptions {
    fn default() -> Self {
        Self { namespaces: Namespaces::zpt(), restricted_namespace: true }
    }
}

/// `ElementParser`: parses tokens (spans of `src`, from [`crate::tokenize`]) into top-level
/// items. Like Chameleon, every token is parsed before anything else looks at the result.
pub fn parse(src: &str, tokens: &[Span], options: &ParseOptions) -> Result<Vec<Item>, ParseError> {
    let mut queue: Vec<Item> = Vec::new();
    let mut index: Vec<(Span, usize)> = Vec::new();
    let mut namespaces: Vec<Arc<Namespaces>> = vec![Arc::new(options.namespaces.clone())];
    let restricted = options.restricted_namespace;
    let top = |namespaces: &[Arc<Namespaces>]| namespaces.last().map(|ns| (**ns).clone()).unwrap_or_default();
    for &span in tokens {
        let item = match identify(src, span)? {
            Kind::Text => Item::Text(span),
            Kind::Comment => Item::Comment(span),
            Kind::Cdata => Item::Cdata(span),
            Kind::Declaration | Kind::Error => Item::Default(span),
            Kind::ProcessingInstruction => processing_instruction(src, span),
            Kind::StartTag => {
                // Chameleon pushes the copy before parsing; a failure aborts either way.
                let mut ns = top(&namespaces);
                let tag = parse_tag(src, span, &mut ns, restricted)?;
                namespaces.push(Arc::clone(&tag.ns_map));
                index.push((tag.name, queue.len()));
                Item::StartTag(Box::new(tag))
            }
            Kind::EmptyTag | Kind::XmlDeclaration => {
                let mut ns = top(&namespaces);
                let tag = parse_tag(src, span, &mut ns, restricted)?;
                Item::Element(Box::new(Element { start: tag, end: None, children: Vec::new() }))
            }
            Kind::EndTag => {
                // The namespace stack pops once per end tag, even when the end tag closes
                // several unclosed start tags, exactly as Chameleon's does.
                let popped = namespaces.pop().ok_or_else(|| ParseError::syntax("Unexpected end tag.", span))?;
                let mut ns = Arc::unwrap_or_clone(popped);
                let end = parse_tag(src, span, &mut ns, restricted)?;
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
                Item::Element(Box::new(element.ok_or_else(|| ParseError::syntax("Unexpected end tag.", span))?))
            }
        };
        queue.push(item);
    }
    Ok(queue)
}

/// `visit_processing_instruction`.
fn processing_instruction(src: &str, span: Span) -> Item {
    let token = span.text(src);
    let Some(c) = PROCESSING_INSTRUCTION.captures(token) else { return Item::Default(span) };
    let group = |name: &str| c.name(name).map(|m| span.sub(m.start(), m.end()));
    match (group("name"), group("text")) {
        (Some(name), Some(text)) => Item::ProcessingInstruction { span, name, text },
        _ => Item::Default(span),
    }
}

/// `parse_tag`: `match_tag`, then `update_namespace` and `unpack_attributes`. `namespaces` is
/// updated in place and becomes the tag's `ns_map`.
fn parse_tag(src: &str, span: Span, namespaces: &mut Namespaces, restricted: bool) -> Result<Tag, ParseError> {
    let mut tag = match_tag(src, span)?;
    for attr in &tag.attrs {
        let name = attr.name.text(src);
        let value = NsValue {
            uri: Arc::from(attr.value.text(src)),
            span: (attr.kind != AttrKind::Valueless).then_some(attr.value),
        };
        if name == "xmlns" {
            namespaces.set(None, None, value);
        } else if let Some(prefix) = name.strip_prefix("xmlns:") {
            namespaces.set(Some(prefix), Some(attr.name.sub(6, attr.name.len())), value);
        }
    }
    let tag_name = tag.name.text(src);
    let prefix = tag_name.split_once(':').map(|(p, _)| p);
    let default = namespaces.get(prefix).cloned().unwrap_or_else(|| NsValue::plain(XML_NS));
    let mut ns_attrs: Vec<NsAttr> = Vec::with_capacity(tag.attrs.len());
    let mut index: HashMap<(Arc<str>, String), usize> = HashMap::new();
    for attr in &tag.attrs {
        let name = attr.name.text(src);
        let (ns, local, local_start) = match name.split_once(':') {
            Some((prefix, local)) => match namespaces.get(Some(prefix)) {
                Some(ns) => (ns.clone(), local, prefix.len() + 1),
                None if restricted => {
                    return Err(ParseError {
                        kind: ParseErrorKind::UndefinedPrefix,
                        message: format!("Undefined namespace prefix: {prefix}."),
                        span: attr.name,
                    });
                }
                None => (default.clone(), local, prefix.len() + 1),
            },
            None => (default.clone(), name, 0),
        };
        let NsValue { uri: ns, span: ns_span } = ns;
        let name_span = attr.name.sub(local_start, attr.name.len());
        // An OrderedDict keyed by (namespace, name): a repeat keeps its place, takes the value.
        // A linear scan for ordinary tags, an index past `INDEX_FROM` attributes.
        let found = if ns_attrs.len() < INDEX_FROM {
            ns_attrs.iter().position(|a| a.ns == ns && a.name == local)
        } else {
            if index.is_empty() {
                index.extend(ns_attrs.iter().enumerate().map(|(i, a)| ((a.ns.clone(), a.name.clone()), i)));
            }
            index.get(&(ns.clone(), local.to_owned())).copied()
        };
        if let Some(existing) = found.and_then(|i| ns_attrs.get_mut(i)) {
            existing.value = attr.value;
            existing.kind = attr.kind;
        } else {
            if !index.is_empty() {
                index.insert((ns.clone(), local.to_owned()), ns_attrs.len());
            }
            ns_attrs.push(NsAttr {
                ns,
                ns_span,
                name: local.to_owned(),
                name_span,
                value: attr.value,
                kind: attr.kind,
            });
        }
    }
    tag.namespace = default;
    tag.ns_attrs = ns_attrs;
    tag.ns_map = Arc::new(std::mem::take(namespaces));
    Ok(tag)
}

/// Attribute count past which `parse_tag` indexes `ns_attrs` instead of scanning it.
const INDEX_FROM: usize = 16;

/// The span of a named group, relative to `base`.
fn group(c: &regex::Captures<'_>, name: &str, base: usize) -> Option<Span> {
    c.name(name).map(|m| Span::new(base + m.start(), base + m.end()))
}

/// `match_tag`: the `match_tag_prefix_and_name` regex, then `match_single_attribute` with
/// `finditer` over the rest of the token.
fn match_tag(src: &str, span: Span) -> Result<Tag, ParseError> {
    let token = span.text(src);
    let base = span.start as usize;
    let no_name = || ParseError {
        kind: ParseErrorKind::NoTagName,
        message: "'NoneType' object has no attribute 'groupdict'".to_owned(),
        span,
    };
    let c = TAG_PREFIX_AND_NAME.captures(token).ok_or_else(no_name)?;
    let (Some(prefix), Some(name), Some(whole)) = (group(&c, "prefix", base), group(&c, "name", base), c.get(0)) else {
        return Err(no_name());
    };
    let mut suffix = group(&c, "suffix", base);
    let space = group(&c, "space", base);

    let end = whole.end();
    let rest = &token[end..];
    let rest_base = base + end;
    let at = |(a, b): (usize, usize)| Span::new(rest_base + a, rest_base + b);
    let mut attrs = Vec::new();
    for m in single_attributes(rest) {
        attrs.push(Attr {
            space: at(m.space),
            name: at(m.name),
            eq: at(m.eq),
            quote: at(m.quote),
            value: at(m.value),
            kind: m.kind,
        });
        suffix = Some(Span::new(rest_base + m.end, span.end as usize));
    }

    Ok(Tag {
        span,
        prefix,
        name,
        attrs,
        suffix,
        space,
        namespace: NsValue::plain(XML_NS),
        ns_attrs: Vec::new(),
        ns_map: Arc::default(),
    })
}

/// One `match_single_attribute` match: group offsets relative to the searched string. For
/// `Unquoted` the quote, and for `Valueless` the `=`, quote and value, are empty ranges (the
/// plain `''` Chameleon substitutes).
#[derive(Debug, Clone, PartialEq, Eq)]
struct AttrMatch {
    space: (usize, usize),
    name: (usize, usize),
    eq: (usize, usize),
    quote: (usize, usize),
    value: (usize, usize),
    kind: AttrKind,
    end: usize,
}

/// `match_single_attribute.finditer(s)`.
fn single_attributes(s: &str) -> Vec<AttrMatch> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < s.len() {
        if let Some(m) = single_attribute_at(s, pos) {
            pos = m.end;
            out.push(m);
        } else {
            pos += s[pos..].chars().next().map_or(1, char::len_utf8);
        }
    }
    out
}

/// Byte offsets after each char of the leading run of chars matching `pred`, from `p`.
fn run_ends(s: &str, p: usize, pred: impl Fn(char) -> bool) -> Vec<usize> {
    s[p..].char_indices().take_while(|&(_, c)| pred(c)).map(|(i, c)| p + i + c.len_utf8()).collect()
}

fn char_at(s: &str, p: usize) -> Option<char> {
    s.get(p..).and_then(|r| r.chars().next())
}

/// `match_single_attribute` anchored at `p`, by hand (the regex has lookaheads and a
/// backreference, which the `regex` crate lacks, and fancy-regex's fixed backtracking stack
/// overflows on long values). The pattern, `(?P<space>\s+)(?!\d)(?P<name>[^ =/>\n\t\r]+)`
/// `((?P<eq>\s*=\s*)((?P<quote>['"])(?P<value>.*?)(?P=quote)|(?P<alt_value>[^\s'">/]+))`
/// `|(?P<simple_value>(?![ \\n\\t\\r]*=)))`, ends with the alternation, so the first
/// success in a backtracking engine's priority order is the match: every length of `space`
/// longest first, every length of `name` longest first, then the alternatives in order.
/// Within the `eq` alternative only the longest `\s*` runs can succeed (a shorter one leaves
/// `=` or the value on a whitespace character, which neither value alternative accepts), and
/// `.*?` up to the same quote is the first such quote. `\s` and `\d` are Python's.
fn single_attribute_at(text: &str, start: usize) -> Option<AttrMatch> {
    let bytes = text.as_bytes();
    let skip_space = |q: usize| q + crate::pychars::run_len(&text[q..], is_space);
    for &n0 in run_ends(text, start, is_space).iter().rev() {
        if char_at(text, n0).is_none_or(is_digit) {
            continue;
        }
        let name_ends = run_ends(text, n0, |c| !matches!(c, ' ' | '=' | '/' | '>' | '\n' | '\t' | '\r'));
        for &e in name_ends.iter().rev() {
            let base = |eq, quote, value, kind, end| AttrMatch {
                space: (start, n0),
                name: (n0, e),
                eq,
                quote,
                value,
                kind,
                end,
            };
            let w1 = skip_space(e);
            if bytes.get(w1) == Some(&b'=') {
                let v = skip_space(w1 + 1);
                match char_at(text, v) {
                    Some(q @ ('"' | '\'')) => {
                        if let Some(n) = memchr::memchr(q as u8, &bytes[v + 1..]) {
                            let close = v + 1 + n;
                            return Some(base((e, v), (v, v + 1), (v + 1, close), AttrKind::Quoted, close + 1));
                        }
                    }
                    Some(c) if !is_space(c) && !matches!(c, '>' | '/') => {
                        let end = v + crate::pychars::run_len(&text[v..], |c| {
                            !is_space(c) && !matches!(c, '\'' | '"' | '>' | '/')
                        });
                        return Some(base((e, v), (v, v), (v, end), AttrKind::Unquoted, end));
                    }
                    _ => {}
                }
            }
            let skipped =
                e + bytes[e..].iter().take_while(|&&c| matches!(c, b' ' | b'\\' | b'n' | b't' | b'r')).count();
            if bytes.get(skipped) != Some(&b'=') {
                return Some(base((e, e), (e, e), (e, e), AttrKind::Valueless, e));
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tokenize::tokenize;
    use proptest::prelude::*;

    /// `match_single_attribute.finditer(s)` on fancy-regex, converted like Chameleon's
    /// `match_tag` does.
    fn reference_attributes(s: &str) -> Result<Vec<AttrMatch>, String> {
        static RE: LazyLock<fancy_regex::Regex> = LazyLock::new(|| pyre::compile(SINGLE_ATTRIBUTE_RE, true));
        let mut out = Vec::new();
        for c in RE.captures_iter(s) {
            let c = c.map_err(|e| e.to_string())?;
            let g = |n: &str| c.name(n).map(|m| (m.start(), m.end()));
            let (space, name) = (g("space").ok_or("space")?, g("name").ok_or("name")?);
            let end = c.get(0).ok_or("match")?.end();
            let e = (name.1, name.1);
            out.push(if let Some(alt) = g("alt_value") {
                let eq = g("eq").ok_or("eq")?;
                AttrMatch { space, name, eq, quote: (alt.0, alt.0), value: alt, kind: AttrKind::Unquoted, end }
            } else if g("simple_value").is_some() {
                AttrMatch { space, name, eq: e, quote: e, value: e, kind: AttrKind::Valueless, end }
            } else {
                let (eq, quote, value) = (g("eq").ok_or("eq")?, g("quote").ok_or("q")?, g("value").ok_or("v")?);
                AttrMatch { space, name, eq, quote, value, kind: AttrKind::Quoted, end }
            });
        }
        Ok(out)
    }

    fn reference_double_hyphen(s: &str) -> Result<Option<usize>, String> {
        static RE: LazyLock<fancy_regex::Regex> = LazyLock::new(|| pyre::compile(DOUBLE_HYPHEN_RE, false));
        RE.find(s).map(|m| m.map(|m| m.start())).map_err(|e| e.to_string())
    }

    proptest! {
        #[test]
        fn hand_matchers_agree_with_the_regexes(
            s in r#"([ \t\n\r\x{b}\x{3000}\x{1c}=/>'"`\\ntrab1\x{663}-]|é){0,30}"#
        ) {
            prop_assert_eq!(Ok(single_attributes(&s)), reference_attributes(&s));
            prop_assert_eq!(Ok(double_hyphen(&s)), reference_double_hyphen(&s));
        }
    }

    /// The token spans an item tree was built from, in document order.
    fn flatten(items: &[Item], out: &mut Vec<Span>) {
        for item in items {
            match item {
                Item::Text(s) | Item::Comment(s) | Item::Cdata(s) | Item::Default(s) => out.push(*s),
                Item::ProcessingInstruction { span, .. } => out.push(*span),
                Item::StartTag(t) => out.push(t.span),
                Item::Element(e) => {
                    out.push(e.start.span);
                    flatten(&e.children, out);
                    if let Some(end) = &e.end {
                        out.push(end.span);
                    }
                }
            }
        }
    }

    /// Every part of a tag lies inside the tag's token, on char boundaries.
    fn check_tag(src: &str, tag: &Tag) -> Result<(), TestCaseError> {
        let parts = [tag.prefix, tag.name].into_iter().chain(tag.suffix).chain(tag.space);
        let attrs = tag.attrs.iter().flat_map(|a| [a.space, a.name, a.eq, a.quote, a.value]);
        for s in parts.chain(attrs) {
            prop_assert!(s.start >= tag.span.start && s.end <= tag.span.end, "{s:?} outside {:?}", tag.span);
            prop_assert!(src.get(s.range()).is_some());
        }
        Ok(())
    }

    fn check_tags(src: &str, items: &[Item]) -> Result<(), TestCaseError> {
        for item in items {
            match item {
                Item::StartTag(t) => check_tag(src, t)?,
                Item::Element(e) => {
                    check_tag(src, &e.start)?;
                    if let Some(end) = &e.end {
                        check_tag(src, end)?;
                    }
                    check_tags(src, &e.children)?;
                }
                _ => {}
            }
        }
        Ok(())
    }

    proptest! {
        #[test]
        fn trees_cover_the_tokens_and_never_panic(
            src in r#"([<>/="' a:xtl!?\[\]-]|<a|</a|<p |xmlns:a|tal:|\x{3000}|\x{1c}|é){0,40}"#
        ) {
            let tokens = tokenize(&src);
            if let Ok(items) = parse(&src, &tokens, &ParseOptions::default()) {
                let mut spans = Vec::new();
                flatten(&items, &mut spans);
                prop_assert_eq!(&spans, &tokens);
                check_tags(&src, &items)?;
            }
        }
    }

    fn parse_zpt(src: &str) -> Result<Vec<Item>, ParseError> {
        parse(src, &tokenize(src), &ParseOptions::default())
    }

    fn tree(src: &str) -> Vec<Item> {
        parse_zpt(src).unwrap()
    }

    fn first_tag(src: &str) -> Tag {
        match tree(src).into_iter().next() {
            Some(Item::Element(e)) => e.start.clone(),
            Some(Item::StartTag(t)) => *t,
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn namespace_declarations_keep_their_positions() {
        let src = r#"<a xmlns:x="u1" xmlns="d"><b x:c="1" xmlns:x="u2"/></a>"#;
        let tokens = tokenize(src);
        let items = parse(src, &tokens, &ParseOptions::default()).unwrap();
        let Item::Element(a) = &items[0] else { panic!("{items:?}") };
        let Item::Element(b) = &a.children[0] else { panic!("{:?}", a.children) };
        let spans: Vec<_> = b.start.ns_map.iter_spans().map(|(p, s, v)| (p, s.map(|s| s.text(src)), &*v.uri)).collect();
        // The prefix keeps its first declaration's key (in `a`), the value is the newer one.
        assert!(spans.contains(&(Some("x"), Some("x"), "u2")));
        let x = b.start.ns_map.iter_spans().find(|(p, _, _)| *p == Some("x")).and_then(|(_, s, _)| s).unwrap();
        assert_eq!(x.start, 9);
        assert!(spans.contains(&(None, None, "d")));
        let attr = &b.start.ns_attrs[0];
        assert_eq!((&*attr.ns, attr.ns_span.map(|s| s.text(src))), ("u2", Some("u2")));
    }

    #[test]
    fn attributes_keep_their_exact_text() {
        let src = "<p class=\"a\"  id='b' c\nd=e>x</p>";
        let tag = first_tag(src);
        let parts: Vec<_> = tag
            .attrs
            .iter()
            .map(|a| {
                (a.space.text(src), a.name.text(src), a.eq.text(src), a.quote.text(src), a.value.text(src), a.kind)
            })
            .collect();
        assert_eq!(
            parts,
            [
                (" ", "class", "=", "\"", "a", AttrKind::Quoted),
                ("  ", "id", "=", "'", "b", AttrKind::Quoted),
                (" ", "c", "", "", "", AttrKind::Valueless),
                ("\n", "d", "=", "", "e", AttrKind::Unquoted)
            ]
        );
        assert_eq!(tag.suffix.map(|s| s.text(src)), Some(">"));
    }

    #[test]
    fn namespaces_resolve_with_tal_predeclared() {
        let src = "<tal:block repeat=\"x xs\" tal:content=\"y\" class=\"c\"/>";
        let tag = first_tag(src);
        assert_eq!(&*tag.namespace.uri, TAL_NS);
        let ns: Vec<_> = tag.ns_attrs.iter().map(|a| (&*a.ns, a.name.as_str(), a.name_span.text(src))).collect();
        assert_eq!(ns, [(TAL_NS, "repeat", "repeat"), (TAL_NS, "content", "content"), (TAL_NS, "class", "class")]);
    }

    #[test]
    fn xmlns_declarations_scope_to_the_namespace_stack() {
        let src = "<a xmlns:x=\"u\"><x:b x:c=\"1\"/></a><d xmlns=\"v\" e=\"2\"/>";
        let items = tree(src);
        let [Item::Element(a), Item::Element(d)] = items.as_slice() else { panic!("{items:?}") };
        let [Item::Element(b)] = a.children.as_slice() else { panic!() };
        assert_eq!(&*b.start.namespace.uri, "u");
        assert_eq!(b.start.namespace.span.map(|s| s.text(src)), Some("u"));
        assert_eq!(&*b.start.ns_attrs[0].ns, "u");
        assert_eq!(&*d.start.namespace.uri, "v");
        assert!(d.start.ns_map.get(Some("x")).is_none());
    }

    #[test]
    fn undefined_prefix_is_a_key_error() {
        let err = parse_zpt("<svg xlink:href=\"a\"/>").unwrap_err();
        assert_eq!(err.kind, ParseErrorKind::UndefinedPrefix);
        assert_eq!(err.message, "Undefined namespace prefix: xlink.");
        let src = "<svg xlink:href=\"a\"/>";
        let lenient = ParseOptions { restricted_namespace: false, ..ParseOptions::default() };
        let items = parse(src, &tokenize(src), &lenient).unwrap();
        let [Item::Element(e)] = items.as_slice() else { panic!() };
        assert_eq!(&*e.start.ns_attrs[0].ns, XML_NS);
    }

    #[test]
    fn end_tags_close_through_unclosed_start_tags() {
        let items = tree("<p>a<b>c</p>d");
        let [Item::Element(p), Item::Text(_)] = items.as_slice() else { panic!("{items:?}") };
        assert!(matches!(p.children.as_slice(), [Item::Text(_), Item::StartTag(_), Item::Text(_)]));
    }

    #[test]
    fn unexpected_end_tag() {
        let err = parse_zpt("<p>a</b>").unwrap_err();
        assert_eq!(
            (err.kind, err.message.as_str(), err.span),
            (ParseErrorKind::Syntax, "Unexpected end tag.", Span::new(4, 8))
        );
    }

    #[test]
    fn nameless_tag_is_an_attribute_error() {
        assert_eq!(parse_zpt("</").unwrap_err().kind, ParseErrorKind::NoTagName);
    }

    #[test]
    fn double_hyphen_in_comment() {
        let err = parse_zpt("<!-- a -- b -->").unwrap_err();
        assert_eq!(err.span, Span::new(7, 9));
        assert!(parse_zpt("<!-- ok --->").is_err());
        assert!(parse_zpt("<!-- ok -->").is_ok());
    }

    #[test]
    fn declarations_and_processing_instructions() {
        let src = "<!DOCTYPE html><?php x ?><? ?><?xml version=\"1.0\"?><![CDATA[x]]>";
        let items = tree(src);
        assert!(matches!(items[0], Item::Default(_)));
        let Item::ProcessingInstruction { name, text, .. } = items[1] else { panic!("{items:?}") };
        assert_eq!((name.text(src), text.text(src)), ("php", " x "));
        assert!(matches!(items[2], Item::Default(_)));
        assert!(matches!(items[3], Item::Text(_)));
        let Item::Element(e) = &items[4] else { panic!("{items:?}") };
        assert_eq!(e.start.name.text(src), "?xml");
        assert_eq!(e.start.suffix.map(|s| s.text(src)), Some("?>"));
        assert!(matches!(items[5], Item::Cdata(_)));
    }
}
