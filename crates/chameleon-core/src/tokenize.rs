// Ported from Chameleon's src/chameleon/tokenize.py (itself based on Paul Prescod's XML
// shallow-parsing recipe, PSF License), modified: a hand-written subset of the `XML_SPE`
// regex for the Phase 2 walking skeleton. Phase 3 replaces this with a verbatim port of the
// regex (fancy-regex); the output shape (contiguous byte spans covering the input) stays.

//! Splits template source into tokens: runs of text and markup (`<...>`) pieces.
//!
//! Like Chameleon's tokenizer this never fails: every byte belongs to exactly one token and
//! the tokens concatenate back to the input. Classifying tokens (and rejecting malformed
//! ones) is the parser's job.

use crate::pychars::is_space;
use crate::source::Span;

/// Tokenizes `src` into contiguous spans. `src` must satisfy [`crate::source::check_len`].
#[must_use]
pub(crate) fn tokenize(src: &str) -> Vec<Span> {
    let b = src.as_bytes();
    let mut tokens = Vec::new();
    let mut i = 0;
    while i < b.len() {
        let end = if b[i] == b'<' { markup(b, i) } else { memchr::memchr(b'<', &b[i..]).map_or(b.len(), |n| i + n) };
        tokens.push(Span::new(i, end));
        i = end;
    }
    tokens
}

/// One `MarkupSPE` token starting at `b[i] == b'<'`; returns its end.
fn markup(b: &[u8], i: usize) -> usize {
    let j = i + 1;
    match b.get(j) {
        Some(b'!') => {
            if b[j..].starts_with(b"!--") {
                // `--(?:CommentCE)?`: up to the first `--` after `<!--`, plus an optional `>`.
                let body = j + 3;
                match memchr::memmem::find(&b[body..], b"--") {
                    Some(n) => {
                        let end = body + n + 2;
                        if b.get(end) == Some(&b'>') { end + 1 } else { end }
                    }
                    None => body,
                }
            } else {
                // Declarations, doctypes and CDATA: not supported by the skeleton; take them up to
                // the next `>` so the parser can reject them with a position.
                until_after(b, j, b'>')
            }
        }
        Some(b'?') => match memchr::memmem::find(&b[j..], b"?>") {
            Some(n) => j + n + 2,
            None => b.len(),
        },
        Some(b'/') => {
            // `/(?:EndTagCE)?` with EndTagCE = `Name (S)? >?`.
            let Some(mut p) = name(b, j + 1) else { return j + 1 };
            p = skip_s(b, p);
            if b.get(p) == Some(&b'>') { p + 1 } else { p }
        }
        _ => elem_tag(b, j).unwrap_or(j),
    }
}

/// `ElemTagCE`: `(Name)(?:(S)(Name)(((?:S)?=(?:S)?)(?:AttValSE|Simple)|(?!(?:S)?=)))*(?:S)?(/?>)?`.
fn elem_tag(b: &[u8], start: usize) -> Option<usize> {
    let mut p = name(b, start)?;
    loop {
        let save = p;
        let s_end = skip_s(b, p);
        if s_end == p {
            break;
        }
        let Some(n_end) = name(b, s_end) else {
            p = save;
            break;
        };
        let eq = skip_s(b, n_end);
        if b.get(eq) == Some(&b'=') {
            let v = skip_s(b, eq + 1);
            if let Some(end) = att_value(b, v).or_else(|| simple(b, v)) {
                p = end;
            } else {
                p = save;
                break;
            }
        } else {
            p = n_end;
        }
    }
    p = skip_s(b, p);
    if b[p..].starts_with(b"/>") {
        Some(p + 2)
    } else if b.get(p) == Some(&b'>') {
        Some(p + 1)
    } else {
        Some(p)
    }
}

/// `AttValSE`: `"[^"]*"|'[^']*'|[^\s=<>\x60]+` (Python `\s`).
fn att_value(b: &[u8], p: usize) -> Option<usize> {
    if let Some(&q @ (b'"' | b'\'')) = b.get(p)
        && let Some(n) = memchr::memchr(q, &b[p + 1..])
    {
        return Some(p + 1 + n + 1);
    }
    let end = scan_chars(b, p, |c| !is_space(c) && !matches!(c, '=' | '<' | '>' | '`'));
    (end > p).then_some(end)
}

