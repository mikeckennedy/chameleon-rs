//! Compile-time errors (D8). Positions are byte offsets plus code points ([`TokPos`]); the
//! Python layer converts them to code-point offsets and `(line, column)` on the error path.

use crate::parser::{ParseError, ParseErrorKind};
use crate::source::Span;
use crate::tok::{Tok, TokPos};

/// Which of Chameleon's exception classes an error corresponds to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// `chameleon.exc.ParseError`
    Parse,
    /// `chameleon.exc.CompilationError` (for example a bad attribute for a namespace)
    Compilation,
    /// `chameleon.exc.LanguageError`
    Language,
    /// `chameleon.exc.ExpressionError`
    Expression,
    /// `chameleon.exc.TranslationError`
    Translation,
    /// A bare `KeyError` (an undefined namespace prefix under `restricted_namespace`, an
    /// unknown prefix in a `data-` attribute).
    Key,
    /// A bare `AttributeError` (Chameleon's parser fails this way on a tag with no name).
    Attribute,
    /// A bare `AssertionError` with no message: Chameleon asserts in a few places that a
    /// template can reach (`tal:repeat` with several clauses, a `tal:case` whose switch has
    /// not been evaluated yet).
    Assertion,
    /// A bare `ValueError` (an entity reference `int()` or `chr()` rejects).
    Value,
    /// A bare `TypeError` (Chameleon applies a regex to `None` for a tag without a suffix
    /// under `trim_attribute_space`).
    Type,
    /// Valid Chameleon that this build does not support yet.
    Unsupported,
}

/// The token an error points at: Chameleon's `TemplateError` reports its text, its `pos`
/// as `offset`, and `(line, column)` computed from `pos`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ErrToken {
    /// A token of the source at its own position.
    Span(Span),
    /// A derived token (see [`Tok`]); without a position it is a plain `str`.
    Tok(Tok),
}

impl ErrToken {
    /// The token's text and position.
    #[must_use]
    pub fn resolve(&self, src: &str) -> (String, Option<TokPos>) {
        match self {
            Self::Span(span) => (span.text(src).to_owned(), Some(TokPos::at(span.start))),
            Self::Tok(tok) => (tok.text.clone(), tok.pos),
        }
    }
}

impl From<Span> for ErrToken {
    fn from(span: Span) -> Self {
        Self::Span(span)
    }
}

impl From<Tok> for ErrToken {
    fn from(tok: Tok) -> Self {
        Self::Tok(tok)
    }
}

impl From<&Tok> for ErrToken {
    fn from(tok: &Tok) -> Self {
        Self::Tok(tok.clone())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum CompileError<E> {
    #[error("{message}")]
    Template { kind: ErrorKind, message: String, token: ErrToken },
    /// An error from the expression compiler that is not a syntax error, passed through
    /// unchanged (for `chameleon-py`, a `PyErr`).
    #[error("expression compiler error")]
    Expr(#[source] E),
    #[error("template source is larger than 4 GiB")]
    TooLarge,
}

impl<E> CompileError<E> {
    pub(crate) fn new(kind: ErrorKind, message: impl Into<String>, token: impl Into<ErrToken>) -> Self {
        Self::Template { kind, message: message.into(), token: token.into() }
    }

    pub(crate) fn unsupported(what: impl std::fmt::Display, token: impl Into<ErrToken>) -> Self {
        Self::new(ErrorKind::Unsupported, format!("{what} is not supported yet by chameleon-rs"), token)
    }

    /// The error's kind, if it is a template error.
    #[must_use]
    pub fn kind(&self) -> Option<ErrorKind> {
        match self {
            Self::Template { kind, .. } => Some(*kind),
            _ => None,
        }
    }
}

impl<E> From<ParseError> for CompileError<E> {
    fn from(e: ParseError) -> Self {
        let kind = match e.kind {
            ParseErrorKind::Syntax => ErrorKind::Parse,
            ParseErrorKind::UndefinedPrefix => ErrorKind::Key,
            ParseErrorKind::NoTagName => ErrorKind::Attribute,
        };
        Self::new(kind, e.message, e.span)
    }
}
