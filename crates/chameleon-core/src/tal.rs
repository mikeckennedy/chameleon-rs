// Ported from Chameleon's src/chameleon/tal.py (`split_parts`, `parse_defines`,
// `parse_substitution`, `WHITELIST`) and metal.py / i18n.py (`WHITELIST`), modified:
// rewritten in Rust without regular expressions.

//! Parsing the values of TAL attributes.

use crate::pychars::{is_space, run_len};

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

pub(crate) const I18N_WHITELIST: &[&str] = &[
    "translate",
    "domain",
    "context",
    "target",
    "source",
    "attributes",
    "data",
    "name",
    "mode",
    "xmlns",
    "xml",
    "comment",
    "ignore",
    "ignore-attributes",
];

/// `split_parts`: split at single `;`, turn `;;` into `;`. A `;` that ends an entity reference
/// is doubled first so it survives.
pub(crate) fn split_parts(arg: &str) -> Vec<String> {
    let mut arg = arg.to_owned();
    let mut i = 0;
    while let Some(end) = find_entity_end(&arg, i) {
        arg.insert(end, ';');
        i = end + 1;
    }
    let arg = arg.replace(";;", "\0");
    let mut parts: Vec<String> = arg.split(';').map(|p| p.replace('\0', ";")).collect();
    if parts.len() > 1 && parts.last().is_some_and(|p| p.chars().all(is_space)) {
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

/// `parse_substitution`: `\s*(?:(text|structure)\s+)?(.*)\Z`. Returns whether the key is
/// `structure`, and the expression.
pub(crate) fn parse_substitution(clause: &str) -> (bool, &str) {
    let s = clause.get(run_len(clause, is_space)..).unwrap_or_default();
    for (key, structure) in [("text", false), ("structure", true)] {
        if let Some(rest) = s.strip_prefix(key) {
            let ws = run_len(rest, is_space);
            if ws > 0 {
                return (structure, rest.get(ws..).unwrap_or_default());
            }
        }
    }
    (false, s)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Define {
    pub(crate) global: bool,
    pub(crate) names: Vec<String>,
    pub(crate) expr: String,
}

/// `parse_defines`. `Err` carries the part that did not match (`"Invalid define syntax"`).
pub(crate) fn parse_defines(clause: &str) -> Result<Vec<Define>, String> {
    split_parts(clause).into_iter().map(|part| parse_define(&part).ok_or(part)).collect()
}

/// `DEFINE_RE`: `(?s)\s*(?:(global|local)\s+)?(NAME|\(NAME(?:,\s*NAME)*\))\s+(.*)\Z`, with
/// `NAME = [a-zA-Z_][-a-zA-Z0-9_]*`.
fn parse_define(part: &str) -> Option<Define> {
    let s = part.get(run_len(part, is_space)..)?;
    for context in ["global", "local"] {
        if let Some(rest) = s.strip_prefix(context) {
            let ws = run_len(rest, is_space);
            if ws > 0
                && let Some((names, expr)) = names_and_expr(rest.get(ws..)?)
            {
                return Some(Define { global: context == "global", names, expr });
            }
        }
    }
    let (names, expr) = names_and_expr(s)?;
    Some(Define { global: false, names, expr })
}

fn names_and_expr(s: &str) -> Option<(Vec<String>, String)> {
    let (names, rest) = if let Some(inner) = s.strip_prefix('(') {
        let mut names = Vec::new();
        let mut rest = inner;
        loop {
            let n = name_len(rest)?;
            names.push(rest.get(..n)?.to_owned());
            rest = rest.get(n..)?;
            if let Some(after) = rest.strip_prefix(',') {
                rest = after.get(run_len(after, is_space)..)?;
            } else {
                rest = rest.strip_prefix(')')?;
                break;
            }
        }
        (names, rest)
    } else {
        let n = name_len(s)?;
        (vec![s.get(..n)?.to_owned()], s.get(n..)?)
    };
    let ws = run_len(rest, is_space);
    if ws == 0 {
        return None;
    }
    Some((names, rest.get(ws..)?.to_owned()))
}

fn name_len(s: &str) -> Option<usize> {
    let first = s.chars().next()?;
    if !(first.is_ascii_alphabetic() || first == '_') {
        return None;
    }
    Some(1 + run_len(s.get(1..)?, |c| c.is_ascii_alphanumeric() || c == '_' || c == '-'))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn define(names: &[&str], expr: &str) -> Define {
        Define { global: false, names: names.iter().map(|s| (*s).to_owned()).collect(), expr: expr.to_owned() }
    }

    #[test]
    fn defines() {
        assert_eq!(parse_defines("hello lovely").unwrap(), [define(&["hello"], "lovely")]);
        assert_eq!(parse_defines("local hello lovely").unwrap(), [define(&["hello"], "lovely")]);
        assert_eq!(parse_defines("local items").unwrap(), [define(&["local"], "items")]);
        assert!(parse_defines("global hello lovely").unwrap()[0].global);
        assert_eq!(parse_defines("(hello, howdy) lovely").unwrap(), [define(&["hello", "howdy"], "lovely")]);
        assert_eq!(parse_defines("\u{a0}hello lovely").unwrap(), [define(&["hello"], "lovely")]);
        assert_eq!(
            parse_defines("hello lovely; tea time").unwrap(),
            [define(&["hello"], "lovely"), define(&["tea"], "time")]
        );
        assert!(parse_defines("( a, b) c").is_err());
    }

    #[test]
    fn split_parts_handles_entities_and_doubled_semicolons() {
        assert_eq!(split_parts("a;; b; c;"), ["a; b", " c"]);
        assert_eq!(split_parts("x &amp; y; z"), ["x &amp; y", " z"]);
    }

    #[test]
    fn substitution_keys() {
        assert_eq!(parse_substitution(" structure x"), (true, "x"));
        assert_eq!(parse_substitution("text  x"), (false, "x"));
        assert_eq!(parse_substitution("textual"), (false, "textual"));
    }
}