/// `Simple`: `[^"'>/]+`.
fn simple(b: &[u8], p: usize) -> Option<usize> {
    let end = p + b[p..].iter().take_while(|&&c| !matches!(c, b'"' | b'\'' | b'>' | b'/')).count();
    (end > p).then_some(end)
}

/// `Name`: `(?:[A-Za-z_:@]|[^\x00-\x7F])(?:[A-Za-z0-9_:.-]|[^\x00-\x7F])*`. Non-ASCII bytes all
/// qualify, so this can work on bytes and still end on a char boundary.
fn name(b: &[u8], p: usize) -> Option<usize> {
    let first = *b.get(p)?;
    if !(first.is_ascii_alphabetic() || matches!(first, b'_' | b':' | b'@') || first >= 0x80) {
        return None;
    }
    let rest = b[p + 1..]
        .iter()
        .take_while(|&&c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b':' | b'.' | b'-') || c >= 0x80)
        .count();
    Some(p + 1 + rest)
}

/// `S`: `[ \n\t\r]*` (ASCII only, unlike `\s`).
fn skip_s(b: &[u8], p: usize) -> usize {
    p + b[p..].iter().take_while(|&&c| matches!(c, b' ' | b'\n' | b'\t' | b'\r')).count()
}

fn until_after(b: &[u8], p: usize, stop: u8) -> usize {
    memchr::memchr(stop, &b[p..]).map_or(b.len(), |n| p + n + 1)
}

/// Advances over UTF-8 chars while `pred` holds. `p` must be on a char boundary.
pub(crate) fn scan_chars(b: &[u8], p: usize, pred: impl Fn(char) -> bool) -> usize {
    let Ok(rest) = std::str::from_utf8(&b[p..]) else { return p };
    p + rest.char_indices().find(|&(_, c)| !pred(c)).map_or(rest.len(), |(n, _)| n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn tokens(src: &str) -> Vec<&str> {
        tokenize(src).into_iter().map(|s| s.text(src)).collect()
    }

    #[test]
    fn splits_text_tags_and_comments() {
        insta::assert_debug_snapshot!(tokens(
            "<div class=\"a\" id='b' c>x &amp; ${y}</div><!-- c --><br/>a < b</p >"
        ), @r#"
        [
            "<div class=\"a\" id='b' c>",
            "x &amp; ${y}",
            "</div>",
            "<!-- c -->",
            "<br/>",
            "a ",
            "<",
            " b",
            "</p >",
        ]
        "#);
    }

    #[test]
    fn quoted_values_may_contain_markup() {
        assert_eq!(tokens("<a title=\"<b>\" x='>'>t"), ["<a title=\"<b>\" x='>'>", "t"]);
    }

    #[test]
    fn comment_ends_at_first_double_hyphen() {
        assert_eq!(tokens("<!-- a -- b -->"), ["<!-- a --", " b -->"]);
        assert_eq!(tokens("<!---->x"), ["<!---->", "x"]);
        assert_eq!(tokens("<!--->"), ["<!--", "->"]);
    }

    #[test]
    fn python_whitespace_matches_str_isspace() {
        for c in ['\u{1c}', '\u{85}', '\u{a0}', '\u{3000}', '\x0b'] {
            assert!(crate::pychars::is_space(c), "{c:?}");
        }
        for c in ['\u{200b}', 'a', '\u{180e}'] {
            assert!(!crate::pychars::is_space(c), "{c:?}");
        }
    }

    proptest! {
        #[test]
        fn tokens_cover_input(src in "[<>a-z /=\"'!-]{0,40}|\\PC{0,40}") {
            let spans = tokenize(&src);
            let mut at = 0;
            for span in &spans {
                prop_assert_eq!(span.start as usize, at);
                prop_assert!(span.end > span.start);
                prop_assert!(src.is_char_boundary(span.end as usize));
                at = span.end as usize;
            }
            prop_assert_eq!(at, src.len());
        }
    }
}
