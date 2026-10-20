// The loop name handling follows Chameleon's src/chameleon/compiler.py (`visit_Repeat`,
// `_enter_assignment`, `_leave_assignment`), modified: implemented as a `Runtime` for the
// core VM.

//! The Python [`Runtime`] (D2): values are `Py<PyAny>`, errors are `PyErr` untouched.

use pyo3::exceptions::{PyNameError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyInt, PyIterator, PyList, PyString, PyTuple};

use chameleon_core::{Countdown, Emitted, Escape, ExprId, NameId, Output, Runtime, Step};

use crate::convert;
use crate::expr::{Cx, RenderCtx, eval, eval_emitted};
use crate::program::Program;
use crate::scope::Scope;

/// Rebuilt for every render (and, once async lands, every resume): it holds `Bound`
/// references, the VM state holds only `Py`.
pub(crate) struct PyRuntime<'a, 'py> {
    py: Python<'py>,
    render: &'a RenderCtx<'py>,
    code: Bound<'py, Program>,
    scope: Bound<'py, Scope>,
}

impl<'a, 'py> PyRuntime<'a, 'py> {
    pub(crate) fn new(render: &'a RenderCtx<'py>, code: Bound<'py, Program>, scope: Bound<'py, Scope>) -> Self {
        Self { py: code.py(), render, code, scope }
    }

    fn cx(&self) -> Cx<'_, 'py> {
        Cx { scope: &self.scope, render: self.render }
    }

    fn name(&self, name: NameId) -> PyResult<&Py<PyString>> {
        self.code.get().names.get(name.0 as usize).ok_or_else(|| PyValueError::new_err("unknown name id"))
    }
}

impl Runtime for PyRuntime<'_, '_> {
    type Value = Py<PyAny>;
    type Error = PyErr;

    fn activate(&mut self, code: &Py<PyAny>, scope: &Py<PyAny>) -> PyResult<()> {
        self.code = code.bind(self.py).cast::<Program>()?.clone();
        self.scope = scope.bind(self.py).cast::<Scope>()?.clone();
        Ok(())
    }

    #[inline]
    fn eval(&mut self, expr: ExprId, emitted: bool) -> PyResult<Step<Py<PyAny>>> {
        let program = self.code.get();
        let e = program.exprs.get(expr.0 as usize).ok_or_else(|| PyValueError::new_err("unknown expression id"))?;
        if emitted { eval_emitted(e, &self.cx()) } else { Ok(Step::Ready(eval(e, &self.cx())?.unbind())) }
    }

    #[inline]
    fn emit(&mut self, value: Py<PyAny>, esc: Escape, lone: bool, out: &mut Output) -> PyResult<Emitted> {
        convert::emit(&self.cx(), value.bind(self.py), esc, lone, out)
    }

    fn is_default(&mut self, value: &Py<PyAny>) -> bool {
        value.bind(self.py).is(&self.render.marker)
    }

    fn truthy(&mut self, value: &Py<PyAny>) -> PyResult<bool> {
        value.bind(self.py).is_truthy()
    }

    fn lookup(&mut self, name: NameId) -> PyResult<Option<Py<PyAny>>> {
        let key = self.name(name)?.bind(self.py);
        Ok(Scope::lookup(&self.scope, key.as_any())?.map(Bound::unbind))
    }

    fn set_null(&mut self, name: NameId) -> PyResult<()> {
        let key = self.name(name)?.bind(self.py);
        self.scope.as_super().set_item(key, self.py.None())
    }

    /// `__iterator, INDEX = getname('repeat')(key, iterable)`: whatever `repeat` is bound to,
    /// called with the name (or a tuple of names), its result unpacked like a Python
    /// assignment. The loop iterates `iter(__iterator)` (any iterable works, as in a `for`);
    /// the length stays a Python value unless it is an exact `int` that fits `i64`.
    fn repeat(&mut self, names: &[NameId], iterable: Py<PyAny>) -> PyResult<(Py<PyAny>, Countdown<Py<PyAny>>)> {
        let py = self.py;
        let repeat_name = intern!(py, "repeat");
        let repeat = Scope::lookup(&self.scope, repeat_name.as_any())?.ok_or_else(|| PyNameError::new_err("repeat"))?;
        let key = if let [one] = names {
            self.name(*one)?.bind(py).clone().into_any()
        } else {
            let keys = names.iter().map(|n| self.name(*n).map(|k| k.bind(py).clone())).collect::<PyResult<Vec<_>>>()?;
            PyTuple::new(py, keys)?.into_any()
        };
        let result = repeat.call1((key, iterable))?;
        let mut pair = unpack(&result, 2)?.into_iter();
        let (Some(iterable), Some(length)) = (pair.next(), pair.next()) else {
            return Err(PyValueError::new_err("not enough values to unpack (expected 2)"));
        };
        let iterator = iterable.try_iter()?.into_any().unbind();
        let index = match length.cast_exact::<PyInt>().ok().and_then(|n| n.extract::<i64>().ok()) {
            Some(n) => Countdown::Int(n),
            None => Countdown::Value(length.unbind()),
        };
        Ok((iterator, index))
    }

    /// `index -= 1; index > 0` with Python's arithmetic and comparison.
    fn countdown(&mut self, index: &Py<PyAny>) -> PyResult<(Py<PyAny>, bool)> {
        let next = index.bind(self.py).sub(1)?;
        let more = next.rich_compare(0, CompareOp::Gt)?.is_truthy()?;
        Ok((next.unbind(), more))
    }

    #[inline]
    fn next(&mut self, iterator: &Py<PyAny>) -> PyResult<Option<Py<PyAny>>> {
        let mut it = iterator.bind(self.py).cast::<PyIterator>()?.clone();
        it.next().transpose().map(|v| v.map(Bound::unbind))
    }

    #[inline]
    fn assign(&mut self, names: &[NameId], item: Py<PyAny>) -> PyResult<()> {
        let py = self.py;
        if let [one] = names {
            return self.scope.as_super().set_item(self.name(*one)?.bind(py), item);
        }
        let values = unpack(item.bind(py), names.len())?;
        for (name, value) in names.iter().zip(values) {
            self.scope.as_super().set_item(self.name(*name)?.bind(py), value)?;
        }
        Ok(())
    }

    /// `if backup is marker: del econtext[KEY] else: econtext[KEY] = backup`.
    fn restore(&mut self, name: NameId, backup: Option<Py<PyAny>>) -> PyResult<()> {
        let key = self.name(name)?.bind(self.py);
        match backup {
            Some(value) => self.scope.as_super().set_item(key, value),
            None => self.scope.as_super().del_item(key),
        }
    }
}

/// Python's sequence unpacking for `a, b = item`, with CPython's messages.
fn unpack<'py>(item: &Bound<'py, PyAny>, n: usize) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let sized = item.is_exact_instance_of::<PyTuple>() || item.is_exact_instance_of::<PyList>();
    let mut values = Vec::with_capacity(n);
    for value in item.try_iter()? {
        if values.len() == n {
            let message = if sized {
                format!("too many values to unpack (expected {n}, got {})", item.len()?)
            } else {
                format!("too many values to unpack (expected {n})")
            };
            return Err(PyValueError::new_err(message));
        }
        values.push(value?);
    }
    if values.len() < n {
        return Err(PyValueError::new_err(format!("not enough values to unpack (expected {n}, got {})", values.len())));
    }
    Ok(values)
}
