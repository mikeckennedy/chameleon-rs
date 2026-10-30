//! Lone surrogates in the template source (architecture.md, Phase 8 amendments).
//!
//! A Python `str` may hold lone surrogates (U+D800..U+DFFF); a Rust `str` cannot. Chameleon
//! renders them where the source has them (text, attribute values, `string:`), so the
//! Python layer maps each one onto a block of 2,048 code points of the BMP Private Use Area
//! that the source does not use, one to one (both take 3 UTF-8 bytes, so byte and
//! code-point offsets are unchanged), and the core compiles that text. Wherever source text
//! leaves the core it is mapped back: the program's output arena holds the surrogates'
//! `surrogatepass` bytes (WTF-8), and the embedding converts every other piece of source
//! text with [`to_wtf8`].

/// Lone surrogates per block (U+D800..U+DFFF).
const SPAN: u32 = 0x800;

/// The BMP Private Use Area (U+E000..U+F8FF), whose characters take 3 UTF-8 bytes, as lone
/// surrogates do.
const PUA: std::ops::Range<u32> = 0xE000..0xF900;

/// Whether the 2,048 code points from `base` lie in the BMP Private Use Area.
#[must_use]
pub fn is_block(base: u32) -> bool {
    PUA.start <= base && base.checked_add(SPAN).is_some_and(|end| end <= PUA.end)
}

const SURROGATE_START: u32 = 0xD800;

fn decode3(b: &[u8]) -> u32 {
    (u32::from(b[0] & 0x0F) << 12) | (u32::from(b[1] & 0x3F) << 6) | u32::from(b[2] & 0x3F)
}

#[allow(clippy::cast_possible_truncation)] // each piece is masked to a byte
fn encode3(c: u32, out: &mut [u8]) {
    out[0] = 0xE0 | (c >> 12) as u8;
    out[1] = 0x80 | ((c >> 6) & 0x3F) as u8;
    out[2] = 0x80 | (c & 0x3F) as u8;
}

/// Rewrites, in place, every character of the block at `base` to its lone surrogate's
/// `surrogatepass` bytes. `bytes` must be UTF-8. Returns whether anything changed.
pub fn block_to_surrogates(bytes: &mut [u8], base: u32) -> bool {
    let mut changed = false;
    let mut i = 0;
    while i + 2 < bytes.len() {
        // The Private Use Area's characters (U+E000..U+F8FF) have lead bytes 0xEE and 0xEF.
        if bytes[i] == 0xEE || bytes[i] == 0xEF {
            let c = decode3(&bytes[i..i + 3]);
            if (base..base + SPAN).contains(&c) {
                encode3(SURROGATE_START + (c - base), &mut bytes[i..i + 3]);
                changed = true;
            }
            i += 3;
        } else {
            i += 1;
        }
    }
    changed
}

/// The `surrogatepass` bytes (WTF-8) of `text` with the block at `base` mapped back to lone
/// surrogates; `None` if `text` has none of the block's characters (it is then its own
/// bytes).
#[must_use]
pub fn to_wtf8(text: &str, base: u32) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    let first = text.char_indices().find(|&(_, c)| (base..base + SPAN).contains(&u32::from(c)))?.0;
    let mut out = bytes.to_vec();
    block_to_surrogates(&mut out[first..], base);
    Some(out)
}

/// Text that may hold lone surrogates as their `surrogatepass` bytes (WTF-8): a translation
/// stream, a name or a capture taken from the output, which holds whatever values and static
/// text wrote. The embedding decodes it with `surrogatepass`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Wtf8Buf(Vec<u8>);

impl Wtf8Buf {
    /// `bytes`, if it is UTF-8 apart from `surrogatepass` sequences.
    #[must_use]
    pub fn from_bytes(bytes: Vec<u8>) -> Option<Self> {
        // Almost always plain UTF-8: one validation pass.
        let valid = std::str::from_utf8(&bytes).is_ok() || chars(&bytes).all(|c| c.is_some());
        valid.then_some(Self(bytes))
    }

