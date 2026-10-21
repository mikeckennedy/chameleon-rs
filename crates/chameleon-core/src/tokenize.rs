// Ported from Chameleon's src/chameleon/tokenize.py (itself from Paul Prescod's XML
// shallow-parsing recipe, http://code.activestate.com/recipes/65125, PSF License), modified:
// the `recollector` patterns are rebuilt verbatim in Rust; markup branches run on the `regex`
// crate, `ElemTagCE` is matched by hand; tokens are byte spans instead of `Token` strings.

//! Splits template source into tokens: runs of text and markup (`<...>`) pieces.
//!
//! The `XML_SPE` pattern is assembled from Chameleon's named pieces exactly as its
//! `recollector` does, so the text is identical to Python's (a pytest checks this), and only
//! `\s` is translated to Python's code points ([`crate::pyre`]). [`tokenize`] explains how
//! it is matched without a backtracking engine. Like Chameleon's tokenizer
//! this never fails: every character belongs to exactly one token and the tokens
//! concatenate back to the input. Classifying tokens is the parser's job.

use std::sync::LazyLock;

use crate::source::Span;

/// Chameleon's `recollector` table, in order: each pattern may refer to the earlier ones with
/// `%(Name)s`, and `%%` is a literal `%` (Python's `%` formatting).
const PIECES: [(&str, &str); 28] = [
    ("TextSE", "[^<]+"),
    ("UntilHyphen", "[^-]*-"),
    ("Until2Hyphens", "%(UntilHyphen)s(?:[^-]%(UntilHyphen)s)*-"),
    ("CommentCE", "%(Until2Hyphens)s>?"),
    ("UntilRSBs", "[^\\]]*](?:[^\\]]+])*]+"),
    ("CDATA_CE", "%(UntilRSBs)s(?:[^\\]>]%(UntilRSBs)s)*>"),
    ("S", "[ \\n\\t\\r]+"),
    ("Simple", "[^\"'>/]+"),
    ("NameStrt", "[A-Za-z_:@]|[^\\x00-\\x7F]"),
    ("NameChar", "[A-Za-z0-9_:.-]|[^\\x00-\\x7F]"),
    ("Name", "(?:%(NameStrt)s)(?:%(NameChar)s)*"),
    ("QuoteSE", "\"[^\"]*\"|'[^']*'"),
    ("DT_IdentSE", "%(S)s%(Name)s(?:%(S)s(?:%(Name)s|%(QuoteSE)s))*"),
    ("MarkupDeclCE", "(?:[^\\]\"'><]+|%(QuoteSE)s)*>"),
    ("S1", "[\\n\\r\\t ]"),
    ("UntilQMs", "[^?]*\\?+"),
    ("PI_Tail", "\\?>|%(S1)s%(UntilQMs)s(?:[^>?]%(UntilQMs)s)*>"),
    (
        "DT_ItemSE",
        concat!("<(?:!(?:--%(Until2Hyphens)s>|[^-]%(MarkupDeclCE)s)|", "\\?%(Name)s(?:%(PI_Tail)s))|%%%(Name)s;|%(S)s"),
    ),
    ("DocTypeCE", "%(DT_IdentSE)s(?:%(S)s)?(?:\\[(?:%(DT_ItemSE)s)*](?:%(S)s)?)?>?"),
    ("DeclCE", concat!("--(?:%(CommentCE)s)?|\\[CDATA\\[(?:%(CDATA_CE)s)?|", "DOCTYPE(?:%(DocTypeCE)s)?")),
    ("PI_CE", "%(Name)s(?:%(PI_Tail)s)?"),
    ("EndTagCE", "%(Name)s(?:%(S)s)?>?"),
    ("AttValSE", r#"\"[^\"]*\"|'[^']*'|[^\s=<>`]+"#),
    (
        "ElemTagCE",
        concat!(
            "(%(Name)s)(?:(%(S)s)(%(Name)s)(((?:%(S)s)?=(?:%(S)s)?)",
            "(?:%(AttValSE)s|%(Simple)s)|(?!(?:%(S)s)?=)))*(?:%(S)s)?(/?>)?"
        ),
    ),
    ("MarkupSPE", concat!("<(?:!(?:%(DeclCE)s)?|", "\\?(?:%(PI_CE)s)?|/(?:%(EndTagCE)s)?|(?:%(ElemTagCE)s)?)")),
    ("XML_SPE", "%(TextSE)s|%(MarkupSPE)s"),
    ("XML_MARKUP_ONLY_SPE", "%(MarkupSPE)s"),
    ("ElemTagSPE", "<|%(Name)s"),
];

/// `collector.res[name]`: the expanded Python pattern (before translation to Rust syntax).
#[must_use]
pub fn pattern(name: &str) -> Option<String> {
    let mut res: Vec<(&str, String)> = Vec::with_capacity(PIECES.len());
    for (key, template) in PIECES {
        let expanded = expand(template, &res);
        if key == name {
            return Some(expanded);
        }
        res.push((key, expanded));
    }
    None
}

/// Python's `template % res` for `%(Name)s` and `%%`.
fn expand(template: &str, res: &[(&str, String)]) -> String {
    let mut out = String::with_capacity(template.len() * 4);
    let mut rest = template;
    while let Some(i) = rest.find('%') {
        out.push_str(&rest[..i]);
        let after = &rest[i + 1..];
        if let Some(tail) = after.strip_prefix('%') {
            out.push('%');
            rest = tail;
        } else if let Some(inner) = after.strip_prefix('(')
            && let Some(close) = inner.find(")s")
        {
            let key = &inner[..close];
            let value = res.iter().rev().find(|(k, _)| *k == key).map_or("", |(_, v)| v.as_str());
            out.push_str(value);
            rest = &inner[close + 2..];
        } else {
            out.push('%');
            rest = after;
        }
    }
    out.push_str(rest);
    out
}

/// A `MarkupSPE` branch, anchored, compiled with the `regex` crate: `^<` + `lead` +
/// `(?:%(piece)s)?`.
fn branch(lead: &str, piece: &str) -> regex::Regex {
    let p = pattern(piece).unwrap_or_else(|| panic!("unknown tokenizer pattern {piece}"));
    crate::pyre::compile_plain(&format!("^<{lead}(?:{p})?"), false)
}

/// `<!(?:DeclCE)?`: comments, CDATA, doctypes and other declarations.
static DECL: LazyLock<regex::Regex> = LazyLock::new(|| branch("!", "DeclCE"));
/// `<\?(?:PI_CE)?`: processing instructions.
static PI: LazyLock<regex::Regex> = LazyLock::new(|| branch("\\?", "PI_CE"));
/// `</(?:EndTagCE)?`: end tags.
static END_TAG: LazyLock<regex::Regex> = LazyLock::new(|| branch("/", "EndTagCE"));

/// Chameleon's `iter_xml`: `re_xml_spe.finditer(body)`. `src` must satisfy
/// [`crate::source::check_len`].
///
/// `XML_SPE` is `TextSE|MarkupSPE` with `TextSE = [^<]+`, and every `MarkupSPE` match
/// starts with `<`. So at a position holding anything but `<` the match is the run up to the
/// next `<` (found with memchr). At a `<`, `MarkupSPE` is
/// `<(?:!(?:DeclCE)?|\?(?:PI_CE)?|/(?:EndTagCE)?|(?:ElemTagCE)?)`: every branch can match
/// empty after its lead character and nothing follows the group, so the character after the
/// `<` picks the branch. The `!`, `?` and `/` branches have no lookaround or backreferences
/// and run on the `regex` crate (linear time, leftmost-first, the same match a backtracking
/// engine finds); `ElemTagCE` has a lookahead and is matched by hand ([`elem_tag`]).
/// [`tokenize_reference`] is the literal `finditer` on fancy-regex, and the tests check the
/// two agree.
#[must_use]
pub fn tokenize(src: &str) -> Vec<Span> {
    let b = src.as_bytes();
    let mut tokens = Vec::with_capacity(src.len() / 16 + 1);
    let mut i = 0;
    while i < b.len() {
        let end = if b[i] == b'<' {
            // No pattern looks behind, so matching on the rest of the source is the same as
            // Python's `finditer` continuing at `i`.
            let rest = &src[i..];
            let found = match b.get(i + 1) {
                Some(b'!') => DECL.find(rest).map(|m| m.end()),
                Some(b'?') => PI.find(rest).map(|m| m.end()),
                Some(b'/') => END_TAG.find(rest).map(|m| m.end()),
                _ => Some(elem_tag(rest)),
            };
            // Each branch matches at least its lead characters.
            debug_assert!(found.is_some_and(|n| n > 0), "MarkupSPE failed at {i}");
            i + found.unwrap_or(1).max(1)
        } else {
            memchr::memchr(b'<', &b[i..]).map_or(b.len(), |n| i + n)
        };
        tokens.push(Span::new(i, end));
        i = end;
    }
    tokens
}

/// `<(?:ElemTagCE)?` at the start of `s` (which starts with `<`); returns the match length.
///
/// `ElemTagCE` is `(Name)(?:(S)(Name)(((?:S)?=(?:S)?)(?:AttValSE|Simple)|(?!(?:S)?=)))*`
/// `(?:S)?(/?>)?`. Its tail always matches (possibly empty), so a backtracking engine never
/// revisits a successful step: the first name is maximal and each attribute step is the
/// first alternative that succeeds, in priority order. Working through the priorities:
///
/// - the step's `S` is the maximal run (a `Name` cannot start with whitespace);
/// - with the maximal `Name`: if `S?=` follows, the value is tried after the maximal `S?`,
///   then (when that fails) after one whitespace character fewer, where `Simple` always
///   matches; otherwise the lookahead succeeds and the attribute is valueless;
/// - only when `=` follows and the value fails (end of input or `>` right after the `=`)
///   is a shorter `Name` tried; one character shorter, the next character is a name
///   character, so the lookahead succeeds there and the step ends inside the name
///   (`<a bc=>` tokenizes as `<a b`, `c=>`). A one-character name fails the step.
fn elem_tag(s: &str) -> usize {
    let b = s.as_bytes();
    let Some(mut p) = name_end(b, 1) else { return 1 };
    while let Some(next) = attribute_step(s, p) {
        p = next;
    }
    let q = skip_s(b, p);
    if b[q..].starts_with(b"/>") {
        q + 2
    } else if b.get(q) == Some(&b'>') {
        q + 1
    } else {
        q
    }
}

/// One iteration of `ElemTagCE`'s attribute loop starting at `p`; its end, or `None`.
fn attribute_step(s: &str, p: usize) -> Option<usize> {
    let b = s.as_bytes();
    let start = skip_s(b, p);
    if start == p {
        return None;
    }
    let end = name_end(b, start)?;
    let eq = skip_s(b, end);
    if b.get(eq) != Some(&b'=') {
        return Some(end); // `(?!(?:S)?=)` succeeds: a valueless attribute
    }
    let v0 = eq + 1;
    let v = skip_s(b, v0);
    if let Some(value_end) = att_value(s, v) {
        return Some(value_end);
    }
    if v > v0 {
        // One whitespace character fewer: `Simple` matches from that whitespace.
        return Some(simple_end(b, v - 1));
    }
    // The value failed and the lookahead sees `=`: back off one character of the name.
    let shorter = s[..end].char_indices().next_back().map(|(i, _)| i)?;
    (shorter > start).then_some(shorter)
}

/// `AttValSE|Simple` at `v`: `"[^"]*"|'[^']*'|[^\s=<>\x60]+|[^"'>/]+` (Python `\s`).
fn att_value(s: &str, v: usize) -> Option<usize> {
    let b = s.as_bytes();
    let c = s.get(v..)?.chars().next()?;
    if let q @ ('"' | '\'') = c
        && let Some(n) = memchr::memchr(q as u8, &b[v + 1..])
    {
        return Some(v + 1 + n + 1);
    }
    let unquoted = |c: char| !crate::pychars::is_space(c) && !matches!(c, '=' | '<' | '>' | '`');
    if unquoted(c) {
        return Some(v + crate::pychars::run_len(&s[v..], unquoted));
    }
    if !matches!(c, '"' | '\'' | '>' | '/') {
        return Some(simple_end(b, v));
    }
    None
}

/// End of the `Simple` run (`[^"'>/]+`) starting at `v`.
fn simple_end(b: &[u8], v: usize) -> usize {
    v + b[v..].iter().take_while(|&&c| !matches!(c, b'"' | b'\'' | b'>' | b'/')).count()
}

/// `Name`: `(?:[A-Za-z_:@]|[^\x00-\x7F])(?:[A-Za-z0-9_:.-]|[^\x00-\x7F])*`, maximal. Every
/// non-ASCII byte qualifies, so this works on bytes and still ends on a char boundary.
fn name_end(b: &[u8], p: usize) -> Option<usize> {
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

/// `(?:S)?` with `S = [ \n\t\r]+`, maximal.
fn skip_s(b: &[u8], p: usize) -> usize {
    p + b[p..].iter().take_while(|&&c| matches!(c, b' ' | b'\n' | b'\t' | b'\r')).count()
}

/// `re_xml_spe.finditer(body)`, literally, on fancy-regex (for tests of [`tokenize`]). Fails
/// where fancy-regex's backtracking stack overflows (markup tokens around 1 MB).
#[cfg(test)]
fn tokenize_reference(src: &str) -> Result<Vec<Span>, String> {
    static XML_SPE: LazyLock<fancy_regex::Regex> =
        LazyLock::new(|| crate::pyre::compile(&pattern("XML_SPE").unwrap_or_default(), false));
    XML_SPE.find_iter(src).map(|m| m.map(|m| Span::new(m.start(), m.end())).map_err(|e| e.to_string())).collect()
}

/// Chameleon's `iter_text`: the whole body as one token (even when empty).
#[must_use]
pub fn tokenize_text(src: &str) -> Vec<Span> {
    vec![Span::new(0, src.len())]
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn tokens(src: &str) -> Vec<&str> {
        tokenize(src).into_iter().map(|s| s.text(src)).collect()
    }

    #[test]
    fn patterns_compile() {
        for (name, _) in PIECES {
            let p = pattern(name).unwrap();
            crate::pyre::translate(&p, false).unwrap();
        }
        LazyLock::force(&DECL);
        LazyLock::force(&PI);
        LazyLock::force(&END_TAG);
    }

    #[test]
    fn expansion_matches_pythons_formatting() {
        assert_eq!(pattern("Until2Hyphens").unwrap(), "[^-]*-(?:[^-][^-]*-)*-");
        assert!(pattern("DT_ItemSE").unwrap().contains("|%(?:[A-Za-z_:@]"));
        assert_eq!(pattern("XML_SPE").unwrap().len(), 1064);
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
    fn declarations_cdata_and_processing_instructions() {
        assert_eq!(tokens("<!DOCTYPE html>x"), ["<!DOCTYPE html>", "x"]);
        assert_eq!(tokens("<![CDATA[ a ]] ]]>x"), ["<![CDATA[ a ]] ]]>", "x"]);
        assert_eq!(tokens("<?xml version=\"1.0\"?><a/>"), ["<?xml version=\"1.0\"?>", "<a/>"]);
        assert_eq!(tokens("<? ?>"), ["<?", " ?>"]);
        assert_eq!(tokens("</>"), ["</", ">"]);
    }

    #[test]
    fn unicode_whitespace_ends_unquoted_values() {
        // `[^\s=<>`]` uses Python's `\s`, which includes U+001C (Rust's `\s` does not).
        // The tag then has no `>`, so it ends there (verified against Chameleon).
        assert_eq!(tokens("<a b=c\u{1c}d>"), ["<a b=c", "\u{1c}d>"]);
        assert_eq!(tokens("<a b=c\u{3000}>"), ["<a b=c", "\u{3000}>"]);
    }

    #[test]
    fn markup_tokens_over_a_megabyte() {
        let big = "A".repeat(1_100_000);
        let attr = format!("<p title=\"{big}\" b c=d>x</p>");
        assert_eq!(tokens(&attr).len(), 3);
        // The reason production does not run on fancy-regex: its stack overflows here.
        assert!(tokenize_reference(&attr).is_err());
        for markup in [format!("<!--{big}-->"), format!("<![CDATA[{big}]]>"), format!("<?php {big} ?>")] {
            assert_eq!(tokens(&markup), [markup.as_str()]);
        }
        let many = format!("<p{}>", " a=\"x\"".repeat(250_000));
        assert_eq!(tokens(&many), [many.as_str()]);
    }

    #[test]
    fn text_mode_is_one_token() {
        assert_eq!(tokenize_text(""), [Span::new(0, 0)]);
        assert_eq!(tokenize_text("<a>"), [Span::new(0, 3)]);
    }

    proptest! {
        #[test]
        fn tokens_cover_input(src in r#"[<>a-z /=\n\t"'`!?\[\]\x{3000}\x{1c}é-]{0,40}|\PC{0,40}"#) {
            let spans = tokenize(&src);
            let mut at = 0;
            for span in &spans {
                prop_assert_eq!(span.start as usize, at);
                prop_assert!(span.end > span.start);
                prop_assert!(src.is_char_boundary(span.end as usize));
                at = span.end as usize;
            }
            prop_assert_eq!(at, src.len());
            prop_assert_eq!(Ok(spans), tokenize_reference(&src));
        }
    }
}
