// The engine interface is ported from Chameleon's src/chameleon/compiler.py
// (`ExpressionEngine.parse`, `ExpressionCompiler.assign_value` / `assign_text` /
// `assign_bool`, `emit_bool`), modified: a custom expression type's sub-expressions are
// compiled natively and called from its Python code through `SubEval`.

//! Custom expression types (D11): a user's `expression_types` entry runs at compile time and
//! returns Python AST statements, which `chameleon_rs._bridge` turns into a function. The
//! sub-expressions it asked the engine for (`engine.parse(s).assign_value(target)`) are
//! compiled here, natively, and its code evaluates them through a [`SubEval`] handle.

use std::sync::Arc;

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::convert::convert_text;
use crate::expr::{Cx, Expr, FrameCx, RenderCtx, TextConv, eval};
use crate::scope::Scope;

/// What the custom code asked for a sub-expression.
#[derive(Debug)]
pub(crate) enum SubKind {
    /// `assign_value`: the value.
    Value,
    /// `assign_text`: converted like the engine converts text.
    Text(TextConv),
    /// `assign_bool(target, s)` (`emit_bool`): the marker becomes `default` (when the engine
    /// has the marker), a true value `s`, anything else `None`.
    Bool { s: Py<PyAny>, marker: bool, default: Option<Py<PyString>> },
}

#[derive(Debug)]
pub(crate) struct Sub {
    pub(crate) expr: Expr,
    pub(crate) kind: SubKind,
}

/// A compiled custom expression.
#[derive(Debug)]
pub(crate) struct Custom {
    /// `f(econtext, rcontext, default_marker, template_builtins, sub)`.
    pub(crate) func: Py<PyAny>,
    pub(crate) subs: Arc<[Sub]>,
}

impl Custom {
    pub(crate) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.func)?;
        for sub in self.subs.iter() {
            sub.expr.traverse(visit)?;
            match &sub.kind {
                SubKind::Value => {}
                SubKind::Text(conv) => {
                    if let Some(d) = &conv.default {
                        visit.call(d)?;
                    }
                }
                SubKind::Bool { s, default, .. } => {
                    visit.call(s)?;
                    if let Some(d) = default {
                        visit.call(d)?;
                    }
                }
            }
        }
        Ok(())
    }

    pub(crate) fn eval<'py>(&self, cx: &Cx<'_, 'py>) -> PyResult<Bound<'py, PyAny>> {
        let py = cx.scope.py();
        let rcontext = cx.rcontext();
        let sub = SubEval {
            subs: Arc::clone(&self.subs),
            scope: cx.scope.clone().unbind(),
            rcontext: rcontext.clone().unbind(),
            marker: cx.render.marker.clone().unbind(),
            defaults: cx.defaults().map(|d| d.clone().unbind()),
            builtins: cx.builtins().clone().unbind(),
            serial: cx.frame.serial,
            domain: cx.frame.domain.as_ref().map(|d| d.clone().unbind()),
            context: cx.frame.context.as_ref().map(|c| c.clone().unbind()),
            target_language: cx.frame.target_language.clone().unbind(),
        };
        self.func.bind(py).call1((cx.scope, rcontext, &cx.render.marker, cx.builtins(), sub))
    }
}

/// Evaluates a custom expression's native sub-expressions in the render that called it.
#[pyclass(frozen, module = "chameleon_rs._native")]
pub(crate) struct SubEval {
    subs: Arc<[Sub]>,
    scope: Py<Scope>,
    rcontext: Py<PyDict>,
    marker: Py<PyAny>,
    defaults: Option<Py<PyDict>>,
    builtins: Py<PyDict>,
    serial: u64,
    domain: Option<Py<PyString>>,
    context: Option<Py<PyString>>,
    target_language: Py<PyAny>,
}

#[pymethods]
impl SubEval {
    fn __call__<'py>(&self, py: Python<'py>, index: usize) -> PyResult<Bound<'py, PyAny>> {
        let sub = self.subs.get(index).ok_or_else(|| PyIndexError::new_err("sub-expression index"))?;
        let render = RenderCtx::new(
            self.marker.bind(py).clone(),
            self.defaults.as_ref().map(|d| d.bind(py).clone()),
            self.builtins.bind(py).clone(),
        );
        let _ = render.rcontext.set(self.rcontext.bind(py).clone());
        let scope = self.scope.bind(py);
        let frame = FrameCx {
            builtins: Some(self.builtins.bind(py).clone()),
            unit: 0,
            target: None,
            serial: self.serial,
            domain: self.domain.as_ref().map(|d| d.bind(py).clone()),
            context: self.context.as_ref().map(|c| c.bind(py).clone()),
            target_language: self.target_language.bind(py).clone(),
        };
        let cx = Cx { scope, render: &render, frame: &frame };
        let value = eval(&sub.expr, &cx)?;
        match &sub.kind {
            SubKind::Value => Ok(value),
            SubKind::Text(conv) => convert_text(&cx, &value, conv),
            SubKind::Bool { s, marker, default } => Ok(if *marker && value.is(&render.marker) {
                default.as_ref().map_or_else(|| py.None().into_bound(py), |d| d.bind(py).clone().into_any())
            } else if value.is_truthy()? {
                s.bind(py).clone()
            } else {
                py.None().into_bound(py)
            }),
        }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.scope)?;
        visit.call(&self.rcontext)?;
        visit.call(&self.marker)?;
        visit.call(&self.builtins)?;
        visit.call(&self.target_language)?;
        if let Some(d) = &self.domain {
            visit.call(d)?;
        }
        if let Some(c) = &self.context {
            visit.call(c)?;
        }
        if let Some(d) = &self.defaults {
            visit.call(d)?;
        }
        Ok(())
    }
}