    #[must_use]
    pub fn as_bytes(&self) -> &[u8] {
        &self.0
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// The text, if it holds no lone surrogate.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        std::str::from_utf8(&self.0).ok()
    }

    /// `__re_whitespace(text).strip()`: runs of Python whitespace become one space, then the
    /// ends are stripped (a lone surrogate is not whitespace).
    #[must_use]
    pub fn normalize_space(&self) -> Self {
        let mut out = Vec::with_capacity(self.0.len());
        let mut space = false;
        let mut at = 0;
        for c in chars(&self.0) {
            let (len, is_space) = match c {
                Some(Piece::Char(c)) => (c.len_utf8(), crate::pychars::is_space(c)),
                Some(Piece::Surrogate) | None => (3, false),
            };
            if is_space {
                space = true;
            } else {
                if space && !out.is_empty() {
                    out.push(b' ');
                }
                space = false;
                out.extend_from_slice(self.0.get(at..at + len).unwrap_or_default());
            }
            at += len;
        }
        Self(out)
    }
}

impl From<&str> for Wtf8Buf {
    fn from(s: &str) -> Self {
        Self(s.as_bytes().to_vec())
    }
}

impl std::fmt::Display for Wtf8Buf {
    /// Lossy: a lone surrogate shows as U+FFFD.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&String::from_utf8_lossy(&self.0))
    }
}

enum Piece {
    Char(char),
    Surrogate,
}

/// The characters of WTF-8 text; `None` for an invalid sequence that is not a surrogate's.
/// Each valid run is validated once (linear in the length).
fn chars(bytes: &[u8]) -> impl Iterator<Item = Option<Piece>> + '_ {
    let mut rest = bytes;
    let mut run = "".chars();
    std::iter::from_fn(move || {
        if let Some(c) = run.next() {
            return Some(Some(Piece::Char(c)));
        }
        if rest.is_empty() {
            return None;
        }
        let (valid, tail) = match std::str::from_utf8(rest) {
            Ok(s) => (s, &rest[rest.len()..]),
            Err(e) => {
                let (head, tail) = rest.split_at(e.valid_up_to());
                (std::str::from_utf8(head).unwrap_or_default(), tail)
            }
        };
        if !valid.is_empty() {
            rest = tail;
            run = valid.chars();
            return run.next().map(|c| Some(Piece::Char(c)));
        }
        let piece = match rest {
            [0xED, b1, b2, ..] if (0xA0..=0xBF).contains(b1) && (0x80..=0xBF).contains(b2) => {
                rest = &rest[3..];
                Some(Piece::Surrogate)
            }
            _ => {
                rest = &[];
                None
            }
        };
        Some(piece)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_the_block_both_ways() {
        let base = 0xE800;
        let text = "a\u{E800}b\u{EFFF}c\u{E000}\u{F000}";
        let wtf8 = to_wtf8(text, base).unwrap_or_default();
        assert_eq!(wtf8.len(), text.len());
        // U+D800 and U+DFFF in surrogatepass form; the other private-use characters stay.
        assert_eq!(&wtf8[1..4], &[0xED, 0xA0, 0x80]);
        assert_eq!(&wtf8[5..8], &[0xED, 0xBF, 0xBF]);
        assert_eq!(&wtf8[9..], "\u{E000}\u{F000}".as_bytes());
        assert_eq!(to_wtf8("plain \u{E000}", base), None);
    }

    #[test]
    fn wtf8_text() {
        assert_eq!(Wtf8Buf::from_bytes(b"a\xFFb".to_vec()), None);
        assert_eq!(Wtf8Buf::from_bytes(b"a\xED\xA0".to_vec()), None);
        let text = Wtf8Buf::from_bytes(b"  a\xED\xA0\x80 \t b\xC3\xA9\n".to_vec()).unwrap_or_default();
        assert_eq!(text.normalize_space().as_bytes(), b"a\xED\xA0\x80 b\xC3\xA9");
        assert_eq!(text.as_str(), None);
        assert_eq!(Wtf8Buf::from(" x  y ").normalize_space().as_str(), Some("x y"));
    }
}
