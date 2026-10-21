// Ported from Chameleon's src/chameleon/utils.py (`decode_htmlentities`, `substitute_entity`,
// `char2entity`), modified: rewritten in Rust without regular expressions.

//! HTML entity decoding for TAL attribute values and `${...}` expressions.

use crate::entities_table::NAME_TO_CODEPOINT;
use crate::pychars::{is_digit, is_word};

/// Why an entity reference could not be decoded: Python raises a bare `ValueError` from
/// `int()` or `chr()` (`message` is its text), or the reference names a surrogate, which
/// Python decodes to a lone surrogate that a Rust string cannot hold.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub(crate) struct EntityError {
    pub message: String,
    pub surrogate: bool,
}

/// Chameleon's `decode_htmlentities`: replaces every match of `&(#?)(x?)(\d{1,5}|\w{1,8});`
/// with its character. Named references use Python's HTML 4 table (`&apos;` is not in it);
/// unknown names are kept as they are.
///
/// A reference to a lone surrogate, which Python decodes to one, decodes into the private-use
/// block at `surrogate_base` ([`crate::surrogates`]); without one it is an error.
pub(crate) fn decode_htmlentities(
    s: &str,
    surrogate_base: Option<u32>,
) -> Result<std::borrow::Cow<'_, str>, EntityError> {
    if !s.contains('&') {
        return Ok(std::borrow::Cow::Borrowed(s));
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(amp) = rest.find('&') {
        out.push_str(rest.get(..amp).unwrap_or_default());
        let after = rest.get(amp + 1..).unwrap_or_default();
        if let Some(m) = match_reference(after) {
            out.push_str(&substitute(&m, rest.get(amp..amp + 1 + m.len).unwrap_or_default(), surrogate_base)?);
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

/// `substitute_entity`: `chr(int(ent))`, `chr(int('0x' + ent, 16))`, or the HTML 4 name.
fn substitute(m: &Reference<'_>, whole: &str, surrogate_base: Option<u32>) -> Result<String, EntityError> {
    if m.hash {
        let (literal, base) = if m.x { (format!("0x{}", m.body), 16) } else { (m.body.to_owned(), 10) };
        let value = py_int(&literal, base).ok_or_else(|| EntityError {
            message: format!("invalid literal for int() with base {base}: '{literal}'"),
            surrogate: false,
        })?;
        if value >= 0x11_0000 {
            return Err(EntityError { message: "chr() arg not in range(0x110000)".to_owned(), surrogate: false });
        }
        #[allow(clippy::cast_possible_truncation)] // checked above
        let value = value as u32;
        if let Some(base) = surrogate_base
            && (0xD800..0xE000).contains(&value)
        {
            return Ok(char::from_u32(base + (value - 0xD800)).map(String::from).unwrap_or_default());
        }
        return char::from_u32(value).map(String::from).ok_or_else(|| EntityError {
            message: format!("the character reference {whole} is a lone surrogate"),
            surrogate: true,
        });
    }
    Ok(match NAME_TO_CODEPOINT.binary_search_by(|(name, _)| (*name).cmp(m.body)) {
        Ok(i) => char::from_u32(NAME_TO_CODEPOINT[i].1).map_or_else(|| whole.to_owned(), String::from),
        Err(_) => whole.to_owned(),
    })
}

/// Python's `int(s, base)` for the strings an entity body can be (`\d{1,5}` or `\w{1,8}`,
/// behind `0x` for base 16): Unicode decimal digits count as digits, a single `_` may follow
/// the `0x` prefix or separate two digits. `None` where Python raises `ValueError`.
fn py_int(s: &str, base: u32) -> Option<u64> {
    let body = if base == 16 {
        let rest = s.strip_prefix("0x")?;
        rest.strip_prefix('_').unwrap_or(rest)
    } else {
        s
    };
    let mut value: u64 = 0;
    let mut digits = 0;
    let mut last_underscore = false;
    for c in body.chars() {
        if c == '_' {
            if digits == 0 || last_underscore {
                return None;
            }
            last_underscore = true;
            continue;
        }
        let d = decimal_value(c).or_else(|| if base == 16 { c.to_digit(16) } else { None })?;
        last_underscore = false;
        digits += 1;
        value = value.checked_mul(u64::from(base))?.checked_add(u64::from(d))?;
    }
    (digits > 0 && !last_underscore).then_some(value)
}

/// The value of a Unicode decimal digit (`Py_UNICODE_TODECIMAL`). Decimal digits always come
/// in contiguous runs of whole `0..=9` groups, so the value is the number of digits directly
/// before `c` in its run, modulo 10.
fn decimal_value(c: char) -> Option<u32> {
    if c.is_ascii_digit() {
        return c.to_digit(10);
    }
    if !is_digit(c) {
        return None;
    }
    let mut n = 0u32;
    let mut cp = u32::from(c);
    while let Some(prev) = cp.checked_sub(1).and_then(char::from_u32) {
        if !is_digit(prev) {
            break;
        }
        n += 1;
        cp -= 1;
    }
    Some(n % 10)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decode(s: &str) -> String {
        decode_htmlentities(s, None).unwrap().into_owned()
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
    fn numeric_references_follow_python_int_and_chr() {
        assert_eq!(decode("&#x1_0;&#x_1;&#\u{663};&#x\u{663};&#1_0;&#00065;"), "\u{10}\u{1}\u{3}\u{3}\n\u{41}");
        let err = |s: &str| decode_htmlentities(s, None).unwrap_err().message;
        assert_eq!(err("&#xzz;"), "invalid literal for int() with base 16: '0xzz'");
        assert_eq!(err("&#x;"), "invalid literal for int() with base 10: 'x'");
        assert_eq!(err("&#x1__0;"), "invalid literal for int() with base 16: '0x1__0'");
        assert_eq!(err("&#x110000;"), "chr() arg not in range(0x110000)");
        assert!(decode_htmlentities("&#xD800;", None).unwrap_err().surrogate);
        // With a private-use block for the source's lone surrogates, into that block.
        assert_eq!(decode_htmlentities("a&#xDFFF;&#55296;", Some(0xE800)).unwrap(), "a\u{EFFF}\u{E800}");
        // U+1D7CE..U+1D7FF are five runs of mathematical digits 0-9.
        assert_eq!(decode("&#\u{1d7d9};"), "\u{1}");
    }
}
