//! Python's `re` character classes for `str` patterns, so ported regexes match the same text.
//!
//! The tables in `pychars_table.rs` are generated from Python's own `re` module
//! (`scripts/gen_pychars.py`), so `\s`, `\d` and `\w` here mean exactly what they mean in
//! Chameleon's regexes, which differ from Rust's: Python's `\s` includes `\x1c`-`\x1f`, and its
//! `\w` is `str.isalnum()` plus `_` (no combining marks, all numerics).

use crate::pychars_table::{DIGIT, SPACE, WORD};

fn in_table(table: &[(char, char)], c: char) -> bool {
    table
        .binary_search_by(|&(lo, hi)| {
            if hi < c {
                std::cmp::Ordering::Less
            } else if lo > c {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// `\s`: Python's `str.isspace()`.
#[must_use]
pub fn is_space(c: char) -> bool {
    if c.is_ascii() {
        return matches!(c, '\t'..='\r' | '\x1c'..=' ');
    }
    in_table(&SPACE, c)
}

/// `\d`: Unicode decimal digits (`str.isdecimal()`).
#[must_use]
pub(crate) fn is_digit(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_digit();
    }
    in_table(&DIGIT, c)
}

/// `\w`: `str.isalnum()` or `_`.
#[must_use]
pub(crate) fn is_word(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_alphanumeric() || c == '_';
    }
    in_table(&WORD, c)
}

/// Which of Python's shorthand classes a regex escape names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Class {
    Space,
    Digit,
    Word,
}

impl Class {
    fn table(self) -> &'static [(char, char)] {
        match self {
            Self::Space => &SPACE,
            Self::Digit => &DIGIT,
            Self::Word => &WORD,
        }
    }

    /// The class's ranges in regex class syntax (without the brackets), for example
    /// `\x{9}-\x{d}\x{1c}-\x{20}...`.
    pub(crate) fn regex_body(self) -> String {
        use std::fmt::Write;
        let mut out = String::new();
        for &(lo, hi) in self.table() {
            let _ = if lo == hi {
                write!(out, "\\x{{{:x}}}", u32::from(lo))
            } else {
                write!(out, "\\x{{{:x}}}-\\x{{{:x}}}", u32::from(lo), u32::from(hi))
            };
        }
        out
    }

    /// Every character in the class, for tests.
    pub fn chars(self) -> impl Iterator<Item = char> {
        self.table().iter().flat_map(|&(lo, hi)| lo..=hi)
    }
}

/// Length in bytes of the leading run of chars matching `pred`.
#[must_use]
pub(crate) fn run_len(s: &str, pred: impl Fn(char) -> bool) -> usize {
    s.char_indices().find(|&(_, c)| !pred(c)).map_or(s.len(), |(i, _)| i)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn predicates_agree_with_the_tables() {
        for class in [Class::Space, Class::Digit, Class::Word] {
            let pred: fn(char) -> bool = match class {
                Class::Space => is_space,
                Class::Digit => is_digit,
                Class::Word => is_word,
            };
            let members: std::collections::HashSet<char> = class.chars().collect();
            for c in (0..=0x10_ffff).filter_map(char::from_u32) {
                assert_eq!(pred(c), members.contains(&c), "{class:?} {c:?}");
            }
        }
    }

    #[test]
    fn python_whitespace_matches_str_isspace() {
        for c in ['\u{1c}', '\u{85}', '\u{a0}', '\u{3000}', '\x0b'] {
            assert!(is_space(c), "{c:?}");
        }
        for c in ['\u{200b}', 'a', '\u{180e}'] {
            assert!(!is_space(c), "{c:?}");
        }
        // Python's \w has no combining marks; \d is decimal digits only.
        assert!(!is_word('\u{301}'));
        assert!(is_word('\u{b2}')); // SUPERSCRIPT TWO is numeric
        assert!(!is_digit('\u{b2}'));
        assert!(is_digit('\u{663}'));
    }
}
