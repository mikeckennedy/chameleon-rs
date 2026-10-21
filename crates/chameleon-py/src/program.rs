//! The native `Program` (D7): a compiled template plus its expression table.

use std::cell::OnceCell;
use std::sync::Arc;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit, intern};

use chameleon_core::{Done, Mode, Options, Output, VmState, run};

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
        // Chameleon's `DEFAULT_MARKER` (an `ImportableMarker` named `DEFAULT`): its `str()` is
        // what a template writes when the marker is converted as an ordinary object.
        "<DEFAULT>"
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
    /// Compiles `source`. The keywords are Chameleon's template-level settings that change
    /// the program (see [`options`]).
    #[new]
    #[pyo3(signature = (source, filename = "<string>", **options))]
    fn new(py: Python<'_>, source: &str, filename: &str, options: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        let options = self::options(options)?;
        catch_panics(py, || {
            let mut compiler = PyExprCompiler::new(py, marker(py)?)?;
            let core = chameleon_core::compile(source, &mut compiler, &options)
                .map_err(|e| compile_error(py, e, source, filename))?;
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

/// `Options` from keyword arguments: `mode` (`"xml"` or `"text"`), `escape` (default: true in
/// XML mode), `boolean_attributes`, `implicit_i18n_translate`, `implicit_i18n_attributes`,
/// `trim_attribute_space`, `enable_data_attributes`, `enable_comment_interpolation`,
/// `restricted_namespace`, `strict`. `None` values mean the default, like Chameleon's
/// `_pop_defaults`.
pub(crate) fn options(kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<Options> {
    let mut o = Options::default();
    let mut escape = None;
    let Some(kwargs) = kwargs else { return Ok(o) };
    for (key, value) in kwargs.iter() {
        if value.is_none() {
            continue;
        }
        let key: String = key.extract()?;
        match key.as_str() {
            "mode" => {
                o.mode = match value.extract::<String>()?.as_str() {
                    "xml" => Mode::Xml,
                    "text" => Mode::Text,
                    other => return Err(PyValueError::new_err(format!("unknown mode {other:?}"))),
                }
            }
            "escape" => escape = Some(value.is_truthy()?),
            "boolean_attributes" => o.boolean_attributes = strings(&value)?,
            "implicit_i18n_translate" => o.implicit_i18n_translate = value.is_truthy()?,
            "implicit_i18n_attributes" => o.implicit_i18n_attributes = strings(&value)?,
            "trim_attribute_space" => o.trim_attribute_space = value.is_truthy()?,
            "enable_data_attributes" => o.enable_data_attributes = value.is_truthy()?,
            "enable_comment_interpolation" => o.enable_comment_interpolation = value.is_truthy()?,
            "restricted_namespace" => o.restricted_namespace = value.is_truthy()?,
            "strict" => o.strict = value.is_truthy()?,
            other => return Err(PyTypeError::new_err(format!("unexpected template option {other:?}"))),
        }
    }
    o.escape = escape.unwrap_or(o.mode == Mode::Xml);
    Ok(o)
}

/// The strings of any iterable (a set's order does not matter: only membership is used).
fn strings(value: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    value.try_iter()?.map(|v| v?.extract::<String>()).collect()
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
