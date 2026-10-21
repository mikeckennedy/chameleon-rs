//! Compile-time errors (D8). Positions are byte spans; the Python layer converts them to
//! code-point offsets and `(line, column)` on the error path.

use crate::parser::{ParseError, ParseErrorKind};
use crate::source::Span;

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
    /// A bare `KeyError` (an undefined namespace prefix under `restricted_namespace`).
    Key,
    /// A bare `AttributeError` (Chameleon's parser fails this way on a tag with no name).
    Attribute,
    /// Valid Chameleon that this build does not support yet.
    Unsupported,
}

#[derive(Debug, thiserror::Error)]
pub enum CompileError<E> {
    #[error("{message}")]
    Template { kind: ErrorKind, message: String, span: Span },
    /// An error from the expression compiler that is not a syntax error, passed through
    /// unchanged (for `chameleon-py`, a `PyErr`).
    #[error("expression compiler error")]
    Expr(#[source] E),
    #[error("template source is larger than 4 GiB")]
    TooLarge,
}

impl<E> CompileError<E> {
    pub(crate) fn new(kind: ErrorKind, message: impl Into<String>, span: Span) -> Self {
        Self::Template { kind, message: message.into(), span }
    }

    pub(crate) fn unsupported(what: impl std::fmt::Display, span: Span) -> Self {
        Self::new(ErrorKind::Unsupported, format!("{what} is not supported yet by chameleon-rs"), span)
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
