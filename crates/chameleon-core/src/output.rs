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

    /// Inserts `bytes` at `mark`, moving what follows (a slot's output written to the stream
    /// its caller had open, before streams opened since).
    pub fn insert_at(&mut self, mark: Mark, bytes: &[u8]) {
        let at = mark.0.min(self.buf.len());
        self.buf.splice(at..at, bytes.iter().copied());
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
