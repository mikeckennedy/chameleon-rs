//! The native `Program` (D7): a compiled template plus its expression table.

use std::sync::Arc;

use pyo3::exceptions::{PyRecursionError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyList, PyString};
use pyo3::{PyTraverseError, PyVisit, intern};

use chameleon_core::{Done, ErrorSite, Mode, Options, Output, VmError, VmState, run};

use crate::compiler::{Config, PyExprCompiler, TokenSpan};
use crate::errors::{catch_panics, compile_error, internal_error, vm_error};
use crate::expr::{RenderCtx, Site};
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

    /// Chameleon's marker is an `ImportableMarker` named `DEFAULT_MARKER` (it shows in a
    /// render error's arguments through `value_repr`).
    #[getter]
    #[allow(clippy::unused_self)] // a Python attribute
    fn __name__(&self) -> &'static str {
        "DEFAULT_MARKER"
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
    pub(crate) exprs: Vec<Site>,
    /// The core program's names, interned, indexed by `NameId`.
    pub(crate) names: Vec<Py<PyString>>,
    /// Whether a native partial frame running this program needs a `repeat` variable.
    pub(crate) needs_repeat: bool,
    pub(crate) token_table: Vec<TokenSpan>,
    /// The (normalized) source and filename, for error positions.
    source: Box<str>,
    pub(crate) filename: Py<PyString>,
    /// The object `default` evaluates to (the template's `default_marker`).
    pub(crate) marker: Py<PyAny>,
}

impl Program {
    /// An error token's text, line and column: Chameleon's `__tokens[__token]`, the source
    /// re-sliced at the token's code-point position for its length.
    /// The error token for an error the VM reports at `at` (Chameleon's `__token`): a nested
    /// token set while the last expression ran, else that expression's own token; an
    /// expression whose source has no position sets none, so then the one before counts.
    pub(crate) fn token_at(&self, at: ErrorSite, nested: (u32, u32)) -> u32 {
        for expr in [at.last, at.previous].into_iter().flatten() {
            if nested.0 == expr.0.wrapping_add(1) && nested.1 != 0 {
                return nested.1;
            }
            match self.exprs.get(expr.0 as usize) {
                Some(site) if site.token != 0 => return site.token,
                _ => {}
            }
        }
        0
    }

    pub(crate) fn token_info(&self, token: u32) -> Option<(&str, usize, usize)> {
        let span = self.token_table.get((token as usize).checked_sub(1)?)?;
        let text = self.source.get(span.start as usize..span.end as usize).unwrap_or_default();
        Some((text, span.line as usize, span.column as usize))
    }
}

#[pymethods]
impl Program {
    /// Compiles `source`. The keywords are Chameleon's template-level settings that change
    /// the program (see [`options`]), plus the expression configuration: `expression_types`
    /// (what `chameleon_rs.tales.native_types` returns), `default_expression`, `builtins`
    /// (the template builtins' names) and `fast_path` (`False` sends every Python expression
    /// to the fallback, for measurements).
    #[new]
    #[pyo3(signature = (
        source, filename = "<string>", *, expression_types = None, default_expression = None, builtins = None,
        fast_path = true, default_marker = None, token_lengths = None, **options
    ))]
    #[allow(clippy::too_many_arguments)] // Python keyword arguments
    fn new(
        py: Python<'_>,
        source: &str,
        filename: &str,
        expression_types: Option<&Bound<'_, PyAny>>,
        default_expression: Option<String>,
        builtins: Option<Vec<String>>,
        fast_path: bool,
        default_marker: Option<Bound<'_, PyAny>>,
        token_lengths: Option<Vec<usize>>,
        options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let options = self::options(options)?;
        let mut config = Config::page_template();
        if let Some(types) = expression_types {
            config.set_types(types)?;
        }
        if let Some(default) = default_expression {
            config.default_type = default;
        }
        if let Some(builtins) = builtins {
            config.template_builtins = builtins;
        }
        config.fast_path = fast_path;
        let marker = match default_marker {
            Some(m) => m,
            None => marker(py)?,
        };
        let tokens = token_lengths.map(|lengths| crate::elements::spans_from_lengths(source, &lengths)).transpose()?;
        catch_panics(py, || {
            let mut compiler = PyExprCompiler::new(py, marker.clone(), config, source, filename)?;
            let core = chameleon_core::compile_tokens(source, tokens, &mut compiler, &options)
                .map_err(|e| compile_error(py, e, source, filename))?;
            let names = core.names().iter().map(|n| PyString::intern(py, n).unbind()).collect();
            let has_loops = core.ops().iter().any(|op| matches!(op, chameleon_core::Op::LoopStart { .. }));
            Ok(Self {
                needs_repeat: has_loops || compiler.mentions_repeat,
                core: Arc::new(core),
                exprs: compiler
                    .exprs
                    .into_iter()
                    .zip(compiler.tokens)
                    .zip(compiler.has_nested)
                    .map(|((expr, token), nested)| Site { expr, token, nested })
                    .collect(),
                names,
                token_table: compiler.token_table,
                source: source.into(),
                filename: PyString::new(py, filename).unbind(),
                marker: marker.unbind(),
            })
        })
    }

    /// Renders with `variables` as the scope. `partials` is the prototype registry for the
    /// built-in `render_partial` (name -> `Program`); `defaults` holds the template's
    /// `__translate`/`__decode`/... for frames whose scope lacks them; `builtins` the template
    /// builtins' values (`template`, `macros`, `nothing`, ...).
    #[pyo3(signature = (variables, partials = None, defaults = None, builtins = None))]
    fn render<'py>(
        slf: &Bound<'py, Self>,
        variables: &Bound<'py, PyDict>,
        partials: Option<Bound<'py, PyDict>>,
        defaults: Option<Bound<'py, PyDict>>,
        builtins: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyString>> {
        let py = slf.py();
        let scope = Scope::new_root(py, variables)?;
        render_in(slf, &scope, partials, defaults, builtins)
    }

    /// Renders with `scope` itself as the scope, so names the template assigns stay in it
    /// (`ExpressionEvaluator` evaluates in the caller's scope, as Chameleon does).
    #[pyo3(signature = (scope, builtins = None))]
    fn render_scope<'py>(
        slf: &Bound<'py, Self>,
        scope: &Bound<'py, Scope>,
        builtins: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyString>> {
        render_in(slf, scope, None, None, builtins)
    }

    /// A readable listing of the bytecode.
    fn dump(&self) -> String {
        self.core.dump()
    }

    /// The macros' names as Chameleon's `Macros.names` lists them: the mangled function
    /// names (`render_<name>` without the prefix), each once, in definition order.
    #[getter]
    fn macro_names(&self) -> Vec<String> {
        let mut names: Vec<String> = Vec::new();
        for m in self.core.macros() {
            if !names.iter().any(|n| **n == *m.mangled) {
                names.push(m.mangled.to_string());
            }
        }
        names
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for site in &self.exprs {
            site.expr.traverse(&visit)?;
        }
        for n in &self.names {
            visit.call(n)?;
        }
        visit.call(&self.filename)?;
        visit.call(&self.marker)
    }
}

