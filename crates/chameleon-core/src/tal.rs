// Ported from Chameleon's src/chameleon/tal.py (`split_parts`, `parse_attributes`,
// `parse_substitution`, `parse_defines`, `prepare_attributes`, `WHITELIST`) and metal.py
// (`WHITELIST`), modified: rewritten in Rust without regular expressions, over `Tok` so
// positions follow Chameleon's `Token` arithmetic.

//! Parsing the values of TAL attributes.

use crate::pychars::{is_space, run_len};
use crate::tok::Tok;
use crate::vm::Visibility;

/// TAL attributes Chameleon accepts at all (anything else is a `CompilationError`).
pub(crate) const TAL_WHITELIST: &[&str] = &[
    "define",
    "comment",
    "condition",
    "content",
    "replace",
    "repeat",
    "attributes",
    "on-error",
    "omit-tag",
    "script",
    "switch",
    "case",
    "xmlns",
    "xml",
];

pub(crate) const METAL_WHITELIST: &[&str] =
    &["define-macro", "extend-macro", "use-macro", "define-slot", "fill-slot", "xmlns", "xml"];

/// `split_parts`: split at single `;`, turn `;;` into `;`. A `;` that ends an entity reference
/// is doubled first so it survives. Positions follow `Token.split` (see [`Tok::split`]).
pub(crate) fn split_parts(arg: &Tok) -> Vec<Tok> {
    let mut text = arg.text.clone();
    let mut i = 0;
    while let Some(end) = find_entity_end(&text, i) {
        text.insert(end, ';');
        i = end + 1;
    }
    let text = text.replace(";;", "\0");
    let mut parts: Vec<Tok> =
        arg.with_text(text).split(";").into_iter().map(|p| p.with_text(p.text.replace('\0', ";"))).collect();
    if parts.len() > 1 && parts.last().is_some_and(|p| p.text.chars().all(is_space)) {
        parts.pop();
    }
    parts
}

/// End (exclusive, a byte offset in `s`) of the first match of `&(#?)(x?)(\d{1,5}|\w{1,8});`
/// at or after `from`.
fn find_entity_end(s: &str, from: usize) -> Option<usize> {
    let mut at = from;
    while let Some(i) = s.get(at..)?.find('&') {
        let amp = at + i;
        let tail = s.get(amp + 1..)?;
        if let Some(len) = crate::entities::reference_len(tail) {
            return Some(amp + 1 + len);
        }
        at = amp + 1;
    }
    None
}

/// `parse_substitution`: `\s*(?:(text|structure)\s+)?(.*)\Z` (DOTALL). Returns the key
/// (`None` when absent, which means `text`) and the expression.
pub(crate) fn parse_substitution(clause: &Tok) -> (Option<Tok>, Tok) {
    let s = &clause.text;
    let lead = run_len(s, is_space);
    let rest = &s[lead..];
    for key in ["text", "structure"] {
        if let Some(after) = rest.strip_prefix(key) {
            let ws = run_len(after, is_space);
            if ws > 0 {
                let key_tok = clause.slice(lead, lead + key.len());
                return (Some(key_tok), clause.slice_from(lead + key.len() + ws));
            }
        }
    }
    (None, clause.slice_from(lead))
}

