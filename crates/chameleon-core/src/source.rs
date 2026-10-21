// Ported in part from Chameleon's src/chameleon/tokenize.py (`Token.location`), modified:
// positions are byte spans, converted to code points through a lazily built line index.

//! Template source positions.
//!
//! Everything inside the core works with byte offsets ([`Span`]). Python users see code-point
//! offsets and `(line, column)` pairs; [`Source`] computes them on demand, building a line
//! index the first time one is asked for, so the compile path never pays for it.

use std::ops::Range;
use std::sync::OnceLock;

/// A byte range into the template source. `u32` keeps tokens and nodes small; templates over
/// 4 GiB are rejected up front ([`check_len`]).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Span {
    pub start: u32,
    pub end: u32,
}

impl Span {
    /// Builds a span from byte offsets already checked by [`check_len`].
    #[must_use]
    #[allow(clippy::cast_possible_truncation)] // the whole source fits in u32 (check_len)
    pub fn new(start: usize, end: usize) -> Self {
        debug_assert!(start <= end);
        Self { start: start as u32, end: end as u32 }
    }

    #[must_use]
    pub fn empty_at(at: usize) -> Self {
        Self::new(at, at)
    }

    #[must_use]
    pub fn range(self) -> Range<usize> {
        self.start as usize..self.end as usize
    }

    #[must_use]
    pub fn len(self) -> usize {
        (self.end - self.start) as usize
    }

    #[must_use]
    pub fn is_empty(self) -> bool {
        self.start == self.end
    }

    /// The text this span covers. Returns `""` for a span that is out of range or not on char
    /// boundaries, which a correctly built span never is.
    #[must_use]
    pub fn text(self, src: &str) -> &str {
        src.get(self.range()).unwrap_or_default()
    }

    /// A sub-span by byte offsets relative to this span's start.
    #[must_use]
    pub fn sub(self, start: usize, end: usize) -> Self {
        let base = self.start as usize;
        Self::new(base + start, base + end)
    }
}

/// Largest template accepted, so every byte offset fits in a `u32`.
pub(crate) const MAX_SOURCE_LEN: usize = u32::MAX as usize;

/// Whether `src` is small enough for `u32` spans.
#[must_use]
pub(crate) fn check_len(src: &str) -> bool {
    src.len() <= MAX_SOURCE_LEN
}

/// Template source text with on-demand conversion of byte offsets to what Python reports.
#[derive(Debug)]
pub struct Source<'a> {
    text: &'a str,
    index: OnceLock<LineIndex>,
}

/// Where each line starts, in bytes and in code points.
#[derive(Debug)]
struct LineIndex {
    ascii: bool,
    /// `(byte, code point)` offset of the start of each line; line 1 starts at `(0, 0)`.
    lines: Vec<(usize, usize)>,
}

impl<'a> Source<'a> {
    #[must_use]
    pub fn new(text: &'a str) -> Self {
        Self { text, index: OnceLock::new() }
    }

    #[must_use]
    pub fn text(&self) -> &'a str {
        self.text
    }

    fn index(&self) -> &LineIndex {
        self.index.get_or_init(|| {
            let ascii = self.text.is_ascii();
            let mut lines = vec![(0, 0)];
            let mut chars = 0;
            let mut last = 0;
            for nl in memchr::memchr_iter(b'\n', self.text.as_bytes()) {
                chars += if ascii { nl + 1 - last } else { count_chars(&self.text.as_bytes()[last..=nl]) };
                last = nl + 1;
                lines.push((last, chars));
            }
            LineIndex { ascii, lines }
        })
    }

    /// The line (0-based) containing byte `byte` and that line's start.
    fn line_of(&self, byte: usize) -> (usize, (usize, usize)) {
        let lines = &self.index().lines;
        let i = lines.partition_point(|&(start, _)| start <= byte).saturating_sub(1);
        (i, lines.get(i).copied().unwrap_or((0, 0)))
    }

    /// Number of code points before byte offset `byte` (what Python calls the string index).
    #[must_use]
    pub fn char_offset(&self, byte: usize) -> usize {
        let byte = floor_char_boundary(self.text, byte);
        if self.index().ascii {
            return byte;
        }
        let (_, (start, chars)) = self.line_of(byte);
        chars + count_chars(&self.text.as_bytes()[start..byte])
    }

    /// `(line, column)` of a byte offset, exactly like Chameleon's `Token.location`: lines
    /// count from 1, columns from 0, both in code points.
    #[must_use]
    pub fn location(&self, byte: usize) -> (usize, usize) {
        let byte = floor_char_boundary(self.text, byte);
        let (line, (start, _)) = self.line_of(byte);
        let column = if self.index().ascii { byte - start } else { count_chars(&self.text.as_bytes()[start..byte]) };
        (line + 1, column)
    }
}

/// Code points in a UTF-8 byte slice that starts and ends on char boundaries.
fn count_chars(b: &[u8]) -> usize {
    b.iter().filter(|&&c| (c & 0xC0) != 0x80).count()
}

/// Number of code points before byte offset `byte`. For one-off use; [`Source`] amortizes.
#[must_use]
pub fn char_offset(src: &str, byte: usize) -> usize {
    Source::new(src).char_offset(byte)
}

/// `(line, column)` of a byte offset, like `Token.location`. For one-off use.
#[must_use]
pub fn location(src: &str, byte: usize) -> (usize, usize) {
    Source::new(src).location(byte)
}

fn floor_char_boundary(src: &str, mut byte: usize) -> usize {
    byte = byte.min(src.len());
    while !src.is_char_boundary(byte) {
        byte -= 1;
    }
    byte
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn location_counts_code_points() {
        let src = "é\nab€c";
        assert_eq!(location(src, 0), (1, 0));
        assert_eq!(location(src, 2), (1, 1));
        assert_eq!(location(src, 3), (2, 0));
        // "ab€" is 5 bytes, 3 code points.
        assert_eq!(location(src, 3 + 5), (2, 3));
        assert_eq!(char_offset(src, 3 + 5), 5);
    }

    #[test]
    fn span_text_never_panics() {
        let src = "é";
        assert_eq!(Span::new(0, 1).text(src), "");
        assert_eq!(Span::new(0, 2).text(src), "é");
        assert_eq!(Span::new(5, 9).text(src), "");
    }

    /// The naive definition, straight from `Token.location`.
    fn naive(src: &str, byte: usize) -> (usize, (usize, usize)) {
        let before = &src[..byte];
        let pos = before.chars().count();
        let line = before.matches('\n').count() + 1;
        let column = match before.rfind('\n') {
            Some(nl) => before[nl + 1..].chars().count(),
            None => pos,
        };
        (pos, (line, column))
    }

    proptest! {
        #[test]
        fn indexed_positions_match_the_definition(src in "[a\n\u{e9}\u{20ac}\u{1f600}]{0,40}") {
            let source = Source::new(&src);
            for (byte, _) in src.char_indices().chain([(src.len(), ' ')]) {
                let (pos, loc) = naive(&src, byte);
                prop_assert_eq!(source.char_offset(byte), pos);
                prop_assert_eq!(source.location(byte), loc);
            }
        }
    }
}
