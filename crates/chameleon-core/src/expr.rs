//! The compile-time expression interface (D1).
//!
//! The core decides *where* expressions are (TAL attributes, `${...}` interpolation) and in
//! what lexical context, but never parses them: an [`ExprCompiler`] supplied by the caller
//! does. `chameleon-py` implements it with CPython's `ast`; core tests use a toy one.

use crate::tok::Tok;

/// Index of a compiled expression in the caller's expression table.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ExprId(pub u32);

/// What the name `default` means where an expression appears. Chameleon makes it a
/// compile-time alias (`Alias(["default"], ...)` in a `Define`), not a scope lookup.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum DefaultAlias {
    /// An ordinary name.
    #[default]
    Name,
    /// The default marker: inside `tal:content` / `tal:replace` (the expression and the
    /// default content), dynamic attribute values, and `tal:case` (the value and its body).
    Marker,
    /// The value `target_language` had at the enclosing `i18n:target` element (its
    /// expression and body).
    TargetLanguage,
}

/// An element's static attributes, the value of its `attrs` alias.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StaticAttrs<'a> {
    /// Which element: Chameleon makes one dictionary per element (a compile-time `Static`),
    /// shared by all its expressions and by every render, so an implementation must return
    /// the same object for the same id.
    pub id: u32,
    /// Name to value as written, in order.
    pub items: &'a [(String, String)],
}

/// Where an expression appears, which changes how some names resolve.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ExprContext<'a> {
    pub default: DefaultAlias,
    /// `attrs` is a compile-time alias for the enclosing element's static attributes, or an
    /// ordinary name outside every element (`None`).
    pub attrs: Option<StaticAttrs<'a>>,
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
    /// Not a valid expression (Chameleon's `ExpressionError`). Drives the Interpolator's
    /// longest-candidate-first retry; where no retry is possible it becomes a compile error.
    /// `token` is the part the message is about (`None`: the whole source).
    #[error("invalid expression: {message}")]
    Invalid { message: String, token: Option<Tok> },
    /// Any other failure (unknown expression type, unsupported construct, interpreter error).
    /// Aborts compilation unchanged.
    #[error("expression compiler error")]
    Other(#[source] E),
}

pub trait ExprCompiler {
    type Error;

    /// Parses and registers one TALES expression. `source` is the expression text after
    /// entity decoding, with Chameleon's position (for error messages).
    fn compile(&mut self, source: &Tok, cx: ExprContext<'_>) -> Result<ExprId, ExprCompileError<Self::Error>>;

    /// Parses and registers a `<?python ... ?>` code block (statements, not an expression).
    fn compile_code(&mut self, source: &Tok) -> Result<ExprId, ExprCompileError<Self::Error>>;
}
