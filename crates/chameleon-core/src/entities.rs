// Ported from Chameleon's src/chameleon/utils.py (`decode_htmlentities`, `substitute_entity`,
// `char2entity`), modified: rewritten in Rust without regular expressions.

//! HTML entity decoding for TAL attribute values and `${...}` expressions.

use crate::entities_table::NAME_TO_CODEPOINT;
use crate::pychars::{is_digit, is_word};

/// Why an entity reference could not be decoded. Python raises `ValueError` from `chr()` or
/// `int()` in these cases, so it is a compile error here too.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("invalid character reference: {reference}")]
pub(crate) struct EntityError {
    pub reference: String,
}

/// Chameleon's `decode_htmlentities`: replaces every match of `&(#?)(x?)(\d{1,5}|\w{1,8});`
/// with its character. Named references use Python's HTML 4 table (`&apos;` is not in it);
/// unknown names are kept as they are.
pub(crate) fn decode_htmlentities(s: &str) -> Result<std::borrow::Cow<'_, str>, EntityError> {
    if !s.contains('&') {
        return Ok(std::borrow::Cow::Borrowed(s));
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(amp) = rest.find('&') {
        out.push_str(rest.get(..amp).unwrap_or_default());
        let after = rest.get(amp + 1..).unwrap_or_default();
        if let Some(m) = match_reference(after) {
            out.push_str(&substitute(&m, rest.get(amp..amp + 1 + m.len).unwrap_or_default())?);
            rest = after.get(m.len..).unwrap_or_default();
        } else {
            out.push('&');
            rest = after;
        }
    }
    out.push_str(rest);
    Ok(std::borrow::Cow::Owned(out))
}

/// Bytes of an entity reference after its `&` (through the `;`), if `s` starts with one.
pub(crate) fn reference_len(s: &str) -> Option<usize> {
    match_reference(s).map(|m| m.len)
}

struct Reference<'a> {
    hash: bool,
    x: bool,
    body: &'a str,
    /// Bytes matched after the `&`, including the `;`.
    len: usize,
}

/// Matches `(#?)(x?)(\d{1,5}|\w{1,8});` at the start of `s` with the regex's preference order
/// (greedy optional groups first, then backtracking).
fn match_reference(s: &str) -> Option<Reference<'_>> {
    let hash = s.starts_with('#');
    let hash_options: &[bool] = if hash { &[true, false] } else { &[false] };
    for &take_hash in hash_options {
        let after_hash = if take_hash { s.get(1..)? } else { s };
        let x_options: &[bool] = if after_hash.starts_with('x') { &[true, false] } else { &[false] };
        for &take_x in x_options {
            let body_start = usize::from(take_hash) + usize::from(take_x);
            let tail = s.get(body_start..)?;
            if let Some(n) = run_then_semicolon(tail, 5, is_digit).or_else(|| run_then_semicolon(tail, 8, is_word)) {
                return Some(Reference {
                    hash: take_hash,
                    x: take_x,
                    body: tail.get(..n).unwrap_or_default(),
                    len: body_start + n + 1,
                });
            }
        }
    }
    None
}

/// Length in bytes of a run of 1..=`max` chars matching `pred`, if a `;` follows it.
fn run_then_semicolon(s: &str, max: usize, pred: impl Fn(char) -> bool) -> Option<usize> {
    let mut end = 0;
    for (count, (i, c)) in s.char_indices().enumerate() {
        if count == max || !pred(c) {
            end = i;
            break;
        }
        end = i + c.len_utf8();
    }
    (end > 0 && s.get(end..)?.starts_with(';')).then_some(end)
}

fn substitute(m: &Reference<'_>, whole: &str) -> Result<String, EntityError> {
    let invalid = || EntityError { reference: whole.to_owned() };
    if m.hash {
        let value = if m.x { u32::from_str_radix(m.body, 16) } else { m.body.parse::<u32>() };
        let c = value.ok().and_then(char::from_u32).ok_or_else(invalid)?;
        return Ok(c.to_string());
    }
    Ok(match NAME_TO_CODEPOINT.binary_search_by(|(name, _)| (*name).cmp(m.body)) {
        Ok(i) => char::from_u32(NAME_TO_CODEPOINT[i].1).ok_or_else(invalid)?.to_string(),
        Err(_) => whole.to_owned(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decode(s: &str) -> String {
        decode_htmlentities(s).unwrap().into_owned()
    }

    #[test]
    fn decodes_like_chameleon() {
        assert_eq!(decode("&amp;amp;"), "&amp;");
        assert_eq!(decode("a &lt; b &gt; c"), "a < b > c");
        assert_eq!(decode("&#65;&#x41;&nbsp;"), "AA\u{a0}");
        assert_eq!(decode("&apos; &bogus; & &;"), "&apos; &bogus; & &;");
        assert_eq!(decode("&x41;"), "&x41;");
    }

    #[test]
    fn invalid_numeric_reference_is_an_error() {
        assert!(decode_htmlentities("&#xZZ;").is_err());
        assert!(decode_htmlentities("&#99999999;").is_err());
    }
}