/// Whether a `parse_substitution` key selects `structure` (no escaping).
pub(crate) fn content_kind(key: Option<&Tok>) -> crate::nodes::ContentKind {
    if key.is_some_and(|k| k.text == "structure") {
        crate::nodes::ContentKind::Structure
    } else {
        crate::nodes::ContentKind::Text
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Define {
    pub(crate) visibility: Visibility,
    pub(crate) names: Vec<Tok>,
    pub(crate) expr: Tok,
}

/// `parse_defines`. `Err` carries the part that did not match (`"Invalid define syntax"`).
pub(crate) fn parse_defines(clause: &Tok) -> Result<Vec<Define>, Tok> {
    split_parts(clause).into_iter().map(|part| parse_define(&part).ok_or(part)).collect()
}

/// `DEFINE_RE`: `(?s)\s*(?:(global|local)\s+)?(NAME|\(NAME(?:,\s*NAME)*\))\s+(.*)\Z`, with
/// `NAME = [a-zA-Z_][-a-zA-Z0-9_]*`. Names in parentheses are `name.strip('()').split(',')`,
/// each stripped, with `Token`'s positions.
fn parse_define(part: &Tok) -> Option<Define> {
    let s = part.text.as_str();
    let lead = run_len(s, is_space);
    for context in ["global", "local"] {
        if let Some(rest) = s[lead..].strip_prefix(context) {
            let ws = run_len(rest, is_space);
            if ws > 0
                && let Some(d) = names_and_expr(
                    part,
                    lead + context.len() + ws,
                    if context == "global" { Visibility::Global } else { Visibility::Local },
                )
            {
                return Some(d);
            }
        }
    }
    names_and_expr(part, lead, Visibility::Local)
}

/// The `(NAME|\(NAME(?:,\s*NAME)*\))\s+(.*)` part, matched at byte `at` of `part`.
fn names_and_expr(part: &Tok, at: usize, visibility: Visibility) -> Option<Define> {
    let s = part.text.as_str();
    let rest = &s[at..];
    let name_end = if let Some(inner) = rest.strip_prefix('(') {
        let mut i = 1 + name_len(inner)?;
        loop {
            let tail = &rest[i..];
            if let Some(after) = tail.strip_prefix(',') {
                let ws = run_len(after, is_space);
                i += 1 + ws + name_len(&after[ws..])?;
            } else {
                tail.strip_prefix(')')?;
                break i + 1;
            }
        }
    } else {
        name_len(rest)?
    };
    let ws = run_len(&rest[name_end..], is_space);
    if ws == 0 {
        return None;
    }
    let name = part.slice(at, at + name_end);
    let names = if name.text.starts_with('(') {
        name.strip_chars(&['(', ')']).split(",").iter().map(Tok::strip).collect()
    } else {
        vec![name]
    };
    Some(Define { visibility, names, expr: part.slice_from(at + name_end + ws) })
}

fn name_len(s: &str) -> Option<usize> {
    let first = s.chars().next()?;
    if !(first.is_ascii_alphabetic() || first == '_') {
        return None;
    }
    Some(1 + run_len(&s[1..], |c| c.is_ascii_alphanumeric() || c == '_' || c == '-'))
}

/// `parse_attributes` (`tal:attributes`): `(name, expression)` pairs, `name` `None` for a
/// dictionary expression. `Err` carries the part with a duplicate name.
pub(crate) fn parse_attributes(clause: &Tok) -> Result<Vec<(Option<Tok>, Tok)>, Tok> {
    let mut attrs: Vec<(Option<Tok>, Tok)> = Vec::new();
    for part in split_parts(clause) {
        let (name, expr) = match match_attr(&part) {
            Some((name, expr)) => (Some(name), expr),
            None => (None, part.strip()),
        };
        if attrs.iter().any(|(seen, _)| seen.as_ref().map(Tok::as_str) == name.as_ref().map(Tok::as_str)) {
            return Err(part);
        }
        attrs.push((name, expr));
    }
    Ok(attrs)
}

/// `ATTR_RE`: `\s*([^\s{}'"]+)\s+([^\s].*)\Z` (DOTALL). The name run is maximal (it cannot
/// contain whitespace, so backtracking cannot help), whitespace must follow it, and then a
/// non-whitespace character.
fn match_attr(part: &Tok) -> Option<(Tok, Tok)> {
    let s = part.text.as_str();
    let lead = run_len(s, is_space);
    let n = run_len(&s[lead..], |c| !(is_space(c) || matches!(c, '{' | '}' | '\'' | '"')));
    if n == 0 {
        return None;
    }
    let ws = run_len(&s[lead + n..], is_space);
    let expr_at = lead + n + ws;
    if ws == 0 || expr_at == s.len() {
        return None;
    }
    Some((part.slice(lead, lead + n), part.slice_from(expr_at)))
}

/// An attribute as written in the tag, after `data-` attributes are removed.
#[derive(Debug, Clone)]
pub(crate) struct RawAttr {
    pub(crate) name: Tok,
    pub(crate) value: Tok,
    pub(crate) quote: String,
    pub(crate) space: String,
    pub(crate) eq: String,
}

/// One entry of `prepare_attributes`' result: `(name, text, quote, space, eq, expr)`.
#[derive(Debug, Clone)]
pub(crate) struct Prepared {
    /// `None` for a `tal:attributes` dictionary expression.
    pub(crate) name: Option<Tok>,
    /// The static value, if the attribute is written in the tag (or added by
    /// `i18n:attributes`).
    pub(crate) text: Option<Tok>,
    pub(crate) quote: String,
    pub(crate) space: String,
    pub(crate) eq: String,
    /// The `tal:attributes` expression.
    pub(crate) expr: Option<Tok>,
}

/// `prepare_attributes`. `drop` holds the names of the attributes to leave out (computed by
/// the caller from Chameleon's positional pairing of attributes and namespaced keys).
///
/// Chameleon records a new `tal:attributes` name at index `len(attributes) - 1` *before*
/// inserting it at `len(attributes)`, so a later name that differs only in case replaces the
/// attribute before it (`tal:attributes="title 1; Title 2" a="x" b="y"` renders
/// `a="x" Title="2" title="1"`). That is reproduced.
pub(crate) fn prepare_attributes(
    attrs: &[RawAttr],
    dynamic: Vec<(Option<Tok>, Tok)>,
    i18n: &[(Tok, Option<Tok>)],
    drop: &[String],
) -> Vec<Prepared> {
    let mut attributes: Vec<Prepared> = Vec::new();
    // `normalized`: lower-cased name to a Python list index (which may be -1, see above).
    let mut normalized: Vec<(String, isize)> = Vec::new();
    let set = |normalized: &mut Vec<(String, isize)>, key: String, index: isize| match normalized
        .iter_mut()
        .find(|(k, _)| *k == key)
    {
        Some(entry) => entry.1 = index,
        None => normalized.push((key, index)),
    };
    let get = |normalized: &[(String, isize)], key: &str| normalized.iter().find(|(k, _)| k == key).map(|e| e.1);
    let len = |v: &Vec<Prepared>| isize::try_from(v.len()).unwrap_or(isize::MAX);
    for attr in attrs {
        if drop.contains(&attr.name.text) {
            continue;
        }
        attributes.push(Prepared {
            name: Some(attr.name.clone()),
            text: Some(attr.value.clone()),
            quote: attr.quote.clone(),
            space: attr.space.clone(),
            eq: attr.eq.clone(),
            expr: None,
        });
        set(&mut normalized, attr.name.text.to_lowercase(), len(&attributes) - 1);
    }
    for (name, expr) in dynamic {
        let found = name.as_ref().and_then(|n| get(&normalized, &n.text.to_lowercase()));
        // Python indexing: -1 is the last item. Every recorded index is valid when looked up.
        let found = found.and_then(|i| usize::try_from(if i < 0 { len(&attributes) + i } else { i }).ok());
        if let Some(old) = found.and_then(|i| attributes.get(i)) {
            let replacement = Prepared {
                name,
                text: old.text.clone(),
                quote: old.quote.clone(),
                space: old.space.clone(),
                eq: old.eq.clone(),
                expr: Some(expr),
            };
            if let Some(i) = found {
                attributes[i] = replacement;
            }
        } else {
            if let Some(n) = &name {
                // `len(attributes) - 1`, recorded before the insert (see above).
                set(&mut normalized, n.text.to_lowercase(), len(&attributes) - 1);
            }
            attributes.push(Prepared {
                name,
                text: None,
                quote: "\"".to_owned(),
                space: " ".to_owned(),
                eq: "=".to_owned(),
                expr: Some(expr),
            });
        }
    }
    for (name, _) in i18n {
        let key = name.text.to_lowercase();
        if get(&normalized, &key).is_none() {
            attributes.push(Prepared {
                name: Some(name.clone()),
                text: Some(name.clone()),
                quote: "\"".to_owned(),
                space: " ".to_owned(),
                eq: "=".to_owned(),
                expr: None,
            });
            set(&mut normalized, key, len(&attributes) - 1);
        }
    }
    attributes
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tok::TokPos;

    fn tok(s: &str) -> Tok {
        Tok { text: s.to_owned(), pos: Some(TokPos::at(0)) }
    }

    fn text(t: &[Tok]) -> Vec<&str> {
        t.iter().map(Tok::as_str).collect()
    }

    fn at(t: &Tok) -> u32 {
        t.pos.map_or(u32::MAX, |p| p.chars)
    }

    #[test]
    fn defines() {
        let d = parse_defines(&tok("hello lovely")).unwrap();
        assert_eq!(
            (text(&d[0].names), d[0].expr.as_str(), d[0].visibility),
            (vec!["hello"], "lovely", Visibility::Local)
        );
        let d = parse_defines(&tok("local items")).unwrap();
        assert_eq!((text(&d[0].names), d[0].expr.as_str()), (vec!["local"], "items"));
        assert_eq!(parse_defines(&tok("global hello lovely")).unwrap()[0].visibility, Visibility::Global);
        let d = parse_defines(&tok("(hello,  howdy) lovely")).unwrap();
        assert_eq!(text(&d[0].names), ["hello", "howdy"]);
        // `strip('()')` then `split(',')`: the second name's position does not count the comma.
        assert_eq!(d[0].names.iter().map(at).collect::<Vec<_>>(), [1, 8]);
        assert_eq!(text(&parse_defines(&tok("\u{a0}hello lovely")).unwrap()[0].names), ["hello"]);
        let d = parse_defines(&tok("x 1;y 2;z 3; econtext 1")).unwrap();
        // Chameleon reports `econtext` at 10 (three separators not counted), not 13.
        assert_eq!((d[3].names[0].as_str(), at(&d[3].names[0])), ("econtext", 10));
        assert_eq!(parse_defines(&tok("( a, b) c")).unwrap_err().as_str(), "( a, b) c");
        assert!(parse_defines(&tok("a.b c")).is_err());
    }

    #[test]
    fn split_parts_handles_entities_and_doubled_semicolons() {
        assert_eq!(text(&split_parts(&tok("a;; b; c;"))), ["a; b", " c"]);
        assert_eq!(text(&split_parts(&tok("x &amp; y; z"))), ["x &amp; y", " z"]);
    }

    #[test]
    fn substitution_keys() {
        let (key, expr) = parse_substitution(&tok(" structure x"));
        assert_eq!((key.map(|k| k.text), expr.as_str(), at(&expr)), (Some("structure".into()), "x", 11));
        assert_eq!(parse_substitution(&tok("text  x")).1.as_str(), "x");
        assert_eq!(parse_substitution(&tok("textual")), (None, tok("textual")));
    }

    #[test]
    fn attributes() {
        let a = parse_attributes(&tok("href url; title 'a b';  d")).unwrap();
        let names: Vec<_> = a.iter().map(|(n, e)| (n.as_ref().map(Tok::as_str), e.as_str())).collect();
        assert_eq!(names, [(Some("href"), "url"), (Some("title"), "'a b'"), (None, "d")]);
        assert_eq!(parse_attributes(&tok("a 1; a 2")).unwrap_err().as_str(), " a 2");
        assert!(parse_attributes(&tok("x; y")).is_err());
        assert_eq!(parse_attributes(&tok("dummy 'dummy'")).unwrap()[0].0.as_ref().map(Tok::as_str), Some("dummy"));
        assert_eq!(parse_attributes(&tok("{'a': 1}")).unwrap()[0].0, None);
    }

    fn raw(name: &str, value: &str) -> RawAttr {
        RawAttr { name: tok(name), value: tok(value), quote: "\"".into(), space: " ".into(), eq: "=".into() }
    }

    fn names(p: &[Prepared]) -> Vec<(String, Option<String>, Option<String>)> {
        p.iter()
            .map(|a| {
                let n = a.name.as_ref().map_or("-".into(), |n| n.text.clone());
                (n, a.text.as_ref().map(|t| t.text.clone()), a.expr.as_ref().map(|t| t.text.clone()))
            })
            .collect()
    }

    #[test]
    fn prepare_reproduces_the_index_quirk() {
        let dynamic = parse_attributes(&tok("title 1; Title 2")).unwrap();
        let p = prepare_attributes(&[raw("a", "x"), raw("b", "y")], dynamic, &[], &[]);
        let s = |v: &str| Some(v.to_owned());
        assert_eq!(
            names(&p),
            [("a".into(), s("x"), None), ("Title".into(), s("y"), s("2")), ("title".into(), None, s("1"))]
        );
        // With no static attributes, the first new name records -1: the last attribute.
        let dynamic = parse_attributes(&tok("title 1; Title 2")).unwrap();
        assert_eq!(names(&prepare_attributes(&[], dynamic, &[], &[])), [("Title".into(), None, s("2"))]);
        let p = prepare_attributes(&[raw("a", "x")], vec![], &[(tok("alt"), None), (tok("A"), None)], &[]);
        assert_eq!(names(&p), [("a".into(), s("x"), None), ("alt".into(), s("alt"), None)]);
    }
}
