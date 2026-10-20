//! The Python side of the spike VM: the frozen `Program` pyclass, the `PyRuntime`, and
//! experiment 6 (D9), the native awaitable that suspends the VM on a Python awaitable.

use std::sync::atomic::{AtomicUsize, Ordering};

use pyo3::exceptions::{PyRuntimeError, PyStopIteration, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use crate::expr::{self, Env, Escape, Expr};
use crate::scope::Scope;
use crate::vm::{self, Op, Runtime, Step, VmState};

#[pyclass(frozen, module = "chameleon_spike")]
pub(crate) struct Program {
    ops: Vec<Op>,
    exprs: Vec<Expr>,
    names: Vec<Py<PyString>>,
    capacity: AtomicUsize,
}

struct PyRuntime<'a, 'py> {
    program: &'a Program,
    env: Env<'py>,
    async_mode: bool,
}

impl Runtime for PyRuntime<'_, '_> {
    type Value = Py<PyAny>;
    type Error = PyErr;

    fn eval(&mut self, expr: usize) -> PyResult<Py<PyAny>> {
        expr::eval(&self.program.exprs[expr], &self.env).map(Bound::unbind)
    }

    fn is_awaitable(&mut self, value: &Py<PyAny>) -> bool {
        self.async_mode
            && value.bind(self.env.scope.py()).hasattr(intern!(self.env.scope.py(), "__await__")).unwrap_or(false)
    }

    fn emit(&mut self, value: Py<PyAny>, escape: Escape, out: &mut String) -> PyResult<()> {
        expr::emit(value.bind(self.env.scope.py()), escape, out)
    }

    fn iter_start(&mut self, value: Py<PyAny>) -> PyResult<Vec<Py<PyAny>>> {
        value.bind(self.env.scope.py()).try_iter()?.map(|item| item.map(Bound::unbind)).collect()
    }

    fn define(&mut self, name: usize, value: Py<PyAny>) -> PyResult<Option<Py<PyAny>>> {
        let key = self.program.names[name].bind(self.env.scope.py());
        let backup = Scope::lookup(&self.env.scope, key)?.map(Bound::unbind);
        self.env.scope.as_super().set_item(key, value)?;
        Ok(backup)
    }

    fn set(&mut self, name: usize, value: Py<PyAny>) -> PyResult<()> {
        self.env.scope.as_super().set_item(self.program.names[name].bind(self.env.scope.py()), value)
    }

    fn restore(&mut self, name: usize, backup: Option<Py<PyAny>>) -> PyResult<()> {
        let key = self.program.names[name].bind(self.env.scope.py());
        match backup {
            Some(value) => self.env.scope.as_super().set_item(key, value),
            None => self.env.scope.as_super().del_item(key),
        }
    }
}

fn compile_expr(py: Python<'_>, spec: &Bound<'_, PyAny>, exprs: &mut Vec<Expr>) -> PyResult<usize> {
    let expr = if let Ok(source) = spec.cast::<PyString>() {
        expr::compile(py, source.to_str()?)?
    } else {
        Expr::Fallback(spec.clone().unbind())
    };
    exprs.push(expr);
    Ok(exprs.len() - 1)
}

fn intern_name(py: Python<'_>, name: &str, names: &mut Vec<Py<PyString>>) -> usize {
    if let Some(i) = names.iter().position(|n| n.bind(py).to_str().is_ok_and(|s| s == name)) {
        return i;
    }
    names.push(PyString::intern(py, name).unbind());
    names.len() - 1
}

impl Program {
    fn env<'py>(py: Python<'py>, vars: &Bound<'py, PyDict>, builtins: &Bound<'py, PyDict>) -> PyResult<Env<'py>> {
        let scope = Scope::new_root(py, vars)?;
        let get_name = scope.getattr(intern!(py, "get_name"))?;
        let lookup_attr = wrap_pyfunction!(expr::py_lookup_attr, py)?;
        let fallback_args = PyTuple::new(py, [get_name, lookup_attr.into_any()])?;
        Ok(Env { scope, builtins: builtins.clone(), fallback_args })
    }
}

