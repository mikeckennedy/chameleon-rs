//! The render output buffer (D6).

/// UTF-8 bytes plus a flag recording that some piece was encoded with `surrogatepass` (a
/// Python `str` holding lone surrogates). The final Python `str` is decoded once, with
/// `surrogatepass` only when the flag is set.
///
/// The representation stays behind this API (marks, not sub-buffers, D3) because D9's
/// placeholders will turn it into a list of segments.
#[derive(Debug, Default)]
pub struct Output {
    buf: Vec<u8>,
    has_surrogates: bool,
}

/// A position in the output, for rolling back (`tal:on-error`, dropped attributes).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct Mark(usize);

impl Mark {
    /// The byte offset.
    #[must_use]
    pub(crate) fn position(self) -> usize {
        self.0
    }

    /// The mark moved by `n` bytes (after `n` bytes were inserted before it).
    #[must_use]
    pub(crate) fn shifted(self, n: usize) -> Self {
        Self(self.0 + n)
    }
}

impl Output {
    #[must_use]
    pub fn with_capacity(capacity: usize) -> Self {
        Self { buf: Vec::with_capacity(capacity), has_surrogates: false }
    }

    #[inline]
    pub fn push(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
    }

    /// Reserves room for at least `additional` more bytes.
    #[inline]
    pub fn reserve(&mut self, additional: usize) {
        self.buf.reserve(additional);
    }

    #[inline]
    pub fn push_str(&mut self, s: &str) {
        self.buf.extend_from_slice(s.as_bytes());
    }

    /// Appends bytes produced by a `surrogatepass` encode.
    pub fn push_surrogate_bytes(&mut self, bytes: &[u8]) {
        self.has_surrogates = true;
        self.buf.extend_from_slice(bytes);
    }

    /// Marks the buffer as containing `surrogatepass` bytes (after an escape wrote them).
    pub fn set_has_surrogates(&mut self) {
        self.has_surrogates = true;
    }

    #[must_use]
    pub fn mark(&self) -> Mark {
        Mark(self.buf.len())
    }

    /// Drops everything after `mark` (nothing if the buffer is already shorter).
    pub fn truncate(&mut self, mark: Mark) {
        self.buf.truncate(mark.0);
    }

    /// Removes and returns everything after `mark` (a translation's or `i18n:name`'s stream).
    pub fn take_since(&mut self, mark: Mark) -> Vec<u8> {
        if mark.0 >= self.buf.len() { Vec::new() } else { self.buf.split_off(mark.0) }
    }

    /// Moves the bytes between `from` and `to` back to `dest`, shifting what lay between
    /// `dest` and `from` after them (a slot body's output written to its defining function's
    /// stream, before streams opened since). Nothing unless `dest <= from <= to <= len`.
    pub fn move_back(&mut self, dest: Mark, from: Mark, to: Mark) {
        if dest.0 <= from.0
            && from.0 <= to.0
            && let Some(region) = self.buf.get_mut(dest.0..to.0)
        {
            region.rotate_right(to.0 - from.0);
        }
    }

    /// Replaces each occurrence of `prefix` followed by `width` bytes and `prefix` again (a
    /// placeholder) for which `fill` returns text (its key is the `width` bytes) by that text,
    /// in one pass. Text marked with surrogates sets the flag.
    pub fn fill_placeholders(
        &mut self,
        prefix: &[u8],
        width: usize,
        mut fill: impl FnMut(&[u8]) -> Option<(Vec<u8>, bool)>,
    ) {
        let size = prefix.len() * 2 + width;
        let mut out = Vec::with_capacity(self.buf.len());
        let mut rest = &self.buf[..];
        while let Some(at) = memchr::memmem::find(rest, prefix) {
            let (before, from) = rest.split_at(at);
            out.extend_from_slice(before);
            let filled = from
                .get(..size)
                .filter(|p| p.ends_with(prefix))
                .and_then(|p| p.get(prefix.len()..prefix.len() + width))
                .and_then(&mut fill);
            if let Some((text, surrogates)) = filled {
                out.extend_from_slice(&text);
                self.has_surrogates |= surrogates;
                rest = from.get(size..).unwrap_or_default();
            } else {
                let (kept, after) = from.split_at(prefix.len());
                out.extend_from_slice(kept);
                rest = after;
            }
        }
        out.extend_from_slice(rest);
        self.buf = out;
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.buf.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }

    #[must_use]
    pub fn as_bytes(&self) -> &[u8] {
        &self.buf
    }

    #[must_use]
    pub fn has_surrogates(&self) -> bool {
        self.has_surrogates
    }
}

/// Formatting straight into the buffer (integers, without a temporary `String`).
impl std::fmt::Write for Output {
    fn write_str(&mut self, s: &str) -> std::fmt::Result {
        self.push_str(s);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn move_back_rotates_a_range_before_what_preceded_it() {
        let mut out = Output::default();
        out.push_str("ab[cd]ef");
        // "[cd]" moves before "ab"; "ef" stays.
        out.move_back(Mark(0), Mark(2), Mark(6));
        assert_eq!(out.as_bytes(), b"[cd]abef");
        // Out-of-order or out-of-range marks change nothing.
        out.move_back(Mark(3), Mark(2), Mark(6));
        out.move_back(Mark(0), Mark(2), Mark(60));
        assert_eq!(out.as_bytes(), b"[cd]abef");
    }
}
