// Ported from Chameleon's src/chameleon/zpt/template.py (`Macro`, `Macros.__getitem__`,
// `PageTemplate.include`) and compiler.py (the `__fill_<name>` slot closures
// `visit_UseExternalMacro` defines), modified: native objects the VM runs as frames.

//! Macros and slot fills as Python objects (D3 item 4, D7).
//!
//! A [`Macro`] names a function of a compiled program (a `metal:define-macro`, or the main
//! render function for a template used as a macro) together with its template's builtins.
//! The VM runs one as a frame when a `metal:use-macro` expression evaluates to it; from
//! Python (or Chameleon's generated code) it is callable with Chameleon's `include`
//! signature. A [`SlotFill`] is a `metal:fill-slot` body as registered in the scope's slot
//! stacks (`econtext['__slot_<name>']`), with what Chameleon's closure captures.

use std::sync::Arc;

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit, intern};

use chameleon_core::{Done, Fill, I18n, VmError, VmState, run};

use crate::errors::{internal_error, vm_error};
use crate::expr::RenderCtx;
use crate::program::Program;
use crate::runtime::PyRuntime;
use crate::scope::Scope;

/// A macro of a compiled template (`template.macros['name']`, or `template.include` for the
/// whole template).
#[pyclass(frozen, module = "chameleon_rs._native")]
pub(crate) struct Macro {
    pub(crate) program: Py<Program>,
    pub(crate) entry: u32,
    /// The template's builtins (`template`, `macros`, `nothing`, ...), which the macro's
    /// expressions see wherever it is used.
    pub(crate) builtins: Py<PyDict>,
}

#[pymethods]
impl Macro {
    /// The macro whose function Chameleon names `render_<name>` (`name` already mangled as
    /// `Macros.__getitem__` does it), or the main render function when `name` is `None`.
    #[new]
    #[pyo3(signature = (program, name, builtins))]
    fn new(program: Bound<'_, Program>, name: Option<&str>, builtins: Bound<'_, PyDict>) -> PyResult<Self> {
        let entry = match name {
            None => 0,
            Some(name) => program
                .get()
                .core
                .macros()
                .iter()
                .rev()
                .find(|m| &*m.mangled == name)
                .map(|m| m.entry)
                .ok_or_else(|| PyKeyError::new_err(format!("Macro does not exist: '{name}'.")))?,
        };
        Ok(Self { program: program.unbind(), entry, builtins: builtins.unbind() })
    }

    /// Chameleon's `Macro.include`: the macro itself is the callable.
    #[getter]
    fn include<'py>(slf: &Bound<'py, Self>) -> Bound<'py, Self> {
        slf.clone()
    }

    /// `include(stream, econtext, rcontext, __i18n_domain=None, __i18n_context=None,
    /// target_language=None)`: renders the macro and appends the result to `stream`.
    #[pyo3(signature = (stream, econtext, rcontext, domain = None, context = None, target_language = None))]
    #[allow(clippy::too_many_arguments)] // Chameleon's signature
    fn __call__(
        slf: &Bound<'_, Self>,
        stream: &Bound<'_, PyAny>,
        econtext: &Bound<'_, PyAny>,
        rcontext: &Bound<'_, PyAny>,
        domain: Option<String>,
        context: Option<String>,
        target_language: Option<Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let py = slf.py();
        let m = slf.get();
        let i18n = I18n {
            domain: domain.map(Arc::from),
            context: context.map(Arc::from),
            target_language: target_language.map(Bound::unbind),
        };
        let program = m.program.bind(py);
        let builtins = m.builtins.bind(py).clone();
        let text = render_function(
            program,
            m.entry,
            slf.clone().into_any(),
            builtins,
            stream,
            econtext,
            rcontext,
            (i18n, None),
            Vec::new(),
        )?;
        stream.call_method1(pyo3::intern!(py, "append"), (text,))?;
        Ok(())
    }

    fn __repr__(&self, py: Python<'_>) -> String {
        let program = self.program.bind(py).get();
        let name = program.core.macros().iter().find(|m| m.entry == self.entry).map_or("<main>", |m| &*m.name);
        format!("<Macro {name:?} of {}>", program.filename.bind(py))
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.program)?;
        visit.call(&self.builtins)
    }
}

/// A `metal:fill-slot` body registered for a macro call (Chameleon's `__fill_<name>`
/// closure): its function, and what the closure captured where it was defined.
#[pyclass(frozen, module = "chameleon_rs._native")]
pub(crate) struct SlotFill {
    program: Arc<chameleon_core::Program>,
    code: Py<PyAny>,
    entry: u32,
    domain: Option<Arc<str>>,
    context: Option<Arc<str>>,
    target_language: Option<Py<PyAny>>,
    slots: Vec<Option<Py<PyAny>>>,
    stream: Option<u64>,
    /// The i18n settings of the render function around a fill defined in a slot body
    /// (`Fill::helpers`).
    helpers: Option<Box<I18n<Py<PyAny>>>>,
}

impl SlotFill {
    pub(crate) fn new(fill: Fill<Py<PyAny>>) -> Self {
        Self {
            program: fill.program,
            code: fill.code,
            entry: fill.entry,
            domain: fill.i18n.domain,
            context: fill.i18n.context,
            target_language: fill.i18n.target_language,
            slots: fill.slots,
            stream: fill.stream,
            helpers: fill.helpers,
        }
    }

