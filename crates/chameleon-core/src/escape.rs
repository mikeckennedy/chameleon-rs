// Ported from Chameleon's src/chameleon/compiler.py (`emit_func_convert_and_escape`, the
// `__quote` helper, and `ExpressionEngine._convert_text`), modified: escaping rewritten as a
// table-driven pass over bytes.

//! Output escaping contexts and Chameleon's exact escaping rule (D6).

use crate::output::Output;

/// The quote character around an attribute value in the source.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Quote {
    /// `"`, replaced by `&quot;`.
    Double,
    /// `'`, replaced by `&#39;` (Python's HTML 4 entity table has no `apos`).
    Single,
    /// An unquoted value. Chameleon's quote is then `''`, and since `'' in s` is always true,
    /// `s.replace('', '&#0;')` puts `&#0;` around every character once escaping is triggered.
    Bare,
}

impl Quote {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Double => "\"",
            Self::Single => "'",
            Self::Bare => "",
        }
    }
}

/// How a converted value is written to the output.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Escape {
    /// `${...}` in text or comments: `&`, `<`, `>`, and the text "quote" NUL as `&#0;`.
    Text,
    /// `tal:content` / `tal:replace` text: `&`, `<`, `>` (Chameleon passes no quote).
    Content,
    /// `structure`: no escaping.
    Structure,
    /// An attribute value: `&`, `<`, `>` and the attribute's quote character.
    Attr(Quote),
}

impl Escape {
    /// In attribute substitutions the `default` marker converts to the (absent) default,
    /// which drops the attribute.
    #[must_use]
    pub fn drops_default(self) -> bool {
        matches!(self, Self::Attr(_))
    }

    #[must_use]
    pub fn escapes(self) -> bool {
        !matches!(self, Self::Structure)
    }
}

/// Chameleon's gate (`re.compile(r'[&<>"\']').search`): a string is only escaped at all if
/// it contains one of these. NUL is not in the gate.
const GATE: [bool; 256] = {
    let mut gate = [false; 256];
    gate[b'&' as usize] = true;
    gate[b'<' as usize] = true;
    gate[b'>' as usize] = true;
    gate[b'"' as usize] = true;
    gate[b'\'' as usize] = true;
    gate
};

/// Classes: 1 `&amp;`, 2 `&lt;`, 3 `&gt;`, 4 `&quot;`, 5 `&#39;`, 6 `&#0;` (see `replace`).
const NUL_CLASS: u8 = 6;

const fn table(quote: Option<u8>) -> [u8; 256] {
    let mut t = [0u8; 256];
    t[b'&' as usize] = 1;
    t[b'<' as usize] = 2;
    t[b'>' as usize] = 3;
    match quote {
        Some(b'"') => t[b'"' as usize] = 4,
        Some(b'\'') => t[b'\'' as usize] = 5,
        Some(b'\0') => t[0] = NUL_CLASS,
        _ => {}
    }
    t
}

const TEXT_TABLE: [u8; 256] = table(Some(b'\0'));
const PLAIN_TABLE: [u8; 256] = table(None);
const DOUBLE_TABLE: [u8; 256] = table(Some(b'"'));
const SINGLE_TABLE: [u8; 256] = table(Some(b'\''));

/// Whether Chameleon would escape this string at all.
#[inline]
#[must_use]
pub fn needs_escape(s: &[u8]) -> bool {
    s.iter().any(|&b| GATE[b as usize])
}

/// Writes `s` (UTF-8, or `surrogatepass` bytes: escaping only touches ASCII) with Chameleon's
/// escaping for `esc`. A string without a gate character is copied as is, so `'a\0b'` stays
/// raw in text while `'a&\0b'` becomes `a&amp;&#0;b`.
pub fn escape_into(s: &[u8], esc: Escape, out: &mut Output) {
    let table = match esc {
        Escape::Structure => {
            out.push(s);
            return;
        }
        Escape::Text => &TEXT_TABLE,
        Escape::Content | Escape::Attr(Quote::Bare) => &PLAIN_TABLE,
        Escape::Attr(Quote::Double) => &DOUBLE_TABLE,
        Escape::Attr(Quote::Single) => &SINGLE_TABLE,
    };
    if !needs_escape(s) {
        out.push(s);
        return;
    }
    if esc == Escape::Attr(Quote::Bare) {
        let mut escaped = Output::with_capacity(s.len() + 16);
        replace(s, table, &mut escaped);
        interleave_nul_entity(escaped.as_bytes(), out);
        return;
    }
    replace(s, table, out);
}

/// Copies `s`, replacing the bytes `table` classifies. Each entity is written as a
/// constant-length copy (inlined stores, no `memmove` call) and the buffer is reserved once:
/// the profile of the listing benchmark showed two `memmove` calls per escaped character.
fn replace(s: &[u8], table: &[u8; 256], out: &mut Output) {
    // Most escaped strings hold a few entities; `push` still grows the buffer if needed.
    out.reserve(s.len() + 24);
    let mut last = 0;
    for (i, &b) in s.iter().enumerate() {
        let class = table[b as usize];
        if class != 0 {
            if last < i {
                out.push(&s[last..i]);
            }
            match class {
                1 => out.push(b"&amp;"),
                2 => out.push(b"&lt;"),
                3 => out.push(b"&gt;"),
                4 => out.push(b"&quot;"),
                5 => out.push(b"&#39;"),
                _ => out.push(b"&#0;"),
            }
            last = i + 1;
        }
    }
    if last < s.len() {
        out.push(&s[last..]);
    }
}

/// Python's `s.replace('', '&#0;')`: the entity before every character and at the end.
fn interleave_nul_entity(s: &[u8], out: &mut Output) {
    for (i, &b) in s.iter().enumerate() {
        // A UTF-8 lead byte (also true for `surrogatepass` sequences) starts a character.
        if b & 0xC0 != 0x80 {
            out.push(b"&#0;");
        }
        out.push(&s[i..=i]);
    }
    out.push(b"&#0;");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn esc(s: &str, e: Escape) -> String {
        let mut out = Output::default();
        escape_into(s.as_bytes(), e, &mut out);
        String::from_utf8(out.as_bytes().to_vec()).unwrap()
    }

    #[test]
    fn gate_decides_whether_anything_is_escaped() {
        assert_eq!(esc("a\0b", Escape::Text), "a\0b");
        assert_eq!(esc("a&\0b", Escape::Text), "a&amp;&#0;b");
        assert_eq!(esc("a&\0b", Escape::Content), "a&amp;\0b");
        assert_eq!(esc("<'\">", Escape::Content), "&lt;'\"&gt;");
        assert_eq!(esc("<'\">", Escape::Structure), "<'\">");
    }

    #[test]
    fn attribute_quotes() {
        assert_eq!(esc("a&'\"b\0", Escape::Attr(Quote::Double)), "a&amp;'&quot;b\0");
        assert_eq!(esc("a&'\"b", Escape::Attr(Quote::Single)), "a&amp;&#39;\"b");
        assert_eq!(esc("ab", Escape::Attr(Quote::Bare)), "ab");
        assert_eq!(esc("aé&", Escape::Attr(Quote::Bare)), "&#0;a&#0;é&#0;&&#0;a&#0;m&#0;p&#0;;&#0;");
    }
}
