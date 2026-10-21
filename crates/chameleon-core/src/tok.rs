// Ported from Chameleon's src/chameleon/tokenize.py (`Token.__getitem__`, `__add__`,
// `replace`, `split`, `strip`, `lstrip`, `rstrip`), modified: a Rust string plus a lazily
// resolved position instead of a `str` subclass.

//! Strings that remember where Chameleon thinks they start.
//!
//! Chameleon's `Token` is a `str` with a `pos` (a code-point offset into the template). The
//! program builder slices, strips, splits and rewrites attribute values, and the position
//! that ends up on an expression or in an error message follows `Token`'s own arithmetic,
//! which is not always the text's real location: `Token.split` advances the position by
//! each part's length without counting the separator, and entity decoding keeps the
//! original position while shortening the text. [`Tok`] reproduces that arithmetic, so
//! error offsets match Chameleon's exactly.
//!
//! A position is kept as a byte offset into the source plus a number of code points after
//! it ([`TokPos`]): nothing is counted until an error or a test asks for the code-point
//! offset. A `Tok` without a position is a plain `str` in Chameleon (for example the text
//! `'<?' + name + text + '?>'` builds); errors on it report offset 0 and location (0, 0).

use crate::pychars::is_space;
use crate::source::{Source, Span};

/// Chameleon's `Token.pos`: `char_offset(byte) + chars` code points into the template.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct TokPos {
    pub byte: u32,
    pub chars: u32,
}

impl TokPos {
    #[must_use]
    pub fn at(byte: u32) -> Self {
        Self { byte, chars: 0 }
    }

    /// The position `n` code points later.
    #[must_use]
    #[allow(clippy::cast_possible_truncation)] // positions stay within a u32-sized source
    pub fn plus(self, n: usize) -> Self {
        Self { byte: self.byte, chars: self.chars + n as u32 }
    }

    /// The code-point offset (`Token.pos`).
    #[must_use]
    pub fn offset(self, source: &Source<'_>) -> usize {
        source.char_offset(self.byte as usize) + self.chars as usize
    }
}

/// A string with Chameleon `Token` semantics (see the module docs).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Tok {
    pub text: String,
    /// `None` for a plain `str` (no position).
    pub pos: Option<TokPos>,
}

impl Tok {
    /// The source text under `span`, at its own position.
    #[must_use]
    pub fn span(src: &str, span: Span) -> Self {
        Self { text: span.text(src).to_owned(), pos: Some(TokPos::at(span.start)) }
    }

    /// A plain string (no position).
    #[must_use]
    pub fn plain(text: impl Into<String>) -> Self {
        Self { text: text.into(), pos: None }
    }

    /// Different text at the same position (`string.replace(string, decoded)`,
    /// `Token.replace`).
    #[must_use]
    pub fn with_text(&self, text: impl Into<String>) -> Self {
        Self { text: text.into(), pos: self.pos }
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.text
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.text.is_empty()
    }

    fn at_chars(&self, n: usize) -> Option<TokPos> {
        self.pos.map(|p| p.plus(n))
    }

    /// `token[a:b]` for byte offsets `a..b` into the text (on char boundaries).
    #[must_use]
    pub fn slice(&self, start: usize, end: usize) -> Self {
        let prefix = self.text.get(..start).unwrap_or_default();
        Self {
            text: self.text.get(start..end).unwrap_or_default().to_owned(),
            pos: self.at_chars(prefix.chars().count()),
        }
    }

    /// `token[a:]` for a byte offset.
    #[must_use]
    pub fn slice_from(&self, start: usize) -> Self {
        self.slice(start, self.text.len())
    }

    /// `Token.lstrip()` (Python whitespace).
    #[must_use]
    pub fn lstrip(&self) -> Self {
        let stripped = self.text.trim_start_matches(is_space);
        self.slice_from(self.text.len() - stripped.len())
    }

    /// `Token.rstrip()` (Python whitespace).
    #[must_use]
    pub fn rstrip(&self) -> Self {
        Self { text: self.text.trim_end_matches(is_space).to_owned(), pos: self.pos }
    }

    /// `Token.strip()`.
    #[must_use]
    pub fn strip(&self) -> Self {
        self.lstrip().rstrip()
    }

    /// `Token.strip(chars)`.
    #[must_use]
    pub fn strip_chars(&self, chars: &[char]) -> Self {
        let left = self.text.trim_start_matches(chars);
        let lstripped = self.slice_from(self.text.len() - left.len());
        Self { text: lstripped.text.trim_end_matches(chars).to_owned(), pos: lstripped.pos }
    }

    /// `Token.split(sep)`: each part's position is the previous part's position plus the
    /// previous part's length, *not* counting the separator (Chameleon's arithmetic).
    #[must_use]
    pub fn split(&self, sep: &str) -> Vec<Self> {
        self.split_parts(self.text.split(sep).map(str::to_owned).collect())
    }

    /// `Token.split()`: split on runs of whitespace (`str.split()`), with the same position
    /// arithmetic; the first part starts at the token's own position even after leading
    /// whitespace.
    #[must_use]
    pub fn split_whitespace(&self) -> Vec<Self> {
        let parts = self.text.split(is_space).filter(|p| !p.is_empty()).map(str::to_owned).collect();
        self.split_parts(parts)
    }

    fn split_parts(&self, parts: Vec<String>) -> Vec<Self> {
        let mut at = 0;
        parts
            .into_iter()
            .map(|text| {
                let pos = self.at_chars(at);
                at += text.chars().count();
                Self { text, pos }
            })
            .collect()
    }

    /// The code-point offset, or 0 for a plain string (what `TemplateError.offset` reports).
    #[must_use]
    pub fn offset(&self, source: &Source<'_>) -> usize {
        self.pos.map_or(0, |p| p.offset(source))
    }
}

impl std::fmt::Display for Tok {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tok(text: &str) -> Tok {
        Tok { text: text.to_owned(), pos: Some(TokPos::at(10)) }
    }

    fn chars(t: &Tok) -> u32 {
        t.pos.map_or(u32::MAX, |p| p.chars)
    }

    #[test]
    fn split_does_not_count_separators() {
        let parts = tok("ab;cd;ef").split(";");
        assert_eq!(parts.iter().map(chars).collect::<Vec<_>>(), [0, 2, 4]);
        let parts = tok("  a  bé c").split_whitespace();
        assert_eq!(parts.iter().map(|t| t.text.as_str()).collect::<Vec<_>>(), ["a", "bé", "c"]);
        assert_eq!(parts.iter().map(chars).collect::<Vec<_>>(), [0, 1, 3]);
    }

    #[test]
    fn strip_and_slice_track_code_points() {
        let t = tok("\u{a0} é x ");
        assert_eq!((t.strip().text.as_str(), chars(&t.strip())), ("é x", 2));
        let s = tok("é(a, b)").slice_from(2);
        assert_eq!((s.text.as_str(), chars(&s)), ("(a, b)", 1));
        let s = tok("((a))").strip_chars(&['(', ')']);
        assert_eq!((s.text.as_str(), chars(&s)), ("a", 2));
        assert_eq!(Tok::plain("x").slice_from(0).pos, None);
    }
}
