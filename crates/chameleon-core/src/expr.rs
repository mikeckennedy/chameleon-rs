//! The compile-time expression interface (D1).
//!
//! The core decides *where* expressions are (TAL attributes, `${...}` interpolation) but
//! never parses them: an [`ExprCompiler`] supplied by the caller does. `chameleon-py`
//! implements it with CPython's `ast`; core tests use a toy one.

use crate::source::Span;

/// Index of a compiled expression in the caller's expression table.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ExprId(pub u32);

/// Where an expression appears, which changes how some names resolve.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ExprContext {
    /// `default` is a compile-time alias for the default marker (inside `tal:content`, its
    /// default body, and dynamic attributes), not an ordinary name lookup.
    pub default_is_marker: bool,
    /// The value is emitted directly (`${...}` or `tal:content`), so the expression may be a
    /// native frame call such as `render_partial(...)` that writes into the output itself.
    pub emitted: bool,
    /// One of the Interpolator's candidates, so a cheap structural rejection is fine (the
    /// message of the final failure is obtained by compiling again with this unset).
    pub candidate: bool,
}

/// Why an expression did not compile.
#[derive(Debug, thiserror::Error)]
pub enum ExprCompileError<E> {
    /// Not a valid expression. Drives the Interpolator's longest-candidate-first retry; where
    /// no retry is possible it becomes a compile error.
    #[error("invalid expression: {message}")]
    Invalid { message: String },
    /// Any other failure (unknown expression type, unsupported construct, interpreter error).
    /// Aborts compilation unchanged.
    #[error("expression compiler error")]
    Other(#[source] E),
}

pub trait ExprCompiler {
    type Error;

    /// Parses and registers one expression. `source` is the expression text after entity
    /// decoding; `span` locates it in the template for error messages.
    fn compile(&mut self, source: &str, span: Span, cx: ExprContext) -> Result<ExprId, ExprCompileError<Self::Error>>;
}
