//! Python's `re` character classes for `str` patterns, so ported regexes match the same text.

/// `\s`: Python's `str.isspace()`.
#[must_use]
pub fn is_space(c: char) -> bool {
    matches!(
        c,
        '\t'..='\r'
            | '\x1c'..='\x1f'
            | ' '
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}

/// `\d`: Unicode decimal digits. ASCII exactly; for other scripts an approximation (numeric
/// and not alphabetic) until Phase 3 ports the tokenizer's classes from Unicode data.
#[must_use]
pub(crate) fn is_digit(c: char) -> bool {
    c.is_ascii_digit() || (!c.is_ascii() && c.is_numeric() && !c.is_alphabetic())
}

/// `\w`: alphanumerics and underscore.
#[must_use]
pub(crate) fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Length in bytes of the leading run of chars matching `pred`.
#[must_use]
pub(crate) fn run_len(s: &str, pred: impl Fn(char) -> bool) -> usize {
    s.char_indices().find(|&(_, c)| !pred(c)).map_or(s.len(), |(i, _)| i)
}