    /// The fill, to run as a frame.
    pub(crate) fn fill(&self, py: Python<'_>) -> Fill<Py<PyAny>> {
        Fill {
            program: Arc::clone(&self.program),
            code: self.code.clone_ref(py),
            entry: self.entry,
            i18n: self.i18n(py),
            slots: self.slots.iter().map(|s| s.as_ref().map(|v| v.clone_ref(py))).collect(),
            stream: self.stream,
            helpers: self.helpers.as_ref().map(|h| Box::new(copy_i18n(py, h))),
        }
    }

    fn i18n(&self, py: Python<'_>) -> I18n<Py<PyAny>> {
        I18n {
            domain: self.domain.clone(),
            context: self.context.clone(),
            target_language: self.target_language.as_ref().map(|t| t.clone_ref(py)),
        }
    }

    /// The i18n settings the body's conversion helpers use.
    fn helpers(&self, py: Python<'_>) -> I18n<Py<PyAny>> {
        self.helpers.as_ref().map_or_else(|| self.i18n(py), |h| copy_i18n(py, h))
    }
}

fn copy_i18n(py: Python<'_>, i18n: &I18n<Py<PyAny>>) -> I18n<Py<PyAny>> {
    I18n {
        domain: i18n.domain.clone(),
        context: i18n.context.clone(),
        target_language: i18n.target_language.as_ref().map(|t| t.clone_ref(py)),
    }
}

#[pymethods]
impl SlotFill {
    /// `__fill_<name>(__stream, econtext, rcontext)`, as a foreign macro calls it: renders
    /// the body and appends the result to `stream`.
    fn __call__(
        &self,
        py: Python<'_>,
        stream: &Bound<'_, PyAny>,
        econtext: &Bound<'_, PyAny>,
        rcontext: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let code = self.code.bind(py).clone();
        let (program, builtins) = match code.cast::<Macro>() {
            Ok(m) => (m.get().program.bind(py).clone(), m.get().builtins.bind(py).clone()),
            Err(_) => (code.cast::<Program>()?.clone(), PyDict::new(py)),
        };
        let slots = self.slots.iter().map(|s| s.as_ref().map(|v| v.clone_ref(py))).collect();
        let i18n = (self.i18n(py), Some(self.helpers(py)));
        let text = render_function(&program, self.entry, code, builtins, stream, econtext, rcontext, i18n, slots)?;
        stream.call_method1(pyo3::intern!(py, "append"), (text,))?;
        Ok(())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.code)?;
        if let Some(t) = &self.target_language {
            visit.call(t)?;
        }
        if let Some(t) = self.helpers.as_ref().and_then(|h| h.target_language.as_ref()) {
            visit.call(t)?;
        }
        for slot in self.slots.iter().flatten() {
            visit.call(slot)?;
        }
        Ok(())
    }
}

/// Renders one function of `program` with the given scope (a copy, as the caller made it)
/// and `rcontext`, for a call from outside a native render. Errors propagate unformatted
/// (the caller's render formats them), after being recorded in `rcontext['__error__']`.
#[allow(clippy::too_many_arguments)]
fn render_function<'py>(
    program: &Bound<'py, Program>,
    entry: u32,
    code: Bound<'py, PyAny>,
    builtins: Bound<'py, PyDict>,
    stream: &Bound<'py, PyAny>,
    econtext: &Bound<'py, PyAny>,
    rcontext: &Bound<'py, PyAny>,
    (i18n, helpers): (I18n<Py<PyAny>>, Option<I18n<Py<PyAny>>>),
    slots: Vec<Option<Py<PyAny>>>,
) -> PyResult<Bound<'py, PyString>> {
    let py = program.py();
    let scope = match econtext.cast::<Scope>() {
        Ok(scope) => scope.clone(),
        // Another engine's scope: its items, as a mapping.
        Err(_) => Scope::new_root(py, &PyDict::from_sequence(&econtext.call_method0("items")?)?)?,
    };
    // Chameleon's render functions start with `translate = econtext['__translate']` and so on
    // (outside their `try`, so the error is not recorded).
    for key in [intern!(py, "__translate"), intern!(py, "__decode"), intern!(py, "__on_error_handler")] {
        if Scope::lookup(&scope, key.as_any())?.is_none() {
            return Err(PyKeyError::new_err((key.clone().unbind(),)));
        }
    }
    let render = RenderCtx::new(program.get().marker.bind(py).clone(), None, builtins);
    // Called from outside: the scope came from user code.
    render.scope_exposed.set(true);
    let _ = render.rcontext.set(rcontext.cast::<PyDict>()?.clone());
    let _ = render.stream.set(stream.clone());
    let compiled = Arc::clone(&program.get().core);
    let mut st = VmState::at(compiled, entry, code.unbind(), scope.clone().into_any().unbind(), i18n, slots);
    if let Some(helpers) = helpers {
        st = st.with_helpers(helpers);
    }
    let mut rt = PyRuntime::<false>::new(&render, program.clone(), scope);
    match run(&mut st, &mut rt) {
        Ok(Done::Finished) => {}
        Ok(Done::Suspended(_)) => return Err(internal_error(py, "a synchronous render suspended")),
        Err(VmError::Runtime(e)) => return Err(e),
        Err(e) => return Err(vm_error(py, e)),
    }
    crate::program::output_str(py, &st.out)
}