#[pymethods]
impl Program {
    /// `ops` is a list of tuples: ("text", s), ("emit", expr, "text"|"attr"),
    /// ("loop", name, expr, whitespace), ("endloop",). An `expr` is Python source (fast path)
    /// or a callable `f(getname, lookup_attr)` (fallback).
    #[new]
    fn new(py: Python<'_>, ops: &Bound<'_, PyList>) -> PyResult<Self> {
        let mut out = Vec::new();
        let mut exprs = Vec::new();
        let mut names = Vec::new();
        let mut open_loops = Vec::new();
        for spec in ops.iter() {
            let spec = spec.cast::<PyTuple>()?;
            let kind: String = spec.get_item(0)?.extract()?;
            match kind.as_str() {
                "text" => out.push(Op::Text(spec.get_item(1)?.extract()?)),
                "emit" => {
                    let expr = compile_expr(py, &spec.get_item(1)?, &mut exprs)?;
                    let escape = match spec.get_item(2)?.extract::<String>()?.as_str() {
                        "attr" => Escape::Attr,
                        _ => Escape::Text,
                    };
                    out.push(Op::Emit { expr, escape });
                }
                "loop" => {
                    let name = intern_name(py, &spec.get_item(1)?.extract::<String>()?, &mut names);
                    let iter = compile_expr(py, &spec.get_item(2)?, &mut exprs)?;
                    open_loops.push(out.len());
                    out.push(Op::Loop { name, iter, whitespace: spec.get_item(3)?.extract()?, end: 0 });
                }
                "endloop" => {
                    let start = open_loops.pop().ok_or_else(|| PyValueError::new_err("endloop without loop"))?;
                    let end_index = out.len();
                    if let Some(Op::Loop { end, .. }) = out.get_mut(start) {
                        *end = end_index;
                    }
                    out.push(Op::EndLoop { start });
                }
                other => return Err(PyValueError::new_err(format!("unknown op {other}"))),
            }
        }
        out.push(Op::End);
        Ok(Self { ops: out, exprs, names, capacity: AtomicUsize::new(0) })
    }

    /// Synchronous render: the whole page in one VM run.
    fn render<'py>(
        &self,
        py: Python<'py>,
        vars: &Bound<'py, PyDict>,
        builtins: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyString>> {
        let mut rt = PyRuntime { program: self, env: Self::env(py, vars, builtins)?, async_mode: false };
        let mut st = VmState::with_capacity(self.capacity.load(Ordering::Relaxed));
        match vm::run(&self.ops, &mut st, &mut rt, None)? {
            Step::Done => {}
            Step::Suspend(_) => return Err(PyRuntimeError::new_err("cannot suspend in a sync render")),
        }
        self.capacity.store(st.out.len(), Ordering::Relaxed);
        Ok(PyString::new(py, &st.out))
    }

    /// Async render: returns a native awaitable that suspends the VM on awaitable values.
    fn render_async(
        slf: &Bound<'_, Self>,
        vars: &Bound<'_, PyDict>,
        builtins: &Bound<'_, PyDict>,
    ) -> PyResult<RenderAwaitable> {
        let py = slf.py();
        let env = Self::env(py, vars, builtins)?;
        Ok(RenderAwaitable {
            program: slf.clone().unbind(),
            scope: env.scope.unbind(),
            builtins: builtins.clone().unbind(),
            fallback_args: env.fallback_args.unbind(),
            state: VmState::with_capacity(slf.get().capacity.load(Ordering::Relaxed)),
            inner: None,
            done: false,
        })
    }
}

