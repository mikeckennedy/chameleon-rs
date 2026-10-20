//! The native `Program` (D7): a compiled template plus its expression table.

use std::cell::OnceCell;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit, intern};

use chameleon_core::{Done, Output, VmState, run};

use crate::compiler::PyExprCompiler;
use crate::errors::{catch_panics, compile_error, internal_error, vm_error};
use crate::expr::{Expr, RenderCtx};
use crate::runtime::PyRuntime;
use crate::scope::Scope;

/// The object `default` evaluates to inside `tal:content` and dynamic attributes.
#[pyclass(frozen, module = "chameleon_rs._native")]
pub(crate) struct DefaultMarker;

#[pymethods]
impl DefaultMarker {
    #[allow(clippy::unused_self)] // Python's protocol method takes self
    fn __repr__(&self) -> &'static str {
        "<default>"
    }
}

static MARKER: PyOnceLock<Py<DefaultMarker>> = PyOnceLock::new();
static REPEAT_DICT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

pub(crate) fn marker(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    Ok(MARKER.get_or_try_init(py, || Py::new(py, DefaultMarker))?.bind(py).clone().into_any())
}

/// A fresh `chameleon_rs.tal.RepeatDict({})`, as `PageTemplate.render` provides.
pub(crate) fn new_repeat_dict(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    let class = REPEAT_DICT.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.tal"))?.getattr(intern!(py, "RepeatDict"))?.unbind())
    })?;
    class.bind(py).call1((PyDict::new(py),))
}

/// An immutable compiled template. Frozen, so it is `Sync` and can be shared by threads
/// (free-threaded Python) and by partial frames.
#[pyclass(frozen, module = "chameleon_rs._native")]
pub(crate) struct Program {
    pub(crate) core: Arc<chameleon_core::Program>,
    pub(crate) exprs: Vec<Expr>,
    /// The core program's names, interned, indexed by `NameId`.
    pub(crate) names: Vec<Py<PyString>>,
    /// Whether a native partial frame running this program needs a `repeat` variable.
    pub(crate) needs_repeat: bool,
}

#[pymethods]
impl Program {
    #[new]
    #[pyo3(signature = (source, filename = "<string>"))]
    fn new(py: Python<'_>, source: &str, filename: &str) -> PyResult<Self> {
        catch_panics(py, || {
            let mut compiler = PyExprCompiler::new(py, marker(py)?)?;
            let core =
                chameleon_core::compile(source, &mut compiler).map_err(|e| compile_error(py, e, source, filename))?;
            let names = core.names().iter().map(|n| PyString::intern(py, n).unbind()).collect();
            let has_loops = core.ops().iter().any(|op| matches!(op, chameleon_core::Op::LoopStart { .. }));
            Ok(Self {
                needs_repeat: has_loops || compiler.mentions_repeat,
                core: Arc::new(core),
                exprs: compiler.exprs,
                names,
            })
        })
    }

    /// Renders with `variables` as the scope. `partials` is the prototype registry for the
    /// built-in `render_partial` (name -> `Program`); `defaults` holds the template's
    /// `__translate`/`__decode`/... for frames whose scope lacks them.
    #[pyo3(signature = (variables, partials = None, defaults = None))]
    fn render<'py>(
        slf: &Bound<'py, Self>,
        variables: &Bound<'py, PyDict>,
        partials: Option<Bound<'py, PyDict>>,
        defaults: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyString>> {
        let py = slf.py();
        catch_panics(py, || {
            let program = slf.get();
            let scope = Scope::new_root(py, variables)?;
            let render = RenderCtx { marker: marker(py)?, partials, defaults, rcontext: OnceCell::new() };
            let mut st = VmState::new(
                Arc::clone(&program.core),
                slf.clone().into_any().unbind(),
                scope.clone().into_any().unbind(),
            );
            let mut rt = PyRuntime::new(&render, slf.clone(), scope);
            match run(&mut st, &mut rt) {
                Ok(Done::Finished) => {}
                Ok(Done::Suspended(_)) => return Err(internal_error(py, "a synchronous render suspended")),
                Err(e) => return Err(vm_error(py, e)),
            }
            program.core.record_size(st.out.len());
            output_str(py, &st.out)
        })
    }

    /// A readable listing of the bytecode.
    fn dump(&self) -> String {
        self.core.dump()
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for e in &self.exprs {
            e.traverse(&visit)?;
        }
        for n in &self.names {
            visit.call(n)?;
        }
        Ok(())
    }
}

/// Builds the final `str` once: a plain UTF-8 decode, or `surrogatepass` if some value had
/// lone surrogates (D6).
fn output_str<'py>(py: Python<'py>, out: &Output) -> PyResult<Bound<'py, PyString>> {
    if out.has_surrogates() {
        let bytes = PyBytes::new(py, out.as_bytes());
        return PyString::from_encoded_object(&bytes, Some(c"utf-8"), Some(c"surrogatepass"));
    }
    PyString::from_bytes(py, out.as_bytes())
}
