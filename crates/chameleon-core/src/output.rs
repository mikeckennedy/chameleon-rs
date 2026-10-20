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
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Mark(usize);

impl Output {
    #[must_use]
    pub fn with_capacity(capacity: usize) -> Self {
        Self { buf: Vec::with_capacity(capacity), has_surrogates: false }
    }

    #[inline]
    pub fn push(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
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

    pub fn truncate(&mut self, mark: Mark) {
        self.buf.truncate(mark.0);
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