/// A suspended render: `VmState<Py<PyAny>>` stored in a pyclass, driven like a coroutine.
/// Non-frozen; `PyO3`'s borrow flag rejects re-entrant `send` ("already executing").
#[pyclass(module = "chameleon_spike")]
pub(crate) struct RenderAwaitable {
    program: Py<Program>,
    scope: Py<Scope>,
    builtins: Py<PyDict>,
    fallback_args: Py<PyTuple>,
    state: VmState<Py<PyAny>>,
    /// The `__await__()` iterator of the awaitable we are suspended on.
    inner: Option<Py<PyAny>>,
    done: bool,
}

impl RenderAwaitable {
    /// One coroutine step: delegate to the inner iterator like `yield from`; when it finishes,
    /// resume the VM with its result. Returns the value to yield to the event loop, or raises
    /// StopIteration(rendered) when the render completes.
    fn step(
        &mut self,
        py: Python<'_>,
        sent: Option<Bound<'_, PyAny>>,
        mut thrown: Option<PyErr>,
    ) -> PyResult<Py<PyAny>> {
        if self.done {
            return Err(PyRuntimeError::new_err("cannot reuse already awaited render"));
        }
        let mut sent = sent;
        let mut resume: Option<Py<PyAny>> = None;
        loop {
            if let Some(inner) = self.inner.as_ref().map(|i| i.bind(py).clone()) {
                let result = match (thrown.take(), sent.take()) {
                    (Some(err), _) => match inner.getattr_opt(intern!(py, "throw"))? {
                        Some(throw) => throw.call1((err.into_value(py),)),
                        None => Err(err),
                    },
                    (None, Some(value)) if !value.is_none() => inner.call_method1(intern!(py, "send"), (value,)),
                    (None, _) => inner.call_method0(intern!(py, "__next__")),
                };
                match result {
                    Ok(yielded) => return Ok(yielded.unbind()),
                    Err(err) if err.is_instance_of::<PyStopIteration>(py) => {
                        self.inner = None;
                        resume = Some(err.value(py).getattr(intern!(py, "value"))?.unbind());
                    }
                    Err(err) => {
                        // A real VM would unwind its stack here (D3); the spike just stops.
                        self.inner = None;
                        self.done = true;
                        return Err(err);
                    }
                }
            } else if let Some(err) = thrown.take() {
                self.done = true;
                return Err(err);
            }

            let program = self.program.bind(py).get();
            let scope = self.scope.bind(py).clone();
            let env = Env {
                scope,
                builtins: self.builtins.bind(py).clone(),
                fallback_args: self.fallback_args.bind(py).clone(),
            };
            let mut rt = PyRuntime { program, env, async_mode: true };
            match vm::run(&program.ops, &mut self.state, &mut rt, resume.take()) {
                Ok(Step::Done) => {
                    self.done = true;
                    program.capacity.store(self.state.out.len(), Ordering::Relaxed);
                    let rendered = PyString::new(py, &self.state.out);
                    return Err(PyStopIteration::new_err((rendered.unbind(),)));
                }
                Ok(Step::Suspend(awaitable)) => {
                    self.inner = Some(awaitable.bind(py).call_method0(intern!(py, "__await__"))?.unbind());
                }
                Err(err) => {
                    self.done = true;
                    return Err(err);
                }
            }
        }
    }
}

#[pymethods]
impl RenderAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.step(py, None, None)
    }

    fn send(&mut self, py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        self.step(py, Some(value.clone()), None)
    }

    #[pyo3(signature = (typ, val=None, _tb=None))]
    fn throw(
        &mut self,
        py: Python<'_>,
        typ: &Bound<'_, PyAny>,
        val: Option<&Bound<'_, PyAny>>,
        _tb: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let err = match val {
            Some(val) if !val.is_none() => PyErr::from_value(val.clone()),
            _ => PyErr::from_value(typ.clone()),
        };
        self.step(py, None, Some(err))
    }

    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Some(inner) = self.inner.take()
            && let Some(close) = inner.bind(py).getattr_opt(intern!(py, "close"))?
        {
            close.call0()?;
        }
        self.done = true;
        Ok(())
    }
}
