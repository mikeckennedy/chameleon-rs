//! Running Chameleon's Python regexes verbatim on Rust engines.
//!
//! Patterns without lookaround or backreferences run on the `regex` crate, whose
//! leftmost-first matches (and groups) are the ones Python's backtracking `re` reports.
//! fancy-regex, which also has lookaround and backreferences, runs the full patterns as test
//! references only (see [`compile`] for why). What differs is syntax and the meaning of the shorthand classes, so a
//! pattern written for Python is translated before it is compiled:
//!
//! - `\s`, `\d`, `\w` (and `\S`, `\D`, `\W` outside classes) become explicit classes with
//!   Python's code points ([`crate::pychars`]);
//! - inside a class, `[`, `&` and `~` are escaped (Rust gives them meaning there: nested
//!   classes and set operations; Python treats them as literals);
//! - outside a class, a `]` is escaped (a literal in Python);
//! - `re.DOTALL` becomes the `s` flag.
//!

#[cfg(test)]
use fancy_regex::{Regex, RegexBuilder};

use crate::pychars::Class;

/// Translates a Python `str` pattern to Rust regex syntax (see the module docs).
///
/// # Errors
///
/// A negated shorthand class (`\S`, `\D`, `\W`) inside a character class, which the patterns
/// we port never use and which has no simple translation.
pub(crate) fn translate(pattern: &str, dotall: bool) -> Result<String, String> {
    let mut out = String::with_capacity(pattern.len() + 16);
    if dotall {
        out.push_str("(?s)");
    }
    let mut chars = pattern.chars().peekable();
    // Inside `[...]`: `Some(n)` where n counts the chars seen since the `[` (and `^`), so a
    // `]` right after the opening is a literal, as in Python.
    let mut class: Option<usize> = None;
    while let Some(c) = chars.next() {
        match (c, class) {
            ('\\', _) => {
                let Some(e) = chars.next() else {
                    out.push('\\');
                    break;
                };
                let shorthand = match e {
                    's' | 'S' => Some(Class::Space),
                    'd' | 'D' => Some(Class::Digit),
                    'w' | 'W' => Some(Class::Word),
                    _ => None,
                };
                match (shorthand, class) {
                    (Some(sh), None) => {
                        out.push_str(if e.is_ascii_uppercase() { "[^" } else { "[" });
                        out.push_str(&sh.regex_body());
                        out.push(']');
                    }
                    (Some(_), Some(_)) if e.is_ascii_uppercase() => {
                        return Err(format!("\\{e} inside a character class is not supported"));
                    }
                    (Some(sh), Some(_)) => out.push_str(&sh.regex_body()),
                    (None, _) => {
                        out.push('\\');
                        out.push(e);
                    }
                }
                if let Some(n) = class.as_mut() {
                    *n += 1;
                }
            }
            ('[', None) => {
                out.push('[');
                if chars.peek() == Some(&'^') {
                    chars.next();
                    out.push('^');
                }
                class = Some(0);
            }
            (']', Some(n)) if n > 0 => {
                out.push(']');
                class = None;
            }
            (']', Some(_)) => {
                out.push_str("\\]");
                class = Some(1);
            }
            (']', None) => out.push_str("\\]"),
            ('[' | '&' | '~', Some(n)) => {
                out.push('\\');
                out.push(c);
                class = Some(n + 1);
            }
            (_, Some(n)) => {
                out.push(c);
                class = Some(n + 1);
            }
            (_, None) => out.push(c),
        }
    }
    Ok(out)
}

/// Compiles a Python pattern on fancy-regex (the test references). Panics only on a bad
/// built-in pattern, which the unit tests compile; callers hold the result in a `LazyLock`.
///
/// fancy-regex's backtracking VM has a fixed stack of 1,000,000 entries (not configurable in
/// 0.19): a match that needs more fails with `StackOverflow` where Python's `re` succeeds.
/// That is why production matching uses [`compile_plain`] (patterns without lookaround or
/// backreferences) and hand-written matchers (the two that have them), and fancy-regex only
/// checks them in tests, whose callers must treat its errors as errors.
#[cfg(test)]
pub(crate) fn compile(pattern: &str, dotall: bool) -> Regex {
    let translated = translate(pattern, dotall).unwrap_or_else(|e| panic!("bad built-in pattern: {e}"));
    RegexBuilder::new(&translated)
        .backtrack_limit(usize::MAX)
        .build()
        .unwrap_or_else(|e| panic!("bad built-in pattern {pattern:?}: {e}"))
}

/// Compiles a Python pattern that has no lookaround or backreferences on the `regex` crate:
/// linear time, no stack, and the same match and groups a backtracking engine reports
/// (leftmost-first). Panics only on a bad built-in pattern, as [`compile`].
pub(crate) fn compile_plain(pattern: &str, dotall: bool) -> regex::Regex {
    let translated = translate(pattern, dotall).unwrap_or_else(|e| panic!("bad built-in pattern: {e}"));
    regex::Regex::new(&translated).unwrap_or_else(|e| panic!("bad built-in pattern {pattern:?}: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shorthand_classes_use_python_code_points() {
        let re = compile(r"\s", false);
        assert!(re.is_match("\u{1c}").unwrap());
        let re = compile(r"[^\s=]+", false);
        assert_eq!(re.find("ab\u{1f}c").unwrap().unwrap().as_str(), "ab");
        assert!(!compile(r"\w", false).is_match("\u{301}").unwrap());
    }

    #[test]
    fn class_literals_are_escaped() {
        assert_eq!(translate("[]a]", false).unwrap(), "[\\]a]");
        assert_eq!(translate("[^]a]", false).unwrap(), "[^\\]a]");
        assert_eq!(translate("a]", false).unwrap(), "a\\]");
        assert_eq!(translate("[[&&~]", false).unwrap(), "[\\[\\&\\&\\~]");
        assert!(compile("[[&&~]+", false).is_match("&").unwrap());
        assert!(translate(r"[\S]", false).is_err());
    }

    #[test]
    fn dotall() {
        assert!(compile("a.b", true).is_match("a\nb").unwrap());
        assert!(!compile("a.b", false).is_match("a\nb").unwrap());
    }
}