/// A render of `program` in `scope` (see [`Program::render`]).
fn render_in<'py>(
    slf: &Bound<'py, Program>,
    scope: &Bound<'py, Scope>,
    partials: Option<Bound<'py, PyDict>>,
    defaults: Option<Bound<'py, PyDict>>,
    builtins: Option<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyString>> {
    let py = slf.py();
    catch_panics(py, || {
        let program = slf.get();
        let builtins = builtins.unwrap_or_else(|| PyDict::new(py));
        let render = RenderCtx::new(program.marker.bind(py).clone(), partials, defaults, builtins);
        // `render(..., target_language=__kw.get("target_language"))`.
        // (`None` stays the VM's "no value", so frames without i18n settings stay plain.)
        let target_language =
            Scope::lookup(scope, intern!(py, "target_language").as_any())?.filter(|t| !t.is_none()).map(Bound::unbind);
        let mut st = VmState::new(
            Arc::clone(&program.core),
            slf.clone().into_any().unbind(),
            scope.clone().into_any().unbind(),
            target_language,
        );
        let mut rt = PyRuntime::new(&render, slf.clone(), scope.clone());
        match run(&mut st, &mut rt) {
            Ok(Done::Finished) => {}
            Ok(Done::Suspended(_)) => return Err(internal_error(py, "a synchronous render suspended")),
            Err(VmError::Runtime(e)) => return Err(render_error(py, e, scope, &render)),
            Err(e) => return Err(vm_error(py, e)),
        }
        program.core.record_size(st.out.len());
        output_str(py, &st.out)
    })
}

static RENDER_ERROR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// An error that ended the render (`BaseTemplate.render`'s handler, D8): a `RecursionError`
/// passes through; otherwise, if the frames recorded where it happened in
/// `rcontext['__error__']`, Chameleon's formatted `RenderError` subclass of its type is
/// raised instead (built in Python, `chameleon_rs.exc.render_error`, off the hot path).
fn render_error(py: Python<'_>, error: PyErr, econtext: &Bound<'_, Scope>, render: &RenderCtx<'_>) -> PyErr {
    if error.is_instance_of::<PyRecursionError>(py) {
        return error;
    }
    let Some(rcontext) = render.rcontext.get() else { return error };
    let errors = match rcontext.get_item(intern!(py, "__error__")) {
        Ok(Some(errors)) if errors.is_truthy().unwrap_or(false) => errors,
        _ => return error,
    };
    let formatted = RENDER_ERROR
        .get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import(intern!(py, "chameleon_rs.exc"))?.getattr(intern!(py, "render_error"))?.unbind())
        })
        .and_then(|f| f.bind(py).call1((error.value(py), errors, econtext, rcontext, &render.builtins)));
    match formatted {
        Ok(exc) => PyErr::from_value(exc),
        Err(e) => e,
    }
}

/// `rcontext.setdefault('__error__', []).append((token, line, column, filename, exc))`.
pub(crate) fn record_error(
    py: Python<'_>,
    rcontext: &Bound<'_, PyDict>,
    program: &Program,
    token: u32,
    error: &PyErr,
) -> PyResult<()> {
    let Some((text, line, column)) = program.token_info(token) else { return Ok(()) };
    let key = intern!(py, "__error__");
    let list = if let Some(list) = rcontext.get_item(key)? {
        list
    } else {
        let list = PyList::empty(py).into_any();
        rcontext.set_item(key, &list)?;
        list
    };
    list.call_method1(intern!(py, "append"), ((text, line, column, program.filename.bind(py), error.value(py)),))?;
    Ok(())
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
pub(crate) fn output_str<'py>(py: Python<'py>, out: &Output) -> PyResult<Bound<'py, PyString>> {
    if out.has_surrogates() {
        let bytes = PyBytes::new(py, out.as_bytes());
        return PyString::from_encoded_object(&bytes, Some(c"utf-8"), Some(c"surrogatepass"));
    }
    PyString::from_bytes(py, out.as_bytes())
}
