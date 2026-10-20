//! Converting core errors and panics to the Python exceptions in `chameleon_rs.exc` (D8).

use std::panic::{AssertUnwindSafe, catch_unwind};

use pyo3::exceptions::{PyRecursionError, PyRuntimeError};
use pyo3::intern;
use pyo3::prelude::*;

use chameleon_core::{CompileError, ErrorKind, VmError, char_offset, location};

fn exc_class<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(intern!(py, "chameleon_rs.exc"))?.getattr(name)
}

/// `chameleon_rs.exc.UnsupportedError` for valid Chameleon this build cannot handle yet.
pub(crate) fn unsupported_error(py: Python<'_>, what: &str) -> PyErr {
    let message = format!("{what} is not supported yet by chameleon-rs");
    match exc_class(py, "UnsupportedError").and_then(|cls| cls.call1((message,))) {
        Ok(exc) => PyErr::from_value(exc),
        Err(e) => e,
    }
}

/// Converts a compile error, computing the code-point offset and `(line, column)` only now.
pub(crate) fn compile_error(py: Python<'_>, err: CompileError<PyErr>, src: &str, filename: &str) -> PyErr {
    match err {
        CompileError::Expr(e) => e,
        CompileError::TooLarge => PyRuntimeError::new_err("template source is larger than 4 GiB"),
        CompileError::Template { kind, message, span } => {
            let class = match kind {
                ErrorKind::Parse => "ParseError",
                ErrorKind::Compilation => "CompilationError",
                ErrorKind::Language => "LanguageError",
                ErrorKind::Expression => "ExpressionError",
                ErrorKind::Translation => "TranslationError",
                ErrorKind::Unsupported => "UnsupportedError",
            };
            let token = span.text(src);
            let offset = char_offset(src, span.start as usize);
            let loc = location(src, span.start as usize);
            match exc_class(py, class).and_then(|cls| cls.call1((message, token, offset, loc, filename))) {
                Ok(exc) => PyErr::from_value(exc),
                Err(e) => e,
            }
        }
    }
}

pub(crate) fn vm_error(py: Python<'_>, err: VmError<PyErr>) -> PyErr {
    match err {
        VmError::Runtime(e) => e,
        VmError::Recursion => PyRecursionError::new_err("maximum recursion depth exceeded while rendering a template"),
        VmError::Internal(what) => internal_error(py, what),
    }
}

pub(crate) fn internal_error(py: Python<'_>, what: &str) -> PyErr {
    let message = format!("internal error in chameleon-rs: {what}");
    match exc_class(py, "InternalError").and_then(|cls| cls.call1((message,))) {
        Ok(exc) => PyErr::from_value(exc),
        Err(e) => e,
    }
}

/// Runs `f`, turning a Rust panic into `chameleon_rs.exc.InternalError` (a `RuntimeError`)
/// instead of PyO3's `PanicException`, which `except Exception` handlers miss. Continuing is
/// safe: programs are immutable and render state is per call.
pub(crate) fn catch_panics<T>(py: Python<'_>, f: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(result) => result,
        Err(payload) => {
            let what = payload
                .downcast_ref::<&str>()
                .map(|s| (*s).to_owned())
                .or_else(|| payload.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "panic".to_owned());
            Err(internal_error(py, &what))
        }
    }
}
