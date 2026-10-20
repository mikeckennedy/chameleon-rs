//! Template source positions.
//!
//! Everything inside the core works with byte offsets ([`Span`]). Python users see code-point
//! offsets and `(line, column)` pairs, computed lazily on the error path only.

use std::ops::Range;

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

/// Number of code points before byte offset `byte` (what Python calls the string index).
#[must_use]
pub fn char_offset(src: &str, byte: usize) -> usize {
    let byte = floor_char_boundary(src, byte);
    src.get(..byte).map_or(0, |s| s.chars().count())
}

/// `(line, column)` of a byte offset, exactly like Chameleon's `Token.location`: lines count
/// from 1, columns from 0, both in code points.
#[must_use]
pub fn location(src: &str, byte: usize) -> (usize, usize) {
    let byte = floor_char_boundary(src, byte);
    let before = src.get(..byte).unwrap_or_default();
    let line = before.bytes().filter(|&b| b == b'\n').count() + 1;
    let column = match before.rfind('\n') {
        Some(nl) => before.get(nl + 1..).map_or(0, |s| s.chars().count()),
        None => before.chars().count(),
    };
    (line, column)
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
}
